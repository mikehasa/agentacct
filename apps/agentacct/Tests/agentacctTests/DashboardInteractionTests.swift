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
            (.limits, .usage, nil, nil),
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
        XCTAssertEqual(item.evidence, "4/4 supported")
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

    func testUsageSeriesDescribesTheSelectedRangeAndEffectiveGranularity() throws {
        let daily = try usagePeriodPresentation(granularity: "daily")
        let weekly = try usagePeriodPresentation(granularity: "weekly")
        let unknown = try usagePeriodPresentation(granularity: nil)

        XCTAssertEqual(
            DashboardUsageSeries.tokens.subtitle(
                rangeDays: 7,
                periodPresentation: daily
            ),
            "Fresh tokens · last 7 days · client reported"
        )
        XCTAssertEqual(
            DashboardUsageSeries.tokens.subtitle(
                rangeDays: 90,
                periodPresentation: weekly
            ),
            "Fresh tokens · last 90 days · weekly buckets · client reported"
        )
        XCTAssertEqual(
            DashboardUsageSeries.cost.subtitle(
                rangeDays: 90,
                periodPresentation: weekly
            ),
            "Estimated cost · last 90 days · weekly buckets · pricing-table basis"
        )
        XCTAssertEqual(
            DashboardUsageSeries.tokens.subtitle(
                rangeDays: 30,
                periodPresentation: unknown
            ),
            "Fresh tokens · last 30 days · period buckets · client reported"
        )
        XCTAssertEqual(weekly.pinAccessibilityHint, "Pins or clears this week's value")
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
    func testWorkBrowseStateSurvivesReceiptRoundTrip() {
        let selection = AppSelection()
        selection.workBrowse.query = "pytest"
        selection.workBrowse.group = .attention
        selection.workBrowse.sort = .cost
        selection.workBrowse.pendingFocusRestorationTaskId = "task-1"

        selection.open(.task("task-1"))
        selection.open(.work)

        XCTAssertEqual(selection.workBrowse.query, "pytest")
        XCTAssertEqual(selection.workBrowse.group, .attention)
        XCTAssertEqual(selection.workBrowse.sort, .cost)
        XCTAssertEqual(selection.workBrowse.pendingFocusRestorationTaskId, "task-1")
    }

    func testWorkSelectionUsesAdaptiveMasterDetailPolicy() {
        XCTAssertEqual(
            workLayoutMode(for: 960, dynamicTypeSize: .medium, hasSelection: true),
            .pushDetail
        )
        XCTAssertEqual(
            workLayoutMode(for: 1120, dynamicTypeSize: .medium, hasSelection: true),
            .split
        )
        XCTAssertEqual(
            workLayoutMode(for: 1600, dynamicTypeSize: .accessibility1, hasSelection: true),
            .pushDetail
        )
        XCTAssertEqual(
            workLayoutMode(for: 960, dynamicTypeSize: .medium, hasSelection: false),
            .table
        )
    }

    func testRequestCancellationIsNeverPublishedAsAFetchFailure() {
        XCTAssertTrue(
            requestWasCancelled(CancellationError(), taskIsCancelled: false)
        )
        XCTAssertTrue(
            requestWasCancelled(URLError(.cancelled), taskIsCancelled: false)
        )
        XCTAssertTrue(
            requestWasCancelled(
                GlanceClientError.transport("cancelled"),
                taskIsCancelled: true
            )
        )
        XCTAssertTrue(
            requestWasCancelled(
                GlanceClientError.noDiscovery("/tmp/missing-local-api.json"),
                taskIsCancelled: true
            )
        )
        XCTAssertFalse(
            requestWasCancelled(
                GlanceClientError.transport("connection refused"),
                taskIsCancelled: false
            )
        )
    }

    func testWorkSelectionOutsideCurrentFiltersIsExplicit() throws {
        let tasks = try decode(
            [ReceiptSummary].self,
            from: """
            [
              {
                "task_id": "finding",
                "decision_status": { "key": "finding" },
                "evidence_strength": { "key": "unchecked" },
                "cost": {}
              },
              {
                "task_id": "verified",
                "decision_status": { "key": "verified" },
                "evidence_strength": { "key": "independently_checked" },
                "cost": {}
              }
            ]
            """
        )
        let attention = visibleWorkReceipts(tasks, query: "", group: .attention, sort: .latest)

        XCTAssertTrue(
            workSelectionIsOutsideBrowse(
                taskId: "verified",
                allTasks: tasks,
                visibleTasks: attention
            )
        )
        XCTAssertFalse(
            workSelectionIsOutsideBrowse(
                taskId: "finding",
                allTasks: tasks,
                visibleTasks: attention
            )
        )
    }

    func testWorkBrowseCountsNameTheLoadedSlice() {
        XCTAssertEqual(
            workBrowseCountText(visible: 4, loaded: 4, total: 4, truncated: false),
            "4 of 4 receipts"
        )
        XCTAssertEqual(
            workBrowseCountText(visible: 12, loaded: 200, total: 529, truncated: true),
            "12 of 200 loaded · 529 in store"
        )
    }

    func testWorkBrowseNamesUnknownAndExplicitlyTruncatedTotals() {
        XCTAssertEqual(
            workBrowseCountText(visible: 12, loaded: 200, total: nil, truncated: true),
            "12 of 200 loaded · more may exist"
        )
        XCTAssertEqual(
            workBrowseCountText(visible: 12, loaded: 200, total: nil, truncated: nil),
            "12 of 200 loaded · total not reported"
        )
        XCTAssertTrue(workReceiptCollectionIsPartial(loaded: 200, total: nil, truncated: true))
        XCTAssertFalse(workReceiptCollectionIsPartial(loaded: 200, total: nil, truncated: nil))
    }

    @MainActor
    func testWorkReturnFocusFallsBackWhenReceiptIsOutsideFilters() throws {
        let visible = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "visible",
              "title": "Visible task",
              "decision_status": { "key": "reported" },
              "evidence_strength": { "key": "unchecked" },
              "cost": {}
            }
            """
        )
        let hidden = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "hidden",
              "title": "Hidden task",
              "decision_status": { "key": "finding" },
              "evidence_strength": { "key": "unchecked" },
              "cost": {}
            }
            """
        )
        let browse = WorkBrowseState()
        browse.group = .reported

        browse.prepareReturnFocus(from: hidden.taskId, in: [visible, hidden])

        XCTAssertNil(browse.pendingFocusRestorationTaskId)
        XCTAssertTrue(browse.shouldFocusSearchOnReturn)

        browse.prepareReturnFocus(from: visible.taskId, in: [visible, hidden])

        XCTAssertEqual(browse.pendingFocusRestorationTaskId, visible.taskId)
        XCTAssertFalse(browse.shouldFocusSearchOnReturn)
    }

    func testFailedChecksPutReportedReceiptInAttentionGroupAndSort() throws {
        let reportedFailure = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "reported-failure",
              "decision_status": { "key": "reported" },
              "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
              "cost": {}
            }
            """
        )
        let findingResolved = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "resolved",
              "decision_status": { "key": "finding_resolved_by_user" },
              "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
              "cost": {}
            }
            """
        )

        XCTAssertEqual(WorkGroup.forTask(reportedFailure), .attention)
        XCTAssertEqual(WorkGroup.forTask(findingResolved), .reported)
        XCTAssertEqual(
            sortedReceipts([findingResolved, reportedFailure], by: .attention).map(\.taskId),
            ["reported-failure", "resolved"]
        )
    }

    @MainActor
    func testDashboardRefreshReadsSelectionAfterCollectionRefresh() async {
        var selection = "task-a"
        var refreshedTaskIds: [String] = []

        await refreshDashboardAndSelectedWork(
            dashboardRefresh: { selection = "task-b" },
            selectedTaskId: { selection },
            receiptRefresh: { refreshedTaskIds.append($0) }
        )

        XCTAssertEqual(refreshedTaskIds, ["task-b"])
    }

    @MainActor
    func testCancelledDashboardRefreshDoesNotStartDetailRefresh() async {
        var didRefreshReceipt = false

        await refreshDashboardAndSelectedWork(
            dashboardRefresh: {
                withUnsafeCurrentTask { $0?.cancel() }
            },
            selectedTaskId: { "task-a" },
            receiptRefresh: { _ in didRefreshReceipt = true }
        )

        XCTAssertFalse(didRefreshReceipt)
    }

    func testReceiptRefreshErrorsStayScopedToTheirTask() {
        XCTAssertEqual(
            workReceiptRefreshError(
                selectedTaskId: "task-a",
                errorTaskId: "task-a",
                error: "offline"
            ),
            "offline"
        )
        XCTAssertNil(
            workReceiptRefreshError(
                selectedTaskId: "task-b",
                errorTaskId: "task-a",
                error: "offline"
            )
        )
    }

    func testMissingEvidenceCountsRemainUnreported() throws {
        let task = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "partial",
              "decision_status": { "key": "reported", "label": "Reported" },
              "evidence_strength": {
                "key": "self_checked",
                "gradeable": true,
                "checkable_total": 3,
                "checks_total": 2
              },
              "cost": {}
            }
            """
        )

        let presentation = WorkReceiptRowPresentation(task: task)
        XCTAssertEqual(presentation.coverageText, "support count not reported · 3 checkable claims")
        XCTAssertEqual(presentation.checkRunsText, "passes not reported · 2 check runs")
        XCTAssertFalse(presentation.accessibilityLabel.contains("0/"))
    }

    func testCompactCheckRunCopyDoesNotCollapseNoRunsToNo() throws {
        let task = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "no-checks",
              "decision_status": { "key": "blocked", "label": "Blocked" },
              "evidence_strength": {
                "key": "undefined",
                "gradeable": false,
                "checkable_total": 0,
                "checked_total": 0,
                "checks_total": 0,
                "checks_passed": 0,
                "checks_failed": 0
              },
              "cost": {}
            }
            """
        )

        XCTAssertEqual(
            WorkReceiptRowPresentation(task: task).compactCheckRunsText,
            "no check runs"
        )
    }

    func testReceiptPresentationsNameInconsistentCounts() throws {
        let evidence = try decode(
            ReceiptEvidence.self,
            from: """
            {
              "key": "self_checked",
              "gradeable": true,
              "checkable_total": 0,
              "checked_total": 2,
              "by_tier": { "self_checked": 2 }
            }
            """
        )

        let coverage = ReceiptCoveragePresentation(evidence: evidence)
        let checks = ReceiptCheckRunsPresentation(total: 1, passed: 2, failed: 1)

        XCTAssertEqual(coverage.value, "Inconsistent counts")
        XCTAssertTrue(coverage.isInconsistent)
        XCTAssertEqual(coverage.qualifier, "2 supported · 0 checkable reported")
        XCTAssertEqual(coverage.rowText, "inconsistent coverage · 2 supported of 0 reported")
        XCTAssertEqual(evidence.compactHeadline, coverage.rowText)
        XCTAssertEqual(evidence.headline, "Inconsistent counts (2 supported · 0 checkable reported)")
        XCTAssertEqual(checks.value, "Inconsistent counts")
        XCTAssertTrue(checks.isInconsistent)
        XCTAssertEqual(checks.qualifier, "2 passed · 1 failed · 1 total reported")
        XCTAssertEqual(checks.rowText, "inconsistent check runs · 2 passed · 1 failed · 1 total")
        XCTAssertEqual(checks.headerText, "inconsistent · 2 passed · 1 failed · 1 total")
    }

    func testReceiptCheckPresentationMarksZeroTotalTalliesInconsistent() {
        let checks = ReceiptCheckRunsPresentation(total: 0, passed: 1, failed: 0)

        XCTAssertTrue(checks.isInconsistent)
        XCTAssertEqual(checks.value, "0 total reported")
        XCTAssertEqual(checks.qualifier, "1 passed · 0 failed · tallies conflict with total")
    }

    func testEmptyCheckDetailsDistinguishMissingItemsFromNoRuns() {
        XCTAssertEqual(
            receiptEmptyCheckDetailsCopy(total: 3, passed: 2, failed: 1),
            ReceiptEmptyCheckDetailsCopy(
                title: "No itemized check details recorded",
                detail: "Summary counts are available above; this payload did not include per-run details."
            )
        )
        XCTAssertEqual(
            receiptEmptyCheckDetailsCopy(total: nil, passed: nil, failed: nil),
            ReceiptEmptyCheckDetailsCopy(
                title: "No check runs recorded",
                detail: "Machine checks land here when a hook or CI reports one."
            )
        )
    }

    func testCoveragePresentationNamesUnavailableAndConflictingTierBreakdowns() throws {
        let missing = try decode(
            ReceiptEvidence.self,
            from: """
            {
              "key": "self_checked",
              "gradeable": true,
              "checkable_total": 2,
              "checked_total": 1
            }
            """
        )
        let conflicting = try decode(
            ReceiptEvidence.self,
            from: """
            {
              "key": "self_checked",
              "gradeable": true,
              "checkable_total": 2,
              "checked_total": 1,
              "by_tier": { "self_checked": 2 }
            }
            """
        )

        let missingPresentation = ReceiptCoveragePresentation(evidence: missing)
        let conflictingPresentation = ReceiptCoveragePresentation(evidence: conflicting)

        XCTAssertFalse(missingPresentation.isInconsistent)
        XCTAssertFalse(missingPresentation.tierBreakdownAvailable)
        XCTAssertEqual(
            missingPresentation.tierBreakdownNotice,
            "Evidence-tier breakdown not reported."
        )
        XCTAssertTrue(conflictingPresentation.isInconsistent)
        XCTAssertFalse(conflictingPresentation.tierBreakdownAvailable)
        XCTAssertEqual(
            conflictingPresentation.tierBreakdownNotice,
            "Evidence tiers report 2 supported claims; the summary reports 1."
        )
    }

    func testAttentionDecisionSummarySeparatesBlockerAndFailedChecks() throws {
        let receipt = try decode(
            Receipt.self,
            from: """
            {
              "schema_version": "agentacct.receipt.v1",
              "task_id": "blocked",
              "axes": {
                "decision_status": {
                  "key": "blocked",
                  "label": "Blocked",
                  "statement": "A representative window is required.",
                  "asserted_by": "agent_report",
                  "blocker": { "text": "The sample is too short." }
                },
                "evidence_strength": {
                  "key": "unchecked",
                  "gradeable": true,
                  "checkable_total": 2,
                  "checked_total": 1
                }
              },
              "dimensions": {
                "task": {}, "actors": {}, "actions": {}, "cost": {},
                "evidence": { "checks_total": 2, "checks_passed": 1, "checks_failed": 1 },
                "outcome": {}, "gaps": {}, "provenance": {}
              }
            }
            """
        )

        let presentation = WorkReceiptDecisionPresentation(receipt: receipt)
        XCTAssertTrue(presentation.isAttention)
        XCTAssertEqual(presentation.explanation, "A representative window is required. — agent reported")
        XCTAssertEqual(presentation.coverageValue, "1 of 2")
        XCTAssertEqual(presentation.checksValue, "1 of 2")
        XCTAssertTrue(presentation.checksQualifier.contains("1 failed"))
        XCTAssertFalse(presentation.explanation.contains("The sample is too short"))
    }

    func testWorkReceiptRowPresentationIncludesDecisionRelevantFields() throws {
        let task = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "task-1",
              "title": "Investigate pytest errors",
              "decision_status": {
                "key": "finding",
                "label": "Finding",
                "statement": "A recorded check found an issue."
              },
              "evidence_strength": {
                "key": "unchecked",
                "gradeable": true,
                "checkable_total": 1,
                "checked_total": 0,
                "checks_total": 3,
                "checks_passed": 2,
                "checks_failed": 1
              },
              "cost": {
                "estimated_cost_usd": 7.66,
                "cost_confidence": "estimated_from_tokens",
                "cost_complete": false
              },
              "primary_root": { "client": "codex", "client_session_id": "session-1" },
              "last_activity_at": 1000,
              "handed_off": true
            }
            """
        )

        let row = WorkReceiptRowPresentation(task: task)

        XCTAssertEqual(row.coverageText, "0/1 claims supported")
        XCTAssertEqual(row.checkRunsText, "2/3 check runs passed · 1 failed")
        XCTAssertEqual(row.costText, "~$7.66")
        XCTAssertTrue(row.accessibilityLabel.contains("Finding"))
        XCTAssertTrue(row.accessibilityLabel.contains("handed off"))
        XCTAssertTrue(row.accessibilityLabel.contains("0/1 claims supported"))
        XCTAssertTrue(row.accessibilityLabel.contains("2/3 check runs passed, 1 failed"))
        XCTAssertTrue(row.accessibilityLabel.contains("Codex"))
        XCTAssertTrue(row.accessibilityLabel.contains("~$7.66"))
    }

    func testWorkReceiptRowPresentationPrefersFreshDetailHandoffState() throws {
        let task = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "task-1",
              "title": "Investigate pytest errors",
              "decision_status": { "key": "finding", "label": "Finding" },
              "evidence_strength": { "key": "unchecked" },
              "cost": {},
              "handed_off": true
            }
            """
        )
        let detail = try decode(
            Receipt.self,
            from: """
            {
              "schema_version": "agentacct.receipt.v1",
              "task_id": "task-1",
              "title": "Investigate pytest errors",
              "axes": {
                "decision_status": { "key": "finding", "label": "Finding" },
                "evidence_strength": { "key": "unchecked" },
                "handoff": { "handed_off": false }
              },
              "dimensions": {
                "task": {}, "actors": {}, "actions": {}, "cost": {},
                "evidence": {}, "outcome": {}, "gaps": {}, "provenance": {}
              }
            }
            """
        )

        let row = WorkReceiptRowPresentation(task: task, detail: detail)

        XCTAssertFalse(row.handedOff)
        XCTAssertFalse(row.accessibilityLabel.contains("handed off"))
    }

    func testWorkDecisionSummaryKeepsClaimCoverageSeparateFromCheckRuns() throws {
        let fixtureURL = try XCTUnwrap(
            Bundle.module.url(forResource: "dashboard", withExtension: "json")
        )
        let receipt = try XCTUnwrap(DashboardSnapshotFixture.load(from: fixtureURL).work?.receipt)

        let presentation = WorkReceiptDecisionPresentation(receipt: receipt)

        XCTAssertEqual(presentation.headline, "Current outcome")
        XCTAssertEqual(presentation.coverageValue, "4 of 4")
        XCTAssertEqual(presentation.coverageQualifier, "claims supported")
        XCTAssertEqual(presentation.checksValue, "6 of 6")
        XCTAssertEqual(presentation.checksQualifier, "check runs passed")
        XCTAssertFalse(presentation.isAttention)
        XCTAssertTrue(presentation.accessibilityLabel.contains("Coverage: 4 of 4"))
        XCTAssertTrue(presentation.accessibilityLabel.contains("Checks: 6 of 6"))
    }

    func testReceiptPresentationsPreservePartialCountsWithoutInventingZeroes() throws {
        let receipt = try decode(
            Receipt.self,
            from: """
            {
              "schema_version": "agentacct.receipt.v1",
              "task_id": "partial",
              "axes": {
                "decision_status": { "key": "reported" },
                "evidence_strength": {
                  "key": "unchecked",
                  "gradeable": true,
                  "checkable_total": 4
                }
              },
              "dimensions": {
                "task": {}, "actors": {}, "actions": {}, "cost": {},
                "evidence": { "checks_passed": 2, "checks_failed": 1 },
                "outcome": {}, "gaps": {}, "provenance": {}
              }
            }
            """
        )

        let decision = WorkReceiptDecisionPresentation(receipt: receipt)
        let coverage = ReceiptCoveragePresentation(evidence: receipt.axes.evidenceStrength)
        let checks = ReceiptCheckRunsPresentation(total: nil, passed: 2, failed: 1)

        XCTAssertEqual(decision.coverageValue, "Not reported")
        XCTAssertEqual(decision.coverageQualifier, "support count unavailable · 4 checkable claims")
        XCTAssertEqual(coverage.rowText, "support count not reported · 4 checkable claims")
        XCTAssertEqual(decision.checksValue, "Total not reported")
        XCTAssertEqual(decision.checksQualifier, "2 passed · 1 failed")
        XCTAssertEqual(checks.rowText, "total not reported · 2 passed · 1 failed")
        XCTAssertFalse(decision.accessibilityLabel.contains("0 of"))
    }

    func testGradeableReceiptWithMissingCoverageTotalIsNotCalledNotGradeable() throws {
        let evidence = try decode(
            ReceiptEvidence.self,
            from: """
            {
              "key": "self_checked",
              "gradeable": true,
              "checked_total": 2
            }
            """
        )

        let presentation = ReceiptCoveragePresentation(evidence: evidence)

        XCTAssertEqual(presentation.value, "Total not reported")
        XCTAssertEqual(presentation.rowText, "2 supported · checkable total not reported")
        XCTAssertFalse(presentation.rowText.contains("not gradeable"))
    }

    func testInconsistentZeroCheckTotalNamesSuppliedTallies() {
        let presentation = ReceiptCheckRunsPresentation(total: 0, passed: 1, failed: 1)

        XCTAssertEqual(presentation.value, "0 total reported")
        XCTAssertEqual(presentation.qualifier, "1 passed · 1 failed · tallies conflict with total")
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

    func testAttentionPresentationUsesCompleteAggregateAndFailsClosedForLegacyTruncation()
        throws
    {
        let payload = try decode(
            ReceiptTasksPayload.self,
            from: """
            {
              "schema": "agentacct.receipt.v1",
              "total": 203,
              "truncated": true,
              "tasks": [
                {
                  "task_id": "recent-complete",
                  "decision_status": { "key": "verified" },
                  "evidence_strength": { "key": "self_checked" },
                  "cost": {}
                }
              ],
              "attention": {
                "total": 3,
                "limit": 2,
                "truncated": true,
                "tasks": [
                  {
                    "task_id": "older-finding",
                    "decision_status": { "key": "finding" },
                    "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
                    "cost": {}
                  },
                  {
                    "task_id": "older-blocked",
                    "decision_status": { "key": "blocked" },
                    "evidence_strength": { "key": "not_gradeable" },
                    "cost": {}
                  }
                ]
              }
            }
            """
        )
        let complete = DashboardAttentionPresentation(
            recentTasks: payload.tasks,
            recentTasksTruncated: payload.truncated,
            attention: payload.attention
        )

        XCTAssertEqual(complete.items.map(\.id), ["older-finding", "older-blocked"])
        XCTAssertEqual(complete.totalCount, 3)
        XCTAssertTrue(complete.isComplete)
        XCTAssertTrue(complete.isTruncated)

        let failedRefresh = DashboardAttentionPresentation(
            recentTasks: payload.tasks,
            recentTasksTruncated: payload.truncated,
            attention: payload.attention,
            fetchError: "receipts fetch failed: connection lost"
        )
        XCTAssertTrue(failedRefresh.items.isEmpty)
        XCTAssertNil(failedRefresh.totalCount)
        XCTAssertFalse(failedRefresh.isComplete)
        XCTAssertFalse(failedRefresh.isTruncated)
        XCTAssertTrue(failedRefresh.isUnavailable)

        let legacy = DashboardAttentionPresentation(
            recentTasks: [],
            recentTasksTruncated: true,
            attention: nil
        )
        XCTAssertTrue(legacy.items.isEmpty)
        XCTAssertNil(legacy.totalCount)
        XCTAssertFalse(legacy.isComplete)
        XCTAssertTrue(legacy.isTruncated)

        let exhaustiveLegacy = DashboardAttentionPresentation(
            recentTasks: [],
            recentTasksTruncated: false,
            attention: nil
        )
        XCTAssertEqual(exhaustiveLegacy.totalCount, 0)
        XCTAssertTrue(exhaustiveLegacy.isComplete)
        XCTAssertFalse(exhaustiveLegacy.isTruncated)
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
            "2 min ago"
        )
        XCTAssertEqual(
            dashboardFreshnessText(Date(timeIntervalSince1970: 1_001)),
            "just now"
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

    private func usagePeriodPresentation(granularity: String?) throws -> UsagePeriodPresentation {
        let filtersEcho = granularity.map {
            ",\"filters_echo\":{\"granularity\":\"\($0)\"}"
        } ?? ""
        let usage = try decode(
            UsageSummary.self,
            from: "{\"by_client\":[],\"by_model\":[]\(filtersEcho)}"
        )
        return UsagePeriodPresentation(usage: usage)
    }
}
