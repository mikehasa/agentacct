import XCTest
@testable import agentacct

final class UsagePeriodPresentationTests: XCTestCase {
    func testUsesEffectiveDailyGranularity() throws {
        let presentation = UsagePeriodPresentation(
            usage: try usageSummary(granularity: "daily")
        )

        XCTAssertEqual(presentation.label, "Active days")
        XCTAssertEqual(presentation.value, "2/3")
        XCTAssertEqual(presentation.absent, "no daily series")
        XCTAssertEqual(presentation.costChartTitle, "Cost per day")
        XCTAssertEqual(presentation.tokenChartTitle(group: nil), "Fresh tokens per day")
        XCTAssertEqual(presentation.previousAccessibilityLabel, "Previous usage day")
        XCTAssertEqual(presentation.selectionAccessibilityHint, "Selects this day's value")
    }

    func testUsesEffectiveWeeklyGranularity() throws {
        let presentation = UsagePeriodPresentation(
            usage: try usageSummary(granularity: "weekly")
        )

        XCTAssertEqual(presentation.label, "Active weeks")
        XCTAssertEqual(presentation.value, "2/3")
        XCTAssertEqual(presentation.absent, "no weekly series")
        XCTAssertEqual(presentation.costChartTitle, "Cost per week")
        XCTAssertEqual(
            presentation.tokenChartTitle(group: "codex"),
            "Fresh tokens per week · codex"
        )
        XCTAssertEqual(presentation.nextAccessibilityLabel, "Next usage week")
        XCTAssertEqual(presentation.selectionAccessibilityHint, "Selects this week's value")
    }

    func testUnknownOrMissingGranularityUsesTruthfulGenericCopy() throws {
        for granularity in ["monthly", nil] as [String?] {
            let presentation = UsagePeriodPresentation(
                usage: try usageSummary(granularity: granularity)
            )

            XCTAssertEqual(presentation.label, "Active periods")
            XCTAssertEqual(presentation.value, "2/3")
            XCTAssertEqual(presentation.absent, "no period series")
            XCTAssertEqual(presentation.costChartTitle, "Cost per period")
            XCTAssertEqual(presentation.previousAccessibilityLabel, "Previous usage period")
        }
    }

    func testEmptySeriesKeepsGranularitySpecificAbsenceCopy() throws {
        let presentation = UsagePeriodPresentation(
            usage: try usageSummary(granularity: "weekly", periods: "")
        )

        XCTAssertEqual(presentation.label, "Active weeks")
        XCTAssertNil(presentation.value)
        XCTAssertEqual(presentation.absent, "no weekly series")
    }

    private func usageSummary(
        granularity: String?,
        periods: String = """
        {"period":"2026-08-01","fresh_tokens":120},
        {"period":"2026-08-02","fresh_tokens":0},
        {"period":"2026-08-03","estimated_cost_usd":0}
        """
    ) throws -> UsageSummary {
        let filtersEcho = granularity.map {
            ",\"filters_echo\":{\"granularity\":\"\($0)\"}"
        } ?? ""
        let json = """
        {
          "by_client": [],
          "by_model": [],
          "by_period": [\(periods)],
          "totals": null
          \(filtersEcho)
        }
        """
        return try JSONDecoder().decode(UsageSummary.self, from: Data(json.utf8))
    }
}
