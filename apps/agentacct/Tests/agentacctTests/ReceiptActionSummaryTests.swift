import XCTest
@testable import agentacct

/// The tool-call digest renders the reducer's `actions_synopsis` verbatim: the
/// taxonomy, integrity states, named absences and the tile are Python-owned
/// (`display_vocabulary.actions_synopsis`, pinned in tests/test_display_vocabulary.py).
/// These tests pin that the app decodes that payload and derives nothing but
/// presentation from it.
final class ReceiptActionSummaryTests: XCTestCase {
    private func decode(_ json: String) throws -> ReceiptActionSynopsis {
        try JSONDecoder().decode(ReceiptActionSynopsis.self, from: Data(json.utf8))
    }

    func testDecodesTheReducerSynopsisVerbatim() throws {
        let exact = try decode("""
        {
          "state": "exact",
          "headline": "80 tool calls captured",
          "integrity_detail": null,
          "metrics": [
            {"key": "read", "label": "Read", "detail": "File or context read tool calls", "count": 38},
            {"key": "edit", "label": "Edit", "detail": "Edit or write tool calls", "count": 42}
          ],
          "can_show_distribution": true,
          "capture_boundary": "No ordered action ledger; captured tool-call counts cannot be linked to results or timing.",
          "stored_total": 80,
          "categorized_total": 80,
          "tile": {"value": "80", "absent": null, "qualifier": "Hook-captured"}
        }
        """)
        XCTAssertEqual(exact.integrity, .exact)
        XCTAssertEqual(exact.headlineText, "80 tool calls captured")
        XCTAssertEqual(exact.metrics.map(\.label), ["Read", "Edit"])
        XCTAssertEqual(exact.shareDenominator, 80)
        XCTAssertEqual(exact.tile, ReceiptTileText(value: "80", absent: nil, qualifier: "Hook-captured"))
    }

    func testNamedAbsencesStayNamedAndNeverDrawAShare() throws {
        for (state, words) in [
            ("not_instrumented", "not instrumented"),
            ("no_tool_calls", "no tool calls recorded"),
            ("capture_unknown", "capture coverage unknown"),
        ] {
            let synopsis = try decode("""
            {"state": "\(state)", "headline": "\(words)", "metrics": [], "can_show_distribution": false,
             "stored_total": 0, "tile": {"value": null, "absent": "\(words)", "qualifier": null}}
            """)
            XCTAssertTrue(synopsis.integrity.isAbsence, state)
            XCTAssertEqual(synopsis.headlineText, words)
            XCTAssertNil(synopsis.shareDenominator)
            XCTAssertNil(synopsis.tile?.value)
        }
    }

    func testShareDenominatorNeedsTheReducersReconciledPartition() throws {
        // The reducer says a distribution is dishonest (a conflict): no share,
        // even though the numbers are present.
        let mismatch = try decode("""
        {"state": "mismatch", "headline": "Tool-call totals conflict",
         "integrity_detail": "category counts sum to 12 · stored total is 10",
         "metrics": [{"key": "read", "label": "Read", "detail": "d", "count": 12}],
         "can_show_distribution": false, "stored_total": 10, "categorized_total": 12,
         "tile": {"value": null, "absent": "tool-call totals conflict", "qualifier": null}}
        """)
        XCTAssertEqual(mismatch.integrity, .mismatch)
        XCTAssertNil(mismatch.shareDenominator)
        XCTAssertEqual(mismatch.integrityDetail, "category counts sum to 12 · stored total is 10")
    }

    func testMissingSynopsisIsANamedAbsenceNotAZero() throws {
        let dim = try JSONDecoder().decode(ReceiptActionsDim.self, from: Data("{}".utf8))
        XCTAssertEqual(dim.synopsis.headlineText, PayloadAbsence.toolCalls)
        XCTAssertNil(dim.synopsis.shareDenominator)
        XCTAssertEqual(ReceiptActionIntegrity(payload: "brand_new"), .captureUnknown)
    }

    func testSnapshotFixturesComeFromTheReducer() {
        // The gallery's fixtures decode (a malformed paste would render blanks).
        XCTAssertEqual(ActionSynopsisSnapshotFixtures.byID.count, 10)
        XCTAssertEqual(ActionSynopsisSnapshotFixtures.synopsis("zero").headlineText, "not instrumented")
        XCTAssertEqual(ActionSynopsisSnapshotFixtures.synopsis("exact").shareDenominator, 80)
    }
}
