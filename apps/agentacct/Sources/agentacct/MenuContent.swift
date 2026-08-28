import ServiceManagement
import SwiftUI

// The menu-bar surface is an instrument panel, not a second dashboard. It
// answers the account question first, keeps usage and quota evidence distinct,
// and sends deeper work to the main window.
struct MenuContent: View {
    @EnvironmentObject var state: GlanceState
    @EnvironmentObject var dashboard: DashboardStore
    @EnvironmentObject var selection: AppSelection
    @Environment(\.openWindow) private var openWindow
    private let buildIdentity: AppBuildIdentity
    private let lastUpdatedTextOverride: String?
    private let launchAtLoginInitialState: Bool?
    private let snapshotBodyMaxHeight: CGFloat?

    init(
        buildIdentity: AppBuildIdentity = .current,
        lastUpdatedTextOverride: String? = nil,
        launchAtLoginInitialState: Bool? = nil,
        snapshotBodyMaxHeight: CGFloat? = nil
    ) {
        self.buildIdentity = buildIdentity
        self.lastUpdatedTextOverride = lastUpdatedTextOverride
        self.launchAtLoginInitialState = launchAtLoginInitialState
        self.snapshotBodyMaxHeight = snapshotBodyMaxHeight
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            switch state.phase {
            case .connecting:
                waitingView("Connecting to the agentacct daemon…")
                    .padding(14)
            case .disconnected(let reason):
                disconnectedView(reason: reason)
                    .padding(14)
            case .incompatible(let message):
                Label(message, systemImage: "exclamationmark.triangle.fill")
                    .font(Type.caption)
                    .foregroundStyle(Theme.amber)
                    .padding(14)
            case .connected(let snapshot):
                menuBody {
                    connectedView(snapshot: snapshot)
                }
            }

            Rectangle()
                .fill(Theme.hairline)
                .frame(height: 1)
            footer
                .padding(.horizontal, 10)
                .padding(.vertical, 7)
        }
        .frame(width: 360)
    }

    @ViewBuilder
    private func menuBody<Content: View>(@ViewBuilder content: () -> Content) -> some View {
        if SnapshotMode.enabled, let snapshotBodyMaxHeight {
            content()
                .padding(14)
                .frame(height: snapshotBodyMaxHeight, alignment: .top)
                .clipped()
        } else if SnapshotMode.enabled {
            content()
                .padding(14)
        } else {
            ScrollView(showsIndicators: true) {
                content()
                    .padding(14)
            }
            .scrollBounceBehavior(.basedOnSize)
            .frame(maxHeight: 420)
        }
    }

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

    private func connectedView(snapshot: GlanceSnapshot) -> some View {
        let glance = snapshot.glance
        let limits = MenuLimitPresentation(glance: glance)
        let usage = MenuUsagePresentation(usage: glance.usage)

        return VStack(alignment: .leading, spacing: Space.m) {
            limitHero(limits)
            usageLedger(usage)

            if !limits.secondary.isEmpty {
                otherLimits(limits)
            }

            sessions(glance.recentSessions, plan: glance.plan)
        }
    }

    private func limitHero(_ limits: MenuLimitPresentation) -> some View {
        Button {
            openMain(selecting: .limits)
        } label: {
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline) {
                    CapsLabel(text: "WEEKLY LIMIT")
                    Spacer()
                    if state.isRefreshing {
                        ProgressView().controlSize(.mini)
                            .accessibilityLabel("Refreshing")
                    } else if let updated = state.lastUpdated {
                        Text("Updated \(lastUpdatedTextOverride ?? dashboardFreshnessText(updated))")
                            .font(Type.dataSmall)
                            .foregroundStyle(Theme.muted)
                    }
                }

                if let primary = limits.primary {
                    HStack(alignment: .lastTextBaseline, spacing: 7) {
                        Text(primary.percentageText)
                            .font(Face.monoFont(28, .bold))
                            .foregroundStyle(Theme.ink)
                        Text("used")
                            .font(Type.captionSemibold)
                            .foregroundStyle(Theme.muted)
                        Spacer()
                        Image(systemName: "chevron.forward")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(Theme.muted)
                    }
                    MenuLimitMeter(usedPercent: primary.usedPercent)
                    HStack(spacing: 8) {
                        Text(primary.sourceLabel)
                            .lineLimit(1)
                        Spacer(minLength: 8)
                        Text(primary.resetText.map { "Resets in \($0)" } ?? "Reset not reported")
                            .layoutPriority(1)
                    }
                    .font(Type.caption)
                    .foregroundStyle(Theme.muted)
                } else {
                    HStack(alignment: .firstTextBaseline) {
                        Text("Unavailable")
                            .font(Face.monoFont(22, .bold))
                            .foregroundStyle(Theme.ink)
                        Spacer()
                        Image(systemName: "chevron.forward")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(Theme.muted)
                    }
                    MenuLimitMeter(usedPercent: nil)
                    Text(limits.hasStaleLimits ? "Live 7-day usage is stale" : "No live 7-day limit was reported")
                        .font(Type.caption)
                        .foregroundStyle(Theme.muted)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(QuietButtonStyle(horizontalPadding: 0, verticalPadding: 0))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(heroAccessibilityLabel(limits.primary))
        .accessibilityHint("Opens Usage and limits")
        .accessibilityIdentifier("menu.weekly-limit")
    }

    private func heroAccessibilityLabel(_ primary: MenuLimitItem?) -> String {
        let limit: String
        if let primary {
            let reset = primary.resetText.map { ", resets in \($0)" } ?? ", reset not reported"
            limit = "Weekly limit, \(primary.percentageText) used, \(primary.sourceLabel)\(reset)"
        } else {
            limit = "Weekly limit unavailable"
        }
        if state.isRefreshing {
            return "\(limit), refreshing"
        }
        guard let updated = state.lastUpdated else { return limit }
        return "\(limit), updated \(lastUpdatedTextOverride ?? dashboardFreshnessText(updated))"
    }

    private func usageLedger(_ usage: MenuUsagePresentation) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .firstTextBaseline) {
                SectionCaption(text: "Tracked usage")
                    .accessibilityAddTraits(.isHeader)
                Spacer()
                Text("fresh tokens")
                    .font(Type.caption)
                    .foregroundStyle(Theme.muted)
            }
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(usage.rows.enumerated()), id: \.element.id) { index, row in
                    if index > 0 {
                        Rectangle()
                            .fill(Theme.hairline)
                            .frame(height: 1)
                    }
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(row.label)
                            .font(Type.captionSemibold)
                            .foregroundStyle(Theme.ink)
                            .frame(width: 82, alignment: .leading)
                        Spacer(minLength: 4)
                        Text(row.costText)
                            .font(Type.dataSmallSemibold)
                            .foregroundStyle(Theme.ink)
                            .lineLimit(1)
                        Text(row.tokenText)
                            .font(Type.dataSmall)
                            .foregroundStyle(Theme.muted)
                            .frame(width: 96, alignment: .trailing)
                            .lineLimit(1)
                    }
                    .padding(.vertical, 5)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(usageAccessibilityLabel(row))
                }
            }

            if let legend = usage.legendText {
                Text("Client-token pricing · \(legend)")
                    .font(Type.caption)
                    .foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func usageAccessibilityLabel(_ row: MenuUsageRow) -> String {
        let tokens = row.tokenText == "Not reported"
            ? "fresh tokens not reported"
            : "\(row.tokenText) fresh tokens"
        return "\(row.label), \(row.costText), \(tokens)"
    }

    private func otherLimits(_ limits: MenuLimitPresentation) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline) {
                SectionCaption(text: "Other limits")
                    .accessibilityAddTraits(.isHeader)
                Spacer()
                if limits.hiddenSecondaryCount > 0 {
                    Button("+\(limits.hiddenSecondaryCount) in Usage") {
                        openMain(selecting: .limits)
                    }
                    .buttonStyle(QuietButtonStyle(horizontalPadding: 3, verticalPadding: 0))
                    .font(Type.caption)
                    .foregroundStyle(Theme.accent)
                    .frame(minHeight: 28)
                    .accessibilityHint("Opens Usage and limits")
                    .accessibilityIdentifier("menu.limits-more")
                }
            }

            ForEach(limits.secondary) { item in
                Button {
                    openMain(selecting: .limits)
                } label: {
                    VStack(alignment: .leading, spacing: 5) {
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(item.sourceLabel)
                                .font(Type.captionSemibold)
                                .foregroundStyle(Theme.ink)
                                .lineLimit(1)
                            Spacer(minLength: 8)
                            Text(item.percentageText)
                                .font(Type.dataSmallSemibold)
                                .foregroundStyle(Theme.ink)
                        }
                        HStack(spacing: 8) {
                            MenuLimitMeter(usedPercent: item.usedPercent)
                            Text(item.resetText.map { "Resets in \($0)" } ?? "Reset not reported")
                                .font(Type.dataSmall)
                                .foregroundStyle(Theme.muted)
                                .fixedSize()
                        }
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(QuietButtonStyle(horizontalPadding: 6, verticalPadding: 6))
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(limitAccessibilityLabel(item))
                .accessibilityHint("Opens Usage and limits")
                .accessibilityIdentifier("menu.limit.\(item.id)")
            }
        }
    }

    private func limitAccessibilityLabel(_ item: MenuLimitItem) -> String {
        let reset = item.resetText.map { "resets in \($0)" } ?? "reset not reported"
        return "\(item.sourceLabel), \(item.percentageText) used, \(reset)"
    }

    private func sessions(_ allSessions: [RecentSession], plan: [PlanEntry]) -> some View {
        let visible = Array(allSessions.prefix(2))
        let hiddenCount = max(0, allSessions.count - visible.count)
        let calibration = MenuCalibrationPresentation(plan)

        return VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline) {
                SectionCaption(text: "Recent sessions")
                    .accessibilityAddTraits(.isHeader)
                Spacer()
                Text("last 6 hours")
                    .font(Type.caption)
                    .foregroundStyle(Theme.muted)
                if hiddenCount > 0 {
                    Button("+\(hiddenCount) in Work") {
                        openMain(selecting: .work)
                    }
                    .buttonStyle(QuietButtonStyle(horizontalPadding: 3, verticalPadding: 0))
                    .font(Type.captionSemibold)
                    .foregroundStyle(Theme.accent)
                    .frame(minHeight: 28)
                    .accessibilityHint("Opens all recent sessions")
                    .accessibilityIdentifier("menu.sessions-more")
                }
            }

            if visible.isEmpty {
                Text("No recent sessions")
                    .font(Type.caption)
                    .foregroundStyle(Theme.muted)
                    .padding(.vertical, 6)
            } else {
                ForEach(visible, id: \.sessionId) { session in
                    sessionRow(session)
                }
            }

            if let calibration {
                Label(calibration.summary, systemImage: "circle.dotted")
                    .font(Type.caption)
                    .foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
                    .help("Share appears after enough stable weekly-limit history is recorded.")
                    .accessibilityHint(calibration.detail ?? "More stable limit history is needed.")
                    .padding(.top, 2)
            }
        }
    }

    private func sessionRow(_ session: RecentSession) -> some View {
        Button {
            openMain(selecting: .session("\(session.client)::\(session.sessionId)"))
        } label: {
            HStack(spacing: 8) {
                Image(systemName: sessionStatusSymbol(session.status))
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(Theme.statusColor(session.status))
                    .frame(width: 14)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(session.title ?? "\(MenuLimitPresentation.clientLabel(session.client)) session")
                            .font(Type.captionSemibold)
                            .foregroundStyle(Theme.ink)
                            .lineLimit(1)
                        Spacer(minLength: 4)
                        if let pct = session.planPctText {
                            Text("\(pct) share")
                                .font(Type.dataSmallSemibold)
                                .foregroundStyle(Theme.muted)
                                .fixedSize()
                        }
                    }
                    Text(sessionMetadata(session))
                        .font(Type.dataSmall)
                        .foregroundStyle(Theme.muted)
                        .lineLimit(1)
                }
                Spacer(minLength: 8)
                Image(systemName: "chevron.forward")
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(Theme.muted)
            }
            .frame(minHeight: 30)
            .contentShape(Rectangle())
        }
        .buttonStyle(QuietButtonStyle(horizontalPadding: 6, verticalPadding: 3))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(sessionAccessibilityLabel(session))
        .accessibilityHint("Opens this work session")
        .accessibilityIdentifier("menu.session.\(session.sessionId)")
    }

    private func sessionMetadata(_ session: RecentSession) -> String {
        [session.shortSessionId, statusLabel(session.status), agoText(session.lastActivityAt)]
            .compactMap { $0 }
            .joined(separator: " · ")
    }

    private func sessionAccessibilityLabel(_ session: RecentSession) -> String {
        let title = session.title ?? "\(MenuLimitPresentation.clientLabel(session.client)) session"
        return [
            title,
            session.shortSessionId,
            statusLabel(session.status),
            agoText(session.lastActivityAt),
            session.planPctText.map { "\($0) plan share" },
        ]
            .compactMap { $0 }
            .joined(separator: ", ")
    }

    private func statusLabel(_ status: String?) -> String? {
        status?.replacingOccurrences(of: "_", with: " ")
    }

    private func sessionStatusSymbol(_ status: String?) -> String {
        switch status {
        case "blocked", "failed": return "exclamationmark.triangle.fill"
        case "handed_off": return "arrow.up.right"
        case "completed": return "checkmark"
        case "in_progress", "started", "checkpoint": return "circle.fill"
        default: return "circle"
        }
    }

    // MARK: footer

    private var footer: some View {
        HStack(spacing: 3) {
            Button {
                openMain(selecting: nil)
            } label: {
                Label("Open", systemImage: "macwindow")
                    .font(Type.captionSemibold)
                    .foregroundStyle(Theme.accent)
            }
            .buttonStyle(QuietButtonStyle(horizontalPadding: 4, verticalPadding: 7))
            .accessibilityLabel("Open agentacct")
            .accessibilityHint("Opens the main window")
            .accessibilityIdentifier("menu.open")

            Spacer(minLength: 2)
            LaunchAtLoginToggle(initialEnabled: launchAtLoginInitialState)

            Group {
                if state.isRefreshing {
                    ProgressView()
                        .controlSize(.small)
                        .frame(width: 28, height: 28)
                        .accessibilityLabel("Refreshing")
                } else {
                    footerButton(
                        systemImage: "arrow.clockwise",
                        help: "Refresh now",
                        identifier: "menu.refresh"
                    ) {
                        state.refreshNow()
                    }
                    .keyboardShortcut("r", modifiers: .command)
                }
            }

            footerButton(
                systemImage: "info.circle",
                help: "About agentacct",
                identifier: "menu.about"
            ) {
                AppAbout.present(identity: buildIdentity)
            }

            footerButton(
                systemImage: "power",
                help: "Quit agentacct",
                identifier: "menu.quit"
            ) {
                NSApplication.shared.terminate(nil)
            }
            .keyboardShortcut("q", modifiers: .command)
        }
    }

    private func footerButton(
        systemImage: String,
        help: String,
        identifier: String,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Image(systemName: systemImage)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(Theme.muted)
                .frame(width: 28, height: 28)
                .contentShape(Rectangle())
        }
        .buttonStyle(QuietButtonStyle(horizontalPadding: 0, verticalPadding: 0))
        .help(help)
        .accessibilityLabel(help)
        .accessibilityIdentifier(identifier)
    }

    private func openMain(selecting destination: DashboardDestination?) {
        if let destination {
            selection.open(destination)
        }
        openWindow(id: "main")
        NSApp.activate(ignoringOtherApps: true)
        Task { await dashboard.refresh() }
    }
}

private struct MenuLimitMeter: View {
    let usedPercent: Double?

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                if let usedPercent {
                    RoundedRectangle(cornerRadius: 2)
                        .fill(Theme.rule)
                    RoundedRectangle(cornerRadius: 2)
                        .fill(Theme.limitColor(usedPercent: usedPercent))
                        .frame(width: proxy.size.width * max(0, min(usedPercent, 100)) / 100)
                } else {
                    RoundedRectangle(cornerRadius: 2)
                        .strokeBorder(Theme.rule, style: StrokeStyle(lineWidth: 1, dash: [3, 3]))
                }
            }
        }
        .frame(height: 6)
        .accessibilityHidden(true)
    }
}

/// "Launch at Login" via SMAppService — the app registers itself. State reads
/// back from the service so the control reflects changes made in Settings.
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
                    // Reflect the service's actual state for unsigned/moved builds.
                }
                enabled = SMAppService.mainApp.status == .enabled
            }
        )) {
            Text("Launch at login")
        }
        .toggleStyle(MenuCheckboxToggleStyle())
        .help("Launch agentacct at login")
        .accessibilityIdentifier("menu.launch-at-login")
    }
}

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
            .frame(minHeight: 28)
            .contentShape(Rectangle())
        }
        .buttonStyle(QuietButtonStyle(horizontalPadding: 4, verticalPadding: 0))
        .accessibilityLabel("Launch agentacct at login")
        .accessibilityValue(configuration.isOn ? "On" : "Off")
    }
}
