import AppKit
import XCTest
@testable import agentacct

final class WorkTimelineFocusTests: XCTestCase {
    // MARK: pressing a check must not throw the document (F9)

    /// A row in the Checks table asks the activity surface for one recorded
    /// event. The surface re-frames its own window to show that record's card —
    /// and declines to re-frame at all when the card is already drawn, so a
    /// reviewer who can see the mark keeps their zoom. Neither branch moves the
    /// document: that displacement is what threw the reader past their own
    /// expanded row.
    func testSelectingACheckReframesTheSurfaceOnlyWhenTheCardIsNotAlreadyDrawn() {
        let window = WorkTimelineInterval(lower: 1_000, upper: 2_000)
        let inside = WorkTimelineRecord(id: "in", laneID: "s", laneTitle: "S", lineage: "root",
                                        kind: .check, title: "Tests", start: 1_500)
        XCTAssertNil(WorkTimelineRangeNavigation.reframed(window, toShow: inside))

        let outside = WorkTimelineRecord(id: "out", laneID: "s", laneTitle: "S", lineage: "root",
                                         kind: .check, title: "Tests", start: 9_000)
        XCTAssertEqual(WorkTimelineRangeNavigation.reframed(window, toShow: outside),
                       WorkTimelineRangeNavigation.focused(on: outside))

        // A section whose span crosses the window still draws its card at its
        // start, so it needs the re-frame its card position asks for.
        var section = WorkTimelineRecord(id: "section", laneID: "s", laneTitle: "S", lineage: "root",
                                         kind: .step, title: "Section", start: 200)
        section.end = 5_000
        XCTAssertEqual(WorkTimelineRangeNavigation.reframed(window, toShow: section),
                       WorkTimelineRangeNavigation.focused(on: section))

        // No recorded time is no position to re-frame to; the undated menu
        // still names the record, and nothing moves.
        let undated = WorkTimelineRecord(id: "undated", laneID: "s", laneTitle: "S", lineage: "root",
                                         kind: .check, title: "Tests", start: nil)
        XCTAssertNil(WorkTimelineRangeNavigation.reframed(window, toShow: undated))
    }

    /// The expanded detail is revealed by the MINIMUM scroll that holds the row
    /// and its detail together — and by no scroll at all when they already fit.
    func testExpandedRowIsRevealedByTheSmallestScrollAndNeverWhenItAlreadyFits() {
        let visible = CGRect(x: 0, y: 500, width: 800, height: 400)
        let document = CGSize(width: 800, height: 4_000)

        // Already on screen: the reading position is the reviewer's.
        XCTAssertNil(RegionReveal.contentOffset(region: CGRect(x: 0, y: 600, width: 800, height: 200),
                                                visible: visible, document: document, flipped: true))
        // Hanging past the bottom edge: scroll exactly far enough, not to the
        // region's own top (that would be a 100 pt jump instead of 12).
        XCTAssertEqual(RegionReveal.contentOffset(region: CGRect(x: 0, y: 600, width: 800, height: 304),
                                                  visible: visible, document: document, flipped: true),
                       CGPoint(x: 0, y: 512))
        // Above the top edge: the same rule in the other direction.
        XCTAssertEqual(RegionReveal.contentOffset(region: CGRect(x: 0, y: 450, width: 800, height: 100),
                                                  visible: visible, document: document, flipped: true),
                       CGPoint(x: 0, y: 442))
        // Taller than the viewport: show the TOP — the row that was pressed —
        // in a flipped clip view, and the equivalent edge in an unflipped one.
        let tall = CGRect(x: 0, y: 600, width: 800, height: 900)
        XCTAssertEqual(RegionReveal.contentOffset(region: tall, visible: visible, document: document, flipped: true),
                       CGPoint(x: 0, y: 592))
        XCTAssertEqual(RegionReveal.contentOffset(region: tall, visible: visible, document: document, flipped: false),
                       CGPoint(x: 0, y: 1_108))
        // Never past the end of the document, and never for nonsense geometry.
        XCTAssertEqual(RegionReveal.contentOffset(region: CGRect(x: 0, y: 3_900, width: 800, height: 100),
                                                  visible: visible, document: document, flipped: true),
                       CGPoint(x: 0, y: 3_600))
        XCTAssertNil(RegionReveal.contentOffset(region: CGRect(x: 0, y: CGFloat.nan, width: 800, height: 100),
                                                visible: visible, document: document, flipped: true))
        XCTAssertNil(RegionReveal.contentOffset(region: CGRect(x: 0, y: 600, width: 800, height: 0),
                                                visible: visible, document: document, flipped: true))
    }

    /// The same numbers applied to a real clip view: the offset lands inside
    /// the document and the region ends up visible.
    @MainActor func testTheRevealOffsetLeavesTheRegionInsideARealClipView() throws {
        let scroll = NSScrollView(frame: CGRect(x: 0, y: 0, width: 800, height: 400))
        let document = NSView(frame: CGRect(x: 0, y: 0, width: 800, height: 4_000))
        scroll.documentView = document
        scroll.contentView.scroll(to: CGPoint(x: 0, y: 500))
        scroll.reflectScrolledClipView(scroll.contentView)
        let region = CGRect(x: 0, y: 600, width: 800, height: 304)
        let offset = try XCTUnwrap(RegionReveal.contentOffset(region: region, visible: scroll.contentView.bounds,
                                                             document: document.bounds.size,
                                                             flipped: scroll.contentView.isFlipped))
        scroll.contentView.scroll(to: offset)
        scroll.reflectScrolledClipView(scroll.contentView)
        let now = scroll.contentView.bounds
        XCTAssertTrue(now.minY <= region.minY && now.maxY >= region.maxY)
        XCTAssertNil(RegionReveal.contentOffset(region: region, visible: now,
                                               document: document.bounds.size,
                                               flipped: scroll.contentView.isFlipped))
    }

    func testReturnRequestKeepsOriginalRecordAcrossRemountAndIsConsumedOnce() {
        var focus = WorkTimelineFocusRestoration()
        focus.remember(taskID: "a", recordID: "check-failed")
        focus.prepare(taskID: "a")
        focus.remember(taskID: "a", recordID: nil)
        XCTAssertEqual(focus.consume(taskID: "a", visibleRecordIDs: ["check-failed", "check-passed"]), .record("check-failed"))
        XCTAssertNil(focus.consume(taskID: "a", visibleRecordIDs: ["check-failed"]))
    }

    func testMissingOrFilteredRecordReturnsToHeadingWithoutSelectingDifferentEvidence() {
        var focus = WorkTimelineFocusRestoration()
        focus.remember(taskID: "a", recordID: "removed")
        focus.prepare(taskID: "a")
        XCTAssertEqual(focus.consume(taskID: "a", visibleRecordIDs: ["another"]), .heading)
    }

    func testNewInvestigationActionCancelsPendingReturnFocus() {
        var focus = WorkTimelineFocusRestoration()
        focus.remember(taskID: "a", recordID: "earlier")
        focus.prepare(taskID: "a")
        focus.cancel()
        XCTAssertNil(focus.consume(taskID: "a", visibleRecordIDs: ["earlier"]))
    }

    func testTaskChangeDiscardsPendingFocusAndCannotBorrowAnotherTaskRecord() {
        var focus = WorkTimelineFocusRestoration()
        focus.remember(taskID: "a", recordID: "same-looking-id")
        focus.prepare(taskID: "a")
        XCTAssertNil(focus.consume(taskID: "b", visibleRecordIDs: ["same-looking-id"]))
        focus.prepare(taskID: "b")
        XCTAssertEqual(focus.consume(taskID: "b", visibleRecordIDs: ["same-looking-id"]), .heading)
    }
}
