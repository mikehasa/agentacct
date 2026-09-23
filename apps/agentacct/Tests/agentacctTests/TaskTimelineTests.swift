import XCTest
@testable import agentacct

final class TaskTimelineTests: XCTestCase {
    func testHistoryPagesAreAssembledBeforePublishing() async throws {
        var requests: [String?] = []
        let snapshot = try await TaskTimelineLoader.load(taskID: "task") { cursor in
            requests.append(cursor)
            return try self.page(ids: cursor == nil ? ["new"] : ["old"], offset: cursor == nil ? 0 : 1,
                                 total: 2, cursor: cursor == nil ? "snapshot:1" : nil)
        }
        XCTAssertEqual(requests.count, 2)
        XCTAssertEqual(snapshot.events.compactMap(\.id), ["new", "old"])
        XCTAssertEqual(snapshot.shown, 2)
        XCTAssertEqual(snapshot.offset, 0)
        XCTAssertFalse(snapshot.truncated)
        XCTAssertNil(snapshot.nextCursor)
    }

    func testUnchangedSnapshotDoesNotDownloadOlderPages() async throws {
        let previous = try page(ids: ["a", "b"], total: 2)
        var calls = 0
        let snapshot = try await TaskTimelineLoader.load(taskID: "task", previous: previous) { _ in
            calls += 1
            return try self.page(ids: ["b"], total: 2, cursor: "snapshot:1")
        }
        XCTAssertEqual(snapshot, previous)
        XCTAssertEqual(calls, 1)
    }

    func testExpiredCursorRestartsWithANewCoherentSnapshot() async throws {
        var calls = 0
        let snapshot = try await TaskTimelineLoader.load(taskID: "task") { _ in
            calls += 1
            if calls == 1 { return try self.page(ids: ["old"], total: 2, cursor: "snapshot:1") }
            if calls == 2 { throw GlanceClientError.http(409) }
            return try self.page(ids: ["replacement"], total: 1, snapshot: "fresh")
        }
        XCTAssertEqual(calls, 3)
        XCTAssertEqual(snapshot.events.compactMap(\.id), ["replacement"])
    }

    func testExpiredHistoryHasABoundedRetryCount() async {
        var calls = 0
        do {
            _ = try await TaskTimelineLoader.load(taskID: "task") { _ in
                calls += 1
                throw GlanceClientError.http(409)
            }
            XCTFail("Expected expiration error")
        } catch { XCTAssertEqual(calls, 3) }
    }

    func testMalformedPagesNeverPublishPartialHistory() async throws {
        let invalid = [
            try page(ids: ["second"], offset: 1, total: 2, task: "other"),
            try page(ids: ["second"], offset: 1, total: 2, snapshot: "changed"),
            try page(ids: ["second"], offset: 0, total: 2),
            try page(ids: ["first"], offset: 1, total: 2), // repeated identity
            try page(ids: [], offset: 1, total: 2, cursor: "snapshot:1"), // no progress
            try page(ids: ["second"], offset: 1, total: 3), // incomplete final page
        ]
        for second in invalid {
            var calls = 0
            do {
                _ = try await TaskTimelineLoader.load(taskID: "task") { _ in
                    calls += 1
                    return calls == 1 ? try self.page(ids: ["first"], total: 2, cursor: "snapshot:1") : second
                }
                XCTFail("Expected inconsistent history")
            } catch { XCTAssertTrue(error is TaskTimelineError) }
        }
    }

    func testCancellationStopsPaginationWithoutRetrying() async {
        var calls = 0
        do {
            _ = try await TaskTimelineLoader.load(taskID: "task") { _ in
                calls += 1
                throw CancellationError()
            }
            XCTFail("Expected cancellation")
        } catch { XCTAssertTrue(error is CancellationError); XCTAssertEqual(calls, 1) }
    }

    func testCanonicalFieldsPreserveSourceAndRelationships() throws {
        let raw: [String: Any] = ["id": "event:test", "event_id": "test", "kind": "check", "title": "Tests",
            "status": "failed", "occurred_at": 10, "source_label": "Agent-reported check", "session_key": "codex::root",
            "session_title": "Root", "lineage": "Root session", "section_record_ids": ["section-a", "section-b"],
            "superseded": true, "superseded_by_event_id": "later", "artifact_path": "/private/log",
            "artifact_path_redacted": true, "artifact_url": "https://example.test/private", "artifact_url_redacted": true,
            "exit_code": 1, "resolution": "Reported correction", "resolution_scope": "partial"]
        let event = try JSONDecoder().decode(TaskTimelineEvent.self, from: JSONSerialization.data(withJSONObject: raw))
        let record = try XCTUnwrap(event.record(taskID: "task"))
        XCTAssertEqual(record.id, "event:test")
        XCTAssertEqual(record.result, "failed")
        XCTAssertFalse(record.isCurrentFailure)
        XCTAssertEqual(record.sectionRecordIDs, ["section-a", "section-b"])
        XCTAssertEqual(record.source, "Agent-reported check")
        XCTAssertEqual(record.supersededBy, "later")
        XCTAssertEqual(record.exitCode, 1)
        XCTAssertNil(record.artifactPath)
        XCTAssertNil(record.artifactURL)
        XCTAssertEqual(record.resolutionScope, "partial")
    }

    func testOldReceiptTimelineDoesNotInventEventIdentity() throws {
        let raw: [String: Any] = ["events": [["kind": "check", "title": "Tests", "status": "passed", "occurred_at": 10]],
                                  "total": 1, "shown": 1, "truncated": false]
        let timeline = try JSONDecoder().decode(TaskTimelinePage.self, from: JSONSerialization.data(withJSONObject: raw))
        let projection = timeline.projection(taskID: "task")
        XCTAssertTrue(projection.records.isEmpty)
        XCTAssertEqual(projection.notices.count, 1)
    }

    func testOfflineCacheAcceptsOnlyTheAssembledTimeline() {
        XCTAssertTrue(SavedWorkSnapshot.accepts("/v1/task-timeline?task=task%26id"))
        XCTAssertFalse(SavedWorkSnapshot.accepts("/v1/task-timeline?task=task&limit=500"))
        XCTAssertFalse(SavedWorkSnapshot.accepts("/v1/task-timeline?task=task&cursor=abc"))
    }

    func testProjectionGenerationsCannotBeMixedBetweenTimelinePages() async throws {
        var calls = 0
        do {
            _ = try await TaskTimelineLoader.load(taskID: "task") { _ in
                calls += 1
                var page = try self.page(ids: calls == 1 ? ["new"] : ["old"], offset: calls == 1 ? 0 : 1,
                                         total: 2, cursor: calls == 1 ? "snapshot:1" : nil)
                page.workProjection = .init(state: "current", builtAt: 100, generation: "g\(calls)", error: nil)
                return page
            }
            XCTFail("Mixed generations must not publish a timeline")
        } catch { XCTAssertTrue(error is TaskTimelineError) }
    }

    func testUnchangedTimelineStillUpdatesProjectionFreshness() async throws {
        var previous = try page(ids: ["a"], total: 1)
        previous.workProjection = .init(state: "updating", builtAt: 100, generation: "g1", error: nil)
        let current = WorkProjectionMetadata(state: "current", builtAt: 120, generation: "g2", error: nil)
        let result = try await TaskTimelineLoader.load(taskID: "task", previous: previous) { _ in
            var page = try self.page(ids: ["a"], total: 1)
            page.workProjection = current
            return page
        }
        XCTAssertEqual(result.events, previous.events)
        XCTAssertEqual(result.workProjection, current)
    }

    private func page(ids: [String], offset: Int = 0, total: Int, cursor: String? = nil,
                      task: String = "task", snapshot: String = "snapshot") throws -> TaskTimelinePage {
        var raw: [String: Any] = ["schema_version": TaskTimelinePage.schema, "task_id": task,
            "snapshot_id": snapshot, "events": ids.map { ["id": $0, "kind": "check", "title": $0, "status": "passed"] },
            "offset": offset, "shown": ids.count, "total": total, "truncated": cursor != nil]
        raw["next_cursor"] = cursor
        return try JSONDecoder().decode(TaskTimelinePage.self, from: JSONSerialization.data(withJSONObject: raw))
    }
}
