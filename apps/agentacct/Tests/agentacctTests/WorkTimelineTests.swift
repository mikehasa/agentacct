import XCTest
@testable import agentacct

final class WorkTimelineTests: XCTestCase {
    func testCachedPendingIDsFollowReviewRestoreAndLiveTransitions() {
        let original = WorkTimelineProjection(records: [record("original", time: 10)])
        let arrived = WorkTimelineProjection(records: [record("new", time: 20)])
        var feed = WorkTimelineFeed()
        feed.ingest(original, following: false)
        XCTAssertTrue(feed.pendingIDs.isEmpty)
        feed.ingest(arrived, following: false)
        XCTAssertEqual(feed.pendingIDs, ["original", "new"])
        feed.reviewArrivals()
        XCTAssertTrue(feed.pendingIDs.isEmpty)
        XCTAssertEqual(feed.removedArrivalCount, 1)
        feed.restoreHistory()
        XCTAssertEqual(feed.pendingIDs, ["original", "new"])
        feed.ingest(arrived, following: true)
        XCTAssertTrue(feed.pendingIDs.isEmpty)
        feed.ingest(.empty, following: false)
        XCTAssertEqual(feed.pendingIDs, ["new"])
        feed.reveal()
        XCTAssertTrue(feed.pendingIDs.isEmpty)
    }

    func testSnapshotBoundsIgnoreDiscardedDuplicates() {
        var first = record("first", time: 10)
        first.end = 30
        first.eventID = "same-event"
        var duplicate = first
        duplicate.end = 1_000
        let projection = WorkTimelineProjection(records: [first, duplicate, record("later-start", time: 20)])
        XCTAssertEqual(projection.interval, .init(lower: 9, upper: 31))
        XCTAssertEqual(projection.newestRecord?.id, "first")
        XCTAssertNil(WorkTimelineProjection.empty.interval)
        XCTAssertNil(WorkTimelineProjection.empty.newestRecord)
    }

    func testChronologyUsesSourceTimeWithStableTiesAndUndatedOutsideAxis() {
        let projection = WorkTimelineProjection(records: [
            record("undated"), record("later", time: 30), record("b", time: 10), record("a", time: 10),
        ])
        XCTAssertEqual(projection.records.map(\.id), ["a", "b", "later", "undated"])
        XCTAssertEqual(projection.interval, WorkTimelineInterval(lower: 9, upper: 31))
        XCTAssertTrue(WorkTimelineInterval(lower: 20, upper: 25).contains(record("undated")))
        XCTAssertFalse(WorkTimelineInterval(lower: 20, upper: 25).contains(record("a", time: 10)))
    }

    func testDedupRequiresImmutableEventIdentity() {
        var first = record("first", time: 10)
        first.eventID = "event-1"
        var duplicate = first
        duplicate.id = "duplicate"
        let anonymousA = record("anonymous-a", time: 10)
        let anonymousB = record("anonymous-b", time: 10)
        let projection = WorkTimelineProjection(records: [first, duplicate, anonymousA, anonymousB])
        XCTAssertEqual(projection.records.count, 3)
        XCTAssertEqual(projection.records.filter { $0.eventID == "event-1" }.count, 1)
        XCTAssertEqual(projection.records.filter { $0.eventID == nil }.count, 2)
    }

    func testLaterPassDoesNotClearUnrelatedFailure() {
        var failure = record("failed", time: 10)
        failure.result = "failed"
        failure.resultTone = "failure"
        failure.eventID = "failure-event"
        var passed = record("passed", time: 20)
        passed.result = "passed"
        passed.eventID = "pass-event"
        XCTAssertTrue(failure.isCurrentFailure)
        failure.superseded = true
        failure.supersededBy = "pass-event"
        XCTAssertFalse(failure.isCurrentFailure)
        XCTAssertEqual(failure.result, "failed")
    }

    func testHeldFeedPreservesSelectedPayloadAndCountsRevisionsOnce() {
        let first = record("first", time: 20)
        var feed = WorkTimelineFeed()
        feed.ingest(WorkTimelineProjection(records: [first]), following: true)
        var revised = first
        revised.summary = "New detail"
        let late = record("late-arrival", time: 10)
        let next = WorkTimelineProjection(records: [revised, late])
        feed.ingest(next, following: false)
        feed.ingest(next, following: false)
        XCTAssertEqual(feed.pendingIDs, ["first", "late-arrival"])
        XCTAssertNil(feed.visible.records.first?.summary)
        XCTAssertEqual(feed.visible.records.count, 1)
        feed.reveal()
        XCTAssertTrue(feed.pendingIDs.isEmpty)
        XCTAssertEqual(feed.visible.records.map(\.id), ["late-arrival", "first"])
        XCTAssertEqual(feed.visible.records.last?.summary, "New detail")
    }

    func testRemovedRecordRemainsHeldUntilExplicitReveal() {
        var feed = WorkTimelineFeed()
        feed.ingest(WorkTimelineProjection(records: [record("saved", time: 10)]), following: true)
        feed.ingest(.empty, following: false)
        XCTAssertEqual(feed.visible.records.map(\.id), ["saved"])
        XCTAssertEqual(feed.pendingIDs, ["saved"])
        feed.reveal()
        XCTAssertTrue(feed.visible.records.isEmpty)
    }

    func testNestedArrivalInspectionKeepsOriginalHistoryBookmark() {
        var state = WorkTimelineNavigation()
        state.view.selectedID = "original"
        state.view.query = "test.swift"
        state.view.file = "Sources/test.swift"
        state.view.anchorID = "anchor"
        state.view.interval = WorkTimelineInterval(lower: 10, upper: 20)
        let original = state.view
        state.beginArrivals()
        state.view.selectedID = "arrival-a"
        state.beginArrivals()
        state.view.selectedID = "arrival-b"
        XCTAssertEqual(state.history, original)
        state.returnToHistory()
        XCTAssertEqual(state.view, original)
        XCTAssertNil(state.history)
        XCTAssertFalse(state.following)
    }

    @MainActor func testPreferencesRemainTaskScopedAndDoNotPersistEvidence() throws {
        let suite = "WorkTimelineTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        var state = WorkTimelineNavigation()
        state.view.selectedID = "selected"
        state.view.query = "query"
        state.beginArrivals()
        WorkTimelinePreferences.save(state, taskID: "task-a", defaults: defaults)
        XCTAssertEqual(WorkTimelinePreferences.load(taskID: "task-a", defaults: defaults), state)
        XCTAssertEqual(WorkTimelinePreferences.load(taskID: "task-b", defaults: defaults), WorkTimelineNavigation())
        let data = try XCTUnwrap(defaults.data(forKey: WorkTimelinePreferences.key("task-a")))
        let json = try XCTUnwrap(String(data: data, encoding: .utf8))
        XCTAssertFalse(json.contains("records"))
    }

    func testArrivalReviewAndReturnRestoreHeldEvidenceIncludingRemovedSelection() {
        let original = record("selected", time: 10)
        var feed = WorkTimelineFeed()
        feed.ingest(.init(records: [original]), following: false)
        feed.ingest(.init(records: [record("late-arrival", time: 5)]), following: false)
        feed.reviewArrivals()
        XCTAssertEqual(feed.arrivalIDs, ["selected", "late-arrival"])
        XCTAssertEqual(feed.removedArrivalCount, 1)
        XCTAssertEqual(feed.visible.records.map(\.id), ["late-arrival"])
        feed.restoreHistory()
        XCTAssertEqual(feed.visible.records, [original])
        XCTAssertEqual(feed.pendingIDs, ["selected", "late-arrival"])
        XCTAssertNil(feed.historySnapshot)
    }

    func testLiveTargetUsesLatestUpdateOfAnEarlierSection() {
        var earlier = record("ongoing", time: 10); earlier.end = 50
        let recentStart = record("recent-check", time: 40)
        XCTAssertEqual(WorkTimelineProjection(records: [earlier, recentStart]).newestRecord?.id, "ongoing")
    }

    private func record(_ id: String, time: Double? = nil) -> WorkTimelineRecord {
        WorkTimelineRecord(id: id, laneID: "session", laneTitle: "Session", lineage: "Root", kind: .check,
                           title: "Check", start: time)
    }

}
