import Foundation
import XCTest
@testable import agentacct

final class DashboardOverviewTests: XCTestCase {
    func testQueueKeepsServerOrderAndWholeQueueCount() throws {
        let payload = try payload(ids: ["third", "first", "second", "fourth"], total: 9)
        let queue = DashboardAttentionQueue(payload: payload, error: nil, projection: nil)
        XCTAssertEqual(queue.state, .items)
        XCTAssertEqual(queue.items.map(\.id), ["third", "first", "second"])
        XCTAssertEqual(queue.total, 9)
        XCTAssertEqual(queue.detail, "Showing 3 of 9 recorded issues")
        XCTAssertNil(queue.items.first?.nextStep, "The preview must not invent a recovery step")
    }

    func testFailedRefreshRetainsPreviouslyValidRowsAndError() throws {
        let queue = DashboardAttentionQueue(
            payload: try payload(ids: ["saved"], total: 1),
            error: "Refresh failed", projection: .init(state: "error", builtAt: 100, generation: "g1")
        )
        XCTAssertEqual(queue.state, .items)
        XCTAssertEqual(queue.items.map(\.id), ["saved"])
        XCTAssertEqual(queue.error, "Refresh failed")
    }

    func testPrivacyInvalidationSuppressesOldQueue() throws {
        let queue = DashboardAttentionQueue(
            payload: try payload(ids: ["removed"], total: 1), error: nil,
            projection: .init(state: "pending", available: false, builtAt: nil, generation: nil)
        )
        XCTAssertEqual(queue.state, .preparing)
        XCTAssertTrue(queue.items.isEmpty)
        XCTAssertNil(queue.total)
    }

    func testFirstBuildAndFailureNeverBecomeAnEmptyQueue() {
        let pending = DashboardAttentionQueue(payload: nil, error: nil, projection: .pending)
        XCTAssertEqual(pending.state, .preparing)
        XCTAssertNil(pending.total)
        let loading = DashboardAttentionQueue(payload: nil, error: nil, projection: nil)
        XCTAssertEqual(loading.state, .loading)
        let failed = DashboardAttentionQueue(payload: nil, error: "Unavailable", projection: nil)
        XCTAssertEqual(failed.state, .unavailable)
    }

    func testStaleEmptyQueueIsQualifiedAndBadEnvelopeStaysUnknown() throws {
        let empty = try payload(ids: [], total: 0)
        let stale = DashboardAttentionQueue(payload: empty, error: nil,
                                            projection: .init(state: "updating", builtAt: 100, generation: "old"))
        XCTAssertEqual(stale.detail, "No recorded issues in this saved snapshot.")
        let failed = DashboardAttentionQueue(payload: empty, error: "Offline", projection: nil)
        XCTAssertEqual(failed.detail, stale.detail)
        let inconsistent = DashboardAttentionQueue(payload: try payload(ids: ["duplicate", "duplicate"], total: 2), error: nil, projection: nil)
        XCTAssertEqual(inconsistent.state, .inconsistent)
        XCTAssertTrue(inconsistent.items.isEmpty)
        XCTAssertNil(inconsistent.total)
    }

    func testRecentWorkKeepsTaskProjectAndOutcomeSeparate() throws {
        let task = try task(id: "active")
        let work = DashboardWorkItem(task: task)
        XCTAssertEqual(work.project, "agentacct")
        XCTAssertEqual(work.client, "codex")
        XCTAssertEqual(work.outcome, "In progress")
        XCTAssertEqual(work.cost, "—")
        XCTAssertEqual(DashboardRecentWorkScope.text(visible: 5, total: 42), "Showing 5 of 42 recorded tasks")
        XCTAssertEqual(DashboardRecentWorkScope.text(visible: 5, total: nil), "5 recent tasks shown")
        XCTAssertEqual(DashboardRecentWorkScope.text(visible: 5, total: 2), "5 recent tasks shown")
    }

    func testRecentActivitySortsByRecordedTimeAndPreservesTies() throws {
        let tasks = try [
            task(id: "unknown"), task(id: "older", activity: 100),
            task(id: "newer", activity: 300), task(id: "same-time", activity: 300),
            task(id: "invalid", activity: -10)
        ]
        XCTAssertEqual(DashboardRecentActivity.items(tasks).map(\.id), ["newer", "same-time", "older", "unknown", "invalid"])
        XCTAssertEqual(DashboardRecentActivity.items(tasks, limit: 2).map(\.id), ["newer", "same-time"])
        XCTAssertTrue(DashboardRecentActivity.items(tasks, limit: 0).isEmpty)
    }

    func testRecentSessionStatusIsBoundedAndNeverClaimsLiveness() {
        let active = RecentSession(client: "codex", sessionId: "active", title: nil,
                                   status: "started", lastActivityAt: 100, planPct: nil)
        let unknown = RecentSession(client: "hermes", sessionId: "unknown", title: nil,
                                    status: "future-status", lastActivityAt: nil, planPct: nil)
        let status = DashboardSessionActivity(sessions: [active, unknown], availability: .connected)
        XCTAssertEqual(status.value, "1 in progress")
        XCTAssertTrue(status.scope.contains("Recorded status · 2 recent sessions"))
        XCTAssertTrue(status.scope.contains("1 with unknown status"))
        XCTAssertTrue(status.scope.contains("last activity"))
        XCTAssertFalse(status.scope.contains("Working now"))
        XCTAssertEqual(DashboardSessionActivity(sessions: [unknown], availability: .connected).value, "Status unknown")
        XCTAssertEqual(DashboardSessionActivity(sessions: [], availability: .connected).value, "None recorded")
        XCTAssertEqual(DashboardSessionActivity(sessions: [active], availability: .unavailable("Offline")).value, "Unavailable")
    }

    private func payload(ids: [String], total: Int) throws -> V1AttentionPayload {
        V1AttentionPayload(
            schema: "agentacct.v1-attention.v1", items: try ids.map { try task(id: $0) }, total: total,
            counts: .init(failedCheck: total, failedStep: 0, blocker: 0),
            snapshot: nil, offset: 0, limit: 5, truncated: total > ids.count
        )
    }

    private func task(id: String, activity: Double? = nil) throws -> ReceiptSummary {
        var json: [String: Any] = [
            "task_id": id, "title": "Recorded task \(id)", "project": "agentacct",
            "decision_status": ["key": "in_progress"],
            "evidence_strength": ["key": "unchecked"], "cost": [String: Any](),
            "primary_root": ["client": "codex", "client_session_id": "session-\(id)"],
            "attention": ["kind": "failed_check", "summary": "Recorded failure"]
        ]
        if let activity { json["last_activity_at"] = activity }
        return try JSONDecoder().decode(ReceiptSummary.self, from: JSONSerialization.data(withJSONObject: json))
    }
}
