import XCTest
@testable import agentacct

/// The chart's peak label yields to the tooltip on the same bar, so the peak
/// value is printed once.
final class UsageChartPeakLabelTests: XCTestCase {
    func testLabelShowsWhenAnotherBarIsActive() {
        XCTAssertTrue(UsageChartPeakLabel.isShown(peakIndex: 6, activeIndex: 3, peakValue: 525.14))
    }

    func testLabelShowsWhenNothingIsActive() {
        XCTAssertTrue(UsageChartPeakLabel.isShown(peakIndex: 6, activeIndex: nil, peakValue: 525.14))
    }

    func testLabelYieldsToTheTooltipOnThePeakBar() {
        XCTAssertFalse(UsageChartPeakLabel.isShown(peakIndex: 6, activeIndex: 6, peakValue: 525.14))
    }

    func testNoLabelWithoutAPositivePeak() {
        XCTAssertFalse(UsageChartPeakLabel.isShown(peakIndex: nil, activeIndex: nil, peakValue: nil))
        XCTAssertFalse(UsageChartPeakLabel.isShown(peakIndex: 2, activeIndex: nil, peakValue: 0))
    }
}
