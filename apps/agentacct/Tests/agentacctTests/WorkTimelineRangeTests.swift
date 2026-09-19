import XCTest
@testable import agentacct

final class WorkTimelineRangeTests: XCTestCase {
    func testFocusUsesRecordedSpanAndContextAndDoesNotInventUndatedTime() {
        var record = WorkTimelineRecord(id: "r", laneID: "session", laneTitle: "Session", lineage: "root", kind: .check, title: "Check", start: 500)
        XCTAssertEqual(WorkTimelineRangeNavigation.focused(on: record), .init(lower: 440, upper: 560))
        record.kind = .step; record.end = 1500
        XCTAssertEqual(WorkTimelineRangeNavigation.focused(on: record), .init(lower: 350, upper: 1650))
        record.start = nil; record.end = nil
        XCTAssertNil(WorkTimelineRangeNavigation.focused(on: record))
    }

    func testRangeSelectionSupportsReverseDragsAndClampsBothEdges() {
        let full = WorkTimelineInterval(lower: 100, upper: 1000)
        XCTAssertEqual(WorkTimelineRangeNavigation.selected(from: 800, to: 200, within: full), .init(lower: 200, upper: 800))
        XCTAssertEqual(WorkTimelineRangeNavigation.selected(from: -100, to: 2000, within: full), full)
        let edge = WorkTimelineRangeNavigation.selected(from: 1000, to: 1200, within: full)
        XCTAssertEqual(edge, .init(lower: 999, upper: 1000))
    }

    func testAxisNamesDatesAcrossMidnightEvenForAShortRange() {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        XCTAssertTrue(WorkTimelineTimeAxis.showsDates(in: .init(lower: 86_390, upper: 86_410), calendar: calendar))
        XCTAssertFalse(WorkTimelineTimeAxis.showsDates(in: .init(lower: 86_410, upper: 86_500), calendar: calendar))
        XCTAssertTrue(WorkTimelineTimeAxis.showsDates(in: .init(lower: 100, upper: 259_200), calendar: calendar))
    }

    func testPanningPreservesRangeDurationAtEitherBoundary() {
        let full = WorkTimelineInterval(lower: 100, upper: 1000)
        let window = WorkTimelineInterval(lower: 300, upper: 400)
        XCTAssertEqual(WorkTimelineRangeNavigation.shifted(window, by: -999, within: full), .init(lower: 100, upper: 200))
        XCTAssertEqual(WorkTimelineRangeNavigation.shifted(window, by: 999, within: full), .init(lower: 900, upper: 1000))
        XCTAssertEqual(WorkTimelineRangeNavigation.shifted(window, by: 20, within: full), .init(lower: 320, upper: 420))
    }
    func testOverviewHitTestingRejectsGapsAndUnknownTimesAndHitsFullSpans() {
        let full = WorkTimelineInterval(lower: 0, upper: 1000)
        var span = WorkTimelineRecord(id: "span", laneID: "s", laneTitle: "S", lineage: "root", kind: .step, title: "Span", start: 100)
        span.end = 300
        let point = WorkTimelineRecord(id: "point", laneID: "s", laneTitle: "S", lineage: "root", kind: .check, title: "Check", start: 200)
        let unknown = WorkTimelineRecord(id: "unknown", laneID: "s", laneTitle: "S", lineage: "root", kind: .check, title: "Unknown", start: nil)
        let records = [span, point, unknown]
        XCTAssertNil(WorkTimelineRangeNavigation.hitRecord(records, laneID: "s", x: 700, width: 1000, within: full))
        XCTAssertNil(WorkTimelineRangeNavigation.hitRecord(records, laneID: "s", x: -1, width: 1000, within: full))
        XCTAssertNil(WorkTimelineRangeNavigation.hitRecord(records, laneID: "other", x: 200, width: 1000, within: full))
        XCTAssertEqual(WorkTimelineRangeNavigation.hitRecord(records, laneID: "s", x: 250, width: 1000, within: full)?.id, "span")
        XCTAssertEqual(WorkTimelineRangeNavigation.hitRecord(records, laneID: "s", x: 205, width: 1000, within: full)?.id, "point")
        XCTAssertEqual(WorkTimelineRangeNavigation.hitRecord(records, laneID: "s", x: 306, width: 1000, within: full)?.id, "span")
        XCTAssertNil(WorkTimelineRangeNavigation.hitRecord(records, laneID: "s", x: 307, width: 1000, within: full))
    }

    /// A long section's SPAN crossing the window is not a card on screen. The
    /// canvas draws a card at its record's start, so a window that holds only
    /// the tail of a 70-minute section renders the named-empty state — the
    /// exact first paint task_c5bffb80 opened with. An app-chosen window must
    /// therefore be repaired to one that actually draws something.
    func testAWindowHoldingOnlyALongSectionsTailIsNotAPopulatedCanvas() {
        let full = WorkTimelineInterval(lower: 0, upper: 10_000)
        let section = WorkTimelineRecord(id: "section", laneID: "s", laneTitle: "S", lineage: "root",
                                         kind: .step, title: "Long section", start: 1_000, end: 5_200)
        let tail = WorkTimelineInterval(lower: 5_000, upper: 5_100)

        // ONE rule now answers both questions (F5). The heading's count asks
        // `contains`; the canvas asks `holdsACard`; they used to disagree here
        // — span overlap said "in the window", card position said "nothing is
        // drawn" — so the header could read "1 of 16" over an empty canvas.
        XCTAssertFalse(tail.contains(section),
                       "the count rule and the drawing rule are the same rule")
        XCTAssertFalse(WorkTimelineRangeNavigation.holdsACard(tail, records: [section]),
                       "no card is drawn there: the card sits at the section's start")

        let repaired = WorkTimelineRangeNavigation.populated(tail, records: [section], newest: section, within: full)
        XCTAssertNotEqual(repaired, tail, "an app-chosen empty window is repaired")
        XCTAssertTrue(WorkTimelineRangeNavigation.holdsACard(repaired, records: [section]),
                      "the repaired window draws the card")
    }

    /// The repair is a threshold, not a blanket move: a window that already
    /// draws a card is left exactly as it is, and a task with no dated record
    /// has nothing to repair toward.
    func testAWindowThatAlreadyDrawsACardIsLeftAlone() {
        let full = WorkTimelineInterval(lower: 0, upper: 10_000)
        let point = WorkTimelineRecord(id: "point", laneID: "s", laneTitle: "S", lineage: "root",
                                       kind: .check, title: "Check", start: 5_050)
        let window = WorkTimelineInterval(lower: 5_000, upper: 5_100)
        XCTAssertEqual(WorkTimelineRangeNavigation.populated(window, records: [point], newest: point, within: full),
                       window)

        let undated = WorkTimelineRecord(id: "undated", laneID: "s", laneTitle: "S", lineage: "root",
                                         kind: .check, title: "Undated", start: nil)
        XCTAssertEqual(WorkTimelineRangeNavigation.populated(window, records: [undated], newest: undated, within: full),
                       window, "an undated task keeps the window rather than jumping somewhere arbitrary")
    }
}
