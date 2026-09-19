import AppKit
import SwiftUI

/// Explicit fixture-only entry points for native design review. This harness
/// never starts a recorder or uses a real installer. Production windows do not
/// instantiate it. Both screenshots and the interactive window use app views.
struct NativeTimelineReviewFrame: Decodable {
    let receipt: Receipt
    let sessions: [V1SessionDetail]
}

enum NativeReviewScreen: String, CaseIterable, Identifiable {
    case largeDashboard, largeUsage, liveTimeline, largeOffline, largeSources, largeTimeline, largeHealth, largeSetup, compactLargeSetup, largeSetupContent, largeActivation, welcome, review, setupContent, working, pending, failure, recovery, recovered, updateRecovery, activation, timeline, recordDetail, focus, work, compactWork, offline, health, sources
    var id: String { rawValue }
    var usesLargeText: Bool { [.largeDashboard, .largeUsage, .largeOffline, .largeSources, .largeTimeline, .largeHealth, .largeSetup, .compactLargeSetup, .largeSetupContent, .largeActivation].contains(self) }
    var title: String {
        switch self {
        case .largeOffline: return "Large text saved work"
        case .liveTimeline: return "Live sample timeline"
        case .recordDetail: return "Record details"
        case .largeSources: return "Large text Sources"
        case .largeTimeline: return "Large text timeline"
        case .largeHealth: return "Large text health"
        case .largeSetup: return "Large text setup"
        case .compactLargeSetup: return "Compact large text setup"
        case .largeSetupContent: return "Large text content"
        case .largeActivation: return "Large text activation"
        case .setupContent: return "Setup content"
        case .updateRecovery: return "Update recovery"
        case .compactWork: return "Compact Work"
        default: return rawValue.capitalized
        }
    }
}

struct NativeReviewSurface: View {
    let fixture: DashboardSnapshotFixture
    let screen: NativeReviewScreen
    @Environment(\.dynamicTypeSize) private var inheritedReadingSize
    @State private var dashboard: DashboardStore
    @State private var glance: GlanceState
    @State private var selection = AppSelection()
    @State private var healthCoordinator = RecordingHealthCoordinator()
    @StateObject private var setup: SetupModel
    @State private var liveFrame = 0
    @State private var liveRun = 0
    @State private var playingSample = false
    @State private var showingSourceSetup = false

    init(fixture: DashboardSnapshotFixture, screen: NativeReviewScreen) {
        self.fixture = fixture
        self.screen = screen
        SnapshotMode.reviewExpandSetupDetails = screen.usesLargeText
        if (screen == .offline || screen == .largeOffline), let saved = NativeReviewRunner.savedFixture {
            _dashboard = State(initialValue: DashboardStore(savedWork: saved, taskID: fixture.work?.receipt.taskId))
        } else {
            let store = DashboardStore(preloaded: fixture)
            if screen == .liveTimeline, let first = NativeReviewRunner.liveFrames.first { store.applyNativeReviewSessions(first.sessions, receipt: first.receipt) }
            _dashboard = State(initialValue: store)
        }
        _glance = State(initialValue: GlanceState(preloaded: fixture.glanceSnapshot))
        let initialSelection = AppSelection()
        initialSelection.pane = .work
        initialSelection.taskId = fixture.work?.receipt.taskId
        _selection = State(initialValue: initialSelection)
        let phase: SetupModel.Phase
        switch screen {
        case .working: phase = .working("Connecting Codex…")
        case .pending, .recovery, .recovered, .activation, .largeActivation: phase = .done
        case .updateRecovery: phase = .failed("Synthetic fixture: recorder update was interrupted before synchronization completed.")
        case .failure: phase = .failed("Could not update the client configuration: permission denied.")
        default: phase = .idle
        }
        let choosingClient = [.welcome, .review, .setupContent, .largeSetup, .compactLargeSetup, .sources, .largeSources].contains(screen)
        _setup = StateObject(wrappedValue: SetupModel(
            reviewPhase: phase,
            selectedClient: choosingClient ? nil : .codex,
            onboardingCompletedAt: screen == .pending ? Date(timeIntervalSince1970: 1_700_000_000) : nil
        ))
    }

    private var health: RecordingHealthSnapshot {
        .project(
            glancePhase: .disconnected("Synthetic fixture: local endpoint refused the connection."),
            setupPhase: .idle,
            ingestion: fixture.ingestion?.ingestion,
            ingestionError: "Synthetic fixture: source health could not be refreshed.",
            canSetUp: true,
            configuredClientIDs: screen == .largeHealth ? ["codex", "claude-code"] : [],
            captures: screen == .largeHealth ? [.init(clientID: "codex", eventID: "synthetic-capture", observedAt: Date(timeIntervalSince1970: 1_700_000_060))] : [],
            requiredCaptureAfter: screen == .largeHealth ? ["codex": Date(timeIntervalSince1970: 1_700_000_000), "claude-code": Date(timeIntervalSince1970: 1_700_000_000)] : [:]
        )
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Label("Native macOS design review", systemImage: "macwindow")
                Spacer()
                Text("Synthetic data · no setup writes · no live network")
            }
            .font(.system(size: 11, weight: .medium))
            .foregroundStyle(Theme.muted)
            .padding(10)
            Divider()
            if screen == .liveTimeline { sampleControls }
            Group {
                switch screen {
                case .largeSetup, .compactLargeSetup, .welcome, .review, .working, .pending, .failure:
                    NativeSetupFlow(setup: setup, onClose: {}, initialReview: screen == .review || screen.usesLargeText, contentPreview: screen == .review || screen.usesLargeText ? NativeReviewRunner.setupContentFixture : nil)
                case .setupContent, .largeSetupContent:
                    ScrollView {
                        NativeSetupContentPreviewView(state: NativeReviewRunner.setupContentFixture.map(SetupContentPreviewState.available) ?? .unavailable("A synthetic preview was not supplied in this fixture."), onRetry: {}, isExpanded: .constant(true)).padding(24)
                    }
                case .recovery:
                    NativeSetupFlow(setup: setup, onClose: {}, recoveryReason: "Synthetic fixture: the configured recorder no longer responds.", onReconnect: { false })
                case .recovered:
                    NativeSetupFlow(setup: setup, onClose: {}, recoveryReason: "Synthetic fixture: the configured recorder no longer responds.", onReconnect: { true }, reviewRecoveryResult: true)
                case .updateRecovery:
                    NativeSetupFlow(setup: setup, onClose: {}, recoveryReason: "Synthetic fixture: recorder update was interrupted before synchronization completed.", recoveryKind: .synchronization, onReconnect: { false })
                case .activation, .largeActivation:
                    NativeClientActivationView(client: .claudeCode, boundary: Date(timeIntervalSince1970: 1_700_000_000), capture: nil, savedSetupLog: ["Synthetic setup output for Claude Code.", "No configuration files were changed."], onClose: {}, onOpenWork: {})
                case .focus:
                    WorkPane(timelineFocused: true)
                case .offline, .largeOffline:
                    SavedWorkView(store: dashboard, onReconnect: {})
                case .sources, .largeSources:
                    if showingSourceSetup {
                        NativeSetupFlow(setup: setup, onClose: { showingSourceSetup = false })
                    } else {
                        SourcesPane(onSetup: { _ in showingSourceSetup = true })
                    }
                case .largeDashboard:
                    DashboardPane()
                case .largeUsage:
                    UsagePane()
                case .timeline, .recordDetail, .largeTimeline, .liveTimeline:
                    if let receipt = (screen == .liveTimeline ? dashboard.receipt : fixture.work?.receipt) {
                        ScrollViewReader { proxy in
                            ScrollView {
                                WorkTimelineView(receipt: receipt, reviewSelectedRecord: screen == .recordDetail,
                                    onRevealInspector: { proxy.scrollTo("work.timeline.inspector", anchor: .top) },
                                    onRevealRecords: { proxy.scrollTo("work.timeline.records", anchor: .top) },
                                    onRevealHeading: { proxy.scrollTo("work.timeline.heading", anchor: .top) }).padding(20)
                                    .id(liveRun)
                            }
                        }
                    }
                case .work, .compactWork:
                    MainWindow(canSetUpOverride: true)
                        .onAppear {
                            selection.pane = .work
                            selection.taskId = fixture.work?.receipt.taskId
                        }
                case .health, .largeHealth:
                    HStack(alignment: .top, spacing: 30) {
                        RecordingHealthPopover(snapshot: health, onAction: { _ in })
                        RecordingHealthNoticeStack(
                            coordinator: healthCoordinator,
                            onSetup: {}, onSources: {}, onRefresh: {}
                        )
                    }
                    .padding(24)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                    .onAppear { healthCoordinator.update(health) }
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        }
        .background(Theme.canvas)
        .environment(\.dynamicTypeSize, screen.usesLargeText ? .accessibility3 : inheritedReadingSize)
        .environment(dashboard)
        .environment(glance)
        .environment(selection)
        .task(id: playingSample) {
            guard playingSample, screen == .liveTimeline else { return }
            while !Task.isCancelled, liveFrame + 1 < NativeReviewRunner.liveFrames.count {
                do { try await Task.sleep(for: .seconds(3)) } catch { return }
                guard !Task.isCancelled else { return }
                advanceSample()
            }
            playingSample = false
        }
    }

    private var sampleControls: some View {
        HStack(spacing: 12) {
            Button(playingSample ? "Pause sample" : "Play sample progress") { playingSample.toggle() }
                .disabled(liveFrame + 1 >= NativeReviewRunner.liveFrames.count)
                .buttonStyle(QuietButtonStyle())
                .accessibilityIdentifier("native-review.play")
            Button("Next sample update", action: advanceSample)
                .disabled(liveFrame + 1 >= NativeReviewRunner.liveFrames.count)
                .buttonStyle(QuietButtonStyle())
                .accessibilityIdentifier("native-review.next")
            Button("Restart sample") {
                playingSample = false; liveFrame = 0; liveRun += 1
                if let first = NativeReviewRunner.liveFrames.first { dashboard.applyNativeReviewSessions(first.sessions, receipt: first.receipt) }
            }.buttonStyle(QuietButtonStyle())
            Spacer()
            Text(NativeReviewRunner.liveFrames.isEmpty ? "This fixture has no sample updates." : "Synthetic snapshot \(liveFrame + 1) of \(NativeReviewRunner.liveFrames.count) · 3 seconds per update")
        }
        .font(.system(size: 12)).padding(12)
        .background(Theme.chrome)
    }

    private func advanceSample() {
        guard liveFrame + 1 < NativeReviewRunner.liveFrames.count else { return }
        liveFrame += 1
        let frame = NativeReviewRunner.liveFrames[liveFrame]
        dashboard.applyNativeReviewSessions(frame.sessions, receipt: frame.receipt)
    }
}

struct NativeInteractiveReview: View {
    let fixture: DashboardSnapshotFixture
    @State private var screen: NativeReviewScreen
    init(fixture: DashboardSnapshotFixture) {
        self.fixture = fixture
        _screen = State(initialValue: NativeReviewRunner.liveFrames.isEmpty ? .timeline : .liveTimeline)
    }
    var body: some View {
        VStack(spacing: 0) {
            Picker("Review scene", selection: $screen) {
                ForEach(NativeReviewScreen.allCases) { Text($0.title).tag($0) }
            }
            .pickerStyle(.menu)
            .accessibilityIdentifier("native-review.scene")
            .padding(12)
            NativeReviewSurface(fixture: fixture, screen: screen).id(screen)
        }
        .frame(minWidth: 960, minHeight: 640)
    }
}

@MainActor
private struct NativeReviewApp: App {
    @State private var readingSize = 0
    @State private var windowLaunch = NativeWindowLaunchRequest()
    static var fixture: DashboardSnapshotFixture?
    var body: some Scene {
        Window("agentacct — Native design review (synthetic)", id: "native-review") {
            if let fixture = Self.fixture {
                NativeInteractiveReview(fixture: fixture)
                    .environment(\.dynamicTypeSize, ReadingSize.dynamicTypeSize(readingSize))
                    .task {
                        NSApp.setActivationPolicy(.regular)
                        // Launch Services can retain a hidden state between bare
                        // review executable launches. Present after scene layout.
                        try? await Task.sleep(for: .milliseconds(150))
                        guard !Task.isCancelled else { return }
                        NSApp.unhide(nil)
                        NSApp.windows.first(where: { $0.title == "agentacct — Native design review (synthetic)" })?.makeKeyAndOrderFront(nil)
                        NSApp.activate(ignoringOtherApps: true)
                    }
            }
        }
        .defaultSize(width: 1320, height: 900)
        .commands {
            NativeWindowCommands(id: "native-review", title: "native review", launch: windowLaunch)
            ReadingSizeCommands(selection: $readingSize)
            CommandGroup(after: .newItem) {
                Button("Capture current review") { NativeReviewRunner.captureCurrentReview() }.buttonStyle(QuietButtonStyle())
            }
        }
    }
}

enum NativeReviewRunner {
    @MainActor private static var reviewWindow: NSWindow?
    @MainActor static var savedFixture: SavedWorkSnapshot?
    @MainActor static var setupContentFixture: SetupContentPreview?
    @MainActor static var liveFrames: [NativeTimelineReviewFrame] = []

    @MainActor static func captureCurrentReview() {
        guard SnapshotMode.enabled,
              let window = NSApp.windows.first(where: {
                  $0.isVisible && $0.title != "agentacct — Native design review (synthetic)"
                    && $0.contentView != nil && $0.frame.width > 100
              }) ?? NSApp.keyWindow ?? reviewWindow,
              let view = window.contentView,
              let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
        view.cacheDisplay(in: view.bounds, to: bitmap)
        guard let png = bitmap.representation(using: .png, properties: [:]) else { return }
        let path = FileManager.default.temporaryDirectory.appendingPathComponent("agentacct-native-current.png")
        do {
            try png.write(to: path)
            func scrollState(_ view: NSView) -> [[String: Double]] {
                let own: [[String: Double]]
                if let scroll = view as? NSScrollView {
                    own = [["x": scroll.contentView.bounds.minX, "y": scroll.contentView.bounds.minY,
                            "width": scroll.contentView.bounds.width, "height": scroll.contentView.bounds.height]]
                } else { own = [] }
                return own + view.subviews.flatMap(scrollState)
            }
            let state: [String: Any] = ["active": NSApp.isActive,
                "processID": ProcessInfo.processInfo.processIdentifier,
                "capturedAt": Date().ISO8601Format(), "keyWindow": window.title,
                "capturedWindowNumber": window.windowNumber,
                "windowWidth": window.frame.width, "windowHeight": window.frame.height,
                "visibleWindows": NSApp.windows.filter(\.isVisible).map {
                    ["number": $0.windowNumber, "title": $0.title,
                     "width": $0.frame.width, "height": $0.frame.height] as [String: Any]
                },
                "firstResponder": String(describing: window.firstResponder),
                "scrollViews": scrollState(view)]
            try JSONSerialization.data(withJSONObject: state, options: [.prettyPrinted, .sortedKeys])
                .write(to: path.deletingPathExtension().appendingPathExtension("json"))
            print("Native review capture: \(path.path)")
        }
        catch { print("Native review capture failed: \(error)") }
    }

    @MainActor private static func makeSavedFixture(path: String) throws -> SavedWorkSnapshot {
        guard let raw = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: path))) as? [String: Any] else {
            throw CocoaError(.fileReadCorruptFile)
        }
        if let frames = raw["native_review_frames"] {
            liveFrames = try JSONDecoder().decode([NativeTimelineReviewFrame].self, from: JSONSerialization.data(withJSONObject: frames))
        } else { liveFrames = [] }
        if let proposal = raw["setup_preview"] {
            setupContentFixture = try JSONDecoder().decode(SetupContentPreview.self, from: JSONSerialization.data(withJSONObject: proposal))
        } else { setupContentFixture = nil }
        var saved = SavedWorkSnapshot(storePath: "/synthetic-review/state")
        let generatedAt = (raw["glance"] as? [String: Any])?["generated_at"] as? Double ?? 1_787_620_000
        let date = Date(timeIntervalSince1970: generatedAt + 180)
        func add(_ path: String, _ value: Any, date: Date) throws {
            saved.entries[path] = .init(path: path, receivedAt: date, data: try JSONSerialization.data(withJSONObject: value))
        }
        if let tasks = raw["tasks"] { try add("/v1/tasks?limit=200", tasks, date: date) }
        if let work = raw["work"] as? [String: Any] {
            if let receipt = work["receipt"] as? [String: Any], let id = receipt["task_id"] as? String {
                try add("/v1/receipt?task=\(DashboardStore.queryValue(id))", receipt, date: date.addingTimeInterval(-60))
            }
            for detail in work["sessions"] as? [[String: Any]] ?? [] {
                if let session = detail["session"] as? [String: Any], let client = session["client"] as? String,
                   let id = session["client_session_id"] as? String {
                    try add("/v1/session?client=\(DashboardStore.queryValue(client))&session_id=\(DashboardStore.queryValue(id))", detail, date: date.addingTimeInterval(-120))
                }
            }
        }
        return saved
    }

    @MainActor
    static func render(fixture: DashboardSnapshotFixture, directory: URL) throws {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        SnapshotMode.enabled = true
        SnapshotMode.interactiveFixture = true
        SnapshotMode.boundsScrollContentToViewport = true
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        SnapshotMode.setFixtureDate(fixture.glance.generatedAt.map(Date.init(timeIntervalSince1970:)))
        defer {
            SnapshotMode.enabled = false
            SnapshotMode.reviewExpandSetupDetails = false
            SnapshotMode.interactiveFixture = false
            SnapshotMode.boundsScrollContentToViewport = false
            SnapshotMode.setFixtureDate(nil)
            SnapshotScheme.override = nil
        }
        let requestedScenes = ProcessInfo.processInfo.environment["AGENTACCT_NATIVE_REVIEW_SCENES"]?.split(separator: ",").map(String.init)
        for screen in NativeReviewScreen.allCases where requestedScenes == nil || requestedScenes!.contains(screen.rawValue) {
            for scheme in [ColorScheme.light, .dark] {
                SnapshotScheme.override = scheme
                let compact = screen == .largeOffline || screen == .largeSources || screen == .compactWork || screen == .compactLargeSetup || screen == .largeActivation
                let tall = screen == .recordDetail || screen == .sources || screen == .setupContent || screen == .largeSetupContent
                let size = CGSize(width: screen == .focus ? 1320 : compact ? 960 : 1120, height: compact ? 640 : tall ? 1500 : 860)
                let view = NativeReviewSurface(fixture: fixture, screen: screen)
                    .frame(width: size.width, height: size.height)
                    .environment(\.colorScheme, scheme)
                    .environment(\.displayScale, 2)
                    .transaction { $0.disablesAnimations = true }
                let name = "native-\(screen.rawValue)-\(scheme == .dark ? "dark" : "light").png"
                // AppKit hosts the real scroll views and native control chrome.
                // ImageRenderer alone omits NSScrollView contents on this host.
                let host = NSHostingView(rootView: view)
                let window = NSWindow(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless], backing: .buffered, defer: false)
                window.contentView = host
                window.setFrameOrigin(NSPoint(x: -3000, y: -3000))
                window.orderFront(nil)
                host.layoutSubtreeIfNeeded()
                RunLoop.main.run(until: Date().addingTimeInterval(0.2))
                guard let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { throw SnapshotError.renderProducedNoImage }
                host.cacheDisplay(in: host.bounds, to: bitmap)
                guard let png = bitmap.representation(using: .png, properties: [:]) else { throw SnapshotError.pngEncodingFailed }
                try png.write(to: directory.appendingPathComponent(name))
                window.orderOut(nil)
            }
        }
    }

    static func run(fixturePath: String, outputDirectory: String?) {
        // CLI entry executes on the main thread. Keep App.main outside an
        // asynchronous task so AppKit owns the actual application event loop.
        MainActor.assumeIsolated { runOnMain(fixturePath: fixturePath, outputDirectory: outputDirectory) }
    }

    @MainActor
    private static func runOnMain(fixturePath: String, outputDirectory: String?) {
        do {
            savedFixture = try makeSavedFixture(path: fixturePath)
            let fixture = try DashboardSnapshotFixture.load(from: URL(fileURLWithPath: fixturePath))
            if let outputDirectory {
                try render(fixture: fixture, directory: URL(fileURLWithPath: outputDirectory))
            } else {
                SnapshotMode.enabled = true
                SnapshotMode.interactiveFixture = true
                SnapshotMode.setFixtureDate(fixture.glance.generatedAt.map(Date.init(timeIntervalSince1970:)))
                NativeReviewApp.fixture = fixture
                NSApplication.shared.setActivationPolicy(.regular)
                NativeReviewApp.main()
            }
            exit(0)
        } catch {
            FileHandle.standardError.write(Data("Native review failed: \(error)\n".utf8))
            exit(1)
        }
    }
}
