import XCTest
@testable import agentacct

final class SourcesPresentationTests: XCTestCase {
    func testGlobalReconciliationGroupsSourcesWithoutDroppingWatcherOrIdentityFault() {
        let clients = ["codex", "claude-code", "opencode", "hermes", "openclaw", "cursor"]
        let globals = clients.map {
            V1IngestionIssue(code: "evidence_refreshable_usage_failed", source: $0, action: "Inspect shared reconciliation")
        }
        let groups = SourceIssueGroup.group(globals + [
            .init(code: "watcher_stale", source: nil, action: "Restart usage watch."),
            .init(code: "source_identity_unresolved", source: "claude-code", action: "Inspect client identity")
        ])

        XCTAssertEqual(groups.count, 3)
        XCTAssertEqual(groups.first?.affectedSources, clients.sorted())
        XCTAssertEqual(groups.first?.issues.count, 6)
        XCTAssertEqual(groups.filter { !$0.isGlobalReconciliation }.flatMap(\.issues).compactMap(\.code), [
            "watcher_stale", "source_identity_unresolved"
        ])
        XCTAssertEqual(groups.flatMap(\.issues).count, 8)
    }

    func testSimilarWordsAndUnknownCodesDoNotEstablishSharedCause() {
        let issues: [V1IngestionIssue] = [
            .init(code: "source_scan_failed", source: "codex", action: "Reconciliation failed"),
            .init(code: "source_scan_failed", source: "hermes", action: "Reconciliation failed"),
            .init(code: nil, source: nil, action: "Reconciliation failed")
        ]

        let groups = SourceIssueGroup.group(issues)

        XCTAssertEqual(groups.count, 3)
        XCTAssertTrue(groups.allSatisfy { !$0.isGlobalReconciliation && $0.issues.count == 1 })
    }

    func testDuplicateSourceReportsRemainInspectableAndDoNotInflateAffectedSourceCount() {
        let duplicate = V1IngestionIssue(code: "evidence_refreshable_usage_failed", source: "codex", action: "First diagnostic")
        let groups = SourceIssueGroup.group([
            duplicate,
            .init(code: "source_scan_failed", source: "codex", action: "Read failed"),
            .init(code: "evidence_refreshable_usage_failed", source: "codex", action: "Second diagnostic"),
            .init(code: "source_scan_failed", source: "codex", action: "Parse failed")
        ])

        XCTAssertEqual(groups.count, 3)
        XCTAssertEqual(Set(groups.map(\.id)).count, 3)
        XCTAssertEqual(groups.first?.affectedSources, ["codex"])
        XCTAssertEqual(groups.first?.issues.compactMap(\.action), ["First diagnostic", "Second diagnostic"])
        XCTAssertEqual(groups.flatMap(\.issues).count, 4)
    }

    func testSingleGlobalIssueNamesItsAffectedSources() {
        let groups = SourceIssueGroup.group([
            .init(code: "evidence_refreshable_usage_failed", source: nil, action: "Refresh usage",
                  affectedSources: ["codex", "claude-code"])
        ])

        XCTAssertEqual(groups.count, 1)
        XCTAssertEqual(groups.first?.isGlobalReconciliation, true)
        XCTAssertEqual(groups.first?.affectedSources, ["claude-code", "codex"])
        XCTAssertEqual(groups.first?.issues.count, 1)
    }

    func testAffectedSourcesDecodeFromSnakeCase() throws {
        let json = #"{"code":"evidence_refreshable_usage_failed","source":null,"action":"Refresh usage","affected_sources":["codex","hermes"]}"#
        let issue = try JSONDecoder().decode(V1IngestionIssue.self, from: Data(json.utf8))
        XCTAssertEqual(issue.affectedSources, ["codex", "hermes"])
        XCTAssertEqual(issue.namedSources, ["codex", "hermes"])
    }

    func testHeaderChipYieldsOnlyWhenEveryRowDisplaysTheHeaderWord() throws {
        let reporting = try JSONDecoder().decode([V1IngestionSource].self, from: Data("""
        [{"source": "codex", "state": "healthy", "parsed": 980}, {"source": "claude-code", "state": "healthy", "parsed": 1200}]
        """.utf8))
        // Every row says Reporting and so would the header: the header yields.
        XCTAssertTrue(SourcesPane.rowsShareState(reporting, overall: "healthy", watcherRunning: true))
        // A single row never hides the header.
        XCTAssertFalse(SourcesPane.rowsShareState(Array(reporting.prefix(1)), overall: "healthy", watcherRunning: true))

        let mixed = try JSONDecoder().decode([V1IngestionSource].self, from: Data("""
        [{"source": "codex", "state": "healthy", "parsed": 980}, {"source": "hermes", "state": "healthy", "parsed": 0}]
        """.utf8))
        // Same raw state, different words (Reporting vs Watching): the header stays.
        XCTAssertFalse(SourcesPane.rowsShareState(mixed, overall: "healthy", watcherRunning: true))

        let degraded = try JSONDecoder().decode([V1IngestionSource].self, from: Data("""
        [{"source": "codex", "state": "degraded"}, {"source": "hermes", "state": "degraded"}]
        """.utf8))
        // Rows say Degraded; the header's summary says "Needs a fix", which is
        // not a repeat, so it stays.
        XCTAssertFalse(SourcesPane.rowsShareState(degraded, overall: "degraded", watcherRunning: true))
    }

    func testMissingGlobalSourceRemainsUnknown() {
        let groups = SourceIssueGroup.group([
            .init(code: "evidence_refreshable_usage_failed", source: nil, action: "Inspect evidence")
        ])

        XCTAssertEqual(groups.count, 1)
        XCTAssertEqual(groups.first?.isGlobalReconciliation, true)
        XCTAssertEqual(groups.first?.affectedSources, [])
        XCTAssertEqual(groups.first?.issues.count, 1)
    }

    func testFailedRefreshDoesNotPresentRetainedRunningWatcherAsCurrent() {
        let watcher = V1IngestionWatcher(state: "running", intervalSeconds: 30, heartbeatAt: 100)
        let retained = SourceHealthPresentation(refreshError: "Current fetch failed")

        XCTAssertTrue(retained.isRetained)
        XCTAssertFalse(retained.watcherIsCurrentlyRunning(watcher))
        XCTAssertEqual(retained.retainedStatus(watcher.state), "Last reported: Running")
        XCTAssertEqual(retained.retainedStatus("healthy"), "Last reported: Healthy")
        XCTAssertEqual(retained.retainedStatus(nil), "Last reported: Unknown")
        XCTAssertTrue(SourceHealthPresentation(refreshError: nil).watcherIsCurrentlyRunning(watcher))
    }
}
