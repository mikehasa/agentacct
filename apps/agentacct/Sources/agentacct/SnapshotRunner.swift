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
        // These docs screenshots run on whatever machine builds the app, not the
        // pinned golden toolchain, so draw native button chrome as static
        // primitives. The fixture renderers (golden path) deliberately do NOT set
        // this, keeping their references pixel-stable.
        SnapshotMode.rendersStaticControls = true
        // "<with checks>,<without checks>" — the docs pipeline narrows the open
        // step set so one screenshot fits the step spine and the timeline.
        if let spec = ProcessInfo.processInfo.environment["AGENTACCT_SNAPSHOT_EXPANDED_STEPS"] {
            let parts = spec.split(separator: ",").map { Int($0.trimmingCharacters(in: .whitespaces)) }
            if parts.count == 2, let a = parts[0], let b = parts[1] {
                SnapshotMode.expandedStepsWithChecks = a
                SnapshotMode.expandedStepsWithoutChecks = b
            }
        }
        var finished = false
        Task { @MainActor in
            defer { finished = true }
            do {
                let snapshot = try await GlanceClient().fetch()
                let glance = GlanceState(preloaded: snapshot)
                let dashboard = DashboardStore()
                await dashboard.refresh()
                // The Work (worksets) pane's own loader is gated off in snapshot
                // mode (deterministic rendering can't wait on its SwiftUI .task),
                // and refresh() doesn't cover it — fetch the groupings explicitly
                // so window-work-*.png renders populated cards, not the empty state.
                await dashboard.fetchWorksets()
                let selection = AppSelection()
                // refresh() also loads the Task list. Select the newest Task
                // (or the one named by AGENTACCT_SNAPSHOT_TASK — id or prefix —
                // so a design pass can render a specific record, e.g. a blocked
                // one) and preload its Receipt because snapshot mode suppresses
                // the Work pane's network-backed SwiftUI `.task`.
                let wanted = ProcessInfo.processInfo.environment["AGENTACCT_SNAPSHOT_TASK"]
                let requested = dashboard.receiptTasks.first { task in
                    guard let wanted, !wanted.isEmpty else { return false }
                    return task.taskId == wanted || task.taskId.hasPrefix(wanted)
                }
                if let wanted, !wanted.isEmpty, requested == nil {
                    FileHandle.standardError.write(Data(
                        "AGENTACCT_SNAPSHOT_TASK '\(wanted)' matched no loaded task; rendering the newest instead\n".utf8
                    ))
                }
                let flagship = requested ?? dashboard.receiptTasks.first
                if let flagship {
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
                            .environment(glance)
                            .environment(dashboard)
                            .environment(selection)
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
                        .environment(glance)
                        .environment(dashboard)
                        .environment(selection)
                        .frame(width: 1120, alignment: .top)
                        .environment(\.colorScheme, scheme)
                    try SnapshotImageWriter.render(
                        tableWindow,
                        to: out.appendingPathComponent("window-work-table-\(suffix).png")
                    )
                    selection.taskId = recordTaskId

                    // A WIDE Work render: the receipt's adaptive two-column
                    // layout (record detail + evidence side rail: coverage,
                    // sources, gaps) only appears past the side-by-side width
                    // breakpoint. The 1120 all-pane width stacks those columns,
                    // so render the flagship record once more at a wide window
                    // for the README hero. Same record, wider canvas.
                    selection.pane = .work
                    // The Work pane's body is a GeometryReader, which takes the
                    // proposed height rather than growing to its content (the
                    // record detail is a full-height ScrollBox in snapshot mode).
                    // Propose a tall canvas so the whole record — summary strip,
                    // the two-column dimensions + evidence rail, and Sessions &
                    // steps — renders; the docs pipeline trims trailing canvas.
                    // The height must sit at the record's natural height: the
                    // offscreen ScrollBox pins content to the top, so a taller
                    // frame stretches the flexible timeline card into an empty
                    // band and a shorter one clips the supporting sections. The
                    // docs pipeline owns the value (next to its crop table) and
                    // passes it as AGENTACCT_SNAPSHOT_WIDE_HEIGHT (points).
                    let wideHeight = ProcessInfo.processInfo.environment["AGENTACCT_SNAPSHOT_WIDE_HEIGHT"]
                        .flatMap(Double.init) ?? 1500
                    let wideWork = MainWindow()
                        .environment(glance)
                        .environment(dashboard)
                        .environment(selection)
                        .frame(width: 1520, height: wideHeight, alignment: .top)
                        .environment(\.colorScheme, scheme)
                    try SnapshotImageWriter.render(
                        wideWork,
                        to: out.appendingPathComponent("window-work-wide-\(suffix).png")
                    )

                    let menu = MenuContent()
                        .environment(glance)
                        .environment(dashboard)
                        .environment(selection)
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

    static func runUsageFixture(fixturePath: String, outputDir: String) {
        let fixtureURL = URL(fileURLWithPath: (fixturePath as NSString).expandingTildeInPath)
        let outputURL = URL(fileURLWithPath: (outputDir as NSString).expandingTildeInPath)
        var finished = false
        var exitCode: Int32 = 0
        Task { @MainActor in
            defer { finished = true }
            do {
                let fixture = try DashboardSnapshotFixture.load(from: fixtureURL)
                let rendered = try UsageSnapshotRenderer.render(
                    fixture: fixture,
                    outputDirectory: outputURL
                )
                print("usage snapshots written to \(outputURL.path): \(rendered.count) files")
            } catch {
                exitCode = 1
                FileHandle.standardError.write(Data("usage snapshot failed: \(error)\n".utf8))
            }
        }
        while !finished {
            RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.05))
        }
        exit(exitCode)
    }

    static func runSourcesFixture(fixturePath: String, outputDir: String) {
        let fixtureURL = URL(fileURLWithPath: (fixturePath as NSString).expandingTildeInPath)
        let outputURL = URL(fileURLWithPath: (outputDir as NSString).expandingTildeInPath)
        var finished = false
        var exitCode: Int32 = 0
        Task { @MainActor in
            defer { finished = true }
            do {
                let fixture = try DashboardSnapshotFixture.load(from: fixtureURL)
                let rendered = try SourcesSnapshotRenderer.render(
                    fixture: fixture,
                    outputDirectory: outputURL
                )
                print("sources snapshots written to \(outputURL.path): \(rendered.count) files")
            } catch {
                exitCode = 1
                FileHandle.standardError.write(Data("sources snapshot failed: \(error)\n".utf8))
            }
        }
        while !finished {
            RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.05))
        }
        exit(exitCode)
    }

    static func runMenuFixture(fixturePath: String, outputDir: String) {
        let fixtureURL = URL(fileURLWithPath: (fixturePath as NSString).expandingTildeInPath)
        let outputURL = URL(fileURLWithPath: (outputDir as NSString).expandingTildeInPath)
        var finished = false
        var exitCode: Int32 = 0
        Task { @MainActor in
            defer { finished = true }
            do {
                let fixture = try DashboardSnapshotFixture.load(from: fixtureURL)
                let rendered = try MenuSnapshotRenderer.render(
                    fixture: fixture,
                    outputDirectory: outputURL
                )
                print("menu snapshots written to \(outputURL.path): \(rendered.count) files")
            } catch {
                exitCode = 1
                FileHandle.standardError.write(Data("menu snapshot failed: \(error)\n".utf8))
            }
        }
        while !finished {
            RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.05))
        }
        exit(exitCode)
    }

    static func runWorkFixture(fixturePath: String, outputDir: String) {
        let fixtureURL = URL(fileURLWithPath: (fixturePath as NSString).expandingTildeInPath)
        let outputURL = URL(fileURLWithPath: (outputDir as NSString).expandingTildeInPath)
        var finished = false
        var exitCode: Int32 = 0
        Task { @MainActor in
            defer { finished = true }
            do {
                let fixture = try DashboardSnapshotFixture.load(from: fixtureURL)
                let rendered = try renderWorkFixture(
                    fixture: fixture,
                    outputDirectory: outputURL
                )
                print("work snapshots written to \(outputURL.path): \(rendered.count) files")
            } catch {
                exitCode = 1
                FileHandle.standardError.write(Data("work snapshot failed: \(error)\n".utf8))
            }
        }
        while !finished {
            RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.05))
        }
        exit(exitCode)
    }

    /// The CLI and its regression test share the complete Work render path.
    /// Keep action/check galleries alongside pages, steps and shared components
    /// so a successful review render can satisfy the fixed candidate inventory.
    @MainActor
    static func renderWorkFixture(
        fixture: DashboardSnapshotFixture,
        outputDirectory: URL
    ) throws -> [URL] {
        let pages = try WorkSnapshotRenderer.render(fixture: fixture, outputDirectory: outputDirectory)
        let steps = try SessionStepsSnapshotRenderer.render(fixture: fixture, outputDirectory: outputDirectory)
        let actions = try ReceiptActionSnapshotRenderer.render(outputDirectory: outputDirectory)
        let checks = try ReceiptCheckSnapshotRenderer.render(outputDirectory: outputDirectory)
        let components = try WorkComponentSnapshotRenderer.render(outputDirectory: outputDirectory)
        return pages + steps + actions + checks + components
    }

    static func runAbout(applicationIconPath: String, outputDir: String) {
        let iconURL = URL(fileURLWithPath: (applicationIconPath as NSString).expandingTildeInPath)
        let outputURL = URL(fileURLWithPath: (outputDir as NSString).expandingTildeInPath)
        var finished = false
        var exitCode: Int32 = 0
        Task { @MainActor in
            defer { finished = true }
            do {
                guard let applicationIcon = NSImage(contentsOf: iconURL) else {
                    throw AboutSnapshotError.applicationIconUnavailable(iconURL)
                }
                let rendered = try AboutSnapshotRenderer.render(
                    outputDirectory: outputURL,
                    applicationIcon: applicationIcon
                )
                print("About snapshots written to \(outputURL.path): \(rendered.count) files")
            } catch {
                exitCode = 1
                FileHandle.standardError.write(Data("About snapshot failed: \(error)\n".utf8))
            }
        }
        while !finished {
            RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.05))
        }
        exit(exitCode)
    }
}
