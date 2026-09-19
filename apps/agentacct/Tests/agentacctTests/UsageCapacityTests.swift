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

    func testWindowStatusRendersTheReducerValuePhraseVerbatim() throws {
        let windows = try decode([LimitWindow].self, from: """
        [
          {"kind":"7d","used_percent":99,"value_text":"99% used"},
          {"kind":"5h","used_percent":3,"reset_passed":true,"value_text":"last reported 3%"},
          {"kind":"7d","used_percent":100,"value_text":"100% used · limit reached"},
          {"kind":"7d"}
        ]
        """)
        let presentations = windows.map { LimitWindowPresentation(window: $0, stale: false) }

        XCTAssertEqual(presentations.map(\.statusText), [
            "99% used",
            "last reported 3%",
            "100% used · limit reached",
            "used percent not reported",
        ])
        // One threshold text color: ink below 75, amber from 75, coral from 90;
        // a passed-reset share is muted history, never cobalt (K11/K32).
        XCTAssertEqual(presentations[0].statusColor, Theme.coral)
        XCTAssertEqual(presentations[1].statusColor, Theme.muted)
        XCTAssertTrue(presentations[1].resetPassed)
        XCTAssertEqual(Theme.limitTextColor(usedPercent: 3), Theme.ink)
        XCTAssertEqual(Theme.limitTextColor(usedPercent: 80), Theme.amber)
        XCTAssertNotEqual(Theme.limitTextColor(usedPercent: 3), Theme.accent)
    }

    func testResetCopyRendersTheReducerPhraseCapitalizedAtSentenceStartOnly() throws {
        let windows = try decode([LimitWindow].self, from: """
        [
          {"kind":"7d","used_percent":50,"resets_at":1000,"reset_text":"reset passed Sep 15, 3:36 AM"},
          {"kind":"7d","used_percent":50,"resets_at":9000000000,"reset_text":"resets in 4d 3h"},
          {"kind":"7d","used_percent":50}
        ]
        """)
        let texts = windows.map { LimitWindowPresentation(window: $0, stale: false).resetText }

        // A passed reset is named as passed, never as "not reported".
        XCTAssertEqual(texts[0], "Reset passed Sep 15, 3:36 AM")
        XCTAssertEqual(texts[1], "Resets in 4d 3h")
        XCTAssertEqual(texts[2], "Reset time not reported")
    }

    func testWindowNameComesFromTheReducerWindowLabel() throws {
        let windows = try decode([LimitWindow].self, from: """
        [
          {"kind":"7d","window_label":"7-day limit"},
          {"kind":"5h","window_label":"5-hour limit"},
          {"kind":"7d"}
        ]
        """)
        let names = windows.map { LimitWindowPresentation(window: $0, stale: false).name }

        XCTAssertEqual(names, ["7-day limit", "5-hour limit", "limit window"])
    }

    func testCostPresentationKeepsAbsenceAndPartialCoverageMutuallyExclusive() throws {
        let buckets = try decode([UsageBucket].self, from: """
        [
          {"client":"none","rows":0,"priced_rows":0,"unpriced_rows":0,"cost_state":"none_recorded",
           "cost_complete":false,"cost_confidence_display":"cost basis not reported"},
          {"client":"openclaw","rows":3,"priced_rows":0,"unpriced_rows":3,"cost_state":"unpriced",
           "cost_complete":false,"cost_confidence_display":"cost basis not reported",
           "cost_total_label":"no priced usage · 3 of 3 usage records unpriced"},
          {"client":"mixed","rows":9,"priced_rows":7,"unpriced_rows":2,"cost_state":"partial",
           "cost_total_label":"Partial subtotal · 2 of 9 usage records unpriced",
           "cost_complete":false,"known_additive_cost_usd":1635.57,
           "cost_confidence":"estimated_from_tokens","cost_confidence_display":"mixed · mostly pricing estimate"},
          {"client":"done","rows":4,"priced_rows":4,"unpriced_rows":0,"cost_state":"complete",
           "cost_complete":true,"estimated_cost_usd":10.77,
           "cost_confidence":"estimated_from_tokens","cost_confidence_display":"pricing estimate"}
        ]
        """)
        let presentations = buckets.map(UsageCostPresentation.init(bucket:))

        XCTAssertNil(presentations[0].figure)
        XCTAssertEqual(presentations[0].valueText, "no usage recorded")
        XCTAssertNil(presentations[0].qualifier)

        XCTAssertNil(presentations[1].figure)
        XCTAssertEqual(presentations[1].valueText, "no priced usage · 3 of 3 usage records unpriced")
        XCTAssertNil(presentations[1].qualifier)
        XCTAssertFalse(presentations[1].accessibilityText.contains("Partial subtotal"))

        XCTAssertEqual(presentations[2].figure, "~$1,635.57")
        XCTAssertEqual(
            presentations[2].qualifier,
            "Partial subtotal · 2 of 9 usage records unpriced · mixed · mostly pricing estimate"
        )

        XCTAssertEqual(presentations[3].figure, "≈$10.77")
        XCTAssertEqual(presentations[3].qualifier, "pricing estimate")
    }

    func testOutOfBandPlanShareIsAMutedChipWithoutAProgressFraction() throws {
        let client = try decode(V1PlanClient.self, from: """
        {"client":"codex","calibration_state":"out_of_band","intervals_used":160,"intervals_needed":3,
         "chip_text":"plan share unavailable","sentence_text":"won't calibrate at current ratio",
         "headline":"Won't calibrate: recorded usage doesn't track the weekly % closely enough",
         "basis_text":"the fit (x8.50) is outside the trusted band [0.5, 2.5]"}
        """)

        let presentation = UsagePlanPresentation(client: client, days: 7)

        // Payload words only: the chip, the plain conclusion first, the fit
        // detail behind the disclosure (K36/K113).
        XCTAssertEqual(presentation.chipText, "plan share unavailable")
        XCTAssertTrue(presentation.detailText.hasPrefix("Won't calibrate:"))
        XCTAssertFalse(presentation.detailText.contains("trusted band"))
        XCTAssertEqual(presentation.basisText, "the fit (x8.50) is outside the trusted band [0.5, 2.5]")
        XCTAssertEqual(UsagePlanPresentation.chipTint(calibrationState: "out_of_band"), Theme.muted)
        XCTAssertNotEqual(UsagePlanPresentation.chipTint(calibrationState: "out_of_band"), Theme.amber)
        XCTAssertFalse(presentation.detailText.contains("160 of 3"))
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
          "model_tokens_label":"tokens incl. cache-read",
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
        XCTAssertEqual(presentation.modelRows[0], "opus · ≈0% · 0 tokens incl. cache-read")
        XCTAssertEqual(presentation.modelRows[1], "sonnet · share not reported · tokens incl. cache-read not reported")
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

    func testMainNavigationPanesAndNoLimitsTab() {
        XCTAssertEqual(MainPane.allCases, [.dashboard, .worksets, .work, .usage, .sources])
        XCTAssertFalse(MainPane.allCases.map(\.rawValue).contains("Limits"))
    }

    private func decode<Value: Decodable>(_ type: Value.Type, from json: String) throws -> Value {
        try JSONDecoder().decode(type, from: Data(json.utf8))
    }
}
