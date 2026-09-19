import XCTest
@testable import agentacct

final class WorkTimelineFilterRevealTests: XCTestCase {
    func testFailuresOnlyWithNoInWindowMatchMovesTheInterval() throws {
        var failure = record("failure", time: 1_000)
        failure.result = "failed"
        failure.resultTone = "failure"
        let passing = record("passing", time: 5_000)
        let records = [failure, passing]
        let liveWindow = WorkTimelineInterval(lower: 4_000, upper: 5_100)
        let matches = records.filter(\.isCurrentFailure)
        XCTAssertEqual(matches.map(\.id), ["failure"])

        let moved = try XCTUnwrap(WorkTimelineFilterReveal.interval(for: matches, window: liveWindow))
        XCTAssertTrue(moved.contains(failure))
        XCTAssertNotEqual(moved, liveWindow)

        // A window that already shows a match stays put; so does no match.
        XCTAssertNil(WorkTimelineFilterReveal.interval(for: matches, window: moved))
        XCTAssertNil(WorkTimelineFilterReveal.interval(for: [], window: liveWindow))
    }

    func testRevealTargetsTheNewestMatch() throws {
        var older = record("older", time: 1_000)
        older.result = "failed"
        var newer = record("newer", time: 2_000)
        newer.result = "failed"
        let window = WorkTimelineInterval(lower: 9_000, upper: 9_500)
        let moved = try XCTUnwrap(WorkTimelineFilterReveal.interval(for: [older, newer], window: window))
        XCTAssertTrue(moved.contains(newer))
        XCTAssertFalse(moved.contains(older))
    }

    func testOutsideMatchesAreNamed() {
        var failure = record("failure", time: 1_000)
        failure.result = "failed"
        let window = WorkTimelineInterval(lower: 4_000, upper: 5_000)
        let outside = WorkTimelineFilterReveal.outside([failure], window: window)
        XCTAssertEqual(outside.before, 1)
        XCTAssertEqual(outside.after, 0)
        XCTAssertEqual(outside.newest?.id, "failure")
        XCTAssertEqual(WorkTimelineFilterReveal.cueText(outside, failuresOnly: true), "1 failed check before this window")
        let later = WorkTimelineFilterReveal.outside([record("a", time: 6_000), record("b", time: 7_000)], window: window)
        XCTAssertEqual(WorkTimelineFilterReveal.cueText(later, failuresOnly: false), "2 matching records after this window")
        XCTAssertNil(WorkTimelineFilterReveal.cueText(WorkTimelineFilterReveal.outside([record("in", time: 4_500)], window: window),
                                                     failuresOnly: false))
    }

    func testFailuresButtonCountsTheReceiptTally() {
        XCTAssertEqual(WorkTimelineFilterReveal.failuresButtonTitle(tallyFailed: 1, currentFailureRecords: 3), "1 failed check")
        XCTAssertEqual(WorkTimelineFilterReveal.failuresButtonTitle(tallyFailed: nil, currentFailureRecords: 2), "2 failed checks")
        XCTAssertEqual(WorkTimelineFilterReveal.failuresButtonTitle(tallyFailed: 0, currentFailureRecords: 1), "1 failed check")
    }

    func testPayloadAttentionPredicateWinsAndSupersessionIsNamed() throws {
        let raw: [String: Any] = ["schema_version": TaskTimelinePage.schema, "task_id": "task", "snapshot_id": "s",
            "events": [["id": "event:reviewed", "event_id": "reviewed", "kind": "check", "title": "pytest",
                        "name": "pytest", "status": "failed", "status_label": "Failed", "result_tone": "failure",
                        "occurred_at": 10, "is_current_failure": false,
                        "revision_label": "revision not captured", "supersedes_check_event_id": "earlier",
                        "command_state_text": "The agent's command argument was not stored; the title is the name the agent recorded."]],
            "offset": 0, "shown": 1, "total": 1, "truncated": false]
        let page = try JSONDecoder().decode(TaskTimelinePage.self, from: JSONSerialization.data(withJSONObject: raw))
        let record = try XCTUnwrap(page.projection(taskID: "task").records.first)
        XCTAssertFalse(record.isCurrentFailure, "A reviewed finding no longer counts as a current failure")
        XCTAssertEqual(record.displayTitle, "pytest")
        XCTAssertEqual(record.revisionLabel, "revision not captured")
        XCTAssertEqual(record.supersedesCheckEventID, "earlier")
        XCTAssertEqual(record.commandStateText, "The agent's command argument was not stored; the title is the name the agent recorded.")

        // The superseded state rides the payload's status words.
        var superseded = record
        superseded.superseded = true
        superseded.resultText = "Failed · superseded"
        XCTAssertEqual(superseded.resultLabel, "Failed · superseded")
        XCTAssertFalse(superseded.isDanger)
    }

    func testCheckThatCouldNotRunIsNeverACurrentFailure() throws {
        let raw: [String: Any] = ["schema_version": TaskTimelinePage.schema, "task_id": "task", "snapshot_id": "s",
            "events": [["id": "event:mypy", "event_id": "mypy", "kind": "check", "title": "mypy",
                        "status": "error", "status_label": "Could not run", "result_tone": "not_run",
                        "occurred_at": 10, "is_current_failure": false]],
            "offset": 0, "shown": 1, "total": 1, "truncated": false]
        let page = try JSONDecoder().decode(TaskTimelinePage.self, from: JSONSerialization.data(withJSONObject: raw))
        var record = try XCTUnwrap(page.projection(taskID: "task").records.first)
        XCTAssertEqual(record.resultLabel, "Could not run")
        XCTAssertEqual(record.checkTone, .notRun)
        XCTAssertFalse(record.isCurrentFailure)
        XCTAssertNotEqual(record.presentationTint, Theme.coral)
        // Without the payload predicate, the tone key still decides.
        record.attentionOpenFailure = nil
        XCTAssertFalse(record.isCurrentFailure)
    }

    private func record(_ id: String, time: Double) -> WorkTimelineRecord {
        WorkTimelineRecord(id: id, laneID: "session", laneTitle: "Session", lineage: "Root", kind: .check,
                           title: id, start: time, result: "passed")
    }
}
