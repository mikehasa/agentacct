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
    @StateObject private var setup = SetupModel()
    @State private var showSetup = false
    /// Design-review renders cannot infer whether the executable was packaged
    /// with the recorder. Live windows leave this nil and use SetupModel.
    var canSetUpOverride: Bool? = nil

    private var canSetUp: Bool {
        canSetUpOverride ?? (setup.bundledCLIDir != nil)
    }

    var body: some View {
        VStack(spacing: 0) {
            TopBar(canSetUp: canSetUp) { showSetup = true }
                .fixedSize(horizontal: false, vertical: true)
            Rectangle().fill(Theme.rule).frame(height: 1)
            // Keep the window's content slot stable while old and new panes
            // overlap for the fade. Replacing the VStack child itself lets
            // both heavy pane trees participate in parent layout mid-flight,
            // which reads as a vertical shove instead of a crossfade.
            ZStack(alignment: .top) {
                Group {
                    switch selection.pane {
                    case .dashboard: DashboardPane()
                    case .work: WorkPane()
                    case .usage: UsagePane()
                    case .sources: SourcesPane()
                    }
                }
                .id(selection.pane)
                .transition(.opacity)
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
        .sheet(isPresented: $showSetup) {
            SetupSheet(setup: setup) { showSetup = false }
        }
        .task {
            // Fixture-backed design review must stay deterministic and must
            // never consult the developer's live daemon/account data.
            guard !SnapshotMode.enabled else { return }
            // First-run: a packaged build whose recorder isn't installed yet
            // offers setup once, automatically. A dev build (no embedded CLI)
            // never prompts.
            if setup.shouldOfferSetup { showSetup = true }
            await refreshDashboardAndSelectedWork(
                dashboardRefresh: { await dashboard.refresh() },
                selectedTaskId: { selection.taskId },
                receiptRefresh: { await dashboard.fetchReceipt(taskId: $0) }
            )
            // The window is a live instrument: refresh while it stays open
            // (the daemon caches by fingerprint, so a quiet minute is one
            // store read + a hash for it, not a rebuild).
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(60))
                guard !Task.isCancelled else { break }
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
    var onSetUp: () -> Void = {}
    @Namespace private var paneSelection
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var showsRefreshProgress = false

    private var isRefreshing: Bool {
        dashboard.isRefreshing || glance.isRefreshing
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
                    // The Work tab always lands on the receipts table: without
                    // clearing, a stale taskId makes the tab a no-op while a
                    // record is open and resurrects the last record on the next
                    // visit. Row/deep links still open records via open(.task).
                    if pane == .work {
                        selection.open(.work)
                    } else {
                        selection.pane = pane
                    }
                }
            }
        }
        .padding(3)
        .background(Theme.tintNeutral, in: RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous))
        .animation(reduceMotion ? nil : Motion.selection, value: selection.pane)
    }

    var body: some View {
        HStack(spacing: 14) {
            BrandLockup()
                // Four destinations fit with full labels at the minimum
                // window once the old Limits tab is removed.
                .padding(.trailing, 8)

            // Preserve full labels when four panes fit; icon-only remains the
            // safety fallback for accessibility text or unusually narrow chrome.
            ViewThatFits(in: .horizontal) {
                paneTabs(iconOnly: false)
                paneTabs(iconOnly: true)
            }

            Spacer()

            if canSetUp {
                Button(action: onSetUp) {
                    HStack(spacing: 4) {
                        Image(systemName: "record.circle")
                            .font(.system(size: 12, weight: .medium))
                        Text("Set up recording").workFont(.captionSemibold)
                    }
                    .foregroundStyle(Theme.accent)
                }
                .buttonStyle(QuietButtonStyle(horizontalPadding: 8))
                .help("Install the recorder and configure your coding agents")
                .accessibilityIdentifier("dashboard.setup-recording")
            }
            if let updated = dashboard.lastUpdated {
                let freshness = dashboardFreshnessText(updated)
                HStack(spacing: 5) {
                    Circle().fill(Theme.green).frame(width: 5, height: 5)
                    Text("Local data · \(freshness)")
                }
                .workFont(.dataSmall)
                .foregroundStyle(Theme.muted)
                .accessibilityElement(children: .combine)
                .accessibilityLabel("Local data updated \(freshness)")
            }
            ZStack {
                if showsRefreshProgress {
                    ProgressView()
                        .controlSize(.small)
                        .tint(Theme.muted)
                        .accessibilityLabel("Refreshing local data")
                        .transition(.opacity)
                } else {
                    Button {
                        glance.refreshNow()
                        Task {
                            await refreshDashboardAndSelectedWork(
                                dashboardRefresh: { await dashboard.refresh() },
                                selectedTaskId: { selection.taskId },
                                receiptRefresh: { await dashboard.fetchReceipt(taskId: $0) }
                            )
                        }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                            .font(.system(size: 11.5, weight: .medium))
                            .foregroundStyle(Theme.muted)
                            .frame(width: 28, height: 28)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(QuietButtonStyle(
                        tint: Theme.muted,
                        horizontalPadding: 0,
                        verticalPadding: 0
                    ))
                    .disabled(isRefreshing)
                    .help("Refresh local data")
                    .accessibilityLabel("Refresh local data")
                    .accessibilityIdentifier("dashboard.refresh")
                    .transition(.opacity)
                }
            }
            .frame(width: 28, height: 28)
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
        .padding(.horizontal, 14)
        .frame(minHeight: 46)
        .background(WindowSurfaceBackground(role: .chrome))
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
                    .font(.system(size: 12, weight: selected ? .semibold : .medium))
                    .symbolRenderingMode(.monochrome)
                    .frame(width: 14, height: 14)
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
                        .fill(Theme.card)
                        .matchedGeometryEffect(id: "selected-pane", in: selectionNamespace)
                } else if hovering {
                    RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
                        .fill(Theme.card.opacity(0.55))
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
        case .work: return "checklist"
        case .usage: return "chart.bar.xaxis"
        case .sources: return "point.3.connected.trianglepath.dotted"
        }
    }
}

// MARK: - Shared bits used by several panes

/// The evidence four-state enum, in the product's confidence colors — strong
/// proof green, failure red, weak amber, nothing muted. This chip IS the
/// product (what did the agent prove?), so it never renders as an
/// undifferentiated gray.
func evidenceTint(_ status: String?) -> Color {
    switch status {
    case "strong": return Theme.green
    case "failed": return Theme.coral
    case "weak": return Theme.amber
    default: return Theme.muted
    }
}

func joinTint(_ state: String?) -> Color {
    switch state {
    case "attributed": return Theme.green
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

func dashboardFreshnessText(_ date: Date) -> String {
    guard let text = agoText(date.timeIntervalSince1970) else { return "time unavailable" }
    return text == "0s ago" ? "just now" : text
}
