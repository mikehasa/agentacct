import SwiftUI

// The dropdown: a glance, not a workspace. A today hero, compact window
// stats, live limit meters, and root sessions — click anything deep to open
// the full window. Honesty rules carried from the TUI: costs are complete-$ /
// partial-~$ / em-dash (never a fabricated $0), plan shares only when
// calibrated, statuses use the server-side reduction. Stale limit accounts
// are hidden here (the full window has the toggle).

struct MenuContent: View {
    @EnvironmentObject var state: GlanceState
    @EnvironmentObject var dashboard: DashboardStore
    @EnvironmentObject var selection: AppSelection
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            switch state.phase {
            case .connecting:
                waitingView("Connecting to the agentacct daemon…")
            case .disconnected(let reason):
                disconnectedView(reason: reason)
            case .incompatible(let message):
                Label { Text(message) } icon: { Image(systemName: "exclamationmark.triangle.fill") }
                    .font(.callout)
                    .foregroundStyle(Theme.orange)
            case .connected(let snapshot):
                connectedView(snapshot: snapshot)
            }
            footer
        }
        .padding(14)
        .frame(width: 360)
    }

    // MARK: states

    private func waitingView(_ text: String) -> some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            Text(text).foregroundStyle(.secondary)
        }
        .font(.callout)
        .padding(.vertical, 12)
    }

    private func disconnectedView(reason: String) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Label("Daemon not reachable", systemImage: "bolt.slash.fill")
                .font(.callout.weight(.medium))
                .foregroundStyle(.secondary)
            Text(reason)
                .font(.caption)
                .foregroundStyle(.tertiary)
                .lineLimit(2)
            Text("agentacct start")
                .font(.caption.monospaced())
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 6))
        }
        .padding(.vertical, 6)
    }

    // MARK: connected

    @ViewBuilder
    private func connectedView(snapshot: GlanceSnapshot) -> some View {
        let glance = snapshot.glance
        let windows = Dictionary(uniqueKeysWithValues: glance.usage.windows.map { ($0.label, $0.totals) })

        // Hero: today.
        VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline) {
                Text("TODAY")
                    .font(.system(size: 10, weight: .semibold))
                    .tracking(0.8)
                    .foregroundStyle(.secondary)
                Spacer()
                if state.isRefreshing {
                    ProgressView().controlSize(.mini)
                } else if let updated = state.lastUpdated {
                    Text(updated, style: .relative)
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                }
            }
            Text(windows["today"]?.costText ?? "—")
                .font(.system(size: 30, weight: .bold, design: .rounded))
                .monospacedDigit()
            Text("\(windows["today"]?.tokensText ?? "—") fresh tokens")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
        }

        HStack(spacing: 8) {
            StatTile(label: "7 days",
                     value: windows["last 7 days"]?.costText ?? "—",
                     detail: (windows["last 7 days"]?.tokensText).map { "\($0) tok" })
            StatTile(label: "30 days",
                     value: windows["last 30 days"]?.costText ?? "—",
                     detail: (windows["last 30 days"]?.tokensText).map { "\($0) tok" })
        }

        let liveLimits = glance.limits.filter { $0.stale != true }
        if !liveLimits.isEmpty {
            VStack(alignment: .leading, spacing: 7) {
                SectionCaption(text: "Limits")
                ForEach(Array(liveLimits.enumerated()), id: \.offset) { _, limit in
                    limitRows(limit)
                }
            }
        }

        if !glance.recentSessions.isEmpty {
            VStack(alignment: .leading, spacing: 3) {
                SectionCaption(text: "Sessions")
                ForEach(Array(glance.recentSessions.prefix(6).enumerated()), id: \.offset) { _, session in
                    sessionRow(session)
                }
            }
        }

        planFootnote(glance.plan)
    }

    @ViewBuilder
    private func limitRows(_ limit: LimitEntry) -> some View {
        ForEach(Array((limit.windows ?? []).enumerated()), id: \.offset) { _, window in
            if let used = window.usedPercent {
                HStack(spacing: 8) {
                    StatusDot(color: Theme.clientColor(limit.client), size: 6)
                    Text("\(limit.client ?? "?") \(window.kind ?? "")")
                        .font(.system(size: 11.5))
                        .foregroundStyle(.secondary)
                        .frame(width: 96, alignment: .leading)
                    MeterBar(fraction: used / 100.0, tint: Theme.limitColor(usedPercent: used))
                    Text(String(format: "%.0f%%", used))
                        .font(.system(size: 11.5, weight: .medium))
                        .monospacedDigit()
                        .frame(width: 34, alignment: .trailing)
                    Text(Theme.resetsIn(window.resetsAt) ?? "")
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                        .frame(width: 44, alignment: .trailing)
                }
            }
        }
    }

    private func sessionRow(_ session: RecentSession) -> some View {
        MenuHoverButton {
            openMain(selecting: "\(session.client)::\(session.sessionId)")
        } content: {
            HStack(spacing: 8) {
                StatusDot(color: Theme.statusColor(session.status), size: 7)
                Text(session.title ?? "\(session.client) · \(session.shortSessionId)")
                    .font(.system(size: 12))
                    .lineLimit(1)
                    .truncationMode(.tail)
                Spacer(minLength: 8)
                if let pct = session.planPctText {
                    Text(pct)
                        .font(.system(size: 10.5, weight: .medium))
                        .monospacedDigit()
                        .foregroundStyle(.secondary)
                }
                Image(systemName: "chevron.right")
                    .font(.system(size: 8, weight: .semibold))
                    .foregroundStyle(.quaternary)
            }
        }
    }

    @ViewBuilder
    private func planFootnote(_ plan: [PlanEntry]) -> some View {
        // Only clients the daemon says are actually warming up. "never"
        // clients (codex) must not be promised a number that will not arrive,
        // and an old daemon without the field gets no claim at all.
        let calibrating = plan.filter { $0.calibrationState == "calibrating" }.map(\.client)
        if !calibrating.isEmpty {
            Text("plan share calibrating from your own limit history: \(calibrating.joined(separator: ", "))")
                .font(.system(size: 9.5))
                .foregroundStyle(.tertiary)
        }
    }

    private var footer: some View {
        HStack(spacing: 10) {
            Button {
                openMain(selecting: nil)
            } label: {
                Label("Open agentacct", systemImage: "macwindow")
                    .font(.system(size: 11.5, weight: .medium))
            }
            .buttonStyle(.plain)
            .foregroundStyle(Theme.blue)
            Spacer()
            Button {
                state.refreshNow()
            } label: {
                Image(systemName: "arrow.clockwise")
                    .font(.system(size: 11))
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .help("Refresh now")
            Button {
                NSApplication.shared.terminate(nil)
            } label: {
                Image(systemName: "power")
                    .font(.system(size: 11))
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .help("Quit agentacct")
        }
        .padding(.top, 2)
    }

    private func openMain(selecting sessionId: String?) {
        if let sessionId {
            selection.pane = .sessions
            selection.sessionId = sessionId
        }
        openWindow(id: "main")
        NSApp.activate(ignoringOtherApps: true)
        Task { await dashboard.refresh() }
    }
}

/// A plain row button with a soft hover highlight (menu-item feel).
struct MenuHoverButton<Content: View>: View {
    let action: () -> Void
    @ViewBuilder let content: () -> Content
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            content()
                .padding(.horizontal, 7)
                .padding(.vertical, 4.5)
                .background(
                    hovering ? AnyShapeStyle(.quaternary.opacity(0.7)) : AnyShapeStyle(.clear),
                    in: RoundedRectangle(cornerRadius: 6, style: .continuous)
                )
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
    }
}
