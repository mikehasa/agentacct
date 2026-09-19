import XCTest
@testable import agentacct

/// Guards for the payload fields the app decoded-and-dropped, or never decoded
/// at all. Each test decodes the SHAPE the reducer actually emits (copied from
/// a live `/v1/receipt` and `/v1/task-timeline` response) rather than a
/// hand-built model value, so a renamed key fails here instead of silently
/// rendering an absence.
final class WorkEvidenceRenderingTests: XCTestCase {

    // MARK: - the recorded check table (B1) and its recovery story (B5)

    /// The fail → pass pair as `/v1/receipt` sends it: an earlier run marked
    /// `history_run`, then the standing run pointing back at it.
    private static let recoveryChecksJSON = """
    [
      {"name": "python -m pytest tests/test_percent.py",
       "title": "python -m pytest tests/test_percent.py",
       "result": "failed", "result_label": "Failed", "result_tone": "failure",
       "evidence_type": "test", "exit_code": 1, "at": 1789441995.084926,
       "event_id": "evt_c53bd242114c", "history_run": true, "superseded": true,
       "superseded_by_event_id": "evt_7765ddb9842f",
       "command_state": "agent_recorded",
       "command_state_text": "The agent recorded this check's command; the receipt shows the name it recorded, not the command text.",
       "revision_label": "HEAD when recorded: 484fa16 · main · uncommitted changes",
       "revision_absent_files": [], "revision_contradiction_text": null,
       "runs_total": 2, "earlier_failed": 0,
       "files": ["moneyutil/core.py", "tests/test_percent.py"]},
      {"name": "python -m pytest tests/test_percent.py",
       "title": "python -m pytest tests/test_percent.py",
       "result": "passed", "result_label": "Passed", "result_tone": "pass",
       "evidence_type": "test", "exit_code": 0, "at": 1789441995.24246,
       "event_id": "evt_7765ddb9842f", "history_run": false, "superseded": false,
       "supersedes_check_event_id": "evt_c53bd242114c",
       "supersedes_basis": "reciprocal_of_supersession",
       "command_state": "agent_recorded",
       "revision_label": "HEAD when recorded: 8a4e024 · main · uncommitted changes",
       "runs_total": 2, "earlier_failed": 1,
       "files": ["moneyutil/core.py", "tests/test_percent.py"]}
    ]
    """

    func testBothRunsOfARecoveredCheckSurviveDecodingAsDistinctRows() throws {
        let checks = try JSONDecoder().decode([ReceiptCheck].self,
                                              from: Data(Self.recoveryChecksJSON.utf8))
        XCTAssertEqual(checks.count, 2)
        // Identity is the EVENT. The old name/result/exit triple collided the
        // moment two runs of one check identity reached the same list, which
        // is exactly when the recovery story exists.
        XCTAssertEqual(checks.map(\.id), ["evt_c53bd242114c", "evt_7765ddb9842f"])
        XCTAssertEqual(Set(checks.map(\.id)).count, 2, "two runs must not share a row identity")

        let failed = checks[0], passed = checks[1]
        XCTAssertEqual(failed.historyRun, true)
        XCTAssertEqual(failed.supersededByEventId, "evt_7765ddb9842f")
        XCTAssertEqual(passed.supersedesCheckEventId, "evt_c53bd242114c")
        XCTAssertEqual(passed.supersedesBasis, "reciprocal_of_supersession")
        XCTAssertEqual(failed.commandState, "agent_recorded")
        XCTAssertEqual(failed.revisionText, "HEAD when recorded: 484fa16 · main · uncommitted changes")
        XCTAssertEqual(passed.revisionText, "HEAD when recorded: 8a4e024 · main · uncommitted changes")
    }

    /// The third state the canvas and the table both need: a failure that is
    /// answered. It is neither a live failure nor neutral history.
    func testAResolvedFailureIsItsOwnStateOnBothTheReceiptAndTheTimeline() throws {
        let checks = try JSONDecoder().decode([ReceiptCheck].self,
                                              from: Data(Self.recoveryChecksJSON.utf8))
        XCTAssertTrue(checks[0].isResolvedFailure)
        XCTAssertFalse(checks[1].isResolvedFailure)

        let failed = Self.record(id: "a", kind: .check, resultTone: "failure",
                                 superseded: true, supersededBy: "evt_later")
        let live = Self.record(id: "b", kind: .check, resultTone: "failure")
        let passed = Self.record(id: "c", kind: .check, resultTone: "pass")

        XCTAssertTrue(failed.isResolvedFailure)
        XCTAssertFalse(failed.isCurrentFailure, "an answered failure owes the reviewer nothing")
        XCTAssertEqual(failed.resolvedByEventID, "evt_later")
        XCTAssertTrue(live.isCurrentFailure)
        XCTAssertFalse(live.isResolvedFailure)
        XCTAssertFalse(passed.isResolvedFailure)

        // Three distinguishable marks, and none of them is the interactive
        // accent: colour separates failure from neutral, SHAPE separates an
        // answered failure from a live one.
        XCTAssertEqual(failed.markTint, Theme.coral)
        XCTAssertEqual(live.markTint, Theme.coral)
        XCTAssertEqual(passed.markTint, Theme.chartNeutral)
        XCTAssertTrue(failed.markIsHollow)
        XCTAssertFalse(live.markIsHollow)
        XCTAssertFalse(passed.markIsHollow)
    }

    // MARK: - lane, kind and next step (B4a/B4b)

    func testTimelineEventsCarryTheReducersLaneKindAndNextStep() throws {
        let json = """
        {"id": "event:evt_1", "event_id": "evt_1", "kind": "check", "title": "pytest",
         "status": "failed", "status_label": "Failed · superseded",
         "lane": "evidence", "lane_label": "Check evidence",
         "note_text": "The recorded result and exit code disagree.",
         "occurred_at": 1789441995.084926, "started_at": 1789441995.084926}
        """
        let event = try JSONDecoder().decode(TaskTimelineEvent.self, from: Data(json.utf8))
        XCTAssertEqual(event.lane, "evidence")
        XCTAssertEqual(event.laneLabel, "Check evidence")

        let record = try XCTUnwrap(event.record(taskID: "task"))
        XCTAssertEqual(record.lane, "evidence")
        XCTAssertEqual(record.laneLabel, "Check evidence")
        XCTAssertEqual(record.laneBand, .evidence)
        XCTAssertFalse(record.laneBand.isAbove)
        // The disagreement the reducer named reaches the record that renders it.
        XCTAssertEqual(record.noteText, "The recorded result and exit code disagree.")

        let step = """
        {"id": "work:1", "kind": "work", "title": "fix rounding", "status": "completed",
         "status_label": "Reported completed", "lane": "primary", "lane_label": "Primary session",
         "section_kind": "debugging", "next_step": "land the PR", "started_at": 1}
        """
        let stepRecord = try XCTUnwrap(
            JSONDecoder().decode(TaskTimelineEvent.self, from: Data(step.utf8)).record(taskID: "task"))
        XCTAssertEqual(stepRecord.laneBand, .work)
        XCTAssertEqual(stepRecord.sectionKind, "debugging")
        XCTAssertEqual(stepRecord.nextStep, "land the PR")
    }

    /// A payload predating the lane grammar still lands checks and steps on
    /// opposite sides — the fallback states the same fact, weaker.
    func testAPayloadWithoutALaneStillSeparatesChecksFromSteps() {
        XCTAssertEqual(Self.record(id: "a", kind: .check, resultTone: "pass").laneBand, .evidence)
        XCTAssertEqual(Self.record(id: "b", kind: .step, resultTone: nil).laneBand, .work)
    }

    // MARK: - the opening surface (B2)

    /// A list ORDERS; a canvas POSITIONS. So the opening surface turns on one
    /// question — does the time axis separate these records — and never on how
    /// many there are.
    func testAShortSpanKeepsTheCanvasBecausePositionStillCarriesTheShape() {
        // Three records at :12, :14, :17 — bunched, then a pause. Ordering
        // destroys that shape, so a third of a second still earns the canvas.
        XCTAssertFalse(WorkTimelineView.listSuitsData(records: Self.stamped([12, 14, 17])))
        // Two records a third of a second apart are still two positions.
        XCTAssertFalse(WorkTimelineView.listSuitsData(records: Self.stamped([100, 100.33])))
        // A long, populated task keeps the canvas too — same reason, not count.
        XCTAssertFalse(WorkTimelineView.listSuitsData(records: Self.stamped([0, 90_000, 180_000])))
    }

    func testRecordsThatShareOneStampAreAListAtAnyN() {
        for count in [1, 5, 20, 200] {
            XCTAssertTrue(
                WorkTimelineView.listSuitsData(records: Self.stamped(Array(repeating: 1_700, count: count))),
                "\(count) records sharing one stamp cannot be separated on a time axis"
            )
        }
        // A single record is one position, so a time axis buys nothing.
        XCTAssertTrue(WorkTimelineView.listSuitsData(records: Self.stamped([1_700])))
        // Unusable stamps leave no positions at all, however many records.
        XCTAssertTrue(WorkTimelineView.listSuitsData(records: Self.stamped(Array(repeating: Double?.none, count: 20))))
        XCTAssertTrue(WorkTimelineView.listSuitsData(records: Self.stamped([Double.infinity, Double.nan, 0, -5])))
        // Nothing loaded is not a reason to choose either surface.
        XCTAssertFalse(WorkTimelineView.listSuitsData(records: []))
    }

    /// The reading surface a reviewer asked for outlives the task they asked on
    /// it: widest scope loses, so the per-task override beats the app-wide
    /// default, which beats the data.
    func testTheSurfaceResolvesPerTaskThenAppWideThenFromTheData() {
        let burst = Self.stamped([1_700, 1_700, 1_700])   // data says list
        let spread = Self.stamped([1_700, 1_760])         // data says canvas

        // A per-task override wins over both the app default and the data.
        XCTAssertFalse(WorkTimelineView.usesRecordList(chosenForTask: false, appDefault: true, records: burst))
        XCTAssertTrue(WorkTimelineView.usesRecordList(chosenForTask: true, appDefault: false, records: spread))

        // With no override, the app-wide default wins over the data — this is
        // what stops a reviewer re-pressing "Show timeline" on every task.
        XCTAssertFalse(WorkTimelineView.usesRecordList(chosenForTask: nil, appDefault: false, records: burst))
        XCTAssertTrue(WorkTimelineView.usesRecordList(chosenForTask: nil, appDefault: true, records: spread))

        // With neither, the data decides.
        XCTAssertTrue(WorkTimelineView.usesRecordList(chosenForTask: nil, appDefault: nil, records: burst))
        XCTAssertFalse(WorkTimelineView.usesRecordList(chosenForTask: nil, appDefault: nil, records: spread))
    }

    /// `@AppStorage` cannot hold `Bool?`, and the third state is load-bearing:
    /// "never chosen" must stay distinguishable from "chose the list".
    func testTheAppWideDefaultKeepsNeverChosenDistinctFromChoseTheList() {
        XCTAssertNil(WorkTimelineSurfaceDefault.choice(WorkTimelineSurfaceDefault.unset))
        XCTAssertEqual(WorkTimelineSurfaceDefault.choice(WorkTimelineSurfaceDefault.stored(true)), true)
        XCTAssertEqual(WorkTimelineSurfaceDefault.choice(WorkTimelineSurfaceDefault.stored(false)), false)
    }

    private static func stamped(_ starts: [Double?]) -> [WorkTimelineRecord] {
        starts.enumerated().map { index, start in
            var record = Self.record(id: "r\(index)", kind: .step, resultTone: nil)
            record.start = start
            record.end = nil
            return record
        }
    }

    // MARK: - the captured action ledger (B4d)

    func testTheActionsDimensionDecodesTheLedgerTheCLIAlreadyPrints() throws {
        let json = """
        {"tool_name_counts": {"Bash": 25, "Read": 26, "Agent": 3},
         "tool_name_total": 54, "tool_names_elided": 2,
         "commands": ["git status", "swift build"], "command_count": 5, "commands_elided": 3,
         "touched_files": ["src/agentacct/receipt.py", "apps/agentacct/Sources/agentacct/WorkPane.swift"],
         "touched_files_elided": 1, "touched_file_count": 3,
         "capture_coverage": {"captured_first_at": 1789258879.0, "captured_last_at": 1789259880.9,
           "activity_first_at": 1789258834.0, "activity_last_at": 1789442196.8,
           "record_shortfalls": [{"call_label": "record_machine_check",
             "record_label": "recorded check", "captured": 0, "recorded": 4}]},
         "actions_synopsis": {"state": "partial", "headline": "60 tool calls captured",
           "integrity_detail": "captured 60 calls covering part of the task", "metrics": []}}
        """
        let actions = try JSONDecoder().decode(ReceiptActionsDim.self, from: Data(json.utf8))

        // Counts by tool NAME, ordered by count then name so the list is stable.
        XCTAssertEqual(actions.toolNameRows.map(\.name), ["Read", "Bash", "Agent"])
        XCTAssertEqual(actions.toolNameRows.map(\.count), [26, 25, 3])
        XCTAssertEqual(actions.toolNamesElided, 2)
        XCTAssertEqual(actions.commandLines, ["git status", "swift build"])
        XCTAssertEqual(actions.commandsElided, 3)
        XCTAssertEqual(actions.touchedFileLines.count, 2)
        XCTAssertEqual(actions.touchedFilesElided, 1)
        XCTAssertEqual(actions.captureCoverage?.recordShortfalls.first?.recorded, 4)
        XCTAssertEqual(actions.captureCoverage?.recordShortfalls.first?.captured, 0)
    }

    /// `partial` is counted evidence, not a named absence. Without its own arm
    /// it fell through to `captureUnknown`, and a real capture of sixty calls
    /// was drawn in the muted tone that means "nothing was captured".
    func testPartialCaptureIsCountedEvidenceAndNotANamedAbsence() {
        let partial = ReceiptActionIntegrity(payload: "partial")
        XCTAssertEqual(partial, .partial)
        XCTAssertFalse(partial.isAbsence)
        XCTAssertTrue(ReceiptActionIntegrity(payload: "capture_unknown").isAbsence)
        // An unknown state still degrades to the honest absence.
        XCTAssertEqual(ReceiptActionIntegrity(payload: "something_new"), .captureUnknown)
    }

    // MARK: - the coverage meter (C2)

    /// `/v1/receipt` for task_87729af1: one checkable step, self-checked and
    /// passing, in a FIVE step Task — two still open, two out of scope.
    private static let partialCoverageJSON = """
    { "key": "self_checked", "gradeable": true, "strongest_tier": "self_checked",
      "checkable_total": 1, "checked_total": 1,
      "by_tier": {"externally_verified": 0, "independently_checked": 0, "self_checked": 1, "unchecked": 0},
      "not_checkable": 2, "open_or_incomplete": 2, "still_open": 2, "total_steps": 5,
      "coverage_hero": "1/1 self-checked", "coverage_row": "1/1 self-checked",
      "coverage_tile": {"value": "1/1", "absent": null, "qualifier": "self-checked completed steps"},
      "coverage_ledger": "2 steps still open · 2 not check-relevant",
      "definition": "Counts are passing checks over checkable steps, split by how independent each check is." }
    """

    /// The bar drew `checked / checkable`, so `1/1 self-checked` filled the
    /// whole track while three of the Task's five steps were open or out of
    /// scope — a solid full-width bar that could not express incompleteness.
    /// It is now drawn over every recorded step.
    func testACoverageBarCannotFillWhileStepsAreStillOpen() throws {
        let evidence = try JSONDecoder().decode(ReceiptEvidence.self,
                                                from: Data(Self.partialCoverageJSON.utf8))
        let bar = CoverageBar(evidence: evidence)
        XCTAssertEqual(bar.denominator, evidence.totalSteps, "the bar is not drawn over every recorded step")
        XCTAssertEqual(bar.denominator, 5)

        let visible = bar.visible
        XCTAssertEqual(visible.map(\.kind), [.tier("self_checked"), .open, .notCheckRelevant])
        XCTAssertEqual(visible.map(\.count), [1, 2, 2])
        // The proven span is ONE FIFTH of the track, not all of it.
        let proven = visible.first { $0.grade == "self_checked" }?.count ?? 0
        XCTAssertLessThan(Double(proven) / Double(bar.denominator), 1.0)

        // The three classes are three different marks, so the incompleteness is
        // visible and not carried by hue alone.
        XCTAssertEqual(Set(visible.map(\.kind)).count, 3)
    }

    /// A fully proven Task still fills the bar — the guard above is a real
    /// threshold, not "the bar can never be full".
    func testAFullyCheckedTaskStillFillsTheBar() throws {
        let evidence = try JSONDecoder().decode(
            ReceiptEvidence.self,
            from: Data("""
            { "key": "self_checked", "gradeable": true, "checkable_total": 1, "checked_total": 1,
              "by_tier": {"externally_verified": 0, "independently_checked": 0, "self_checked": 1, "unchecked": 0},
              "not_checkable": 0, "open_or_incomplete": 0, "total_steps": 1 }
            """.utf8)
        )
        let bar = CoverageBar(evidence: evidence)
        XCTAssertEqual(bar.denominator, 1)
        XCTAssertEqual(bar.visible.map(\.kind), [.tier("self_checked")])
    }

    /// The bar is a DATA mark. Every tier it can paint must stay out of the
    /// app's one interactive voice — `self_checked` was literally `Theme.accent`,
    /// so the filled half of every coverage bar read as a control (K04).
    func testNoEvidenceTierMarkWearsTheInteractiveAccent() {
        for grade in ["externally_verified", "independently_checked", "self_checked",
                      "unchecked", "claimed", "none"] {
            let style = EvidenceTierStyle.forGrade(grade)
            XCTAssertNotEqual(style.tint, Theme.accent, "\(grade) tier mark wears the interactive accent")
            XCTAssertNotEqual(style.fill, Theme.accent, "\(grade) tier fill wears the interactive accent")
        }
    }

    // MARK: - the standing proof (C3 / C5)

    private static func receipt(checks: String, timeline: String = "[]",
                                attention: String = "null") throws -> Receipt {
        try JSONDecoder().decode(Receipt.self, from: Data("""
        { "schema_version": "agentacct.receipt.v1", "task_id": "task_x", "title": "Fix it",
          "attention": \(attention),
          "axes": {
            "decision_status": {"key": "verified", "label": "Verified"},
            "evidence_strength": {"key": "self_checked", "strongest_tier": "self_checked",
              "tier_legend": [{"key": "self_checked", "label": "self-checked", "definition": "d"}]}
          },
          "dimensions": {
            "task": {}, "actors": {}, "actions": {}, "cost": {},
            "evidence": {"checks": \(checks)},
            "outcome": {}, "gaps": {}, "provenance": {}
          },
          "timeline": {"events": \(timeline), "shown": 0, "total": 0, "truncated": false}
        }
        """.utf8))
    }

    /// The record reserved ONE slot that names a check in full — and filled it
    /// only when something was WRONG, so a passing Task showed strictly less
    /// evidence than a failing one (C3). The standing proof is now nameable
    /// from the same payload, with every fact the attention block has.
    func testAPassingTaskCanNameItsStandingProof() throws {
        let timeline = """
        [{"id": "e1", "kind": "work", "title": "Fix percentage()", "status": "completed",
          "evidence_grade": "self_checked", "evidence_grade_label": "self-checked",
          "evidence_grade_reason": "the agent reported a check passed (python -m pytest tests/test_percent.py) — the agent's own, not independent"}]
        """
        let receipt = try Self.receipt(checks: Self.recoveryChecksJSON, timeline: timeline)
        let proof = try XCTUnwrap(ReceiptStandingProof(receipt: receipt))

        // The STANDING run, never the earlier failure it replaced.
        XCTAssertEqual(proof.check.eventId, "evt_7765ddb9842f")
        XCTAssertEqual(proof.check.resultLabel, "Passed")
        XCTAssertTrue(proof.identityLine.contains("python -m pytest tests/test_percent.py"))
        XCTAssertTrue(proof.identityLine.contains("Passed"))
        XCTAssertTrue(proof.identityLine.contains("Exit 0"))
        XCTAssertTrue(proof.identityLine.contains("test"))
        XCTAssertEqual(proof.tierKey, "self_checked")
        XCTAssertEqual(proof.tierLabel, "self-checked")
        XCTAssertEqual(
            proof.gradeReason,
            "the agent reported a check passed (python -m pytest tests/test_percent.py) — the agent's own, not independent"
        )
    }

    /// A Task with nothing standing has no proof to name — the slot stays for
    /// the attention block rather than inventing a positive one.
    func testAFailingOrHistoryOnlyTaskHasNoStandingProof() throws {
        let historyOnly = """
        [{"name": "pytest", "result": "passed", "result_tone": "pass", "exit_code": 0,
          "at": 1, "event_id": "evt_old", "history_run": true, "superseded": true}]
        """
        XCTAssertNil(ReceiptStandingProof(receipt: try Self.receipt(checks: historyOnly)))

        let failedOnly = """
        [{"name": "pytest", "result": "failed", "result_tone": "failure", "exit_code": 1,
          "at": 1, "event_id": "evt_bad"}]
        """
        XCTAssertNil(ReceiptStandingProof(receipt: try Self.receipt(checks: failedOnly)))
    }

    /// C5: a hero that says Verified over a tree with uncommitted changes owes
    /// the reader that qualifier — in the reducer's words, never composed here.
    func testTheProvingRunCarriesItsDirtyRevisionQualifier() throws {
        let dirty = """
        [{"name": "pytest", "result": "passed", "result_tone": "pass", "exit_code": 0,
          "at": 2, "event_id": "evt_ok",
          "revision": {"commit": "8a4e024", "branch": "main", "dirty": true, "basis": "host_hook"},
          "revision_label": "ran at 8a4e024 · main · uncommitted changes"}]
        """
        let proof = try XCTUnwrap(ReceiptStandingProof(receipt: try Self.receipt(checks: dirty)))
        XCTAssertEqual(proof.dirtyRevisionLabel, "ran at 8a4e024 · main · uncommitted changes")

        let clean = """
        [{"name": "pytest", "result": "passed", "result_tone": "pass", "exit_code": 0,
          "at": 2, "event_id": "evt_ok",
          "revision": {"commit": "8a4e024", "branch": "main", "dirty": false, "basis": "host_hook"},
          "revision_label": "ran at 8a4e024 · main"}]
        """
        let cleanProof = try XCTUnwrap(ReceiptStandingProof(receipt: try Self.receipt(checks: clean)))
        XCTAssertNil(cleanProof.dirtyRevisionLabel, "a clean tree must not be qualified as dirty")
        XCTAssertEqual(cleanProof.check.revisionText, "ran at 8a4e024 · main")
    }

    // MARK: - a disposition is a judgement, not a delete (C4)

    private static func summary(attentionOpen: Bool) throws -> ReceiptSummary {
        try JSONDecoder().decode(ReceiptSummary.self, from: Data("""
        { "task_id": "task-1", "title": "Add subtract()",
          "decision_status": {"key": "finding", "label": "Finding"},
          "evidence_strength": {"key": "unchecked"}, "cost": {},
          "attention": {
            "kind": "failed_check", "reason_label": "Failed check",
            "summary": "1 failed, 1 passed: format_amount('-1.5') returns '$-1.50', expected '-$1.50'",
            "label": "Failed test check · pytest tests/test_format.py · exit 1",
            "result_tone": "failure", "exit_code": 1,
            "disposition_state": "\(attentionOpen ? "open" : "reviewed")",
            "open": \(attentionOpen)
          }
        }
        """.utf8))
    }

    /// Marking a finding REVIEWED used to delete the only rendering of its
    /// failure summary from the row, while the coral Finding badge stayed — a
    /// judgement that erased the facts it was a judgement about. The facts stay;
    /// only the tone drops, because a reviewed item does not need you today.
    func testAReviewedFindingKeepsTheFactsAndOnlyLosesItsAlarmTone() throws {
        let open = WorkReceiptRowPresentation(task: try Self.summary(attentionOpen: true))
        let reviewed = WorkReceiptRowPresentation(task: try Self.summary(attentionOpen: false))

        let reason = try XCTUnwrap(reviewed.attentionReason)
        XCTAssertEqual(reason, try XCTUnwrap(open.attentionReason),
                       "a disposition changed the recorded facts, not just their tone")
        XCTAssertTrue(reason.contains("format_amount"))
        XCTAssertEqual(open.attentionReasonTint, Theme.coral)
        XCTAssertEqual(reviewed.attentionReasonTint, Theme.muted,
                       "a reviewed finding still shouts in the failure colour")
    }

    // MARK: - helpers

    private static func record(id: String, kind: WorkTimelineRecord.Kind, resultTone: String?,
                              superseded: Bool = false, supersededBy: String? = nil) -> WorkTimelineRecord {
        .init(id: id, eventID: "evt_\(id)", laneID: "session", laneTitle: "Session",
              lineage: "Recorded session", kind: kind, title: id, start: 1,
              result: "recorded", superseded: superseded, supersededBy: supersededBy,
              resultTone: resultTone)
    }
}
