import XCTest
@testable import agentacct

final class UsageTokenBasisTests: XCTestCase {
    func testUsesCanonicalTotalAcrossEveryUsageSurface() throws {
        // The total includes cache creation AND reads. Never derive it by
        // adding cache reads to fresh or adding caches to inclusive raw input.
        let json = Data("""
        {"fresh_tokens":120,"cache_creation_tokens":30,"cache_read_tokens":850,
         "total_tokens_including_cached":1000}
        """.utf8)
        let decoder = JSONDecoder()
        let values: [any UsageTokenValues] = [
            try decoder.decode(UsageBucket.self, from: json),
            try decoder.decode(PeriodBucket.self, from: json),
            try decoder.decode(PeriodClientSlice.self, from: json),
            try decoder.decode(UsageTotals.self, from: json),
        ]
        for value in values {
            XCTAssertEqual(UsageTokenBasis.fresh.value(value), 120)
            XCTAssertEqual(UsageTokenBasis.all.value(value), 1000)
        }
    }

    func testMissingOrInvalidTotalNeverMasqueradesAsFreshOrZero() throws {
        for fields in ["", ",\"total_tokens_including_cached\":null", ",\"total_tokens_including_cached\":-1"] {
            let bucket = try JSONDecoder().decode(UsageBucket.self, from: Data("{\"fresh_tokens\":120\(fields)}".utf8))
            XCTAssertEqual(UsageTokenBasis.fresh.value(bucket), 120)
            XCTAssertNil(UsageTokenBasis.all.value(bucket))
        }
        XCTAssertNil(UsageTokenBasis.all.value(nil))
        let zero = try JSONDecoder().decode(UsageBucket.self, from: Data("{\"total_tokens_including_cached\":0}".utf8))
        XCTAssertEqual(UsageTokenBasis.all.value(zero), 0)
        XCTAssertNil(UsageTokenBasis.fresh.value(zero))
    }

    func testClientSeriesAndRangeTotalsAgreeInBothModes() throws {
        let fixture = try DashboardSnapshotFixture.load(from: Bundle.module.url(forResource: "dashboard", withExtension: "json")!)
        for usage in [fixture.usage, fixture.usage90Days] {
            for basis in UsageTokenBasis.allCases {
                let total = try XCTUnwrap(basis.value(usage.totals))
                XCTAssertEqual(usage.byClient.compactMap { basis.value($0) }.reduce(0, +), total)
                XCTAssertEqual(usage.byModel.compactMap { basis.value($0) }.reduce(0, +), total)
                XCTAssertEqual((usage.byPeriod ?? []).compactMap { basis.value($0) }.reduce(0, +), total)
            }
        }
        let today = try XCTUnwrap(fixture.glance.usage.windows.first { $0.label == "today" }?.totals)
        for basis in UsageTokenBasis.allCases {
            XCTAssertEqual(basis.value(today), basis.value(fixture.usage.byPeriod?.last))
        }
        for period in fixture.usage.byPeriod ?? [] {
            for basis in UsageTokenBasis.allCases {
                XCTAssertEqual(period.byClient!.values.compactMap { basis.value($0) }.reduce(0, +), basis.value(period))
            }
        }
    }

    func testCacheOnlyUsageCountsAsAnActivePeriodAndLabelsMatchBasis() throws {
        let usage = try JSONDecoder().decode(UsageSummary.self, from: Data("""
        {"by_client":[],"by_model":[],"filters_echo":{"granularity":"weekly"},
         "by_period":[{"fresh_tokens":0,"total_tokens_including_cached":900}]}
        """.utf8))
        let presentation = UsagePeriodPresentation(usage: usage)
        XCTAssertEqual(presentation.value, "1/1")
        XCTAssertEqual(presentation.tokenChartTitle(group: "codex", basis: .all), "All tokens per week · codex")
        XCTAssertEqual(presentation.tokenChartTitle(group: nil, basis: .fresh), "Fresh tokens per week")
    }

    func testCapacityAccessibilityUsesTheSelectedCount() throws {
        let bucket = try JSONDecoder().decode(UsageBucket.self, from: Data("{\"fresh_tokens\":120,\"total_tokens_including_cached\":1000}".utf8))
        let row = UsageCapacityRow(id: "codex", client: "codex", usage: bucket, readings: [], plan: nil, hasHiddenStaleReading: false)
        XCTAssertTrue(row.accessibilitySummary(days: 7, tokenBasis: .all).contains("1000 all tokens"))
        XCTAssertFalse(row.accessibilitySummary(days: 7, tokenBasis: .all).contains("fresh tokens"))
        XCTAssertTrue(row.accessibilitySummary(days: 7, tokenBasis: .fresh).contains("120 fresh tokens"))
    }
}
