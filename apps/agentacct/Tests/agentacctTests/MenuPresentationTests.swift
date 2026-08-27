import XCTest
@testable import agentacct

final class MenuPresentationTests: XCTestCase {
    func testWeeklyHeroNamesPreferredSourceAndRemovesItsDuplicate() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())
        let presentation = MenuLimitPresentation(glance: fixture.glance)

        XCTAssertEqual(presentation.primary?.client, "claude-code")
        XCTAssertEqual(presentation.primary?.sourceLabel, "Claude Code · 7-day limit")
        XCTAssertEqual(presentation.primary?.percentageText, "47%")
        XCTAssertFalse(presentation.secondary.contains { $0.id == presentation.primary?.id })
        XCTAssertEqual(presentation.secondary.count, 3)
    }

    func testWeeklyHeroFallsBackToLiveSevenDayLimitAndNamesStaleAbsence() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())
        let sparse = try XCTUnwrap(fixture.menuSparseGlance)
        let fallback = MenuLimitPresentation(glance: sparse)

        XCTAssertEqual(fallback.primary?.sourceLabel, "Codex · 7-day limit")
        XCTAssertEqual(fallback.primary?.percentageText, "4%")
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
              "windows": [{ "kind": "7d", "used_percent": 21 }]
            },
            {
              "client": "codex",
              "stream_id": "codex_rate_limit:gpt-5-spark",
              "org": "personal",
              "windows": [{ "kind": "7d", "used_percent": 64 }]
            }
          ],
          "plan": [],
          "recent_sessions": []
        }
        """)
        let presentation = MenuLimitPresentation(glance: glance)

        XCTAssertEqual(presentation.primary?.percentageText, "64%")
        XCTAssertEqual(presentation.secondary.map(\.percentageText), ["21%"])
        XCTAssertNotEqual(presentation.primary?.id, presentation.secondary.first?.id)
    }

    func testUsageUsesWindowDurationAndNamesMissingEvidence() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())
        let sparse = try XCTUnwrap(fixture.menuSparseGlance)
        let populated = MenuUsagePresentation(usage: sparse.usage)

        XCTAssertEqual(populated.rows.map(\.label), ["Today", "Last 7 days", "Last 30 days"])
        XCTAssertEqual(populated.rows[0].costText, "≈$200.67")
        XCTAssertEqual(populated.rows[2].costText, "~$990.99")
        XCTAssertEqual(populated.rows[2].tokenText, "41.9M")
        XCTAssertEqual(populated.legendText, "≈ estimate · ~ priced subtotal")

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
        XCTAssertEqual(absent.rows[1].costText, "Unpriced")
        XCTAssertEqual(absent.rows[1].tokenText, "Not reported")
    }

    func testCalibrationProgressIsSpecificAndBounded() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())
        let sparse = try XCTUnwrap(fixture.menuSparseGlance)
        let presentation = try XCTUnwrap(MenuCalibrationPresentation(sparse.plan))

        XCTAssertEqual(
            presentation.summary,
            "Claude Code session share calibrating · 9/24 intervals"
        )
        XCTAssertTrue(presentation.detail?.contains("stable intervals") == true)
    }

    private func fixtureURL() throws -> URL {
        try XCTUnwrap(Bundle.module.url(forResource: "dashboard", withExtension: "json"))
    }

    private func decodeGlance(_ json: String) throws -> Glance {
        try JSONDecoder().decode(Glance.self, from: Data(json.utf8))
    }
}
