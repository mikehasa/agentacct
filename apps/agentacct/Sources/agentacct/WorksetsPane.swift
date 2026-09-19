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
    // The group whose detail (zoomable timeline) is open, if any.
    @State private var openWorksetId: String?

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
                // Prose takes the subtitle face, never the metric mono one —
                // the same correction the Diagnostics header carries.
                Text("Group a folder's sessions across every agent you run")
                    .workFont(FieldFont.subtitle).foregroundStyle(Theme.muted)
            }
            Spacer(minLength: Space.m)
            if openWorksetId == nil && (!dashboard.worksets.isEmpty || isCreating) {
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
        } else if let id = openWorksetId, let card = dashboard.worksets.first(where: { $0.worksetId == id }) {
            WorksetDetailView(workset: card, onBack: { openWorksetId = nil })
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
                        WorksetCardView(workset: workset, onOpen: { openWorksetId = workset.worksetId })
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
        openWorksetId = nil  // leave any open detail so the form is what shows
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
    var onOpen: () -> Void = {}
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
            WorksetSummaryRow(summary: workset.summary)
            WorksetTimeline(
                lanes: workset.sessions,
                sources: workset.summary.sources,
                sessionsTotal: workset.sessionsTotal ?? workset.summary.sessionCount,
                truncated: workset.sessionsTruncated ?? false
            )
            WorksetHonestyNote(summary: workset.summary)
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
                Button(action: onOpen) {
                    HStack(spacing: 4) {
                        Text(workset.name).workFont(.titleCard).foregroundStyle(Theme.ink)
                        Image(systemName: "chevron.right").workFont(.caption).foregroundStyle(Theme.muted)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(QuietButtonStyle(horizontalPadding: 4, verticalPadding: 2))
                .accessibilityIdentifier("worksets.open")
                .help("Open this work group")
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

private struct WorksetTimeline: View {
    let lanes: [WorksetLane]
    /// From the summary (ALL members), so the legend covers every source.
    let sources: [WorksetSource]
    let sessionsTotal: Int
    let truncated: Bool
    /// The detail page turns on pan (drag) and zoom (pinch / the ± buttons).
    var zoomable = false

    @Environment(AppSelection.self) private var appSelection
    @State private var hovered: WorksetLane?
    @State private var hoverPoint: CGPoint = .zero
    @State private var zoom: Double = 1
    @State private var panCenter: Double = 0.5
    // Frames of each session row inside the canvas, so the native input layer
    // lets clicks/hover through to the rows while it keeps scroll + drag.
    @State private var rowFrames: [CGRect] = []
    // panCenter captured when a scrubber drag begins, so the drag is relative.
    @State private var scrubStartCenter: Double?

    private static let rowUnit: CGFloat = 22
    private static let labelWidth: CGFloat = 176
    private static let hoverCardWidth: CGFloat = 240
    private static let scrubberHeight: CGFloat = 26
    // The detail canvas traps the scroll wheel for zoom, so it must fit the
    // viewport; bound it to this many rows and disclose the rest.
    private static let maxDetailRows = 16
    // The per-row button's horizontal inset (QuietButtonStyle horizontalPadding),
    // so the bar track the input math uses matches where the bars actually draw.
    private static let barInset: CGFloat = 4

    static func pipColor(_ status: String?) -> Color {
        switch status {
        case "blocked": return Theme.coral
        case "active", "handed_off": return Theme.accent
        case "completed": return Theme.ink
        default: return Theme.muted
        }
    }

    private var fullLo: Double? { lanes.compactMap { $0.firstActivityAt }.filter { $0 > 0 }.min() }
    private var fullHi: Double? {
        (lanes.compactMap { $0.firstActivityAt } + lanes.compactMap { $0.lastActivityAt })
            .filter { $0 > 0 }.max()
    }

    // Full range in the list; a zoom/pan sub-window in the detail.
    private var window: (start: Double, end: Double)? {
        guard let lo = fullLo, let hi = fullHi, hi > lo else { return nil }
        guard zoomable else { return (lo, hi) }
        let w = WorksetZoomWindow(lo: lo, hi: hi, zoom: zoom, panCenter: panCenter)
        return (w.start, w.end)
    }

    // When zoomed in, only sessions that overlap the visible window are drawn —
    // a session entirely outside it must not be clamped to the edge and shown
    // as if it were active inside the window. Timeless sessions have NO position,
    // so they're kept regardless of the window (shown faded at the start and
    // flagged by their own note) rather than reclassified as "outside".
    private var visibleLanes: [WorksetLane] {
        guard zoomable, let win = window, let lo = fullLo, let hi = fullHi,
              (win.end - win.start) < (hi - lo) - 0.0001 else { return lanes }
        return lanes.filter {
            WorksetZoomWindow.laneVisible(first: $0.firstActivityAt, last: $0.lastActivityAt, start: win.start, end: win.end)
        }
    }

    // Only TIMED sessions are ever culled by the window, so this counts what the
    // "outside this range" note honestly refers to (timeless lanes stay in view).
    private var hiddenByZoom: Int { max(0, lanes.count - visibleLanes.count) }

    private var layout: WorksetTimelineLayout {
        WorksetTimelineLayout(lanes: visibleLanes, windowStart: window?.start, windowEnd: window?.end)
    }

    // The list collapses to a handful and scrolls in place. The detail is a
    // native scroll-to-zoom canvas that traps the wheel, so it must fit the
    // viewport (a taller canvas would trap the page scroll): it's bounded to
    // maxDetailRows and discloses any sessions beyond the cap, which zooming
    // into a time range brings into view.
    private var visibleRows: Int { zoomable ? Self.maxDetailRows : 8 }
    private var scrollsInternally: Bool { !zoomable && layout.bars.count > visibleRows }

    // The bars actually drawn. In the bounded detail we keep every timeless bar
    // (its note points at it) plus the most recent timed bars up to the cap.
    private var renderedBars: [WorksetTimelineLayout.Bar] {
        guard zoomable, layout.bars.count > Self.maxDetailRows else { return layout.bars }
        let timeless = layout.bars.filter { $0.timeUnknown }
        let timed = layout.bars.filter { !$0.timeUnknown }
        let keepTimed = max(0, Self.maxDetailRows - timeless.count)
        return Array(timed.suffix(keepTimed)) + timeless
    }

    // Timed sessions inside the window but past the row cap (disjoint from
    // hiddenByZoom, which is sessions outside the window entirely).
    private var cappedOverflow: Int { max(0, layout.bars.count - renderedBars.count) }

    private var rowsHeight: CGFloat {
        let shown = scrollsInternally ? visibleRows : max(1, renderedBars.count)
        return CGFloat(shown) * Self.rowUnit
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            HStack(alignment: .center) {
                legend
                Spacer()
                if zoomable { zoomControls }
            }
            timelineArea
            if zoomable { scrubber }
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

    private var zoomControls: some View {
        HStack(spacing: 2) {
            Text(zoomCoverageLabel).workFont(.dataSmall).foregroundStyle(Theme.muted).padding(.trailing, 4)
            Button { setZoom(zoom / 1.6) } label: { Image(systemName: "minus.magnifyingglass") }
                .buttonStyle(QuietButtonStyle(horizontalPadding: 6)).disabled(zoom <= 1.001)
            Button { zoom = 1; panCenter = 0.5 } label: { Image(systemName: "arrow.counterclockwise") }
                .buttonStyle(QuietButtonStyle(horizontalPadding: 6)).disabled(zoom <= 1.001)
            Button { setZoom(zoom * 1.6) } label: { Image(systemName: "plus.magnifyingglass") }
                .buttonStyle(QuietButtonStyle(horizontalPadding: 6)).disabled(zoom >= WorksetZoomWindow.maxZoom - 0.001)
        }
    }

    private var zoomCoverageLabel: String {
        guard let lo = fullLo, let hi = fullHi, hi > lo, let win = window else { return "" }
        let pct = Int((WorksetZoomWindow(lo: lo, hi: hi, zoom: zoom, panCenter: panCenter)
            .coverage(lo: lo, hi: hi) * 100).rounded())
        _ = win
        return zoom <= 1.001 ? "full range" : "~\(max(1, pct))% of range"
    }

    private func setZoom(_ z: Double) {
        zoom = min(WorksetZoomWindow.maxZoom, max(1, z))
        clampPan()
    }

    // The window only actually moves while the center sits inside
    // [half, 1-half]; clamping panCenter to that avoids a dead zone where an
    // edge-ward drag (or a zoom change) produces no movement.
    private func panHalf() -> Double { 0.5 / max(1, zoom) }
    private func clampPanValue(_ value: Double) -> Double {
        let half = panHalf()
        return min(1 - half, max(half, value.isFinite ? value : 0.5))
    }
    private func clampPan() { panCenter = clampPanValue(panCenter) }

    // In the detail (zoomable), a native input layer catches scroll/pinch → zoom
    // (the same one the session detail uses), while the session rows stay
    // clickable/hoverable via `interactiveRegions`. Left/right panning is the
    // bottom scrubber's job; because the rows tile the whole canvas the native
    // drag-pan only fires on the rare blank area, so the scrubber is primary.
    // The list just shows the rows (scrolling in place when there are many).
    private var timelineArea: some View {
        GeometryReader { geo in
            let canvasW = geo.size.width
            let trackWidth = max(1, canvasW - Self.labelWidth - Space.m - Self.barInset * 2)
            Group {
                if zoomable {
                    WorkTimeCanvasInput(
                        interactiveRegions: rowFrames,
                        onPan: { pixels in panBy(pixels: pixels, trackWidth: trackWidth) },
                        onZoom: { factor, anchor in
                            applyScrollZoom(factor: factor,
                                            anchor: remapAnchor(anchor, canvasW: canvasW, trackWidth: trackWidth))
                        },
                        accessibilityValue: zoomCoverageLabel,
                        accessibilityIdentifier: "worksets.timeline.navigation"
                    ) {
                        rowsWithHover(canvasSize: geo.size)
                    }
                    .renderingSurface
                } else {
                    rowsWithHover(canvasSize: geo.size)
                }
            }
        }
        .frame(height: rowsHeight)
    }

    // Rows plus the hover card, sized to fill the canvas exactly so bar
    // positions and measured row frames share one top-left coordinate space.
    private func rowsWithHover(canvasSize: CGSize) -> some View {
        ZStack(alignment: .topLeading) {
            rowsContent
            if let hovered {
                WorksetHoverCard(lane: hovered)
                    .frame(width: Self.hoverCardWidth)
                    .offset(
                        x: min(max(8, hoverPoint.x + 14), max(8, canvasSize.width - Self.hoverCardWidth - 8)),
                        y: min(max(0, hoverPoint.y + 12), max(0, canvasSize.height - 176))
                    )
                    .allowsHitTesting(false)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .coordinateSpace(name: "wsTimeline")
        .contentShape(Rectangle())
        .onPreferenceChange(WorksetRowFrameKey.self) { rowFrames = $0 }
    }

    @ViewBuilder
    private var rowsContent: some View {
        let rows = VStack(spacing: 0) { ForEach(renderedBars) { bar in laneRow(bar) } }
        if scrollsInternally {
            ScrollView { rows }.frame(height: rowsHeight)
        } else {
            rows.frame(height: rowsHeight, alignment: .top)
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
            // A fixed row height so N rows measure exactly N × rowUnit — the axis
            // and notes below then sit clear of the rows instead of overlapping.
            .frame(height: Self.rowUnit)
            .contentShape(Rectangle())
        }
        .buttonStyle(QuietButtonStyle(horizontalPadding: 4, verticalPadding: 0))
        .background(
            GeometryReader { proxy in
                Color.clear.preference(key: WorksetRowFrameKey.self,
                                       value: [proxy.frame(in: .named("wsTimeline"))])
            }
        )
        .onContinuousHover(coordinateSpace: .named("wsTimeline")) { phase in
            switch phase {
            case .active(let point):
                hovered = bar.lane
                hoverPoint = point
            case .ended:
                if hovered?.id == bar.lane.id { hovered = nil }
            }
        }
        .accessibilityLabel(Self.accessibleLabel(bar.lane))
    }

    // Parity with the hover card so VoiceOver hears the same facts.
    static func accessibleLabel(_ lane: WorksetLane) -> String {
        var parts = [lane.displayTitle, WorksetFormat.sourceLabel(lane.client ?? "")]
        if let status = lane.status, !status.isEmpty { parts.append(status) }
        if let dur = WorksetFormat.duration(lane.durationSeconds) { parts.append(dur) }
        if let cost = WorksetFormat.laneCost(lane) { parts.append(cost) }
        if let tokens = lane.totalTokens, tokens > 0 { parts.append("\(tokens) tokens") }
        if let calls = lane.toolCalls, calls > 0 { parts.append(Fmt.count(calls, "tool call")) }
        if let steps = lane.steps, steps > 0 { parts.append(Fmt.count(steps, "step")) }
        if let checks = lane.checks, checks > 0 {
            let failed = lane.checksFailed ?? 0
            parts.append(failed > 0 ? "\(Fmt.count(checks, "check")), \(failed) failed" : Fmt.count(checks, "check"))
        }
        return parts.joined(separator: ", ")
    }

    // Native drag (blank canvas areas) pans by content-motion pixels: dragging
    // right reveals earlier time, so the visible window's center moves left.
    private func panBy(pixels: Double, trackWidth: CGFloat) {
        guard zoomable, trackWidth > 0, pixels.isFinite else { return }
        let visibleFraction = 1.0 / max(1.0, zoom)
        panCenter = clampPanValue(panCenter - pixels / Double(trackWidth) * visibleFraction)
    }

    // Scroll wheel / pinch: zoom around the point under the pointer so the time
    // there stays fixed, matching the session detail's feel.
    private func applyScrollZoom(factor: Double, anchor: Double) {
        guard let lo = fullLo, let hi = fullHi, hi > lo else { return }
        let result = WorksetZoomWindow.applyZoom(currentZoom: zoom, panCenter: panCenter,
                                                 factor: factor, anchor: anchor, lo: lo, hi: hi)
        zoom = result.zoom
        panCenter = clampPanValue(result.panCenter)
    }

    // The onZoom anchor is a fraction across the whole canvas (label + inset +
    // track); remap it to a fraction across just the bar track it zooms. The bar
    // track starts at labelWidth + Space.m + barInset (the button's own inset).
    private func remapAnchor(_ anchor: Double, canvasW: CGFloat, trackWidth: CGFloat) -> Double {
        guard trackWidth > 0 else { return 0.5 }
        let x = anchor * Double(canvasW) - Double(Self.labelWidth + Space.m + Self.barInset)
        return min(1.0, max(0.0, x / Double(trackWidth)))
    }

    // MARK: bottom scrubber (drag left/right to pan, like the session detail)

    @ViewBuilder
    private var scrubber: some View {
        if let lo = fullLo, let hi = fullHi, hi > lo {
            GeometryReader { geo in
                let w = geo.size.width
                let full = hi - lo
                let win = WorksetZoomWindow(lo: lo, hi: hi, zoom: zoom, panCenter: panCenter)
                let rawLeft = CGFloat((win.start - lo) / full) * w
                let rawWidth = CGFloat((win.end - win.start) / full) * w
                let windowWidth = min(w, max(10, rawWidth))
                let windowLeft = min(max(0, rawLeft), w - windowWidth)
                ZStack(alignment: .topLeading) {
                    RoundedRectangle(cornerRadius: 5).fill(Theme.chrome)
                        .overlay(RoundedRectangle(cornerRadius: 5).strokeBorder(Theme.hairline, lineWidth: Metrics.borderW))
                    ForEach(Array(scrubberTicks.enumerated()), id: \.offset) { _, frac in
                        // A positional tick is a data mark, so it takes the
                        // chart-neutral token rather than an alpha-derived
                        // semantic colour (ColorTokenLintTests).
                        Rectangle().fill(Theme.chartNeutral)
                            .frame(width: 1.5, height: 9)
                            .offset(x: CGFloat(frac) * (w - 1.5), y: (Self.scrubberHeight - 9) / 2)
                    }
                    RoundedRectangle(cornerRadius: 5)
                        // The named accent wash — the same token the Work
                        // timeline's window thumb uses — not an ad-hoc alpha.
                        .fill(Theme.tintAccent)
                        .overlay(RoundedRectangle(cornerRadius: 5).strokeBorder(Theme.accent, lineWidth: 1.5))
                        .frame(width: windowWidth, height: Self.scrubberHeight)
                        .offset(x: windowLeft)
                        .allowsHitTesting(false)
                }
                .frame(height: Self.scrubberHeight)
                .contentShape(Rectangle())
                .gesture(scrubberDrag(trackWidth: w))
                .accessibilityElement()
                .accessibilityLabel("Visible time window")
                .accessibilityValue(zoomCoverageLabel)
            }
            .frame(height: Self.scrubberHeight)
            .padding(.leading, Self.labelWidth + Space.m + Self.barInset)
            .padding(.trailing, Self.barInset)
        }
    }

    // A tick per session start (deduped) so the scrubber shows where the work
    // actually sits inside the full range, not just an empty rail.
    private var scrubberTicks: [Double] {
        guard let lo = fullLo, let hi = fullHi, hi > lo else { return [] }
        let full = hi - lo
        let fractions = lanes.compactMap { lane -> Double? in
            guard let t = lane.firstActivityAt, t > 0 else { return nil }
            return min(1.0, max(0.0, (t - lo) / full))
        }
        return Array(Set(fractions.map { ($0 * 200).rounded() / 200 })).sorted()
    }

    private func scrubberDrag(trackWidth: CGFloat) -> some Gesture {
        DragGesture(minimumDistance: 0)
            .onChanged { value in
                guard trackWidth > 0 else { return }
                if scrubStartCenter == nil { scrubStartCenter = panCenter }
                let delta = Double(value.translation.width / trackWidth)
                panCenter = clampPanValue((scrubStartCenter ?? panCenter) + delta)
            }
            .onEnded { _ in scrubStartCenter = nil }
    }

    @ViewBuilder
    private var axisLabels: some View {
        if let start = layout.windowStart, let end = layout.windowEnd {
            HStack {
                Text(WorksetFormat.axisLabel(start, span: end - start)).workFont(.dataSmall).foregroundStyle(Theme.muted)
                Spacer()
                Text(WorksetFormat.axisLabel(end, span: end - start)).workFont(.dataSmall).foregroundStyle(Theme.muted)
            }
            .padding(.leading, Self.labelWidth + Space.m + Self.barInset)
            .padding(.trailing, Self.barInset)
        }
    }

    @ViewBuilder
    private var notes: some View {
        let timeless = layout.timelessCount
        let hidden = hiddenByZoom
        let capped = cappedOverflow
        if truncated || timeless > 0 || hidden > 0 || capped > 0 {
            VStack(alignment: .leading, spacing: 2) {
                if capped > 0 {
                    Text("Showing \(renderedBars.count) of \(layout.bars.count) sessions in view — zoom into a time range to see the rest.")
                        .workFont(.caption).foregroundStyle(Theme.muted)
                }
                if hidden > 0 {
                    Text("\(hidden) session\(hidden == 1 ? "" : "s") outside this range — zoom out to see \(hidden == 1 ? "it" : "them").")
                        .workFont(.caption).foregroundStyle(Theme.muted)
                }
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

/// Collects each session row's frame (in the canvas's top-left space) so the
/// native input layer can pass clicks and hover through to the rows while it
/// keeps scroll-to-zoom and blank-area drag-to-pan.
private struct WorksetRowFrameKey: PreferenceKey {
    static var defaultValue: [CGRect] = []
    static func reduce(value: inout [CGRect], nextValue: () -> [CGRect]) {
        value.append(contentsOf: nextValue())
    }
}

// MARK: - Hover card (a dedicated info panel, not the native tooltip)

private struct WorksetHoverCard: View {
    let lane: WorksetLane

    // A compact two-column fact grid keeps the panel short so it doesn't
    // overflow a small card's timeline strip.
    private var facts: [(String, String)] {
        var out: [(String, String)] = []
        if let d = WorksetFormat.duration(lane.durationSeconds) { out.append(("Duration", d)) }
        if let cost = WorksetFormat.laneCost(lane) { out.append(("Cost", cost)) }
        if let tokens = lane.totalTokens, tokens > 0 { out.append(("Tokens", tokens.formatted())) }
        if let calls = lane.toolCalls, calls > 0 { out.append(("Tool calls", "\(calls)")) }
        if let steps = lane.steps, steps > 0 { out.append(("Steps", "\(steps)")) }
        if let checks = lane.checks, checks > 0 { out.append(("Checks", checksValue(checks))) }
        return out
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top, spacing: 6) {
                Circle().fill(WorksetTimeline.pipColor(lane.status)).frame(width: 7, height: 7).padding(.top, 4)
                Text(lane.displayTitle).workFont(.rowLabel).foregroundStyle(Theme.ink)
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 6) {
                Text(WorksetFormat.sourceLabel(lane.client ?? "")).workFont(.captionSemibold)
                    .foregroundStyle(Theme.sourceColor(lane.client))
                if let status = lane.status, !status.isEmpty {
                    Text("·").workFont(.caption).foregroundStyle(Theme.muted)
                    Text(status).workFont(.caption).foregroundStyle(Theme.muted)
                }
            }
            if !facts.isEmpty {
                Rectangle().fill(Theme.hairline).frame(height: 1)
                let columns = [GridItem(.flexible(), spacing: Space.m), GridItem(.flexible(), spacing: Space.m)]
                LazyVGrid(columns: columns, alignment: .leading, spacing: 4) {
                    ForEach(facts, id: \.0) { fact in
                        VStack(alignment: .leading, spacing: 0) {
                            Text(fact.0).workFont(.caption).foregroundStyle(Theme.muted)
                            Text(fact.1).workFont(.dataSmall).foregroundStyle(Theme.ink)
                        }
                    }
                }
            }
        }
        .padding(Space.m)
        .frame(width: 240, alignment: .leading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Theme.cardLine, lineWidth: Metrics.borderW))
    }

    private func checksValue(_ checks: Int) -> String {
        let failed = lane.checksFailed ?? 0
        return failed > 0 ? "\(checks) · \(failed) failed" : "\(checks)"
    }
}

// MARK: - Detail view (one group, session-level zoomable timeline)

private struct WorksetDetailView: View {
    let workset: WorksetCard
    let onBack: () -> Void

    private var sessionsTotal: Int { workset.sessionsTotal ?? workset.summary.sessionCount }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.l) {
            Button(action: onBack) {
                HStack(spacing: 4) {
                    Image(systemName: "chevron.left")
                    Text("All work groups")
                }
            }
            .buttonStyle(QuietButtonStyle(horizontalPadding: 4))
            .foregroundStyle(Theme.accent)
            .accessibilityIdentifier("worksets.back")

            HStack(spacing: Space.s) {
                Image(systemName: "folder").foregroundStyle(Theme.muted)
                Text(workset.name).workFont(.titleSection).foregroundStyle(Theme.ink)
                WorksetChip(text: "grouped by folder")
            }

            WorksetKPIRow(card: workset)
            OutcomeBar(
                title: "Sessions",
                total: "\(sessionsTotal) total",
                segments: WorksetOutcome.sessionSegments(workset.sessions)
            )

            // The sessions list leads the detail so the group is more than a
            // timeline: each row is one session's own honest state and drills in.
            WorksetDetailSectionHeader(
                title: "Sessions",
                trailing: "\(sessionsTotal) session\(sessionsTotal == 1 ? "" : "s") · \(workset.summary.sources.count) source\(workset.summary.sources.count == 1 ? "" : "s")"
            )
            WorksetSessionsList(
                lanes: workset.sessions,
                sessionsTotal: sessionsTotal
            )

            WorksetDetailSectionHeader(
                title: "Activity",
                trailing: WorksetFormat.span(from: workset.summary.firstActivityAt, to: workset.summary.lastActivityAt)
            )
            WorksetTimeline(
                lanes: workset.sessions,
                sources: workset.summary.sources,
                sessionsTotal: sessionsTotal,
                truncated: workset.sessionsTruncated ?? false,
                zoomable: true
            )
            Text("Scroll or pinch to zoom · drag the bar below to move left/right · click a session to open it")
                .workFont(.caption).foregroundStyle(Theme.muted)

            WorksetHonestyNote(summary: workset.summary)
        }
        .padding(Space.cardPad)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Theme.cardLine, lineWidth: Metrics.borderW))
    }
}

// MARK: - Detail section header (caps eyebrow + trailing count + hairline)

private struct WorksetDetailSectionHeader: View {
    let title: String
    var trailing: String? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                CapsLabel(text: title, tone: Theme.ink)
                Spacer(minLength: Space.s)
                if let trailing, !trailing.isEmpty {
                    Text(trailing).workFont(.dataSmall).foregroundStyle(Theme.muted)
                }
            }
            Rectangle().fill(Theme.hairline).frame(height: 1)
        }
        .padding(.top, Space.xs)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }
}

// MARK: - Sessions list (one honest, drillable row per member session)

private struct WorksetSessionsList: View {
    let lanes: [WorksetLane]
    let sessionsTotal: Int
    @Environment(AppSelection.self) private var appSelection

    private var hiddenCount: Int { max(0, sessionsTotal - lanes.count) }

    var body: some View {
        VStack(spacing: 0) {
            ForEach(Array(lanes.enumerated()), id: \.element.id) { index, lane in
                if index > 0 { Divider().overlay(Theme.hairline) }
                WorksetSessionRow(lane: lane) {
                    if let key = lane.sessionKey, !key.isEmpty { appSelection.open(.session(key)) }
                }
            }
            if hiddenCount > 0 {
                Divider().overlay(Theme.hairline)
                Text("\(hiddenCount) more session\(hiddenCount == 1 ? "" : "s") in this group — open a narrower time range to see them")
                    .workFont(.caption).foregroundStyle(Theme.muted)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, Space.s)
            }
        }
    }
}

private struct WorksetSessionRow: View {
    let lane: WorksetLane
    let onOpen: () -> Void

    private var isSubagent: Bool {
        guard let kind = lane.sessionKind else { return false }
        return kind != "root" && !kind.isEmpty
    }

    var body: some View {
        Button(action: onOpen) {
            HStack(alignment: .center, spacing: Space.m) {
                RoundedRectangle(cornerRadius: 2)
                    .fill(Theme.sourceColor(lane.client))
                    .frame(width: 6, height: 36)
                    .accessibilityHidden(true)

                VStack(alignment: .leading, spacing: 5) {
                    HStack(spacing: Space.s) {
                        Text(lane.displayTitle)
                            .workFont(.rowLabel).foregroundStyle(Theme.ink)
                            .lineLimit(2)
                            .layoutPriority(1)
                        if lane.status == "blocked" {
                            Chip(text: "blocked", tint: Theme.coral)
                        }
                        if isSubagent, let kind = lane.sessionKind {
                            Chip(text: kind, tint: Theme.muted)
                        }
                    }
                    HStack(spacing: Space.s) {
                        Text(WorksetFormat.sourceLabel(lane.client ?? "unknown"))
                            .workFont(.caption).foregroundStyle(Theme.muted)
                        laneOutcome
                    }
                }

                Spacer(minLength: Space.s)

                VStack(alignment: .trailing, spacing: 4) {
                    if let cost = WorksetFormat.laneCost(lane) {
                        Text(cost).workFont(.dataSmall).foregroundStyle(Theme.ink)
                    }
                    HStack(spacing: Space.s) {
                        if let tokens = lane.totalTokens, tokens > 0 {
                            Text("\(UsageTotals.compact(tokens)) tok")
                                .workFont(.dataSmall).foregroundStyle(Theme.muted)
                        }
                        if let dur = WorksetFormat.duration(lane.durationSeconds) {
                            Text(dur).workFont(.dataSmall).foregroundStyle(Theme.muted)
                        }
                    }
                }

                Image(systemName: "chevron.right")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Theme.muted)
                    .accessibilityHidden(true)
            }
            .padding(.vertical, Space.m)
            .padding(.horizontal, Space.xs)
            .contentShape(Rectangle())
        }
        .buttonStyle(SurfaceButtonStyle(focusInset: 2))
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityLabel)
        .accessibilityHint("Opens this session")
    }

    /// Step count and the honest check tally. A failing count is coral; a plain
    /// check total stays muted — the group view never implies verification with
    /// green, matching the workset honesty note.
    @ViewBuilder private var laneOutcome: some View {
        HStack(spacing: 6) {
            if let steps = lane.steps, steps > 0 {
                Text("· \(steps) step\(steps == 1 ? "" : "s")")
                    .workFont(.dataSmall).foregroundStyle(Theme.muted)
            }
            if let failed = lane.checksFailed, failed > 0 {
                Text("· \(failed) failed")
                    .workFont(.dataSmallSemibold).foregroundStyle(Theme.coral)
            } else if let checks = lane.checks, checks > 0 {
                Text("· \(checks) check\(checks == 1 ? "" : "s")")
                    .workFont(.dataSmall).foregroundStyle(Theme.muted)
            }
        }
    }

    private var accessibilityLabel: String {
        var parts = [lane.displayTitle, WorksetFormat.sourceLabel(lane.client ?? "unknown")]
        if isSubagent, let kind = lane.sessionKind { parts.append("\(kind) subagent") }
        if let status = lane.status { parts.append(status) }
        if let steps = lane.steps, steps > 0 { parts.append(Fmt.count(steps, "step")) }
        if let failed = lane.checksFailed, failed > 0 { parts.append(Fmt.count(failed, "failed check")) }
        else if let checks = lane.checks, checks > 0 { parts.append(Fmt.count(checks, "check")) }
        if let cost = WorksetFormat.laneCost(lane) { parts.append(cost) }
        if let tokens = lane.totalTokens, tokens > 0 { parts.append("\(UsageTotals.compact(tokens)) tokens") }
        if let dur = WorksetFormat.duration(lane.durationSeconds) { parts.append(dur) }
        return parts.joined(separator: ", ")
    }
}

// MARK: - Detail overview (KPI tiles + session-outcome bar)

/// The work group's honest sums, as tiles. A workset never re-grades — these
/// are a sum of independent receipts, so the Checks tile flags failures but the
/// composition (which sessions are blocked / done) lives in the outcome bar and
/// the per-session list, never in a single combined verdict.
private struct WorksetKPIRow: View {
    let card: WorksetCard

    private var lanes: [WorksetLane] { card.sessions }
    private var stepsSum: Int { lanes.reduce(0) { $0 + ($1.steps ?? 0) } }
    private var checksSum: Int { lanes.reduce(0) { $0 + ($1.checks ?? 0) } }
    private var failedSum: Int { lanes.reduce(0) { $0 + ($1.checksFailed ?? 0) } }
    private var toolCallsSum: Int { lanes.reduce(0) { $0 + ($1.toolCalls ?? 0) } }

    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: Space.s)], spacing: Space.s) {
            PanelTile(label: "Steps", value: "\(stepsSum)")
            PanelTile(label: "Checks", value: "\(checksSum)",
                      detail: failedSum > 0 ? "\(failedSum) failed" : nil,
                      accent: failedSum > 0 ? Theme.coral : Theme.ink)
            PanelTile(label: "Tool calls", value: toolCallsSum > 0 ? "\(toolCallsSum)" : "—")
            PanelTile(label: card.summary.costComplete == true ? "Cost, sum" : "Cost, partial",
                      value: worksetCostLabel(card.summary) ?? "not priced")
            PanelTile(label: "Tokens",
                      value: card.summary.totalTokens.map { UsageTotals.compact($0) } ?? "—")
            PanelTile(label: "Span",
                      value: WorksetFormat.span(from: card.summary.firstActivityAt, to: card.summary.lastActivityAt))
        }
    }
}

enum WorksetOutcome {
    /// Sessions grouped by their own recorded status — a sum of statuses, not a
    /// re-graded verdict (green is never used here; completion stays ink).
    static func sessionSegments(_ lanes: [WorksetLane]) -> [OutcomeSegment] {
        var completed = 0, active = 0, blocked = 0, handedOff = 0, other = 0
        for lane in lanes {
            switch lane.status {
            case "completed": completed += 1
            case "active": active += 1
            case "blocked": blocked += 1
            case "handed_off": handedOff += 1
            default: other += 1
            }
        }
        var segments: [OutcomeSegment] = []
        if completed > 0 { segments.append(.init(count: completed, color: Theme.ink, label: "completed")) }
        // `active` takes the chart-bar token, not the cobalt accent: this bar is
        // a proportional data mark, and the accent is reserved for the
        // interactive voice (K04, AccentReservationTests).
        if active > 0 { segments.append(.init(count: active, color: Theme.chartBar, label: "active")) }
        if blocked > 0 { segments.append(.init(count: blocked, color: Theme.coral, label: "blocked")) }
        if handedOff > 0 { segments.append(.init(count: handedOff, color: Theme.amber, label: "handed off")) }
        if other > 0 { segments.append(.init(count: other, color: Theme.muted, label: "other")) }
        return segments
    }
}

// MARK: - Shared summary + honesty note (used by the card and the detail)

private struct WorksetSummaryRow: View {
    let summary: WorksetSummary

    var body: some View {
        HStack(alignment: .top, spacing: Space.xl) {
            metric("sessions", "\(summary.sessionCount)")
            metric("sources", "\(summary.sources.count)")
            metric("span", WorksetFormat.span(from: summary.firstActivityAt, to: summary.lastActivityAt))
            if let cost = worksetCostLabel(summary) {
                metric(summary.costComplete == true ? "cost, sum of receipts" : "cost, partial sum", cost)
            }
            Spacer()
        }
    }

    private func metric(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).workFont(.caption).foregroundStyle(Theme.muted)
            Text(value).workFont(.kpi).foregroundStyle(Theme.ink)
        }
    }
}

private struct WorksetHonestyNote: View {
    let summary: WorksetSummary

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if (summary.unpricedSessions ?? 0) > 0 {
                note("Some sessions here carry no imported cost, so the total above is a partial sum.")
            }
            note("Grouped because you pointed this at a folder. Each session keeps its own receipt and evidence; the total is a sum of \(summary.sessionCount) session\(summary.sessionCount == 1 ? "" : "s"), not a combined verdict.")
        }
    }

    private func note(_ text: String) -> some View {
        HStack(alignment: .top, spacing: 6) {
            Image(systemName: "info.circle").workFont(.caption).foregroundStyle(Theme.muted)
            Text(text).workFont(.caption).foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
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

    /// One session's own cost in the app-wide grammar: a bare `$` for a
    /// billed/reported figure, `≈$` for a pricing estimate, nothing when
    /// unpriced — never a fabricated $0.
    static func laneCost(_ lane: WorksetLane) -> String? {
        Fmt.costDisplay(usd: lane.estimatedCostUsd, complete: lane.estimatedCostUsd != nil, confidence: lane.costConfidence)
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

    /// A group that fits inside two days reads as a workday, so its axis
    /// carries the clock in the viewer's own time zone ("Sep 15 09:05"); a
    /// longer span keeps the date-only label.
    static let axisClockSpan: Double = 48 * 3600

    private static let axisClockFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "MMM d HH:mm"
        formatter.timeZone = TimeZone.current
        return formatter
    }()

    static func axisLabel(_ epoch: Double, span: Double) -> String {
        guard span.isFinite, span >= 0, span < axisClockSpan else { return axisDate(epoch) }
        return axisClockFormatter.string(from: Date(timeIntervalSince1970: epoch))
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
