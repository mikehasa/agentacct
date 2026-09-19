import XCTest
@testable import agentacct

final class MenuPresentationTests: XCTestCase {
    func testHeroIsTheReducerHeadlineWindowAndRemovesItsDuplicate() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())
        let presentation = MenuLimitPresentation(glance: fixture.glance)

        // The hero is the payload's headline_limit_key (the most constrained
        // live window: claude-code 7d at 47% over codex 7d at 39%) — no client
        // is preferred by name (K11).
        XCTAssertEqual(fixture.glance.headlineLimitKey, "1|claude-code|7d|0")
        XCTAssertEqual(presentation.primary?.client, "claude-code")
        // The client identity is the slug (C54); the window name is the
        // reducer's window_label, and a window with no reset instant keeps the
        // reducer's named absence.
        XCTAssertEqual(presentation.primary?.sourceLabel, "claude-code · 7-day limit")
        XCTAssertEqual(presentation.primary?.resetText, "reset time not reported")
        XCTAssertTrue(presentation.secondary.contains { $0.resetText == "resets in 6d 13h" })
        XCTAssertEqual(presentation.primary?.valueText, "47% used")
        XCTAssertFalse(presentation.secondary.contains { $0.id == presentation.primary?.id })
        XCTAssertEqual(presentation.secondary.count, 3)
    }

    func testWeeklyHeroFallsBackToLiveSevenDayLimitAndNamesStaleAbsence() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())
        let sparse = try XCTUnwrap(fixture.menuSparseGlance)
        let fallback = MenuLimitPresentation(glance: sparse)

        XCTAssertEqual(fallback.primary?.client, "codex")
        XCTAssertEqual(fallback.primary?.valueText, "4% used")
        XCTAssertTrue(fallback.secondary.isEmpty)

        let stale = MenuLimitPresentation(glance: try decodeGlance("""
        {
          "schema": "agentacct.glance.v1",
          "usage": { "windows": [] },
          "limits": [{
            "client": "codex",
            "stale": true,
            "windows": [{ "kind": "7d", "used_percent": 88 }]
          }],
          "plan": [],
          "recent_sessions": []
        }
        """))
        XCTAssertNil(stale.primary)
        XCTAssertTrue(stale.hasStaleLimits)
    }

    func testIdenticalLimitLabelsKeepIndependentStreamIdentity() throws {
        let glance = try decodeGlance("""
        {
          "schema": "agentacct.glance.v1",
          "usage": { "windows": [] },
          "limits": [
            {
              "client": "codex",
              "stream_id": "codex_rate_limit",
              "org": "personal",
              "windows": [{ "kind": "7d", "used_percent": 21, "limit_key": "0|codex|7d|0", "value_text": "21% used" }]
            },
            {
              "client": "codex",
              "stream_id": "codex_rate_limit:gpt-5-spark",
              "org": "personal",
              "windows": [{ "kind": "7d", "used_percent": 64, "limit_key": "1|codex|7d|0", "value_text": "64% used" }]
            }
          ],
          "headline_limit_key": "1|codex|7d|0",
          "plan": [],
          "recent_sessions": []
        }
        """)
        let presentation = MenuLimitPresentation(glance: glance)

        XCTAssertEqual(presentation.primary?.valueText, "64% used")
        XCTAssertEqual(presentation.secondary.map(\.valueText), ["21% used"])
        XCTAssertNotEqual(presentation.primary?.id, presentation.secondary.first?.id)
    }

    func testHeroFollowsTheHeadlineKeyNotAClientNameAndPassedResetsReadAsHistory() throws {
        let glance = try decodeGlance("""
        {
          "schema": "agentacct.glance.v1",
          "usage": { "windows": [] },
          "limits": [
            {
              "client": "claude-code",
              "data_age_text": "as of 1d 15h ago",
              "windows": [
                { "kind": "7d", "used_percent": 3, "limit_key": "0|claude-code|7d|0", "value_text": "3% used" },
                { "kind": "5h", "used_percent": 3, "limit_key": "0|claude-code|5h|1",
                  "reset_passed": true, "value_text": "last reported 3%" }
              ]
            },
            {
              "client": "codex",
              "data_age_text": "as of 3d 0h ago",
              "windows": [{ "kind": "7d", "used_percent": 99, "limit_key": "1|codex|7d|0", "value_text": "99% used" }]
            }
          ],
          "headline_limit_key": "1|codex|7d|0",
          "plan": [],
          "recent_sessions": []
        }
        """)
        let presentation = MenuLimitPresentation(glance: glance)

        XCTAssertEqual(presentation.primary?.client, "codex")
        XCTAssertEqual(presentation.primary?.valueText, "99% used")
        XCTAssertEqual(presentation.primary?.dataAgeText, "as of 3d 0h ago")
        let passed = try XCTUnwrap(presentation.secondary.first { $0.kind == "5h" })
        XCTAssertTrue(passed.resetPassed)
        XCTAssertEqual(passed.valueText, "last reported 3%")

        // No headline key: no hero is invented and no client is preferred.
        let unkeyed = MenuLimitPresentation(glance: try decodeGlance("""
        {
          "schema": "agentacct.glance.v1",
          "usage": { "windows": [] },
          "limits": [{ "client": "claude-code", "windows": [{ "kind": "7d", "used_percent": 50 }] }],
          "plan": [],
          "recent_sessions": []
        }
        """))
        XCTAssertNil(unkeyed.primary)
        XCTAssertEqual(unkeyed.secondary.count, 1)
    }

    func testUsageUsesWindowDurationAndNamesMissingEvidence() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())
        let sparse = try XCTUnwrap(fixture.menuSparseGlance)
        let populated = MenuUsagePresentation(usage: sparse.usage)

        XCTAssertEqual(populated.rows.map(\.label), ["Today", "Last 7 days", "Last 30 days"])
        XCTAssertEqual(populated.rows[0].costText, "≈$200.67")
        XCTAssertEqual(populated.rows[2].costText, "~$990.99")
        XCTAssertEqual(populated.rows[2].tokenText, "41.9M")
        // The payload legend's glyph/definition pairs are bound with NO-BREAK
        // SPACE (U+00A0) in the vocabulary, so a wrapping popover can only
        // break at " · " and never orphans a symbol from its meaning (K25).
        // The basis prefix in front of it is ordinary prose and wraps freely.
        let legend = try XCTUnwrap(populated.legendText)
        XCTAssertEqual(
            legend,
            "pricing estimate · ~$\u{00a0}partial\u{00a0}subtotal · ≈$\u{00a0}estimate · $\u{00a0}reported\u{00a0}or\u{00a0}billed"
        )
        for pair in legend.components(separatedBy: " · ").dropFirst() {
            XCTAssertFalse(pair.contains(" "), "legend pair \(pair) can be split across lines")
        }

        let missing = try decodeGlance("""
        {
          "schema": "agentacct.glance.v1",
          "usage": {
            "windows": [{ "label": "last 7 days", "totals": {} }]
          },
          "limits": [],
          "plan": [],
          "recent_sessions": []
        }
        """)
        let absent = MenuUsagePresentation(usage: missing.usage)
        XCTAssertEqual(absent.rows[1].costText, "cost not reported")
        XCTAssertEqual(absent.rows[1].tokenText, "not reported")
    }

    func testUsageLegendIsThePayloadStringNotASwiftCopy() throws {
        let glance = try decodeGlance("""
        {
          "schema": "agentacct.glance.v1",
          "usage": {
            "windows": [
              { "label": "7d", "days": 7,
                "totals": { "fresh_tokens": 10, "estimated_cost_usd": 1.0, "cost_complete": true,
                            "cost_state": "complete", "cost_confidence_display": "pricing estimate" } }
            ],
            "cost_legend": "legend words from the reducer"
          },
          "limits": [],
          "plan": [],
          "recent_sessions": []
        }
        """)
        XCTAssertEqual(
            MenuUsagePresentation(usage: glance.usage).legendText,
            "pricing estimate · legend words from the reducer"
        )
    }

    func testCalibrationProgressIsSpecificAndBounded() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())
        let sparse = try XCTUnwrap(fixture.menuSparseGlance)
        let presentation = try XCTUnwrap(MenuCalibrationPresentation(sparse.plan))

        XCTAssertEqual(presentation.client, "claude-code")
        XCTAssertEqual(presentation.progressText, "9/24 intervals")
        XCTAssertTrue(presentation.detail?.contains("stable intervals") == true)

        let glance = try decodeGlance("""
        {
          "schema": "agentacct.glance.v1",
          "usage": { "windows": [] },
          "limits": [],
          "plan": [{
            "client": "claude-code",
            "confidence": "baseline",
            "calibration_state": "calibrating",
            "intervals_used": 9,
            "intervals_needed": 24,
            "headline": "calibrating — not enough 7-day history yet"
          }],
          "recent_sessions": []
        }
        """)
        let calibrating = try XCTUnwrap(MenuCalibrationPresentation(glance.plan))
        XCTAssertEqual(
            calibrating.summary,
            "claude-code calibrating — not enough 7-day history yet · 9/24 intervals"
        )
    }

    func testOutOfBandCalibrationNamesUnavailableShareWithoutProgressFraction() throws {
        let glance = try decodeGlance("""
        {
          "schema": "agentacct.glance.v1",
          "usage": { "windows": [] },
          "limits": [],
          "plan": [{
            "client": "codex",
            "confidence": "baseline",
            "calibration_state": "out_of_band",
            "intervals_used": null,
            "intervals_needed": null,
            "headline": "Won't calibrate: recorded usage doesn't track the weekly % closely enough",
            "basis_text": "160 intervals recorded; the fit (x8.50) is outside the trusted band [0.5, 2.5]"
          }],
          "recent_sessions": []
        }
        """)
        let presentation = try XCTUnwrap(MenuCalibrationPresentation(glance.plan))
        XCTAssertEqual(
            presentation.summary,
            "codex Won't calibrate: recorded usage doesn't track the weekly % closely enough"
        )
        XCTAssertNil(presentation.progressText)
        // The technical fit detail stays behind the help affordance.
        XCTAssertEqual(
            presentation.detail,
            "160 intervals recorded; the fit (x8.50) is outside the trusted band [0.5, 2.5]"
        )

        // A still-"calibrating" entry whose counts already meet the need never
        // shows a nonsensical progress fraction such as 160/3.
        let stuck = try decodeGlance("""
        {
          "schema": "agentacct.glance.v1",
          "usage": { "windows": [] },
          "limits": [],
          "plan": [{
            "client": "codex",
            "confidence": "baseline",
            "calibration_state": "calibrating",
            "intervals_used": 160,
            "intervals_needed": 3,
            "headline": "calibrating — not enough 7-day history yet"
          }],
          "recent_sessions": []
        }
        """)
        XCTAssertNil(try XCTUnwrap(MenuCalibrationPresentation(stuck.plan)).progressText)
    }

    func testLimitRowsRenderReducerWindowAndResetPhrases() throws {
        let glance = try decodeGlance("""
        {
          "schema": "agentacct.glance.v1",
          "usage": { "windows": [] },
          "limits": [{
            "client": "claude-code",
            "stream_id": "claude_code_rate_limit",
            "windows": [
              { "kind": "7d", "used_percent": 47, "resets_at": 1788184000, "limit_key": "0|claude-code|7d|0",
                "window_label": "7-day limit", "reset_text": "resets in 4d 3h" },
              { "kind": "5h", "used_percent": 7, "resets_at": 1000, "limit_key": "0|claude-code|5h|1",
                "reset_passed": true, "value_text": "last reported 7%",
                "window_label": "5-hour limit", "reset_text": "reset passed Sep 13, 10:50 PM" }
            ]
          }],
          "headline_limit_key": "0|claude-code|7d|0",
          "plan": [],
          "recent_sessions": []
        }
        """)
        let presentation = MenuLimitPresentation(glance: glance)
        XCTAssertEqual(presentation.primary?.sourceLabel, "claude-code · 7-day limit")
        XCTAssertEqual(presentation.primary?.resetText, "resets in 4d 3h")
        XCTAssertEqual(presentation.secondary.first?.sourceLabel, "claude-code · 5-hour limit")
        // A past reset is its own named state, never "not reported".
        XCTAssertEqual(presentation.secondary.first?.resetText, "reset passed Sep 13, 10:50 PM")
    }

    func testUsageLedgerNamesNoUsageAndRendersReducerBasis() throws {
        let glance = try decodeGlance("""
        {
          "schema": "agentacct.glance.v1",
          "usage": {
            "windows": [
              { "label": "today", "days": 1,
                "totals": { "usage_availability": "unknown", "rows": 0, "cost_state": "none_recorded" } },
              { "label": "7d", "days": 7,
                "totals": { "fresh_tokens": 1200, "estimated_cost_usd": 2.5, "cost_complete": false,
                            "known_additive_cost_usd": 2.2, "cost_state": "partial",
                            "cost_confidence_display": "mixed · mostly pricing estimate" } },
              { "label": "30d", "days": 30,
                "totals": { "fresh_tokens": 1200, "estimated_cost_usd": 2.5, "cost_complete": false,
                            "known_additive_cost_usd": 2.2, "cost_state": "partial",
                            "cost_confidence_display": "mixed · mostly pricing estimate" } }
            ],
            "cost_legend": "~$ partial subtotal · ≈$ estimate · $ reported or billed"
          },
          "limits": [],
          "plan": [],
          "recent_sessions": []
        }
        """)
        let usage = MenuUsagePresentation(usage: glance.usage)
        XCTAssertEqual(usage.rows[0].costText, "no usage recorded")
        XCTAssertFalse(usage.rows[0].isPriced)
        XCTAssertEqual(usage.rows[1].basisText, "mixed · mostly pricing estimate")
        XCTAssertEqual(
            usage.legendText,
            "mixed · mostly pricing estimate · ~$ partial subtotal · ≈$ estimate · $ reported or billed"
        )
    }

    private func fixtureURL() throws -> URL {
        try XCTUnwrap(Bundle.module.url(forResource: "dashboard", withExtension: "json"))
    }

    private func decodeGlance(_ json: String) throws -> Glance {
        try JSONDecoder().decode(Glance.self, from: Data(json.utf8))
    }
}
