import XCTest
@testable import agentacct

/// Words and orders the Python vocabulary owns reach the app through payload
/// fields; these pin that the app renders them instead of a Swift copy.
final class VocabularyPayloadRenderingTests: XCTestCase {
    private func decode<T: Decodable>(_ type: T.Type, from json: String) throws -> T {
        try JSONDecoder().decode(T.self, from: Data(json.utf8))
    }

    private func row(_ id: String, order: Int?, at: Double, group: String) throws -> ReceiptSummary {
        let orderField = order.map { "\"attention_order\": \($0)," } ?? ""
        return try decode(ReceiptSummary.self, from: """
        {\(orderField) "task_id": "\(id)", "group_key": "\(group)", "last_activity_at": \(at),
         "decision_status": {"key": "reported"}, "evidence_strength": {"key": "unchecked"}, "cost": {}}
        """)
    }

    func testAttentionSortFollowsTheReducerOrderThenRecencyOnEveryFilter() throws {
        let rows = [
            try row("old-finding", order: 0, at: 10, group: "attention"),
            try row("new-blocker", order: 1, at: 50, group: "attention"),
            try row("done", order: nil, at: 99, group: "reported"),
            try row("new-finding", order: 0, at: 40, group: "attention"),
            try row("not-run", order: 2, at: 60, group: "attention"),
        ]
        let all = sortedReceipts(rows, by: .attention).map(\.taskId)
        XCTAssertEqual(all, ["new-finding", "old-finding", "new-blocker", "not-run", "done"])
        // The Attention filter is a subsequence of the same order.
        let attention = sortedReceipts(rows.filter { WorkGroup.forTask($0) == .attention }, by: .attention)
            .map(\.taskId)
        XCTAssertEqual(attention, all.filter { $0 != "done" })
        // The rule shown is the payload's sort text.
        let queue = AttentionQueueCopy(noun: "Attention", countText: "4 in Attention", openAction: "Open Attention",
                                       sortText: "failed checks and steps, then blockers, then checks that could not run, then most recent")
        XCTAssertEqual(WorkSort.attention.footerText(queue: queue), queue.sortText)
        XCTAssertEqual(WorkSort.attention.footerText(queue: nil), "attention order not reported")
    }

    func testDecisionLegendAndGroupsDecodeFromTheTasksPayload() throws {
        let payload = try decode(ReceiptTasksPayload.self, from: """
        {"schema": "agentacct.receipt.v1", "tasks": [],
         "decision_legend": {
           "decisions": [{"key": "blocked", "label": "Blocked", "definition": "The agent recorded a blocker for this Task.", "group_key": "attention"}],
           "groups": [{"key": "stopped", "label": "Stopped", "definition": "Work that stopped without finishing."}]
         },
         "queue": {"noun": "Attention", "count_text": "0 in Attention", "open_action": "Open Attention", "sort_text": "s"},
         "field_labels": {"task": "Task", "actions": "Tool calls", "weekly_plan": "Weekly plan"}}
        """)
        let legend = try XCTUnwrap(payload.decisionLegend)
        XCTAssertEqual(legend.decisions.first?.groupKey, "attention")
        XCTAssertEqual(WorkGroup.stopped.label(in: legend), "Stopped")
        XCTAssertEqual(payload.queue?.countText, "0 in Attention")
        XCTAssertEqual(payload.fieldLabels?.actionsLabel, "Tool calls")
        XCTAssertEqual(payload.fieldLabels?.weeklyPlanLabel, "Weekly plan")
    }

    func testGapItemsPrintTheirDimensionLabel() throws {
        let item = try decode(ReceiptGapItem.self, from: """
        {"dimension": "actors", "dimension_label": "Agents", "reason": "No model was observed."}
        """)
        XCTAssertEqual(item.label, "Agents")
        let legacy = try decode(ReceiptGapItem.self, from: #"{"dimension": "weekly_plan", "reason": "r"}"#)
        XCTAssertNotEqual(legacy.label, "weekly_plan")
    }

    func testDispositionEffectsIncludeReopen() throws {
        let effects = try decode(ReceiptDispositionEffects.self, from: """
        {"reviewed": "Leaves Attention; the badge stays Finding until resolved.",
         "resolved": "Leaves Attention and records your resolution; the badge becomes Finding resolved.",
         "reopen": "Returns to Attention with its original badge."}
        """)
        XCTAssertEqual(effects.reopen, "Returns to Attention with its original badge.")
    }
}
