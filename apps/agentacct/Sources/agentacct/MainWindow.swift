import AppKit
import SwiftUI

// The full window follows the system appearance through semantic Theme tokens.
// Custom chrome keeps navigation quiet; the menu bar is the glance and this
// window is where work evidence and usage details live.
//
// Panes live in their own files: DashboardPane (the home), WorkPane (Tasks +
// their session drill-down), UsagePane, LimitsPane.

struct MainWindow: View {
    @EnvironmentObject var dashboard: DashboardStore
    @EnvironmentObject var glance: GlanceState
    @EnvironmentObject var selection: AppSelection
    @StateObject private var setup = SetupModel()
    @State private var showSetup = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    /// Design-review renders cannot infer whether the executable was packaged
    /// with the recorder. Live windows leave this nil and use SetupModel.
    var canSetUpOverride: Bool? = nil

    private var canSetUp: Bool {
        canSetUpOverride ?? (setup.bundledCLIDir != nil)
    }

    var body: some View {
        VStack(spacing: 0) {
            TopBar(canSetUp: canSetUp) { showSetup = true }
            Rectangle().fill(Theme.border).frame(height: 1)
            Group {
                switch selection.pane {
                case .dashboard: DashboardPane()
                case .work: WorkPane()
                case .usage: UsagePane()
                case .limits: LimitsPane()
                }
            }
            .id(selection.pane)
            .transition(.opacity)
            .animation(reduceMotion ? nil : Motion.paneCrossfade, value: selection.pane)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
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
            await dashboard.refresh()
            // The window is a live instrument: refresh while it stays open
            // (the daemon caches by fingerprint, so a quiet minute is one
            // store read + a hash for it, not a rebuild).
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(60))
                guard !Task.isCancelled else { break }
                await dashboard.refresh()
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

struct TopBar: View {
    @EnvironmentObject var dashboard: DashboardStore
    @EnvironmentObject var selection: AppSelection
    /// Packaged build → show the "Set up recording" entry point.
    var canSetUp: Bool = false
    var onSetUp: () -> Void = {}
    @Namespace private var paneSelection
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        HStack(spacing: 14) {
            HStack(spacing: 7) {
                Circle().fill(Theme.accent).frame(width: 8, height: 8)
                Text("agentacct")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(Theme.text)
            }
            .padding(.leading, 76)  // clear the traffic lights (hidden titlebar)

            HStack(spacing: 3) {
                ForEach(MainPane.allCases) { pane in
                    PaneTab(
                        pane: pane,
                        selected: selection.pane == pane,
                        selectionNamespace: paneSelection
                    ) {
                        selection.pane = pane
                    }
                }
            }
            .padding(3)
            .background(Theme.surface, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            .animation(reduceMotion ? nil : Motion.selection, value: selection.pane)

            Spacer()

            if canSetUp {
                Button(action: onSetUp) {
                    HStack(spacing: 4) {
                        Image(systemName: "record.circle")
                            .font(.system(size: 12, weight: .medium))
                        Text("Set up recording").font(Type.action)
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
                .font(.system(size: 10.5))
                .foregroundStyle(Theme.textFaint)
                .accessibilityElement(children: .combine)
                .accessibilityLabel("Local data updated \(freshness)")
            }
            if dashboard.isRefreshing {
                ProgressView().controlSize(.small).tint(Theme.textMuted)
            } else {
                Button {
                    Task { await dashboard.refresh() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                        .font(.system(size: 11.5, weight: .medium))
                        .foregroundStyle(Theme.textMuted)
                        .frame(width: 28, height: 28)
                        .contentShape(Rectangle())
                }
                .buttonStyle(QuietButtonStyle(
                    tint: Theme.textMuted,
                    horizontalPadding: 0,
                    verticalPadding: 0
                ))
                .help("Refresh local data")
                .accessibilityIdentifier("dashboard.refresh")
            }
        }
        .padding(.horizontal, 14)
        .frame(height: 46)
        .background(WindowSurfaceBackground(role: .chrome))
    }
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
            .overlay(Theme.bg.opacity(veilOpacity))
        } else {
            Theme.bg
        }
    }
}

struct PaneTab: View {
    let pane: MainPane
    let selected: Bool
    let selectionNamespace: Namespace.ID
    let action: () -> Void
    @State private var hovering = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Button(action: action) {
            HStack(spacing: 5) {
                Image(systemName: pane.icon(selected: selected))
                    .font(.system(size: 12, weight: selected ? .semibold : .medium))
                    .symbolRenderingMode(.monochrome)
                    .frame(width: 14, height: 14)
                Text(pane.rawValue)
                    .font(.system(size: 12.5, weight: selected ? .semibold : .medium))
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .foregroundStyle(selected ? Theme.text : (hovering ? Theme.textMuted : Theme.textFaint))
            .background {
                if selected {
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .fill(Theme.cardAlt)
                        .matchedGeometryEffect(id: "selected-pane", in: selectionNamespace)
                } else if hovering {
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .fill(Theme.cardAlt.opacity(0.55))
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(PaneTabPressStyle())
        .accessibilityIdentifier("navigation.\(pane.rawValue.lowercased())")
        .onHover { inside in
            withAnimation(reduceMotion ? nil : Motion.hover) {
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
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.isFocused) private var isFocused

    var body: some View {
        configuration.label
            .opacity(configuration.isPressed ? 0.72 : 1)
            .overlay {
                if isFocused {
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .strokeBorder(Theme.accent, lineWidth: 2)
                }
            }
            .animation(reduceMotion ? nil : Motion.feedback, value: configuration.isPressed)
    }
}

extension MainPane {
    func icon(selected: Bool) -> String {
        switch self {
        case .dashboard: return selected ? "square.grid.2x2.fill" : "square.grid.2x2"
        case .work: return "checklist"
        case .usage: return "chart.bar.xaxis"
        case .limits: return "gauge.with.needle"
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
    case "failed": return Theme.red
    case "weak": return Theme.orange
    default: return Theme.textMuted
    }
}

func joinTint(_ state: String?) -> Color {
    switch state {
    case "attributed": return Theme.green
    case "ambiguous": return Theme.orange
    case "sections_only": return Theme.blue
    default: return Theme.textMuted
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
