import XCTest
@testable import agentacct

final class UsageDailyBreakdownTests: XCTestCase {
    private func fixture() throws -> DashboardSnapshotFixture {
        try DashboardSnapshotFixture.load(from: XCTUnwrap(Bundle.module.url(forResource: "dashboard", withExtension: "json")))
    }

    func testSelectedDateKeepsSameModelSeparateByClientAndChangesNumbers() throws {
        let usage = try XCTUnwrap(fixture().usageDailyBreakdown)
        XCTAssertEqual(UsageDaySelection.latestActive(in: usage), "2026-08-25")
        let latest = UsageDaySelection.groups(in: usage, period: "2026-08-25")
        XCTAssertEqual(latest.map(\.client), ["codex", "hermes"])
        XCTAssertEqual(latest.flatMap(\.models).map(\.model), ["gpt-6", "gpt-6"])
        XCTAssertEqual(Set(latest.flatMap(\.models).map(\.id)).count, 2)
        XCTAssertEqual(latest.map { $0.totals?.freshTokens }, [1_000_000, 200_000])
        let earlier = UsageDaySelection.groups(in: usage, period: "2026-08-23")
        XCTAssertEqual(earlier.map { $0.totals?.freshTokens }, [500_000, 120_000])
        let middle = UsageDaySelection.groups(in: usage, period: "2026-08-24")
        XCTAssertEqual(middle.map(\.client), ["hermes"])
        XCTAssertEqual(middle.first?.totals?.freshTokens, 210_000)
        let all = UsageDaySelection.groups(in: usage, period: nil)
        XCTAssertEqual(all.map { $0.totals?.freshTokens }, [1_500_000, 530_000])
    }

    func testDailyFixtureReconcilesEveryTokenColumnThroughClientAndModel() throws {
        let usage = try XCTUnwrap(fixture().usageDailyBreakdown)
        for period in usage.byPeriod ?? [] {
            let groups = UsageDaySelection.groups(in: usage, period: period.period)
            for column in [UsageTokenColumn.fresh, .cacheRead, .cacheWrite, .total] {
                let clientValues = groups.compactMap { column.metric($0.totals).value }
                let modelValues = groups.flatMap(\.models).compactMap { column.metric($0).value }
                XCTAssertEqual(clientValues.reduce(0, +), modelValues.reduce(0, +))
                XCTAssertEqual(clientValues.reduce(0, +), column.metric(period.usage).value ?? 0)
            }
            XCTAssertEqual(groups.compactMap { $0.totals?.knownAdditiveCostUsd }.reduce(0, +), period.usage.knownAdditiveCostUsd ?? 0, accuracy: 0.001)
        }
        XCTAssertEqual(usage.byPeriod?.compactMap(\.freshTokens).reduce(0, +), usage.totals?.freshTokens)
        XCTAssertEqual(usage.byPeriod?.compactMap(\.totalTokensIncludingCached).reduce(0, +), usage.totals?.totalTokensIncludingCached)
    }

    func testUnreportedCacheZeroIsUnknownAndPartialCountsRemainExplicit() throws {
        let usage = try XCTUnwrap(fixture().usageDailyBreakdown)
        let partial = try XCTUnwrap(UsageDaySelection.groups(in: usage, period: "2026-08-25").last?.totals)
        XCTAssertNil(UsageTokenColumn.cacheWrite.metric(partial).value)
        XCTAssertEqual(UsageTokenColumn.cacheRead.metric(partial), .init(value: 1_200_000))
        XCTAssertEqual(UsageTokenColumn.cache.metric(partial), .init(value: 1_200_000, partial: true))
        XCTAssertTrue(UsageTokenColumn.total.metric(partial).partial)
        XCTAssertEqual(partial.costText, "—")
        XCTAssertTrue(try XCTUnwrap(usage.totals).costText.hasPrefix("~$"))
        let reported = try XCTUnwrap(UsageDaySelection.groups(in: usage, period: "2026-08-25").first?.totals)
        XCTAssertEqual(UsageTokenColumn.cacheWrite.metric(reported), .init(value: 0))
    }

    func testMissingDailyAttributionNeverSubstitutesWholeRangeModels() throws {
        let legacy = try fixture().usage
        let date = try XCTUnwrap(legacy.byPeriod?.last?.period)
        XCTAssertFalse(legacy.byModel.isEmpty)
        XCTAssertTrue(UsageDaySelection.groups(in: legacy, period: date).flatMap(\.models).isEmpty)
        XCTAssertTrue(UsageDaySelection.groups(in: legacy, period: "missing-date").isEmpty)
        XCTAssertEqual(UsageDaySelection.groups(in: legacy, period: nil).flatMap(\.models).count, legacy.byModel.count)
    }

    func testHeldAndMissingTotalsDoNotBecomeZeroAndProviderIdentityIsDistinct() throws {
        func decode(_ text: String) throws -> UsageBucket { try JSONDecoder().decode(UsageBucket.self, from: Data(text.utf8)) }
        let held = try decode(#"{"usage_availability":"held","rows":1,"fresh_tokens":0,"total_tokens_including_cached":0}"#)
        XCTAssertNil(UsageTokenColumn.fresh.metric(held).value)
        XCTAssertNil(UsageTokenColumn.total.metric(held).value)
        XCTAssertNil(UsageTokenColumn.total.metric(try decode(#"{"fresh_tokens":100}"#)).value)
        let empty = try decode(#"{"rows":0,"fresh_tokens":0,"cache_creation_tokens":0}"#)
        XCTAssertEqual(UsageTokenColumn.cacheWrite.metric(empty), .init(value: 0))
        let a = try decode(#"{"client":"hermes","provider":"openai","model":"gpt-6"}"#)
        let b = try decode(#"{"client":"hermes","provider":"openrouter","model":"gpt-6"}"#)
        XCTAssertNotEqual(a.id, b.id)
    }

    func testCostAndTokenHistoryUseTheSamePeriodsWithoutInventingUnpricedZero() throws {
        let usage = try XCTUnwrap(fixture().usageDailyBreakdown)
        let periods = try XCTUnwrap(usage.byPeriod)
        XCTAssertEqual(UsageHistoryMeasure.cost.value(periods[0], basis: .fresh), 7)
        XCTAssertNil(UsageHistoryMeasure.cost.value(periods[1], basis: .all))
        XCTAssertEqual(UsageHistoryMeasure.cost.text(periods[1], basis: .fresh), "Unpriced")
        XCTAssertEqual(UsageHistoryMeasure.tokens.value(periods[2], basis: .fresh), 1_200_000)
        XCTAssertEqual(UsageHistoryMeasure.tokens.value(periods[2], basis: .all), 6_400_000)
        XCTAssertTrue(UsageHistoryMeasure.cost.text(periods[2], basis: .all).hasPrefix("~$"))
    }

    func testLegacyPartialCostAndHeldChartAreHonest() throws {
        let legacy = try JSONDecoder().decode(PeriodBucket.self, from: Data(#"{"period":"2026-08-23","estimated_cost_usd":12.5,"cost_complete":false}"#.utf8))
        XCTAssertEqual(legacy.costText, "~$12.50")
        let held = try JSONDecoder().decode(PeriodBucket.self, from: Data(#"{"rows":1,"usage_availability":"held","fresh_tokens":0,"total_tokens_including_cached":0}"#.utf8))
        XCTAssertNil(UsageHistoryMeasure.tokens.value(held, basis: .fresh))
        XCTAssertNil(UsageHistoryMeasure.tokens.value(held, basis: .all))
        XCTAssertEqual(UsageHistoryMeasure.tokens.text(held, basis: .all), "Not reported")
    }

    @MainActor func testRenderDailyBreakdownReview() throws {
        let root = ProcessInfo.processInfo.environment["AGENTACCT_USAGE_BREAKDOWN_REVIEW_DIR"]
        let output = root.map { URL(fileURLWithPath: $0) } ?? FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { if root == nil { try? FileManager.default.removeItem(at: output) } }
        let configurations = UsageSnapshotConfiguration.reviewConfigurations.filter { $0.viewport.hasPrefix("day-clients") || $0.viewport == "minimum" }
        XCTAssertEqual(try UsageSnapshotRenderer.render(fixture: fixture(), outputDirectory: output, configurations: configurations).count, 6)
    }
}
