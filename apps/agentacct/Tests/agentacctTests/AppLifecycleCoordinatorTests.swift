import AppKit
import SwiftUI
import XCTest
@testable import agentacct

final class AppLifecycleCoordinatorTests: XCTestCase {
    @MainActor
    func testWindowStartupDoesNotCancelItsInitialDashboardRefresh() async throws {
        // Exercise the actual SwiftUI task identity transition, not just the
        // async refresh-loop helper. The first .task can run after its sibling
        // changes readiness, even though it still belongs to the old identity.
        let store = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let previousStore = ProcessInfo.processInfo.environment["AGENTACCT_STORE_DIR"]
        setenv("AGENTACCT_STORE_DIR", store.path, 1)
        let previousSnapshotMode = SnapshotMode.enabled
        SnapshotMode.enabled = false
        defer {
            SnapshotMode.enabled = previousSnapshotMode
            if let previousStore { setenv("AGENTACCT_STORE_DIR", previousStore, 1) }
            else { unsetenv("AGENTACCT_STORE_DIR") }
        }

        let setup = SetupModel(preloaded: .idle, log: [])
        let glance = GlanceState(preloadedPhase: .connecting)
        let lifecycle = AppLifecycleCoordinator(
            setup: setup, glance: glance,
            synchronizeCLI: { .notNeeded }, startPolling: {}
        )
        // AppDelegate can finish its process-owned startup before the window's
        // tasks are scheduled (the common already-installed recorder path).
        _ = await lifecycle.waitUntilReady()
        let completed = expectation(description: "first dashboard refresh publishes without waiting for the minute timer")
        var refreshing = false
        var starts = 0
        var skipped = 0
        var cancellations = 0
        let root = MainWindow(setup: setup, lifecycle: lifecycle, dashboardRefresh: {
            // Match DashboardStore's single-flight gate and cancellable HTTP.
            guard !refreshing else { skipped += 1; return }
            refreshing = true
            starts += 1
            defer { refreshing = false }
            do { try await Task.sleep(for: .milliseconds(150)) }
            catch { cancellations += 1; return }
            completed.fulfill()
        })
        .environment(DashboardStore())
        .environment(glance)
        .environment(AppSelection())
        let window = NSWindow(
            contentRect: NSRect(x: -6000, y: -6000, width: 1180, height: 820),
            styleMask: [.borderless], backing: .buffered, defer: false
        )
        window.isReleasedWhenClosed = false
        let hosting = NSHostingView(rootView: root)
        hosting.sizingOptions = []
        window.contentView = hosting
        window.orderFrontRegardless()
        defer { window.close() }
        await fulfillment(of: [completed], timeout: 3)
        XCTAssertEqual(starts, 1)
        XCTAssertEqual(skipped, 0, "the replacement task must not skip a cancelled refresh then sleep for a minute")
        XCTAssertEqual(cancellations, 0, "startup must not cancel the only dashboard request")
    }

    @MainActor
    func testWindowRefreshLoopStartsAfterRecoveryAndKeepsRefreshing() async {
        var refreshes = 0
        var ticks = 0
        await refreshLocalDataWhileReady(
            ready: false,
            snapshotMode: false,
            waitForNextRefresh: { XCTFail("failed synchronization must not start a timer") },
            refresh: { refreshes += 1 }
        )
        XCTAssertEqual(refreshes, 0)

        // MainWindow's keyed task runs again when successful retry changes
        // recorderSynchronizationFinished from false to true.
        await refreshLocalDataWhileReady(
            ready: true,
            snapshotMode: false,
            waitForNextRefresh: {
                ticks += 1
                if ticks == 3 { throw CancellationError() }
            },
            refresh: { refreshes += 1 }
        )
        XCTAssertEqual(refreshes, 3)

        await refreshLocalDataWhileReady(
            ready: true,
            snapshotMode: true,
            waitForNextRefresh: { XCTFail("snapshots must not start a timer") },
            refresh: { XCTFail("snapshots must not read local data") }
        )
    }

    @MainActor
    func testMenuOnlyLaunchSynchronizesCLIBeforeStartingAnyGlancePoll() async {
        let setup = SetupModel(preloaded: .idle, log: [])
        let glance = GlanceState(preloadedPhase: .connecting)
        let gate = AsyncStream<Void>.makeStream()
        var events: [String] = []
        let coordinator = AppLifecycleCoordinator(
            setup: setup,
            glance: glance,
            synchronizeCLI: {
                events.append("sync-started")
                for await _ in gate.stream { break }
                events.append("sync-finished")
                return .notNeeded
            },
            startPolling: {
                events.append("polling-started")
            }
        )

        // This is the AppDelegate path: no MainWindow or menu content exists.
        coordinator.start()
        for _ in 0..<10 where events.isEmpty { await Task.yield() }
        XCTAssertEqual(events, ["sync-started"])

        gate.continuation.yield(())
        gate.continuation.finish()
        let outcome = await coordinator.waitUntilReady()

        XCTAssertEqual(outcome, .notNeeded)
        XCTAssertEqual(events, ["sync-started", "sync-finished", "polling-started"])
        XCTAssertEqual(coordinator.latestOutcome, .notNeeded)
    }

    @MainActor
    func testWorkSelectionDoesNotMountLocalDataPaneBeforeSynchronization() {
        let selection = AppSelection()
        selection.open(.work)

        XCTAssertEqual(selection.pane, .work)
        XCTAssertFalse(
            localDataPaneCanMount(
                selection.pane,
                recorderSynchronizationFinished: false,
                snapshotMode: false
            )
        )
        XCTAssertTrue(
            localDataPaneCanMount(
                selection.pane,
                recorderSynchronizationFinished: true,
                snapshotMode: false
            )
        )
    }

    @MainActor
    func testMenuOpensRecoveryWindowBeforeRecorderSynchronizationThenRefreshesAfterSuccess() async {
        let gate = AsyncStream<Void>.makeStream()
        var events: [String] = []
        let action = Task {
            await presentWindowThenRefreshAfterRecorderSynchronization(
                awaitReady: {
                    events.append("sync-started")
                    for await _ in gate.stream { break }
                    events.append("sync-finished")
                    return .notNeeded
                },
                presentWindow: {
                    events.append("selection-applied")
                    events.append("window-opened")
                },
                refresh: {
                    events.append("dashboard-refreshed")
                }
            )
        }

        for _ in 0..<10 where events.isEmpty { await Task.yield() }
        XCTAssertEqual(events, ["selection-applied", "window-opened", "sync-started"])

        gate.continuation.yield(())
        gate.continuation.finish()
        _ = await action.value
        XCTAssertEqual(events, [
            "selection-applied", "window-opened", "sync-started",
            "sync-finished", "dashboard-refreshed",
        ])
    }

    @MainActor
    func testMenuStillOpensRecoveryWindowWhenSynchronizationFailsWithoutRefreshing() async {
        var events: [String] = []

        let refreshed = await presentWindowThenRefreshAfterRecorderSynchronization(
            awaitReady: {
                events.append("sync-failed")
                return .failed("unsafe recorder")
            },
            presentWindow: {
                events.append("selection-applied")
                events.append("window-opened")
            },
            refresh: {
                events.append("dashboard-refreshed")
            }
        )

        XCTAssertFalse(refreshed)
        XCTAssertEqual(events, ["selection-applied", "window-opened", "sync-failed"])
    }

    @MainActor
    func testManualRefreshWaitsForRecorderSynchronization() async {
        let gate = AsyncStream<Void>.makeStream()
        var events: [String] = []
        let action = Task {
            await performAfterRecorderSynchronization(
                awaitReady: {
                    events.append("sync-started")
                    for await _ in gate.stream { break }
                    events.append("sync-finished")
                    return .notNeeded
                },
                operation: {
                    events.append("glance-refreshed")
                    events.append("dashboard-refreshed")
                }
            )
        }

        for _ in 0..<10 where events.isEmpty { await Task.yield() }
        XCTAssertEqual(events, ["sync-started"])

        gate.continuation.yield(())
        gate.continuation.finish()
        _ = await action.value
        XCTAssertEqual(events, [
            "sync-started", "sync-finished", "glance-refreshed", "dashboard-refreshed",
        ])
    }

    @MainActor
    func testSynchronizationFailureStartsNoPollingAndRunsNoLocalDataOperation() async {
        let setup = SetupModel(preloaded: .idle, log: [])
        let glance = GlanceState(preloadedPhase: .connecting)
        var events: [String] = []
        let failure = SetupModel.AutomaticUpgradeOutcome.failed("unsafe recorder")
        let coordinator = AppLifecycleCoordinator(
            setup: setup,
            glance: glance,
            synchronizeCLI: {
                events.append("sync-failed")
                return failure
            },
            retryCLI: {
                events.append("sync-retried")
                return .upgraded
            },
            startPolling: {
                events.append("polling-started")
            }
        )

        let outcome = await coordinator.waitUntilReady()
        let operationRan = await performAfterRecorderSynchronization(
            awaitReady: { await coordinator.waitUntilReady() },
            operation: { events.append("local-data-operation") }
        )

        XCTAssertEqual(outcome, failure)
        XCTAssertFalse(operationRan)
        XCTAssertEqual(events, ["sync-failed"])
        XCTAssertEqual(coordinator.latestOutcome, failure)
        XCTAssertFalse(
            localDataPaneCanMount(
                .dashboard,
                recorderSynchronizationFinished: false,
                snapshotMode: false
            )
        )

        let retryOutcome = await coordinator.retrySynchronization()
        let operationRanAfterRetry = await performAfterRecorderSynchronization(
            awaitReady: { await coordinator.waitUntilReady() },
            operation: { events.append("local-data-operation") }
        )

        XCTAssertEqual(retryOutcome, .upgraded)
        XCTAssertTrue(operationRanAfterRetry)
        XCTAssertEqual(events, [
            "sync-failed", "sync-retried", "polling-started", "local-data-operation",
        ])
        XCTAssertEqual(coordinator.latestOutcome, .upgraded)
    }
}
