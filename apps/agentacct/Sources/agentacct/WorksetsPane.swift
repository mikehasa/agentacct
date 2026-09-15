import SwiftUI

// The Work surface — folder-anchored groupings the user defines themselves.
//
// A workset says "the sessions under this folder are one piece of work" and
// gathers them LIVE across Claude Code and Codex onto one shared timeline, so a
// project's runs stop reading as two separate orderings. It is an overlay, never
// a re-grading: each session keeps its own receipt and evidence tier, and every
// aggregate shown here is a labeled SUM of independently-attributed parts. The
// grouping is the user's assertion (a curation act), so nothing here is dressed
// up as machine verification.

struct WorksetsPane: View {
    @Environment(DashboardStore.self) private var dashboard
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    @State private var isCreating = false
    @State private var newName = ""
    @State private var selectedCandidate: String?
    @State private var writeError: String?
    @State private var isSubmitting = false
    // Minted once when the form opens and reused across retries, so a create
    // whose response was lost replays idempotently instead of duplicating.
    @State private var pendingWorksetId = ""

    private var stacksRows: Bool { dynamicTypeSize.isAccessibilitySize }

    var body: some View {
        ScrollBox {
            VStack(alignment: .leading, spacing: 0) {
                header
                content.padding(.top, Space.xl)
            }
            .padding(Space.gutter)
            .frame(maxWidth: 1172 + Space.gutter * 2, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .workFont(.body)
        .task {
            guard !SnapshotMode.enabled else { return }
            await dashboard.fetchWorksets()
            await dashboard.fetchWorksetCandidates()
        }
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: Space.l) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Work")
                    .workFont(.titlePage).tracking(Type.titlePageTracking)
                    .foregroundStyle(Theme.ink)
                Text("Group a folder's sessions across every agent you run")
                    .workFont(.dataSmall).foregroundStyle(Theme.muted)
            }
            Spacer(minLength: Space.m)
            if !dashboard.worksets.isEmpty || isCreating {
                Button {
                    beginCreating()
                } label: {
                    Label("New work group", systemImage: "plus")
                }
                .buttonStyle(NativeSetupActionStyle())
                .disabled(dashboard.isOfflineSnapshot || isCreating)
                .accessibilityIdentifier("worksets.new")
            }
        }
    }

    @ViewBuilder
    private var content: some View {
        if dashboard.isOfflineSnapshot {
            offlineNotice
        } else if let error = dashboard.worksetsError, dashboard.worksets.isEmpty {
            unavailableNotice(error)
        } else {
            if isCreating {
                WorksetCreateForm(
                    candidates: dashboard.worksetCandidates,
                    candidatesError: dashboard.worksetCandidatesError,
                    name: $newName,
                    selected: $selectedCandidate,
                    isSubmitting: isSubmitting,
                    writeError: writeError,
                    onCreate: submitCreate,
                    onCancel: cancelCreating
                )
                .padding(.bottom, Space.xl)
            }
            if dashboard.worksets.isEmpty && dashboard.isLoadingWorksets && !isCreating {
                loadingState
            } else if dashboard.worksets.isEmpty && !isCreating {
                emptyState
            } else {
                VStack(alignment: .leading, spacing: Space.xl) {
                    ForEach(dashboard.worksets) { workset in
                        WorksetCardView(workset: workset)
                    }
                }
            }
        }
    }

    private var loadingState: some View {
        HStack(spacing: Space.s) {
            ProgressView().controlSize(.small)
            Text("Loading your work groups…").workFont(.body).foregroundStyle(Theme.muted)
        }
        .padding(Space.cardPad)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Theme.cardLine, lineWidth: Metrics.borderW))
    }

    // MARK: states

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            Text("Point Work at a folder")
                .workFont(.titleCard).foregroundStyle(Theme.ink)
            Text("Pick a project folder and agentacct gathers every session that ran there — across all your agents — onto one timeline. It never changes a session's own receipt; the group is your view of the work.")
                .workFont(.body).foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
            Button {
                beginCreating()
            } label: {
                Label("New work group", systemImage: "plus")
            }
            .buttonStyle(NativeSetupActionStyle())
            .disabled(dashboard.isOfflineSnapshot)
            .accessibilityIdentifier("worksets.new-empty")
        }
        .padding(Space.cardPad)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(
            RoundedRectangle(cornerRadius: Metrics.radius)
                .strokeBorder(Theme.cardLine, lineWidth: Metrics.borderW)
        )
    }

    private var offlineNotice: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Work groups need the recorder").workFont(.rowLabel).foregroundStyle(Theme.ink)
            Text("This is a saved, read-only view. Reconnect the recorder to create or change a work group.")
                .workFont(.caption).foregroundStyle(Theme.muted)
        }
        .padding(Space.cardPad)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Theme.cardLine, lineWidth: Metrics.borderW))
    }

    private func unavailableNotice(_ error: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Work groups unavailable").workFont(.rowLabel).foregroundStyle(Theme.ink)
            Text(error).workFont(.caption).foregroundStyle(Theme.muted)
        }
        .padding(Space.cardPad)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Theme.cardLine, lineWidth: Metrics.borderW))
    }

    // MARK: actions

    private func beginCreating() {
        writeError = nil
        newName = ""
        selectedCandidate = nil
        pendingWorksetId = DashboardStore.newWorksetId()
        isCreating = true
    }

    private func cancelCreating() {
        isCreating = false
        writeError = nil
    }

    private func submitCreate() {
        guard let directory = selectedCandidate else {
            writeError = "Pick a folder first."
            return
        }
        let trimmed = newName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            writeError = "Give this work group a name."
            return
        }
        if pendingWorksetId.isEmpty { pendingWorksetId = DashboardStore.newWorksetId() }
        let worksetId = pendingWorksetId
        isSubmitting = true
        writeError = nil
        Task {
            do {
                // Reuses the same id on a retry, so a lost response replays
                // idempotently rather than forking a second group.
                try await dashboard.createWorkset(name: trimmed, directory: directory, worksetId: worksetId)
                isSubmitting = false
                isCreating = false
            } catch {
                isSubmitting = false
                writeError = "Couldn't create the group: \(error.localizedDescription)"
            }
        }
    }
}

// MARK: - Create form (inline; the app avoids sheets)

private struct WorksetCreateForm: View {
    let candidates: [WorksetCandidate]
    let candidatesError: String?
    @Binding var name: String
    @Binding var selected: String?
    let isSubmitting: Bool
    let writeError: String?
    let onCreate: () -> Void
    let onCancel: () -> Void

    /// Only folders that don't already have a group — one group per folder.
    private var available: [WorksetCandidate] { candidates.filter { !$0.alreadyGrouped } }
    private var groupedCount: Int { candidates.count - available.count }

    private var folderNote: String {
        var parts: [String] = ["\(available.count) folder\(available.count == 1 ? "" : "s")"]
        if available.count > 4 { parts[0] += " · scroll for more" }
        if groupedCount > 0 { parts.append("\(groupedCount) already grouped") }
        return parts.joined(separator: " · ")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.l) {
            Text("New work group")
                .workFont(.titleCard).foregroundStyle(Theme.ink)

            VStack(alignment: .leading, spacing: 6) {
                Text("Folder").workFont(.labelCaps).tracking(Type.labelCapsTracking)
                    .foregroundStyle(Theme.muted)
                if let candidatesError {
                    Text(candidatesError).workFont(.caption).foregroundStyle(Theme.muted)
                } else if candidates.isEmpty {
                    Text("No folders recorded yet. Run a session in a project, then come back.")
                        .workFont(.caption).foregroundStyle(Theme.muted)
                } else if available.isEmpty {
                    Text("Every folder agentacct has seen already has a work group.")
                        .workFont(.caption).foregroundStyle(Theme.muted)
                } else {
                    ScrollView {
                        VStack(spacing: 0) {
                            ForEach(available) { candidate in
                                candidateRow(candidate)
                                if candidate.id != available.last?.id {
                                    Rectangle().fill(Theme.hairline).frame(height: 1)
                                }
                            }
                        }
                    }
                    // Snug to the row count, but capped so a long folder list
                    // scrolls inside its box instead of pushing the form open.
                    .frame(height: min(CGFloat(available.count) * 54 + 2, 260))
                    .background(Theme.chrome, in: RoundedRectangle(cornerRadius: Metrics.radius))
                    .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Theme.hairline, lineWidth: Metrics.borderW))
                    Text(folderNote).workFont(.caption).foregroundStyle(Theme.muted)
                }
            }

            VStack(alignment: .leading, spacing: 6) {
                Text("Name").workFont(.labelCaps).tracking(Type.labelCapsTracking)
                    .foregroundStyle(Theme.muted)
                TextField("tryairis.ai", text: $name)
                    .textFieldStyle(.plain)
                    .workFont(.body)
                    .padding(.horizontal, Space.m)
                    .frame(height: Metrics.buttonH)
                    .background(Theme.chrome, in: RoundedRectangle(cornerRadius: Metrics.radius))
                    .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Theme.hairline, lineWidth: Metrics.borderW))
                    .accessibilityIdentifier("worksets.name-field")
            }

            if let writeError {
                Text(writeError).workFont(.caption).foregroundStyle(Theme.coral)
            }

            HStack(spacing: Space.s) {
                Button("Create work group", action: onCreate)
                    .buttonStyle(NativeSetupActionStyle())
                    .disabled(isSubmitting)
                    .accessibilityIdentifier("worksets.create-submit")
                Button("Cancel", action: onCancel)
                    .buttonStyle(QuietButtonStyle(horizontalPadding: 10))
                    .disabled(isSubmitting)
            }
        }
        .padding(Space.cardPad)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Theme.cardLine, lineWidth: Metrics.borderW))
    }

    private func candidateRow(_ candidate: WorksetCandidate) -> some View {
        Button {
            selected = candidate.projectIdentity
            if name.trimmingCharacters(in: .whitespaces).isEmpty { name = candidate.label }
        } label: {
            HStack(spacing: Space.s) {
                Image(systemName: selected == candidate.projectIdentity ? "largecircle.fill.circle" : "circle")
                    .foregroundStyle(selected == candidate.projectIdentity ? Theme.accent : Theme.muted)
                VStack(alignment: .leading, spacing: 1) {
                    Text(candidate.label).workFont(.rowLabel).foregroundStyle(Theme.ink)
                    Text(candidateSubtitle(candidate)).workFont(.caption).foregroundStyle(Theme.muted)
                }
                Spacer()
            }
            .padding(.horizontal, Space.m)
            .frame(minHeight: Metrics.rowTable, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(QuietButtonStyle(horizontalPadding: 0, verticalPadding: 0))
        .accessibilityIdentifier("worksets.candidate.\(candidate.label)")
    }

    private func candidateSubtitle(_ candidate: WorksetCandidate) -> String {
        let sessions = "\(candidate.sessionCount) session\(candidate.sessionCount == 1 ? "" : "s")"
        let sources = candidate.sources.map(WorksetFormat.sourceLabel).joined(separator: " · ")
        return sources.isEmpty ? sessions : "\(sessions) · \(sources)"
    }
}

// MARK: - One workset card

private struct WorksetCardView: View {
    let workset: WorksetCard
    @Environment(DashboardStore.self) private var dashboard
    @State private var isRenaming = false
    @State private var renameText = ""
    @State private var confirmingDelete = false
    @State private var actionError: String?

    var body: some View {
        VStack(alignment: .leading, spacing: Space.l) {
            headerRow
            if confirmingDelete {
                deleteConfirm
            }
            summaryRow
            WorksetTimelineStrip(
                lanes: workset.sessions,
                sources: workset.summary.sources,
                sessionsTotal: workset.sessionsTotal ?? workset.summary.sessionCount,
                truncated: workset.sessionsTruncated ?? false
            )
            if (workset.summary.unpricedSessions ?? 0) > 0 {
                footnote("Some sessions here carry no imported cost, so the total above is a partial sum.")
            }
            footnote("Grouped because you pointed this at a folder. Each session keeps its own receipt and evidence; the total is a sum of \(workset.summary.sessionCount) session\(workset.summary.sessionCount == 1 ? "" : "s"), not a combined verdict.")
            if let actionError {
                Text(actionError).workFont(.caption).foregroundStyle(Theme.coral)
            }
        }
        .padding(Space.cardPad)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Theme.cardLine, lineWidth: Metrics.borderW))
    }

    private var headerRow: some View {
        HStack(alignment: .center, spacing: Space.s) {
            Image(systemName: "folder").foregroundStyle(Theme.muted)
            if isRenaming {
                TextField(workset.name, text: $renameText)
                    .textFieldStyle(.plain)
                    .workFont(.titleCard)
                    .frame(maxWidth: 320)
                    .accessibilityIdentifier("worksets.rename-field")
                Button("Save") { commitRename() }
                    .buttonStyle(QuietButtonStyle(horizontalPadding: 8)).foregroundStyle(Theme.accent)
                Button("Cancel") { isRenaming = false }
                    .buttonStyle(QuietButtonStyle(horizontalPadding: 8)).foregroundStyle(Theme.muted)
            } else {
                Text(workset.name).workFont(.titleCard).foregroundStyle(Theme.ink)
                WorksetChip(text: "grouped by folder")
                Spacer()
                if !dashboard.isOfflineSnapshot {
                    Button { beginRename() } label: { Text("Rename") }
                        .buttonStyle(QuietButtonStyle(horizontalPadding: 8))
                        .foregroundStyle(Theme.muted)
                        .accessibilityIdentifier("worksets.rename")
                    Button { confirmingDelete = true } label: { Text("Delete") }
                        .buttonStyle(QuietButtonStyle(horizontalPadding: 8))
                        .foregroundStyle(Theme.muted)
                        .accessibilityIdentifier("worksets.delete")
                }
            }
        }
    }

    private var deleteConfirm: some View {
        HStack(spacing: Space.s) {
            Text("Delete this group?").workFont(.caption).foregroundStyle(Theme.ink)
            Button("Delete") { commitDelete() }
                .buttonStyle(QuietButtonStyle(horizontalPadding: 8)).foregroundStyle(Theme.coral)
            Button("Keep") { confirmingDelete = false }
                .buttonStyle(QuietButtonStyle(horizontalPadding: 8)).foregroundStyle(Theme.muted)
        }
        .padding(Space.s)
        .background(Theme.chrome, in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Theme.hairline, lineWidth: Metrics.borderW))
    }

    private var summaryRow: some View {
        HStack(alignment: .top, spacing: Space.xl) {
            metric(label: "sessions", value: "\(workset.summary.sessionCount)")
            metric(label: "sources", value: "\(workset.summary.sources.count)")
            metric(label: "span", value: WorksetFormat.span(from: workset.summary.firstActivityAt, to: workset.summary.lastActivityAt))
            if let cost = worksetCostLabel(workset.summary) {
                metric(label: costLabel, value: cost)
            }
            Spacer()
        }
    }

    private var costLabel: String {
        (workset.summary.costComplete == true) ? "cost, sum of receipts" : "cost, partial sum"
    }

    private func metric(label: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).workFont(.caption).foregroundStyle(Theme.muted)
            Text(value).workFont(.kpi).foregroundStyle(Theme.ink)
        }
    }

    private func footnote(_ text: String) -> some View {
        HStack(alignment: .top, spacing: 6) {
            Image(systemName: "info.circle").workFont(.caption).foregroundStyle(Theme.muted)
            Text(text).workFont(.caption).foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func beginRename() {
        renameText = workset.name
        isRenaming = true
    }

    private func commitRename() {
        let trimmed = renameText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed != workset.name else { isRenaming = false; return }
        actionError = nil
        Task {
            do {
                try await dashboard.renameWorkset(id: workset.worksetId, name: trimmed, expectedRevision: workset.revision)
                isRenaming = false
            } catch {
                actionError = "Rename failed: \(error.localizedDescription)"
            }
        }
    }

    private func commitDelete() {
        actionError = nil
        Task {
            do {
                try await dashboard.deleteWorkset(id: workset.worksetId, expectedRevision: workset.revision)
                confirmingDelete = false
            } catch {
                actionError = "Delete failed: \(error.localizedDescription)"
                confirmingDelete = false
            }
        }
    }
}

// MARK: - The shared-axis timeline (bars = sessions, colored by source)

private struct WorksetTimelineStrip: View {
    let lanes: [WorksetLane]
    /// From the summary (ALL members), so the legend covers every source.
    let sources: [WorksetSource]
    let sessionsTotal: Int
    let truncated: Bool

    @Environment(AppSelection.self) private var appSelection

    // The axis is the shown sessions' own first→last, so the timeline ends at
    // the last session — no empty tail.
    private var layout: WorksetTimelineLayout { WorksetTimelineLayout(lanes: lanes) }

    private static let rowHeight: CGFloat = 24
    private static let visibleRows = 8
    private static let labelWidth: CGFloat = 176

    var body: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            legend
            timelineRows
            axisLabels
            notes
        }
    }

    private var legend: some View {
        HStack(spacing: Space.l) {
            ForEach(sources) { source in
                HStack(spacing: 6) {
                    RoundedRectangle(cornerRadius: 2).fill(Theme.sourceColor(source.client)).frame(width: 10, height: 10)
                    Text(WorksetFormat.sourceLabel(source.client)).workFont(.caption).foregroundStyle(Theme.muted)
                }
            }
        }
    }

    @ViewBuilder
    private var timelineRows: some View {
        let rows = VStack(spacing: 4) {
            ForEach(layout.bars) { bar in laneRow(bar) }
        }
        // Collapsed to a handful of rows by default; a big group scrolls in
        // place rather than stretching the card open.
        if layout.bars.count > Self.visibleRows {
            ScrollView { rows }.frame(height: CGFloat(Self.visibleRows) * Self.rowHeight)
        } else {
            rows
        }
    }

    private func laneRow(_ bar: WorksetTimelineLayout.Bar) -> some View {
        Button {
            if let key = bar.lane.sessionKey, !key.isEmpty { appSelection.open(.session(key)) }
        } label: {
            HStack(spacing: Space.m) {
                HStack(spacing: 6) {
                    Circle().fill(Self.pipColor(bar.lane.status)).frame(width: 6, height: 6)
                    Text(bar.lane.displayTitle)
                        .workFont(.caption).foregroundStyle(Theme.ink)
                        .lineLimit(1).truncationMode(.tail)
                }
                .frame(width: Self.labelWidth, alignment: .leading)
                GeometryReader { geo in
                    let width = geo.size.width
                    ZStack(alignment: .leading) {
                        RoundedRectangle(cornerRadius: 2).fill(Theme.hairline).frame(height: 2)
                            .frame(maxWidth: .infinity, alignment: .center)
                        RoundedRectangle(cornerRadius: 3)
                            .fill(bar.timeUnknown ? Theme.muted : Theme.sourceColor(bar.lane.client))
                            .frame(width: max(6, width * bar.widthFraction), height: 12)
                            .offset(x: width * bar.leftFraction)
                            .opacity(bar.timeUnknown ? 0.5 : 1)
                    }
                    .frame(height: 16)
                }
                .frame(height: 16)
            }
            .frame(height: 18)
            .contentShape(Rectangle())
        }
        .buttonStyle(QuietButtonStyle(horizontalPadding: 4, verticalPadding: 1))
        .help(tooltip(bar.lane))
        .accessibilityLabel(tooltip(bar.lane))
    }

    private static func pipColor(_ status: String?) -> Color {
        switch status {
        case "blocked": return Theme.coral
        case "active", "handed_off": return Theme.accent
        case "completed": return Theme.ink
        default: return Theme.muted
        }
    }

    private func tooltip(_ lane: WorksetLane) -> String {
        var lines: [String] = [lane.displayTitle]
        var meta = WorksetFormat.sourceLabel(lane.client ?? "")
        if let status = lane.status, !status.isEmpty { meta += " · \(status)" }
        lines.append(meta)
        var facts: [String] = []
        if let dur = WorksetFormat.duration(lane.durationSeconds) { facts.append(dur) }
        if let cost = lane.estimatedCostUsd { facts.append(Fmt.dollars(cost, prefix: "≈$")) }
        if let tokens = lane.totalTokens, tokens > 0 { facts.append("\(tokens) tokens") }
        if !facts.isEmpty { lines.append(facts.joined(separator: " · ")) }
        var work: [String] = []
        if let calls = lane.toolCalls, calls > 0 { work.append("\(calls) tool calls") }
        if let steps = lane.steps, steps > 0 { work.append("\(steps) steps") }
        if let checks = lane.checks, checks > 0 {
            let failed = lane.checksFailed ?? 0
            work.append(failed > 0 ? "\(checks) checks (\(failed) failed)" : "\(checks) checks")
        }
        if !work.isEmpty { lines.append(work.joined(separator: " · ")) }
        return lines.joined(separator: "\n")
    }

    @ViewBuilder
    private var axisLabels: some View {
        if let start = layout.windowStart, let end = layout.windowEnd {
            HStack {
                Text(WorksetFormat.axisDate(start)).workFont(.dataSmall).foregroundStyle(Theme.muted)
                Spacer()
                Text(WorksetFormat.axisDate(end)).workFont(.dataSmall).foregroundStyle(Theme.muted)
            }
            .padding(.leading, Self.labelWidth + Space.m + 4)
        }
    }

    @ViewBuilder
    private var notes: some View {
        let timeless = layout.timelessCount
        if truncated || timeless > 0 {
            VStack(alignment: .leading, spacing: 2) {
                if truncated {
                    Text("Showing \(lanes.count) of \(sessionsTotal) sessions.")
                        .workFont(.caption).foregroundStyle(Theme.muted)
                }
                if timeless > 0 {
                    Text("\(timeless) session\(timeless == 1 ? "" : "s") with no recorded time — shown faded at the start, not a real position.")
                        .workFont(.caption).foregroundStyle(Theme.muted)
                }
            }
            .padding(.top, 2)
        }
    }
}

// MARK: - Small pieces

private struct WorksetChip: View {
    let text: String
    var body: some View {
        Text(text)
            .workFont(.caption)
            .foregroundStyle(Theme.accent)
            .padding(.horizontal, Space.s)
            .padding(.vertical, 2)
            .background(Theme.tintAccent, in: Capsule())
    }
}

enum WorksetFormat {
    static func sourceLabel(_ client: String) -> String {
        switch client.lowercased() {
        case "claude-code", "claude", "claude code": return "Claude Code"
        case "codex", "openai-codex", "codex-cli": return "Codex"
        case "opencode", "open-code": return "OpenCode"
        case "hermes": return "Hermes"
        default: return client.isEmpty ? "unknown" : client
        }
    }

    /// A short human duration for a single session (its own begin→end span).
    static func duration(_ seconds: Double?) -> String? {
        guard let seconds, seconds.isFinite, seconds > 0 else { return nil }
        let hour = 3_600.0, minute = 60.0
        if seconds >= hour {
            let hours = seconds / hour
            return hours >= 10 ? "\(Int(hours.rounded()))h" : String(format: "%.1fh", hours)
        }
        if seconds >= minute {
            return "\(Int((seconds / minute).rounded()))m"
        }
        return "\(Int(seconds.rounded()))s"
    }

    private static let axisFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "MMM d"
        formatter.timeZone = TimeZone(identifier: "UTC")
        return formatter
    }()

    static func axisDate(_ epoch: Double) -> String {
        axisFormatter.string(from: Date(timeIntervalSince1970: epoch))
    }

    /// A plain, honest span from first to last activity; never a fabricated
    /// figure when the endpoints are missing.
    static func span(from first: Double?, to last: Double?) -> String {
        guard let first, let last, last >= first else { return "—" }
        let seconds = last - first
        let day = 86_400.0, hour = 3_600.0, minute = 60.0
        if seconds >= day {
            let days = Int((seconds / day).rounded())
            return "~\(max(1, days)) day\(days == 1 ? "" : "s")"
        }
        if seconds >= hour {
            let hours = Int((seconds / hour).rounded())
            return "~\(max(1, hours)) hr\(hours == 1 ? "" : "s")"
        }
        let minutes = Int((seconds / minute).rounded())
        return "~\(max(1, minutes)) min"
    }
}
