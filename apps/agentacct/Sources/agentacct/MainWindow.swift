import AppKit
import SwiftUI

// The full window follows the system appearance through semantic Theme tokens.
// Custom chrome keeps navigation quiet; the menu bar is the glance and this
// window is where work evidence and usage details live.
//
// Panes live in their own files: DashboardPane (the home), WorkPane (Tasks +
// their session drill-down), the merged UsagePane, and SourcesPane.

struct MainWindow: View {
    @Environment(DashboardStore.self) var dashboard
    @Environment(GlanceState.self) var glance
    @Environment(AppSelection.self) var selection
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @StateObject private var setup: SetupModel
    @State private var showSetup = false
    @State private var setupRecoveryReason: String?
    @State private var setupRecoveryKind: NativeRecoveryKind = .connection
    @State private var activationClient: SetupClient?
    @State private var savedWork: SavedWorkSnapshot?
    @State private var offlineDashboard: DashboardStore?
    @State private var openWorkAfterSetup = false
    @State private var recorderSynchronizationFinished = false
    @State private var healthCoordinator = RecordingHealthCoordinator()
    @State private var connectionHistory = RecordingConnectionHistory.load()
    /// Design-review renders cannot infer whether the executable was packaged
    /// with the recorder. Live windows leave this nil and use SetupModel.
    var canSetUpOverride: Bool?
    private let lifecycle: AppLifecycleCoordinator?

    init(
        setup: SetupModel? = nil,
        lifecycle: AppLifecycleCoordinator? = nil,
        canSetUpOverride: Bool? = nil
    ) {
        _setup = StateObject(wrappedValue: setup ?? SetupModel())
        self.lifecycle = lifecycle
        self.canSetUpOverride = canSetUpOverride
    }

    private var canSetUp: Bool {
        canSetUpOverride ?? setup.presentation.canSetUp
    }

    private var reconnectStoreExplanation: String? {
        RecorderDisplayStoreGate.explanation(display: try? GlanceClient.storeDir(), managedPath: setup.recordingStorePath)
    }

    /// The one-click restart control for the always-visible health surfaces.
    /// Present only when the app owns a recorder it can actually start (a matching
    /// packaged CLI, and the displayed store is the managed one). Otherwise the
    /// unreachable cause keeps its existing "Open Connections" path, and it is
    /// suppressed entirely in deterministic snapshot renders.
    private var recorderRestart: RecorderRestartControl? {
        guard !SnapshotMode.enabled,
              setup.canReconnectRecorder,
              reconnectStoreExplanation == nil else { return nil }
        return RecorderRestartControl(
            inFlight: setup.reconnectPhase == .working,
            onRestart: { restartRecorderFromHealth() }
        )
    }

    private var canViewSavedWork: Bool {
        recorderSynchronizationFinished || SnapshotMode.enabled || savedWork?.hasWork == true
    }

    private var health: RecordingHealthSnapshot {
        RecordingHealthSnapshot.project(
            glancePhase: glance.phase,
            setupPhase: setup.phase,
            ingestion: dashboard.ingestion,
            ingestionError: dashboard.ingestionError,
            canSetUp: canSetUp,
            needsSetup: setup.presentation.needsSetup,
            configuredClientIDs: Array(connectionHistory.boundaries.keys).sorted(),
            captures: connectionHistory.captures.values.map { RecordingCaptureObservation(clientID: $0.clientID, eventID: $0.eventID, observedAt: $0.observedAt, taskID: $0.taskID) },
            requiredCaptureAfter: connectionHistory.boundaries
        )
    }

    var body: some View {
        VStack(spacing: 0) {
            TopBar(
                canSetUp: canSetUp,
                health: health,
                healthCoordinator: healthCoordinator,
                restart: recorderRestart,
                onActivateClient: { openActivation($0) },
                onSetupCause: { openRecordingSetup(cause: $0) },
                awaitRecorderSynchronization: {
                    await waitForRecorderSynchronization()
                }
            ) {
                openRecordingSetup()
            }
                .disabled(showSetup || offlineDashboard != nil || activationClient != nil)
                .fixedSize(horizontal: false, vertical: true)
            Rectangle().fill(Theme.rule).frame(height: 1)
            // Recording notices sit IN the page flow, above the pane, and
            // push its content down. As a top-trailing overlay they covered
            // the Signal rail's own title with a card that used the very same
            // fill and hairline as the card beneath it — and the design
            // language has no elevation to tell the two layers apart (K09).
            if !showSetup && offlineDashboard == nil && activationClient == nil {
                RecordingHealthNoticeStack(
                    coordinator: healthCoordinator,
                    // Carried over when #292's top-trailing overlay was dropped
                    // in favour of this in-flow placement: `restart` defaults to
                    // nil, so omitting it would have silently lost the in-app
                    // recorder restart rather than failing to build.
                    restart: recorderRestart,
                    onSetup: { openRecordingSetup() },
                    onSetupCause: { openRecordingSetup(cause: $0) },
                    onSources: { selection.open(.sources) },
                    onRefresh: { refreshHealthAndWork() }
                )
                .padding(.horizontal, Space.gutter)
                .padding(.top, Space.l)
                .pageFrame()
                .fixedSize(horizontal: false, vertical: true)
            }
            // Keep the window's content slot stable while old and new panes
            // overlap for the fade. Replacing the VStack child itself lets
            // both heavy pane trees participate in parent layout mid-flight,
            // which reads as a vertical shove instead of a crossfade.
            ZStack(alignment: .top) {
                if let activationClient, let boundary = connectionHistory.boundaries[activationClient.rawValue] {
                    NativeClientActivationView(
                        client: activationClient, boundary: boundary,
                        capture: connectionHistory.captures[activationClient.rawValue],
                        savedSetupLog: connectionHistory.setupLogs[activationClient.rawValue],
                        onClose: { self.activationClient = nil },
                        onOpenWork: { openSavedOrLiveWork() },
                        onOpenCapture: { taskID in
                            self.activationClient = nil
                            selection.taskId = taskID
                            openSavedOrLiveWork()
                        },
                        canViewSavedWork: canViewSavedWork
                    )
                } else if showSetup {
                    NativeSetupFlow(
                        setup: setup,
                        onClose: {
                            showSetup = false
                            if openWorkAfterSetup { selection.open(.work) }
                            openWorkAfterSetup = false
                        },
                        runSetup: { await retrySetupAndRecorderSynchronization() },
                        capture: setup.selectedClient.flatMap { connectionHistory.captures[$0.rawValue] },
                        onOpenCapture: { taskID in
                            showSetup = false
                            selection.pane = .work
                            selection.taskId = taskID
                        },
                        canViewSavedWork: canViewSavedWork,
                        onOpenWork: { openSavedOrLiveWork() },
                        recoveryReason: setupRecoveryReason,
                        recoveryKind: setupRecoveryKind,
                        recoveryUnavailableReasonOverride: setupRecoveryKind == .connection ? reconnectStoreExplanation : nil,
                        onReconnect: { await recoverRecorder() }
                    )
                    .transition(.opacity)
                } else if let offlineDashboard {
                    SavedWorkView(store: offlineDashboard) {
                        self.offlineDashboard = nil
                        showSetup = true
                    }
                } else if localDataPaneCanMount(
                    selection.pane,
                    recorderSynchronizationFinished: recorderSynchronizationFinished,
                    snapshotMode: SnapshotMode.enabled
                ) {
                    Group {
                        switch selection.pane {
                        case .dashboard: DashboardPane()
                        case .worksets: WorksetsPane()
                        case .work: WorkPane()
                        case .usage: UsagePane()
                        case .sources: SourcesPane(onSetup: { client in openRecordingSetup(client: client) })
                        }
                    }
                    .id(selection.pane)
                    .transition(.opacity)
                } else {
                    VStack(spacing: Space.m) {
                        if case .failed = setup.phase {
                            Label("The recorder needs recovery", systemImage: "exclamationmark.triangle")
                                .font(Type.titleSection)
                            Text("Reconnect the local recorder before loading work in this window.")
                                .foregroundStyle(Theme.muted)
                            if savedWork?.hasWork == true {
                                Button("View saved work") { openSavedOrLiveWork() }.buttonStyle(.bordered)
                            }
                            Button("Open recording setup") { showSetup = true }
                                .buttonStyle(PrimaryButtonStyle())
                        } else {
                            ProgressView("Preparing the local recorder…")
                                .controlSize(.small)
                                .foregroundStyle(Theme.muted)
                        }
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .accessibilityIdentifier("dashboard.recorder-synchronization")
                }
            }
            .animation(
                reduceMotion ? Motion.reducedCrossfade : Motion.paneCrossfade,
                value: selection.pane
            )
            // Top-anchored: snapshot mode renders full pane content, which
            // must clip at the bottom, never lose the page header.
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        }
        .background(WindowSurfaceBackground(role: .canvas))
        .frame(minWidth: 960, minHeight: 560)
        .onChange(of: health, initial: true) { _, snapshot in
            healthCoordinator.update(snapshot)
        }
        .task {
            guard !SnapshotMode.enabled else { return }
            // State initializers are evaluated whenever the parent recreates
            // this view. Load the potentially large file once per window, away
            // from rendering and the main actor.
            let loaded = await Task.detached(priority: .utility) { SavedWorkSnapshot.current() }.value
            guard !Task.isCancelled, savedWork == nil else { return }
            savedWork = loaded
        }
        .task {
            // Fixture-backed design review must stay deterministic and must
            // never consult the developer's live daemon/account data.
            guard !SnapshotMode.enabled else { return }
            // A packaged App owns the stable CLI it installed. Before the
            // first local data request, update that CLI transactionally when
            // the new bundle carries different, matching source provenance.
            // If safe recovery still reports a failure, surface the existing
            // in-window setup with its log instead of silently hiding the issue.
            let upgrade = await waitForRecorderSynchronization()
            if case .failed(let message) = upgrade {
                setupRecoveryReason = message
                setupRecoveryKind = .synchronization
                showSetup = true
                return
            }
            recorderSynchronizationFinished = true
            // First-run: a packaged build whose recorder isn't installed yet
            // offers setup once, automatically. A dev build (no embedded CLI)
            // never prompts.
            if setup.presentation.needsSetup {
                openWorkAfterSetup = true
                showSetup = true
            }
        }
        .onChange(of: setup.onboardingCompletedAt, initial: true) { _, boundary in
            guard !SnapshotMode.enabled, reconnectStoreExplanation == nil,
                  let boundary, let target = setup.selectedClient else { return }
            connectionHistory.configured(target, at: boundary, setupLog: setup.log)
            connectionHistory.save()
        }
        .onChange(of: dashboard.setupCaptureTaskAssociations, initial: true) { _, _ in
            if connectionHistory.enrichTaskAssociations(using: { dashboard.taskID(for: $0) }) {
                connectionHistory.save()
            }
        }
        .onChange(of: setup.reconnectCompletedAt) { _, boundary in
            guard !SnapshotMode.enabled, reconnectStoreExplanation == nil, let boundary else { return }
            for id in connectionHistory.boundaries.keys {
                if let target = SetupClient(rawValue: id) { connectionHistory.configured(target, at: boundary) }
            }
            connectionHistory.save()
        }
        .task(id: "\(recorderSynchronizationFinished):\(connectionHistory.pendingKey)") {
            guard !SnapshotMode.enabled, recorderSynchronizationFinished,
                  !connectionHistory.pending.isEmpty else { return }
            while !Task.isCancelled {
                for (id, boundary) in connectionHistory.pending.sorted(by: { $0.key < $1.key }) {
                    guard !Task.isCancelled, let target = SetupClient(rawValue: id) else { return }
                    do {
                        if let capture = try await dashboard.findSetupCapture(client: target, after: boundary) {
                            guard !Task.isCancelled else { return }
                            connectionHistory.observed(capture)
                            connectionHistory.save()
                        }
                    } catch {
                        if Task.isCancelled { return }
                        // An unavailable source remains pending. Health reports
                        // connectivity separately and never invents a capture.
                    }
                }
                do { try await Task.sleep(for: .seconds(3)) } catch { return }
            }
        }
        .task(id: recorderSynchronizationFinished) {
            // The window is a live instrument: refresh while it stays open
            // and restart this loop after a successful synchronization retry.
            await refreshLocalDataWhileReady(
                ready: recorderSynchronizationFinished,
                snapshotMode: SnapshotMode.enabled
            ) {
                await refreshDashboardAndSelectedWork(
                    dashboardRefresh: { await dashboard.refresh() },
                    selectedTaskId: { selection.taskId },
                    receiptRefresh: { await dashboard.fetchReceipt(taskId: $0) }
                )
            }
        }
        .onAppear {
            // A menu-bar app (LSUIElement) has no Dock presence; while the
            // full window is open it should behave like a real app — Dock
            // icon, Cmd-Tab entry — and drop back to accessory on close.
            // NSApp is nil in the offscreen snapshot process (no NSApplication).
            guard !SnapshotMode.enabled, let app = NSApp else { return }
            app.setActivationPolicy(.regular)
            app.activate(ignoringOtherApps: true)
        }
        .onDisappear {
            guard !SnapshotMode.enabled, let app = NSApp else { return }
            app.setActivationPolicy(.accessory)
        }
    }

    private func openActivation(_ id: String) {
        guard let target = SetupClient(rawValue: id), connectionHistory.boundaries[id] != nil else { return }
        selection.prepareWorkReturnFocus()
        savedWork = SavedWorkSnapshot.current()
        showSetup = false
        activationClient = target
    }

    private func openRecordingSetup(cause: RecordingHealthCause? = nil, client: SetupClient? = nil) {
        selection.prepareWorkReturnFocus()
        activationClient = nil
        openWorkAfterSetup = false
        savedWork = SavedWorkSnapshot.current()
        // A per-agent Connect/Re-sync pre-selects that agent in the wizard (its
        // picker initializes from setup.selectedClient).
        if let client { setup.selectClientForSetup(client) }
        switch RecordingSetupRoute.project(
            selectedCause: cause,
            currentCauses: health.causes,
            setupPhase: setup.phase,
            synchronizationFinished: recorderSynchronizationFinished || SnapshotMode.enabled
        ) {
        case .synchronization(let message):
            setupRecoveryKind = .synchronization
            setupRecoveryReason = message
        case .connection(let reason):
            setupRecoveryKind = .connection
            setupRecoveryReason = reason
        case .configuration:
            setupRecoveryKind = .connection
            setupRecoveryReason = nil
        }
        showSetup = true
    }

    private func openSavedOrLiveWork() {
        activationClient = nil
        selection.pane = .work
        if !recorderSynchronizationFinished && !SnapshotMode.enabled, let savedWork, savedWork.hasWork {
            offlineDashboard = DashboardStore(savedWork: savedWork, taskID: selection.taskId)
        }
        showSetup = false
    }

    private func recoverRecorder() async -> Bool {
        if setupRecoveryKind == .synchronization {
            await retrySetupAndRecorderSynchronization()
            return recorderSynchronizationFinished
        }
        guard reconnectStoreExplanation == nil else { return false }
        let succeeded = await setup.reconnectRecorder()
        if succeeded {
            // The route keeps its frozen reason so the success confirmation and
            // diagnostics remain visible until the user leaves deliberately.
            offlineDashboard = nil
            refreshHealthAndWork()
        }
        return succeeded
    }

    private func refreshHealthAndWork() {
        Task {
            await performAfterRecorderSynchronization(
                awaitReady: { await waitForRecorderSynchronization() },
                operation: {
                    glance.refreshNow()
                    await refreshDashboardAndSelectedWork(
                        dashboardRefresh: { await dashboard.refresh() },
                        selectedTaskId: { selection.taskId },
                        receiptRefresh: { await dashboard.fetchReceipt(taskId: $0) }
                    )
                }
            )
        }
    }

    /// One-click recovery from the always-visible health surfaces (the toolbar
    /// popover and the notice stack). Runs the verified app-owned `agentacct
    /// start`; on success it refreshes, and on failure it opens the full recovery
    /// flow so the reconnect log and the specific reason are visible rather than
    /// failing silently. Gated upstream by `recorderRestart` being non-nil.
    private func restartRecorderFromHealth() {
        Task { @MainActor in
            // If another surface (e.g. the menu bar) already has a restart in
            // flight, do nothing rather than misread its busy no-op as a failure
            // and pop an unwanted setup sheet over a reconnect that is proceeding.
            guard setup.reconnectPhase != .working else { return }
            let succeeded = await setup.reconnectRecorder()
            if succeeded {
                offlineDashboard = nil
                refreshHealthAndWork()
            } else {
                openRecordingSetup()
            }
        }
    }

    private func waitForRecorderSynchronization() async -> SetupModel.AutomaticUpgradeOutcome {
        if let lifecycle {
            return await lifecycle.waitUntilReady()
        }
        return await setup.upgradeInstalledCLIIfNeeded()
    }

    private func retrySetupAndRecorderSynchronization() async {
        let outcome: SetupModel.AutomaticUpgradeOutcome
        if let lifecycle,
           let latestOutcome = lifecycle.latestOutcome,
           case .failed = latestOutcome {
            outcome = await lifecycle.retrySynchronization()
        } else {
            if case .failed = setup.phase { setup.reset() }
            await setup.setUp()
            switch setup.phase {
            case .done:
                outcome = .upgraded
            case .failed(let message):
                outcome = .failed(message)
            case .idle, .working:
                outcome = .failed("Recorder synchronization did not finish.")
            }
        }
        guard case .failed = outcome else {
            let alreadyReady = recorderSynchronizationFinished
            recorderSynchronizationFinished = true
            if alreadyReady {
                await refreshDashboardAndSelectedWork(
                    dashboardRefresh: { await dashboard.refresh() },
                    selectedTaskId: { selection.taskId },
                    receiptRefresh: { await dashboard.fetchReceipt(taskId: $0) }
                )
            }
            return
        }
    }
}

@MainActor
func refreshLocalDataWhileReady(
    ready: Bool,
    snapshotMode: Bool,
    waitForNextRefresh: () async throws -> Void = { try await Task.sleep(for: .seconds(60)) },
    refresh: () async -> Void
) async {
    guard ready, !snapshotMode else { return }
    while !Task.isCancelled {
        await refresh()
        do {
            try await waitForNextRefresh()
        } catch {
            return
        }
    }
}

func localDataPaneCanMount(
    _ pane: MainPane,
    recorderSynchronizationFinished: Bool,
    snapshotMode: Bool
) -> Bool {
    _ = pane
    return snapshotMode || recorderSynchronizationFinished
}

// MARK: - Top bar

func refreshProgressVisible(isRefreshing: Bool, delayElapsed: Bool) -> Bool {
    isRefreshing && delayElapsed
}

struct TopBar: View {
    @Environment(DashboardStore.self) var dashboard
    @Environment(GlanceState.self) var glance
    @Environment(AppSelection.self) var selection
    /// Packaged build → show the "Set up recording" entry point.
    var canSetUp: Bool = false
    var health: RecordingHealthSnapshot? = nil
    var healthCoordinator: RecordingHealthCoordinator? = nil
    var restart: RecorderRestartControl? = nil
    var onActivateClient: ((String) -> Void)? = nil
    var onSetupCause: ((RecordingHealthCause) -> Void)? = nil
    var awaitRecorderSynchronization: () async -> SetupModel.AutomaticUpgradeOutcome = { .notNeeded }
    var onSetUp: () -> Void = {}
    @Namespace private var paneSelection
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var showsRefreshProgress = false

    private var isRefreshing: Bool {
        dashboard.isRefreshing || glance.isRefreshing
    }

    /// The ONE way this window changes section, shared by the tabs, the
    /// destination picker and the View menu's ⌘1–⌘4 (K114).
    ///
    /// The Sessions tab (case `.work`) always lands on the receipts table:
    /// without clearing, a stale taskId makes the tab a no-op while a record
    /// is open and resurrects the last record on the next visit. Row/deep
    /// links still open records via open(.task).
    private func openSection(_ pane: MainPane) {
        if pane == .work {
            selection.open(.work)
        } else {
            selection.pane = pane
        }
    }

    private func paneTabs(iconOnly: Bool) -> some View {
        HStack(spacing: 3) {
            ForEach(MainPane.allCases) { pane in
                PaneTab(
                    pane: pane,
                    selected: selection.pane == pane,
                    iconOnly: iconOnly,
                    selectionNamespace: paneSelection
                ) {
                    openSection(pane)
                }
            }
        }
        .padding(3)
        .background(Theme.tintNeutralOnCanvas, in: RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous))
        .animation(reduceMotion ? nil : Motion.selection, value: selection.pane)
    }

    /// The one refresh action shared by the top-bar glyph and the app menu's
    /// Refresh command (⌘R).
    private func refreshLocalData() {
        Task {
            await performAfterRecorderSynchronization(
                awaitReady: awaitRecorderSynchronization,
                operation: {
                    glance.refreshNow()
                    await refreshDashboardAndSelectedWork(
                        dashboardRefresh: { await dashboard.refresh() },
                        selectedTaskId: { selection.taskId },
                        receiptRefresh: { await dashboard.fetchReceipt(taskId: $0) }
                    )
                }
            )
        }
    }

    private var destinationPicker: some View {
        AppMenuPicker(
            title: "Destination",
            selection: Binding(
                get: { selection.pane },
                set: { openSection($0) }
            ),
            options: MainPane.allCases.map { ($0, $0.rawValue) },
            accessibilityIdentifier: "dashboard.destination-picker"
        )
    }

    /// The trailing status cluster. Its compact form keeps every word that
    /// carries meaning (the health title and the freshness value) and drops
    /// only the "Local data ·" prefix; it is never icon-only or dot-only.
    @ViewBuilder
    private func trailingCluster(compact: Bool) -> some View {
        HStack(spacing: 14) {
            if let health {
                RecordingHealthToolbarButton(
                    snapshot: health,
                    coordinator: healthCoordinator,
                    restart: restart,
                    onSetup: onSetUp,
                    onSetupCause: onSetupCause,
                    onActivateClient: onActivateClient,
                    onSources: { selection.open(.sources) },
                    onRefresh: {
                        Task {
                            await performAfterRecorderSynchronization(
                                awaitReady: awaitRecorderSynchronization,
                                operation: {
                                    glance.refreshNow()
                                    await dashboard.refresh()
                                }
                            )
                        }
                    },
                    compact: compact
                )
            } else if canSetUp {
                Button(action: onSetUp) {
                    HStack(spacing: 4) {
                        Image(systemName: "record.circle")
                            .workFont(.icon)
                        Text("Set up recording")
                            .workFont(.captionSemibold)
                            .lineLimit(1)
                    }
                    .foregroundStyle(Theme.accent)
                }
                .buttonStyle(QuietButtonStyle(horizontalPadding: 8))
                .help("Install the recorder and configure your coding agents")
                .accessibilityIdentifier("dashboard.setup-recording")
            }
            if let updated = dashboard.lastUpdated {
                // The ticking caption is its own view, so the age changing
                // does not re-evaluate the health button and refresh glyph
                // beside it — the leading suspect for their `.help` tooltips
                // never appearing in the header while the same components'
                // tooltips work inside the panes (K75).
                TopBarFreshnessLabel(
                    updated: updated,
                    reachable: health?.endpointReachable ?? false,
                    lastRefreshFailed: dashboard.errorText != nil || dashboard.receiptListError != nil,
                    compact: compact
                )
            }
        }
    }
}

/// The top bar's freshness dot and caption, isolated from its neighbours.
///
/// Two rules live here. The dot is `Theme.green` ONLY while the recorder is
/// reachable and the last refresh succeeded (K02); otherwise it is a hollow
/// muted ring beside a named state that keeps its age. And the caption
/// reserves the width of a typical value, so the health button to its left
/// stops sliding ~7px sideways whenever the age changes length (K95).
struct TopBarFreshnessLabel: View {
    let updated: Date
    let reachable: Bool
    let lastRefreshFailed: Bool
    let compact: Bool

    /// The width the caption reserves. A longer value still grows the label;
    /// nothing is ever truncated to fit, and the rare long states
    /// ("time unavailable") are not reserved for.
    static let widthTemplate = "Local data · 59m ago"
    static let compactWidthTemplate = "59m ago"

    var body: some View {
        let age = dashboardFreshnessText(updated)
        let freshness = TopBarFreshness(reachable: reachable, lastRefreshFailed: lastRefreshFailed)
        HStack(spacing: 5) {
            Group {
                if freshness.isLive {
                    Circle().fill(Theme.green)
                } else {
                    Circle().strokeBorder(Theme.muted, lineWidth: 1)
                }
            }
            .workScaledFrame(width: 5, height: 5, relativeTo: .caption)
            ZStack(alignment: .trailing) {
                Text(compact ? Self.compactWidthTemplate : Self.widthTemplate)
                    .hidden()
                    .accessibilityHidden(true)
                Text(freshness.caption(age: age, compact: compact)).lineLimit(1)
            }
        }
        .workFont(.dataSmall)
        .foregroundStyle(Theme.muted)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(freshness.accessibilityLabel(age: age))
        .accessibilityIdentifier("dashboard.freshness")
    }
}

extension TopBar {
    /// Destination names are functional content, so the tabs claim space
    /// first and fall back to a labeled picker only when they cannot fit.
    @ViewBuilder
    private func destinations(asPicker: Bool) -> some View {
        if asPicker { destinationPicker } else { paneTabs(iconOnly: false) }
    }

    /// One top-bar row: brand, destinations, then the status cluster.
    private func topBarRow(asPicker: Bool, compactCluster: Bool) -> some View {
        HStack(spacing: 14) {
            BrandLockup()
                // Four destinations fit with full labels at the minimum
                // window once the old Limits tab is removed.
                .padding(.trailing, 8)
            destinations(asPicker: asPicker).layoutPriority(1)
            Spacer(minLength: 0)
            trailingCluster(compact: compactCluster)
            refreshControl
        }
    }

    var body: some View {
        // Every branch keeps the same facts; only the arrangement changes.
        // The last one stacks the destinations over the status cluster, so at
        // accessibility reading sizes the recorder's title and the freshness
        // value stay words instead of collapsing to a bare glyph and "just…"
        // (K26, honouring C81's "never icon-only or dot-only").
        ViewThatFits(in: .horizontal) {
            topBarRow(asPicker: false, compactCluster: false)
            topBarRow(asPicker: false, compactCluster: true)
            topBarRow(asPicker: true, compactCluster: true)
            VStack(alignment: .leading, spacing: Space.s) {
                HStack(spacing: 14) {
                    BrandLockup().padding(.trailing, 8)
                    destinations(asPicker: false)
                    Spacer(minLength: 0)
                }
                HStack(spacing: 14) {
                    Spacer(minLength: 0)
                    trailingCluster(compact: false)
                    refreshControl
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 4)
        .frame(minHeight: 46)
        .background(WindowSurfaceBackground(role: .chrome))
        .focusedSceneValue(
            \.refreshLocalData,
            RefreshLocalDataAction(isRefreshing: isRefreshing, perform: refreshLocalData)
        )
        // The View menu's ⌘1–⌘4 run the same closure as the tabs (K114).
        .focusedSceneValue(\.openSection, OpenSectionAction(open: openSection))
    }

    private var refreshControl: some View {
        Group {
            ZStack {
                if showsRefreshProgress {
                    ProgressView()
                        .controlSize(.small)
                        .tint(Theme.muted)
                        .accessibilityLabel("Refreshing local data")
                        .transition(.opacity)
                } else {
                    IconButton(
                        systemName: "arrow.clockwise",
                        label: "Refresh local data",
                        help: RefreshCommandText.help,
                        tint: Theme.muted,
                        identifier: "dashboard.refresh",
                        action: refreshLocalData
                    )
                    .disabled(isRefreshing)
                    .transition(.opacity)
                }
            }
            .minimumHitTarget()
            .animation(
                reduceMotion ? Motion.reducedCrossfade : Motion.phaseCrossfade,
                value: showsRefreshProgress
            )
            .task(id: isRefreshing) {
                guard isRefreshing else {
                    showsRefreshProgress = false
                    return
                }
                try? await Task.sleep(for: .milliseconds(150))
                guard !Task.isCancelled else { return }
                showsRefreshProgress = refreshProgressVisible(
                    isRefreshing: isRefreshing,
                    delayElapsed: true
                )
            }
        }
    }
}

/// Refresh the collection first, then read the selection that exists now.
/// Reading it after the await prevents a slow refresh for A from starting a
/// newer A detail request after the user has already navigated to B.
@MainActor
func refreshDashboardAndSelectedWork(
    dashboardRefresh: () async -> Void,
    selectedTaskId: () -> String?,
    receiptRefresh: (String) async -> Void
) async {
    await dashboardRefresh()
    guard !Task.isCancelled, let taskId = selectedTaskId() else { return }
    await receiptRefresh(taskId)
}

enum WindowSurfacePolicy {
    static func usesMaterial(reduceTransparency: Bool, snapshotMode: Bool) -> Bool {
        !reduceTransparency && !snapshotMode
    }
}

private struct WindowSurfaceBackground: View {
    enum Role {
        case canvas
        case chrome
    }

    let role: Role
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency

    private var usesMaterial: Bool {
        WindowSurfacePolicy.usesMaterial(
            reduceTransparency: reduceTransparency,
            snapshotMode: SnapshotMode.enabled
        )
    }

    private var veilOpacity: Double {
        switch (role, colorScheme) {
        case (.canvas, .light): 0.76
        case (.canvas, .dark): 0.82
        case (.chrome, .light): 0.58
        case (.chrome, .dark): 0.68
        @unknown default: 0.80
        }
    }

    @ViewBuilder
    var body: some View {
        if usesMaterial {
            Group {
                switch role {
                case .canvas:
                    Color(nsColor: .windowBackgroundColor)
                case .chrome:
                    Rectangle().fill(.bar)
                }
            }
            // Keep palette and contrast stable while still allowing the
            // system's desktop tint and active-window state to come through.
            .overlay(Theme.canvas.opacity(veilOpacity))
        } else {
            Theme.canvas
        }
    }
}

struct PaneTab: View {
    let pane: MainPane
    let selected: Bool
    var iconOnly = false
    let selectionNamespace: Namespace.ID
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 5) {
                Image(systemName: pane.icon(selected: selected))
                    .workFont(size: Type.icon, weight: selected ? .semibold : .medium, relativeTo: .caption)
                    .symbolRenderingMode(.monochrome)
                    .workScaledFrame(width: 14, height: 14, relativeTo: .caption)
                if !iconOnly {
                    Text(pane.rawValue)
                        .workFont(
                            size: 12.5,
                            weight: selected ? .semibold : .medium,
                            relativeTo: .caption
                        )
                        .fixedSize()
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .foregroundStyle(selected ? Theme.ink : (hovering ? Theme.ink : Theme.muted))
            .background {
                if selected {
                    RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
                        .fill(Theme.thumb)
                        .matchedGeometryEffect(id: "selected-pane", in: selectionNamespace)
                } else if hovering {
                    RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
                        .fill(Theme.thumbHoverOnCanvas)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(PaneTabPressStyle())
        .accessibilityLabel(pane.rawValue)
        .accessibilityAddTraits(selected ? .isSelected : [])
        .accessibilityIdentifier("navigation.\(pane.rawValue.lowercased())")
        .onHover { inside in
            withAnimation(Motion.hover) {
                hovering = inside
            }
        }
    }
}

private struct PaneTabPressStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        PaneTabPressBody(configuration: configuration)
    }
}

private struct PaneTabPressBody: View {
    let configuration: ButtonStyleConfiguration
    @Environment(\.isFocused) private var isFocused

    var body: some View {
        configuration.label
            .opacity(configuration.isPressed ? 0.72 : 1)
            .overlay {
                if isFocused {
                    RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
                        .strokeBorder(Theme.accent, lineWidth: Metrics.focusW)
                }
            }
            .animation(Motion.feedback, value: configuration.isPressed)
    }
}

extension MainPane {
    func icon(selected: Bool) -> String {
        switch self {
        case .dashboard: return selected ? "square.grid.2x2.fill" : "square.grid.2x2"
        case .worksets: return selected ? "folder.fill" : "folder"
        case .work: return "checklist"
        case .usage: return "chart.bar.xaxis"
        case .sources: return "point.3.connected.trianglepath.dotted"
        }
    }
}

// MARK: - Shared bits used by several panes

/// The evidence four-state enum. Strong evidence settles in ink, failure is
/// coral, weak is amber (the unverified tier), nothing is muted. Green is
/// reserved for `EvidenceTierStyle.forGrade("externally_verified")` and the
/// live connection dot (C27), so "strong" never maps to green here.
func evidenceTint(_ status: String?) -> Color {
    switch status {
    case "strong": return Theme.ink
    case "failed": return Theme.coral
    case "weak": return Theme.amber
    default: return Theme.muted
    }
}

func joinTint(_ state: String?) -> Color {
    switch state {
    case "attributed": return Theme.ink
    case "ambiguous": return Theme.amber
    case "sections_only": return Theme.accent
    default: return Theme.muted
    }
}

func durationText(_ seconds: Double) -> String {
    let total = Int(seconds)
    let hours = total / 3600
    let minutes = (total % 3600) / 60
    if hours > 0 { return "\(hours)h \(minutes)m" }
    return "\(minutes)m"
}

func agoText(_ epoch: Double?) -> String? {
    guard let epoch, epoch > 0 else { return nil }
    let delta = SnapshotMode.currentDate.timeIntervalSince1970 - epoch
    guard delta >= 0 else { return nil }
    let total = Int(delta)
    if total < 60 { return "\(total)s ago" }
    if total < 3600 { return "\(total / 60)m ago" }
    if total < 86400 { return "\(total / 3600)h ago" }
    return "\(total / 86400)d ago"
}

/// The relative-time words every freshness stamp shares. The threshold and
/// phrase are `display_vocabulary.FRESHNESS_JUST_NOW_SECONDS` /
/// `FRESHNESS_JUST_NOW_TEXT` (a Python test pins these two lines to them); the
/// age itself must be computed at render time, so only these words mirror.
enum FreshnessVocabulary {
    static let justNowSeconds = 5
    static let justNowText = "just now"
    static let capacityCheckedLabel = "capacity checked"
    static let recordedUsageRefreshedLabel = "recorded usage refreshed"
    static let separator = " · "
}

func dashboardFreshnessText(_ date: Date) -> String {
    let delta = SnapshotMode.currentDate.timeIntervalSince1970 - date.timeIntervalSince1970
    if delta >= 0, delta < Double(FreshnessVocabulary.justNowSeconds) {
        return FreshnessVocabulary.justNowText
    }
    guard let text = agoText(date.timeIntervalSince1970) else { return "time unavailable" }
    return text
}
