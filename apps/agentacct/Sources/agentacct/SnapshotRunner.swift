import AppKit
import SwiftUI

// Offscreen renders of the real UI with real daemon data. Development tooling:
// lets the design be SEEN (and reviewed) without driving the live screen.

enum SnapshotRunner {
    static func run(outputDir: String) {
        let out = URL(fileURLWithPath: (outputDir as NSString).expandingTildeInPath)
        try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

        // Pump the main run loop while the MainActor task works — a semaphore
        // wait on the main thread would deadlock the very actor doing the
        // rendering.
        SnapshotMode.enabled = true
        var finished = false
        Task { @MainActor in
            defer { finished = true }
            do {
                let snapshot = try await GlanceClient().fetch()
                let glance = GlanceState(preloaded: snapshot)
                let dashboard = DashboardStore()
                await dashboard.refresh()
                let selection = AppSelection()
                if let first = dashboard.sessions.first {
                    selection.sessionId = first.id
                    // The deep view loads async in the live app; a snapshot
                    // must render the loaded state, not the spinner.
                    await dashboard.fetchDetail(client: first.client, sessionId: first.clientSessionId)
                }
                // The Work pane loads its Task list + Receipt from a SwiftUI
                // `.task`, which ImageRenderer does not run — so preload them here
                // (as with the deep view above), or the Work pane renders empty.
                // Selecting the first Task (server order = recency) shows a full
                // Receipt; make the flagship demo Task the most recent one.
                await dashboard.fetchReceipts()
                if let flagship = dashboard.receiptTasks.first {
                    selection.taskId = flagship.taskId
                    await dashboard.fetchReceipt(taskId: flagship.taskId)
                }
                // The Receipt's "Sessions & steps" drill-down loads each session's
                // steps through a per-row `.task` too — preload the Task's root
                // sessions so those steps render in the snapshot.
                for group in dashboard.receipt?.sessions ?? [] {
                    for member in group.members where member.role == "root" {
                        await dashboard.preloadSession(client: member.client, sessionId: member.clientSessionId)
                    }
                }

                // Light AND dark of every surface: the theme is adaptive, so
                // a design pass must see both. SnapshotScheme pins the Theme
                // tokens; the environment pins the system styles.
                for scheme in [ColorScheme.light, ColorScheme.dark] {
                    SnapshotScheme.override = scheme
                    let suffix = scheme == .dark ? "dark" : "light"

                    for pane in MainPane.allCases {
                        selection.pane = pane
                        // The Work pane shows the Receipt cards AND the steps
                        // drill-down below them; render it taller so both fit.
                        let height: CGFloat = pane == .work ? 1420 : 680
                        let window = MainWindow()
                            .environmentObject(glance)
                            .environmentObject(dashboard)
                            .environmentObject(selection)
                            .frame(width: 1120, height: height)
                            .environment(\.colorScheme, scheme)
                        try render(
                            window,
                            to: out.appendingPathComponent("window-\(pane.rawValue.lowercased())-\(suffix).png")
                        )
                    }

                    let menu = MenuContent()
                        .environmentObject(glance)
                        .environmentObject(dashboard)
                        .environmentObject(selection)
                        .background(scheme == .dark ? Color(white: 0.13) : Color(white: 0.97))
                        .frame(width: 360)
                        .environment(\.colorScheme, scheme)
                    try render(menu, to: out.appendingPathComponent("menu-\(suffix).png"))
                }
                SnapshotScheme.override = nil
                print("snapshots written to \(out.path)")
            } catch {
                FileHandle.standardError.write(Data("snapshot failed: \(error)\n".utf8))
                exit(1)
            }
        }
        while !finished {
            RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.05))
        }
        exit(0)
    }

    @MainActor
    private static func render(_ view: some View, to url: URL) throws {
        let renderer = ImageRenderer(content: view)
        renderer.scale = 2
        guard let cgImage = renderer.cgImage else {
            throw NSError(domain: "snapshot", code: 1, userInfo: [NSLocalizedDescriptionKey: "render produced no image"])
        }
        let rep = NSBitmapImageRep(cgImage: cgImage)
        guard let png = rep.representation(using: .png, properties: [:]) else {
            throw NSError(domain: "snapshot", code: 2, userInfo: [NSLocalizedDescriptionKey: "png encode failed"])
        }
        try png.write(to: url)
    }
}
