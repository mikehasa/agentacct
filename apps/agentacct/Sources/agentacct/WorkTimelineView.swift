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

/// The reading surface a reviewer prefers, across every task.
///
/// The per-task bookmark holds the WINDOW, which is genuinely about one task.
/// Which SURFACE to read records on is a habit, not a fact about a task, so
/// storing it per task meant a reviewer who wants the canvas had to ask for it
/// on every task, forever. `@AppStorage` cannot hold `Bool?`, and the third
/// state matters — "never chosen" must stay distinguishable from "chose the
/// list" so the data still decides the opening view for a reviewer who has
/// never pressed the switch — so it travels as an Int.
enum WorkTimelineSurfaceDefault {
    static let key = "work.timeline.surface.default.v1"
    static let unset = 0
    private static let list = 1
    private static let canvas = 2

    /// The stored value read back as a choice, or nil when nobody has chosen.
    static func choice(_ stored: Int) -> Bool? {
        switch stored {
        case list: return true
        case canvas: return false
        default: return nil
        }
    }

    /// The value to store for an explicit press of the switch.
    static func stored(_ usesRecordList: Bool) -> Int { usesRecordList ? list : canvas }
}

/// WHEN the timeline was last observed, for the export header.
///
/// This deliberately is not SwiftUI state. Nothing in `body` reads it — only
/// `exportReview()` does — but SwiftUI state invalidates its view on every
/// write regardless, so stamping it on each three-second poll rebuilt the
/// Activity surface twenty times a minute for a value nobody was looking at.
/// A reference the view holds keeps the fact and drops the invalidation.
final class WorkTimelineObservationStamp {
    var lastObserved: Date?
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
    /// The record page's Escape router. The timeline publishes a dismissal
    /// here while it holds a transient layer open (K116).
    var layers: WorkRecordLayers? = nil
    var onRevealInspector: (() -> Void)? = nil
    var onRevealRecords: (() -> Void)? = nil
    var onRevealHeading: (() -> Void)? = nil
    @Environment(DashboardStore.self) private var dashboard
    @Environment(AppSelection.self) private var appSelection
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.workCompactViewport) private var compactViewport
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    /// The app-wide reading-surface preference (see `WorkTimelineSurfaceDefault`).
    @AppStorage(WorkTimelineSurfaceDefault.key) private var storedSurfaceDefault = WorkTimelineSurfaceDefault.unset
    @State private var navigation = WorkTimelineNavigation()
    @State private var feed = WorkTimelineFeed()
    @State private var followWindowSpan: Double = 30 * 60
    @State private var timeline: TaskTimelinePage?
    @State private var timelineError: String?
    @State private var activeTaskID: String?
    @State private var loadingInitialSnapshot = false
    @State private var restoredPositionFromCurrentEvidence = false
    @State private var observation = WorkTimelineObservationStamp()
    @State private var scrollTarget: String?
    @State private var showingArrivals = false
    @State private var clusterMembers: [WorkTimelineRecord] = []
    @State private var clusterBounds: WorkTimelineInterval?
    @State private var chooserPosition: String?
    @State private var exportError: String?
    @FocusState private var focusedEvidence: String?
    @FocusState private var searchFocused: Bool
    @AccessibilityFocusState private var accessibleEvidence: String?
    @State private var requestedRowFocus: String?
    @State private var scrollRequest = 0
    @State private var viewIsVisible = false
    @State private var focusGeneration = 0
    @State private var viewport = WorkTimelineViewport()
    @State private var preferenceSaveTask: Task<Void, Never>?
    /// Whether the page may scroll itself to reveal the selection region. Only
    /// a user action arms it (inspecting a record, opening a dense group,
    /// returning to a remembered one); appearing or restoring never does, so
    /// opening a record cannot pull the verdict off-screen (K104).
    @State private var revealArmed = false

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
        // Anchor on the newest record's CARD position — its start — not on its
        // latest update. A long section's card is drawn at its start, so a
        // window anchored on its end opened onto empty canvas while the
        // overview strip beside it showed a populated task (B3).
        let newest = displayProjection.newestRecord
        let anchor = newest?.start ?? newest?.latestTime
        return populated(WorkTimeCanvasLayout.latestWindow(within: full, latest: anchor, span: 30 * 60),
                         within: full)
    }

    /// A window the APP chose must draw at least one card; the rule itself is
    /// pure and lives in `WorkTimelineRangeNavigation` so it is unit-tested
    /// without a view. It tests CARD POSITIONS, not span overlap: a window
    /// holding nothing but the tail of a long section draws no card and prints
    /// "No activity in this time window" (B3).
    private func populated(_ window: WorkTimelineInterval, within full: WorkTimelineInterval) -> WorkTimelineInterval {
        WorkTimelineRangeNavigation.populated(window,
                                              records: displayProjection.records,
                                              newest: displayProjection.newestRecord,
                                              within: full)
    }

    /// The visible window's own span, in the axis's words, or its named
    /// absence. Printed in the heading so the slice on screen always carries
    /// the denominator it is a slice OF.
    private var windowSpanText: String {
        guard let interval else { return PayloadAbsence.activityTime }
        return "\(WorkTimelineTimeAxis.label(interval.lower, range: interval))"
            + " to \(WorkTimelineTimeAxis.label(interval.upper, range: interval))"
    }

    /// Whether the visible window is narrower than everything loaded — the
    /// predicate behind both the heading's count line and the reveal control.
    private var showsPartialWindow: Bool {
        guard let interval, let full = displayProjection.interval else { return false }
        return interval.lower > full.lower || interval.upper < full.upper
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            heading
            filters
            if !loadingInitialSnapshot, incomplete || timelineError != nil {
                Text(timelineError == nil ? "Activity history is incomplete" : "Activity refresh failed · showing retained records")
                    .workFont(.caption).foregroundStyle(Theme.amber)
            }
            if restoredPositionFromCurrentEvidence {
                // Plain words for what happened, pointing at the control that
                // resolves it (the Live button in this heading) rather than
                // naming an internal snapshot the reader never saw (K102).
                Text("Showing where you left off. Newer records may exist; choose Live to catch up.")
                    .workFont(.caption).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
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
        .onChange(of: selectionActive, initial: true) { _, active in
            layers?.dismissInnermost = active ? { dismissSelection() } : nil
        }
        // The Checks table above asked for one recorded event. The two
        // surfaces then agree about what is being looked at.
        .onChange(of: layers?.selectRequest ?? 0) { _, _ in
            guard let event = layers?.selectEventID else { return }
            inspectEvent(event)
        }
        .onAppear { viewIsVisible = true }
        .onDisappear {
            viewIsVisible = false
            focusGeneration += 1
            preferenceSaveTask?.cancel()
            layers?.dismissInnermost = nil
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
            if SnapshotMode.enabled && !SnapshotMode.interactiveFixture {
                // The offscreen renderer draws an AppKit-backed observer as a
                // full-surface placeholder (visible behind the empty state);
                // a static render has no scrolling to observe.
                recordsSurface.id("work.timeline.records")
            } else {
                recordsSurface.id("work.timeline.records")
                    .background(WorkTimelineScrollObserver(viewport: viewport) { hold() })
            }
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
                .disabled(zoomHereUnavailableReason != nil)
                .help(zoomHereUnavailableReason ?? "Resize the visible window to this group's time range")
                IconButton(systemName: "xmark", label: "Close record group",
                           identifier: "work.timeline.cluster.close") { dismissSelection() }
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(clusterMembers) { record in
                        Button {
                            inspect(record)
                        } label: {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(record.displayTitle).workFont(.rowLabel)
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
        .background(Theme.well, in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Theme.cardLine))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("work.timeline.cluster.chooser")
        .accessibilityLabel("\(clusterMembers.count) records in a time group")
        .background(WorkTimelineRevealProbe { if revealArmed { revealArmed = false; onRevealInspector?() } })
        .onKeyPress(.escape) { dismissSelection(); return .handled }
    }

    /// Why "Zoom here" is disabled, derived from the same predicate (C108).
    private var zoomHereUnavailableReason: String? {
        guard let bounds = clusterBounds else { return "This group's time range is unavailable" }
        if bounds.span <= 1 { return "This group spans under a second" }
        if bounds.span >= (interval?.span ?? 0) * 0.9 { return "This group already fills the visible window" }
        return nil
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
        // A user asked for this group, so the page may scroll to show it.
        revealArmed = true
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

    /// Whether the ordered list, rather than the time canvas, is the reading
    /// surface. Resolution order, widest-scope last: this task's own override,
    /// then the reviewer's app-wide preference (written on every explicit
    /// press, so choosing the canvas once does not have to be repeated on
    /// every task forever), and only then the data.
    private var usesRecordList: Bool {
        Self.usesRecordList(chosenForTask: navigation.view.recordListChosen,
                            appDefault: WorkTimelineSurfaceDefault.choice(storedSurfaceDefault),
                            records: displayProjection.records)
    }

    /// The resolution order itself, away from the view body so a test can pin
    /// it: per-task override, then app-wide default, then the data.
    static func usesRecordList(chosenForTask: Bool?, appDefault: Bool?,
                               records: [WorkTimelineRecord]) -> Bool {
        if let chosenForTask { return chosenForTask }
        if let appDefault { return appDefault }
        return listSuitsData(records: records)
    }

    /// The opening-view rule, kept testable and away from the view body.
    ///
    /// A list ORDERS; a canvas POSITIONS. So the only question that separates
    /// the two surfaces is whether the time axis distinguishes these records
    /// at all — never how many there are. Three records at 8:13:12/:14/:17
    /// have a shape (bunched, then a pause) that ordering destroys, and twenty
    /// records sharing one stamp are a list at any N. A record is drawn at its
    /// recorded start, so those starts are the stamps that decide it: with one
    /// distinct stamp or none, or a span that is not a positive finite number,
    /// position carries nothing and the list is right.
    static func listSuitsData(records: [WorkTimelineRecord]) -> Bool {
        guard !records.isEmpty else { return false }
        let stamps = Set(records.compactMap { WorkTimelineProjection.validTime($0.start) })
        guard stamps.count > 1, let lower = stamps.min(), let upper = stamps.max() else { return true }
        let span = upper - lower
        guard span.isFinite, span > 0 else { return true }
        return false
    }

    /// The presentation switch. Both surfaces read the same records, the same
    /// filters and the same window, so moving between them loses nothing —
    /// which is why this deliberately does NOT call `hold()`. Changing how
    /// records are DRAWN is not an investigation: the window, the filters and
    /// live follow all survive the press.
    private var surfacePicker: some View {
        Button {
            let wantsList = !usesRecordList
            navigation.view.recordListChosen = wantsList
            storedSurfaceDefault = WorkTimelineSurfaceDefault.stored(wantsList)
        } label: {
            Label(usesRecordList ? "Show timeline" : "Show list",
                  systemImage: usesRecordList ? "chart.bar.doc.horizontal" : "list.bullet")
        }
        .buttonStyle(QuietButtonStyle(horizontalPadding: 8, resting: true))
        .accessibilityIdentifier("work.timeline.surface")
        .help("Switch between the ordered record list and the time canvas. Both show the same records and honour the same filters.")
    }

    @ViewBuilder private var recordsSurface: some View {
        if hasActiveFilters {
            let outside = WorkTimelineFilterReveal.outside(matchingRecords, window: interval)
            if let cue = WorkTimelineFilterReveal.cueText(outside, failuresOnly: navigation.view.failuresOnly),
               let newest = outside.newest {
                HStack(spacing: Space.s) {
                    Text(cue).workFont(.caption).foregroundStyle(Theme.muted)
                    Button("Show") { moveWindow(to: newest) }
                        .buttonStyle(QuietButtonStyle(horizontalPadding: 8))
                        .accessibilityLabel("Show \(cue)")
                        .accessibilityIdentifier("work.timeline.outside-matches")
                    Spacer(minLength: 0)
                }
            }
        }
        if let full = displayProjection.interval, let interval, usesRecordList {
            // The canvas is DEMOTED to its navigation strip: the overview keeps
            // zoom and panning over the same window, and the ordered records
            // below it are the reading surface.
            WorkTimeWindowScroller(records: matchingRecords, full: full, window: interval,
                domain: WorkTimeCanvasLayout.expandedDomain(full, by: 0),
                onWindow: { value in hold(); navigation.view.interval = value })
                .frame(height: 32)
            WorkTimelineRecordList(records: filtered, selectedID: navigation.view.selectedID,
                range: full,
                onSelect: { record in
                    clusterMembers = []
                    clusterBounds = nil
                    inspect(record)
                },
                onFocusEnter: { onRevealRecords?() })
        } else if let full = displayProjection.interval, let interval {
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
                onHold: hold, focusRecordID: requestedRowFocus, focusRequest: scrollRequest,
                onFocusEnter: { onRevealRecords?() },
                compact: compactViewport,
                showsLaneCaptions: Set(displayProjection.records.map(\.laneID)).count > 1)
        } else {
            Text(displayProjection.records.isEmpty ? "No activity recorded yet." : "Recorded times are unavailable.")
                .workFont(.body).foregroundStyle(Theme.muted)
                .frame(maxWidth: .infinity, minHeight: 180)
        }
        let undated = matchingRecords.filter { $0.start == nil }
        if !undated.isEmpty {
            SnapshotSafeLabelMenu(
                title: "Time unavailable · \(undated.count)",
                systemName: "clock.badge.questionmark",
                help: "These records have no usable timestamp and cannot be placed on the timeline.",
                identifier: "work.timeline.undated"
            ) {
                ForEach(undated) { record in
                    Button("\(record.displayTitle) · \(record.laneTitle) · \(record.resultLabel)") {
                        clusterMembers = []
                        clusterBounds = nil
                        inspect(record, focusInspector: false)
                    }.buttonStyle(QuietButtonStyle())
                }
            }
        }
    }

    private var loadKey: String { receipt.taskId }

    private var heading: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 12) { headingLabel; Spacer(minLength: 0); surfacePicker; liveControls; activityMenu }
            VStack(alignment: .leading, spacing: 8) {
                headingLabel
                HStack(spacing: 8) { Spacer(minLength: 0); surfacePicker; liveControls; activityMenu }
            }
        }
    }
    private var headingLabel: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 4) {
            // A static heading is NOT a keyboard stop: `.focusable()` here put
            // Tab on a heading that sits under the toolbar, with no ring and
            // nothing to activate (K115). It stays a programmatic and
            // VoiceOver landing target through accessibilityFocused, which is
            // what the return paths below actually use.
            Text("Activity").workFont(.titleCard)
                .id("work.timeline.heading")
                .accessibilityFocused($accessibleEvidence, equals: "timeline-heading")
                .accessibilityAddTraits(.isHeader)
                ContextHelp(title: "About activity",
                    message: "Pinch or Option-scroll to change the visible time span; scroll sideways or drag the canvas to move through time. Plain vertical scrolling moves the page. Drag the overview window to move it and its edges to resize it. Select a record to read its details below the timeline; dense groups list their members there. Stems mark recorded times, not causal links. Section spans end at the latest reported update, not a measured execution finish. Check markers are points. Loaded history refreshes every 3 seconds while this view is open; search covers loaded records.",
                    summary: "How to navigate Activity",
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
            // ALWAYS the denominator, not only while a filter is on: the
            // canvas shows a slice, and a slice with no count and no span is
            // indistinguishable from the whole task (B3).
            windowScopeLine
        }
    }

    private var windowScopeLine: some View {
        HStack(spacing: Space.s) {
            Text("\(filtered.count) of \(displayProjection.records.count) loaded records · \(windowSpanText)")
                .workFont(.caption).foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("work.timeline.window-scope")
            // A count of "20 records" hides the fact that some of them are
            // narration rather than steps. The reducer ships the definition
            // beside the count for exactly this reason (F2); it is offered
            // here, on the count itself, rather than restated in Swift.
            if (receipt.timeline?.beatCount ?? 0) > 0,
               let definition = PayloadAbsence.text(receipt.timeline?.beatDefinition) {
                ContextHelp(title: "About progress notes", message: definition,
                            summary: "What a progress note is",
                            identifier: "work.timeline.beats.help")
            }
            if showsPartialWindow {
                // The one control that answers the count — in the heading
                // beside it, not behind the ellipsis menu.
                // The control that UNDOES a narrowed window earns a resting
                // affordance for the same reason the surface switch does: with
                // no chrome it reads as part of the count's prose.
                Button("Show all time") { showAllTime() }
                    .buttonStyle(QuietButtonStyle(horizontalPadding: 8, resting: true))
                    .accessibilityIdentifier("work.timeline.show-all-time")
                    .help("Widen the visible window to every loaded record")
            }
        }
    }

    private func showAllTime() {
        hold()
        navigation.view.interval = displayProjection.interval
    }

    private var activityMenu: some View {
        SnapshotSafeMenu(
            systemName: "ellipsis",
            label: "Activity actions",
            identifier: "work.timeline.actions"
        ) {
            Button("Export visible records…", action: exportReview)
                .disabled(filtered.isEmpty)
                .buttonStyle(QuietButtonStyle())
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
                snapshotAt: observation.lastObserved,
                failuresOnly: navigation.view.failuresOnly, interval: interval,
                offlineReceiptAt: dashboard.receiptSavedAt,
                outcome: receipt.axes.decisionStatus.key,
                handoff: receipt.axes.handoff?.markerLine)
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
                restoredPositionFromCurrentEvidence = false
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
            if failureCount == 0, !navigation.view.failuresOnly,
               let notRun = PayloadAbsence.text(receipt.axes.evidenceStrength.checksNotRunText) {
                // Checks that could not run are a named gap beside the record
                // search — never counted or offered as failures.
                Label(notRun, systemImage: CheckResultTone.notRun.symbol)
                    .workFont(.caption).foregroundStyle(Theme.muted)
                    .accessibilityIdentifier("work.timeline.not-run")
            }
            if failureCount > 0 || navigation.view.failuresOnly || hasActiveFilters {
                layout {
                    if failureCount > 0 || navigation.view.failuresOnly {
                        Button(navigation.view.failuresOnly ? "Show all records"
                               : WorkTimelineFilterReveal.failuresButtonTitle(
                                   tallyFailed: receipt.axes.evidenceStrength.checksFailed,
                                   currentFailureRecords: failureCount)) {
                            hold(); navigation.view.failuresOnly.toggle()
                            if navigation.view.failuresOnly { revealMatchesIfWindowEmpty() }
                        }.buttonStyle(QuietButtonStyle(horizontalPadding: 8))
                        .accessibilityIdentifier("work.timeline.failures")
                        .help("Show only failed checks that still need attention; one check may appear in more than one source.")
                    }
                    if hasActiveFilters {
                        // The count itself now lives in the heading, printed
                        // for every window rather than only a filtered one.
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
        // The shared app-chrome field (K15); snapshots keep its chrome and
        // swap only the inner field for text.
        AppTextField(
            placeholder: "Search activity",
            text: Binding(
                get: { navigation.view.query },
                set: { query in
                    // AppKit writes the unchanged value back when the field
                    // becomes first responder, and holding on that alone
                    // stopped Live follow the moment focus landed here — a
                    // mode change nobody asked for (K78). Only an actual edit
                    // is an investigation.
                    guard query != navigation.view.query else { return }
                    hold()
                    navigation.view.query = query
                }
            ),
            systemImage: "magnifyingglass",
            font: .body,
            focus: $searchFocused,
            accessibilityLabel: "Search loaded activity, sessions and files",
            accessibilityIdentifier: "work.timeline.search"
        )
        .frame(minWidth: 240, maxWidth: .infinity)
        // ⌘F reaches the record's own search too, so the command is never
        // dead while a search field is on screen (K114).
        .focusedSceneValue(\.focusSearch, FocusSearchAction { searchFocused = true })
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
                        Text(record.displayTitle).workFont(.titleCard)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityFocused($accessibleEvidence, equals: "inspector")
                            .accessibilityAddTraits(.isHeader)
                        if record.kind == .step, let gradeLabel = record.evidenceGradeLabel {
                            // The reducer's tier word and its reason answer
                            // "is this the agent's own claim?" (C04).
                            TierBadge(grade: record.evidenceGrade, text: gradeLabel)
                            if let reason = record.evidenceGradeReason {
                                Text(reason).workFont(.body)
                                    .fixedSize(horizontal: false, vertical: true)
                                    .textSelection(.enabled)
                            }
                        }
                        HStack(spacing: 6) {
                            Text(record.resultLabel)
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
                        if record.kind == .check, let revision = record.revisionLabel {
                            Text(revision).workFont(.caption).foregroundStyle(Theme.muted)
                                .fixedSize(horizontal: false, vertical: true)
                                .textSelection(.enabled)
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                    IconButton(systemName: "xmark", label: "Close record details",
                               identifier: "work.timeline.inspector.close") { dismissInspector(record) }
                        .focused($focusedEvidence, equals: "inspector")
                        .onKeyPress(.escape) { dismissInspector(record); return .handled }
                }
                Divider().overlay(Theme.hairline)
                // WHY this record is marked in the list and on the canvas, in
                // the reducer's own sentence. The leading rule is the visual
                // half of the same fact; without the sentence the mark is a
                // riddle (F3).
                if record.isSalient, let reason = record.salienceReason {
                    HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                        Rectangle().fill(Theme.rule).frame(width: 3, height: 14)
                            .accessibilityHidden(true)
                        Text(reason).workFont(FieldFont.qualifier).foregroundStyle(Theme.muted)
                            .fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityIdentifier("work.timeline.inspector.salience")
                }
                if let summary = record.summary, summary != record.title {
                    Text(summary).workFont(.body).textSelection(.enabled)
                }
                // What a progress note IS, in the reducer's words — so a
                // reviewer reading a beat's prose beside a step's cannot take
                // it for a step of its own (F2). Only the receipt carries the
                // definition; the paged timeline route does not, and an
                // absence here is simply no line.
                if record.isBeat, let definition = PayloadAbsence.text(receipt.timeline?.beatDefinition) {
                    Text(definition).workFont(FieldFont.qualifier).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: Metrics.readingMeasure, alignment: .leading)
                        .accessibilityIdentifier("work.timeline.inspector.beat-definition")
                }
                if let warning = record.timeWarning { Text(warning).workFont(.caption).foregroundStyle(Theme.amber) }
                if let disposition = record.disposition {
                    Text("Human disposition: \(disposition). The recorded check result is unchanged.").workFont(.caption)
                }
                if let resolution = record.resolutionDescription { Text(resolution).workFont(.caption).textSelection(.enabled) }
                if let code = record.exitCode { Text("Exit code: \(code)").workFont(.caption) }
                // The reducer's named result/exit-code disagreement, directly
                // under the exit code it disagrees with. It reached the model
                // and had no render site on any surface at all.
                if let note = record.noteText {
                    Label(note, systemImage: "exclamationmark.triangle")
                        .workFont(.caption).foregroundStyle(Theme.amber)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                        .accessibilityIdentifier("work.timeline.inspector.note")
                }
                // The step's recorded continuation point, verbatim, under its
                // own label — the one line that says what happens next.
                if let next = record.nextStep {
                    VStack(alignment: .leading, spacing: 3) {
                        CapsLabel(text: "Next step")
                        Text(next).workFont(.body).textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityIdentifier("work.timeline.inspector.next-step")
                }
                if !filtered.contains(where: { $0.id == record.id }) {
                    Text("Outside the current filters").workFont(.caption).foregroundStyle(Theme.amber)
                    Button("Show this record") { clearFilters(); focusSelectedRange(); returnToRecord(record) }
                        .buttonStyle(QuietButtonStyle(horizontalPadding: 8))
                }
                let associatedChecks = displayProjection.records.filter { $0.sectionRecordIDs.contains(record.id) || $0.sectionRecordID == record.id }
                if !associatedChecks.isEmpty {
                    DisclosureGroup("Checks · \(associatedChecks.count)") {
                        ForEach(associatedChecks) { check in
                            Button(([check.displayTitle, check.resultLabel, check.source]
                                    + [check.revisionLabel].compactMap { $0 }).joined(separator: " · ")) { inspect(check) }
                                .buttonStyle(QuietButtonStyle(horizontalPadding: 8))
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    .disclosureGroupStyle(FullRowDisclosureStyle())
                    .workFont(.caption)
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
                if let earlier = record.supersedesCheckEventID {
                    if displayProjection.records.contains(where: { $0.eventID == earlier }) {
                        Button("View earlier result") { inspectEvent(earlier) }
                            .buttonStyle(QuietButtonStyle(horizontalPadding: 8))
                    } else {
                        Text("The earlier result is not in loaded activity.").workFont(.caption).foregroundStyle(Theme.muted)
                    }
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
                                    IconButton(systemName: "line.3.horizontal.decrease.circle",
                                               label: "Find loaded activity referencing \(file)",
                                               help: "Find loaded activity referencing this file") {
                                        hold(); clearSelection(); navigation.showFile(file)
                                        revealMatchesIfWindowEmpty()
                                    }
                                }
                            }
                            DisclosureGroup("About file references") {
                                Text("These paths are recorded associations. File contents and diffs are not captured here. Filtering by a file temporarily replaces the other filters; Back to previous filters restores them.")
                                    .workFont(.caption).foregroundStyle(Theme.muted)
                                    .fixedSize(horizontal: false, vertical: true)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                            .disclosureGroupStyle(FullRowDisclosureStyle())
                        }.padding(.top, 6)
                    }
                    .disclosureGroupStyle(FullRowDisclosureStyle())
                    .workFont(.caption)
                }
                if !record.artifactDescriptions.isEmpty {
                    DisclosureGroup("Artifacts") {
                        ForEach(record.artifactDescriptions, id: \.self) {
                            Text($0).textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    .disclosureGroupStyle(FullRowDisclosureStyle())
                    .workFont(.caption)
                }
                DisclosureGroup("Record details") {
                    // A label/value grid on the panel's own leading edge, like
                    // the record page's fact rows. The old centered VStack read
                    // as a detached floating column (K101).
                    Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: Space.m, verticalSpacing: 6) {
                        if let note = record.identityNote { detailNote(note, muted: true) }
                        // The lane this record's canvas position states, in the
                        // reducer's own words, and the step kind behind the
                        // card's eyebrow.
                        if let lane = record.laneLabel { detailRow("Lane", lane) }
                        if let kind = record.sectionKind { detailRow("Kind", kind) }
                        detailRow("Scope", record.scope ?? "unavailable")
                        detailNote(record.timeNote)
                        detailRow("Session", record.laneTitle)
                        detailNote(evidenceIdentity(record))
                        detailNote(record.lineage)
                        detailRow("Event", record.eventID ?? "not supplied")
                        // The local clock the header and axis show, to the
                        // second, then the exact source value labelled UTC —
                        // the two no longer disagree on the date (K101).
                        if let start = record.start {
                            detailRow("Source time",
                                "\(WorkTimelineTimeAxis.spokenLabel(start)) · source \(WorkTimelineTimeAxis.preciseLabel(start))")
                        }
                        if let end = record.end {
                            detailRow("Latest update",
                                "\(WorkTimelineTimeAxis.spokenLabel(end)) · source \(WorkTimelineTimeAxis.preciseLabel(end))")
                        }
                        if let supersededBy = record.supersededBy { detailRow("Superseded by event", supersededBy) }
                        if let commandState = record.commandStateText { detailNote(commandState) }
                    }
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.top, 6)
                }
                .disclosureGroupStyle(FullRowDisclosureStyle())
                .workFont(.caption)
                .accessibilityIdentifier("work.timeline.inspector.identity")
            }
            .id(record.id)
            .padding(Space.m).frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.well, in: RoundedRectangle(cornerRadius: Metrics.radius))
            .id("work.timeline.inspector")
            .background(WorkTimelineRevealProbe { if revealArmed { revealArmed = false; onRevealInspector?() } })
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

    /// One labelled identity fact: its caps label and its value, aligned on
    /// the same baseline in the details grid.
    private func detailRow(_ label: String, _ value: String) -> some View {
        GridRow {
            CapsLabel(text: label)
                .gridColumnAlignment(.leading)
            Text(value)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// A recorded sentence that carries its own subject (a time note, a
    /// lineage) spans both columns rather than inventing a label for it.
    private func detailNote(_ text: String, muted: Bool = false) -> some View {
        GridRow {
            Text(text)
                .foregroundStyle(muted ? Theme.muted : Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .gridCellColumns(2)
        }
    }

    private func dismissInspector(_ record: WorkTimelineRecord) {
        clearSelection()
        requestedRowFocus = record.id
        scrollRequest += 1
    }

    /// `revealsRegion` is the page's permission to scroll the DOCUMENT so the
    /// selection region comes into view. A click inside this surface grants it
    /// — the reviewer is looking here. A request from another part of the
    /// record page does not: see `inspectEvent` (F9).
    private func inspect(_ record: WorkTimelineRecord, focusInspector: Bool = true,
                         revealsRegion: Bool = true) {
        hold()
        // A user asked for these details, so the page may scroll to show them.
        revealArmed = revealsRegion
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
                // The heading takes VoiceOver focus only; keyboard focus stays
                // where the reviewer left it rather than on static text (K115).
                accessibleEvidence = "timeline-heading"
            }
        }
    }
    private func restoreReturnFocusIfReady() {
        guard !loadingInitialSnapshot,
              let target = appSelection.workReturnFocus.consume(taskID: receipt.taskId, visibleRecordIDs: Set(filtered.map(\.id))) else { return }
        switch target {
        case .record(let id):
            hold()
            // A deliberate return to a remembered record may reveal it.
            revealArmed = true
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
                // The heading takes VoiceOver focus only; keyboard focus stays
                // where the reviewer left it rather than on static text (K115).
                accessibleEvidence = "timeline-heading"
            }
        }
    }

    /// A discrete filter change (failures toggle, file filter) that leaves the
    /// visible window without a match moves to the newest match and stops
    /// following. Search keystrokes never move the window (C12).
    private func revealMatchesIfWindowEmpty() {
        guard let range = WorkTimelineFilterReveal.interval(for: matchingRecords, window: interval) else { return }
        hold()
        navigation.view.interval = range
    }

    private func moveWindow(to record: WorkTimelineRecord) {
        guard let range = WorkTimelineRangeNavigation.focused(on: record) else { return }
        hold()
        navigation.view.interval = range
    }

    private func focusSelectedRange() {
        guard let selected, let range = WorkTimelineRangeNavigation.focused(on: selected) else { return }
        hold()
        navigation.view.interval = range
        scrollTarget = selected.id
        scrollRequest += 1
    }
    /// A selection asked for by the Checks table above. The reviewer is reading
    /// the row they pressed — several screens up — so this path re-frames the
    /// canvas/list INTERNALLY and leaves the document exactly where it is.
    ///
    /// It used to take the ordinary `inspect` route, which arms the reveal
    /// probe, and the probe then scrolled the whole record page down to the
    /// Activity region: one press threw the document ~2,600 pt PAST the detail
    /// it had just expanded, and the expanded row was never seen (F9).
    private func inspectEvent(_ eventID: String) {
        guard let record = displayProjection.records.first(where: { $0.eventID == eventID }) else { return }
        if let window = interval,
           let reframed = WorkTimelineRangeNavigation.reframed(window, toShow: record) {
            navigation.view.interval = reframed
        }
        // No inspector focus either: moving the VoiceOver cursor to a region
        // three screens away is the same displacement by another route.
        inspect(record, focusInspector: false, revealsRegion: false)
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
        // Any window or filter move leaves the restored position behind, so
        // the note describing it stops applying (K102).
        restoredPositionFromCurrentEvidence = false
    }
    private func trailingWindow(_ full: WorkTimelineInterval?, span: Double?) -> WorkTimelineInterval? {
        guard let full else { return nil }
        // Following keeps its anchor on the newest activity — the point a
        // reviewer watching live wants at the right edge. It is an APP-chosen
        // window, though, so it goes through the same populated() guarantee as
        // the opening one: when the newest record is a long section, the 30
        // minutes after its last update hold only its tail line and no card at
        // all, and the canvas then opened on its named-empty state (B3).
        let window = WorkTimeCanvasLayout.latestWindow(within: full,
            latest: displayProjection.newestRecord?.latestTime, span: span ?? 30 * 60)
        return populated(window, within: full)
    }
    private func receive(_ projection: WorkTimelineProjection) {
        feed.ingest(projection, following: navigation.following || loadingInitialSnapshot)
        if navigation.following {
            navigation.view.interval = trailingWindow(displayProjection.interval, span: followWindowSpan)
        }
    }
    private func saveMemory() {
        guard let activeTaskID, !dashboard.isOfflineSnapshot, !SnapshotMode.enabled else { return }
        WorkTimelineMemory.cache.save(.init(feed: feed), for: activeTaskID)
        WorkTimelinePreferences.save(navigation, taskID: activeTaskID)
    }

    @MainActor private func loadTask() async {
        let taskID = receipt.taskId
        saveMemory()
        activeTaskID = taskID
        revealArmed = false
        navigation = WorkTimelinePreferences.load(taskID: taskID)
        if appSelection.workEntry == .navigate {
            // Opening a record shows the whole record. A selection saved on an
            // earlier visit would otherwise re-open its inspector and pull the
            // page down to it (K104). The saved window and filters stay — both
            // are named on screen, and an inspector is not.
            navigation.view.selectedID = nil
        }
        followWindowSpan = max(navigation.view.interval?.span ?? 0, 30 * 60)
        let cached = dashboard.isOfflineSnapshot || SnapshotMode.enabled ? nil : WorkTimelineMemory.cache.load(taskID)
        feed = cached?.feed ?? WorkTimelineFeed()
        timeline = nil
        restoredPositionFromCurrentEvidence = cached == nil && navigation.restorePositionWithoutSnapshot()
        if dashboard.isOfflineSnapshot { navigation.following = false }
        loadingInitialSnapshot = cached == nil && !SnapshotMode.enabled && !dashboard.isOfflineSnapshot
        timelineError = nil
        observation.lastObserved = nil
        showingArrivals = navigation.history != nil
        scrollTarget = navigation.view.anchorID
        if dashboard.isOfflineSnapshot {
            timeline = try? await dashboard.loadTimeline(taskID: taskID, previous: nil)
            guard !Task.isCancelled, activeTaskID == taskID else { return }
        }
        if cached == nil { receive(projection) }
        // A window RESTORED from an earlier visit can land in a gap that this
        // snapshot no longer fills. Opening on a named-empty canvas is not a
        // reading position worth keeping, so repair it once, here, where it is
        // the app's choice rather than a reviewer's pan.
        if let saved = navigation.view.interval, let full = displayProjection.interval {
            let repaired = populated(saved, within: full)
            if repaired != saved { navigation.view.interval = repaired }
        }
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
                // Writing SwiftUI state invalidates the view whether or not the
                // value moved, so a three-second poll that re-stamped these on
                // every tick rebuilt the whole Activity surface — canvas,
                // record list and inspector — to show what it already showed.
                // The observation stamp is not state at all: only the export
                // reads it, never `body`.
                if timelineError != nil { timelineError = nil }
                observation.lastObserved = Date()
            } catch {
                guard !Task.isCancelled, activeTaskID == taskID else { return }
                timelineError = error.localizedDescription
            }
            if loadingInitialSnapshot { loadingInitialSnapshot = false }
            // Missing/filtered evidence focuses the heading, never another row.
            restoreReturnFocusIfReady()
            do { try await Task.sleep(for: .seconds(3)) } catch { return }
        }
    }

    private func color(_ record: WorkTimelineRecord) -> Color { record.presentationTint }
    private func symbol(_ record: WorkTimelineRecord) -> String {
        if record.superseded { return "clock.arrow.circlepath" }
        if record.kind == .step { return "text.alignleft" }
        switch record.checkTone {
        case .failure: return "xmark.circle"
        case .pass: return "checkmark.circle"
        case .notRun: return CheckResultTone.notRun.symbol
        }
    }
    /// `Sep 14, 10:50 PM`: the shared date and locale clock helpers (C55).
    private static func dateText(_ time: Double) -> String {
        let date = Date(timeIntervalSince1970: time)
        return "\(Fmt.displayDate(date)), \(Fmt.clockTime(date))"
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
                // Plain vertical scrolling moves the page and must not pause
                // Live; only input the canvas interprets holds the timeline.
                let dx = Double(event.scrollingDeltaX), dy = Double(event.scrollingDeltaY)
                let precise = event.hasPreciseScrollingDeltas, modifiers = event.modifierFlags
                guard WorkTimeCanvasInputIntent.zoomScroll(deltaX: dx, deltaY: dy, precise: precise, modifiers: modifiers) != nil
                        || WorkTimeCanvasInputIntent.panScroll(deltaX: dx, deltaY: dy, precise: precise, modifiers: modifiers) != nil
                else { return event }
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
