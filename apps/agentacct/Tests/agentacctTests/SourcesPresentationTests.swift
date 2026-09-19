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
        let watcher = V1IngestionWatcher(state: "running", intervalSeconds: 30, heartbeatAt: 100, stateTitle: "Running")
        let retained = SourceHealthPresentation(refreshError: "Current fetch failed")

        XCTAssertTrue(retained.isRetained)
        XCTAssertFalse(retained.watcherIsCurrentlyRunning(watcher))
        XCTAssertEqual(
            retained.retainedStatus(title: SourceHealthPresentation.watcherTitle(watcher)),
            "Last reported: Running"
        )
        XCTAssertTrue(SourceHealthPresentation(refreshError: nil).watcherIsCurrentlyRunning(watcher))
    }

    func testTitlesAreThePayloadStateCopyOrANamedAbsence() throws {
        let payload = try JSONDecoder().decode(V1IngestionPayload.self, from: Data("""
        {
          "schema": "agentacct.v1-ingestion.v1",
          "ingestion": {
            "state": "unknown",
            "last_success_at": null,
            "state_title": "Import history not recorded",
            "state_detail": "Usage from 336 sessions is stored, but no import run was recorded for this store.",
            "sources": [
              { "source": "codex", "state": "healthy", "parsed": 8,
                "state_title": "Reporting",
                "state_detail": "The latest import parsed rows and the running watcher keeps this source current." },
              { "source": "cursor", "state": "future_state" }
            ],
            "watcher": { "state": "running", "state_title": "Running",
                         "state_detail": "The importer keeps the store current in the background." },
            "issues": []
          }
        }
        """.utf8))
        let snapshot = payload.ingestion
        XCTAssertEqual(SourceHealthPresentation.overallTitle(snapshot), "Import history not recorded")
        XCTAssertEqual(snapshot.sources?.map(SourceHealthPresentation.sourceTitle), ["Reporting", "Source state not reported"])
        XCTAssertEqual(SourceHealthPresentation.watcherTitle(snapshot.watcher), "Running")
        XCTAssertEqual(SourceHealthPresentation.watcherTitle(nil), "Watcher state not reported")
        XCTAssertEqual(
            SourceHealthPresentation(refreshError: "x").retainedStatus(
                title: SourceHealthPresentation.sourceTitle(try XCTUnwrap(snapshot.sources?.first))
            ),
            "Last reported: Reporting"
        )
    }
}
