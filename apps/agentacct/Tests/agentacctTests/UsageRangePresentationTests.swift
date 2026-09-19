import XCTest
@testable import agentacct

/// The recorded-usage range control is labelled once: a caps label beside the
/// house segmented control on the title row, with the full phrase as the
/// control's accessibility name, and a short segment label per window.
final class UsageRangePresentationTests: XCTestCase {
    func testCaptionAndAccessibilityNameSayTheSameThingOnce() {
        XCTAssertEqual(UsageRangePresentation.caption, "Recorded range")
        XCTAssertEqual(UsageRangePresentation.accessibilityName, "Recorded usage range")
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
