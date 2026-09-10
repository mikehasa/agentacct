import AppKit
import Foundation

/// The single boundary for UI actions that can mount or refresh local data.
/// Callers supply the process-owned lifecycle wait; the operation cannot begin
/// until recorder synchronization has completed or failed safely.
@MainActor
func performAfterRecorderSynchronization(
    awaitReady: () async -> SetupModel.AutomaticUpgradeOutcome,
    operation: () async -> Void
) async -> Bool {
    let outcome = await awaitReady()
    guard !Task.isCancelled else { return false }
    guard case .failed = outcome else {
        await operation()
        return true
    }
    return false
}

/// Presenting the recovery UI is always safe and must remain possible after a
/// failed synchronization. Only the local-data refresh stays behind the gate.
@MainActor
func presentWindowThenRefreshAfterRecorderSynchronization(
    awaitReady: () async -> SetupModel.AutomaticUpgradeOutcome,
    presentWindow: () -> Void,
    refresh: () async -> Void
) async -> Bool {
    presentWindow()
    return await performAfterRecorderSynchronization(
        awaitReady: awaitReady,
        operation: refresh
    )
}

/// Owns recorder synchronization for the process, independently of whether a
/// menu or main window is ever opened. Glance polling is deliberately held
/// until CLI synchronization either succeeds, is unnecessary, or fails safely.
@MainActor
final class AppLifecycleCoordinator {
    typealias UpgradeOutcome = SetupModel.AutomaticUpgradeOutcome

    let setup: SetupModel
    let glance: GlanceState
    private let synchronizeCLI: () async -> UpgradeOutcome
    private let retryCLI: () async -> UpgradeOutcome
    private let startPolling: () -> Void
    private var startupTask: Task<UpgradeOutcome, Never>?
    private var pollingStarted = false
    private(set) var latestOutcome: UpgradeOutcome?

    init() {
        let setup = SetupModel()
        let glance = GlanceState(startImmediately: false)
        self.setup = setup
        self.glance = glance
        synchronizeCLI = { await setup.upgradeInstalledCLIIfNeeded() }
        retryCLI = {
            setup.reset()
            await setup.setUp()
            switch setup.phase {
            case .done:
                return .upgraded
            case .failed(let message):
                return .failed(message)
            case .idle, .working:
                return .failed("Recorder synchronization did not finish.")
            }
        }
        startPolling = { glance.start() }
    }

    init(
        setup: SetupModel,
        glance: GlanceState,
        synchronizeCLI: @escaping () async -> UpgradeOutcome,
        retryCLI: (() async -> UpgradeOutcome)? = nil,
        startPolling: @escaping () -> Void
    ) {
        self.setup = setup
        self.glance = glance
        self.synchronizeCLI = synchronizeCLI
        self.retryCLI = retryCLI ?? synchronizeCLI
        self.startPolling = startPolling
    }

    func start() {
        guard startupTask == nil else { return }
        startupTask = Task { [weak self] in
            guard let self else { return .notNeeded }
            let outcome = await synchronizeCLI()
            finishSynchronization(outcome)
            return outcome
        }
    }

    func waitUntilReady() async -> UpgradeOutcome {
        start()
        guard let startupTask else { return .notNeeded }
        return await startupTask.value
    }

    /// Retry only a failed startup gate. Concurrent callers share the same
    /// retry task, and polling starts exactly once after the first success.
    func retrySynchronization() async -> UpgradeOutcome {
        if latestOutcome == nil, let startupTask {
            return await startupTask.value
        }
        guard let previousOutcome = latestOutcome else { return .notNeeded }
        guard case .failed = previousOutcome else { return previousOutcome }
        latestOutcome = nil
        let task = Task { [weak self] in
            guard let self else { return UpgradeOutcome.notNeeded }
            let outcome = await retryCLI()
            finishSynchronization(outcome)
            return outcome
        }
        startupTask = task
        return await task.value
    }

    private func finishSynchronization(_ outcome: UpgradeOutcome) {
        latestOutcome = outcome
        guard case .failed = outcome else {
            if !pollingStarted {
                pollingStarted = true
                startPolling()
            }
            return
        }
    }
}

@MainActor
final class AgentacctAppDelegate: NSObject, NSApplicationDelegate {
    let lifecycle = AppLifecycleCoordinator()

    func applicationDidFinishLaunching(_ notification: Notification) {
        lifecycle.start()
    }
}
