import Foundation
import XCTest
@testable import agentacct

/// Group F's rendering contract, at the level a test can hold it: the payload
/// facts the new treatments read, and the rules that decide whether a fact is
/// drawn at all.
///
/// The treatments themselves (the beat's indented row, the salience rule, the
/// scroller's marks over its thumb) are pixels; these tests pin the DECISIONS
/// behind them, so a future change cannot quietly re-derive salience in Swift,
/// re-split the window rule, or lose a beat into the generic activity kind.
final class WorkTimelineBeatAndSalienceTests: XCTestCase {

    private func event(_ json: String) throws -> TaskTimelineEvent {
        try JSONDecoder().decode(TaskTimelineEvent.self, from: Data(json.utf8))
    }

    // MARK: - beats (F2)

    /// A progress note is its own kind. It used to fall through to `.activity`,
    /// the bucket a step lands in, so narration and work were indistinguishable
    /// to every surface that switches on kind.
    func testABeatIsItsOwnKindAndIsNeverAStepOrACheck() throws {
        let beat = try event(#"""
        {"id": "beat:evt_1", "event_id": "evt_1", "kind": "beat",
         "title": "subtract() landed in fc3b723 with a passing test.",
         "status": "checkpoint", "status_label": "Reported in progress",
         "occurred_at": 1789483490.0, "started_at": 1789483490.0,
         "lane": "primary", "lane_label": "Primary session",
         "summary": "subtract() landed in fc3b723 with a passing test. Started format_amount().",
         "section_record_id": "sec-1", "section_title": "Add subtract() money helper",
         "salience": null, "salience_reason": null, "salience_keys": [], "important": false}
        """#)
        let record = try XCTUnwrap(beat.record(taskID: "task_1"))
        XCTAssertEqual(record.kind, .beat)
        XCTAssertTrue(record.isBeat)
        XCTAssertNotEqual(record.kind, .step)
        XCTAssertNotEqual(record.kind, .check)
        XCTAssertNotEqual(record.kind, .activity)
        // A beat reports no outcome, so it can never carry a failure state.
        XCTAssertFalse(record.isCurrentFailure)
        XCTAssertFalse(record.isResolvedFailure)
        XCTAssertFalse(record.isDanger)
        // It belongs to the section that wrote it, and says which.
        XCTAssertEqual(record.sectionRecordID, "sec-1")
        XCTAssertEqual(record.sectionTitle, "Add subtract() money helper")
        // The reducer's status words are what the row prints — never Swift's.
        XCTAssertEqual(record.resultLabel, "Reported in progress")
    }

    /// The reducer promises a beat is never salient on its own: its section
    /// carries the salience, and a beat claiming it too would make one section
    /// shout twice. The renderer must not add one back.
    func testABeatCarriesNoSalienceOfItsOwn() throws {
        let beat = try event(#"""
        {"id": "b", "kind": "beat", "title": "note", "status": "checkpoint",
         "salience": null, "salience_keys": [], "important": false}
        """#)
        let record = try XCTUnwrap(beat.record(taskID: "t"))
        XCTAssertFalse(record.isSalient)
        XCTAssertNil(record.salienceReason)
    }

    /// The beat count and its definition ride the RECEIPT's timeline block. The
    /// paged task-timeline route carries neither, and a surface that has only
    /// the page must simply have no definition to show — never a Swift copy.
    func testTheBeatCountAndDefinitionAreReadFromThePayloadAndTolerateTheirAbsence() throws {
        let withBeats = try JSONDecoder().decode(TaskTimelinePage.self, from: Data(#"""
        {"schema_version": "agentacct.task-timeline.v1", "events": [], "shown": 0,
         "total": 0, "truncated": false, "beat_count": 4,
         "beat_definition": "A progress note the agent recorded while the section was still open."}
        """#.utf8))
        XCTAssertEqual(withBeats.beatCount, 4)
        XCTAssertEqual(withBeats.beatDefinition,
                       "A progress note the agent recorded while the section was still open.")

        let paged = try JSONDecoder().decode(TaskTimelinePage.self, from: Data(#"""
        {"schema_version": "agentacct.task-timeline.v1", "events": [], "shown": 0,
         "total": 0, "truncated": false}
        """#.utf8))
        XCTAssertNil(paged.beatCount)
        XCTAssertNil(paged.beatDefinition)
    }

    // MARK: - salience (F3)

    /// Salience is a PAYLOAD fact. It depends on the whole Task — the largest
    /// recorded file set, a run that supersedes an earlier failure, a step
    /// reported complete while still unchecked — so one record cannot re-derive
    /// it and no surface may try.
    func testSalienceIsReadFromThePayloadRatherThanDerivedFromTheRecord() throws {
        let loud = try event(#"""
        {"id": "w1", "kind": "work", "title": "release-build failure", "status": "completed",
         "salience": "owns_failed_check",
         "salience_reason": "A check recorded against this section did not pass.",
         "salience_keys": ["owns_failed_check", "completed_unchecked"], "important": true}
        """#)
        let record = try XCTUnwrap(loud.record(taskID: "t"))
        XCTAssertTrue(record.isSalient)
        XCTAssertEqual(record.salience, "owns_failed_check")
        XCTAssertEqual(record.salienceReason, "A check recorded against this section did not pass.")
        XCTAssertEqual(record.salienceKeys, ["owns_failed_check", "completed_unchecked"])
        // Nothing about this record's own fields says "failed": the reason it
        // is loud lives in a check the reducer looked at, not here.
        XCTAssertFalse(record.isCurrentFailure)
        XCTAssertFalse(record.isDanger)
    }

    /// A routine passing check is QUIET now. Before Group E every check set
    /// `important`, so 29% of rows shouted and the mark meant nothing.
    func testAQuietRecordIsNotMarked() throws {
        let quiet = try event(#"""
        {"id": "c1", "kind": "check", "title": "pytest tests/test_subtract.py", "status": "passed",
         "result_tone": "pass", "status_label": "Passed",
         "salience": null, "salience_reason": null, "salience_keys": [], "important": false}
        """#)
        let record = try XCTUnwrap(quiet.record(taskID: "t"))
        XCTAssertFalse(record.isSalient)
        XCTAssertNil(record.salienceReason)
    }

    /// `important` is the reducer's own restatement of "salience is not null".
    /// When the payload sends it, it wins; a payload that predates the field
    /// falls back to the key rather than to a second Swift rule.
    func testThePayloadsOwnImportantFlagWinsAndAnOlderPayloadFallsBackToTheKey() throws {
        let contradicting = try event(#"""
        {"id": "x", "kind": "work", "title": "t", "status": "completed",
         "salience": "left_in_progress", "important": false}
        """#)
        XCTAssertFalse(try XCTUnwrap(contradicting.record(taskID: "t")).isSalient,
                       "the payload decides; Swift never overrules it")

        let older = try event(#"""
        {"id": "y", "kind": "work", "title": "t", "status": "completed",
         "salience": "left_in_progress"}
        """#)
        XCTAssertTrue(try XCTUnwrap(older.record(taskID: "t")).isSalient)
    }

    /// A payload from before Group E carries none of these keys at all and must
    /// still project — every stored row keeps its meaning (forward-only).
    func testAPayloadFromBeforeSalienceStillProjects() throws {
        let old = try event(#"""
        {"id": "w", "event_id": "e", "kind": "work", "title": "Fix the bug",
         "status": "completed", "started_at": 100}
        """#)
        let record = try XCTUnwrap(old.record(taskID: "t"))
        XCTAssertEqual(record.kind, .step)
        XCTAssertFalse(record.isSalient)
        XCTAssertNil(record.salience)
        XCTAssertEqual(record.salienceKeys, [])
    }

    // MARK: - one window rule (F5)

    /// The Activity heading's count, the ordered list and the canvas's own
    /// tally ask ONE question: is this record's card drawn in this window?
    ///
    /// They used to disagree — the heading counted span overlap, the canvas
    /// drew cards at record starts — so task_c5bffb80's header could read
    /// "2 of 16 loaded records" over a canvas holding a different number of
    /// cards. Whatever the rule is, it must be the same rule.
    func testTheHeadingTheListAndTheCanvasShareOneWindowRule() {
        let window = WorkTimelineInterval(lower: 5_000, upper: 5_100)
        func record(_ id: String, start: Double?, end: Double? = nil) -> WorkTimelineRecord {
            WorkTimelineRecord(id: id, laneID: "s", laneTitle: "S", lineage: "root",
                               kind: .step, title: id, start: start, end: end)
        }
        let inside = record("inside", start: 5_050)
        let crossing = record("crossing", start: 1_000, end: 5_200)
        let outside = record("outside", start: 9_000)
        let undated = record("undated", start: nil)
        let records = [inside, crossing, outside, undated]

        // The heading/list rule.
        XCTAssertEqual(records.filter(window.contains).map(\.id), ["inside", "undated"])
        // The canvas's navigation rule.
        XCTAssertTrue(WorkTimelineRangeNavigation.holdsACard(window, records: records))
        XCTAssertFalse(WorkTimelineRangeNavigation.holdsACard(window, records: [crossing]))
        // The canvas's own tally, computed by the layout.
        let layout = WorkTimeCanvasLayout(records: records, window: window, width: 1_000, height: 420)
        XCTAssertEqual(layout.visibleRecordCount, 1)
        XCTAssertEqual(layout.visibleRecordCount,
                       records.filter { $0.start != nil && window.contains($0) }.count)
        // A crossing span is still accounted for — as a span line, not as a
        // record in view.
        XCTAssertEqual(layout.crossingSpans(in: 1_000).flatMap(\.recordIDs), ["crossing"])
    }

    // MARK: - the heading is printed once (F1)

    func testAStringThatOnlyRestatesTheHeadingIsRecognised() {
        XCTAssertTrue(restatesPayloadText("Fix percentage() off-by-rounding bug",
                                          "Fix percentage() off-by-rounding bug"))
        XCTAssertTrue(restatesPayloadText("  fix percentage() off-by-rounding bug.  ",
                                          "Fix percentage() off-by-rounding bug"))
        XCTAssertFalse(restatesPayloadText("release-build failure: closed as not reproducible",
                                           "agentacct timeline navigation redesign handoff"))
        XCTAssertFalse(restatesPayloadText("anything", nil))
        XCTAssertFalse(restatesPayloadText("   ", "   "),
                       "an empty string restates nothing; it is simply absent")
    }

    // MARK: - one next-step renderer per surface (F6)

    /// The record page shows the recorded next step as a permanent row under
    /// the outcome summary, so it survives a finding being marked reviewed and
    /// does not depend on an attention item existing at all. The attention
    /// callout on that page therefore must NOT print it a second time — and the
    /// positive twin (`EvidenceCallout`, which passes nil) must not print a
    /// fabricated "no next step recorded" over a payload that has one.
    func testTheRecordPagesAttentionBlockNoLongerOwnsTheNextStep() {
        XCTAssertFalse(AttentionBlockBody.Variant.record.showsNextStep)
        // The Dashboard card has no hero row beneath it, so it stays the one
        // renderer on that surface — inline, to hold the first viewport.
        XCTAssertTrue(AttentionBlockBody.Variant.dashboard.showsNextStep)
        XCTAssertTrue(AttentionBlockBody.Variant.dashboard.nextStepIsCompact)
    }
}
