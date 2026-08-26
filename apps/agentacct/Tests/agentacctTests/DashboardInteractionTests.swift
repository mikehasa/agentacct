import Foundation
import XCTest
@testable import agentacct

final class DashboardInteractionTests: XCTestCase {
    @MainActor
    func testDestinationsReplaceStaleDashboardSelection() {
        let cases: [(DashboardDestination, MainPane, String?, String?)] = [
            (.work, .work, nil, nil),
            (.task("task-1"), .work, "task-1", nil),
            (.session("codex::session-1"), .work, nil, "codex::session-1"),
            (.limits, .limits, nil, nil),
        ]

        for (destination, pane, taskID, sessionID) in cases {
            let selection = AppSelection()
            selection.taskId = "stale-task"
            selection.sessionId = "stale-session"

            selection.open(destination)

            XCTAssertEqual(selection.pane, pane, "destination: \(destination)")
            XCTAssertEqual(selection.taskId, taskID, "destination: \(destination)")
            XCTAssertEqual(selection.sessionId, sessionID, "destination: \(destination)")
        }
    }

    func testRecentWorkProjectionKeepsDecisionEvidenceAndCostSeparate() throws {
        let task = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "task-1",
              "title": "Build reusable snapshot harness",
              "decision_status": { "key": "verified", "label": "Verified" },
              "evidence_strength": {
                "key": "independently_checked",
                "gradeable": true,
                "checkable_total": 4,
                "checked_total": 4
              },
              "cost": {
                "estimated_cost_usd": 4.82,
                "cost_basis": "pricing_table",
                "cost_confidence": "estimated",
                "cost_complete": true
              },
              "primary_root": { "client": "codex", "client_session_id": "session-1" },
              "last_activity_at": 1000
            }
            """
        )

        let item = DashboardWorkItem(task: task)

        XCTAssertEqual(item.title, "Build reusable snapshot harness")
        XCTAssertEqual(item.client, "codex")
        XCTAssertEqual(item.outcome, "Verified")
        XCTAssertEqual(item.evidence, "4/4 checked")
        XCTAssertEqual(item.cost, "≈$4.82")
    }

    func testRecentWorkCostLabelsDoNotClaimUnknownCompleteness() throws {
        let cases = [
            (
                cost: #"{"estimated_cost_usd": 4.82, "cost_confidence": "client_reported", "cost_complete": true}"#,
                expected: "$4.82"
            ),
            (
                cost: #"{"estimated_cost_usd": 4.82, "cost_confidence": "client_reported", "cost_complete": false}"#,
                expected: "~$4.82"
            ),
            (
                cost: #"{"estimated_cost_usd": 4.82, "cost_confidence": "client_reported"}"#,
                expected: "≈$4.82"
            ),
            (cost: #"{}"#, expected: "—"),
        ]

        for (index, testCase) in cases.enumerated() {
            let task = try decode(
                ReceiptSummary.self,
                from: """
                {
                  "task_id": "task-\(index)",
                  "decision_status": { "key": "verified" },
                  "evidence_strength": { "key": "none" },
                  "cost": \(testCase.cost)
                }
                """
            )

            XCTAssertEqual(DashboardWorkItem(task: task).cost, testCase.expected)
        }
    }

    func testUsageSeriesFormatsAvailableAndMissingValuesHonestly() throws {
        let periods = try decode(
            [PeriodBucket].self,
            from: """
            [
              { "period": "2026-08-24", "fresh_tokens": 1200000,
                "estimated_cost_usd": 2.50, "cost_complete": true },
              { "period": "2026-08-25", "fresh_tokens": 300000,
                "estimated_cost_usd": 1.25, "cost_complete": true },
              { "period": "2026-08-26" }
            ]
            """
        )
        let completePeriods = Array(periods.prefix(2))

        XCTAssertEqual(DashboardUsageSeries.tokens.valueText(for: periods[0]), "1.2M")
        XCTAssertEqual(DashboardUsageSeries.cost.valueText(for: periods[0]), "≈$2.50")
        XCTAssertEqual(DashboardUsageSeries.cost.valueText(for: periods[2]), "—")
        XCTAssertEqual(DashboardUsageSeries.tokens.valueText(for: periods[2]), "—")
        XCTAssertEqual(DashboardUsageSeries.tokens.totalText(for: completePeriods), "1.5M total")
        XCTAssertEqual(DashboardUsageSeries.cost.totalText(for: completePeriods), "≈$3.75 total")
        XCTAssertEqual(DashboardUsageSeries.tokens.totalText(for: periods), "~1.5M total")
        XCTAssertEqual(DashboardUsageSeries.cost.totalText(for: periods), "~$3.75 total")
        XCTAssertEqual(DashboardUsageSeries.tokens.totalText(for: [periods[2]]), "—")
        XCTAssertEqual(DashboardUsageSeries.cost.totalText(for: [periods[2]]), "—")
    }

    func testActiveWorkIncludesOnlyRunningStates() {
        let cases: [(status: String?, isActive: Bool)] = [
            ("started", true),
            ("checkpoint", true),
            ("in_progress", true),
            ("blocked", false),
            ("handed_off", false),
            ("completed", false),
            (nil, false),
        ]

        for testCase in cases {
            XCTAssertEqual(
                isActiveWorkStatus(testCase.status),
                testCase.isActive,
                "status: \(testCase.status ?? "nil")"
            )
        }
    }

    func testPlanWindowCopyRequiresARealSevenDayWindow() throws {
        let missingProvider = DashboardPlanWindowPresentation(limit: nil)
        XCTAssertEqual(missingProvider.remainingText, "—")
        XCTAssertEqual(missingProvider.remainingCaption, "7-day")
        XCTAssertEqual(missingProvider.resetText, "Provider limit unavailable")
        XCTAssertEqual(missingProvider.provenanceText, "No live provider data")

        let fiveHourOnly = try decode(
            LimitEntry.self,
            from: """
            {
              "client": "codex",
              "plan_type": "pro",
              "windows": [{ "kind": "5h", "used_percent": 31 }]
            }
            """
        )
        let sevenDay = try decode(
            LimitEntry.self,
            from: """
            {
              "client": "codex",
              "plan_type": "pro",
              "windows": [{ "kind": "7d", "used_percent": 39 }]
            }
            """
        )

        let unavailable = DashboardPlanWindowPresentation(limit: fiveHourOnly)
        XCTAssertEqual(unavailable.titleSuffix, "")
        XCTAssertEqual(unavailable.remainingText, "—")
        XCTAssertEqual(unavailable.remainingCaption, "7-day")
        XCTAssertEqual(unavailable.resetText, "7-day limit unavailable")
        XCTAssertEqual(unavailable.provenanceText, "No provider-reported 7-day window")

        let available = DashboardPlanWindowPresentation(limit: sevenDay)
        XCTAssertEqual(available.titleSuffix, " · 7-day window")
        XCTAssertEqual(available.remainingText, "61%")
        XCTAssertEqual(available.remainingCaption, "remaining")
        XCTAssertEqual(available.provenanceText, "Provider reported")
    }

    func testActiveSessionResolutionNeverDropsAnUnmatchedSession() throws {
        let task = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "task-1",
              "decision_status": { "key": "verified" },
              "evidence_strength": { "key": "none" },
              "cost": {},
              "primary_root": { "client": "codex", "client_session_id": "root" }
            }
            """
        )

        XCTAssertEqual(
            workSessionResolution(for: "codex::root", in: [task]),
            .task("task-1")
        )
        XCTAssertEqual(
            workSessionResolution(for: "codex::subagent", in: [task]),
            .unresolved("codex::subagent")
        )
    }

    func testNeedsReviewProjectionUsesActionableTasks() throws {
        let tasks = try decode(
            [ReceiptSummary].self,
            from: """
            [
              {
                "task_id": "failed-check",
                "decision_status": { "key": "reported", "label": "Agent reported" },
                "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
                "cost": {}
              },
              {
                "task_id": "blocked",
                "decision_status": {
                  "key": "blocked", "label": "Blocked", "asserted_by": "agent_report"
                },
                "evidence_strength": { "key": "not_gradeable" },
                "cost": {}
              },
              {
                "task_id": "complete",
                "decision_status": { "key": "verified", "label": "Verified" },
                "evidence_strength": { "key": "independently_checked" },
                "cost": {}
              },
              {
                "task_id": "finding",
                "decision_status": { "key": "finding", "label": "Open finding" },
                "evidence_strength": { "key": "unchecked" },
                "cost": {}
              },
              {
                "task_id": "superseded",
                "decision_status": { "key": "finding_superseded", "label": "Finding superseded" },
                "evidence_strength": { "key": "self_checked", "checks_failed": 1 },
                "cost": {}
              }
            ]
            """
        )
        let items = tasks.map(DashboardWorkItem.init)

        XCTAssertTrue(items[0].needsReview)
        XCTAssertTrue(items[0].hasFinding)
        XCTAssertEqual(items[0].failedChecks, 1)
        XCTAssertTrue(items[1].needsReview)
        XCTAssertFalse(items[1].hasFinding)
        XCTAssertEqual(items[1].outcomeSource, "agent reported")
        XCTAssertFalse(items[2].needsReview)
        XCTAssertTrue(items[3].needsReview)
        XCTAssertTrue(items[3].hasFinding)
        // A superseded finding's failed check belongs to the finding that
        // superseded it — it never resurfaces as needing review.
        XCTAssertFalse(items[4].needsReview)
        XCTAssertFalse(items[4].hasFinding)
    }

    @MainActor
    func testLocalDataFreshnessUsesTheSnapshotClock() {
        SnapshotMode.setFixtureDate(Date(timeIntervalSince1970: 1_000))
        defer { SnapshotMode.setFixtureDate(nil) }

        XCTAssertEqual(
            dashboardFreshnessText(Date(timeIntervalSince1970: 1_000)),
            "just now"
        )
        XCTAssertEqual(
            dashboardFreshnessText(Date(timeIntervalSince1970: 880)),
            "2m ago"
        )
        XCTAssertEqual(
            dashboardFreshnessText(Date(timeIntervalSince1970: 1_001)),
            "time unavailable"
        )
    }

    func testWindowMaterialRespectsAccessibilityAndSnapshotDeterminism() {
        let cases = [
            (reduceTransparency: false, snapshotMode: false, expected: true),
            (reduceTransparency: true, snapshotMode: false, expected: false),
            (reduceTransparency: false, snapshotMode: true, expected: false),
            (reduceTransparency: true, snapshotMode: true, expected: false),
        ]

        for testCase in cases {
            XCTAssertEqual(
                WindowSurfacePolicy.usesMaterial(
                    reduceTransparency: testCase.reduceTransparency,
                    snapshotMode: testCase.snapshotMode
                ),
                testCase.expected,
                "reduceTransparency: \(testCase.reduceTransparency), "
                    + "snapshotMode: \(testCase.snapshotMode)"
            )
        }
    }

    private func decode<Value: Decodable>(_ type: Value.Type, from json: String) throws -> Value {
        try JSONDecoder().decode(type, from: Data(json.utf8))
    }
}
