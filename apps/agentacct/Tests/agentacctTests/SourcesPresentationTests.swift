import XCTest
@testable import agentacct

final class SourcesPresentationTests: XCTestCase {
    func testCurrentErrorTakesPriorityOverRetainedSnapshot() throws {
        let state = SourcesPresentationState.resolve(
            snapshot: try ingestionSnapshot(),
            error: "source health fetch failed: connection reset"
        )

        guard case .unavailable(let message, let hasRetainedSnapshot) = state else {
            return XCTFail("A current fetch error must not render a retained snapshot as live")
        }
        XCTAssertEqual(message, "source health fetch failed: connection reset")
        XCTAssertTrue(hasRetainedSnapshot)
    }

    func testSnapshotRendersConnectedOnlyWithoutCurrentError() throws {
        let state = SourcesPresentationState.resolve(
            snapshot: try ingestionSnapshot(),
            error: nil
        )

        guard case .connected(let snapshot) = state else {
            return XCTFail("A current snapshot without an error should render as connected")
        }
        XCTAssertEqual(snapshot.state, "healthy")
    }

    func testErrorWithoutSnapshotIsUnavailableWithoutRetainedData() {
        let state = SourcesPresentationState.resolve(
            snapshot: nil,
            error: "daemon not running"
        )

        guard case .unavailable(let message, let hasRetainedSnapshot) = state else {
            return XCTFail("A fetch error should render as unavailable")
        }
        XCTAssertEqual(message, "daemon not running")
        XCTAssertFalse(hasRetainedSnapshot)
    }

    func testMissingSnapshotAndErrorIsLoading() {
        let state = SourcesPresentationState.resolve(snapshot: nil, error: nil)

        guard case .loading = state else {
            return XCTFail("The initial state should remain loading")
        }
    }

    private func ingestionSnapshot() throws -> V1IngestionSnapshot {
        let data = Data(
            #"{"state":"healthy","last_success_at":1787590000,"sources":[],"watcher":{"state":"running"},"issues":[]}"#.utf8
        )
        return try JSONDecoder().decode(V1IngestionSnapshot.self, from: data)
    }
}
