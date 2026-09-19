import XCTest
@testable import agentacct

/// The receipt line items are derived state: each test pins one payload shape
/// so every value is the reducer's string, and a missing string is a named
/// absence — never a measured zero, never a dash.
final class RecordSummaryPresentationTests: XCTestCase {
    private func inputs(
        fieldLabels: ReceiptFieldLabels? = nil,
        coverageTile: ReceiptTileText? = nil,
        checksTile: ReceiptTileText? = nil,
        toolValue: String? = "120", toolQualifier: String? = "tool calls", toolAbsent: String? = nil,
        costDisplayText: String? = "$3.20", costBasisLabel: String? = "provider-billed",
        costState: String? = "complete",
        sessionCount: Int? = 2, sessionRoots: Int = 1
    ) -> RecordSummaryPresentation.Inputs {
        .init(fieldLabels: fieldLabels, coverageTile: coverageTile, checksTile: checksTile,
              toolCalls: .init(value: toolValue, absent: toolAbsent, qualifier: toolQualifier),
              costDisplayText: costDisplayText, costBasisLabel: costBasisLabel, costState: costState,
              sessionCount: sessionCount, sessionRoots: sessionRoots)
    }

    private func item(_ presentation: RecordSummaryPresentation, _ id: String) -> ReceiptSummaryItem? {
        presentation.items.first { $0.id == id }
    }

    private func decode<T: Decodable>(_ type: T.Type, from json: String) throws -> T {
        try JSONDecoder().decode(T.self, from: Data(json.utf8))
    }

    func testChecksRenderTheReducerTileAndNameAMissingTile() {
        let failed = RecordSummaryPresentation(inputs: inputs(
            checksTile: ReceiptTileText(value: "150/156", qualifier: "150 passed · 4 failed · 2 superseded")
        ))
        XCTAssertEqual(item(failed, "checks")?.value, "150/156")
        XCTAssertEqual(item(failed, "checks")?.qualifier, "150 passed · 4 failed · 2 superseded")
        XCTAssertNil(item(failed, "checks")?.absent)

        // The reducer's named absence stays an absence — never a value in the
        // metric face, never a fabricated ratio.
        let none = RecordSummaryPresentation(inputs: inputs(
            checksTile: ReceiptTileText(value: nil, absent: "no checks recorded", qualifier: nil)
        ))
        XCTAssertNil(item(none, "checks")?.value)
        XCTAssertEqual(item(none, "checks")?.absent, "no checks recorded")
        XCTAssertNil(item(none, "checks")?.qualifier)

        let missing = RecordSummaryPresentation(inputs: inputs())
        XCTAssertNil(item(missing, "checks")?.value)
        XCTAssertEqual(item(missing, "checks")?.absent, "checks not reported")
    }

    func testCostRendersDisplayTextWithBasisAndNamedAbsences() {
        let partial = RecordSummaryPresentation(inputs: inputs(
            costDisplayText: "~$1,635.57", costBasisLabel: "pricing estimate", costState: "partial"
        ))
        XCTAssertEqual(item(partial, "cost")?.value, "~$1,635.57")
        XCTAssertEqual(item(partial, "cost")?.qualifier, "pricing estimate")

        let missingBasis = RecordSummaryPresentation(inputs: inputs(costBasisLabel: nil))
        XCTAssertEqual(item(missingBasis, "cost")?.qualifier, "cost basis not reported")

        // The reducer's absence words are the absence, never a value.
        let noUsage = RecordSummaryPresentation(inputs: inputs(
            costDisplayText: "no usage recorded", costBasisLabel: "cost basis not reported", costState: "no_usage"
        ))
        XCTAssertNil(item(noUsage, "cost")?.value)
        XCTAssertEqual(item(noUsage, "cost")?.absent, "no usage recorded")

        let unpriced = RecordSummaryPresentation(inputs: inputs(costDisplayText: "unpriced", costState: "unpriced"))
        XCTAssertNil(item(unpriced, "cost")?.value)
        XCTAssertEqual(item(unpriced, "cost")?.absent, "unpriced")

        let missing = RecordSummaryPresentation(inputs: inputs(costDisplayText: nil, costState: nil))
        XCTAssertNil(item(missing, "cost")?.value)
        XCTAssertEqual(item(missing, "cost")?.absent, "cost not reported")
        XCTAssertFalse(missing.items.contains { $0.value == "—" || $0.absent == "—" })
    }

    /// C1: the coverage ratio is still stated ONCE — but in the TILE strip, at
    /// tile size, not as the largest string on the record. K16 moved it to the
    /// hero to avoid printing it twice; the hero then set `Not gradeable (only
    /// step stopped: handed off)` above the title of a Task that had shipped
    /// and tested a function, so the page shouted the grading system. The
    /// clause is a figure like the others, so it lives with the figures.
    func testTheCoverageRatioIsATileFigureAndLeadsTheStrip() {
        let presentation = RecordSummaryPresentation(inputs: inputs(
            coverageTile: ReceiptTileText(value: "1/1", absent: nil, qualifier: "self-checked completed steps")
        ))
        XCTAssertEqual(presentation.items.map(\.id), ["coverage", "checks", "actions", "cost", "sessions"])
        XCTAssertEqual(item(presentation, "coverage")?.value, "1/1")
        XCTAssertEqual(item(presentation, "coverage")?.qualifier, "self-checked completed steps")
        XCTAssertNil(item(presentation, "coverage")?.absent)
    }

    /// A named state arrives as the tile's ABSENT face, never as a metric — the
    /// shape that carries `not gradeable` out of the hero without turning it
    /// into an 18pt numeral spelled with letters.
    func testANotGradeableCoverageTileIsANamedAbsence() {
        let presentation = RecordSummaryPresentation(inputs: inputs(
            coverageTile: ReceiptTileText(value: nil, absent: "not gradeable",
                                          qualifier: "only step stopped: handed off")
        ))
        XCTAssertNil(item(presentation, "coverage")?.value)
        XCTAssertEqual(item(presentation, "coverage")?.absent, "not gradeable")
        XCTAssertEqual(item(presentation, "coverage")?.qualifier, "only step stopped: handed off")

        // A payload that carried no tile at all still names its absence.
        let missing = RecordSummaryPresentation(inputs: inputs())
        XCTAssertNil(item(missing, "coverage")?.value)
        XCTAssertEqual(item(missing, "coverage")?.absent, "coverage not reported")
    }

    /// Losing the tile must not lose the warning it carried: a conflicting
    /// count set names itself in the HERO headline instead.
    func testInconsistentCountsAreNamedByTheHeroHeadline() throws {
        let evidence = try decode(
            ReceiptEvidence.self,
            from: """
            { "key": "self_checked", "gradeable": true, "checkable_total": 2, "checked_total": 5,
              "coverage_hero": "5/2 self-checked", "coverage_row": "5/2 self-checked" }
            """
        )
        let coverage = ReceiptCoveragePresentation(evidence: evidence)
        XCTAssertTrue(coverage.isInconsistent)
        XCTAssertEqual(evidence.headline, "Inconsistent counts (5 checked · 2 checkable reported)")
        XCTAssertFalse(coverage.valueIsMetric, "a named conflict claimed the metric face")
    }

    func testNotGradeableCoverageRendersTheReducerAbsenceAndReason() throws {
        let evidence = try decode(
            ReceiptEvidence.self,
            from: """
            { "key": "undefined", "gradeable": false, "checkable_total": 0, "checked_total": 0,
              "coverage_hero": "Not gradeable (only step stopped: handed off)", "coverage_row": "not gradeable",
              "coverage_tile": { "value": null, "absent": "not gradeable", "qualifier": "only step stopped: handed off" },
              "coverage_ledger": "1 step handed off",
              "checks_total": 2, "checks_passed": 1, "checks_failed": 1,
              "checks_tile": { "value": "1/2", "absent": null, "qualifier": "1 passed · 1 failed" },
              "check_tally_text": "1/2 passed · 1 failed" }
            """
        )
        let coverage = ReceiptCoveragePresentation(evidence: evidence)
        XCTAssertEqual(coverage.value, "not gradeable")
        XCTAssertEqual(coverage.qualifier, "only step stopped: handed off")
        XCTAssertEqual(coverage.rowText, "not gradeable")
        XCTAssertFalse(coverage.isInconsistent)
        XCTAssertEqual(evidence.coverageLedger, "1 step handed off")
        let checks = ReceiptCheckRunsPresentation(strength: evidence)
        XCTAssertEqual(checks.value, "1/2")
        XCTAssertEqual(checks.qualifier, "1 passed · 1 failed")

        let noChecks = ReceiptCheckRunsPresentation(
            total: 0, passed: 0, failed: 0,
            tile: ReceiptTileText(value: nil, absent: "no checks recorded", qualifier: nil),
            tallyText: "no checks recorded"
        )
        XCTAssertEqual(noChecks.value, "no checks recorded")
        XCTAssertEqual(noChecks.qualifier, "")
    }

    func testFieldLabelsComeFromThePayload() {
        let labels = ReceiptFieldLabels(decision: "Decision", coverage: "Coverage", checks: "Checks",
                                        cost: "Cost", agents: "Agents")
        let presentation = RecordSummaryPresentation(inputs: inputs(fieldLabels: labels))
        XCTAssertEqual(presentation.items.map(\.label), ["Coverage", "Checks", "Tool calls", "Cost", "Sessions"])

        let renamed = RecordSummaryPresentation(inputs: inputs(
            fieldLabels: ReceiptFieldLabels(coverage: "Step coverage", checks: "Check results", cost: "Spend")
        ))
        XCTAssertEqual(renamed.items.map(\.label), ["Step coverage", "Check results", "Tool calls", "Spend", "Sessions"])
    }

    func testSessionsQualifyMultipleRootsAndAbsence() {
        let multiple = RecordSummaryPresentation(inputs: inputs(sessionCount: 4, sessionRoots: 3))
        XCTAssertEqual(item(multiple, "sessions")?.value, "4")
        XCTAssertEqual(item(multiple, "sessions")?.qualifier, "3 roots")

        let single = RecordSummaryPresentation(inputs: inputs(sessionCount: 1, sessionRoots: 1))
        XCTAssertNil(item(single, "sessions")?.qualifier)

        let missing = RecordSummaryPresentation(inputs: inputs(sessionCount: nil))
        XCTAssertEqual(item(missing, "sessions")?.absent, "not recorded")
    }

    func testReceiptPayloadStringsFlowIntoTheLineItems() throws {
        let receipt = try decode(
            Receipt.self,
            from: """
            {
              "schema_version": "agentacct.receipt.v1",
              "task_id": "task-1",
              "field_labels": { "decision": "Decision", "coverage": "Coverage", "checks": "Checks",
                                "cost": "Cost", "agents": "Agents" },
              "axes": {
                "decision_status": { "key": "reported", "label": "Reported" },
                "evidence_strength": {
                  "key": "self_checked", "gradeable": true,
                  "checkable_total": 2, "checked_total": 1,
                  "by_tier": { "externally_verified": 0, "independently_checked": 0,
                               "self_checked": 1, "unchecked": 1 },
                  "coverage_tile": { "value": "1/2", "qualifier": "self-checked" },
                  "checks_tile": { "value": "1/1", "qualifier": "passed" }
                }
              },
              "dimensions": {
                "task": { "boundary": { "session_count": 1 } }, "actors": {}, "actions": {},
                "cost": { "estimated_cost_usd": 10.77, "cost_complete": true,
                          "state": "complete", "display_text": "≈$10.77",
                          "basis_label": "pricing estimate" },
                "evidence": { "checks_total": 1, "checks_passed": 1, "checks_failed": 0,
                              "checks_tile": { "value": "1/1", "qualifier": "passed · 1 earlier run failed" } },
                "outcome": {}, "gaps": {}, "provenance": {}
              }
            }
            """
        )

        let presentation = RecordSummaryPresentation(receipt: receipt, summary: nil)
        XCTAssertEqual(item(presentation, "checks")?.value, "1/1")
        // The evidence dimension's tile wins over the axis copy.
        XCTAssertEqual(item(presentation, "checks")?.qualifier, "passed · 1 earlier run failed")
        XCTAssertEqual(item(presentation, "cost")?.value, "≈$10.77")
        XCTAssertEqual(item(presentation, "cost")?.qualifier, "pricing estimate")
        XCTAssertEqual(item(presentation, "sessions")?.value, "1")
    }

    func testFixtureReceiptProducesTheOrderedLineItems() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "dashboard", withExtension: "json"))
        let fixture = try DashboardSnapshotFixture.load(from: url)
        let receipt = try XCTUnwrap(fixture.work?.receipt)

        let presentation = RecordSummaryPresentation(receipt: receipt, summary: nil)
        XCTAssertEqual(presentation.items.map(\.id), ["coverage", "checks", "actions", "cost", "sessions"])
        XCTAssertEqual(presentation.items.map(\.label), ["Coverage", "Checks", "Tool calls", "Cost", "Sessions"])
        // Every line item is either a value with its label or a named absence.
        for item in presentation.items {
            XCTAssertTrue(item.value != nil || item.absent != nil, item.id)
            XCTAssertNotEqual(item.value, "—", item.id)
            XCTAssertNotEqual(item.absent, "—", item.id)
        }
        // The Tool calls tile is the reducer's `actions_tile`, verbatim.
        let tile = try XCTUnwrap(receipt.dimensions.actions.actionsTile)
        let actions = try XCTUnwrap(presentation.items.first(where: { $0.id == "actions" }))
        XCTAssertEqual(actions.value, tile.value)
        XCTAssertEqual(actions.qualifier, tile.qualifier)
        XCTAssertEqual(actions.absent, tile.absent)
    }
}

/// The B1 payload contract as the Swift models decode it: reducer strings are
/// rendered verbatim and a missing string is a named absence, never a dash.
final class PayloadContractDecodingTests: XCTestCase {
    private func decode<T: Decodable>(_ type: T.Type, from json: String) throws -> T {
        try JSONDecoder().decode(T.self, from: Data(json.utf8))
    }

    func testShortSessionIdCutsAtATokenBoundaryAndNeverEndsInASeparator() {
        XCTAssertEqual(RecentSession.shortId("live-fx-1234"), "live-fx")
        XCTAssertEqual(RecentSession.shortId("0792232e-5b26-46ec"), "0792232e")
        XCTAssertEqual(RecentSession.shortId("abcdefghijkl"), "abcdefgh")
        XCTAssertEqual(RecentSession.shortId("run_ab_cdefgh"), "run_ab")
        XCTAssertEqual(RecentSession.shortId("short-"), "short")
        XCTAssertEqual(RecentSession.shortId("abc"), "abc")
        for id in ["live-fx-1234", "a_b_c_d_e_f_g", "--------x", "x-------y"] {
            let short = RecentSession.shortId(id)
            XCTAssertFalse(short.isEmpty, id)
            XCTAssertLessThanOrEqual(short.count, 8, id)
            XCTAssertFalse(short.hasSuffix("-") || short.hasSuffix("_"), id)
        }
    }

    func testVerdictAttentionAndCostDecodeReducerStrings() throws {
        let summary = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "task-1",
              "verdict": {
                "headline": "Blocked — 0/1 checked",
                "proof_clause": "0/1 checked",
                "decision_key": "blocked",
                "gap": "1 completed step unchecked · no usage recorded",
                "gap_evidence": ["1 completed step unchecked"],
                "gap_cost": ["no usage recorded"],
                "gap_label": "Not yet proven",
                "gap_text": "1 completed step unchecked",
                "ledger_text": null
              },
              "decision_status": { "key": "blocked", "label": "Blocked",
                                   "asserted_by_label": "Agent-reported" },
              "evidence_strength": {
                "key": "unchecked", "gradeable": true, "checkable_total": 1, "checked_total": 0,
                "by_tier": { "unchecked": 1 },
                "coverage_hero": "0/1 checked · 1 unchecked", "coverage_row": "0/1 checked",
                "coverage_tile": { "value": "0/1", "absent": null, "qualifier": "completed steps checked" },
                "checks_total": 1, "checks_passed": 0, "checks_failed": 1,
                "checks_tile": { "value": "0/1", "absent": null, "qualifier": "0 passed · 1 failed" },
                "check_tally_text": "0/1 passed · 1 failed",
                "check_runs_state": "failed"
              },
              "attention": {
                "kind": "failed_check", "reason_label": "Failed check", "summary": "pytest",
                "label": "Failed test check · pytest · exit 2", "open": true,
                "effects": { "reviewed": "Marks the finding reviewed by you; it stays a Finding until resolved.",
                             "resolved": null }
              },
              "attention_open": true,
              "cost": { "state": "no_usage", "display_text": "no usage recorded",
                        "basis_label": "cost basis not reported",
                        "legend": "~$ partial subtotal · ≈$ estimate · $ reported or billed" }
            }
            """
        )

        XCTAssertEqual(summary.verdict?.gapLine, "Not yet proven — 1 completed step unchecked")
        XCTAssertEqual(summary.verdict?.badgeClause, "0/1 checked")
        XCTAssertNil(summary.verdict?.ledgerText)
        XCTAssertEqual(summary.verdict?.gapCost, ["no usage recorded"])
        XCTAssertEqual(summary.evidenceStrength.headline, "0/1 checked · 1 unchecked")
        XCTAssertEqual(summary.evidenceStrength.compactHeadline, "0/1 checked")
        XCTAssertEqual(summary.evidenceStrength.checkRunsState, "failed")
        let checks = ReceiptCheckRunsPresentation(strength: summary.evidenceStrength)
        XCTAssertEqual(checks.value, "0/1")
        XCTAssertEqual(checks.qualifier, "0 passed · 1 failed")
        XCTAssertEqual(checks.rowText, "0/1 passed · 1 failed")
        XCTAssertEqual(summary.attention?.reasonLabel, "Failed check")
        XCTAssertEqual(summary.attention?.label, "Failed test check · pytest · exit 2")
        XCTAssertEqual(summary.attention?.open, true)
        XCTAssertNil(summary.attention?.effects?.resolved)
        XCTAssertEqual(summary.attentionOpen, true)
        XCTAssertEqual(summary.cost.text, "no usage recorded")
        XCTAssertTrue(summary.cost.isAbsent)
        XCTAssertEqual(summary.decisionStatus.assertedByLabel, "Agent-reported")
    }

    func testCostDimensionCarriesPlanShareHeadlineAndNeverADash() throws {
        let dim = try decode(
            ReceiptCostDim.self,
            from: """
            { "estimated_cost_usd": 10.77, "cost_complete": true, "state": "complete",
              "display_text": "≈$10.77", "basis_label": "pricing estimate",
              "plan_share_headline": "won't calibrate at current ratio",
              "plan_share": { "pct": null, "calibration_state": "out_of_band" } }
            """
        )
        XCTAssertEqual(dim.text, "≈$10.77 · pricing estimate")
        XCTAssertEqual(dim.planShare?.rowSummary, "won't calibrate at current ratio")
        XCTAssertEqual(dim.planShareText, "won't calibrate at current ratio")

        let empty = try decode(ReceiptCostDim.self, from: "{}")
        XCTAssertEqual(empty.text, "cost not reported")
        XCTAssertEqual(empty.planShareText, "plan share not reported")

        let unknownShare = try decode(ReceiptPlanShare.self, from: #"{"calibration_state": "mystery"}"#)
        XCTAssertEqual(unknownShare.rowSummary, "plan share not reported")
    }

    func testGlanceLimitsAndUsageDecodeStatesAndNamedAbsences() throws {
        let glance = try decode(
            Glance.self,
            from: """
            {
              "schema": "agentacct.glance.v1",
              "usage": {
                "windows": [
                  { "label": "today", "days": 1,
                    "totals": { "rows": 0, "fresh_tokens": 0, "usage_availability": "unknown",
                                "cost_state": "none_recorded",
                                "cost_confidence_display": "cost basis not reported" } },
                  { "label": "last 7 days", "days": 7,
                    "totals": { "rows": 3, "fresh_tokens": 10, "usage_availability": "available",
                                "cost_state": "unpriced" } }
                ]
              },
              "limits": [
                { "client": "claude-code",
                  "windows": [ { "kind": "5h", "used_percent": 12, "resets_at": 1,
                                 "window_label": "5-hour limit",
                                 "reset_text": "reset passed Sep 13, 10:50 PM" } ],
                  "plan_share": { "calibration_state": "calibrated",
                                  "chip_text": "plan share ready",
                                  "headline": "Weekly plan share is estimated from your recorded 7-day limit history" } }
              ],
              "plan": [],
              "recent_sessions": [
                { "client": "codex", "session_id": "live-fx-1234", "plan_pct": null,
                  "plan_share": { "pct": null, "calibration_state": "never",
                                  "headline": "not applicable for this client" } }
              ]
            }
            """
        )

        let today = glance.usage.windows[0].totals
        XCTAssertTrue(today.hasNoUsage)
        XCTAssertEqual(today.costText, "no usage recorded")
        XCTAssertEqual(glance.usage.windows[1].totals.costText, "unpriced")
        XCTAssertEqual(glance.limits[0].windows?.first?.windowLabelText, "5-hour limit")
        XCTAssertEqual(glance.limits[0].windows?.first?.resetLabelText, "reset passed Sep 13, 10:50 PM")
        XCTAssertEqual(
            glance.limits[0].planShare?.headlineText,
            "Weekly plan share is estimated from your recorded 7-day limit history"
        )
        XCTAssertEqual(glance.limits[0].planShare?.chipText, "plan share ready")
        XCTAssertEqual(glance.recentSessions[0].shortSessionId, "live-fx")
        XCTAssertEqual(glance.recentSessions[0].planShare?.headlineText, "not applicable for this client")

        let bare = try decode(LimitWindow.self, from: #"{"kind": "7d"}"#)
        XCTAssertEqual(bare.resetLabelText, "reset time not reported")
        XCTAssertEqual(bare.windowLabelText, "limit window")
        XCTAssertEqual(try decode(UsageTotals.self, from: "{}").tokensText, "not reported")
    }

    func testPeriodBucketsNameEachCostState() throws {
        let periods = try decode(
            [PeriodBucket].self,
            from: """
            [
              { "period": "2026-09-13", "rows": 0, "cost_state": "none_recorded",
                "usage_availability": "unknown" },
              { "period": "2026-09-14", "rows": 4, "cost_state": "unpriced" },
              { "period": "2026-09-15", "rows": 4, "cost_state": "partial",
                "known_additive_cost_usd": 69.97, "cost_complete": false,
                "cost_confidence_display": "mixed · mostly pricing estimate" }
            ]
            """
        )
        XCTAssertEqual(periods.map(\.costState), ["none_recorded", "unpriced", "partial"])
        XCTAssertEqual(periods[0].costText, "no usage recorded")
        XCTAssertEqual(periods[1].costText, "unpriced")
        XCTAssertEqual(periods[2].costText, "~$69.97")
        XCTAssertEqual(periods[2].costConfidenceDisplay, "mixed · mostly pricing estimate")
    }

    func testTimelineEventsDecodeReducerFields() throws {
        let event = try decode(
            TaskTimelineEvent.self,
            from: """
            { "id": "event:1", "kind": "check", "title": "pytest", "name": "pytest", "status": "failed",
              "revision_label": "at 8a4e024 · main", "supersedes_check_event_id": "evt_0",
              "command_state_text": "The agent's command argument was not stored; the title is the name the agent recorded.",
              "is_current_failure": true }
            """
        )
        XCTAssertEqual(event.name, "pytest")
        XCTAssertEqual(event.revisionLabel, "at 8a4e024 · main")
        XCTAssertEqual(event.supersedesCheckEventID, "evt_0")
        XCTAssertEqual(event.isCurrentFailure, true)

        let work = try decode(
            TaskTimelineEvent.self,
            from: """
            { "id": "work:1", "kind": "work", "title": "Block", "status": "blocked",
              "section_kind": "implementation", "next_step": "retry",
              "evidence_grade": "none", "evidence_grade_label": "not graded",
              "evidence_grade_reason": "Blocked before completion — not graded" }
            """
        )
        XCTAssertEqual(work.kind, "work")
        XCTAssertEqual(work.sectionKind, "implementation")
        XCTAssertEqual(work.nextStep, "retry")
        XCTAssertEqual(work.evidenceGradeLabel, "not graded")
    }
}
