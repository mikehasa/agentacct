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
        // Dashboard | Work (worksets) | Sessions (receipts) | Usage | Diagnostics.
        XCTAssertEqual(MainPane.allCases.map(\.rawValue),
                       ["Dashboard", "Work", "Sessions", "Usage", "Diagnostics"])
        // The new feature's tab is "Work"; the receipts pane kept its `.work`
        // case but now reads as "Sessions".
        XCTAssertEqual(MainPane.worksets.rawValue, "Work")
        XCTAssertEqual(MainPane.work.rawValue, "Sessions")
        // The source/watcher-health pane is named for what it is: Diagnostics.
        XCTAssertEqual(MainPane.sources.rawValue, "Diagnostics")
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

    // MARK: bin-packed Gantt (Bug 2: every session visible without a row cap)

    func testPackingCollapsesNonOverlappingSessionsIntoOneRow() {
        // Three sessions spread across the range never overlap → one shared row,
        // so the whole history is visible at once instead of three tall rows.
        let lanes = [
            lane(#"{"session_key":"a","client":"claude-code","first_activity_at":100,"last_activity_at":200}"#),
            lane(#"{"session_key":"b","client":"codex","first_activity_at":400,"last_activity_at":500}"#),
            lane(#"{"session_key":"c","client":"claude-code","first_activity_at":800,"last_activity_at":900}"#),
        ]
        let packed = WorksetTimelineLayout(lanes: lanes).packedRows()
        XCTAssertEqual(packed.rows.count, 1)
        XCTAssertEqual(packed.rows[0].map(\.lane.id), ["a", "b", "c"])
        XCTAssertTrue(packed.timeless.isEmpty)
    }

    func testPackingOpensASecondRowOnlyForOverlap() {
        // Two sessions that overlap in time cannot share a row.
        let lanes = [
            lane(#"{"session_key":"a","client":"claude-code","first_activity_at":100,"last_activity_at":900}"#),
            lane(#"{"session_key":"b","client":"codex","first_activity_at":300,"last_activity_at":1000}"#),
        ]
        let packed = WorksetTimelineLayout(lanes: lanes).packedRows()
        XCTAssertEqual(packed.rows.count, 2)
        XCTAssertEqual(packed.rows[0].map(\.lane.id), ["a"])
        XCTAssertEqual(packed.rows[1].map(\.lane.id), ["b"])
    }

    func testPackingKeepsTimelessSessionsSeparate() {
        let lanes = [
            lane(#"{"session_key":"timed","client":"claude-code","first_activity_at":100,"last_activity_at":200}"#),
            lane(#"{"session_key":"noclock","client":"codex"}"#),
        ]
        let packed = WorksetTimelineLayout(lanes: lanes).packedRows()
        XCTAssertEqual(packed.rows.flatMap { $0 }.map(\.lane.id), ["timed"])
        XCTAssertEqual(packed.timeless.map(\.lane.id), ["noclock"])
    }

    func testPackingNeverDropsASessionEvenPastTheLaneCap() {
        // Pathological: many overlapping sessions with a tiny lane cap. Packing
        // must keep every session on screen (overflow stacks onto the last row),
        // never silently hide one the way the old newest-16 cap did.
        let lanes = (0..<10).map { i in
            lane(#"{"session_key":"s\#(i)","client":"claude-code","first_activity_at":100,"last_activity_at":900}"#)
        }
        let packed = WorksetTimelineLayout(lanes: lanes).packedRows(maxRows: 3)
        XCTAssertLessThanOrEqual(packed.rows.count, 3)
        XCTAssertEqual(packed.rows.flatMap { $0 }.count, 10)  // all ten still present
    }

    // MARK: shared (multi-folder) sessions (Bug 1)

    func testLaneDisclosesTheOtherFoldersItAlsoRanIn() {
        let l = lane(#"{"session_key":"x","client":"claude-code","other_folders":["tofuai","api","zeta"]}"#)
        XCTAssertTrue(l.alsoRanElsewhere)
        // At most two names, then a "+N" so a long list stays a compact chip.
        XCTAssertEqual(l.sharedFoldersLabel, "tofuai · api +1")
    }

    func testSingleFolderLaneIsNotFlaggedAsShared() {
        let solo = lane(#"{"session_key":"y","client":"codex"}"#)
        XCTAssertFalse(solo.alsoRanElsewhere)
        XCTAssertNil(solo.sharedFoldersLabel)
        let emptyList = lane(#"{"session_key":"z","client":"codex","other_folders":[]}"#)
        XCTAssertFalse(emptyList.alsoRanElsewhere)
        XCTAssertNil(emptyList.sharedFoldersLabel)
    }

    func testSummaryCarriesTheSharedSessionCountForDisclosure() {
        let s = summary(#"{"session_count":9,"sources":[],"shared_sessions":1}"#)
        XCTAssertEqual(s.sharedSessions, 1)
        let none = summary(#"{"session_count":3,"sources":[]}"#)
        XCTAssertNil(none.sharedSessions)
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

    func testZoomWindowFullAtZoomOneAndNarrowsCentered() {
        let full = WorksetZoomWindow(lo: 0, hi: 1000, zoom: 1, panCenter: 0.5)
        XCTAssertEqual(full.start, 0, accuracy: 0.001)
        XCTAssertEqual(full.end, 1000, accuracy: 0.001)
        XCTAssertEqual(full.coverage(lo: 0, hi: 1000), 1, accuracy: 0.001)

        // Zoom 2 centered → half the range around the middle: [250, 750].
        let mid = WorksetZoomWindow(lo: 0, hi: 1000, zoom: 2, panCenter: 0.5)
        XCTAssertEqual(mid.start, 250, accuracy: 0.001)
        XCTAssertEqual(mid.end, 750, accuracy: 0.001)
        XCTAssertEqual(mid.coverage(lo: 0, hi: 1000), 0.5, accuracy: 0.001)
    }

    func testZoomWindowPanClampsInsideTheData() {
        // Pan hard left at zoom 4 → window sits at the start, never before lo.
        let left = WorksetZoomWindow(lo: 0, hi: 1000, zoom: 4, panCenter: 0)
        XCTAssertEqual(left.start, 0, accuracy: 0.001)
        XCTAssertEqual(left.end, 250, accuracy: 0.001)
        // Pan hard right → window sits at the end, never past hi.
        let right = WorksetZoomWindow(lo: 0, hi: 1000, zoom: 4, panCenter: 1)
        XCTAssertEqual(right.start, 750, accuracy: 0.001)
        XCTAssertEqual(right.end, 1000, accuracy: 0.001)
    }

    func testLaneOverlapCullsSessionsOutsideTheWindow() {
        // Window [750, 1000] (a right-panned zoom of a [0,1000] range).
        XCTAssertFalse(WorksetZoomWindow.laneOverlaps(first: 0, last: 100, start: 750, end: 1000))   // entirely before
        XCTAssertFalse(WorksetZoomWindow.laneOverlaps(first: 1200, last: 1300, start: 750, end: 1000)) // entirely after
        XCTAssertTrue(WorksetZoomWindow.laneOverlaps(first: 900, last: 1000, start: 750, end: 1000))  // inside
        XCTAssertTrue(WorksetZoomWindow.laneOverlaps(first: 700, last: 800, start: 750, end: 1000))   // straddles start
        XCTAssertTrue(WorksetZoomWindow.laneOverlaps(first: 800, last: nil, start: 750, end: 1000))   // point inside
        XCTAssertFalse(WorksetZoomWindow.laneOverlaps(first: nil, last: 900, start: 750, end: 1000))  // timeless culled
    }

    func testZoomWindowIsRobustToBadInput() {
        // Degenerate range and non-finite inputs never crash or invert.
        let degenerate = WorksetZoomWindow(lo: 500, hi: 500, zoom: 8, panCenter: 0.5)
        XCTAssertEqual(degenerate.start, 500, accuracy: 0.001)
        XCTAssertEqual(degenerate.end, 500, accuracy: 0.001)
        let nan = WorksetZoomWindow(lo: 0, hi: 100, zoom: .nan, panCenter: .nan)
        XCTAssertLessThanOrEqual(nan.start, nan.end)
        XCTAssertGreaterThanOrEqual(nan.start, 0)
        XCTAssertLessThanOrEqual(nan.end, 100)
    }

    func testTimelessLaneStaysVisibleWhenZoomedSoItsHonestyNoteIsKept() {
        // A timed lane outside the window is culled…
        XCTAssertFalse(WorksetZoomWindow.laneVisible(first: 100, last: 200, start: 750, end: 1000))
        XCTAssertTrue(WorksetZoomWindow.laneVisible(first: 900, last: 1000, start: 750, end: 1000))
        // …but a timeless lane has no usable time (nil or ≤0), so it is NEVER
        // culled: it must stay in view (faded at the start) and keep its "no
        // recorded time" flag rather than being recounted as "outside this range".
        XCTAssertTrue(WorksetZoomWindow.laneVisible(first: nil, last: 900, start: 750, end: 1000))
        XCTAssertTrue(WorksetZoomWindow.laneVisible(first: 0, last: nil, start: 750, end: 1000))
    }

    func testScrollZoomKeepsTheAnchoredTimeUnderThePointer() {
        // Zoom in 2× anchored at the right edge (anchor = 1): the time that was
        // at the right edge of the full range must still sit at the right edge.
        let r = WorksetZoomWindow.applyZoom(currentZoom: 1, panCenter: 0.5,
                                            factor: 2, anchor: 1, lo: 0, hi: 1000)
        XCTAssertEqual(r.zoom, 2, accuracy: 0.001)
        let win = WorksetZoomWindow(lo: 0, hi: 1000, zoom: r.zoom, panCenter: r.panCenter)
        XCTAssertEqual(win.end, 1000, accuracy: 0.001)   // 1000 stayed under the anchor
        XCTAssertEqual(win.start, 500, accuracy: 0.001)

        // Anchored at the left edge (anchor = 0): time 0 stays pinned left.
        let l = WorksetZoomWindow.applyZoom(currentZoom: 1, panCenter: 0.5,
                                            factor: 2, anchor: 0, lo: 0, hi: 1000)
        let lwin = WorksetZoomWindow(lo: 0, hi: 1000, zoom: l.zoom, panCenter: l.panCenter)
        XCTAssertEqual(lwin.start, 0, accuracy: 0.001)
        XCTAssertEqual(lwin.end, 500, accuracy: 0.001)
    }

    func testScrollZoomClampsAndSurvivesBadInput() {
        // Zoom never exceeds the cap however many notches scroll in.
        let capped = WorksetZoomWindow.applyZoom(currentZoom: 60, panCenter: 0.5,
                                                 factor: 4, anchor: 0.5, lo: 0, hi: 1000)
        XCTAssertLessThanOrEqual(capped.zoom, WorksetZoomWindow.maxZoom)
        // Zooming all the way back out lands at 1× (full range).
        let out = WorksetZoomWindow.applyZoom(currentZoom: 1, panCenter: 0.5,
                                              factor: 0.1, anchor: 0.5, lo: 0, hi: 1000)
        XCTAssertEqual(out.zoom, 1, accuracy: 0.001)
        // Non-finite factor / degenerate range never crash or invert.
        let nan = WorksetZoomWindow.applyZoom(currentZoom: 2, panCenter: 0.5,
                                              factor: .nan, anchor: .nan, lo: 0, hi: 1000)
        XCTAssertGreaterThanOrEqual(nan.zoom, 1)
        XCTAssertTrue(nan.panCenter.isFinite)
        let flat = WorksetZoomWindow.applyZoom(currentZoom: 1, panCenter: 0.5,
                                               factor: 2, anchor: 0.5, lo: 500, hi: 500)
        XCTAssertGreaterThanOrEqual(flat.zoom, 1)
        XCTAssertTrue(flat.panCenter.isFinite)
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

    func testAxisLabelCarriesTheClockOnlyForWorkdaySpans() {
        let epoch = 1_789_552_000.0
        let workday = WorksetFormat.axisLabel(epoch, span: 9 * 3600)
        let week = WorksetFormat.axisLabel(epoch, span: 6 * 86_400)
        XCTAssertTrue(workday.contains(":"), "a sub-two-day window reads with a clock: \(workday)")
        XCTAssertFalse(week.contains(":"), "a multi-day window keeps the date-only label: \(week)")
        XCTAssertEqual(week, WorksetFormat.axisDate(epoch))
        XCTAssertEqual(WorksetFormat.axisLabel(epoch, span: .nan), WorksetFormat.axisDate(epoch))
    }

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
