import Foundation
import XCTest
@testable import agentacct

final class TemporalTextTests: XCTestCase {
    private var context: TemporalTextContext {
        var calendar = Calendar(identifier: .gregorian)
        calendar.locale = Locale(identifier: "en_US_POSIX")
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        return TemporalTextContext(
            now: Date(timeIntervalSince1970: 1_800_000_000),
            locale: Locale(identifier: "en_US_POSIX"),
            calendar: calendar,
            timeZone: TimeZone(secondsFromGMT: 0)!
        )
    }

    func testAgeUsesNaturalBoundariesAndNamesInvalidFutureTime() {
        let now = context.now.timeIntervalSince1970

        XCTAssertEqual(TemporalText.age(epoch: now, context: context), "just now")
        XCTAssertEqual(TemporalText.age(epoch: now - 59, context: context), "just now")
        XCTAssertEqual(TemporalText.age(epoch: now - 60, context: context), "1 min ago")
        XCTAssertEqual(TemporalText.age(epoch: now - 3_600, context: context), "1 hr ago")
        XCTAssertEqual(TemporalText.age(epoch: now - 86_400, context: context), "1 day ago")
        XCTAssertEqual(
            TemporalText.age(epoch: now - 8 * 86_400, context: context),
            TemporalText.date(epoch: now - 8 * 86_400, context: context)
        )
        XCTAssertEqual(TemporalText.age(epoch: now + 120, context: context), "time appears incorrect")
        XCTAssertNil(TemporalText.age(epoch: nil, context: context))
        XCTAssertNil(TemporalText.age(epoch: .nan, context: context))
    }

    func testRecordedSpanAndCadenceNeverRenderPositiveTimeAsZero() {
        XCTAssertEqual(TemporalText.recordedSpan(seconds: 0), "0 min")
        XCTAssertEqual(TemporalText.recordedSpan(seconds: 1), "<1 min")
        XCTAssertEqual(TemporalText.recordedSpan(seconds: 59), "<1 min")
        XCTAssertEqual(TemporalText.recordedSpan(seconds: 60), "1 min")
        XCTAssertEqual(TemporalText.recordedSpan(seconds: 3_660), "1 hr 1 min")

        XCTAssertEqual(TemporalText.interval(seconds: 30), "30 sec")
        XCTAssertEqual(TemporalText.interval(seconds: 0.25), "<1 sec")
        XCTAssertEqual(TemporalText.interval(seconds: 300), "5 min")
        XCTAssertEqual(TemporalText.interval(seconds: 3_600), "1 hr")
        XCTAssertNil(TemporalText.interval(seconds: .infinity))
        XCTAssertNil(TemporalText.interval(seconds: Double.greatestFiniteMagnitude))
        XCTAssertNil(TemporalText.exactDateTime(epoch: Double.greatestFiniteMagnitude, context: context))
    }

    func testProviderResetKeepsMissingFuturePastAndInvalidDistinct() {
        let now = context.now.timeIntervalSince1970

        XCTAssertEqual(TemporalText.providerReset(epoch: nil, context: context), .missing)
        XCTAssertEqual(
            TemporalText.providerReset(epoch: now + 30, context: context),
            .future(countdown: "<1 min")
        )
        XCTAssertEqual(
            TemporalText.providerReset(epoch: now + 86_400, context: context).text,
            "Resets in 1 day"
        )
        let past = TemporalText.exactDateTime(epoch: now - 60, context: context)
        XCTAssertEqual(
            TemporalText.providerReset(epoch: now - 60, context: context),
            .passed(dateTime: try! XCTUnwrap(past))
        )
        XCTAssertEqual(TemporalText.providerReset(epoch: .nan, context: context), .invalid)
    }

    func testSecondaryFreshnessOnlyAppearsWhenItAddsInformation() {
        let primary = Date(timeIntervalSince1970: 1_000)
        XCTAssertFalse(TemporalText.shouldShowSecondaryRefresh(nil, primary: primary))
        XCTAssertTrue(TemporalText.shouldShowSecondaryRefresh(primary, primary: nil))
        XCTAssertFalse(
            TemporalText.shouldShowSecondaryRefresh(
                primary.addingTimeInterval(59),
                primary: primary
            )
        )
        XCTAssertTrue(
            TemporalText.shouldShowSecondaryRefresh(
                primary.addingTimeInterval(60),
                primary: primary
            )
        )
    }
}
