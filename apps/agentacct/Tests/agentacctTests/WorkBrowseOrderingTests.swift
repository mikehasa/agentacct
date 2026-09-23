import XCTest
@testable import agentacct

final class WorkBrowseOrderingTests: XCTestCase {
    @MainActor
    func testDefaultBrowseShowsEveryStatusInLatestActivityOrder() throws {
        let rows = try tasks([
            ("old-failure", "failed", 100),
            ("new-report", "reported", 300),
            ("middle-active", "in_progress", 200),
        ])
        let browse = WorkBrowseState()

        XCTAssertNil(browse.group)
        XCTAssertEqual(browse.sort, .latest)
        XCTAssertEqual(browse.visibleTasks(in: rows).map(\.taskId), ["new-report", "middle-active", "old-failure"])
    }

    func testLatestSortsIssuePageByActivityInsteadOfSeverityOrder() throws {
        let rows = try tasks([
            ("failed-old", "failed", 100),
            ("blocked-new", "blocked", 300),
            ("finding-middle", "finding", 200),
        ])
        let payload = V1AttentionPayload(
            schema: "agentacct.v1-attention.v1", items: rows, total: rows.count,
            counts: .init(failedCheck: 1, failedStep: 1, blocker: 1),
            snapshot: nil, offset: 0, limit: 5, truncated: false
        )
        let presentation = WorkTaskPresentation(tasks: [], attention: payload, group: .attention, query: "", sort: .latest)

        XCTAssertEqual(presentation.visibleTasks.map(\.taskId), ["blocked-new", "finding-middle", "failed-old"])
    }

    func testEqualAndUnknownTimesRetainSourceOrderAfterKnownActivity() throws {
        let rows = try tasks([
            ("unknown-first", "reported", nil),
            ("tie-first", "reported", 200),
            ("zero-time", "reported", 0),
            ("older", "reported", 100),
            ("tie-second", "reported", 200),
            ("negative-time", "reported", -1),
            ("unknown-last", "reported", nil),
        ])

        XCTAssertEqual(sortedReceipts(rows, by: .latest).map(\.taskId), [
            "tie-first", "tie-second", "older", "unknown-first", "zero-time", "negative-time", "unknown-last",
        ])
    }

    func testAttentionSortingRemainsAnExplicitOption() throws {
        let rows = try tasks([
            ("new-report", "reported", 300),
            ("old-failure", "failed", 100),
            ("middle-active", "in_progress", 200),
        ])

        XCTAssertEqual(sortedReceipts(rows, by: .attention).map(\.taskId), ["old-failure", "new-report", "middle-active"])
        XCTAssertEqual(sortedReceipts(rows, by: .latest).map(\.taskId), ["new-report", "middle-active", "old-failure"])
    }

    func testProjectSearchMatchesContextAndKeepsLatestOrder() throws {
        let rows = try tasks([
            ("older", "reported", 100), ("unrelated", "reported", 400), ("newer", "in_progress", 300),
        ], projects: ["older": "/Projects/AgentAcct", "newer": "/Projects/AgentAcct", "unrelated": "/Projects/Other"])

        XCTAssertEqual(
            visibleWorkReceipts(rows, query: "  agentacct  ", group: nil, sort: .latest).map(\.taskId),
            ["newer", "older"]
        )
        XCTAssertEqual(
            visibleWorkReceipts(rows, query: "/projects/agentacct", group: .reported, sort: .latest).map(\.taskId),
            ["older"]
        )
    }

    private func tasks(_ values: [(String, String, Double?)], projects: [String: String] = [:]) throws -> [ReceiptSummary] {
        let objects: [[String: Any]] = values.map { id, status, time in
            var object: [String: Any] = [
                "task_id": id, "decision_status": ["key": status],
                "evidence_strength": ["key": "unchecked"], "cost": [String: Any](),
            ]
            if let time { object["last_activity_at"] = time }
            if let project = projects[id] { object["project"] = project }
            return object
        }
        return try JSONDecoder().decode([ReceiptSummary].self, from: JSONSerialization.data(withJSONObject: objects))
    }
}
