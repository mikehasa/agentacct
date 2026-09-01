import XCTest
@testable import agentacct

final class UsageCapacityTests: XCTestCase {
    func testBuildJoinsUnionAndSortsOnlyValidFreshCapacityAsRisk() throws {
        let usage = try decode([UsageBucket].self, from: """
        [
          {"client":"codex","fresh_tokens":100,"sessions":2},
          {"client":"claude-code","fresh_tokens":900,"sessions":3},
          {"client":"hermes","fresh_tokens":500,"sessions":1}
        ]
        """)
        let limits = try decode([LimitEntry].self, from: """
        [
          {"client":"codex","windows":[{"kind":"7d","used_percent":91}]},
          {"client":"claude-code","windows":[{"kind":"5h"}]},
          {"client":"hermes","stale":true,"windows":[{"kind":"7d","used_percent":99}]}
        ]
        """)

        let result = UsageCapacitySnapshot.build(
            usage: usage, limits: limits, plans: [], showStale: false
        )

        XCTAssertEqual(result.rows.map(\.client), ["codex", "claude-code", "hermes"])
        XCTAssertEqual(result.rows[0].highestFreshValidUsedPercent, 91)
        XCTAssertNil(result.rows[1].highestFreshValidUsedPercent)
        XCTAssertTrue(result.rows[2].hasHiddenStaleReading)
        XCTAssertTrue(result.rows[2].readings.isEmpty)
        XCTAssertEqual(result.hiddenStaleCount, 1)
    }

    func testBuildPreservesDuplicateEntriesAndEveryWindow() throws {
        let limits = try decode([LimitEntry].self, from: """
        [
          {"client":"codex","plan_type":"pro","windows":[{"kind":"5h","used_percent":12}]},
          {"client":"codex","plan_type":"team","windows":[
            {"kind":"7d","used_percent":44},
            {"kind":"monthly_beta","used_percent":8}
          ]}
        ]
        """)

        let result = UsageCapacitySnapshot.build(
            usage: [], limits: limits, plans: [], showStale: false
        )

        let row = try XCTUnwrap(result.rows.first)
        XCTAssertEqual(result.rows.count, 1)
        XCTAssertEqual(row.readings.count, 2)
        XCTAssertEqual(row.readings.flatMap { $0.entry.windows ?? [] }.count, 3)
        XCTAssertEqual(row.planTypes, ["pro", "team"])
        XCTAssertEqual(row.highestFreshValidUsedPercent, 44)
    }

    func testBuildKeepsFreshAndStaleSiblingsWithoutLettingStaleDriveRisk() throws {
        let limits = try decode([LimitEntry].self, from: """
        [
          {"client":"codex","windows":[{"kind":"7d","used_percent":41}]},
          {"client":"codex","stale":true,"windows":[{"kind":"7d","used_percent":99}]}
        ]
        """)

        let hidden = UsageCapacitySnapshot.build(
            usage: [], limits: limits, plans: [], showStale: false
        )
        let shown = UsageCapacitySnapshot.build(
            usage: [], limits: limits, plans: [], showStale: true
        )

        XCTAssertEqual(try XCTUnwrap(hidden.rows.first).readings.count, 1)
        XCTAssertTrue(try XCTUnwrap(hidden.rows.first).hasHiddenStaleReading)
        XCTAssertEqual(try XCTUnwrap(shown.rows.first).readings.count, 2)
        XCTAssertEqual(try XCTUnwrap(shown.rows.first).highestFreshValidUsedPercent, 41)
    }

    func testUnnamedCapacityNeverJoinsUnattributedUsage() throws {
        let usage = try decode([UsageBucket].self, from: """
        [{"fresh_tokens":40,"sessions":1}]
        """)
        let limits = try decode([LimitEntry].self, from: """
        [{"windows":[{"kind":"7d","used_percent":30}]}]
        """)

        let result = UsageCapacitySnapshot.build(
            usage: usage, limits: limits, plans: [], showStale: false
        )

        XCTAssertEqual(Set(result.rows.map(\.client)), ["Client name not reported", "Unattributed client"])
        XCTAssertEqual(result.rows.filter { $0.usage != nil }.count, 1)
        XCTAssertEqual(result.rows.filter { !$0.readings.isEmpty }.count, 1)
    }

    func testStaleOnlyLimitIsHiddenUntilExplicitlyRevealed() throws {
        let limits = try decode([LimitEntry].self, from: """
        [{"client":"claude-code","stale":true,"windows":[{"kind":"7d","used_percent":82}]}]
        """)

        let hidden = UsageCapacitySnapshot.build(
            usage: [], limits: limits, plans: [], showStale: false
        )
        XCTAssertTrue(hidden.rows.isEmpty)
        XCTAssertEqual(hidden.hiddenStaleCount, 1)

        let shown = UsageCapacitySnapshot.build(
            usage: [], limits: limits, plans: [], showStale: true
        )
        XCTAssertEqual(shown.rows.map(\.client), ["claude-code"])
        XCTAssertTrue(try XCTUnwrap(shown.rows.first).isStaleOnly)
        XCTAssertNil(try XCTUnwrap(shown.rows.first).highestFreshValidUsedPercent)
        XCTAssertEqual(shown.hiddenStaleCount, 0)
    }

    func testLimitStatusNamesZeroThresholdInvalidAndExceededWithoutClampingText() throws {
        let windows = try decode([LimitWindow].self, from: """
        [
          {"kind":"7d","used_percent":0},
          {"kind":"7d","used_percent":75},
          {"kind":"7d","used_percent":90},
          {"kind":"7d","used_percent":100},
          {"kind":"7d","used_percent":127},
          {"kind":"7d","used_percent":-5},
          {"kind":"7d"}
        ]
        """)
        let statuses = windows.map {
            LimitWindowPresentation(window: $0, stale: false).statusText
        }

        XCTAssertEqual(statuses[0], "0% used")
        XCTAssertEqual(statuses[1], "75% used · at attention threshold")
        XCTAssertEqual(statuses[2], "90% used · high attention")
        XCTAssertEqual(statuses[3], "100% used · limit reached")
        XCTAssertEqual(statuses[4], "127% used · limit exceeded")
        XCTAssertEqual(statuses[5], "Invalid provider percentage (-5%)")
        XCTAssertEqual(statuses[6], "Used percent not reported")
    }

    func testWindowSpanHandlesZeroAndOutOfRangeValuesWithoutTrapping() throws {
        let zero = try decode(LimitWindow.self, from: """
        {"kind":"custom","window_minutes":0}
        """)
        let huge = try decode(LimitWindow.self, from: """
        {"kind":"custom","window_minutes":1.7976931348623157e308}
        """)

        XCTAssertEqual(LimitWindowPresentation(window: zero, stale: false).spanText, "0m span")
        XCTAssertEqual(LimitWindowPresentation(window: huge, stale: false).spanText, "Invalid span")
    }

    @MainActor
    func testResetCopyDoesNotClaimAReportedPastResetOccurred() throws {
        SnapshotMode.setFixtureDate(Date(timeIntervalSince1970: 2_000))
        defer { SnapshotMode.setFixtureDate(nil) }
        let window = try decode(LimitWindow.self, from: """
        {"kind":"7d","used_percent":50,"resets_at":1000}
        """)

        let text = LimitWindowPresentation(window: window, stale: false).resetText

        XCTAssertTrue(text.hasPrefix("Reported reset passed "))
        XCTAssertFalse(text.contains("Resets today"))
    }

    func testResetClockUsesAnUnambiguousTwentyFourHourTime() {
        let date = Date(timeIntervalSince1970: 1_788_184_000)

        XCTAssertEqual(
            usageResetClockText(date, timeZone: TimeZone(secondsFromGMT: 0)!),
            "13:46"
        )
    }

    func testAccessibilitySummaryDistinguishesMissingValuesFromObservedZero() throws {
        let usage = try decode([UsageBucket].self, from: """
        [
          {"client":"missing"},
          {"client":"zero","fresh_tokens":0,"sessions":0}
        ]
        """)
        let rows = UsageCapacitySnapshot.build(
            usage: usage, limits: [], plans: [], showStale: false
        ).rows
        let summaries = Dictionary(uniqueKeysWithValues: rows.map { ($0.client, $0.accessibilitySummary(days: 7)) })

        XCTAssertTrue(try XCTUnwrap(summaries["missing"]).contains("tokens not reported"))
        XCTAssertTrue(try XCTUnwrap(summaries["missing"]).contains("sessions not reported"))
        XCTAssertTrue(try XCTUnwrap(summaries["zero"]).contains("0 fresh tokens"))
        XCTAssertTrue(try XCTUnwrap(summaries["zero"]).contains("0 sessions"))
    }

    func testAccessibilitySummaryDistinguishesLoadedAbsenceFromPendingUsage() throws {
        let limits = try decode([LimitEntry].self, from: """
        [{"client":"codex","windows":[]}]
        """)
        let row = try XCTUnwrap(UsageCapacitySnapshot.build(
            usage: [], limits: limits, plans: [], showStale: false
        ).rows.first)

        let loaded = row.accessibilitySummary(days: 7, usageLoaded: true)
        let pending = row.accessibilitySummary(days: 7, usageLoaded: false)

        XCTAssertTrue(loaded.contains("provider reading contained no quota windows"))
        XCTAssertTrue(loaded.contains("no recorded usage in this range"))
        XCTAssertTrue(pending.contains("recorded usage not loaded"))
        XCTAssertFalse(pending.contains("no recorded usage in this range"))
    }

    func testPlanPresentationPreservesCalibrationDailyAndModelTruth() throws {
        let client = try decode(V1PlanClient.self, from: """
        {
          "client":"claude-code",
          "calibration_state":"calibrated",
          "intervals_used":3,
          "intervals_needed":3,
          "window_pcts":{"today":0,"7d":12.25},
          "unknown_time_pct":0.05,
          "daily":[{"date":"2026-08-01","pct":0},{"date":"2026-08-08","pct":4.5}],
          "by_model":[
            {"model":"opus","pct":0,"total_tokens":0},
            {"model":"sonnet"},
            {"model":"overflow","pct":1,"total_tokens":9.223372036854776e18}
          ]
        }
        """)

        let presentation = UsagePlanPresentation(client: client, days: 30)

        XCTAssertTrue(presentation.detailText.contains("today ≈0% of weekly plan"))
        XCTAssertTrue(presentation.detailText.contains("7d ≈12.2% of weekly plan"))
        XCTAssertTrue(presentation.detailText.contains("≈<0.1% from unusable timestamps"))
        XCTAssertTrue(try XCTUnwrap(presentation.dailyText).contains("2 reported days"))
        XCTAssertTrue(try XCTUnwrap(presentation.dailyText).contains("unreported dates are not zero"))
        XCTAssertEqual(presentation.dailyRows, [
            "2026-08-01 · ≈0% of weekly plan",
            "2026-08-08 · ≈4.5% of weekly plan",
        ])
        XCTAssertEqual(presentation.modelHeading, "Model plan-share estimates · accumulated over last 30d")
        XCTAssertEqual(presentation.modelRows[0], "opus · ≈0% · 0 tokens")
        XCTAssertEqual(presentation.modelRows[1], "sonnet · share not reported · tokens not reported")
        XCTAssertEqual(presentation.modelRows[2], "overflow · ≈1.0% · invalid token total")
    }

    func testPlanPresentationDoesNotExposeShareBeforeCalibration() throws {
        let client = try decode(V1PlanClient.self, from: """
        {
          "client":"claude-code",
          "calibration_state":"calibrating",
          "intervals_used":0,
          "intervals_needed":3,
          "window_pcts":{"today":44,"7d":88}
        }
        """)

        let presentation = UsagePlanPresentation(client: client, days: 7)

        XCTAssertTrue(presentation.detailText.contains("0 of 3 clean intervals observed"))
        XCTAssertFalse(presentation.detailText.contains("44"))
        XCTAssertFalse(presentation.detailText.contains("88"))
        XCTAssertNil(presentation.dailyText)
        XCTAssertTrue(presentation.modelRows.isEmpty)
    }

    func testBuildRetainsOneHundredClientRowsWithStableUniqueIdentities() throws {
        let usageJSON = (0..<100).map { index in
            "{\"client\":\"client-\(index)\",\"fresh_tokens\":\(index)}"
        }.joined(separator: ",")
        let limitsJSON = stride(from: 0, to: 100, by: 2).map { index in
            "{\"client\":\"client-\(index)\",\"windows\":[{\"kind\":\"7d\",\"used_percent\":\(index)}]}"
        }.joined(separator: ",")
        let usage = try decode([UsageBucket].self, from: "[\(usageJSON)]")
        let limits = try decode([LimitEntry].self, from: "[\(limitsJSON)]")

        let rows = UsageCapacitySnapshot.build(
            usage: usage, limits: limits, plans: [], showStale: false
        ).rows

        XCTAssertEqual(rows.count, 100)
        XCTAssertEqual(Set(rows.map(\.id)).count, 100)
        XCTAssertEqual(rows.first?.client, "client-98")
        XCTAssertEqual(rows.last?.client, "client-1")
    }

    func testMainNavigationHasFourPanesAndNoLimitsTab() {
        XCTAssertEqual(MainPane.allCases, [.dashboard, .work, .usage, .sources])
        XCTAssertFalse(MainPane.allCases.map(\.rawValue).contains("Limits"))
    }

    private func decode<Value: Decodable>(_ type: Value.Type, from json: String) throws -> Value {
        try JSONDecoder().decode(type, from: Data(json.utf8))
    }
}
