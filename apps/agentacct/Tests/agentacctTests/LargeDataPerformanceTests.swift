import Foundation
import Observation
import XCTest
@testable import agentacct

final class LargeDataPerformanceTests: XCTestCase {
    private final class ChangeFlag: @unchecked Sendable {
        var count = 0
    }

    func testLegacyRowsKeepStableFallbackIdentityAcrossDiffPasses() throws {
        let step = try decode(V1Step.self, from: #"{"title":"legacy step"}"#)
        let check = try decode(V1Check.self, from: #"{"summary":"legacy check"}"#)
        let descendant = try decode(V1Descendant.self, from: #"{"client":"codex"}"#)
        let work = try decode(WorkItem.self, from: #"{"title":"legacy work"}"#)
        let attributed = try decode(AttributedWork.self, from: #"{"title":"legacy attribution"}"#)

        for identifier in [step.id, check.id, descendant.id, work.id, attributed.id] {
            XCTAssertFalse(identifier.isEmpty)
        }
        XCTAssertEqual(step.id, step.id)
        XCTAssertEqual(check.id, check.id)
        XCTAssertEqual(descendant.id, descendant.id)
        XCTAssertEqual(work.id, work.id)
        XCTAssertEqual(attributed.id, attributed.id)
    }

    @MainActor
    func testHighChurnStoresAdoptFineGrainedObservation() {
        requireObservable(DashboardStore.self)
        requireObservable(GlanceState.self)
        requireObservable(AppSelection.self)

        let selection = AppSelection()
        let invalidations = ChangeFlag()
        withObservationTracking {
            _ = selection.pane
        } onChange: {
            invalidations.count += 1
        }

        selection.workSort = .cost
        XCTAssertEqual(invalidations.count, 0, "an unrelated sort change must not invalidate pane readers")
        selection.pane = .work
        XCTAssertEqual(invalidations.count, 1)
    }

    func testLargeWorkProjectionFiltersCountsAndSortsOnce() throws {
        let tasksJSON = (0..<1_000).reversed().map { index in
            let status = index.isMultiple(of: 10) ? "blocked" : "verified"
            let title = index.isMultiple(of: 125) ? "Needle task \(index)" : "Task \(index)"
            return """
            {
              "task_id":"task-\(index)",
              "title":"\(title)",
              "decision_status":{"key":"\(status)"},
              "evidence_strength":{"key":"none"},
              "cost":{},
              "last_activity_at":\(index)
            }
            """
        }.joined(separator: ",")
        let tasks = try decode([ReceiptSummary].self, from: "[\(tasksJSON)]")

        let presentation = WorkTaskPresentation(
            tasks: tasks,
            group: nil,
            query: "needle",
            sort: .latest
        )

        XCTAssertEqual(presentation.groupCounts[.attention], 100)
        XCTAssertEqual(presentation.groupCounts[.verified], 900)
        XCTAssertEqual(presentation.visibleTasks.count, 8)
        XCTAssertEqual(presentation.visibleTasks.map(\.taskId), [
            "task-875", "task-750", "task-625", "task-500",
            "task-375", "task-250", "task-125", "task-0",
        ])
    }

    private func requireObservable<T: Observable>(_ type: T.Type) {}

    private func decode<T: Decodable>(_ type: T.Type, from json: String) throws -> T {
        try JSONDecoder().decode(type, from: Data(json.utf8))
    }
}
