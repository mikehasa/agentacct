import AppKit
import SwiftUI
import XCTest
@testable import agentacct

/// A Task can carry hundreds of sessions. Opening the "N more sessions" fold
/// must build only the rows scrolled into view, never the whole list.
@MainActor
final class WorkSessionsFoldTests: XCTestCase {
    private static let memberCount = 300

    /// Records each session whose row SwiftUI actually built.
    private final class BuiltRows {
        var ids = Set<String>()
    }

    private struct ProbeRow: View {
        let member: ReceiptSessionMember
        let built: BuiltRows

        var body: some View {
            built.ids.insert(member.id)
            return Text(member.title ?? member.id).frame(height: 44)
        }
    }

    private func members() -> [ReceiptSessionMember] {
        (0..<Self.memberCount).map { index in
            ReceiptSessionMember(
                client: "codex",
                clientSessionId: "root-0000:agent-\(index)",
                sessionKind: "subagent",
                role: "subagent",
                title: "Synthetic session \(index)",
                project: nil,
                lastActivityAt: nil
            )
        }
    }

    /// Hosts the production section in a real (offscreen) window's scroll
    /// view, as the record page does, and returns the rows it built.
    private func rowsBuilt(overflowInitiallyExpanded: Bool) -> Set<String> {
        let wasSnapshot = SnapshotMode.enabled
        SnapshotMode.enabled = false  // the live app's lazy path, not the renderer's
        defer { SnapshotMode.enabled = wasSnapshot }

        let built = BuiltRows()
        let root = ScrollView {
            RecordSubagentsSection(
                members: members(),
                overflowInitiallyExpanded: overflowInitiallyExpanded
            ) { ProbeRow(member: $0, built: built) }
        }
        let window = NSWindow(
            contentRect: NSRect(x: -6000, y: -6000, width: 1180, height: 820),
            styleMask: [.borderless], backing: .buffered, defer: false
        )
        let hosting = NSHostingView(rootView: root)
        hosting.sizingOptions = []
        window.contentView = hosting
        window.orderFrontRegardless()
        defer { window.orderOut(nil) }
        for _ in 0..<40 {
            RunLoop.main.run(mode: .default, before: Date(timeIntervalSinceNow: 0.005))
            CATransaction.flush()
            hosting.layoutSubtreeIfNeeded()
        }
        return built.ids
    }

    func testClosedFoldBuildsOnlyThePreviewRows() {
        let built = rowsBuilt(overflowInitiallyExpanded: false)
        XCTAssertEqual(built.count, RecordSubagentsSection<ProbeRow>.previewLimit)
    }

    func testOpenFoldBuildsOnlyTheRowsInView() {
        let built = rowsBuilt(overflowInitiallyExpanded: true)
        XCTAssertGreaterThan(built.count, RecordSubagentsSection<ProbeRow>.previewLimit,
                             "the open fold shows rows beyond the preview")
        XCTAssertLessThan(built.count, Self.memberCount / 3,
                          "opening the fold built \(built.count) of \(Self.memberCount) rows; it must stay lazy")
        XCTAssertTrue(built.contains("codex::root-0000:agent-\(RecordSubagentsSection<ProbeRow>.previewLimit)"),
                      "the first overflow row is the one directly under the fold")
    }

    /// The offscreen renderer cannot lay out lazy containers, so review renders
    /// keep every row. This pins that exception to snapshot mode only.
    func testSnapshotRendererStillBuildsEveryRow() {
        let wasSnapshot = SnapshotMode.enabled
        let wasInteractive = SnapshotMode.interactiveFixture
        defer {
            SnapshotMode.enabled = wasSnapshot
            SnapshotMode.interactiveFixture = wasInteractive
        }
        SnapshotMode.enabled = true
        SnapshotMode.interactiveFixture = false
        let built = BuiltRows()
        let renderer = ImageRenderer(content: RecordSubagentsSection(
            members: members(), overflowInitiallyExpanded: true
        ) { ProbeRow(member: $0, built: built) }.frame(width: 900))
        _ = renderer.nsImage
        XCTAssertEqual(built.ids.count, Self.memberCount)
    }
}
