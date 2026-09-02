import SwiftUI
import XCTest
@testable import agentacct

/// APP-side rendering of the daemon's honest `inactive` task-status state.
///
/// The daemon is the single source of the decision word: an open Task with
/// nothing recorded finished, where the store demonstrably moved on elsewhere,
/// reads `decision_status.key == "inactive"`, `asserted_by == "inferred"`,
/// `label == "Inactive"`, and a non-completion statement. These tests pin that
/// the app RENDERS that word truthfully and never re-derives the honesty logic:
/// inactive is quiet (its own Stopped bucket, a neutral non-green tint), it is
/// never treated as attention, and it never reads done-ish or verified.
final class InactiveDecisionStateTests: XCTestCase {
    private func decode<Value: Decodable>(_ type: Value.Type, from json: String) throws -> Value {
        try JSONDecoder().decode(type, from: Data(json.utf8))
    }

    /// A daemon-shaped `/v1/tasks` row for an inactive Task: the label and
    /// statement ride the payload exactly as the daemon emits them.
    private func inactiveSummary(
        label: String? = "Inactive",
        checksFailed: Int? = nil
    ) throws -> ReceiptSummary {
        let labelField = label.map { ",\"label\":\"\($0)\"" } ?? ""
        let failedField = checksFailed.map { ",\"checks_failed\":\($0)" } ?? ""
        return try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "went-quiet",
              "title": "Refactor the importer",
              "decision_status": {
                "key": "inactive"\(labelField),
                "statement": "This Task has open steps, nothing recorded as finished, and work has since continued elsewhere in the store — agentacct inferred it went quiet. Not a completion, and not a stated stop.",
                "asserted_by": "inferred"
              },
              "evidence_strength": { "key": "unchecked"\(failedField) },
              "cost": {}
            }
            """
        )
    }

    // MARK: - Lifecycle bucket

    func testInactiveKeyFoldsIntoStoppedBucket() {
        XCTAssertEqual(WorkGroup.forKey("inactive"), .stopped)
    }

    func testInactiveTaskGroupsAsStoppedNotAttention() throws {
        let summary = try inactiveSummary()
        // A pure renderer: the quiet inferred state sits beside ended_open in
        // Stopped, never promoted to the review queue.
        XCTAssertEqual(WorkGroup.forTask(summary), .stopped)
    }

    // MARK: - Attention exclusion

    func testInactiveNeverEntersAttention() {
        XCTAssertFalse(workReceiptNeedsAttention(decisionKey: "inactive", checksFailed: nil))
        XCTAssertFalse(workReceiptNeedsAttention(decisionKey: "inactive", checksFailed: 0))
    }

    // MARK: - Tint (quiet, never green, never done-ish)

    func testInactiveTintIsNeutralAndNeverGreenOrClaimed() {
        let tint = DecisionTintClass.forKey("inactive")
        XCTAssertEqual(tint, .neutral)
        // Never machine-verified green, never the done-ish claimed cobalt.
        XCTAssertNotEqual(tint, .verified)
        XCTAssertNotEqual(tint, .claimed)
        // The rendered decision colour must not be the verified green.
        XCTAssertNotEqual(receiptDecisionTint("inactive"), Theme.green)
    }

    // MARK: - Legend

    func testDecisionLegendCarriesInactiveAsAnInferredNonCompletion() throws {
        let entry = try XCTUnwrap(
            DecisionLegend.entries.first { $0.key == "inactive" },
            "The status legend must define the inactive word."
        )
        XCTAssertEqual(entry.label, "Inactive")
        // The definition states an inference, not a completion.
        XCTAssertTrue(entry.definition.lowercased().contains("inferred"))
        XCTAssertFalse(entry.definition.lowercased().contains("verified"))
        XCTAssertFalse(entry.definition.lowercased().contains("finished successfully"))
    }

    // MARK: - Dashboard label

    func testDashboardOutcomeUsesDaemonLabelForInactive() throws {
        let item = DashboardWorkItem(task: try inactiveSummary())
        XCTAssertEqual(item.outcomeKey, "inactive")
        XCTAssertEqual(item.outcome, "Inactive")
    }

    func testDashboardOutcomeFallbackCapitalizesInactiveWhenDaemonOmitsLabel() throws {
        // Older daemon payload with no explicit label — the fallback still reads
        // "Inactive", never the raw key.
        let item = DashboardWorkItem(task: try inactiveSummary(label: nil))
        XCTAssertEqual(item.outcomeKey, "inactive")
        XCTAssertEqual(item.outcome, "Inactive")
    }

    // MARK: - Work row presentation

    func testWorkRowRendersDaemonInactiveWordWithNoAttentionReason() throws {
        let presentation = WorkReceiptRowPresentation(task: try inactiveSummary())
        XCTAssertEqual(presentation.decisionKey, "inactive")
        XCTAssertEqual(presentation.decisionLabel, "Inactive")
        XCTAssertFalse(presentation.decisionHelp.isEmpty)
        // Quiet: an inactive Task states no coral attention reason.
        XCTAssertNil(presentation.attentionReason)
        XCTAssertFalse(presentation.handedOff)
    }
}
