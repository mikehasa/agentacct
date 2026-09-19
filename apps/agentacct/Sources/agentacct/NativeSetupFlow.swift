import AppKit
import SwiftUI

/// A review of the named global onboarding adapter, not a simulated installer
/// or a byte-for-byte diff. Paths mirror cli.py's user-scope adapters.
struct SetupConfigurationPlan {
    struct Change: Identifiable, Equatable {
        let path: String
        let action: String
        var id: String { path }
    }

    let client: SetupClient
    let changes: [Change]

    init(
        client: SetupClient,
        homeDirectory: URL = FileManager.default.homeDirectoryForCurrentUser,
        environment: [String: String] = ProcessInfo.processInfo.environment,
        fileExists: (String) -> Bool = {
            var isDirectory: ObjCBool = false
            return FileManager.default.fileExists(atPath: $0, isDirectory: &isDirectory) && !isDirectory.boolValue
        }
    ) {
        self.client = client
        func home(_ path: String) -> String { homeDirectory.appendingPathComponent(path).path }
        switch client {
        case .codex:
            changes = [
                Change(path: home(".codex/config.toml"), action: "Add or update agentacct's MCP server block and recognized older agentacct registrations."),
                Change(path: home(".codex/AGENTS.md"), action: "Add or update the managed instruction to record work sections and checks."),
                Change(path: home(".codex/hooks.json"), action: "Merge agentacct's tool-activity and session-end hooks."),
                Change(path: home(".codex/hooks/agentacct_codex_hook.py"), action: "Install the observe-only hook wrapper.")
            ]
        case .claudeCode:
            changes = [
                Change(path: home(".claude.json"), action: "Merge the user-level agentacct MCP server registration."),
                Change(path: home(".claude/CLAUDE.md"), action: "Add or update the managed instruction to record work sections and checks."),
                Change(path: home(".claude/settings.json"), action: "Merge recording hooks and ENABLE_TOOL_SEARCH=auto. Add the agentacct status line only if none exists."),
                Change(path: home(".claude/hooks/claude_pre_tool_use.py"), action: "Install the recording hook wrapper."),
                Change(path: home(".claude/settings.agent-sentinel.example.json"), action: "Write the generated settings example for inspection or manual merge.")
            ]
        case .openCode:
            let xdg = environment["XDG_CONFIG_HOME"]?.trimmingCharacters(in: .whitespacesAndNewlines)
            let root = xdg.flatMap { $0.isEmpty ? nil : URL(fileURLWithPath: $0) }
                ?? homeDirectory.appendingPathComponent(".config")
            let directory = root.appendingPathComponent("opencode")
            let jsonc = directory.appendingPathComponent("opencode.jsonc").path
            let json = directory.appendingPathComponent("opencode.json").path
            let config = fileExists(jsonc) ? jsonc : (fileExists(json) ? json : jsonc)
            changes = [
                Change(path: config, action: "Merge agentacct's MCP server if the file can be parsed safely. JSONC comments require manual registration."),
                Change(path: directory.appendingPathComponent("AGENTS.md").path, action: "Add or update the managed instruction to record work sections and checks."),
                Change(path: directory.appendingPathComponent("plugins/agentacct.js").path, action: "Install the observe-only tool-activity plugin.")
            ]
        case .hermes:
            changes = [
                Change(path: home(".hermes/config.yaml"), action: "Merge agentacct's MCP server and tool, check, turn-boundary, and recording-instruction hooks."),
                Change(path: home(".hermes/hooks/agentacct_hermes_hook.py"), action: "Install the hook wrapper. Hermes requires separate approval before hooks run.")
            ]
        case .deepseekHarness:
            let dshEnv = (environment["DSH_HOME"] ?? environment["DSH_DIR"])?.trimmingCharacters(in: .whitespacesAndNewlines)
            let dshHome = dshEnv.flatMap { $0.isEmpty ? nil : URL(fileURLWithPath: ($0 as NSString).expandingTildeInPath) }
                ?? homeDirectory.appendingPathComponent(".dsh")
            changes = [
                Change(path: dshHome.appendingPathComponent("cordis.patch.yml").path, action: "Add the @deepseek-ai/dsh-mcp-client MCP server to the home patch applied over every dsh profile (non-destructive append; previewed if the file cannot be safely extended)."),
                Change(path: dshHome.appendingPathComponent("AGENTS.md").path, action: "Add or update the managed instruction to record work sections and checks.")
            ]
        }
    }

    var activationInstruction: String {
        Self.activationInstruction(for: client)
    }

    var scopeSummary: String {
        "Connect \(client.title), install the shared recorder, import available local usage across clients, and start background sync and the local API."
    }

    static func activationInstruction(for client: SetupClient) -> String {
        switch client {
        case .codex:
            return "Open a new Codex session and approve the agentacct hook when Codex asks."
        case .claudeCode:
            return "Open a new Claude Code session so it loads the MCP server, instructions, and hooks."
        case .openCode:
            return "Open a new OpenCode session so it loads the MCP server, rules, and activity plugin."
        case .hermes:
            return "Approve the agentacct hooks in Hermes, then restart its gateway or open a new session. The setup output contains the exact consent steps."
        case .deepseekHarness:
            return "Start a new dsh session so it loads the home-patch MCP server and $DSH_HOME/AGENTS.md instructions. If dsh reports the @deepseek-ai/dsh-mcp-client plugin is missing, run: dsh plugin --profile <name> add @deepseek-ai/dsh-mcp-client."
        }
    }
}

/// A capture confirmation must identify one client event after this setup
/// attempt. The caller supplies a supported observation, never service liveness.
struct SetupCaptureConfirmation: Equatable, Codable {
    let clientID: String
    let eventID: String
    let observedAt: Date
    let taskID: String?
    let clientSessionID: String?
    let sessionKey: String?

    init(
        clientID: String,
        eventID: String,
        observedAt: Date,
        taskID: String?,
        clientSessionID: String? = nil,
        sessionKey: String? = nil
    ) {
        self.clientID = clientID
        self.eventID = eventID
        self.observedAt = observedAt
        self.taskID = taskID
        self.clientSessionID = clientSessionID
        self.sessionKey = sessionKey
    }

    /// Old persisted confirmations have no session identity and remain valid
    /// capture history, but cannot acquire a task link through inference.
    var exactSessionKey: String? {
        if let clientSessionID, !clientSessionID.isEmpty {
            let joined = "\(clientID)::\(clientSessionID)"
            guard sessionKey == nil || sessionKey == joined else { return nil }
            return joined
        }
        guard let sessionKey, sessionKey.hasPrefix("\(clientID)::"),
              sessionKey.count > clientID.count + 2 else { return nil }
        return sessionKey
    }

    func associatingTask(_ taskID: String) -> Self {
        Self(clientID: clientID, eventID: eventID, observedAt: observedAt, taskID: taskID,
             clientSessionID: clientSessionID, sessionKey: sessionKey)
    }

    func confirms(client: SetupClient?, after boundary: Date?) -> Bool {
        guard let client, let boundary, !eventID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return false
        }
        return clientID == client.rawValue && observedAt > boundary
    }
}

enum NativeRecoveryKind: Equatable {
    case connection
    case synchronization
}

/// Setup, preview, and saved activation output use the same scalable geometry.
/// Larger text keeps a similar amount of output visible, while controls move
/// into a column before their labels compete for the window's width.
struct NativeSetupLayout: DynamicProperty {
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @ScaledMetric(relativeTo: .caption) private var scaledOutputHeight: CGFloat = 180
    @ScaledMetric(relativeTo: .caption) private var scaledGeneratedContentHeight: CGFloat = 220
    @ScaledMetric(relativeTo: .caption) private var scaledStepDiameter: CGFloat = 24
    @ScaledMetric(relativeTo: .body) private var scaledIconColumnWidth: CGFloat = 26

    var outputHeight: CGFloat {
        WorkTypeScale.resolved(base: 180, systemScaled: scaledOutputHeight, dynamicTypeSize: dynamicTypeSize)
    }
    var generatedContentHeight: CGFloat {
        WorkTypeScale.resolved(base: 220, systemScaled: scaledGeneratedContentHeight, dynamicTypeSize: dynamicTypeSize)
    }
    var stepDiameter: CGFloat {
        WorkTypeScale.resolved(base: 24, systemScaled: scaledStepDiameter, dynamicTypeSize: dynamicTypeSize)
    }
    var iconColumnWidth: CGFloat {
        WorkTypeScale.resolved(base: 26, systemScaled: scaledIconColumnWidth, dynamicTypeSize: dynamicTypeSize)
    }

    var stacksControls: Bool { dynamicTypeSize.isAccessibilitySize }

    func row<Content: View>(spacing: CGFloat, @ViewBuilder content: () -> Content) -> some View {
        let layout = stacksControls
            ? AnyLayout(VStackLayout(alignment: .leading, spacing: spacing))
            : AnyLayout(HStackLayout(spacing: spacing))
        return layout { content() }
    }
}

/// Keep setup action labels and targets scalable on macOS, where the native
/// bordered styles can replace an inherited font with a fixed control size.
///
/// The prominent variant is folded into `PrimaryButtonStyle` (C36): the one
/// accent-filled control with an `onAccent` label. `prominent: true` remains
/// only as a forwarding spelling so existing call sites render identically.
struct NativeSetupActionStyle: ButtonStyle {
    var prominent = false

    @ViewBuilder
    func makeBody(configuration: Configuration) -> some View {
        if prominent {
            PrimaryButtonStyle().makeBody(configuration: configuration)
        } else {
            NativeSetupActionBody(configuration: configuration)
        }
    }
}

private struct NativeSetupActionBody: View {
    let configuration: ButtonStyleConfiguration
    /// Hover and press feedback follows the shared `ButtonFeedback` ramp, the
    /// same interaction wash `QuietButtonStyle` applies to its tint.
    private let feedbackTint = Theme.accent
    @State private var hovering = false
    @Environment(\.isEnabled) private var isEnabled
    @Environment(\.isFocused) private var isFocused
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @ScaledMetric(relativeTo: .body) private var horizontalPadding: CGFloat = 16
    @ScaledMetric(relativeTo: .body) private var verticalPadding: CGFloat = 8
    @ScaledMetric(relativeTo: .body) private var minimumHeight: CGFloat = 36

    init(configuration: ButtonStyleConfiguration) {
        self.configuration = configuration
    }

    private var phase: ButtonInteractionPhase {
        buttonInteractionPhase(isEnabled: isEnabled, isPressed: configuration.isPressed, isHovering: hovering)
    }

    var body: some View {
        configuration.label
            .workFont(.body)
            .fixedSize(horizontal: false, vertical: true)
            .foregroundStyle(Theme.ink)
            .padding(.horizontal, WorkTypeScale.resolved(base: 16, systemScaled: horizontalPadding, dynamicTypeSize: dynamicTypeSize))
            .padding(.vertical, WorkTypeScale.resolved(base: 8, systemScaled: verticalPadding, dynamicTypeSize: dynamicTypeSize))
            .frame(minHeight: WorkTypeScale.resolved(base: 36, systemScaled: minimumHeight, dynamicTypeSize: dynamicTypeSize))
            .background {
                RoundedRectangle(cornerRadius: Metrics.radius)
                    .fill(Theme.thumb)
                    .overlay {
                        RoundedRectangle(cornerRadius: Metrics.radius)
                            .fill(feedbackTint.opacity(ButtonFeedback.surfaceFillOpacity(for: phase)))
                    }
                    .overlay {
                        RoundedRectangle(cornerRadius: Metrics.radius)
                            .strokeBorder(Theme.cardLine, lineWidth: Metrics.borderW)
                    }
            }
            .opacity(ButtonFeedback.labelOpacity(for: phase))
            .overlay {
                if isFocused && isEnabled {
                    RoundedRectangle(cornerRadius: Metrics.radius + 3)
                        .strokeBorder(Theme.accent, lineWidth: Metrics.focusW)
                        .padding(-3)
                }
            }
            .contentShape(RoundedRectangle(cornerRadius: Metrics.radius))
            .onHover { hovering = $0 }
            .animation(Motion.feedback, value: phase)
    }
}

/// Production setup occupies the window. It invokes the existing installer only
/// after the user has reviewed a named client's configuration changes.
@MainActor
struct NativeSetupFlow: View {
    @ObservedObject var setup: SetupModel
    let onClose: () -> Void
    let onOpenWork: () -> Void
    let capture: SetupCaptureConfirmation?
    let onOpenCapture: ((String) -> Void)?
    let canViewSavedWork: Bool
    let recoveryReason: String?
    let recoveryKind: NativeRecoveryKind
    let recoveryUnavailableReasonOverride: String?
    let onReconnect: (() async -> Bool)?
    private let runSetup: () async -> Void

    @State private var selectedClient: SetupClient
    @State private var reviewing = false
    @State private var setupTask: Task<Void, Never>?
    @State private var showingLog = false
    @State private var reconnectResult: Bool?
    @State private var reconnectTask: Task<Void, Never>?
    @State private var contentPreviewState: SetupContentPreviewState = .idle
    @State private var contentPreviewExpanded = false
    @State private var contentPreviewRetry = 0
    private let suppliedContentPreview: SetupContentPreview?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private var layout = NativeSetupLayout()
    @AccessibilityFocusState private var headingFocused: Bool

    init(
        setup: SetupModel,
        onClose: @escaping () -> Void,
        runSetup: (() async -> Void)? = nil,
        capture: SetupCaptureConfirmation? = nil,
        onOpenCapture: ((String) -> Void)? = nil,
        canViewSavedWork: Bool = true,
        initialReview: Bool = false,
        onOpenWork: (() -> Void)? = nil,
        recoveryReason: String? = nil,
        recoveryKind: NativeRecoveryKind = .connection,
        recoveryUnavailableReasonOverride: String? = nil,
        onReconnect: (() async -> Bool)? = nil,
        reviewRecoveryResult: Bool? = nil,
        contentPreview: SetupContentPreview? = nil
    ) {
        self.setup = setup
        self.onClose = onClose
        self.onOpenWork = onOpenWork ?? onClose
        self.capture = capture
        self.onOpenCapture = onOpenCapture
        self.canViewSavedWork = canViewSavedWork
        self.recoveryReason = recoveryReason
        self.recoveryKind = recoveryKind
        self.recoveryUnavailableReasonOverride = recoveryUnavailableReasonOverride
        self.onReconnect = onReconnect
        suppliedContentPreview = contentPreview
        if SnapshotMode.enabled {
            if let contentPreview, contentPreview.client == (setup.selectedClient ?? .codex).rawValue {
                _contentPreviewState = State(initialValue: .available(contentPreview))
                _contentPreviewExpanded = State(initialValue: true)
            } else {
                _contentPreviewState = State(initialValue: .unavailable("A generated content fixture was not supplied for this native review."))
            }
        }
        _selectedClient = State(initialValue: setup.selectedClient ?? .codex)
        _reviewing = State(initialValue: initialReview)
        _reconnectResult = State(initialValue: SnapshotMode.enabled ? reviewRecoveryResult : nil)
        if SnapshotMode.enabled && SnapshotMode.reviewExpandSetupDetails {
            _showingLog = State(initialValue: true)
        }
        if case .failed = setup.phase { _showingLog = State(initialValue: true) }
        self.runSetup = runSetup ?? {
            if case .failed = setup.phase { setup.reset() }
            if case .done = setup.phase { setup.reset() }
            await setup.setUp()
        }
    }

    private var plan: SetupConfigurationPlan {
        if SnapshotMode.enabled {
            return SetupConfigurationPlan(
                client: selectedClient,
                homeDirectory: URL(fileURLWithPath: "/Users/review", isDirectory: true),
                environment: [:],
                fileExists: { _ in false }
            )
        }
        return SetupConfigurationPlan(client: selectedClient)
    }
    private var savedWorkLabel: String { canViewSavedWork ? "View saved work" : "Back to app" }
    private var openSavedWork: () -> Void { canViewSavedWork ? onOpenWork : onClose }
    private var isRecovery: Bool { recoveryReason != nil || recoveryKind == .synchronization }
    private var activeLog: [String] { isRecovery && recoveryKind == .connection ? setup.reconnectLog : setup.log }
    private var isReconnecting: Bool {
        reconnectTask != nil || (recoveryKind == .synchronization ? isWorking : setup.reconnectPhase == .working)
    }
    private var recoveryUnavailableReason: String? {
        if let recoveryUnavailableReasonOverride { return recoveryUnavailableReasonOverride }
        if recoveryKind == .connection { return setup.presentation.reconnectUnavailableReason }
        return setup.presentation.canRunInteractiveSetup ? nil
            : "This build has no validated recorder payload for update recovery. Open the packaged agentacct app that owns this installation to resume the update."
    }
    /// True while the reason is only the placeholder the recorder check
    /// leaves behind before it answers.
    private var recoveryCheckIsPending: Bool {
        recoveryUnavailableReason == SetupModel.Presentation.pendingRecorderCheckReason
    }
    /// Why the Reconnect button is disabled, in the same words the page shows
    /// above it — a disabled action always says why (K53).
    private var disabledRecoveryReason: String? {
        guard !canAttemptRecovery, !isReconnecting else { return nil }
        if let recoveryUnavailableReason { return recoveryUnavailableReason }
        if onReconnect == nil {
            return "Recovery is unavailable in this window. Reopen Connections from the main app."
        }
        return nil
    }
    private var canAttemptRecovery: Bool {
        !isReconnecting && !isWorking && setup.reconnectPhase != .working
            && recoveryUnavailableReason == nil && onReconnect != nil
    }
    private var recoveryFailureMessage: String? {
        if recoveryKind == .synchronization, case .failed(let message) = setup.phase { return message }
        if recoveryKind == .connection, case .failed(let message) = setup.reconnectPhase { return message }
        return nil
    }
    private var recoveryTitle: String {
        if recoveryKind == .synchronization {
            return isReconnecting ? "Recovering recorder update" : reconnectResult == true ? "Recorder update recovered" : "Recover recorder update"
        }
        return isReconnecting ? "Reconnecting the recorder" : reconnectResult == true ? "Recorder reconnected" : "Reconnect recording"
    }
    private var recoveryActionTitle: String {
        if recoveryKind == .synchronization { return isReconnecting ? "Recovering…" : "Retry recovery" }
        return isReconnecting ? "Reconnecting…" : reconnectResult == false ? "Retry reconnect" : "Reconnect recorder"
    }
    private var isWorking: Bool {
        if case .working = setup.phase { return true }
        return setupTask != nil
    }
    private var captureBoundary: Date? {
        [setup.onboardingCompletedAt, setup.reconnectCompletedAt].compactMap { $0 }.max()
    }
    private var captureConfirmed: Bool {
        capture?.confirms(client: setup.selectedClient, after: captureBoundary) == true
    }
    private var phaseKey: SetupPhaseKey { setupPhaseKey(for: setup.phase) }
    private var contentPreviewRequestID: String {
        reviewing && phaseKey == .idle && !isRecovery ? "\(selectedClient.rawValue):\(contentPreviewRetry)" : "inactive"
    }
    private var selectedClientContentPreview: SetupContentPreviewState {
        if case .available(let preview) = contentPreviewState, preview.client != selectedClient.rawValue {
            return SnapshotMode.enabled
                ? .unavailable("A generated content fixture is not available for \(selectedClient.title).")
                : .loading
        }
        return contentPreviewState
    }
    private var stageIndex: Int {
        switch setup.phase {
        case .idle: return reviewing ? 1 : 0
        case .working: return 2
        case .failed: return 2
        case .done: return 3
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            layout.row(spacing: Space.m) {
                Label(isRecovery ? (recoveryKind == .synchronization ? "Recorder update recovery" : "Recorder connection") : "Recording setup", systemImage: "waveform.path")
                    .workFont(.rowLabel)
                    .foregroundStyle(Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)
                if !layout.stacksControls { Spacer() }
                Button(savedWorkLabel, action: openSavedWork)
                    .buttonStyle(QuietButtonStyle(tint: Theme.accent))
                    .help(canViewSavedWork ? "Return to Work. Setup can be reopened from Connections in recording health." : "Return to the app. Recorder recovery must finish before local work can load.")
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, Space.gutter)
            .padding(.vertical, Space.l)
            Divider().overlay(Theme.cardLine)

            ScrollView {
                VStack(alignment: .leading, spacing: Space.xl) {
                    if !isRecovery { progressNavigation }
                    pageContent
                    if !activeLog.isEmpty { setupOutput }
                }
                .frame(maxWidth: 880, alignment: .leading)
                .padding(Space.gutter)
                .frame(maxWidth: .infinity, alignment: .center)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)

            Divider().overlay(Theme.cardLine)
            Group {
                if isRecovery { recoveryFooter } else { footer }
            }
                .padding(.horizontal, Space.gutter)
                .padding(.vertical, Space.l)
                .background(Theme.chrome)
        }
        .workFont(.body)
        .background(Theme.canvas)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("native-setup-flow")
        .onChange(of: phaseKey) {
            headingFocused = true
            if phaseKey == .failed { showingLog = true }
        }
        .task {
            guard !SnapshotMode.enabled else { return }
            setup.refreshPresentation()
        }
        .task(id: contentPreviewRequestID) {
            guard contentPreviewRequestID != "inactive" else { return }
            if SnapshotMode.enabled {
                if let suppliedContentPreview, suppliedContentPreview.client == selectedClient.rawValue {
                    contentPreviewState = .available(suppliedContentPreview)
                } else {
                    contentPreviewState = .unavailable("A generated content fixture is not available for \(selectedClient.title).")
                }
                return
            }
            contentPreviewState = .loading
            let result = await setup.previewSetupContent(for: selectedClient)
            guard !Task.isCancelled else { return }
            contentPreviewState = result
        }
        // Leaving the route does not cancel the protected installer transaction.
        // The running task finishes and SetupModel retains its recovery state.
    }

    @ViewBuilder private var progressNavigation: some View {
        if layout.stacksControls {
            Text("Step \(stageIndex + 1) of 4 · \(["Choose client", "Review", "Set up", "First capture"][stageIndex])")
                .workFont(.captionSemibold).foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
        } else {
            fullProgressNavigation
        }
    }

    private var fullProgressNavigation: some View {
        HStack(spacing: Space.m) {
            ForEach(Array(["Choose client", "Review", "Set up", "First capture"].enumerated()), id: \.offset) { index, title in
                HStack(spacing: Space.s) {
                    Text("\(index + 1)")
                        .workFont(.dataSmallSemibold)
                        .foregroundStyle(index == stageIndex ? Theme.onAccent : Theme.muted)
                        .frame(width: layout.stepDiameter, height: layout.stepDiameter)
                        .background(index == stageIndex ? Theme.accent : Theme.tintNeutral, in: Circle())
                    Text(title).workFont(index == stageIndex ? .captionSemibold : .caption)
                        .foregroundStyle(index == stageIndex ? Theme.ink : Theme.muted)
                }
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("Step \(index + 1), \(title)\(index == stageIndex ? ", current" : "")")
                if index < 3 { Spacer(minLength: 0) }
            }
        }
    }

    @ViewBuilder private var pageContent: some View {
        if isRecovery {
            recoveryContent
        } else {
            switch setup.phase {
            case .idle:
                if reviewing { reviewContent } else { welcomeContent }
            case .working(let status):
                installationContent(status: status)
            case .failed(let message):
                failureContent(message)
            case .done:
                captureContent
            }
        }
    }

    private var recoveryContent: some View {
        VStack(alignment: .leading, spacing: Space.xl) {
            heading(recoveryTitle, detail: recoveryKind == .synchronization
                ? (reconnectResult == true
                   ? "The protected recorder update finished. Fresh client capture remains a separate check."
                   : "Resume the interrupted recorder update before connecting to its local work store.")
                : (reconnectResult == true
                   ? "The recorder endpoint and watcher responded. Fresh client capture remains a separate check."
                   : "Restore the connection to the local recorder so new work can appear in this window."))
            if let recoveryReason {
                informationRow(symbol: recoveryKind == .synchronization ? "arrow.triangle.2.circlepath" : "network.slash",
                               title: reconnectResult == true ? "Previous recovery issue" : recoveryKind == .synchronization ? "Recorder update needs attention" : "Connection needs attention",
                               detail: recoveryReason, tint: reconnectResult == true ? Theme.muted : Theme.amber)
            }
            if isReconnecting {
                stageRow(symbol: "arrow.clockwise",
                         title: recoveryKind == .synchronization ? "Recovering the app-owned recorder" : "Starting and checking the recorder",
                         detail: recoveryProgressDetail, active: true)
            } else if reconnectResult == true {
                if recoveryKind == .synchronization {
                    informationRow(symbol: "clock", title: "Check recording in Work",
                                   detail: "Open Work to inspect recorded events. Completing the recorder update does not itself confirm a fresh capture from your client.", tint: Theme.amber)
                } else {
                    informationRow(symbol: captureConfirmed ? "checkmark.circle" : "clock", title: captureConfirmed ? "Fresh capture received" : "Waiting for fresh client capture", detail: captureConfirmed
                        ? "A new event from the selected client was observed after reconnect."
                        : "Run a small task in your client and check Work for a newly recorded event. Reconnecting the endpoint does not confirm capture.", tint: captureConfirmed ? Theme.green : Theme.amber)
                }
            } else if reconnectResult == false {
                informationRow(symbol: "exclamationmark.triangle", title: recoveryKind == .synchronization ? "Recovery did not finish" : "Reconnect did not finish",
                               detail: recoveryFailureMessage ?? "The recovery check did not succeed. Review the output, then try again.", tint: Theme.coral)
            }
            if reconnectResult != true, let unavailable = recoveryUnavailableReason {
                // A check still in flight is a PENDING state, not a missing
                // feature: "Recovery unavailable here" used to sit directly
                // above "Checking the installed recorder…" (K53).
                if recoveryCheckIsPending {
                    informationRow(
                        symbol: "clock",
                        title: "Checking recorder…",
                        detail: unavailable
                    )
                } else {
                    informationRow(symbol: "shippingbox", title: "Recovery unavailable here", detail: unavailable, tint: Theme.amber)
                }
            } else if reconnectResult != true {
                informationRow(symbol: "gearshape",
                               title: recoveryKind == .synchronization ? "Resume the protected update" : "Reconnect the recorder only",
                               detail: recoveryKind == .synchronization
                               ? "Retry uses the app's recorder synchronization and transaction recovery checks. The output reports the update stage and any remaining issue."
                               : "Starts the verified app-owned recorder and checks its endpoint. This action does not reinstall the recorder or refresh client MCP settings, hooks, or instructions.")
                if onReconnect == nil {
                    Text("Recovery is unavailable in this window. Reopen Connections from the main app.")
                        .workFont(.body).foregroundStyle(Theme.muted)
                }
            }
            Text(reconnectResult == true
                 ? (canViewSavedWork ? "Saved work is available." : "Recorder recovery finished. Return to the app to check Work availability.")
                 : (canViewSavedWork ? "Your saved work is available while you resolve the connection." : "Your work remains stored locally. This window can load it after recorder recovery finishes."))
                .workFont(.body).foregroundStyle(Theme.muted)
        }
        .accessibilityIdentifier("recorder-recovery")
    }

    private var recoveryProgressDetail: String {
        if recoveryKind == .synchronization, case .working(let status) = setup.phase { return status }
        return "The app verifies recorder ownership and the local store before and after the command."
    }

    private var recoveryFooter: some View {
        layout.row(spacing: Space.l) {
            // "View saved work" already sits in this page's header and stays
            // there through every recovery state, so the footer repeated it
            // on the same screen (K53). The footer keeps the way out and the
            // one primary action.
            Button("Back to app", action: onClose).buttonStyle(NativeSetupActionStyle())
            if !layout.stacksControls { Spacer() }
            if reconnectResult == true {
                Button(canViewSavedWork ? "Open Work" : "Back to app", action: openSavedWork)
                    .buttonStyle(PrimaryButtonStyle())
            } else {
                Button(recoveryActionTitle, action: startReconnect)
                    .buttonStyle(PrimaryButtonStyle())
                    .disabled(!canAttemptRecovery)
                    .keyboardShortcut(.defaultAction)
                    .modifier(OptionalAccessibilityHint(hint: disabledRecoveryReason))
                    .accessibilityIdentifier("reconnect-recorder")
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func startReconnect() {
        guard canAttemptRecovery, let onReconnect else { return }
        reconnectResult = nil
        showingLog = true
        reconnectTask = Task {
            reconnectResult = await onReconnect()
            reconnectTask = nil
            headingFocused = true
        }
    }

    private func heading(_ title: String, detail: String) -> some View {
        VStack(alignment: .leading, spacing: Space.m) {
            Text(title).workFont(.titlePage).tracking(Type.titlePageTracking)
                .foregroundStyle(Theme.ink)
                .accessibilityAddTraits(.isHeader)
                .accessibilityFocused($headingFocused)
            Text(detail).workFont(.body).foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var welcomeContent: some View {
        VStack(alignment: .leading, spacing: Space.xl) {
            heading("Connect your coding client", detail: "See recorded activity, checks and imported usage together in Work.")
            VStack(alignment: .leading, spacing: Space.l) {
                Text("Choose a client").workFont(.titleCard).foregroundStyle(Theme.ink)
                Text("You can connect more clients later.")
                    .workFont(.body).foregroundStyle(Theme.muted)
                Picker("Coding client", selection: $selectedClient) {
                    ForEach(SetupClient.allCases) { client in
                        Text(client.title).tag(client)
                    }
                }
                .pickerStyle(.radioGroup)
                .workFont(.body)
                .accessibilityIdentifier("setup-client-picker")
                .labelsHidden()
            }
            .padding(Space.cardPad)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
            .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Theme.cardLine))
            informationRow(symbol: "internaldrive", title: "A local work record", detail: "The recorder uses a local store on this Mac. Setup does not need an API key.")
            if !setup.presentation.canRunInteractiveSetup { unavailableInstaller }
        }
    }

    private var reviewContent: some View {
        VStack(alignment: .leading, spacing: Space.xl) {
            heading("Review changes for \(selectedClient.title)", detail: "Install and connect applies these changes to your user account.")
            informationRow(
                symbol: "list.bullet.rectangle",
                title: "Planned scope",
                detail: plan.scopeSummary
            )
            Text("Unrelated settings are preserved. Some files may need manual steps if they cannot be merged safely. No project configuration is added.")
                .workFont(.caption).foregroundStyle(Theme.muted).fixedSize(horizontal: false, vertical: true)
            VStack(alignment: .leading, spacing: 0) {
                ForEach(plan.changes) { change in
                    VStack(alignment: .leading, spacing: Space.s) {
                        Text(change.path).workFont(size: 13, weight: .regular, relativeTo: .body, monospaced: true).foregroundStyle(Theme.ink)
                            .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                        Text(change.action).workFont(.body).foregroundStyle(Theme.muted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(Space.l)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    if change.id != plan.changes.last?.id { Divider().overlay(Theme.cardLine) }
                }
            }
            .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
            .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Theme.cardLine))
            NativeSetupContentPreviewView(state: selectedClientContentPreview, onRetry: { contentPreviewRetry += 1 }, isExpanded: $contentPreviewExpanded)
            DisclosureGroup("Recorder locations and merge behavior") {
                VStack(alignment: .leading, spacing: Space.m) {
                    Text("Recorder: ~/.local/share/agentacct\nLauncher: ~/.local/bin/agentacct")
                        .workFont(.dataSmall).textSelection(.enabled)
                    if let store = setup.recordingStorePath {
                        Text("Recording store: \(store)").workFont(.dataSmall).textSelection(.enabled)
                    }
                    Text("Setup preserves unrelated settings and replaces agentacct's managed entries. Files that cannot be merged safely may be skipped. Review setup output for manual steps. No project configuration files are added.")
                        .workFont(.caption).fixedSize(horizontal: false, vertical: true)
                }
                .foregroundStyle(Theme.muted).padding(.top, Space.s)
            }
            .workFont(.caption)
            if !setup.presentation.canRunInteractiveSetup { unavailableInstaller }
        }
    }

    private func installationContent(status: String) -> some View {
        VStack(alignment: .leading, spacing: Space.xl) {
            heading("Connecting \(setup.selectedClient?.title ?? "your recorder")", detail: status)
            VStack(alignment: .leading, spacing: Space.xl) {
                stageRow(symbol: setup.isRunningOnboard ? "checkmark.circle" : "circle.dotted", title: "Prepare the recorder", detail: "Validate the packaged recorder and safely install or recover the app-managed version.", active: !setup.isRunningOnboard)
                stageRow(symbol: "gearshape.2", title: "Configure and start", detail: "Run onboarding for the selected client and start local sync. The output below reports any skipped configuration.", active: setup.isRunningOnboard)
                stageRow(symbol: "clock", title: "Wait for the first capture", detail: "A new client session must produce a fresh recorded event.", active: false)
            }
            .padding(Space.cardPad)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
            Text(canViewSavedWork ? "You can view saved work while setup continues. The installer retains its recovery state if the app is interrupted." : "Saved work remains stored locally. Recorder recovery must finish before this window can load it. The installer retains its recovery state if the app is interrupted.")
                .workFont(.body).foregroundStyle(Theme.muted)
        }
    }

    private func failureContent(_ message: String) -> some View {
        VStack(alignment: .leading, spacing: Space.xl) {
            heading("\(setup.selectedClient.map { $0.title + " setup" } ?? "Recorder setup") needs attention", detail: message)
            Text("If setup output asks for client consent, complete that step in the client before retrying.")
                .workFont(.body).foregroundStyle(Theme.muted).fixedSize(horizontal: false, vertical: true)
            if !setup.presentation.canRunInteractiveSetup { unavailableInstaller }
        }
    }

    private var captureContent: some View {
        VStack(alignment: .leading, spacing: Space.xl) {
            heading(captureConfirmed ? "Fresh capture received" : "Waiting for your first capture", detail: captureConfirmed
                ? "A recorded event from \(setup.selectedClient?.title ?? "the selected client") arrived after this setup."
                : "The setup command finished. Capture from \(setup.selectedClient?.title ?? "your client") has not yet been confirmed.")
            if captureConfirmed {
                Text("This confirms one recorded event. Check Sources for separate usage, identity or history issues.")
                    .workFont(.body).foregroundStyle(Theme.muted)
            }
            if !captureConfirmed {
                VStack(alignment: .leading, spacing: Space.l) {
                    informationRow(symbol: "1.circle", title: "Activate \(setup.selectedClient?.title ?? "your client")", detail: setup.selectedClient == nil
                        ? "Open a new session in a configured client so it loads its recording integration. Check the setup output for any required consent steps."
                        : plan.activationInstruction)
                    informationRow(symbol: "2.circle", title: "Run a small task", detail: "In the new session, ask the agent to inspect a project and record a short work section with agentacct.")
                    informationRow(symbol: "3.circle", title: "Check the recorded work", detail: "Open Work to inspect the new session's evidence. If it does not arrive, inspect the setup output and Sources for the reported cause.")
                }
            }
            DisclosureGroup("Capture check details") {
                VStack(alignment: .leading, spacing: Space.s) {
                    Text("Client: \(setup.selectedClient?.title ?? "not selected")")
                    Text(captureBoundary.map { "Required after: \($0.ISO8601Format())" } ?? "Capture boundary unavailable; fresh capture cannot be confirmed.")
                    if let capture, captureConfirmed {
                        Text("Observed: \(capture.observedAt.ISO8601Format())")
                        Text("Event: \(capture.eventID)")
                    }
                }
                .workFont(.dataSmall).foregroundStyle(Theme.muted)
                .textSelection(.enabled).padding(.top, Space.s)
            }
            .workFont(.caption)
            .accessibilityIdentifier("setup.capture-details")
        }
    }

    private var unavailableInstaller: some View {
        informationRow(symbol: "shippingbox", title: "Packaged installer unavailable", detail: "This build does not contain a validated recorder payload. Use a packaged agentacct app to install from this screen. In a development build, start the development backend separately; saved work remains available when it is connected.", tint: Theme.amber)
    }

    private func informationRow(symbol: String, title: String, detail: String, tint: Color = Theme.accent) -> some View {
        HStack(alignment: .top, spacing: Space.m) {
            Image(systemName: symbol).workFont(size: 19, weight: .regular, relativeTo: .body).foregroundStyle(tint)
                .frame(width: layout.iconColumnWidth).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: Space.s) {
                Text(title).workFont(.rowLabel).foregroundStyle(Theme.ink)
                Text(detail).workFont(.body).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func stageRow(symbol: String, title: String, detail: String, active: Bool) -> some View {
        HStack(alignment: .top, spacing: Space.m) {
            Group {
                if active && !reduceMotion {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: symbol).foregroundStyle(active ? Theme.accent : Theme.muted)
                }
            }
            .workFont(size: 19, weight: .regular, relativeTo: .body)
            .frame(width: layout.iconColumnWidth)
            .frame(minHeight: layout.stepDiameter).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: Space.s) {
                Text(title + (active ? " · In progress" : "")).workFont(.rowLabel).foregroundStyle(Theme.ink)
                Text(detail).workFont(.body).foregroundStyle(Theme.muted).fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var setupOutput: some View {
        DisclosureGroup(isExpanded: $showingLog) {
            VStack(alignment: .leading, spacing: Space.m) {
                layout.row(spacing: Space.m) {
                    Text(isRecovery ? (recoveryKind == .synchronization ? "Recorder update recovery diagnostics." : "Recorder connection diagnostics.") : "Review warnings and any client-side activation steps.")
                        .workFont(.caption).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                    if !layout.stacksControls { Spacer() }
                    Button("Copy output") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(activeLog.joined(separator: "\n"), forType: .string)
                    }.buttonStyle(NativeSetupActionStyle())
                }
                ScrollView([.horizontal, .vertical]) {
                    Text(activeLog.joined(separator: "\n"))
                        .workFont(.dataSmall).foregroundStyle(Theme.ink)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(Space.m)
                }
                .frame(height: layout.outputHeight)
                .background(Theme.chrome, in: RoundedRectangle(cornerRadius: Metrics.radius))
                .accessibilityIdentifier("setup-output-viewport")
            }
            .padding(.top, Space.m)
        } label: {
            Text("\(isRecovery ? (recoveryKind == .synchronization ? "Recovery" : "Reconnect") : "Setup") output · \(Fmt.count(activeLog.count, "line"))").workFont(.rowLabel).foregroundStyle(Theme.ink)
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("setup-output")
    }

    @ViewBuilder private var footer: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            if reviewing && phaseKey == .idle {
                Text(plan.scopeSummary)
                    .workFont(.caption).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("setup-install-scope")
                Text("Files requiring a manual merge may be skipped; setup output identifies the next steps.")
                    .workFont(.caption).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            } else if phaseKey == .failed {
                Text("Some changes may already be applied. Retry checks the existing installation.")
                    .workFont(.caption).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: Space.l) {
                if reviewing && phaseKey == .idle {
                    Button("Back") { reviewing = false; headingFocused = true }
                        .buttonStyle(NativeSetupActionStyle())
                } else {
                    ContextHelp(title: "Return to setup later", message: "Setup can be reopened from Connections in recording health. Saved work remains available while the recorder reconnects.", identifier: "setup.return-help")
                }
                Spacer(minLength: Space.m)
                switch setup.phase {
                case .idle:
                    if reviewing {
                        Button("Install and connect", action: startSetup)
                            .buttonStyle(PrimaryButtonStyle())
                            .disabled(!setup.presentation.canRunInteractiveSetup || isWorking)
                            .keyboardShortcut(.defaultAction)
                            .accessibilityIdentifier("setup-install-and-connect")
                    } else {
                        Button("Review changes") {
                            reviewing = true
                            headingFocused = true
                        }
                        .buttonStyle(PrimaryButtonStyle())
                        .keyboardShortcut(.defaultAction)
                    }
                case .working:
                    Button(savedWorkLabel, action: openSavedWork).buttonStyle(NativeSetupActionStyle())
                case .failed:
                    Button("Retry setup", action: startSetup)
                        .buttonStyle(PrimaryButtonStyle())
                        .disabled(!setup.presentation.canRunInteractiveSetup || isWorking)
                        .keyboardShortcut(.defaultAction)
                case .done:
                    Button(setup.selectedClient == nil ? "Connect a client" : "Connect another client") {
                        setup.reset()
                        reviewing = false
                        headingFocused = true
                    }
                    .buttonStyle(NativeSetupActionStyle())
                    if canViewSavedWork, captureConfirmed, let taskID = capture?.taskID, let onOpenCapture {
                        Button("Open captured work") { onOpenCapture(taskID) }
                            .buttonStyle(PrimaryButtonStyle())
                            .keyboardShortcut(.defaultAction)
                    } else {
                        Button(canViewSavedWork ? "Open Work" : "Back to app", action: openSavedWork)
                            .buttonStyle(PrimaryButtonStyle())
                            .keyboardShortcut(.defaultAction)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func startSetup() {
        guard !isWorking, setup.canRunInteractiveSetup else { return }
        // A failed automatic recorder update can enter this route without any
        // client review. Its Retry must recover that operation, not silently
        // configure the picker's default client.
        if reviewing || setup.selectedClient != nil {
            setup.selectClientForSetup(selectedClient)
        }
        showingLog = true
        setupTask = Task {
            await runSetup()
            setupTask = nil
        }
    }
}
