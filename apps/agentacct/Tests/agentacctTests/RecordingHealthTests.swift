import XCTest
@testable import agentacct

@MainActor
final class RecordingHealthTests: XCTestCase {
    func testReachableRecorderAndHealthyImportDoNotConfirmClientCapture() throws {
        let snapshot = try project(ingestion: healthy(), clients: ["codex", "claude-code"])
        XCTAssertEqual(snapshot.title, "Recorder reachable")
        XCTAssertEqual(snapshot.clients.filter(\.confirmed).count, 0)
        XCTAssertEqual(snapshot.dimensions.first { $0.id == "capture" }?.value, "0 of 2 confirmed")
        XCTAssertTrue(snapshot.causes.isEmpty)
    }

    func testSetupCommandCompletionStillWaitsForCapture() throws {
        let snapshot = try project(setup: .done, ingestion: healthy(), clients: ["codex"])
        XCTAssertEqual(snapshot.title, "Waiting for client capture")
        XCTAssertEqual(snapshot.clients.first?.confirmed, false)
        XCTAssertEqual(snapshot.dimensions.first { $0.id == "setup" }?.value, "Setup command finished")
    }

    func testCaptureRequiresMatchingClientImmutableIDAndStrictEpisodeBoundary() throws {
        let boundary = Date(timeIntervalSince1970: 100)
        let snapshot = try project(
            ingestion: healthy(), clients: ["codex", "claude-code", "opencode"],
            captures: [
                .init(clientID: "codex", eventID: "fresh", observedAt: boundary.addingTimeInterval(1)),
                .init(clientID: "claude-code", eventID: "old", observedAt: boundary),
                .init(clientID: "opencode", eventID: "", observedAt: boundary.addingTimeInterval(1))
            ],
            boundaries: ["codex": boundary, "claude-code": boundary, "opencode": boundary]
        )
        XCTAssertEqual(snapshot.clients.filter(\.confirmed).map(\.id), ["codex"])
        let noBoundary = try project(ingestion: healthy(), clients: ["codex"], captures: [
            .init(clientID: "codex", eventID: "historical", observedAt: boundary)
        ])
        XCTAssertEqual(noBoundary.clients.first?.confirmed, false)
    }

    func testFailedIngestionRefreshSupersedesRetainedHealthySnapshot() throws {
        let snapshot = try project(ingestion: healthy(), error: "current fetch failed")
        XCTAssertEqual(snapshot.dimensions.first { $0.id == "imports" }?.value, "Current health unavailable")
        XCTAssertFalse(snapshot.resolutionScopes.contains(.ingestion))
        XCTAssertEqual(snapshot.causes.map(\.id), ["ingestion:unavailable"])
    }

    func testKnownGlobalConflictGroupsSummariesButOtherSourceFaultsStayScoped() {
        let issues = ["codex", "claude-code", "opencode", "openclaw", "hermes", "cursor"].map {
            V1IngestionIssue(code: "evidence_refreshable_usage_failed", source: $0, action: "Inspect evidence")
        } + [
            .init(code: "source_identity_unresolved", source: "codex", action: "Inspect identity"),
            .init(code: "source_identity_unresolved", source: "claude-code", action: "Inspect identity")
        ]
        let causes = RecordingHealthSnapshot.groupedIssues(issues + [issues[0]])
        XCTAssertEqual(causes.count, 3)
        let global = causes.first { $0.id == "ingestion:evidence_refreshable_usage_failed" }
        XCTAssertEqual(global?.affectedSources.count, 6)
        XCTAssertTrue(global?.detail.contains("One reconciliation fault") == true)
    }

    func testSingleGlobalIssueWithAffectedSourcesGroupsTheSameWay() {
        let causes = RecordingHealthSnapshot.groupedIssues([
            .init(code: "evidence_refreshable_usage_failed", source: nil, action: "Refresh usage",
                  affectedSources: ["codex", "claude-code", "hermes"])
        ])
        XCTAssertEqual(causes.count, 1)
        XCTAssertEqual(causes.first?.affectedSources, ["claude-code", "codex", "hermes"])
        XCTAssertEqual(causes.first?.title, "Usage totals may be incomplete")
        XCTAssertTrue(causes.first?.detail.contains("3 sources") == true)
    }

    func testDismissalAndRepeatedObservationPreserveOneActiveEpisode() throws {
        let coordinator = RecordingHealthCoordinator()
        let fault = try project(phase: .disconnected("offline"))
        coordinator.update(fault)
        let id = try XCTUnwrap(coordinator.visibleNotices.first?.id)
        coordinator.dismiss(id)
        coordinator.update(fault)
        XCTAssertTrue(coordinator.visibleNotices.isEmpty)
        XCTAssertEqual(coordinator.notices.count, 1)
        XCTAssertEqual(coordinator.activeCauseIDs, ["endpoint:unreachable"])
        XCTAssertEqual(fault.title, "Recorder unreachable")
    }

    func testUnknownIngestionCannotAnnounceRecoveryForExistingCoverageFault() throws {
        let coordinator = RecordingHealthCoordinator()
        coordinator.update(try project(ingestion: conflict()))
        coordinator.update(try project(ingestion: conflict(), error: "fetch unavailable"))
        XCTAssertTrue(coordinator.recentRecoveries.isEmpty)
        XCTAssertTrue(coordinator.activeCauseIDs.contains("ingestion:evidence_refreshable_usage_failed"))
    }

    func testConnectionRecoveryDoesNotResolveCoverageFaultOrConfirmCapture() throws {
        let coordinator = RecordingHealthCoordinator()
        coordinator.update(try project(ingestion: conflict()))
        coordinator.update(try project(phase: .disconnected("offline"), ingestion: conflict()))
        let reconnect = try project(ingestion: conflict(), clients: ["codex"])
        coordinator.update(reconnect)
        XCTAssertEqual(coordinator.recentRecoveries.map(\.cause.id), ["endpoint:unreachable"])
        XCTAssertEqual(coordinator.recentRecoveries.first?.title, "Recorder connection restored")
        XCTAssertTrue(coordinator.activeCauseIDs.contains("ingestion:evidence_refreshable_usage_failed"))
        XCTAssertFalse(reconnect.clients[0].confirmed)
    }

    func testRecoveryIsObservableAfterDismissalAndLaterOutageGetsNewEpisode() throws {
        let coordinator = RecordingHealthCoordinator()
        let fault = try project(phase: .disconnected("offline"))
        coordinator.update(fault)
        let firstID = try XCTUnwrap(coordinator.visibleNotices.first?.id)
        coordinator.dismiss(firstID)
        coordinator.update(try project(ingestion: healthy()))
        XCTAssertEqual(coordinator.visibleNotices.first?.isRecovered, true)
        coordinator.update(fault)
        let latest = try XCTUnwrap(coordinator.visibleNotices.first)
        XCTAssertNotEqual(latest.id, firstID)
        XCTAssertFalse(latest.isRecovered)
        XCTAssertEqual(coordinator.notices.count, 2)
    }

    func testUnknownWatcherStateDoesNotResolveStoppedWatcher() throws {
        let coordinator = RecordingHealthCoordinator()
        coordinator.update(try project(ingestion: healthy(watcher: "stopped")))
        coordinator.update(try project(ingestion: healthy(watcher: nil)))
        XCTAssertTrue(coordinator.recentRecoveries.isEmpty)
        XCTAssertTrue(coordinator.activeCauseIDs.contains("ingestion:watcher"))
        coordinator.update(try project(ingestion: healthy()))
        XCTAssertEqual(coordinator.recentRecoveries.map(\.cause.id), ["ingestion:watcher"])
    }

    func testMissingIssueAssessmentDoesNotClaimNoIssuesOrResolveFault() throws {
        let coordinator = RecordingHealthCoordinator()
        coordinator.update(try project(ingestion: conflict()))
        let unknown = V1IngestionSnapshot(state: "healthy", lastSuccessAt: nil, sources: nil, watcher: nil, issues: nil)
        let snapshot = try project(ingestion: unknown)
        coordinator.update(snapshot)
        XCTAssertEqual(snapshot.dimensions.first { $0.id == "coverage" }?.value, "Not assessed")
        XCTAssertTrue(coordinator.recentRecoveries.isEmpty)
    }

    func testSetupRetryDoesNotClearFailureUntilCommandCompletes() throws {
        let coordinator = RecordingHealthCoordinator()
        coordinator.update(try project(setup: .failed("settings could not be written")))
        coordinator.update(try project(setup: .working("Trying again")))
        XCTAssertTrue(coordinator.recentRecoveries.isEmpty)
        coordinator.update(try project(setup: .done))
        XCTAssertEqual(coordinator.recentRecoveries.map(\.cause.id), ["setup:failed"])
    }

    func testRepeatedFailureHidesPreviousRecoveryWithoutLosingHistory() throws {
        let coordinator = RecordingHealthCoordinator()
        let failed = try project(phase: .disconnected("connection refused"), ingestion: healthy())
        coordinator.update(failed, now: Date(timeIntervalSince1970: 10))
        let firstID = try XCTUnwrap(coordinator.visibleNotices.first?.id)
        coordinator.update(try project(ingestion: healthy()), now: Date(timeIntervalSince1970: 20))
        XCTAssertTrue(try XCTUnwrap(coordinator.visibleNotices.first).isRecovered)
        coordinator.update(failed, now: Date(timeIntervalSince1970: 21))
        let renewedID = try XCTUnwrap(coordinator.visibleNotices.first?.id)
        XCTAssertNotEqual(renewedID, firstID)
        XCTAssertFalse(try XCTUnwrap(coordinator.visibleNotices.first).isRecovered)
        XCTAssertEqual(coordinator.visibleNotices.map(\.id), [renewedID])
        coordinator.update(failed, now: Date(timeIntervalSince1970: 22))
        XCTAssertEqual(coordinator.visibleNotices.map(\.id), [renewedID])
        XCTAssertEqual(coordinator.recentRecoveries.map(\.id), [firstID])
    }

    func testRepeatedSourceRecoveriesCoalesceWhileKeepingBoundedHistory() throws {
        let coordinator = RecordingHealthCoordinator()
        let failed = try project(ingestion: sourceFaults(["claude-code"]))
        let recovered = try project(ingestion: healthy())
        for episode in 0..<25 {
            let startedAt = Date(timeIntervalSince1970: Double(episode) / 4)
            coordinator.update(failed, now: startedAt)
            XCTAssertEqual(coordinator.visibleNotices.count, 1)
            XCTAssertFalse(try XCTUnwrap(coordinator.visibleNotices.first).isRecovered)
            coordinator.update(recovered, now: startedAt.addingTimeInterval(0.1))
            XCTAssertEqual(coordinator.visibleNotices.count, 1)
            XCTAssertTrue(try XCTUnwrap(coordinator.visibleNotices.first).isRecovered)
        }
        XCTAssertEqual(coordinator.notices.count, 20)
        XCTAssertEqual(coordinator.recentRecoveries.count, 5)
        XCTAssertEqual(coordinator.visibleNotices.first?.id, coordinator.recentRecoveries.first?.id)
        let latestID = try XCTUnwrap(coordinator.visibleNotices.first?.id)
        coordinator.dismiss(latestID)
        XCTAssertTrue(coordinator.visibleNotices.isEmpty)
        XCTAssertEqual(coordinator.notices.count, 20)
        XCTAssertEqual(coordinator.recentRecoveries.first?.id, latestID)
        XCTAssertNil(coordinator.nextRecoveryDismissalAt)
    }

    func testRecoveryExpiresAfterTenSecondsWithoutRefreshExtendingDeadline() throws {
        let coordinator = RecordingHealthCoordinator()
        let failed = try project(ingestion: sourceFaults(["claude-code"]))
        let recovered = try project(ingestion: healthy())
        let recoveredAt = Date(timeIntervalSince1970: 100)
        coordinator.update(failed, now: recoveredAt.addingTimeInterval(-1))
        coordinator.update(recovered, now: recoveredAt)
        let recoveryID = try XCTUnwrap(coordinator.visibleNotices.first?.id)
        let deadline = recoveredAt.addingTimeInterval(10)
        XCTAssertEqual(RecordingHealthCoordinator.recoveryNoticeDuration, 10)
        XCTAssertEqual(coordinator.nextRecoveryDismissalAt, deadline)

        coordinator.update(recovered, now: recoveredAt.addingTimeInterval(8))
        XCTAssertEqual(coordinator.nextRecoveryDismissalAt, deadline)
        coordinator.dismissExpiredRecoveries(now: deadline.addingTimeInterval(-0.001))
        XCTAssertEqual(coordinator.visibleNotices.map(\.id), [recoveryID])
        coordinator.dismissExpiredRecoveries(now: deadline)
        XCTAssertTrue(coordinator.visibleNotices.isEmpty)
        XCTAssertNil(coordinator.nextRecoveryDismissalAt)
        XCTAssertEqual(coordinator.recentRecoveries.first?.id, recoveryID)
        XCTAssertEqual(coordinator.recentRecoveries.first?.recoveredAt, recoveredAt)

        coordinator.update(recovered, now: deadline.addingTimeInterval(1))
        XCTAssertTrue(coordinator.visibleNotices.isEmpty)
        coordinator.update(failed, now: deadline.addingTimeInterval(2))
        XCTAssertEqual(coordinator.visibleNotices.count, 1)
        XCTAssertFalse(try XCTUnwrap(coordinator.visibleNotices.first).isRecovered)
        XCTAssertNotEqual(coordinator.visibleNotices.first?.id, recoveryID)
        XCTAssertNil(coordinator.nextRecoveryDismissalAt)
    }

    func testIndependentRecoveriesExpireAtTheirOwnEarliestDeadline() throws {
        let coordinator = RecordingHealthCoordinator()
        let firstRecoveryAt = Date(timeIntervalSince1970: 100)
        coordinator.update(try project(ingestion: sourceFaults(["claude-code", "codex"])), now: firstRecoveryAt.addingTimeInterval(-1))
        coordinator.update(try project(ingestion: sourceFaults(["codex"])), now: firstRecoveryAt)
        let firstID = try XCTUnwrap(coordinator.visibleNotices.first { $0.isRecovered }?.id)
        coordinator.update(try project(ingestion: healthy()), now: firstRecoveryAt.addingTimeInterval(5))
        XCTAssertEqual(coordinator.visibleNotices.count, 2)
        XCTAssertEqual(coordinator.nextRecoveryDismissalAt, firstRecoveryAt.addingTimeInterval(10))

        coordinator.dismissExpiredRecoveries(now: firstRecoveryAt.addingTimeInterval(10))
        XCTAssertEqual(coordinator.visibleNotices.map(\.cause.affectedSources), [["codex"]])
        XCTAssertEqual(coordinator.nextRecoveryDismissalAt, firstRecoveryAt.addingTimeInterval(15))
        XCTAssertTrue(coordinator.recentRecoveries.contains { $0.id == firstID })
        coordinator.dismissExpiredRecoveries(now: firstRecoveryAt.addingTimeInterval(15))
        XCTAssertTrue(coordinator.visibleNotices.isEmpty)
        XCTAssertEqual(coordinator.recentRecoveries.count, 2)
        XCTAssertNil(coordinator.nextRecoveryDismissalAt)
    }

    func testManuallyDismissingRecoveryAdvancesDeadlineWithoutReappearing() throws {
        let coordinator = RecordingHealthCoordinator()
        let firstRecoveryAt = Date(timeIntervalSince1970: 100)
        coordinator.update(try project(ingestion: sourceFaults(["claude-code", "codex"])), now: firstRecoveryAt.addingTimeInterval(-1))
        coordinator.update(try project(ingestion: sourceFaults(["codex"])), now: firstRecoveryAt)
        let firstID = try XCTUnwrap(coordinator.visibleNotices.first { $0.isRecovered }?.id)
        coordinator.update(try project(ingestion: healthy()), now: firstRecoveryAt.addingTimeInterval(5))
        coordinator.dismiss(firstID)
        XCTAssertEqual(coordinator.nextRecoveryDismissalAt, firstRecoveryAt.addingTimeInterval(15))
        XCTAssertEqual(coordinator.visibleNotices.map(\.cause.affectedSources), [["codex"]])
        coordinator.update(try project(ingestion: healthy()), now: firstRecoveryAt.addingTimeInterval(6))
        XCTAssertEqual(coordinator.visibleNotices.count, 1)
        XCTAssertEqual(coordinator.recentRecoveries.count, 2)
    }

    func testRecoveryExpiryDoesNotDismissAnActiveFault() throws {
        let coordinator = RecordingHealthCoordinator()
        let recoveredAt = Date(timeIntervalSince1970: 100)
        coordinator.update(try project(ingestion: sourceFaults(["claude-code"])), now: recoveredAt.addingTimeInterval(-1))
        coordinator.update(try project(ingestion: healthy()), now: recoveredAt)
        coordinator.update(try project(phase: .disconnected("offline")), now: recoveredAt.addingTimeInterval(1))
        XCTAssertEqual(coordinator.visibleNotices.count, 2)
        coordinator.dismissExpiredRecoveries(now: recoveredAt.addingTimeInterval(60))
        XCTAssertEqual(coordinator.visibleNotices.map(\.cause.id), ["endpoint:unreachable"])
        XCTAssertEqual(coordinator.activeCauseIDs, ["endpoint:unreachable"])
        XCTAssertEqual(coordinator.recentRecoveries.count, 1)
        XCTAssertNil(coordinator.nextRecoveryDismissalAt)
    }

    func testSourceRecoveryTitleIdentifiesClientAndOriginalIssue() throws {
        let snapshot = try project(ingestion: sourceFaults(["claude-code"], code: "source_changed_during_scan"))
        let cause = try XCTUnwrap(snapshot.causes.first)
        XCTAssertEqual(cause.recoveryTitle, "Claude Code: Source Changed During Scan no longer reported")
        let multiSourceCause = try XCTUnwrap(RecordingHealthSnapshot.groupedIssues([
            .init(code: "source_scan_failed", source: nil, action: "Retry", affectedSources: ["codex", "claude-code"])
        ]).first)
        XCTAssertEqual(multiSourceCause.recoveryTitle, "Claude Code, Codex: Source Scan Failed no longer reported")
        let unnamedCause = try XCTUnwrap(RecordingHealthSnapshot.groupedIssues([
            .init(code: "scan_stuck", source: nil, action: "Retry")
        ]).first)
        XCTAssertEqual(unnamedCause.recoveryTitle, "Scan Stuck no longer reported")
        let endpoint = try XCTUnwrap(project(phase: .disconnected("offline")).causes.first)
        XCTAssertEqual(endpoint.recoveryTitle, "Recorder connection restored")
    }

    func testUnreachableRecorderIsTheOnlyCauseThatOffersARestart() throws {
        let offline = try project(phase: .disconnected("connection refused"))
        let cause = try XCTUnwrap(offline.causes.first)
        XCTAssertEqual(cause.id, "endpoint:unreachable")
        XCTAssertTrue(cause.isRecorderUnreachable)
        // A coverage/reconciliation fault is a different remedy (diagnostics or
        // setup), never a one-click recorder restart.
        let coverage = try project(ingestion: conflict())
        XCTAssertFalse(coverage.causes.isEmpty)
        XCTAssertTrue(coverage.causes.allSatisfy { !$0.isRecorderUnreachable })
    }

    private func project(
        phase: GlanceState.Phase? = nil,
        setup: SetupModel.Phase = .idle,
        ingestion: V1IngestionSnapshot? = nil,
        error: String? = nil,
        clients: [String] = [],
        captures: [RecordingCaptureObservation] = [],
        boundaries: [String: Date] = [:]
    ) throws -> RecordingHealthSnapshot {
        let glance = try JSONDecoder().decode(Glance.self, from: Data("""
        {"schema":"agentacct.glance.v1","usage":{"windows":[]},"limits":[],"plan":[],"recent_sessions":[]}
        """.utf8))
        return RecordingHealthSnapshot.project(
            glancePhase: phase ?? .connected(.init(glance: glance, daemonVersion: "test")),
            setupPhase: setup, ingestion: ingestion, ingestionError: error,
            configuredClientIDs: clients, captures: captures, requiredCaptureAfter: boundaries
        )
    }

    private func healthy(watcher: String? = "running") -> V1IngestionSnapshot {
        .init(state: "healthy", lastSuccessAt: 100, sources: [], watcher: .init(state: watcher, intervalSeconds: 30, heartbeatAt: 100), issues: [])
    }

    private func conflict() -> V1IngestionSnapshot {
        .init(state: "degraded", lastSuccessAt: 100, sources: [], watcher: .init(state: "running", intervalSeconds: 30, heartbeatAt: 100), issues: [
            .init(code: "evidence_refreshable_usage_failed", source: "codex", action: "Inspect evidence")
        ])
    }

    private func sourceFaults(_ sources: [String], code: String = "source_scan_failed") -> V1IngestionSnapshot {
        .init(state: "degraded", lastSuccessAt: 100, sources: [], watcher: .init(state: "running", intervalSeconds: 30, heartbeatAt: 100), issues: sources.map {
            .init(code: code, source: $0, action: "Retry this source")
        })
    }
}
