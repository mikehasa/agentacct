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
        // SnapshotMode stays OFF until every pane's payload has loaded: the
        // store deliberately skips network lanes (e.g. /v1/ingestion) while it
        // is on, so enabling it first would render Sources before its data
        // exists (C65). It is switched on just before the first render.
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

                // Await each pane's model load (bounded) before any render, so
                // no pane is captured in its loading state.
                for pane in MainPane.allCases {
                    await SnapshotRunner.waitForLoad(of: pane) {
                        SnapshotRunner.paneIsLoaded(pane, dashboard: dashboard, taskId: selection.taskId)
                    }
                }
                SnapshotMode.enabled = true

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
                    // proposed height rather than growing to its content, so a
                    // width-only frame would collapse it to the window minimum.
                    // The ScrollBox never offers that proposed height to the
                    // record content (C66); the image is then cut to the record's
                    // own content height — never a fixed tall canvas with slack.
                    try SnapshotRunner.renderFittingContentHeight(
                        width: 1520,
                        to: out.appendingPathComponent("window-work-wide-\(suffix).png")
                    ) { height in
                        MainWindow()
                            .environment(glance)
                            .environment(dashboard)
                            .environment(selection)
                            .frame(width: 1520, height: height, alignment: .top)
                            .environment(\.colorScheme, scheme)
                    }

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

    /// Polls `isLoaded` on the main actor until it holds or `timeout` passes.
    /// A timeout is reported, not fatal: the pane then renders its own named
    /// loading or error state.
    @MainActor
    private static func waitForLoad(
        of pane: MainPane,
        timeout: TimeInterval = 30,
        until isLoaded: @MainActor () -> Bool
    ) async {
        let deadline = Date().addingTimeInterval(timeout)
        while !isLoaded() {
            guard Date() < deadline else {
                FileHandle.standardError.write(Data(
                    "snapshot: \(pane.rawValue) data did not load within \(Int(timeout))s; rendering its current state\n".utf8
                ))
                return
            }
            try? await Task.sleep(for: .milliseconds(50))
        }
    }

    /// A pane is loaded once each lane it renders has either data or an error.
    @MainActor
    private static func paneIsLoaded(_ pane: MainPane, dashboard: DashboardStore, taskId: String?) -> Bool {
        let usageSettled = dashboard.usage != nil || dashboard.errorText != nil
        switch pane {
        case .dashboard:
            return !dashboard.isRefreshing && usageSettled
                && (dashboard.dashboardAttention != nil || dashboard.dashboardAttentionError != nil)
                && (dashboard.ingestion != nil || dashboard.ingestionError != nil)
        case .work:
            guard !dashboard.isLoadingReceipts else { return false }
            guard let taskId else { return true }
            return dashboard.receipt?.taskId == taskId || dashboard.receiptError != nil
        case .usage:
            return !dashboard.isRefreshing && usageSettled
        case .sources:
            return !dashboard.isRefreshingIngestion
                && (dashboard.ingestion != nil || dashboard.ingestionError != nil)
        case .worksets:
            // The groupings lane has one fetch and an empty list is a settled
            // state (no groups recorded yet), so the in-flight flag is the whole
            // condition — `worksetsLastUpdated` is deliberately nil under
            // SnapshotMode and would never settle here.
            return !dashboard.isLoadingWorksets
        }
    }

    /// Renders a view whose height must be proposed (a GeometryReader root) and
    /// crops the image to its content. Content is measured across the FULL row
    /// width: the record's label/value columns (e.g. the last TASK ID row) sit
    /// left of centre, so a right-half probe would stop at the last hairline
    /// and cut them off. The trailing zone is still uniform row-to-row (master
    /// list ground, its rule, and the detail canvas).
    ///
    /// Trailing canvas alone does not prove the content ended: a proposal that
    /// clips the record between two rows (e.g. the gap between session rows)
    /// also ends in blank canvas. So the proposal doubles until two successive
    /// proposals measure the SAME content height with canvas to spare; only
    /// then is the content known to be fully laid out, and the image keeps the
    /// content plus one gutter.
    @MainActor
    private static func renderFittingContentHeight<Content: View>(
        width: CGFloat,
        to url: URL,
        initialHeight: CGFloat = 1600,
        maximumHeight: CGFloat = 8000,
        content: (CGFloat) -> Content
    ) throws {
        let scale: CGFloat = 2
        let marginRows = Int(Space.gutter * scale)
        var height = initialHeight
        var previousContentRows: Int?
        while true {
            let renderer = ImageRenderer(content: content(height))
            renderer.scale = scale
            renderer.colorMode = .nonLinear
            guard let image = renderer.cgImage else {
                throw SnapshotError.renderProducedNoImage
            }
            let contentRows = contentRowCount(image)
            let hasTrailingCanvas = image.height - contentRows >= marginRows
            let contentIsStable = hasTrailingCanvas && previousContentRows == contentRows
            if contentIsStable || height >= maximumHeight {
                if !contentIsStable {
                    FileHandle.standardError.write(Data(
                        "snapshot: \(url.lastPathComponent) content height not confirmed within \(Int(maximumHeight))pt; image may be clipped\n".utf8
                    ))
                }
                let keptRows = min(image.height, contentRows + marginRows)
                let cropped = image.cropping(to: CGRect(x: 0, y: 0, width: image.width, height: keptRows)) ?? image
                let representation = NSBitmapImageRep(cgImage: cropped)
                guard let png = representation.representation(using: .png, properties: [:]) else {
                    throw SnapshotError.pngEncodingFailed
                }
                try png.write(to: url)
                return
            }
            previousContentRows = contentRows
            height = min(height * 2, maximumHeight)
        }
    }

    /// Rows from the top through the last row that differs from the bottom
    /// row (the uniform trailing canvas), compared across the full width.
    private static func contentRowCount(_ image: CGImage) -> Int {
        let width = image.width
        let height = image.height
        guard width > 0, height > 1 else { return height }
        let bytesPerRow = width * 4
        var pixels = [UInt8](repeating: 0, count: bytesPerRow * height)
        let drawn = pixels.withUnsafeMutableBytes { buffer -> Bool in
            guard let context = CGContext(
                data: buffer.baseAddress,
                width: width,
                height: height,
                bitsPerComponent: 8,
                bytesPerRow: bytesPerRow,
                space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
            ) else { return false }
            context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
            return true
        }
        guard drawn else { return height }
        // Bitmap memory is stored top row first.
        let startByte = 0
        let lastRowOffset = (height - 1) * bytesPerRow
        let comparedBytes = bytesPerRow - startByte
        return pixels.withUnsafeBytes { buffer -> Int in
            guard let base = buffer.baseAddress else { return height }
            let bottom = base + lastRowOffset + startByte
            var row = height - 1
            while row > 0, memcmp(base + (row - 1) * bytesPerRow + startByte, bottom, comparedBytes) == 0 {
                row -= 1
            }
            return row
        }
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
                let pages = try WorkSnapshotRenderer.render(
                    fixture: fixture,
                    outputDirectory: outputURL
                )
                let sessionSteps = try SessionStepsSnapshotRenderer.render(
                    fixture: fixture,
                    outputDirectory: outputURL
                )
                let rendered = pages + sessionSteps
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
