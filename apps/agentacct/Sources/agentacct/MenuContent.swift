import ServiceManagement
import SwiftUI

// The dropdown: a glance, not a workspace. The weekly-plan hero (account
// truth, the subscription user's real question), today's cost as reference,
// live limit meters, and root sessions — click anything deep to open the
// full window. Honesty rules carried from the TUI: costs are complete-$ /
// partial-~$ / em-dash (never a fabricated $0), plan shares only when
// calibrated, statuses use the server-side reduction. Stale limit accounts
// are hidden here (the full window has the toggle).

struct MenuContent: View {
    @EnvironmentObject var state: GlanceState
    @EnvironmentObject var dashboard: DashboardStore
    @EnvironmentObject var selection: AppSelection
    @Environment(\.openWindow) private var openWindow
    private let buildIdentity: AppBuildIdentity
    private let lastUpdatedTextOverride: String?
    private let launchAtLoginInitialState: Bool?

    init(
        buildIdentity: AppBuildIdentity = .current,
        lastUpdatedTextOverride: String? = nil,
        launchAtLoginInitialState: Bool? = nil
    ) {
        self.buildIdentity = buildIdentity
        self.lastUpdatedTextOverride = lastUpdatedTextOverride
        self.launchAtLoginInitialState = launchAtLoginInitialState
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            switch state.phase {
            case .connecting:
                waitingView("Connecting to the agentacct daemon…")
            case .disconnected(let reason):
                disconnectedView(reason: reason)
            case .incompatible(let message):
                Label { Text(message) } icon: { Image(systemName: "exclamationmark.triangle.fill") }
                    .font(Type.caption)
                    .foregroundStyle(Theme.amber)
            case .connected(let snapshot):
                connectedView(snapshot: snapshot)
            }
            buildIdentityLine
            footer
        }
        .padding(14)
        .frame(width: 360)
    }

    // MARK: states

    private func waitingView(_ text: String) -> some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            Text(text).foregroundStyle(Theme.muted)
        }
        .font(Type.caption)
        .padding(.vertical, 12)
    }

    private func disconnectedView(reason: String) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Label("Daemon not reachable", systemImage: "bolt.slash.fill")
                .font(Type.captionSemibold)
                .foregroundStyle(Theme.muted)
            Text(reason)
                .font(Type.caption)
                .foregroundStyle(Theme.muted)
                .lineLimit(2)
            Text("agentacct start")
                .font(Type.dataSmall)
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(Theme.chipBg, in: RoundedRectangle(cornerRadius: Metrics.radius))
        }
        .padding(.vertical, 6)
    }

    // MARK: connected

    @ViewBuilder
    private func connectedView(snapshot: GlanceSnapshot) -> some View {
        let glance = snapshot.glance
        let windows = Dictionary(uniqueKeysWithValues: glance.usage.windows.map { ($0.label, $0.totals) })
        let sevenDay = GlanceState.sevenDayUsedPercent(glance)

        // Hero: the weekly plan (account truth). Falls back to today's cost
        // when no provider reading exists — never a fabricated %.
        VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline) {
                CapsLabel(text: sevenDay != nil ? "WEEKLY PLAN · 7D" : "TODAY")
                Spacer()
                if state.isRefreshing {
                    ProgressView().controlSize(.mini)
                } else if let updated = state.lastUpdated {
                    Group {
                        if let lastUpdatedTextOverride {
                            Text(lastUpdatedTextOverride)
                        } else {
                            Text(updated, style: .relative)
                        }
                    }
                        .font(Type.dataSmall)
                        .foregroundStyle(Theme.muted)
                }
            }
            if let used = sevenDay {
                Text("\(Int(used.rounded()))%")
                    .font(Face.monoFont(24, .bold))
                LimitMeter(usedPercent: used)
                    .padding(.vertical, 2)
                Text("today \(windows["today"]?.costText ?? "—") · \(windows["today"]?.tokensText ?? "—") fresh tok")
                    .font(Type.dataSmall)
                    .foregroundStyle(Theme.muted)
            } else {
                Text(windows["today"]?.costText ?? "—")
                    .font(Face.monoFont(24, .bold))
                Text("\(windows["today"]?.tokensText ?? "—") fresh tokens")
                    .font(Type.dataSmall)
                    .foregroundStyle(Theme.muted)
            }
        }

        HStack(spacing: 8) {
            StatTile(label: "7 days",
                     value: windows["last 7 days"]?.costText ?? "—",
                     detail: (windows["last 7 days"]?.tokensText).map { "\($0) tok" })
            StatTile(label: "30 days",
                     value: windows["last 30 days"]?.costText ?? "—",
                     detail: (windows["last 30 days"]?.tokensText).map { "\($0) tok" })
        }
        Text("≈ costs are pricing estimates from client-reported tokens")
            .font(Type.caption)
            .foregroundStyle(Theme.muted)

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
                    RoundedRectangle(cornerRadius: 1)
                        .fill(Theme.muted)
                        .frame(width: 3, height: 12)
                    Text("\(limit.client ?? "?") \(window.kind ?? "")")
                        .font(Type.caption)
                        .foregroundStyle(Theme.muted)
                        .frame(width: 96, alignment: .leading)
                    LimitMeter(usedPercent: used)
                    Text(String(format: "%.0f%%", used))
                        .font(Type.dataSmallSemibold)
                        .frame(width: 34, alignment: .trailing)
                    Text(Theme.resetsIn(window.resetsAt) ?? "")
                        .font(Type.dataSmall)
                        .foregroundStyle(Theme.muted)
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
                // Lifecycle marker as a bar, not a dot — filled circles are
                // the evidence-tier pip vocabulary.
                RoundedRectangle(cornerRadius: 1)
                    .fill(Theme.statusColor(session.status))
                    .frame(width: 3, height: 14)
                Text(session.title ?? "\(session.client) · \(session.shortSessionId)")
                    .font(Type.caption)
                    .lineLimit(1)
                    .truncationMode(.tail)
                Spacer(minLength: 8)
                if let pct = session.planPctText {
                    Text(pct)
                        .font(Type.dataSmallSemibold)
                        .foregroundStyle(Theme.muted)
                }
                Image(systemName: "chevron.right")
                    .font(.system(size: 8, weight: .semibold))
                    .foregroundStyle(Theme.muted)
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
                .font(Type.caption)
                .foregroundStyle(Theme.muted)
        }
    }

    private var footer: some View {
        HStack(spacing: 10) {
            Button {
                openMain(selecting: nil)
            } label: {
                Label("Open agentacct", systemImage: "macwindow")
                    .font(Type.captionSemibold)
            }
            .buttonStyle(.plain)
            .foregroundStyle(Theme.accent)
            Spacer()
            LaunchAtLoginToggle(initialEnabled: launchAtLoginInitialState)
            Button {
                state.refreshNow()
            } label: {
                Image(systemName: "arrow.clockwise")
                    .font(.system(size: 11))
            }
            .buttonStyle(.plain)
            .foregroundStyle(Theme.muted)
            .help("Refresh now")
            Button {
                NSApplication.shared.terminate(nil)
            } label: {
                Image(systemName: "power")
                    .font(.system(size: 11))
            }
            .buttonStyle(.plain)
            .foregroundStyle(Theme.muted)
            .help("Quit agentacct")
        }
        .padding(.top, 2)
    }

    private var buildIdentityLine: some View {
        Text(buildIdentity.compactLabel)
            .font(Type.dataSmall)
            .foregroundStyle(Theme.muted)
            .frame(maxWidth: .infinity, alignment: .leading)
            .help(buildIdentity.detailLabel)
            .accessibilityLabel(buildIdentity.accessibilityLabel)
            .accessibilityIdentifier("menu.build-identity")
    }

    private func openMain(selecting sessionId: String?) {
        if let sessionId {
            // The Work surface is Task-primary; it resolves this session key to
            // its Task when the compact task summary carries an exact match.
            selection.open(.session(sessionId))
        }
        openWindow(id: "main")
        NSApp.activate(ignoringOtherApps: true)
        Task { await dashboard.refresh() }
    }
}

/// "Launch at Login" via SMAppService — the app registers ITSELF (the OS
/// shows it in System Settings › Login Items under this app's name; no
/// hidden helpers, nothing system-wide). State reads back from the service
/// so the toggle always reflects reality, including changes made in System
/// Settings.
struct LaunchAtLoginToggle: View {
    @State private var enabled: Bool

    init(initialEnabled: Bool? = nil) {
        _enabled = State(
            initialValue: initialEnabled ?? (SMAppService.mainApp.status == .enabled)
        )
    }

    var body: some View {
        Toggle(isOn: Binding(
            get: { enabled },
            set: { wanted in
                do {
                    if wanted {
                        try SMAppService.mainApp.register()
                    } else {
                        try SMAppService.mainApp.unregister()
                    }
                } catch {
                    // Registration can fail for an unsigned dev build moved
                    // mid-run; reflect reality rather than pretending.
                }
                enabled = SMAppService.mainApp.status == .enabled
            }
        )) {
            Text("Login")
        }
        .toggleStyle(MenuCheckboxToggleStyle())
        .help("Launch agentacct at login")
    }
}

/// A SwiftUI-only checkbox keeps the production control fully keyboard and
/// accessibility operable while remaining identical in off-screen snapshots.
private struct MenuCheckboxToggleStyle: ToggleStyle {
    func makeBody(configuration: Configuration) -> some View {
        Button {
            configuration.isOn.toggle()
        } label: {
            HStack(spacing: 4) {
                Image(systemName: configuration.isOn ? "checkmark.square" : "square")
                configuration.label
            }
            .font(Type.caption)
            .foregroundStyle(Theme.muted)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Launch agentacct at login")
        .accessibilityValue(configuration.isOn ? "On" : "Off")
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
                    hovering ? AnyShapeStyle(Theme.tintNeutral) : AnyShapeStyle(.clear),
                    in: RoundedRectangle(cornerRadius: Metrics.radius)
                )
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
    }
}
