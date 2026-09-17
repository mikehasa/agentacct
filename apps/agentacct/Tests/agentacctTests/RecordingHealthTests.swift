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

    func testRepeatedFailurePrecedesUndismissedRecoveryWithoutLosingHistory() throws {
        let coordinator = RecordingHealthCoordinator()
        let failed = try project(phase: .disconnected("connection refused"), ingestion: healthy())
        coordinator.update(failed, now: Date(timeIntervalSince1970: 10))
        let firstID = try XCTUnwrap(coordinator.visibleNotices.first?.id)
        coordinator.update(try project(ingestion: healthy()), now: Date(timeIntervalSince1970: 20))
        XCTAssertTrue(try XCTUnwrap(coordinator.visibleNotices.first).isRecovered)
        coordinator.update(failed, now: Date(timeIntervalSince1970: 30))
        let renewedID = try XCTUnwrap(coordinator.visibleNotices.first?.id)
        XCTAssertNotEqual(renewedID, firstID)
        XCTAssertFalse(try XCTUnwrap(coordinator.visibleNotices.first).isRecovered)
        XCTAssertEqual(coordinator.visibleNotices.last?.id, firstID)
        coordinator.update(failed, now: Date(timeIntervalSince1970: 40))
        XCTAssertEqual(coordinator.visibleNotices.map(\.id), [renewedID, firstID])
        XCTAssertEqual(coordinator.recentRecoveries.map(\.id), [firstID])
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
}
