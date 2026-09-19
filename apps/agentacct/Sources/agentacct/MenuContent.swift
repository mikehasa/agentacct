import ServiceManagement
import SwiftUI

// The menu-bar surface is an instrument panel, not a second dashboard. It
// answers the account question first, keeps usage and quota evidence distinct,
// and sends deeper work to the main window.
struct MenuContent: View {
    @Environment(GlanceState.self) var state
    @Environment(DashboardStore.self) var dashboard
    @Environment(AppSelection.self) var selection
    @Environment(\.openWindow) private var openWindow
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var showsRefreshProgress = false
    @State private var isStartingRecorder = false
    private let buildIdentity: AppBuildIdentity
    private let lastUpdatedTextOverride: String?
    private let launchAtLoginInitialState: Bool?
    private let snapshotBodyMaxHeight: CGFloat?
    private let awaitRecorderSynchronization: () async -> SetupModel.AutomaticUpgradeOutcome
    /// Starts the local recorder from the menu bar (an in-app `agentacct start`).
    /// Returns whether the recorder became ready. nil disables the button (design
    /// review / snapshot fixtures), which keeps the passive `agentacct start` chip.
    private let onStartRecorder: (() async -> Bool)?

    init(
        buildIdentity: AppBuildIdentity = .current,
        lastUpdatedTextOverride: String? = nil,
        launchAtLoginInitialState: Bool? = nil,
        snapshotBodyMaxHeight: CGFloat? = nil,
        awaitRecorderSynchronization: @escaping () async -> SetupModel.AutomaticUpgradeOutcome = { .notNeeded },
        onStartRecorder: (() async -> Bool)? = nil
    ) {
        self.buildIdentity = buildIdentity
        self.lastUpdatedTextOverride = lastUpdatedTextOverride
        self.launchAtLoginInitialState = launchAtLoginInitialState
        self.snapshotBodyMaxHeight = snapshotBodyMaxHeight
        self.awaitRecorderSynchronization = awaitRecorderSynchronization
        self.onStartRecorder = onStartRecorder
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
        // The menu panel is the opaque canvas ground the snapshot harness
        // already paints (K84): no system material bleeds through captions.
        .background(Theme.canvas)
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
            // MenuBarExtra can propose only the footer's height. Preserve the
            // capped scroll body's ideal height instead of accepting zero.
            .fixedSize(horizontal: false, vertical: true)
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
            if let onStartRecorder {
                // The recorder can be revived without leaving the app: this runs
                // the same `agentacct start` and, if it cannot confirm readiness
                // (e.g. a dev backend), opens the window where recovery/setup lives.
                Button {
                    Task {
                        isStartingRecorder = true
                        let started = await onStartRecorder()
                        isStartingRecorder = false
                        if !started { openMain(selecting: nil) }
                    }
                } label: {
                    HStack(spacing: 6) {
                        if isStartingRecorder {
                            ProgressView().controlSize(.small)
                        } else {
                            Image(systemName: "play.circle.fill")
                        }
                        Text(isStartingRecorder ? "Starting recorder…" : "Start recorder")
                    }
                    .font(Type.captionSemibold)
                    .foregroundStyle(Theme.accent)
                    .contentShape(Rectangle())
                }
                .buttonStyle(QuietButtonStyle(horizontalPadding: 6, verticalPadding: 4))
                .disabled(isStartingRecorder)
                .help("Start the local recorder (agentacct start)")
                .accessibilityIdentifier("menu.start-recorder")
            } else {
                Text("agentacct start")
                    .font(Type.dataSmall)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .background(Theme.chipBg, in: RoundedRectangle(cornerRadius: Metrics.radius))
            }
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
                    CapsLabel(text: limits.primary?.windowLabel ?? "Limit")
                    Spacer()
                    if state.isRefreshing {
                        ProgressView().controlSize(.mini)
                            .accessibilityLabel("Refreshing")
                    }
                }

                if let primary = limits.primary {
                    HStack(alignment: .lastTextBaseline, spacing: 7) {
                        Text(primary.valueText)
                            .font(Face.monoFont(28, .bold))
                            .foregroundStyle(primary.usedPercent.map { Theme.limitTextColor(usedPercent: $0) } ?? Theme.muted)
                            .lineLimit(1)
                            .minimumScaleFactor(0.5)
                        Spacer()
                        Image(systemName: "chevron.forward")
                            .workFont(.icon)
                            .foregroundStyle(Theme.muted)
                    }
                    MenuLimitMeter(usedPercent: primary.usedPercent, resetPassed: primary.resetPassed)
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        MenuLimitSourceLabel(item: primary, emphasized: false)
                        Spacer(minLength: 8)
                        Text(primary.resetText)
                            .font(Type.caption)
                            .lineLimit(1)
                            .layoutPriority(1)
                    }
                    .foregroundStyle(Theme.muted)
                    if let caption = freshnessCaption(primary) {
                        // Data age first (from the provider capture time);
                        // the poll time is the secondary fact (K32).
                        Text(caption)
                            .font(Type.caption)
                            .foregroundStyle(Theme.muted)
                            .lineLimit(1)
                    }
                } else {
                    HStack(alignment: .firstTextBaseline) {
                        Text("Unavailable")
                            .font(Face.monoFont(22, .bold))
                            .foregroundStyle(Theme.ink)
                        Spacer()
                        Image(systemName: "chevron.forward")
                            .workFont(.icon)
                            .foregroundStyle(Theme.muted)
                    }
                    MenuLimitMeter(usedPercent: nil)
                    Text(limits.hasStaleLimits ? "Live limit readings are stale" : "No live limit was reported")
                        .font(Type.caption)
                        .foregroundStyle(Theme.muted)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(QuietButtonStyle(horizontalPadding: 0, verticalPadding: 0))
        // NOT `.accessibilityElement(children: .ignore)`: a Button already
        // speaks as ONE element, and that modifier REPLACES it — the
        // control loses its button role and its press action with it
        // (K118). The label below is simply what the button says.
        .accessibilityLabel(heroAccessibilityLabel(limits.primary))
        .accessibilityHint("Opens Usage and limits")
        .accessibilityIdentifier("menu.weekly-limit")
    }

    /// `as of 1d 15h ago · checked just now`: the reading's age, then the poll.
    private func freshnessCaption(_ primary: MenuLimitItem?) -> String? {
        let checked = state.lastUpdated.map { "checked \(lastUpdatedTextOverride ?? dashboardFreshnessText($0))" }
        let parts = [primary?.dataAgeText, checked].compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    private func heroAccessibilityLabel(_ primary: MenuLimitItem?) -> String {
        let limit: String
        if let primary {
            limit = "\(primary.windowLabel), \(primary.valueText), \(primary.client), \(primary.resetText)"
        } else {
            limit = "No live limit reported"
        }
        if state.isRefreshing {
            return "\(limit), refreshing"
        }
        guard let caption = freshnessCaption(primary) else { return limit }
        return "\(limit), \(caption)"
    }

    private func usageLedger(_ usage: MenuUsagePresentation) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .firstTextBaseline) {
                SectionCaption(text: RecordedUsageVocabulary.title)
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
                        // A priced figure reads in ink; a named absence
                        // ("no usage recorded", "unpriced") in the muted
                        // absence style, keyed on the row's priced state.
                        Text(row.costText)
                            .font(row.isPriced ? Type.dataSmallSemibold : Type.caption)
                            .foregroundStyle(row.isPriced ? Theme.ink : Theme.muted)
                            .lineLimit(1)
                            .fixedSize()
                            .layoutPriority(1)
                        if let tokenText = row.tokenText {
                            Text(tokenText)
                                .font(Type.dataSmall)
                                .foregroundStyle(Theme.muted)
                                .frame(width: 96, alignment: .trailing)
                                .lineLimit(1)
                        } else {
                            // No usage recorded: the absence above is the
                            // whole fact; the token column stays empty.
                            Color.clear.frame(width: 96, height: 1)
                        }
                    }
                    .padding(.vertical, 5)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(usageAccessibilityLabel(row))
                }
            }

            if let legend = usage.legendText {
                Text(legend)
                    .font(Type.caption)
                    .foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func usageAccessibilityLabel(_ row: MenuUsageRow) -> String {
        let cost = [row.costText, row.basisText].compactMap { $0 }.joined(separator: ", ")
        guard let tokenText = row.tokenText else { return "\(row.label), \(cost)" }
        let tokens = tokenText == PayloadAbsence.tokens
            ? "fresh tokens not reported"
            : "\(tokenText) fresh tokens"
        return "\(row.label), \(cost), \(tokens)"
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
                        // ONE layout for every client: source and percent on
                        // the title line, the reset caption on its own line,
                        // then a full-width meter — so rows never switch
                        // between inline and stacked resets, and the bars
                        // stay comparable (C62).
                        VStack(alignment: .leading, spacing: 2) {
                            HStack(alignment: .firstTextBaseline, spacing: 8) {
                                MenuLimitSourceLabel(item: item, emphasized: true)
                                Spacer(minLength: 8)
                                otherLimitPercent(item)
                            }
                            otherLimitReset(item)
                        }
                        MenuLimitMeter(usedPercent: item.usedPercent, resetPassed: item.resetPassed)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(QuietButtonStyle(horizontalPadding: 6, verticalPadding: 6))
                // A full-row quiet button pays for its own hit padding: the
                // negative outer inset puts the row's text and meter back on
                // the section's edge, so the secondary meters are as long as
                // the hero's and their threshold ticks line up down the
                // popover (K25).
                .padding(.horizontal, -6)
                // NOT `.accessibilityElement(children: .ignore)`: a Button already
                // speaks as ONE element, and that modifier REPLACES it — the
                // control loses its button role and its press action with it
                // (K118). The label below is simply what the button says.
                .accessibilityLabel(limitAccessibilityLabel(item))
                .accessibilityHint("Opens Usage and limits")
                .accessibilityIdentifier("menu.limit.\(item.id)")
            }
        }
    }

    private func otherLimitReset(_ item: MenuLimitItem) -> some View {
        Text(([item.resetText] + [item.dataAgeText].compactMap { $0 }).joined(separator: " · "))
            .font(Type.caption)
            .foregroundStyle(Theme.muted)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func otherLimitPercent(_ item: MenuLimitItem) -> some View {
        // A passed-reset share is history: muted, never a threshold color.
        Text(item.valueText)
            .font(Type.dataSmallSemibold)
            .foregroundStyle(
                item.resetPassed
                    ? Theme.muted
                    : (item.usedPercent.map { Theme.limitTextColor(usedPercent: $0) } ?? Theme.muted)
            )
            .fixedSize()
    }

    private func limitAccessibilityLabel(_ item: MenuLimitItem) -> String {
        ([item.sourceLabel, item.valueText, item.resetText] + [item.dataAgeText].compactMap { $0 })
            .joined(separator: ", ")
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
                    Button("+\(hiddenCount) in Sessions") {
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
                calibrationNote(calibration)
                    .padding(.top, 2)
            }
        }
    }

    /// The client-level plan-share state, always muted (never amber): the
    /// caveat marker (plan share is not an evidence tier, so no pip) and ONE
    /// plain-language line (the
    /// reducer headline). The reducer's technical detail (fit ratio, trusted
    /// band) stays behind the help affordance rather than an always-visible
    /// jargon paragraph.
    private func calibrationNote(_ calibration: MenuCalibrationPresentation) -> some View {
        HStack(alignment: .top, spacing: 6) {
            CaveatMarker()
                .padding(.top, 2)
            (Text(calibration.client).font(Type.dataSmall)
                + Text(" " + ([calibration.headline] + [calibration.progressText].compactMap { $0 })
                    .joined(separator: " · "))
                    .font(Type.caption))
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityLabel(calibration.summary)
            Spacer(minLength: 4)
            if let detail = calibration.detail {
                ContextHelp(
                    title: "Plan share",
                    message: detail,
                    summary: "Why this plan share is not shown",
                    identifier: "menu.calibration-help"
                )
                .padding(.top, -6)
            }
        }
        .foregroundStyle(Theme.muted)
    }

    private func sessionRow(_ session: RecentSession) -> some View {
        Button {
            openMain(selecting: .session("\(session.client)::\(session.sessionId)"))
        } label: {
            // The row's glyph aligns to the TITLE's baseline, not the middle
            // of a two- or three-line block (K25).
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    sessionTitle(session)
                    // The title keeps the full row; the plan share sits on
                    // the metadata line, or its own line when both don't fit.
                    ViewThatFits(in: .horizontal) {
                        HStack(alignment: .firstTextBaseline, spacing: 6) {
                            sessionMetadataText(session)
                            if let share = sessionShareText(session) {
                                Spacer(minLength: 4)
                                sessionShare(share)
                            }
                        }
                        VStack(alignment: .leading, spacing: 2) {
                            sessionMetadataText(session)
                            if let share = sessionShareText(session) {
                                sessionShare(share)
                            }
                        }
                    }
                }
                Spacer(minLength: 8)
                Image(systemName: "chevron.forward")
                    .workFont(.icon)
                    .foregroundStyle(Theme.muted)
            }
            .frame(minHeight: 30)
            .contentShape(Rectangle())
        }
        .buttonStyle(QuietButtonStyle(horizontalPadding: 6, verticalPadding: 3))
        .padding(.horizontal, -6)
        // NOT `.accessibilityElement(children: .ignore)`: a Button already
        // speaks as ONE element, and that modifier REPLACES it — the
        // control loses its button role and its press action with it
        // (K118). The label below is simply what the button says.
        .accessibilityLabel(sessionAccessibilityLabel(session))
        .accessibilityHint("Opens this work session")
        .accessibilityIdentifier("menu.session.\(session.sessionId)")
    }

    @ViewBuilder
    private func sessionTitle(_ session: RecentSession) -> some View {
        if let title = PayloadAbsence.text(session.title) {
            Text(title)
                .font(Type.captionSemibold)
                .foregroundStyle(Theme.ink)
                .lineLimit(1)
        } else {
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                Text(session.client)
                    .font(Type.dataSmallSemibold)
                Text("session")
                    .font(Type.captionSemibold)
            }
            .foregroundStyle(Theme.ink)
            .lineLimit(1)
        }
    }

    /// The Task's decision word (coral only for danger decisions such as
    /// Finding / Blocked), then the id stub for an untitled row and the age.
    private func sessionMetadataText(_ session: RecentSession) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 0) {
            if let word = sessionStatusWord(session) {
                Text(word)
                    .font(Type.captionSemibold)
                    .foregroundStyle(sessionStatusTint(session))
                if !sessionMetadata(session).isEmpty {
                    Text(" · ").font(Type.dataSmall).foregroundStyle(Theme.muted)
                }
            }
            Text(sessionMetadata(session))
                .font(Type.dataSmall)
                .foregroundStyle(Theme.muted)
        }
        .lineLimit(1)
    }

    /// The Task decision label when the daemon joined one, else the recorded
    /// work status — both payload words.
    private func sessionStatusWord(_ session: RecentSession) -> String? {
        PayloadAbsence.text(session.decisionLabel) ?? PayloadAbsence.text(session.statusLabel)
    }

    private func sessionStatusTint(_ session: RecentSession) -> Color {
        guard PayloadAbsence.text(session.decisionLabel) != nil else { return Theme.muted }
        return DecisionTintClass.forKey(session.decisionKey) == .danger ? Theme.coral : Theme.ink
    }

    private func sessionShare(_ share: String) -> some View {
        Text(share)
            .font(Type.dataSmallSemibold)
            .foregroundStyle(Theme.muted)
            .lineLimit(1)
            .fixedSize()
    }

    /// The reducer's plan-share headline (`≈0.2% of weekly plan`) for a
    /// session whose client is calibrated. Uncalibrated states are named once
    /// for the client by the calibration note, not repeated on every row.
    /// A row with no share of its own (`row_no_share`) omits the share: the
    /// menu names only a real calibrated share.
    private func sessionShareText(_ session: RecentSession) -> String? {
        guard let share = session.planShare, share.calibrationState == "calibrated", share.pct != nil else {
            return nil
        }
        return share.headlineText
    }

    /// The id stub only identifies an UNTITLED row; a titled row never repeats it.
    private func sessionIdStub(_ session: RecentSession) -> String? {
        PayloadAbsence.text(session.title) == nil ? session.shortSessionId : nil
    }

    private func sessionMetadata(_ session: RecentSession) -> String {
        [sessionIdStub(session), agoText(session.lastActivityAt)]
            .compactMap { $0 }
            .joined(separator: " · ")
    }

    private func sessionAccessibilityLabel(_ session: RecentSession) -> String {
        let title = PayloadAbsence.text(session.title) ?? "\(session.client) session"
        return [
            title,
            sessionIdStub(session),
            sessionStatusWord(session),
            agoText(session.lastActivityAt),
            sessionShareText(session),
        ]
            .compactMap { $0 }
            .joined(separator: ", ")
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

            ZStack {
                if showsRefreshProgress {
                    ProgressView()
                        .controlSize(.small)
                        .accessibilityLabel("Refreshing")
                        .transition(.opacity)
                } else {
                    footerButton(
                        systemImage: "arrow.clockwise",
                        help: "Refresh now",
                        identifier: "menu.refresh"
                    ) {
                        Task {
                            await performAfterRecorderSynchronization(
                                awaitReady: awaitRecorderSynchronization,
                                operation: { state.refreshNow() }
                            )
                        }
                    }
                    .disabled(state.isRefreshing)
                    .keyboardShortcut("r", modifiers: .command)
                    .transition(.opacity)
                }
            }
            .frame(width: 28, height: 28)
            .animation(
                reduceMotion ? Motion.reducedCrossfade : Motion.phaseCrossfade,
                value: showsRefreshProgress
            )
            .task(id: state.isRefreshing) {
                guard state.isRefreshing else {
                    showsRefreshProgress = false
                    return
                }
                try? await Task.sleep(for: .milliseconds(150))
                guard !Task.isCancelled else { return }
                showsRefreshProgress = refreshProgressVisible(
                    isRefreshing: state.isRefreshing,
                    delayElapsed: true
                )
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
                .workFont(.icon)
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
        Task {
            await presentWindowThenRefreshAfterRecorderSynchronization(
                awaitReady: awaitRecorderSynchronization,
                presentWindow: {
                    if let destination {
                        selection.open(destination)
                    }
                    openWindow(id: "main")
                    NSApp.activate(ignoringOtherApps: true)
                },
                refresh: {
                    await dashboard.refresh()
                }
            )
        }
    }
}

private struct MenuLimitMeter: View {
    let usedPercent: Double?
    var resetPassed: Bool = false

    static let height: CGFloat = 6
    static let thresholds: [Double] = [0.75, 0.9]

    var body: some View {
        if let usedPercent, resetPassed {
            // A share from before the window reset: drawn in the muted fill so
            // it reads as history, never as a current threshold (K32).
            Theme.MeterBar(
                fraction: max(0, min(usedPercent, 100)) / 100,
                tint: Theme.muted,
                height: Self.height,
                thresholds: Self.thresholds
            )
        } else if let usedPercent {
            // The one meter component: quiet tintNeutral track, fill-weight
            // limit tint, and the 75%/90% threshold ticks drawn outside the
            // bar so they read on any fill (C43/C58/C78).
            Theme.MeterBar(
                fraction: max(0, min(usedPercent, 100)) / 100,
                tint: Theme.limitFillColor(usedPercent: usedPercent),
                height: Self.height,
                thresholds: Self.thresholds
            )
        } else {
            // Not reported: a dashed outline in the same geometry (including
            // the tick allowance) so rows keep equal height and length.
            RoundedRectangle(cornerRadius: 2)
                .strokeBorder(Theme.rule, style: StrokeStyle(lineWidth: 1, dash: [3, 3]))
                .frame(height: Self.height)
                .padding(.vertical, MeterBar.tickOverhang)
                .accessibilityHidden(true)
        }
    }
}

/// `<client slug> · <window label>`: the slug in mono (the one client identity
/// every surface shows, C54), the reducer's window name beside it.
private struct MenuLimitSourceLabel: View {
    let item: MenuLimitItem
    let emphasized: Bool

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 4) {
            Text(item.client)
                .font(emphasized ? Type.dataSmallSemibold : Type.dataSmall)
            Text("· \(item.windowLabel)")
                .font(emphasized ? Type.captionSemibold : Type.caption)
        }
        .foregroundStyle(emphasized ? Theme.ink : Theme.muted)
        .lineLimit(1)
    }
}

/// "Launch at Login" via SMAppService — the app registers itself. State reads
/// back from the service so the control reflects changes made in Settings.
struct LaunchAtLoginToggle: View {
    @State private var enabled: Bool
    @State private var updateMessage: String?

    init(initialEnabled: Bool? = nil) {
        _enabled = State(
            initialValue: initialEnabled ?? (SMAppService.mainApp.status == .enabled)
        )
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Toggle(isOn: Binding(
                get: { enabled },
                set: { wanted in
                    updateMessage = nil
                    do {
                        if wanted {
                            try SMAppService.mainApp.register()
                        } else {
                            try SMAppService.mainApp.unregister()
                        }
                    } catch {
                        updateMessage = "Could not change launch at login: \(error.localizedDescription)"
                    }
                    enabled = SMAppService.mainApp.status == .enabled
                    if updateMessage == nil, wanted, SMAppService.mainApp.status == .requiresApproval {
                        updateMessage = "Allow agentacct in macOS Login Items settings to finish enabling launch at login."
                    }
                }
            )) {
                Text("Launch at login")
            }
            .toggleStyle(MenuCheckboxToggleStyle())
            .help("Launch agentacct at login")
            .accessibilityIdentifier("menu.launch-at-login")
            if let updateMessage {
                Text(updateMessage).workFont(.caption).foregroundStyle(Theme.amber)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("menu.launch-at-login.feedback")
            }
        }
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
