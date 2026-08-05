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
                selection.sessionId = dashboard.sessions.first?.id

                for pane in MainPane.allCases {
                    selection.pane = pane
                    let window = MainWindow()
                        .environmentObject(glance)
                        .environmentObject(dashboard)
                        .environmentObject(selection)
                        .frame(width: 1120, height: 680)
                    try render(window, to: out.appendingPathComponent("window-\(pane.rawValue.lowercased()).png"))
                }

                let menu = MenuContent()
                    .environmentObject(glance)
                    .environmentObject(dashboard)
                    .environmentObject(selection)
                    .background(.regularMaterial)
                    .frame(width: 360)
                    .preferredColorScheme(.dark)
                try render(menu, to: out.appendingPathComponent("menu.png"))
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
