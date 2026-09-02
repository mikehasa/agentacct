import XCTest
@testable import agentacct

/// The Receipt DETAIL Outcome row shows one factual sub-line — "Quiet since …
/// · a newer session started …" — but ONLY when the daemon attached
/// `quiet_since` / `newer_session_started_at` (the `inactive`/`mostly_done`
/// keys). The app is a pure renderer here: it formats the timestamps the daemon
/// already decided to attach and never derives the honesty logic itself.
final class ReceiptOutcomeSublineTests: XCTestCase {
    private func decodeOutcome(_ json: String) throws -> ReceiptOutcomeDim {
        try JSONDecoder().decode(ReceiptOutcomeDim.self, from: Data(json.utf8))
    }

    /// Pin the relative clock so `agoText` renders deterministically, then
    /// restore it — matching how the snapshot renderer freezes the clock.
    private func withFixedClock(_ now: Date, _ body: () throws -> Void) rethrows {
        SnapshotMode.setFixtureDate(now)
        defer { SnapshotMode.setFixtureDate(nil) }
        try body()
    }

    func testSublinePresentWhenQuietFieldsSet() throws {
        // now = quiet_since + 3h; newer session started 2h ago.
        let now = Date(timeIntervalSince1970: 1_000_000)
        let quietSince = now.timeIntervalSince1970 - 3 * 3600
        let newerStart = now.timeIntervalSince1970 - 2 * 3600
        let dim = try decodeOutcome(
            """
            {
              "decision_status": "inactive",
              "statement": "This Task has open steps and work has since continued elsewhere.",
              "asserted_by": "inferred",
              "quiet_since": \(quietSince),
              "newer_session_started_at": \(newerStart)
            }
            """
        )
        withFixedClock(now) {
            let summary = receiptOutcomeSummary(dim)
            // The decision word + statement still lead the block.
            XCTAssertTrue(summary.contains("inactive · state inferred"))
            XCTAssertTrue(summary.contains("This Task has open steps"))
            // The factual sub-line renders both timestamps, relative + app idiom.
            XCTAssertTrue(
                summary.contains("Quiet since 3h ago · a newer session started 2h ago"),
                "Expected the quiet sub-line, got: \(summary)"
            )
        }
    }

    func testSublineAbsentWhenQuietFieldsMissing() throws {
        let dim = try decodeOutcome(
            """
            {
              "decision_status": "verified",
              "statement": "Task finished; checks passed.",
              "asserted_by": "machine"
            }
            """
        )
        withFixedClock(Date(timeIntervalSince1970: 1_000_000)) {
            let summary = receiptOutcomeSummary(dim)
            XCTAssertTrue(summary.contains("verified · machine checked"))
            XCTAssertFalse(
                summary.contains("Quiet since"),
                "No quiet fields means no sub-line, got: \(summary)"
            )
        }
    }

    func testSublineAbsentWhenQuietFieldsPresentButNull() throws {
        // The daemon emits present-but-None off the went-quiet keys.
        let dim = try decodeOutcome(
            """
            {
              "decision_status": "in_progress",
              "statement": "Still working.",
              "asserted_by": "inferred",
              "quiet_since": null,
              "newer_session_started_at": null
            }
            """
        )
        withFixedClock(Date(timeIntervalSince1970: 1_000_000)) {
            let summary = receiptOutcomeSummary(dim)
            XCTAssertFalse(summary.contains("Quiet since"), "Null fields must not render, got: \(summary)")
        }
    }

    func testSublineOmitsNewerSessionClauseWhenOnlyQuietSincePresent() throws {
        let now = Date(timeIntervalSince1970: 1_000_000)
        let quietSince = now.timeIntervalSince1970 - 24 * 3600
        let dim = try decodeOutcome(
            """
            {
              "decision_status": "mostly_done",
              "statement": "A step finished; work moved on elsewhere.",
              "asserted_by": "inferred",
              "quiet_since": \(quietSince)
            }
            """
        )
        withFixedClock(now) {
            let summary = receiptOutcomeSummary(dim)
            XCTAssertTrue(summary.contains("Quiet since 1d ago"), "got: \(summary)")
            XCTAssertFalse(summary.contains("a newer session started"), "got: \(summary)")
        }
    }
}
