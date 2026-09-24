import XCTest
@testable import agentacct

final class UsageRangeSummaryTests: XCTestCase {
    private func fixture() throws -> DashboardSnapshotFixture {
        try DashboardSnapshotFixture.load(from: XCTUnwrap(Bundle.module.url(forResource: "dashboard", withExtension: "json")))
    }

    func testAgentRowsRankByRecordedTokensAndKeepEveryCostBasis() throws {
        let usage = try fixture().usage
        let rows = UsageRangeLedger.agentRows(in: usage)

        // The ledger never re-orders the payload: the cube already returns
        // clients by recorded token volume.
        XCTAssertEqual(rows.map(\.name), usage.byClient.compactMap(\.client))
        XCTAssertEqual(rows.map(\.name), ["codex", "claude-code", "hermes"])
        XCTAssertEqual(rows.map(\.id), ["agent:codex", "agent:claude-code", "agent:hermes"])
        XCTAssertTrue(rows.compactMap(\.detail).isEmpty)
        XCTAssertEqual(rows.map { $0.bucket.sessions }, [43, 9, 2])
        XCTAssertEqual(
            rows.map { UsageTokenColumn.fresh.metric($0.bucket).value },
            [31_200_000, 16_500_000, 400_000]
        )
        XCTAssertEqual(
            rows.map { UsageTokenColumn.total.metric($0.bucket).value },
            [127_500_000, 68_100_000, 800_000]
        )
        XCTAssertEqual(rows.map { $0.bucket.costText }, ["≈$402.10", "≈$680.32", "≈$5.00"])
        XCTAssertEqual(rows.map { $0.bucket.costConfidenceLabel }, ["complete", "complete", "complete"])
        // This legacy-shaped lane reports cache reads without a reporting flag.
        // The column keeps the number and marks the coverage incomplete rather
        // than presenting it as exact.
        XCTAssertEqual(
            UsageTokenColumn.cache.metric(rows[0].bucket),
            .init(value: 96_300_000, partial: true)
        )
        XCTAssertEqual(UsageTokenColumn.cache.metric(rows[0].bucket).text, "96.3M*")
    }

    func testModelRowsKeepTheSameModelNameSeparatePerAgent() throws {
        let usage = try XCTUnwrap(fixture().usageDailyBreakdown)
        let rows = UsageRangeLedger.modelRows(in: usage)

        XCTAssertEqual(rows.map(\.name), ["gpt-6", "gpt-6"])
        XCTAssertEqual(rows.compactMap(\.detail), ["codex · openai", "hermes · openai"])
        XCTAssertEqual(Set(rows.map(\.id)).count, 2)
        XCTAssertEqual(rows.map { $0.bucket.sessions }, [2, 3])
        XCTAssertEqual(rows.map { $0.bucket.costText }, ["≈$17.60", "~$1.40"])
        XCTAssertEqual(rows.map { $0.bucket.costConfidenceLabel }, ["estimated", "known partial subtotal"])
        // The partial subtotal keeps its marker and its incomplete cache write.
        XCTAssertEqual(
            UsageTokenColumn.cacheWrite.metric(rows[1].bucket),
            .init(value: 20_000, partial: true)
        )
        XCTAssertTrue(UsageTokenColumn.total.metric(rows[1].bucket).partial)
    }

    func testHeldAndUnpricedBucketsRankLastWithoutReadingAsZero() throws {
        let usage = try decodeSummary(byClient: """
        {"client":"held-agent","rows":1,"usage_availability":"held","fresh_tokens":0,"total_tokens_including_cached":0,"cost_complete":false},
        {"client":"codex","sessions":2,"fresh_tokens":100,"total_tokens_including_cached":500,"estimated_cost_usd":1.25,"known_additive_cost_usd":1.25,"cost_complete":true}
        """)
        let rows = UsageRangeLedger.agentRows(in: usage)

        XCTAssertEqual(rows.map(\.name), ["codex", "held-agent"])
        let held = try XCTUnwrap(rows.last?.bucket)
        XCTAssertNil(UsageTokenColumn.fresh.metric(held).value)
        XCTAssertNil(UsageTokenColumn.cache.metric(held).value)
        XCTAssertNil(UsageTokenColumn.total.metric(held).value)
        XCTAssertEqual(UsageTokenColumn.total.metric(held).text, "—")
        XCTAssertEqual(held.costText, "—")
        XCTAssertNil(held.sessions)
    }

    func testDuplicateIdentityKeepsTheFirstBucketAndEqualTotalsFallBackToName() throws {
        let usage = try decodeSummary(
            byClient: """
            {"client":"codex","fresh_tokens":10,"total_tokens_including_cached":10},
            {"client":"codex","fresh_tokens":999,"total_tokens_including_cached":999},
            {"client":"claude-code","fresh_tokens":10,"total_tokens_including_cached":10},
            {"client":"hermes","fresh_tokens":7,"total_tokens_including_cached":7}
            """,
            byModel: """
            {"client":"codex","model":"gpt-6","total_tokens_including_cached":10},
            {"client":"claude-code","model":"gpt-6","total_tokens_including_cached":10}
            """
        )

        XCTAssertEqual(UsageRangeLedger.agentRows(in: usage).map(\.name), ["claude-code", "codex", "hermes"])
        XCTAssertEqual(UsageRangeLedger.agentRows(in: usage).first { $0.name == "codex" }?.bucket.freshTokens, 10)
        XCTAssertEqual(
            UsageRangeLedger.modelRows(in: usage).map(\.detail),
            ["claude-code", "codex"]
        )
    }

    func testEmptyRangeNamesTheAbsenceInsteadOfRenderingZeroRows() throws {
        let usage = try decodeSummary(byClient: "", byModel: "")

        XCTAssertTrue(UsageRangeLedger.agentRows(in: usage).isEmpty)
        XCTAssertTrue(UsageRangeLedger.modelRows(in: usage).isEmpty)
        let presentation = UsageRangePresentation(days: 90)
        XCTAssertEqual(presentation.emptyText, "No agent or model usage reported in this range.")
        XCTAssertEqual(presentation.agentEmptyText, "No agent usage reported in this range.")
        XCTAssertEqual(presentation.modelEmptyText, "No model attribution reported in this range.")
    }

    func testPresentationNamesTheRangeAndItsActivityDateAttribution() {
        let presentation = UsageRangePresentation(days: 30)

        XCTAssertEqual(presentation.title, "This range")
        XCTAssertEqual(presentation.caption, "last 30 days · by agent and by model")
        XCTAssertEqual(presentation.agentTitle, "By agent")
        XCTAssertEqual(presentation.modelTitle, "By model")
        XCTAssertTrue(presentation.attributionNote.contains("activity date"))
        XCTAssertTrue(presentation.attributionNote.contains("multi-day session"))
        XCTAssertTrue(presentation.helpMessage.contains("≈$ estimate"))
        XCTAssertTrue(presentation.helpMessage.contains("unpriced"))
        XCTAssertTrue(presentation.helpMessage.contains("The chart's Fresh/All choice changes the chart, not these columns."))
    }

    private func decodeSummary(byClient: String, byModel: String = "") throws -> UsageSummary {
        let json = """
        {
          "by_client": [\(byClient)],
          "by_model": [\(byModel)],
          "by_period": [],
          "totals": null
        }
        """
        return try JSONDecoder().decode(UsageSummary.self, from: Data(json.utf8))
    }
}
