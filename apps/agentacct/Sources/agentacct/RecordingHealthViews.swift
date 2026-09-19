import SwiftUI

extension RecordingHealthTone {
    var color: Color {
        switch self {
        case .neutral: return Theme.muted
        case .positive: return Theme.green
        case .caution: return Theme.amber
        case .failure: return Theme.coral
        }
    }

    var symbol: String {
        switch self {
        case .neutral: return "waveform.path"
        case .positive: return "checkmark.circle"
        case .caution: return "exclamationmark.circle"
        case .failure: return "exclamationmark.triangle"
        }
    }
}

/// A one-click recovery control for an unreachable recorder. `onRestart` runs the
/// same `agentacct start` the CLI would; `inFlight` reflects the in-progress start
/// so the button can show progress and disable itself. It is supplied only when
/// the app owns a matching recorder it can actually start.
struct RecorderRestartControl {
    var inFlight: Bool
    var onRestart: () -> Void
}

/// The "Start recorder" button shown on the unreachable-recorder health cause, so
/// a user revives the recorder from inside the app instead of the terminal.
struct RecorderRestartButton: View {
    let control: RecorderRestartControl
    var identifier: String

    var body: some View {
        Button {
            control.onRestart()
        } label: {
            HStack(spacing: 6) {
                if control.inFlight {
                    ProgressView().controlSize(.small)
                    Text("Starting recorder…")
                } else {
                    Image(systemName: "play.circle")
                    Text("Start recorder")
                }
            }
        }
        .buttonStyle(NativeSetupActionStyle())
        .disabled(control.inFlight)
        .accessibilityIdentifier(identifier)
    }
}

struct RecordingHealthToolbarButton: View {
    let snapshot: RecordingHealthSnapshot
    var coordinator: RecordingHealthCoordinator? = nil
    var restart: RecorderRestartControl? = nil
    let onSetup: () -> Void
    var onSetupCause: ((RecordingHealthCause) -> Void)? = nil
    var onActivateClient: ((String) -> Void)? = nil
    let onSources: () -> Void
    let onRefresh: () -> Void
    /// The top bar's compact alternative: the same title and glyph on one
    /// line that may truncate, never an icon-only control (C81).
    var compact = false
    @State private var isPresented = false

    var body: some View {
        Button {
            isPresented.toggle()
        } label: {
            Label(snapshot.title, systemImage: snapshot.tone.symbol)
                .foregroundStyle(snapshot.tone.color)
                .workFont(.caption)
                .lineLimit(compact ? 1 : nil)
        }
        .buttonStyle(NativeSetupActionStyle())
        .help("Inspect recorder connection, client capture, and coverage")
        .accessibilityIdentifier("recording-health")
        .accessibilityLabel("Recording health: \(snapshot.title)")
        .popover(isPresented: $isPresented, arrowEdge: .bottom) {
            RecordingHealthPopover(
                snapshot: snapshot,
                // Dismiss the popover before starting: unlike every other control
                // here the restart fires directly (not via onAction), so without
                // this it would linger over the setup pane on a failed start.
                restart: restart.map { control in
                    RecorderRestartControl(inFlight: control.inFlight, onRestart: {
                        isPresented = false
                        control.onRestart()
                    })
                },
                lastKnownCauses: coordinator?.notices.filter {
                    !$0.isRecovered && !snapshot.causes.contains($0.cause)
                }.map(\.cause) ?? [],
                recoveries: coordinator?.recentRecoveries ?? [],
                onAction: { action in
                    if action != .refresh { isPresented = false }
                    perform(action)
                },
                onCauseAction: { cause in
                    if cause.action != .refresh { isPresented = false }
                    perform(cause.action, cause: cause)
                },
                onActivateClient: onActivateClient.map { callback in
                    { id in isPresented = false; callback(id) }
                }
            )
            // The popover lays out its own cards on the canvas ground.
            .popoverSurface(Theme.canvas)
        }
    }

    private func perform(_ action: RecordingHealthAction, cause: RecordingHealthCause? = nil) {
        switch action {
        case .setup:
            if let cause, let onSetupCause { onSetupCause(cause) }
            else { onSetup() }
        case .sources: onSources()
        case .refresh: onRefresh()
        }
    }
}

struct RecordingHealthPopover: View {
    let snapshot: RecordingHealthSnapshot
    var restart: RecorderRestartControl? = nil
    var lastKnownCauses: [RecordingHealthCause] = []
    var recoveries: [RecordingHealthNotice] = []
    let onAction: (RecordingHealthAction) -> Void
    var onCauseAction: ((RecordingHealthCause) -> Void)? = nil
    var onActivateClient: ((String) -> Void)? = nil
    var layout = NativeSetupLayout()

    var body: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            Label("Recording health", systemImage: snapshot.tone.symbol)
                .workFont(.titleCard)
                .foregroundStyle(Theme.ink)
                .accessibilityAddTraits(.isHeader)
                .fixedSize(horizontal: false, vertical: true)
            ScrollView {
                VStack(alignment: .leading, spacing: Space.l) {
                    if !snapshot.causes.isEmpty {
                        Divider()
                        Text("Needs attention").workFont(.rowLabel).accessibilityAddTraits(.isHeader)
                        ForEach(snapshot.causes) { cause in
                            VStack(alignment: .leading, spacing: 6) {
                                Label(cause.title, systemImage: cause.tone.symbol)
                                    .workFont(.rowLabel).foregroundStyle(cause.tone.color)
                                Text(cause.detail).workFont(.caption).foregroundStyle(Theme.muted)
                                if !cause.affectedSources.isEmpty {
                                    Text("Affected summaries: \(cause.affectedSources.joined(separator: ", "))")
                                        .workFont(.caption).foregroundStyle(Theme.muted)
                                }
                                if let restart, cause.isRecorderUnreachable {
                                    RecorderRestartButton(control: restart, identifier: "recording-health.restart.\(cause.id)")
                                } else {
                                    Button(cause.action.title) { perform(cause) }
                                        .buttonStyle(NativeSetupActionStyle())
                                        .accessibilityIdentifier("recording-health.action.\(cause.id)")
                                }
                            }
                        }
                    }
                    ForEach(snapshot.dimensions) { dimension in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(dimension.title).workFont(.caption).foregroundStyle(Theme.muted)
                            Label(dimension.value, systemImage: dimension.tone.symbol)
                                .workFont(.rowLabel).foregroundStyle(dimension.tone.color)
                            Text(dimension.detail).workFont(.caption).foregroundStyle(Theme.muted)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .accessibilityElement(children: .combine)
                    }
                    if !snapshot.clients.isEmpty {
                        Divider()
                        ForEach(snapshot.clients) { client in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(client.title).workFont(size: 14, weight: .semibold, relativeTo: .body, monospaced: true)
                                Label(client.status, systemImage: client.confirmed ? "checkmark.circle" : "clock")
                                    .workFont(.caption)
                                    .foregroundStyle(client.confirmed ? Theme.green : Theme.muted)
                                if let observation = client.lastObservation {
                                    Text("Last observed record: \(observation.observedAt.formatted(date: .abbreviated, time: .shortened))")
                                        .workFont(.caption).foregroundStyle(Theme.muted)
                                }
                                DisclosureGroup("Capture evidence") {
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text("Client: \(client.id)")
                                        Text("Required after: \(client.requiredAfter?.formatted(date: .abbreviated, time: .standard) ?? "no boundary recorded")")
                                        if let observation = client.lastObservation {
                                            Text("Event: \(observation.eventID)")
                                            Text("Observed: \(observation.observedAt.formatted(date: .abbreviated, time: .standard))")
                                        } else {
                                            Text("No capture event recorded for this client.")
                                        }
                                    }
                                    .fixedSize(horizontal: false, vertical: true)
                                    .textSelection(.enabled).padding(.top, 4)
                                }
                                .workFont(.caption)
                                .accessibilityIdentifier("recording-health.capture-evidence.\(client.id)")
                                if let onActivateClient {
                                    Button(client.confirmed ? "Review client connection" : "Resume activation") { onActivateClient(client.id) }
                                        .buttonStyle(NativeSetupActionStyle())
                                        .accessibilityIdentifier("recording-health.activate.\(client.id)")
                                }
                            }
                            .accessibilityElement(children: .contain)
                        }
                    }
                    if !recoveries.isEmpty {
                        Divider()
                        DisclosureGroup("Recent updates") {
                            ForEach(recoveries) { recovery in
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(recovery.title).workFont(.caption)
                                    if let recoveredAt = recovery.recoveredAt {
                                        Text("Recovered: \(recoveredAt.formatted(date: .abbreviated, time: .standard))")
                                            .workFont(.caption).foregroundStyle(Theme.muted)
                                    }
                                    Text(recovery.detail).workFont(.caption).foregroundStyle(Theme.muted)
                                    Text("First observed: \(recovery.observedAt.formatted(date: .abbreviated, time: .standard))")
                                        .workFont(.caption).foregroundStyle(Theme.muted)
                                }
                            }
                        }.workFont(.caption)
                    }
                    if !lastKnownCauses.isEmpty {
                        Divider()
                        Text("Last reported issues").workFont(.rowLabel).accessibilityAddTraits(.isHeader)
                        Text("Current status is unconfirmed. Dismissed notices remain here until the relevant health check observes recovery.")
                            .workFont(.caption).foregroundStyle(Theme.muted)
                        ForEach(lastKnownCauses) { cause in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(cause.title).workFont(.rowLabel)
                                Text(cause.detail).workFont(.caption).foregroundStyle(Theme.muted)
                                Button(cause.action.title) { perform(cause) }
                                    .buttonStyle(NativeSetupActionStyle())
                                    .accessibilityIdentifier("recording-health.action.\(cause.id)")
                            }
                        }
                    }
                }
                .textSelection(.enabled)
            }
            .frame(maxHeight: .infinity)
            layout.row(spacing: Space.m) {
                Button("Connections") { onAction(.setup) }
                    .buttonStyle(NativeSetupActionStyle())
                    .accessibilityIdentifier("recording-health.connections")
                Button("Diagnostics") { onAction(.sources) }.buttonStyle(NativeSetupActionStyle())
                if !layout.stacksControls { Spacer() }
                Button("Check again") { onAction(.refresh) }.buttonStyle(NativeSetupActionStyle())
            }
            .buttonStyle(NativeSetupActionStyle())
        }
        .workFont(.body)
        .padding(Space.l)
        .frame(width: layout.stacksControls ? 520 : 420, height: 560)
        .background(Theme.canvas)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("recording-health-popover")
    }

    private func perform(_ cause: RecordingHealthCause) {
        if let onCauseAction { onCauseAction(cause) }
        else { onAction(cause.action) }
    }
}

/// A stable in-window surface. It never opens itself, moves keyboard focus or
/// grants recording health simply because the user dismisses a notice.
struct RecordingHealthNoticeStack: View {
    let coordinator: RecordingHealthCoordinator
    var restart: RecorderRestartControl? = nil
    let onSetup: () -> Void
    var onSetupCause: ((RecordingHealthCause) -> Void)? = nil
    let onSources: () -> Void
    let onRefresh: () -> Void
    @State private var showsAll = false

    var body: some View {
        let visible = coordinator.visibleNotices
        if !visible.isEmpty {
            VStack(alignment: .leading, spacing: Space.s) {
                if showsAll && visible.count > 1 {
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: Space.s) {
                            ForEach(visible) { notice in noticeRow(notice) }
                        }
                    }
                    .frame(maxHeight: 300)
                } else if let notice = visible.first {
                    noticeRow(notice)
                }
                if visible.count > 1 {
                    SnapshotSafeBorderlessButton {
                        showsAll.toggle()
                    } label: {
                        Text(showsAll ? "Show fewer notices" : "Show \(visible.count - 1) more recording \(visible.count == 2 ? "notice" : "notices")")
                    }
                    .workFont(.caption)
                }
            }
            // Full page width: the stack is a banner in the page flow now,
            // not a floating 400pt card pinned to the window's corner (K09).
            .frame(maxWidth: .infinity, alignment: .leading)
            .fixedSize(horizontal: false, vertical: !showsAll)
            .workFont(.body)
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("recording-health-notices")
        }
    }

    private func noticeRow(_ notice: RecordingHealthNotice) -> some View {
        HStack(alignment: .top, spacing: Space.m) {
            Image(systemName: notice.isRecovered ? "arrow.clockwise.circle" : notice.cause.tone.symbol)
                .foregroundStyle(notice.isRecovered ? Theme.accent : notice.cause.tone.color)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text(notice.title).workFont(.rowLabel).foregroundStyle(Theme.ink)
                Text(notice.detail).workFont(.caption).foregroundStyle(Theme.muted)
                if let restart, notice.cause.isRecorderUnreachable, !notice.isRecovered {
                    // The notice stack has no footer (the popover does), so keep a
                    // direct route to setup/Connections alongside the one-click
                    // restart rather than only reaching it after a failed start.
                    HStack(spacing: Space.s) {
                        RecorderRestartButton(control: restart, identifier: "recording-health.notice-restart.\(notice.cause.id)")
                        Button(notice.cause.action.title) { perform(notice.cause.action, cause: notice.actionableCause) }
                            .buttonStyle(NativeSetupActionStyle())
                            .accessibilityIdentifier("recording-health.notice-action.\(notice.cause.id)")
                    }
                } else {
                    Button(notice.cause.action.title) { perform(notice.cause.action, cause: notice.actionableCause) }
                        .buttonStyle(NativeSetupActionStyle())
                        .accessibilityIdentifier("recording-health.notice-action.\(notice.cause.id)")
                }
            }
            Spacer(minLength: 0)
            SnapshotSafeBorderlessButton {
                coordinator.dismiss(notice.id)
            } label: {
                Image(systemName: "xmark")
            }
            .help("Dismiss this notice; recording status remains available")
            .accessibilityLabel("Dismiss notice: \(notice.title)")
        }
        .padding(Space.m)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Theme.cardLine))
    }

    private func perform(_ action: RecordingHealthAction, cause: RecordingHealthCause? = nil) {
        switch action {
        case .setup:
            if let cause, let onSetupCause { onSetupCause(cause) }
            else { onSetup() }
        case .sources: onSources()
        case .refresh: onRefresh()
        }
    }
}
