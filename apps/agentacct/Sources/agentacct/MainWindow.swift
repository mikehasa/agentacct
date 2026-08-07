import AppKit
import SwiftUI

// The full window: a precision instrument on warm paper (light) or the Tokyo
// Night storm (dark), following the system appearance — every color is a
// Theme token with both values. Custom chrome (no system sidebar), like the
// TUI. The menu bar is the glance; this is where details live.
//
// Panes live in their own files: DashboardPane (the home), WorkPane (Tasks +
// their session drill-down), UsagePane, LimitsPane.

struct MainWindow: View {
    @EnvironmentObject var dashboard: DashboardStore
    @EnvironmentObject var glance: GlanceState
    @EnvironmentObject var selection: AppSelection
    @StateObject private var setup = SetupModel()
    @State private var showSetup = false

    var body: some View {
        VStack(spacing: 0) {
            TopBar(canSetUp: setup.bundledCLIDir != nil) { showSetup = true }
            Rectangle().fill(Theme.border).frame(height: 1)
            Group {
                switch selection.pane {
                case .dashboard: DashboardPane()
                case .work: WorkPane()
                case .usage: UsagePane()
                case .limits: LimitsPane()
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(Theme.bg)
        .frame(minWidth: 960, minHeight: 560)
        .sheet(isPresented: $showSetup) {
            SetupSheet(setup: setup) { showSetup = false }
        }
        .task {
            // First-run: a packaged build whose recorder isn't installed yet
            // offers setup once, automatically. A dev build (no embedded CLI)
            // never prompts.
            if !SnapshotMode.enabled, setup.shouldOfferSetup { showSetup = true }
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

    var body: some View {
        HStack(spacing: 14) {
            HStack(spacing: 7) {
                Circle().fill(Theme.accent.gradient).frame(width: 9, height: 9)
                Text("agentacct")
                    .font(.system(size: 13, weight: .semibold, design: .rounded))
                    .foregroundStyle(Theme.text)
            }
            .padding(.leading, 76)  // clear the traffic lights (hidden titlebar)

            HStack(spacing: 3) {
                ForEach(MainPane.allCases) { pane in
                    PaneTab(pane: pane, selected: selection.pane == pane) {
                        selection.pane = pane
                    }
                }
            }
            .padding(3)
            .background(Theme.surface, in: RoundedRectangle(cornerRadius: 8, style: .continuous))

            Spacer()

            if canSetUp {
                Button(action: onSetUp) {
                    HStack(spacing: 4) {
                        Image(systemName: "sparkles")
                        Text("Set up recording").font(.system(size: 11, weight: .medium))
                    }
                    .foregroundStyle(Theme.accent)
                }
                .buttonStyle(.plain)
                .help("Install the recorder and configure your coding agents")
            }
            if let updated = dashboard.lastUpdated {
                Text(updated, style: .relative)
                    .font(.system(size: 10.5))
                    .foregroundStyle(Theme.textFaint)
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
                }
                .buttonStyle(.plain)
                .help("Refresh")
            }
        }
        .padding(.horizontal, 14)
        .frame(height: 46)
        .background(Theme.bg)
    }
}

struct PaneTab: View {
    let pane: MainPane
    let selected: Bool
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 5) {
                Image(systemName: pane.icon).font(.system(size: 10.5, weight: .medium))
                Text(pane.rawValue).font(.system(size: 12, weight: .medium))
            }
            .padding(.horizontal, 11)
            .padding(.vertical, 5)
            .foregroundStyle(selected ? Theme.text : (hovering ? Theme.textMuted : Theme.textFaint))
            .background(
                selected ? AnyShapeStyle(Theme.cardAlt) : AnyShapeStyle(.clear),
                in: RoundedRectangle(cornerRadius: 6, style: .continuous)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
    }
}

extension MainPane {
    var icon: String {
        switch self {
        case .dashboard: return "gauge.with.dots.needle.50percent"
        case .work: return "checklist"
        case .usage: return "chart.bar.fill"
        case .limits: return "gauge.with.needle.fill"
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
    let delta = Date().timeIntervalSince1970 - epoch
    guard delta >= 0 else { return nil }
    let total = Int(delta)
    if total < 60 { return "\(total)s ago" }
    if total < 3600 { return "\(total / 60)m ago" }
    if total < 86400 { return "\(total / 3600)h ago" }
    return "\(total / 86400)d ago"
}
