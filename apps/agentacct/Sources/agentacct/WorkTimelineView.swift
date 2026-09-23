import AppKit
import SwiftUI
import UniformTypeIdentifiers

/// Navigation preferences are separate from receipt/disposition storage.
/// Only task-scoped view state is persisted, never a reconstructed ledger.
enum WorkTimelinePreferences {
    static func key(_ taskID: String) -> String { "work.timeline.navigation.v1.\(taskID)" }
    static func load(taskID: String, defaults: UserDefaults = .standard) -> WorkTimelineNavigation {
        guard !SnapshotMode.enabled, let data = defaults.data(forKey: key(taskID)),
              let state = try? JSONDecoder().decode(WorkTimelineNavigation.self, from: data) else {
            return WorkTimelineNavigation()
        }
        return state
    }
    static func save(_ state: WorkTimelineNavigation, taskID: String, defaults: UserDefaults = .standard) {
        guard !SnapshotMode.enabled else { return }
        if let data = try? JSONEncoder().encode(state) { defaults.set(data, forKey: key(taskID)) }
    }
}

private struct WorkCompactViewportKey: EnvironmentKey {
    static let defaultValue = false
}

extension EnvironmentValues {
    var workCompactViewport: Bool {
        get { self[WorkCompactViewportKey.self] }
        set { self[WorkCompactViewportKey.self] = newValue }
    }
}

struct WorkTimelineView: View {
    let receipt: Receipt
    var reviewSelectedRecord = false
    var onRevealInspector: (() -> Void)? = nil
    var onRevealRecords: (() -> Void)? = nil
    var onRevealHeading: (() -> Void)? = nil
    @Environment(DashboardStore.self) private var dashboard
    @Environment(AppSelection.self) private var appSelection
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.workCompactViewport) private var compactViewport
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @State private var navigation = WorkTimelineNavigation()
    @State private var feed = WorkTimelineFeed()
    @State private var followWindowSpan: Double = 30 * 60
    @State private var timeline: TaskTimelinePage?
    @State private var timelineError: String?
    @State private var workProjection: WorkProjectionMetadata?
    @State private var activeTaskID: String?
    @State private var safetyRevision = 0
    @State private var loadingInitialSnapshot = false
    @State private var restoredPositionFromCurrentEvidence = false
    @State private var lastObserved: Date?
    @State private var scrollTarget: String?
    @State private var showingArrivals = false
    @State private var clusterMembers: [WorkTimelineRecord] = []
    @State private var clusterBounds: WorkTimelineInterval?
    @State private var chooserPosition: String?
    @State private var exportError: String?
    @FocusState private var focusedEvidence: String?
    @AccessibilityFocusState private var accessibleEvidence: String?
    @State private var requestedRowFocus: String?
    @State private var scrollRequest = 0
    @State private var viewIsVisible = false
    @State private var focusGeneration = 0
    @State private var viewport = WorkTimelineViewport()
    @State private var preferenceSaveTask: Task<Void, Never>?

    private var projection: WorkTimelineProjection {
        (timeline ?? receipt.timeline)?.projection(taskID: receipt.taskId) ?? .empty
    }
    private var displayProjection: WorkTimelineProjection {
        SnapshotMode.enabled && !SnapshotMode.interactiveFixture
            ? receipt.timeline?.projection(taskID: receipt.taskId) ?? .empty
            : feed.visible
    }
    private var latestProjection: WorkTimelineProjection { SnapshotMode.enabled && !SnapshotMode.interactiveFixture ? displayProjection : feed.latest }
    private var incomplete: Bool {
        guard let page = timeline ?? receipt.timeline else { return true }
        return page.truncated || page.events.contains { $0.id == nil }
    }
    private var matchingRecords: [WorkTimelineRecord] {
        guard showingArrivals || !navigation.view.query.isEmpty || navigation.view.file != nil
                || navigation.view.failuresOnly else { return displayProjection.records }
        return displayProjection.records.filter { record in
            (!showingArrivals || feed.arrivalIDs.contains(record.id))
                && (navigation.view.query.isEmpty || record.searchableText.localizedCaseInsensitiveContains(navigation.view.query))
                && (navigation.view.file == nil || record.files.contains(navigation.view.file!))
                && (!navigation.view.failuresOnly || record.isCurrentFailure)
        }
    }
    private var filtered: [WorkTimelineRecord] {
        let window = interval
        return matchingRecords.filter { window?.contains($0) ?? true }
    }
    private var selected: WorkTimelineRecord? { displayProjection.records.first { $0.id == navigation.view.selectedID } }
    private var interval: WorkTimelineInterval? {
        guard let full = displayProjection.interval else { return nil }
        guard let saved = navigation.view.interval else { return initialWindow(full) }
        // The canvas may pan a small, capped margin beyond the recorded range
        // so cards on the first and last records stay fully reachable; this
        // safety net matches that bound for restored or foreign values.
        let domain = WorkTimeCanvasLayout.expandedDomain(full,
            by: WorkTimeCanvasLayout.maximumEdgeRevealFraction * saved.span)
        return WorkTimeCanvasLayout.clampedWindow(saved, to: domain)
    }
    private func initialWindow(_ full: WorkTimelineInterval?) -> WorkTimelineInterval? {
        guard let full else { return nil }
        return WorkTimeCanvasLayout.latestWindow(within: full,
            latest: displayProjection.newestRecord?.latestTime, span: 30 * 60)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            heading
            if let projection = workProjection ?? timeline?.workProjection {
                WorkProjectionNotice(projection: projection, isOffline: dashboard.isOfflineSnapshot)
            }
            filters
            if !loadingInitialSnapshot, incomplete || timelineError != nil {
                Text(timelineError == nil ? "Activity history is incomplete" : "Activity refresh failed · showing retained records")
                    .workFont(.caption).foregroundStyle(Theme.amber)
            }
            if restoredPositionFromCurrentEvidence {
                Text("Position restored using current records. The earlier snapshot is no longer available.")
                    .workFont(.caption).foregroundStyle(Theme.muted)
                    .accessibilityIdentifier("work.timeline.restored-position")
            }
            if let file = navigation.view.file {
                HStack(alignment: .top) {
                    Text("Exact file reference: \(file)").workFont(.caption).textSelection(.enabled)
                    Spacer(minLength: 4)
                    Button(navigation.view.previousFileFilters == nil ? "Clear file" : "Back to previous filters") {
                        hold(); navigation.leaveFile()
                    }.buttonStyle(QuietButtonStyle(horizontalPadding: 8))
                }
            }
            if showingArrivals {
                let arrivals = feed.arrivalIDs.count - feed.removedArrivalCount
                Text("\(arrivals) new or changed \(arrivals == 1 ? "record" : "records")")
                    .workFont(.caption).foregroundStyle(Theme.muted)
                if feed.removedArrivalCount > 0 {
                    Text("\(feed.removedArrivalCount) earlier \(feed.removedArrivalCount == 1 ? "record is" : "records are") unavailable in this snapshot.")
                        .workFont(.caption).foregroundStyle(Theme.amber)
                }
            }
            evidenceAndInspector
            if let exportError { Text(exportError).workFont(.caption).foregroundStyle(Theme.coral) }
            // Degradation notices stay visible where they qualify the canvas;
            // the static explanations live with the Activity help. There is no
            // second "Recording details" fold here; the receipt document below
            // the timeline owns the Recording section.
            let notices = latestProjection.notices
            if !notices.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(notices, id: \.self) { notice in
                        Text(notice).workFont(.caption).foregroundStyle(Theme.muted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }.padding(.top, 6)
            }
        }
        .workFont(.body)
        .padding(Space.m)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Theme.cardLine))
        .task(id: loadKey) { await loadTask() }
        .onChange(of: receipt.timeline) { _, _ in
            guard activeTaskID == receipt.taskId, timeline == nil else { return }
            receive(projection)
        }
        .onChange(of: dashboard.nativeReviewRevision) { _, _ in
            guard SnapshotMode.enabled, SnapshotMode.interactiveFixture else { return }
            timeline = nil
            receive(projection)
        }
        .onChange(of: navigation) { _, value in
            guard let activeTaskID else { return }
            // Writing UserDefaults on every wheel tick invalidates AppStorage
            // elsewhere in the app. Persist after the gesture settles; leaving
            // the view still flushes immediately through saveMemory().
            preferenceSaveTask?.cancel()
            preferenceSaveTask = Task {
                do { try await Task.sleep(for: .milliseconds(250)) } catch { return }
                guard !Task.isCancelled, self.activeTaskID == activeTaskID else { return }
                WorkTimelinePreferences.save(value, taskID: activeTaskID)
            }
        }
        .onChange(of: focusedEvidence) { _, id in
            guard let id, id != "timeline-heading" else { return }
            let recordID = id == "inspector" ? navigation.view.selectedID : id
            appSelection.workReturnFocus.remember(taskID: receipt.taskId, recordID: recordID)
        }
        .onAppear { viewIsVisible = true }
        .onDisappear {
            viewIsVisible = false
            focusGeneration += 1
            preferenceSaveTask?.cancel()
            saveMemory()
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("work.timeline")
    }

    /// Selection details open below the timeline, never in a floating window:
    /// the canvas keeps its position and scale, and the region reveals itself
    /// by scrolling the outer page only when it would open out of view.
    private var evidenceAndInspector: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            recordsSurface.id("work.timeline.records")
                .background(WorkTimelineScrollObserver(viewport: viewport) { hold() })
            selectionRegion
        }
    }

    @ViewBuilder private var selectionRegion: some View {
        if !clusterMembers.isEmpty && navigation.view.selectedID == nil {
            clusterChooser
        } else if navigation.view.selectedID != nil {
            inspector
        }
    }

    /// The dense-group member list uses the same surface as record details,
    /// so moving between a group and a member never opens another window.
    private var clusterChooser: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top) {
                Text(Fmt.count(clusterMembers.count, "record")).workFont(.titleCard)
                    .accessibilityFocused($accessibleEvidence, equals: "inspector")
                Spacer()
                Button("Zoom here") {
                    hold()
                    if let bounds = clusterBounds, let full = displayProjection.interval {
                        let padding = max(bounds.span * 0.15, 1)
                        navigation.view.interval = WorkTimeCanvasLayout.clampedWindow(.init(
                            lower: bounds.lower - padding, upper: bounds.upper + padding), to: full)
                    }
                    clearSelection()
                }.buttonStyle(QuietButtonStyle(horizontalPadding: 8))
                .disabled(clusterBounds.map { $0.span <= 1 || $0.span >= (interval?.span ?? 0) * 0.9 } ?? true)
                .help("Resize the visible window to this group's time range")
                Button { dismissSelection() } label: { Image(systemName: "xmark") }
                    .buttonStyle(QuietButtonStyle(horizontalPadding: 7))
                    .accessibilityLabel("Close record group")
                    .accessibilityIdentifier("work.timeline.cluster.close")
                    .help("Close record group")
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(clusterMembers) { record in
                        Button {
                            inspect(record)
                        } label: {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(record.title).workFont(.rowLabel)
                                Text("\(record.laneTitle) · \(record.resultLabel)")
                                    .workFont(.caption).foregroundStyle(color(record))
                            }.frame(maxWidth: .infinity, alignment: .leading).padding(6)
                        }.buttonStyle(SurfaceButtonStyle())
                        .accessibilityIdentifier("work.timeline.chooser.record.\(record.id)")
                    }
                }.scrollTargetLayout()
            }.scrollPosition(id: $chooserPosition).frame(maxHeight: 300)
        }
        .padding(Space.m).frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.canvas, in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Theme.cardLine))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("work.timeline.cluster.chooser")
        .accessibilityLabel("\(clusterMembers.count) records in a time group")
        .background(WorkTimelineRevealProbe { onRevealInspector?() })
        .onKeyPress(.escape) { dismissSelection(); return .handled }
    }

    private var selectionActive: Bool { !clusterMembers.isEmpty || navigation.view.selectedID != nil }

    private func clearSelection() {
        navigation.view.selectedID = nil
        clusterMembers = []
        clusterBounds = nil
    }

    private func dismissSelection() {
        let focusTarget = selected?.id ?? clusterMembers.first?.id
        clearSelection()
        requestedRowFocus = focusTarget
        scrollRequest += 1
    }

    private func presentCluster(_ members: [WorkTimelineRecord], bounds: WorkTimelineInterval) {
        hold()
        clusterBounds = bounds
        clusterMembers = members
        navigation.view.selectedID = nil
        focusSelectionRegion()
    }

    private func focusSelectionRegion() {
        // Keyboard focus stays on the triggering card; VoiceOver focus moves
        // to the region's heading. An inline region is non-modal, so reading
        // position and Escape/Space handling on the card are preserved.
        let generation = focusGeneration
        Task { @MainActor in
            await Task.yield()
            guard viewIsVisible, selectionActive, generation == focusGeneration else { return }
            await Task.yield()
            guard viewIsVisible, selectionActive, generation == focusGeneration else { return }
            accessibleEvidence = "inspector"
        }
    }

    @ViewBuilder private var recordsSurface: some View {
        if let full = displayProjection.interval, let interval {
            WorkTimeCanvas(records: matchingRecords, full: full, window: interval,
                selectedRecord: selected,
                onWindow: { value in hold(); navigation.view.interval = value },
                onSelect: { record in
                    // A canvas selection replaces any open group chooser; the
                    // chooser's member path calls inspect directly and keeps
                    // its Back affordance.
                    clusterMembers = []
                    clusterBounds = nil
                    inspect(record)
                },
                onCluster: { members, bounds in presentCluster(members, bounds: bounds) },
                onDismiss: { clearSelection() },
                onHold: hold, focusRecordID: requestedRowFocus, focusRequest: scrollRequest, compact: compactViewport)
        } else {
            Text(displayProjection.records.isEmpty ? "No activity recorded yet." : "Recorded times are unavailable.")
                .workFont(.body).foregroundStyle(Theme.muted)
                .frame(maxWidth: .infinity, minHeight: 180)
        }
        let undated = matchingRecords.filter { $0.start == nil }
        if !undated.isEmpty {
            Menu {
                ForEach(undated) { record in
                    Button("\(record.title) · \(record.laneTitle) · \(record.resultLabel)") {
                        clusterMembers = []
                        clusterBounds = nil
                        inspect(record, focusInspector: false)
                    }.buttonStyle(QuietButtonStyle())
                }
            } label: {
                Label("Time unavailable · \(undated.count)", systemImage: "clock.badge.questionmark")
            }
            .menuStyle(.borderlessButton).buttonStyle(QuietButtonStyle())
            .fixedSize().accessibilityIdentifier("work.timeline.undated")
            .help("These records have no usable timestamp and cannot be placed on the timeline.")
        }
    }

    private var loadKey: String { receipt.taskId }

    private var heading: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 12) { headingLabel; Spacer(minLength: 0); liveControls; activityMenu }
            VStack(alignment: .leading, spacing: 8) {
                headingLabel
                HStack(spacing: 8) { Spacer(minLength: 0); liveControls; activityMenu }
            }
        }
    }
    private var headingLabel: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 4) {
            Text("Activity").workFont(.titleCard)
                .id("work.timeline.heading")
                .focusable().focused($focusedEvidence, equals: "timeline-heading")
                .accessibilityFocused($accessibleEvidence, equals: "timeline-heading")
                .accessibilityAddTraits(.isHeader)
                ContextHelp(title: "About activity",
                    message: "Drag the canvas to move through time. Scroll to make the visible time span smaller or larger, or pinch to zoom around the pointer. Drag the overview window to move it and its edges to resize it. Select a record to read its details below the timeline; dense groups list their members there. Stems mark recorded times, not causal links. Section spans end at the latest reported update, not a measured execution finish. Check markers are points. Loaded history refreshes every 3 seconds while this view is open; search covers loaded records.",
                    identifier: "work.timeline.help")
            }
            HStack(spacing: 5) {
                if loadingInitialSnapshot && !reduceMotion {
                    ProgressView().controlSize(.mini).accessibilityLabel("Loading activity history")
                }
                if dashboard.isOfflineSnapshot {
                    Text("Saved copy · offline").workFont(.caption).foregroundStyle(Theme.muted)
                }
            }
        }
    }

    @ViewBuilder private var activityMenu: some View {
        if SnapshotMode.rendersStaticControls {
            // The offscreen docs renderer draws a SwiftUI Menu as a yellow
            // "unsupported control" placeholder, so the docs screenshots show
            // the menu's label as a static primitive instead. The live app and
            // the golden fixture renders (which never set this flag) are unchanged.
            Image(systemName: "ellipsis")
                .padding(.horizontal, 8)
                .accessibilityLabel("Activity actions")
        } else {
            Menu {
                Button("Show all time") { hold(); navigation.view.interval = displayProjection.interval }
                    .buttonStyle(QuietButtonStyle())
                Button("Export visible records…", action: exportReview)
                    .disabled(filtered.isEmpty)
                    .buttonStyle(QuietButtonStyle())
            } label: {
                Image(systemName: "ellipsis")
            }
            .menuStyle(.borderlessButton)
            .buttonStyle(QuietButtonStyle(horizontalPadding: 8))
            .fixedSize()
            .accessibilityLabel("Activity actions")
            .accessibilityIdentifier("work.timeline.actions")
            .help("Activity actions")
        }
    }

    private func exportReview() {
        hold()
        let panel = NSSavePanel()
        panel.allowedContentTypes = [.plainText]
        panel.nameFieldStringValue = "work-review.txt"
        panel.title = "Export displayed work evidence"
        panel.message = "\(filtered.count) visible \(filtered.count == 1 ? "record" : "records") with source identities and recording limitations."
        guard panel.runModal() == .OK, let destination = panel.url else { return }
        do {
            let content = WorkTimelineExport.text(taskID: receipt.taskId, title: receipt.title,
                records: filtered, projection: displayProjection, following: navigation.following,
                query: navigation.view.query, file: navigation.view.file, generatedAt: Date(),
                snapshotAt: lastObserved,
                failuresOnly: navigation.view.failuresOnly, interval: interval,
                offlineReceiptAt: dashboard.receiptSavedAt,
                outcome: receipt.axes.decisionStatus.key,
                handoff: receipt.axes.handoff.map { "\($0.handedOff == true ? "Handed off" : "Not the current handoff frontier") · \($0.statement ?? "No handoff statement supplied")" })
            try content.write(to: destination, atomically: true, encoding: .utf8)
            exportError = nil
        } catch { exportError = "Could not export this review: \(error.localizedDescription)" }
    }

    @ViewBuilder private var liveControls: some View {
        let pendingCount = feed.pendingIDs.count
        if !dashboard.isOfflineSnapshot {
        Button {
            cancelDeferredFocus()
            if navigation.following { hold() } else {
                followWindowSpan = interval?.span ?? 30 * 60
                navigation.following = true
                showingArrivals = false
                feed.reveal()
                navigation.view.interval = trailingWindow(displayProjection.interval, span: interval?.span)
                scrollTarget = displayProjection.newestRecord?.id
            }
        } label: {
            Label(navigation.following ? "Live" : "Resume live", systemImage: navigation.following ? "pause.circle" : "play.circle")
        }
        .buttonStyle(QuietButtonStyle(horizontalPadding: 8))
        .accessibilityLabel(navigation.following ? "Pause live updates" : "Resume live updates")
        .disabled(dashboard.isOfflineSnapshot)
        .help("Pause to keep this history still. Resume reveals the latest snapshot and moves to its newest record; search and file filters stay in place.")
        .accessibilityIdentifier("work.timeline.follow")
        if pendingCount > 0 {
        Button("\(pendingCount) new") {
            if navigation.history == nil { navigation.view.scrollOffsets = viewport.capture() }
            navigation.beginArrivals()
            feed.reviewArrivals()
            navigation.view.interval = displayProjection.interval
            showingArrivals = true
        }.buttonStyle(QuietButtonStyle(horizontalPadding: 8))
        .accessibilityLabel("Review \(pendingCount) new or changed records")
        .accessibilityIdentifier("work.timeline.arrivals")
        }
        if navigation.history != nil {
            Button("Back to history") {
                navigation.returnToHistory()
                feed.restoreHistory()
                showingArrivals = false
                restoreHistoryPosition()
            }.buttonStyle(QuietButtonStyle(horizontalPadding: 8))
            .accessibilityIdentifier("work.timeline.history")
        }
        }
    }

    private var filters: some View {
        let layout = dynamicTypeSize.isAccessibilitySize
            ? AnyLayout(VStackLayout(alignment: .leading, spacing: Space.m))
            : AnyLayout(HStackLayout(spacing: Space.m))
        return VStack(alignment: .leading, spacing: 8) {
            layout {
                recordSearch
            }
            let failureCount = displayProjection.records.filter(\.isCurrentFailure).count
            if failureCount > 0 || navigation.view.failuresOnly || hasActiveFilters {
                layout {
                    if failureCount > 0 || navigation.view.failuresOnly {
                        Button(navigation.view.failuresOnly ? "Show all records" : "\(failureCount) failed \(failureCount == 1 ? "record" : "records")") {
                            hold(); navigation.view.failuresOnly.toggle()
                        }.buttonStyle(QuietButtonStyle(horizontalPadding: 8))
                        .accessibilityIdentifier("work.timeline.failures")
                        .help("Current failures in loaded records; one check may appear in more than one source.")
                    }
                    if hasActiveFilters {
                        Text("\(filtered.count) of \(displayProjection.records.count) loaded records")
                            .workFont(.caption).foregroundStyle(Theme.muted)
                        Button("Clear filters") { clearFilters() }
                            .buttonStyle(QuietButtonStyle(horizontalPadding: 8))
                            .accessibilityIdentifier("work.timeline.clear-filters")
                    }
                    Spacer(minLength: 0)
                }
            }
        }
    }

    private var hasActiveFilters: Bool {
        !navigation.view.query.isEmpty || navigation.view.file != nil
            || navigation.view.failuresOnly
    }

    private func clearFilters() {
        hold()
        navigation.view.query = ""
        navigation.view.file = nil
        navigation.view.previousFileFilters = nil
        navigation.view.failuresOnly = false
    }

    private var recordSearch: some View {
        HStack(spacing: Space.s) {
            Image(systemName: "magnifyingglass").foregroundStyle(Theme.muted)
                .accessibilityHidden(true)
            if SnapshotMode.enabled && !SnapshotMode.interactiveFixture {
                // ImageRenderer cannot draw AppKit text fields, and their
                // placeholder height varies between accessibility renders.
                Text(navigation.view.query.isEmpty ? "Search activity" : navigation.view.query)
                    .workFont(.body)
                    .foregroundStyle(navigation.view.query.isEmpty ? Theme.muted : Theme.ink)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 6).padding(.vertical, 3)
                    .background(Theme.card, in: RoundedRectangle(cornerRadius: 4))
                    .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(Theme.cardLine))
            } else {
                TextField("Search activity", text: Binding(
                    get: { navigation.view.query },
                    set: { hold(); navigation.view.query = $0 }
                ))
                .textFieldStyle(.roundedBorder)
                .workFont(.body)
                .accessibilityLabel("Search loaded activity, sessions and files")
                .accessibilityIdentifier("work.timeline.search")
            }
        }
        .frame(minWidth: 240, maxWidth: .infinity)
    }

    private func evidenceIdentity(_ record: WorkTimelineRecord) -> String {
        record.laneID.hasPrefix("task:")
            ? "Task evidence identity: \(record.laneID) · session attribution unavailable"
            : "Session identity: \(record.laneID)"
    }

    @ViewBuilder private var inspector: some View {
        if let record = selected {
            VStack(alignment: .leading, spacing: 12) {
                if !clusterMembers.isEmpty {
                    Button { navigation.view.selectedID = nil } label: {
                        Label("Back to \(Fmt.count(clusterMembers.count, "record"))", systemImage: "chevron.left")
                    }.buttonStyle(QuietButtonStyle(horizontalPadding: 8))
                    .accessibilityIdentifier("work.timeline.cluster.back")
                    .help("Return to the group member list")
                }
                HStack(alignment: .top, spacing: 12) {
                    VStack(alignment: .leading, spacing: 8) {
                        Text(record.title).workFont(.titleCard)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityFocused($accessibleEvidence, equals: "inspector")
                            .accessibilityAddTraits(.isHeader)
                        HStack(spacing: 6) {
                            Text(record.resultLabel + (record.superseded ? " · Superseded" : ""))
                                .foregroundStyle(color(record))
                            Text(record.source).foregroundStyle(Theme.muted)
                            if let note = record.identityNote {
                                Image(systemName: "info.circle")
                                    .foregroundStyle(Theme.muted).help(note)
                                    .accessibilityLabel(note)
                            }
                        }.workFont(.caption)
                        Text(record.start.map(Self.dateText) ?? "Source time unavailable")
                            .workFont(.caption).foregroundStyle(Theme.muted)
                    }.frame(maxWidth: .infinity, alignment: .leading)
                    Button { dismissInspector(record) } label: { Image(systemName: "xmark") }
                        .buttonStyle(QuietButtonStyle(horizontalPadding: 7))
                        .focused($focusedEvidence, equals: "inspector")
                        .onKeyPress(.escape) { dismissInspector(record); return .handled }
                        .accessibilityLabel("Close record details")
                        .accessibilityIdentifier("work.timeline.inspector.close")
                        .help("Close record details")
                }
                Divider().overlay(Theme.hairline)
                if let summary = record.summary, summary != record.title {
                    Text(summary).workFont(.body).textSelection(.enabled)
                }
                if let warning = record.timeWarning { Text(warning).workFont(.caption).foregroundStyle(Theme.amber) }
                if let disposition = record.disposition {
                    Text("Human disposition: \(disposition). The recorded check result is unchanged.").workFont(.caption)
                }
                if let resolution = record.resolutionDescription { Text(resolution).workFont(.caption).textSelection(.enabled) }
                if let code = record.exitCode, code != 0 { Text("Exit code: \(code)").workFont(.caption) }
                if !filtered.contains(where: { $0.id == record.id }) {
                    Text("Outside the current filters").workFont(.caption).foregroundStyle(Theme.amber)
                    Button("Show this record") { clearFilters(); focusSelectedRange(); returnToRecord(record) }
                        .buttonStyle(QuietButtonStyle(horizontalPadding: 8))
                }
                let associatedChecks = displayProjection.records.filter { $0.sectionRecordIDs.contains(record.id) || $0.sectionRecordID == record.id }
                if !associatedChecks.isEmpty {
                    DisclosureGroup("Checks · \(associatedChecks.count)") {
                        ForEach(associatedChecks) { check in
                            Button("\(check.title) · \(check.resultLabel)\(check.superseded ? " · Superseded" : "") · \(check.source)") { inspect(check) }
                                .buttonStyle(QuietButtonStyle(horizontalPadding: 8))
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }.workFont(.caption)
                }
                if let supersededBy = record.supersededBy {
                    if displayProjection.records.contains(where: { $0.eventID == supersededBy }) {
                        Button("View later result") { inspectEvent(supersededBy) }
                            .buttonStyle(QuietButtonStyle(horizontalPadding: 8))
                    } else {
                        Text("The later result is not in loaded activity.").workFont(.caption).foregroundStyle(Theme.muted)
                    }
                } else if record.superseded {
                    Text("The source marks this check superseded; a target event is not supplied.").workFont(.caption)
                }
                ForEach(record.sectionRecordIDs, id: \.self) { sectionID in
                    if let section = displayProjection.records.first(where: { $0.id == sectionID }) {
                        Button(record.sectionRecordIDs.count == 1 ? "View work section" : section.title) { inspect(section) }
                            .buttonStyle(QuietButtonStyle(horizontalPadding: 8))
                    }
                }
                if !record.files.isEmpty {
                    DisclosureGroup("Files · \(record.files.count)") {
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(record.files, id: \.self) { file in
                                HStack(alignment: .top, spacing: 8) {
                                    Text(file).workFont(.caption).textSelection(.enabled)
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                    Button { hold(); clearSelection(); navigation.showFile(file) } label: {
                                        Image(systemName: "line.3.horizontal.decrease.circle")
                                    }
                                    .buttonStyle(QuietButtonStyle(horizontalPadding: 6))
                                    .help("Find loaded activity referencing this file")
                                    .accessibilityLabel("Find loaded activity referencing \(file)")
                                }
                            }
                            DisclosureGroup("About file references") {
                                Text("These paths are recorded associations. File contents and diffs are not captured here. Filtering by a file temporarily replaces the other filters; Back to previous filters restores them.")
                                    .workFont(.caption).foregroundStyle(Theme.muted)
                            }
                        }.padding(.top, 6)
                    }.workFont(.caption)
                }
                if !record.artifactDescriptions.isEmpty {
                    DisclosureGroup("Artifacts") {
                        ForEach(record.artifactDescriptions, id: \.self) { Text($0).textSelection(.enabled) }
                    }.workFont(.caption)
                }
                DisclosureGroup("Record details") {
                    VStack(alignment: .leading, spacing: 6) {
                        if let note = record.identityNote { Text(note).foregroundStyle(Theme.muted) }
                        Text("Scope: \(record.scope ?? "unavailable")")
                        if let exitCode = record.exitCode { Text("Recorded exit code: \(exitCode)") }
                        Text(record.timeNote)
                        Text("Session: \(record.laneTitle)")
                        Text(evidenceIdentity(record))
                        Text(record.lineage)
                        Text("Event: \(record.eventID ?? "not supplied")")
                        if let start = record.start { Text("Source time: \(WorkTimelineTimeAxis.preciseLabel(start))") }
                        if let end = record.end { Text("Latest update: \(WorkTimelineTimeAxis.preciseLabel(end))") }
                        if let supersededBy = record.supersededBy { Text("Superseded by event: \(supersededBy)") }
                        if record.commandRedacted { Text("Command text was deliberately not captured.") }
                    }.fixedSize(horizontal: false, vertical: true).textSelection(.enabled).padding(.top, 6)
                }
                .workFont(.caption)
                .accessibilityIdentifier("work.timeline.inspector.identity")
            }
            .id(record.id)
            .padding(Space.m).frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.canvas, in: RoundedRectangle(cornerRadius: Metrics.radius))
            .id("work.timeline.inspector")
            .background(WorkTimelineRevealProbe { onRevealInspector?() })
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("work.timeline.inspector")
            .onKeyPress(.escape) { dismissInspector(record); return .handled }
        } else if navigation.view.selectedID != nil {
            VStack(alignment: .leading, spacing: 8) {
                Text("This record is unavailable in the current snapshot.").workFont(.caption).foregroundStyle(Theme.muted)
                Button("Dismiss") { navigation.view.selectedID = nil }
                    .buttonStyle(QuietButtonStyle(horizontalPadding: 8))
            }
        }
    }

    private func dismissInspector(_ record: WorkTimelineRecord) {
        clearSelection()
        requestedRowFocus = record.id
        scrollRequest += 1
    }

    private func inspect(_ record: WorkTimelineRecord, focusInspector: Bool = true) {
        hold()
        navigation.view.selectedID = record.id
        navigation.view.anchorID = record.id
        appSelection.workReturnFocus.remember(taskID: receipt.taskId, recordID: record.id)
        if focusInspector {
            // Keyboard focus stays on the triggering card; VoiceOver moves to
            // the region heading. The card keeps its Escape/Space contract.
            let generation = focusGeneration
            Task { @MainActor in
                await Task.yield()
                guard viewIsVisible, navigation.view.selectedID == record.id, generation == focusGeneration else { return }
                await Task.yield()
                guard viewIsVisible, navigation.view.selectedID == record.id, generation == focusGeneration else { return }
                accessibleEvidence = "inspector"
            }
        }
    }
    private func returnToRecord(_ record: WorkTimelineRecord) {
        cancelDeferredFocus()
        onRevealRecords?()
        scrollTarget = record.id
        requestedRowFocus = record.id
        scrollRequest += 1
    }
    private func restoreHistoryPosition() {
        cancelDeferredFocus()
        scrollTarget = nil
        let generation = focusGeneration
        let taskID = receipt.taskId
        let offsets = navigation.view.scrollOffsets
        Task { @MainActor in
            // Wait for the original rows and outer layout to return before
            // restoring geometry. A newer user interaction cancels this work.
            try? await Task.sleep(for: .milliseconds(180))
            guard viewIsVisible, activeTaskID == taskID, generation == focusGeneration else { return }
            await Task.yield()
            guard generation == focusGeneration else { return }
            if let offsets, viewport.restore(offsets) { return }
            if let id = navigation.view.anchorID ?? navigation.view.selectedID,
               let record = filtered.first(where: { $0.id == id }) {
                returnToRecord(record)
            } else {
                onRevealHeading?()
                focusedEvidence = "timeline-heading"; accessibleEvidence = "timeline-heading"
            }
        }
    }
    private func restoreReturnFocusIfReady() {
        guard !loadingInitialSnapshot,
              let target = appSelection.workReturnFocus.consume(taskID: receipt.taskId, visibleRecordIDs: Set(filtered.map(\.id))) else { return }
        switch target {
        case .record(let id):
            hold()
            navigation.view.selectedID = id
            navigation.view.anchorID = id
            onRevealRecords?()
            scrollTarget = id
            requestedRowFocus = id
            scrollRequest += 1
        case .heading:
            let taskID = receipt.taskId
            let generation = focusGeneration
            onRevealHeading?()
            Task { @MainActor in
                try? await Task.sleep(for: .milliseconds(180))
                guard viewIsVisible, activeTaskID == taskID, appSelection.taskId == taskID, generation == focusGeneration else { return }
                focusedEvidence = "timeline-heading"; accessibleEvidence = "timeline-heading"
            }
        }
    }

    private func focusSelectedRange() {
        guard let selected, let range = WorkTimelineRangeNavigation.focused(on: selected) else { return }
        hold()
        navigation.view.interval = range
        scrollTarget = selected.id
        scrollRequest += 1
    }
    private func inspectEvent(_ eventID: String) {
        if let record = displayProjection.records.first(where: { $0.eventID == eventID }) { inspect(record) }
    }
    private func cancelDeferredFocus() {
        appSelection.workReturnFocus.cancel()
        focusGeneration += 1
        requestedRowFocus = nil
    }
    private func hold() {
        // New interaction wins over both pending and already scheduled focus.
        cancelDeferredFocus()
        loadingInitialSnapshot = false
        navigation.following = false
    }
    private func trailingWindow(_ full: WorkTimelineInterval?, span: Double?) -> WorkTimelineInterval? {
        guard let full else { return nil }
        return WorkTimeCanvasLayout.latestWindow(within: full,
            latest: displayProjection.newestRecord?.latestTime, span: span ?? 30 * 60)
    }
    private func receive(_ projection: WorkTimelineProjection) {
        feed.ingest(projection, following: navigation.following || loadingInitialSnapshot)
        if navigation.following {
            navigation.view.interval = trailingWindow(displayProjection.interval, span: followWindowSpan)
        }
    }
    private func saveMemory() {
        guard let activeTaskID, !dashboard.isOfflineSnapshot, !SnapshotMode.enabled,
              dashboard.receiptProjection?.available != false,
              safetyRevision == dashboard.projectionSafetyRevision else { return }
        WorkTimelineMemory.cache.save(.init(feed: feed), for: activeTaskID)
        WorkTimelinePreferences.save(navigation, taskID: activeTaskID)
    }

    @MainActor private func loadTask() async {
        let taskID = receipt.taskId
        saveMemory()
        activeTaskID = taskID
        safetyRevision = dashboard.projectionSafetyRevision
        navigation = WorkTimelinePreferences.load(taskID: taskID)
        followWindowSpan = max(navigation.view.interval?.span ?? 0, 30 * 60)
        let cached = dashboard.isOfflineSnapshot || SnapshotMode.enabled ? nil : WorkTimelineMemory.cache.load(taskID)
        feed = cached?.feed ?? WorkTimelineFeed()
        timeline = nil
        restoredPositionFromCurrentEvidence = cached == nil && navigation.restorePositionWithoutSnapshot()
        if dashboard.isOfflineSnapshot { navigation.following = false }
        loadingInitialSnapshot = cached == nil && !SnapshotMode.enabled && !dashboard.isOfflineSnapshot
        timelineError = nil
        workProjection = nil
        lastObserved = nil
        showingArrivals = navigation.history != nil
        scrollTarget = navigation.view.anchorID
        if dashboard.isOfflineSnapshot {
            timeline = try? await dashboard.loadTimeline(taskID: taskID, previous: nil)
            guard !Task.isCancelled, activeTaskID == taskID else { return }
        }
        if cached == nil { receive(projection) }
        if SnapshotMode.enabled && reviewSelectedRecord {
            navigation.following = false
            navigation.view.selectedID = displayProjection.records.first?.id
        }
        restoreReturnFocusIfReady()
        guard !SnapshotMode.enabled, !dashboard.isOfflineSnapshot else { return }
        while !Task.isCancelled && activeTaskID == taskID {
            do {
                let page = try await dashboard.loadTimeline(taskID: taskID, previous: timeline)
                guard !Task.isCancelled, activeTaskID == taskID else { return }
                if page != timeline {
                    timeline = page
                    receive(page.projection(taskID: taskID))
                }
                timelineError = nil
                workProjection = page.workProjection
                lastObserved = page.workProjection == nil ? Date() : page.workProjection?.builtDate
            } catch let pending as WorkProjectionPending {
                guard !Task.isCancelled, activeTaskID == taskID else { return }
                workProjection = pending.projection.retainingBuild(from: workProjection)
                if pending.projection.available == false {
                    timeline = nil
                    feed = WorkTimelineFeed()
                    lastObserved = nil
                    navigation.view.selectedID = nil
                }
                timelineError = nil
            } catch {
                guard !Task.isCancelled, activeTaskID == taskID else { return }
                timelineError = error.localizedDescription
            }
            loadingInitialSnapshot = workProjection?.state == "pending" && timeline == nil
            // Missing/filtered evidence focuses the heading, never another row.
            restoreReturnFocusIfReady()
            do { try await Task.sleep(for: .seconds(3)) } catch { return }
        }
    }

    private func color(_ record: WorkTimelineRecord) -> Color { record.presentationTint }
    private func symbol(_ record: WorkTimelineRecord) -> String {
        if record.superseded { return "clock.arrow.circlepath" }
        if record.kind == .step { return "text.alignleft" }
        switch record.result {
        case "failed", "error": return "xmark.circle"
        case "passed": return "checkmark.circle"
        case "skipped": return "forward.end"
        default: return "questionmark.circle"
        }
    }
    private static func dateText(_ time: Double) -> String {
        Date(timeIntervalSince1970: time).formatted(date: .abbreviated, time: .standard)
    }
    private static func shortTime(_ time: Double) -> String {
        Date(timeIntervalSince1970: time).formatted(date: .omitted, time: .standard)
    }
}

/// Reveals the selection region when it appears outside the visible scroll
/// area. It measures its own position against the enclosing scroll view once,
/// on appearance; it never observes scrolling and never moves the page for an
/// already-visible region.
private struct WorkTimelineRevealProbe: NSViewRepresentable {
    var onNeedReveal: () -> Void
    func makeNSView(context: Context) -> RevealProbeView {
        let view = RevealProbeView()
        view.onNeedReveal = onNeedReveal
        return view
    }
    func updateNSView(_ nsView: RevealProbeView, context: Context) { nsView.onNeedReveal = onNeedReveal }

    final class RevealProbeView: NSView {
        var onNeedReveal: (() -> Void)?
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            guard window != nil else { return }
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                var current = superview
                while let view = current {
                    if let scroll = view as? NSScrollView {
                        let visible = scroll.contentView.bounds
                        let mine = convert(bounds, to: scroll.contentView)
                        let heading = CGPoint(x: mine.midX, y: mine.minY)
                        if !visible.contains(heading) { onNeedReveal?() }
                        return
                    }
                    current = view.superview
                }
            }
        }
    }
}

/// A scoped event observer holds the timeline when native scrolling begins.
/// It returns the event unchanged and never captures events from another pane.
private struct WorkTimelineScrollObserver: NSViewRepresentable {
    var viewport: WorkTimelineViewport
    var onScroll: () -> Void
    func makeNSView(context: Context) -> ScrollObserverView {
        let view = ScrollObserverView()
        view.onScroll = onScroll
        viewport.anchor = view
        return view
    }
    func updateNSView(_ nsView: ScrollObserverView, context: Context) {
        nsView.onScroll = onScroll
        viewport.anchor = nsView
    }
    static func dismantleNSView(_ nsView: ScrollObserverView, coordinator: ()) { nsView.stop() }

    final class ScrollObserverView: NSView {
        var onScroll: (() -> Void)?
        var monitor: Any?
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            stop()
            guard window != nil else { return }
            monitor = NSEvent.addLocalMonitorForEvents(matching: .scrollWheel) { [weak self] event in
                guard let self, event.window == self.window,
                      self.visibleRect.contains(self.convert(event.locationInWindow, from: nil)) else { return event }
                self.onScroll?()
                return event
            }
        }
        func stop() {
            if let monitor { NSEvent.removeMonitor(monitor) }
            monitor = nil
        }
    }
}
