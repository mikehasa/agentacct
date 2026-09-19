import XCTest
@testable import agentacct

/// The recorded-usage range control prints its caption once. The segmented
/// picker's own label is hidden, so "Recorded usage range" is both the visible
/// caption and the control's accessibility name, and each offered window has
/// a short segment label.
final class UsageRangePresentationTests: XCTestCase {
    func testCaptionIsTheSingleLabelForTheControl() {
        XCTAssertEqual(UsageRangePresentation.caption, "Recorded usage range")
    }

    func testOfferedWindowsAreSevenThirtyAndNinetyDays() {
        XCTAssertEqual(UsageRangePresentation.options.map(\.days), [7, 30, 90])
        XCTAssertEqual(UsageRangePresentation.options.map(\.label), ["7d", "30d", "90d"])
    }

    func testSegmentLabelNamesTheSelectedWindow() {
        XCTAssertEqual(UsageRangePresentation.label(forDays: 7), "7d")
        XCTAssertEqual(UsageRangePresentation.label(forDays: 90), "90d")
        // A window outside the three offered still reads as its own length.
        XCTAssertEqual(UsageRangePresentation.label(forDays: 14), "14d")
    }
}
