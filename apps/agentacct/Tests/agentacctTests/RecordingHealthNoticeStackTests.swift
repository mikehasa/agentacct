import AppKit
import SwiftUI
import XCTest
@testable import agentacct

final class RecordingHealthNoticeStackTests: XCTestCase {
    @MainActor
    func testMountedRecoveryBannerExpiresWithoutAnotherHealthRefresh() async throws {
        let coordinator = RecordingHealthCoordinator()
        let cause = RecordingHealthCause(
            id: "ingestion:source_scan_failed:claude-code", scope: .ingestion,
            title: "Source Scan Failed", detail: "Synthetic scan failure",
            tone: .caution, action: .sources, affectedSources: ["claude-code"],
            recoveryDetail: "The synthetic source recovered."
        )
        let failed = RecordingHealthSnapshot(
            title: "Recording needs review", tone: .caution, dimensions: [], clients: [],
            causes: [cause], resolutionScopes: [.ingestion]
        )
        let recovered = RecordingHealthSnapshot(
            title: "Recorder reachable", tone: .neutral, dimensions: [], clients: [],
            causes: [], resolutionScopes: [.ingestion]
        )
        // Keep the production ten-second duration while leaving about half a
        // second for the actual mounted view's task to expire this recovery.
        let recoveredAt = Date().addingTimeInterval(-9.5)
        coordinator.update(failed, now: recoveredAt.addingTimeInterval(-1))
        coordinator.update(recovered, now: recoveredAt)
        let recoveryID = try XCTUnwrap(coordinator.visibleNotices.first?.id)
        XCTAssertTrue(try XCTUnwrap(coordinator.visibleNotices.first).isRecovered)

        let expired = expectation(description: "the mounted notice expires without a health refresh")
        let root = RecordingHealthNoticeStack(
            coordinator: coordinator,
            onSetup: { XCTFail("expiry must not open setup") },
            onSources: { XCTFail("expiry must not open diagnostics") },
            onRefresh: { XCTFail("expiry must not request another health snapshot") }
        )
        .onChange(of: coordinator.visibleNotices.isEmpty) { _, isEmpty in
            if isEmpty { expired.fulfill() }
        }
        let window = NSWindow(
            contentRect: NSRect(x: -6000, y: -6000, width: 440, height: 240),
            styleMask: [.borderless], backing: .buffered, defer: false
        )
        window.isReleasedWhenClosed = false
        let hosting = NSHostingView(rootView: root)
        hosting.sizingOptions = []
        window.contentView = hosting
        window.orderFrontRegardless()
        defer { window.close() }

        // No coordinator mutation or refresh follows mounting: only the real
        // RecordingHealthNoticeStack timer can fulfill this expectation.
        await fulfillment(of: [expired], timeout: 5)
        XCTAssertTrue(coordinator.visibleNotices.isEmpty)
        XCTAssertNil(coordinator.nextRecoveryDismissalAt)
        XCTAssertEqual(coordinator.recentRecoveries.map(\.id), [recoveryID])
        XCTAssertEqual(coordinator.recentRecoveries.first?.recoveredAt, recoveredAt)
    }
}
