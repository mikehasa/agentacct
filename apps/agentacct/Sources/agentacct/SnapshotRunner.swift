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
                // refresh() also loads the Task list. Select the newest Task and
                // preload its Receipt because ImageRenderer does not run the
                // Work pane's SwiftUI `.task`.
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
                        // Width-only frame: the canvas grows to the pane's full
                        // content height (ImageRenderer centers an overflowing
                        // fixed frame, which would clip both ends).
                        let window = MainWindow()
                            .environmentObject(glance)
                            .environmentObject(dashboard)
                            .environmentObject(selection)
                            .frame(width: 1120, alignment: .top)
                            .environment(\.colorScheme, scheme)
                        try SnapshotImageWriter.render(
                            window,
                            to: out.appendingPathComponent("window-\(pane.rawValue.lowercased())-\(suffix).png")
                        )
                    }

                    // The Work surface has a second state: the receipts TABLE
                    // (no Task selected). Render it too, then restore the
                    // record selection for the other scheme's pass.
                    let recordTaskId = selection.taskId
                    selection.pane = .work
                    selection.taskId = nil
                    let tableWindow = MainWindow()
                        .environmentObject(glance)
                        .environmentObject(dashboard)
                        .environmentObject(selection)
                        .frame(width: 1120, alignment: .top)
                        .environment(\.colorScheme, scheme)
                    try SnapshotImageWriter.render(
                        tableWindow,
                        to: out.appendingPathComponent("window-work-table-\(suffix).png")
                    )
                    selection.taskId = recordTaskId

                    let menu = MenuContent()
                        .environmentObject(glance)
                        .environmentObject(dashboard)
                        .environmentObject(selection)
                        .background(Theme.canvas)
                        .frame(width: 360)
                        .environment(\.colorScheme, scheme)
                    try SnapshotImageWriter.render(menu, to: out.appendingPathComponent("menu-\(suffix).png"))
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

    static func runDashboardFixture(fixturePath: String, outputDir: String) {
        let fixtureURL = URL(fileURLWithPath: (fixturePath as NSString).expandingTildeInPath)
        let outputURL = URL(fileURLWithPath: (outputDir as NSString).expandingTildeInPath)
        var finished = false
        var exitCode: Int32 = 0
        Task { @MainActor in
            defer { finished = true }
            do {
                let fixture = try DashboardSnapshotFixture.load(from: fixtureURL)
                let rendered = try DashboardSnapshotRenderer.render(
                    fixture: fixture,
                    outputDirectory: outputURL
                )
                print("dashboard snapshots written to \(outputURL.path): \(rendered.count) files")
            } catch {
                exitCode = 1
                FileHandle.standardError.write(Data("dashboard snapshot failed: \(error)\n".utf8))
            }
        }
        while !finished {
            RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.05))
        }
        exit(exitCode)
    }
}
