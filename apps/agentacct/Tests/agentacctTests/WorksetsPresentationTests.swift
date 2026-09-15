import XCTest
@testable import agentacct

final class WorksetsPresentationTests: XCTestCase {

    private func lane(_ json: String) -> WorksetLane {
        try! JSONDecoder().decode(WorksetLane.self, from: Data(json.utf8))
    }

    private func summary(_ json: String) -> WorksetSummary {
        try! JSONDecoder().decode(WorksetSummary.self, from: Data(json.utf8))
    }

    // MARK: tab structure

    func testTabOrderAndLabels() {
        // Dashboard | Work (worksets) | Sessions (receipts) | Usage | Sources.
        XCTAssertEqual(MainPane.allCases.map(\.rawValue),
                       ["Dashboard", "Work", "Sessions", "Usage", "Sources"])
        // The new feature's tab is "Work"; the receipts pane kept its `.work`
        // case but now reads as "Sessions".
        XCTAssertEqual(MainPane.worksets.rawValue, "Work")
        XCTAssertEqual(MainPane.work.rawValue, "Sessions")
    }

    // MARK: timeline geometry

    func testBarsSpanTheSharedAxisInTimeOrder() {
        let lanes = [
            lane(#"{"session_key":"codex::b","client":"codex","first_activity_at":300,"last_activity_at":400}"#),
            lane(#"{"session_key":"claude-code::a","client":"claude-code","first_activity_at":100,"last_activity_at":200}"#),
        ]
        let layout = WorksetTimelineLayout(lanes: lanes)
        XCTAssertEqual(layout.windowStart, 100)
        XCTAssertEqual(layout.windowEnd, 400)
        // Sorted by start time: the Claude Code session (100) comes first.
        XCTAssertEqual(layout.bars.map(\.lane.id), ["claude-code::a", "codex::b"])
        // Span is 300s. a: left 0, width 100/300; b: left 200/300, width 100/300.
        XCTAssertEqual(layout.bars[0].leftFraction, 0, accuracy: 0.0001)
        XCTAssertEqual(layout.bars[0].widthFraction, 100.0 / 300.0, accuracy: 0.0001)
        XCTAssertEqual(layout.bars[1].leftFraction, 200.0 / 300.0, accuracy: 0.0001)
        XCTAssertEqual(layout.bars[1].widthFraction, 100.0 / 300.0, accuracy: 0.0001)
        // A bar never overflows the axis.
        for bar in layout.bars {
            XCTAssertLessThanOrEqual(bar.leftFraction + bar.widthFraction, 1.0 + 0.0001)
        }
    }

    func testAxisUsesTrueSpanOverrideSoTruncatedPreviewStaysHonest() {
        // Two visible bars early in a much wider true window (the later sessions
        // are truncated away). The axis + bar positions must reflect the TRUE
        // window, so the bars sit on the left and the rest of the axis is empty.
        let lanes = [
            lane(#"{"session_key":"a","client":"claude-code","first_activity_at":100,"last_activity_at":200}"#),
            lane(#"{"session_key":"b","client":"codex","first_activity_at":200,"last_activity_at":300}"#),
        ]
        let layout = WorksetTimelineLayout(lanes: lanes, windowStart: 100, windowEnd: 1000)
        XCTAssertEqual(layout.windowStart, 100)
        XCTAssertEqual(layout.windowEnd, 1000)
        // Span 900: bar a left 0, width 100/900 (~0.11); bar b left 100/900.
        XCTAssertEqual(layout.bars[0].leftFraction, 0, accuracy: 0.0001)
        XCTAssertEqual(layout.bars[0].widthFraction, 100.0 / 900.0, accuracy: 0.0001)
        XCTAssertEqual(layout.bars[1].leftFraction, 100.0 / 900.0, accuracy: 0.0001)
        // Neither bar reaches the right edge — the empty right honestly shows
        // there is more time than the shown sessions cover.
        XCTAssertLessThan(layout.bars[1].leftFraction + layout.bars[1].widthFraction, 0.5)
    }

    func testZeroDurationSessionStillGetsAMinimumBar() {
        let lanes = [
            lane(#"{"session_key":"a","client":"claude-code","first_activity_at":100,"last_activity_at":300}"#),
            lane(#"{"session_key":"b","client":"codex","first_activity_at":300,"last_activity_at":300}"#),
        ]
        let layout = WorksetTimelineLayout(lanes: lanes)
        let point = layout.bars.first { $0.lane.id == "b" }!
        // Parked at the far end but pulled in just enough to stay visible.
        XCTAssertGreaterThanOrEqual(point.widthFraction, WorksetTimelineLayout.minWidth)
        XCTAssertGreaterThan(point.leftFraction, 0.95)
        XCTAssertLessThanOrEqual(point.leftFraction + point.widthFraction, 1.0 + 0.0001)
    }

    func testTimelessSessionIsFlaggedNotFabricated() {
        let lanes = [
            lane(#"{"session_key":"timed","client":"claude-code","first_activity_at":100,"last_activity_at":200}"#),
            lane(#"{"session_key":"timeless","client":"codex"}"#),  // no times at all
        ]
        let layout = WorksetTimelineLayout(lanes: lanes)
        XCTAssertEqual(layout.timelessCount, 1)
        let timeless = layout.bars.first { $0.lane.id == "timeless" }!
        XCTAssertTrue(timeless.timeUnknown)
        XCTAssertEqual(timeless.leftFraction, 0)  // parked at the start, not invented
        // Timeless sessions sort last.
        XCTAssertEqual(layout.bars.last?.lane.id, "timeless")
    }

    func testAllSameInstantDoesNotDivideByZero() {
        let lanes = [
            lane(#"{"session_key":"a","client":"claude-code","first_activity_at":500,"last_activity_at":500}"#),
            lane(#"{"session_key":"b","client":"codex","first_activity_at":500,"last_activity_at":500}"#),
        ]
        let layout = WorksetTimelineLayout(lanes: lanes)
        for bar in layout.bars {
            XCTAssertEqual(bar.leftFraction, 0)
            XCTAssertEqual(bar.widthFraction, WorksetTimelineLayout.minWidth, accuracy: 0.0001)
            XCTAssertFalse(bar.timeUnknown)  // it HAS a time; the span is just zero
        }
    }

    // MARK: honest cost grammar

    func testCostLabelIsBareOrApproxWhenComplete() {
        let s = summary(#"{"session_count":2,"sources":[],"estimated_cost_usd":3.4,"cost_complete":true,"cost_confidence":"estimated_from_tokens"}"#)
        // A complete estimate wears the ≈ prefix (v10 cost grammar), never a bare $.
        XCTAssertEqual(worksetCostLabel(s), "≈$3.40")
    }

    func testCostLabelIsPartialSumWhenAnyMemberUnpriced() {
        let s = summary(#"{"session_count":3,"sources":[],"estimated_cost_usd":2.4,"cost_complete":false,"priced_sessions":2,"unpriced_sessions":1,"cost_confidence":"estimated_from_tokens"}"#)
        // A known-partial subtotal wears ~$, so it never reads as a full total.
        XCTAssertEqual(worksetCostLabel(s), "~$2.40")
    }

    func testCostLabelIsNilWhenNothingPriced() {
        let s = summary(#"{"session_count":2,"sources":[],"cost_complete":false,"priced_sessions":0,"unpriced_sessions":2}"#)
        // Nothing priced → no number, never a fabricated $0.
        XCTAssertNil(worksetCostLabel(s))
    }

    // MARK: format helpers

    func testSpanIsHonestAndAbsentWhenEndpointsMissing() {
        XCTAssertEqual(WorksetFormat.span(from: 0, to: 3 * 86_400), "~3 days")
        XCTAssertEqual(WorksetFormat.span(from: 0, to: 5 * 3_600), "~5 hrs")
        XCTAssertEqual(WorksetFormat.span(from: 0, to: 20 * 60), "~20 min")
        XCTAssertEqual(WorksetFormat.span(from: nil, to: 100), "—")
        XCTAssertEqual(WorksetFormat.span(from: 100, to: nil), "—")
    }

    func testSourceLabelMapsEveryAgentAndPassesUnknownThrough() {
        XCTAssertEqual(WorksetFormat.sourceLabel("claude-code"), "Claude Code")
        XCTAssertEqual(WorksetFormat.sourceLabel("codex"), "Codex")
        XCTAssertEqual(WorksetFormat.sourceLabel("opencode"), "OpenCode")
        XCTAssertEqual(WorksetFormat.sourceLabel("hermes"), "Hermes")
        XCTAssertEqual(WorksetFormat.sourceLabel("some-new-agent"), "some-new-agent")  // unknown passes through
    }

    func testDurationIsHumanAndAbsentWhenMissing() {
        XCTAssertNil(WorksetFormat.duration(nil))
        XCTAssertNil(WorksetFormat.duration(0))
        XCTAssertEqual(WorksetFormat.duration(45), "45s")
        XCTAssertEqual(WorksetFormat.duration(120), "2m")
        XCTAssertEqual(WorksetFormat.duration(3 * 3600), "3.0h")
        XCTAssertEqual(WorksetFormat.duration(12 * 3600), "12h")
    }
}
