import Foundation
import XCTest
@testable import agentacct

final class DashboardInteractionTests: XCTestCase {
    func testAttentionPayloadPreservesCompleteCountsAndRecordedReason() throws {
        let payload = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [
                {
                  "task_id": "task-finding",
                  "title": "Verify dashboard hierarchy",
                  "project": "agentacct-gui",
                  "decision_status": { "key": "finding", "label": "Open finding" },
                  "evidence_strength": {
                    "key": "unchecked",
                    "gradeable": true,
                    "checkable_total": 1,
                    "checked_total": 0,
                    "checks_failed": 1
                  },
                  "cost": {},
                  "primary_root": { "client": "codex", "client_session_id": "session-1" },
                  "attention": {
                    "kind": "failed_check",
                    "summary": "The reference image changed unexpectedly",
                    "next_step": "Inspect the reference and rerun the snapshot check",
                    "observed_at": 1000,
                    "source": "mcp"
                  }
                }
              ],
              "total": 3,
              "counts": { "failed_check": 2, "failed_step": 0, "blocker": 1 },
              "limit": 1,
              "truncated": true
            }
            """
        )

        XCTAssertEqual(payload.schema, "agentacct.v1-attention.v1")
        XCTAssertEqual(payload.total, 3)
        XCTAssertEqual(payload.counts.failedCheck, 2)
        XCTAssertEqual(payload.counts.failedStep, 0)
        XCTAssertEqual(payload.counts.blocker, 1)
        XCTAssertEqual(payload.items.first?.project, "agentacct-gui")
        XCTAssertEqual(payload.items.first?.attention?.kind, "failed_check")
        XCTAssertEqual(
            payload.items.first?.attention?.nextStep,
            "Inspect the reference and rerun the snapshot check"
        )
        XCTAssertTrue(payload.truncated)
    }

    func testShiftBriefUsesServerReasonWithoutInventingRecoveryCopy() throws {
        let task = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "task-finding",
              "title": "Verify dashboard hierarchy",
              "project": "agentacct-gui",
              "decision_status": { "key": "finding", "label": "Open finding" },
              "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
              "cost": {},
              "attention": {
                "kind": "failed_check",
                "summary": "The reference image changed unexpectedly",
                "next_step": null,
                "observed_at": 1000,
                "source": "mcp"
              }
            }
            """
        )

        let focus = try XCTUnwrap(DashboardAttentionItem(task: task))
        XCTAssertEqual(focus.reasonLabel, "Failed check")
        XCTAssertEqual(focus.summary, "The reference image changed unexpectedly")
        XCTAssertNil(focus.nextStep)
        XCTAssertEqual(focus.project, "agentacct-gui")
        XCTAssertEqual(focus.sourceLabel, "MCP record")
    }

    func testShiftBriefNeverTurnsLoadingUnavailableOrMalformedDataIntoAllClear() throws {
        XCTAssertEqual(
            DashboardAttentionPresentation(payload: nil, error: nil),
            .loading
        )
        XCTAssertEqual(
            DashboardAttentionPresentation(payload: nil, error: nil).dashboardHeadline,
            "Checking recorded work"
        )
        XCTAssertEqual(
            DashboardAttentionPresentation(payload: nil, error: "daemon unavailable"),
            .unavailable("daemon unavailable")
        )
        XCTAssertEqual(
            DashboardAttentionPresentation(payload: nil, error: "daemon unavailable").dashboardHeadline,
            "Review status unavailable"
        )

        let clear = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [],
              "total": 0,
              "counts": { "failed_check": 0, "failed_step": 0, "blocker": 0 },
              "limit": 5,
              "truncated": false
            }
            """
        )
        XCTAssertEqual(
            DashboardAttentionPresentation(payload: clear, error: nil),
            .clear
        )
        XCTAssertEqual(
            DashboardAttentionPresentation(payload: clear, error: nil).dashboardHeadline,
            "No recorded work needs review"
        )
        XCTAssertEqual(
            DashboardAttentionPresentation(payload: clear, error: "refresh failed"),
            .unavailable("refresh failed")
        )

        let falseClear = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [{
                "task_id": "hidden-finding",
                "decision_status": { "key": "finding" },
                "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
                "cost": {},
                "attention": { "kind": "failed_check", "summary": "snapshot failed" }
              }],
              "total": 0,
              "counts": { "failed_check": 1, "failed_step": 0, "blocker": 0 },
              "limit": 5,
              "truncated": false
            }
            """
        )
        XCTAssertEqual(
            DashboardAttentionPresentation(payload: falseClear, error: nil),
            .inconsistent(total: 0)
        )

        let inconsistent = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [],
              "total": 1,
              "counts": { "failed_check": 1, "failed_step": 0, "blocker": 0 },
              "limit": 5,
              "truncated": true
            }
            """
        )
        XCTAssertEqual(
            DashboardAttentionPresentation(payload: inconsistent, error: nil),
            .inconsistent(total: 1)
        )
        XCTAssertEqual(
            DashboardAttentionPresentation(payload: inconsistent, error: nil).dashboardHeadline,
            "Review details unavailable"
        )

        let mismatchedCounts = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [{
                "task_id": "finding",
                "decision_status": { "key": "finding" },
                "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
                "cost": {},
                "attention": { "kind": "failed_check", "summary": "snapshot failed" }
              }],
              "total": 2,
              "counts": { "failed_check": 1, "failed_step": 0, "blocker": 0 },
              "limit": 5,
              "truncated": false
            }
            """
        )
        XCTAssertEqual(
            DashboardAttentionPresentation(payload: mismatchedCounts, error: nil),
            .inconsistent(total: 2)
        )
    }

    func testShiftBriefHeadlineUsesTheLeadingRecordedTaskInsteadOfStaticCopy() throws {
        let payload = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [{
                "task_id": "task-finding",
                "title": "Verify dashboard hierarchy",
                "decision_status": { "key": "finding" },
                "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
                "cost": {},
                "attention": { "kind": "failed_check", "summary": "snapshot failed" }
              }],
              "total": 2,
              "counts": { "failed_check": 1, "failed_step": 0, "blocker": 1 },
              "limit": 1,
              "truncated": true
            }
            """
        )

        let presentation = DashboardAttentionPresentation(payload: payload, error: nil)
        XCTAssertEqual(presentation.dashboardHeadline, "Verify dashboard hierarchy")
        XCTAssertEqual(presentation.dashboardStatus, "2 review items")
        XCTAssertFalse(presentation.dashboardStatusIsWarning)
    }

    func testSignalRailNeverPresentsRetainedSourceHealthAsCurrentAfterAnError() throws {
        let healthy = try decode(
            V1IngestionSnapshot.self,
            from: """
            {
              "state": "healthy",
              "last_success_at": 1000,
              "issues": []
            }
            """
        )

        XCTAssertEqual(
            DashboardIngestionPresentation(snapshot: healthy, error: "source refresh failed"),
            DashboardIngestionPresentation(
                title: "Source status unavailable",
                detail: "source refresh failed",
                tone: .warning
            )
        )
        XCTAssertEqual(
            DashboardIngestionPresentation(snapshot: healthy, error: nil).title,
            "Sources healthy"
        )
        XCTAssertEqual(
            DashboardIngestionPresentation(snapshot: nil, error: nil),
            DashboardIngestionPresentation(
                title: "Checking source status",
                detail: "Waiting for the current ingestion record.",
                tone: .muted
            )
        )
    }

    func testWorkAttentionEmptyCopyRequiresAnAuthoritativeZero() throws {
        let clear = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [], "total": 0,
              "counts": { "failed_check": 0, "failed_step": 0, "blocker": 0 },
              "limit": 5, "truncated": false
            }
            """
        )
        let inconsistent = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [], "total": 2,
              "counts": { "failed_check": 1, "failed_step": 0, "blocker": 1 },
              "limit": 5, "truncated": true
            }
            """
        )
        let filtered = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [{
                "task_id": "task-finding",
                "decision_status": { "key": "finding" },
                "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
                "cost": {},
                "attention": { "kind": "failed_check", "summary": "snapshot failed" }
              }],
              "total": 2,
              "counts": { "failed_check": 1, "failed_step": 0, "blocker": 1 },
              "limit": 1, "truncated": true
            }
            """
        )

        XCTAssertEqual(WorkAttentionEmptyCopy(payload: clear, query: "").title, "No current review items")
        XCTAssertEqual(
            WorkAttentionEmptyCopy(payload: filtered, query: "visual").title,
            "No review items match this filter"
        )
        XCTAssertEqual(
            WorkAttentionEmptyCopy(payload: inconsistent, query: "visual").title,
            "Review queue details unavailable"
        )
    }

    @MainActor
    func testDestinationsReplaceStaleDashboardSelection() {
        let cases: [(DashboardDestination, MainPane, String?, String?)] = [
            (.work, .work, nil, nil),
            (.task("task-1"), .work, "task-1", nil),
            (.session("codex::session-1"), .work, nil, "codex::session-1"),
            (.limits, .usage, nil, nil),
            (.sources, .sources, nil, nil),
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

    @MainActor
    func testReviewQueueDestinationSelectsTheBoundedAttentionQueue() {
        let selection = AppSelection()
        selection.workSort = .latest
        selection.workGroup = nil

        selection.open(.reviewQueue)

        XCTAssertEqual(selection.pane, .work)
        XCTAssertNil(selection.taskId)
        XCTAssertNil(selection.sessionId)
        XCTAssertEqual(selection.workGroup, .attention)
        XCTAssertEqual(selection.workSort, .attention)
    }

    @MainActor
    func testTaskDestinationCarriesItsQueueOriginExplicitly() {
        let selection = AppSelection()
        selection.workGroup = .attention

        selection.open(.task("recent-task"))
        XCTAssertNil(selection.workGroup, "Recent work must not inherit a stale review filter")

        selection.open(.attentionTask("review-task"))
        XCTAssertEqual(selection.taskId, "review-task")
        XCTAssertEqual(selection.workGroup, .attention, "Review-item back navigation should return to the queue")
    }

    func testAttentionRequestGenerationRejectsAStaleResponse() {
        var generation = LatestRequestGeneration()
        let slowRefresh = generation.begin()
        let dispositionRefresh = generation.begin()

        XCTAssertFalse(generation.accepts(slowRefresh))
        XCTAssertTrue(generation.accepts(dispositionRefresh))
    }

    func testAttentionPagesMergeWithoutHidingLaterItems() throws {
        let first = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [{
                "task_id": "task-1",
                "decision_status": { "key": "finding" },
                "evidence_strength": { "key": "unchecked" },
                "cost": {}
              }],
              "total": 2,
              "counts": { "failed_check": 1, "failed_step": 0, "blocker": 1 },
              "snapshot": "queue-v1",
              "offset": 0, "limit": 1, "truncated": true
            }
            """
        )
        let second = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [{
                "task_id": "task-2",
                "decision_status": { "key": "blocked" },
                "evidence_strength": { "key": "unchecked" },
                "cost": {}
              }],
              "total": 2,
              "counts": { "failed_check": 1, "failed_step": 0, "blocker": 1 },
              "snapshot": "queue-v1",
              "offset": 1, "limit": 1, "truncated": false
            }
            """
        )

        let merged = mergedAttentionPages(first, second)

        XCTAssertEqual(merged.items.map(\.taskId), ["task-1", "task-2"])
        XCTAssertEqual(merged.offset, 0)
        XCTAssertEqual(merged.limit, 2)
        XCTAssertFalse(merged.truncated)
        XCTAssertTrue(attentionPageCanAppend(first, second))

        let changedQueue = V1AttentionPayload(
            schema: second.schema,
            items: second.items,
            total: 3,
            counts: second.counts,
            snapshot: "queue-v2",
            offset: second.offset,
            limit: second.limit,
            truncated: true
        )
        XCTAssertFalse(attentionPageCanAppend(first, changedQueue))
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

    func testAgentPlanRowCopyRequiresARealSevenDayWindow() throws {
        // The per-agent row must never fabricate a meter or a reset time: a
        // 5h-only client says so, a limit-less client says so, and only a
        // provider-reported 7d percent produces a meter value.
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

        let noLimit = DashboardAgentPlanRow(client: "hermes", limit: nil, plan: nil, usage: nil)
        XCTAssertNil(noLimit.usedPercent)
        XCTAssertEqual(noLimit.meterCaption, "no limits reported")
        XCTAssertNil(noLimit.usageText)

        // A stale reading is hidden, not never-reported — the copy must not lie.
        let stale = DashboardAgentPlanRow(
            client: "claude-code", limit: nil, staleLimit: true, plan: nil, usage: nil
        )
        XCTAssertNil(stale.usedPercent)
        XCTAssertEqual(stale.meterCaption, "limit reading stale — see Usage")

        let unavailable = DashboardAgentPlanRow(client: "codex", limit: fiveHourOnly, plan: nil, usage: nil)
        XCTAssertNil(unavailable.usedPercent)
        XCTAssertEqual(unavailable.meterCaption, "no 7-day window reported")

        let available = DashboardAgentPlanRow(client: "codex", limit: sevenDay, plan: nil, usage: nil)
        XCTAssertEqual(available.usedPercent, 39)
        XCTAssertEqual(available.meterCaption, "39% of 7-day limit")
        XCTAssertNil(available.resetText, "no reset time was reported — never fabricated")
        XCTAssertEqual(available.detailText, "39% of 7-day limit · provider reported")
    }

    func testAgentPlanRowsIncludeEveryRecordingClientWithoutFavoritism() throws {
        // Regression for the single-meter dashboard: every non-stale limit
        // client AND every usage-only client gets a row — limit clients first
        // by least headroom, usage-only clients after in cube order.
        let claude = try decode(
            LimitEntry.self,
            from: """
            { "client": "claude-code", "windows": [{ "kind": "7d", "used_percent": 47 }] }
            """
        )
        let codex = try decode(
            LimitEntry.self,
            from: """
            { "client": "codex", "plan_type": "pro", "windows": [{ "kind": "7d", "used_percent": 5 }] }
            """
        )
        let usage = try decode(
            [GlanceClientUsage].self,
            from: """
            [
              { "client": "claude-code", "fresh_tokens": 5140000, "estimated_cost_usd": 1200.5,
                "cost_complete": true, "cost_confidence": "estimated_from_tokens" },
              { "client": "codex", "fresh_tokens": 17670000, "estimated_cost_usd": 310.9,
                "cost_complete": true, "cost_confidence": "estimated_from_tokens" },
              { "client": "hermes", "fresh_tokens": 145000, "estimated_cost_usd": 1.0,
                "cost_complete": true, "cost_confidence": "estimated_from_tokens" }
            ]
            """
        )
        let rows = DashboardAgentPlanRow.rows(limits: [codex, claude], planClients: [], usage: usage)
        XCTAssertEqual(rows.map(\.client), ["claude-code", "codex", "hermes"])
        XCTAssertEqual(rows[0].usedPercent, 47)
        XCTAssertEqual(rows[1].planType, "pro")
        // The usage-only client keeps the honest hatched-track copy.
        XCTAssertNil(rows[2].usedPercent)
        XCTAssertEqual(rows[2].meterCaption, "no limits reported")
        XCTAssertNotNil(rows[2].usageText)
        // Every usage figure is anchored to the card's 7-day window.
        XCTAssertTrue(rows[2].usageText?.hasPrefix("7d · ") == true)
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

    func testWorkRecordUsesReferenceWidthForSideBySideEvidence() {
        XCTAssertEqual(workRecordColumnMode(for: 823), .stacked)
        XCTAssertEqual(workRecordColumnMode(for: 824), .sideBySide)
        XCTAssertEqual(workRecordColumnMode(for: 860), .sideBySide)
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
