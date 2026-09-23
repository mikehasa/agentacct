import SwiftUI
import AppKit

// The Work surface — one receipts collection in three adaptive presentations:
//
// * No Task selected → the task collection.
// * A Task selected at a wide width → a resizable master list and the record.
// * A Task selected at a compact width or accessibility text size → the record
//   is pushed over the collection, with an explicit route back.
//
// A Task is the converged unit (root session + continuations + subagents).
// Honesty rides the payload: decision words come from the daemon, evidence
// tiers keep their pip shapes, and absent facts are named, never zeroed.

enum WorkSessionResolution: Equatable {
    case task(String)
    case unresolved(String)
    case pending(String)
}

enum WorkRecordPhaseKey: Hashable {
    case loading(taskId: String)
    case loaded(taskId: String)
    case failed(taskId: String)
    case unresolved(sessionId: String)
    case empty
}

func workRecordPhaseKey(
    selectedTaskId: String?,
    sessionId: String?,
    unresolvedSessionId: String?,
    receiptTaskId: String?,
    errorPresent: Bool
) -> WorkRecordPhaseKey {
    if let selectedTaskId {
        if receiptTaskId == selectedTaskId { return .loaded(taskId: selectedTaskId) }
        if errorPresent { return .failed(taskId: selectedTaskId) }
        return .loading(taskId: selectedTaskId)
    }
    if let sessionId, sessionId == unresolvedSessionId {
        return .unresolved(sessionId: sessionId)
    }
    return .empty
}

func workSessionResolution(
    for sessionId: String,
    in tasks: [ReceiptSummary],
    projection: WorkProjectionMetadata? = nil
) -> WorkSessionResolution {
    if let match = tasks.first(where: { $0.primaryRoot?.sessionKey == sessionId }) {
        return .task(match.taskId)
    }
    if projection?.needsRefresh == true { return .pending(sessionId) }
    return .unresolved(sessionId)
}

/// Lifecycle filter groups for the table tabs. A FILTER grouping only — rows
/// always wear the daemon's own decision word; keys outside every group land
/// in "Other" so the tab counts always sum to All (no receipt is hidden).
enum WorkGroup: String, CaseIterable, Identifiable {
    case attention = "Attention"
    case verified = "Verified"
    case reported = "Reported"
    case inProgress = "In progress"
    case observed = "Observed"
    case stopped = "Stopped"
    case other = "Other"

    var id: String { rawValue }

    /// A contextual filter, not a queue of actions assigned to the user.
    var label: String { self == .attention ? "With issues" : rawValue }

    /// Buckets never upgrade a claim: "Verified" holds only the machine-
    /// asserted key; agent claims of done-ness group under their own word
    /// ("Reported"); ambient activity stays "Observed". "Stopped" holds the
    /// stop shapes — the deliberate handoff, the inferred ended-open, and the
    /// inferred inactive (open, nothing finished, work moved on elsewhere) —
    /// each row still wearing its own decision word.
    static func forKey(_ key: String?) -> WorkGroup {
        switch key {
        case "finding", "failed", "blocked": return .attention
        case "verified": return .verified
        case "reported", "resolved", "mostly_done", "finding_superseded",
             "finding_resolved_by_user", "blocker_resolved_by_user":
            return .reported
        case "in_progress", "started", "checkpoint": return .inProgress
        case "observed": return .observed
        case "handed_off", "ended_open", "inactive": return .stopped
        default: return .other
        }
    }

    static func forTask(_ task: ReceiptSummary) -> WorkGroup {
        workReceiptNeedsAttention(
            decisionKey: task.decisionStatus.key,
            checksFailed: task.evidenceStrength.checksFailed
        ) ? .attention : forKey(task.decisionStatus.key)
    }
}

func workReceiptNeedsAttention(decisionKey: String?, checksFailed: Int?) -> Bool {
    let settledFindingKeys: Set<String> = [
        "finding_superseded", "finding_resolved_by_user",
    ]
    return ["finding", "failed", "blocked"].contains(decisionKey ?? "")
        || ((checksFailed ?? 0) > 0 && !settledFindingKeys.contains(decisionKey ?? ""))
}

/// The Work surface's shared sort modes. One `WorkBrowseState` drives both the
/// receipts table and compact master, so detail round-trips preserve order.
enum WorkSort: String, CaseIterable, Identifiable {
    case latest, attention, cost
    var id: String { rawValue }

    /// The order's name in the sort menu and on its trigger. Raw values stay
    /// stable identifiers; these are the words people read.
    var label: String {
        switch self {
        case .attention: return "Attention first"
        case .latest: return "Latest"
        case .cost: return "Highest cost"
        }
    }

    var footerText: String {
        switch self {
        case .attention: return "attention first, then recency"
        case .latest: return "most recent first"
        case .cost: return "highest estimated cost first"
        }
    }
}

/// Durable state for the Work collection. Selection can change the collection's
/// layout, but it must never destroy the user's query, grouping, order, or
/// one-shot return-focus request. AppSelection owns one instance for the
/// lifetime of the main window.
@MainActor
final class WorkBrowseState: ObservableObject {
    @Published var query = ""
    @Published var group: WorkGroup?
    @Published var sort: WorkSort = .latest
    @Published var pendingFocusRestorationTaskId: String?
    @Published var shouldFocusSearchOnReturn = false

    func visibleTasks(
        in tasks: [ReceiptSummary],
        attention: V1AttentionPayload? = nil
    ) -> [ReceiptSummary] {
        WorkTaskPresentation(
            tasks: tasks,
            attention: attention,
            group: group,
            query: query,
            sort: sort
        ).visibleTasks
    }

    func prepareReturnFocus(
        from taskId: String?,
        in tasks: [ReceiptSummary],
        attention: V1AttentionPayload? = nil
    ) {
        let visible = visibleTasks(in: tasks, attention: attention)
        if let taskId, visible.contains(where: { $0.taskId == taskId }) {
            pendingFocusRestorationTaskId = taskId
            shouldFocusSearchOnReturn = false
        } else {
            pendingFocusRestorationTaskId = nil
            shouldFocusSearchOnReturn = true
        }
    }
}

func visibleWorkReceipts(
    _ tasks: [ReceiptSummary],
    query: String,
    group: WorkGroup?,
    sort: WorkSort
) -> [ReceiptSummary] {
    var rows = tasks
    if let group {
        rows = rows.filter { WorkGroup.forTask($0) == group }
    }
    let needle = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    if !needle.isEmpty {
        rows = rows.filter {
            ($0.title ?? "").lowercased().contains(needle)
                || $0.taskId.lowercased().contains(needle)
                || ($0.primaryRoot?.client ?? "").lowercased().contains(needle)
                || ($0.project ?? "").lowercased().contains(needle)
        }
    }
    return sortedReceipts(rows, by: sort)
}

func workSelectionIsOutsideBrowse(
    taskId: String?,
    allTasks: [ReceiptSummary],
    visibleTasks: [ReceiptSummary]
) -> Bool {
    guard let taskId, allTasks.contains(where: { $0.taskId == taskId }) else { return false }
    return !visibleTasks.contains(where: { $0.taskId == taskId })
}

func workReceiptCollectionIsPartial(loaded: Int, total: Int?, truncated: Bool?) -> Bool {
    truncated == true || total.map { $0 > loaded } == true
}

func workBrowseCountText(
    visible: Int,
    loaded: Int,
    total: Int?,
    truncated: Bool?
) -> String {
    let loadedCount = visible == loaded ? "\(loaded) loaded" : "\(visible) of \(loaded) loaded"
    if workReceiptCollectionIsPartial(loaded: loaded, total: total, truncated: truncated) {
        guard let total else { return "\(loadedCount) · more may exist" }
        return "\(visible) of \(loaded) loaded · \(total) in store"
    }
    guard let total else { return "\(loadedCount) · total not reported" }
    if total < loaded {
        return "\(loadedCount) · \(total) total reported"
    }
    return "\(visible) of \(Fmt.count(total, "task"))"
}

func workReceiptRefreshError(
    selectedTaskId: String?,
    errorTaskId: String?,
    error: String?
) -> String? {
    guard selectedTaskId != nil, selectedTaskId == errorTaskId else { return nil }
    return error
}

enum WorkLayoutMode: Equatable {
    case table
    case split
    case pushDetail
}

func workLayoutMode(
    for availableWidth: CGFloat,
    dynamicTypeSize: DynamicTypeSize,
    hasSelection: Bool
) -> WorkLayoutMode {
    guard hasSelection else { return .table }
    if dynamicTypeSize.isAccessibilitySize || availableWidth < 1_080 {
        return .pushDetail
    }
    return .split
}

/// One semantic row contract for both the task collection and compact master.
/// When the selected receipt has richer/fresher detail, its evidence, check, and
/// cost values replace the compact summary so adjacent UI cannot disagree.
struct WorkReceiptRowPresentation {
    let taskId: String
    let title: String
    let decisionKey: String
    let decisionLabel: String
    let decisionHelp: String
    let evidence: ReceiptEvidence
    let handedOff: Bool
    let coverageText: String
    let coverageQualifier: String
    let coverageIsInconsistent: Bool
    let checkRunsText: String
    let checkRunsValue: String
    let checkRunsQualifier: String
    let checkRunsAreInconsistent: Bool
    let compactCheckRunsText: String
    let clientText: String
    let projectText: String?
    let costText: String
    let updatedText: String
    let updatedAccessibilityText: String
    let attentionReason: String?

    init(task: ReceiptSummary, detail: Receipt? = nil) {
        let selectedDetail = detail?.taskId == task.taskId ? detail : nil
        let decision = selectedDetail?.axes.decisionStatus ?? task.decisionStatus
        let resolvedEvidence = selectedDetail?.axes.evidenceStrength ?? task.evidenceStrength
        let detailChecks = selectedDetail?.dimensions.evidence
        let checksTotal = detailChecks?.checksTotal ?? resolvedEvidence.checksTotal
        let checksPassed = detailChecks?.checksPassed ?? resolvedEvidence.checksPassed
        let checksFailed = detailChecks?.checksFailed ?? resolvedEvidence.checksFailed

        taskId = task.taskId
        title = selectedDetail?.title ?? task.title ?? task.taskId
        decisionKey = decision.key
        decisionLabel = decision.label ?? decision.key
        // A resolved blocker is surfaced (so it can be reopened) but must not
        // colour the row or the badge tooltip as if the task still needs you.
        let blockerIsStanding = (decision.blocker?.disposition?.state ?? "open") != "resolved"
        decisionHelp = (blockerIsStanding ? decision.blocker?.text : nil) ?? decision.statement ?? ""
        evidence = resolvedEvidence
        handedOff = selectedDetail?.axes.handoff?.handedOff ?? (task.handedOff == true)
        let coveragePresentation = ReceiptCoveragePresentation(evidence: resolvedEvidence)
        coverageText = coveragePresentation.rowText
        coverageQualifier = coveragePresentation.qualifier
        coverageIsInconsistent = coveragePresentation.isInconsistent
        let checkRunsPresentation = ReceiptCheckRunsPresentation(
            total: checksTotal,
            passed: checksPassed,
            failed: checksFailed
        )
        checkRunsText = checkRunsPresentation.rowText
        checkRunsValue = checkRunsPresentation.value
        checkRunsQualifier = checkRunsPresentation.qualifier
        checkRunsAreInconsistent = checkRunsPresentation.isInconsistent
        compactCheckRunsText = checkRunsPresentation.headerText
        clientText = task.primaryRoot?.client ?? "unattributed"
        projectText = selectedDetail?.dimensions.task.boundary?.project ?? task.project
        if let cost = selectedDetail?.dimensions.cost.estimatedCostUsd {
            costText = receiptCostDisplay(
                cost,
                complete: selectedDetail?.dimensions.cost.costComplete,
                confidence: selectedDetail?.dimensions.cost.costConfidence
            )
        } else {
            let compact = DashboardWorkItem(task: task).cost
            costText = compact == "—" ? "cost unknown" : compact
        }
        updatedText = agoText(task.lastActivityAt) ?? "Activity time unavailable"
        updatedAccessibilityText = task.lastActivityAt == nil ? "Activity time unavailable" : "updated \(updatedText)"
        // Only a STANDING blocker states the (coral) attention reason; a resolved
        // one is surfaced for reopen but must not read as needing attention.
        if let blocker = decision.blocker?.text, !blocker.isEmpty, blockerIsStanding {
            attentionReason = blocker
        } else if let checksFailed, checksFailed > 0 {
            attentionReason = "\(checksFailed) failed check run\(checksFailed == 1 ? "" : "s")"
        } else if WorkGroup.forKey(decision.key) == .attention {
            attentionReason = decision.statement
        } else {
            attentionReason = nil
        }
    }

    var accessibilityLabel: String {
        var parts = [title, decisionLabel]
        if handedOff && decisionKey != "handed_off" { parts.append("handed off") }
        if let attentionReason, !attentionReason.isEmpty { parts.append(attentionReason) }
        parts.append(coverageText)
        parts.append(checkRunsText.replacingOccurrences(of: " · ", with: ", "))
        parts.append(clientText)
        if let projectText, !projectText.isEmpty { parts.append(projectText) }
        parts.append(costText)
        parts.append(updatedAccessibilityText)
        return parts.joined(separator: ". ")
    }

    var compactCoverageText: String {
        coverageText.replacingOccurrences(of: " claims", with: "")
    }

    var compactCostText: String {
        costText == "cost unknown" ? "unpriced" : costText
    }
}

/// The receipt's two evidence dimensions in decision language. Claim coverage
/// and recorded check runs are deliberately separate so `0/1 supported` can
/// never look like it contradicts `9/11 checks passed`.
struct WorkReceiptDecisionPresentation {
    let headline: String
    let explanation: String
    let coverageValue: String
    let coverageQualifier: String
    let checksValue: String
    let checksQualifier: String
    let isAttention: Bool

    init(receipt: Receipt) {
        let decision = receipt.axes.decisionStatus
        let coverage = receipt.axes.evidenceStrength
        let checks = receipt.dimensions.evidence
        isAttention = workReceiptNeedsAttention(
            decisionKey: decision.key,
            checksFailed: checks.checksFailed ?? coverage.checksFailed
        )
        headline = isAttention ? "Why this needs attention" : "Current outcome"
        let statement = decision.statement ?? "No decision explanation was recorded."
        if let assertedBy = assertedByLabel(decision.assertedBy) {
            explanation = "\(statement) — \(assertedBy)"
        } else {
            explanation = statement
        }

        let coveragePresentation = ReceiptCoveragePresentation(evidence: coverage)
        coverageValue = coveragePresentation.value
        coverageQualifier = coveragePresentation.qualifier

        let checksPresentation = ReceiptCheckRunsPresentation(
            total: checks.checksTotal,
            passed: checks.checksPassed,
            failed: checks.checksFailed
        )
        checksValue = checksPresentation.value
        checksQualifier = checksPresentation.qualifier
    }

    var accessibilityLabel: String {
        "\(headline). \(explanation). Coverage: \(coverageValue), \(coverageQualifier). "
            + "Checks: \(checksValue), \(checksQualifier)."
    }
}

struct WorkAttentionEmptyCopy: Equatable {
    let title: String
    let detail: String

    init(payload: V1AttentionPayload, query: String) {
        if payload.total == 0 {
            title = "No current review items"
            detail = "No failed checks, failed steps, or unresolved blockers are recorded."
        } else if !query.isEmpty, !payload.items.isEmpty {
            title = "No review items match this filter"
            detail = "The bounded queue has \(payload.items.count) of \(payload.total) review items; adjust the filter to inspect them."
        } else {
            title = "Review queue details unavailable"
            detail = "\(Fmt.count(payload.total, "review item")) recorded, but none were returned. Refresh before acting."
        }
    }
}

/// Attention pages arrive ranked by issue type. Latest sorts activity times
/// explicitly, retaining source order for ties and placing unknown times last.
func sortedReceipts(_ rows: [ReceiptSummary], by sort: WorkSort) -> [ReceiptSummary] {
    switch sort {
    case .attention:
        let recent = sortedReceipts(rows, by: .latest)
        let attention = recent.filter { WorkGroup.forTask($0) == .attention }
        return attention + recent.filter { WorkGroup.forTask($0) != .attention }
    case .latest:
        return rows.enumerated().sorted { left, right in
            let lhs = left.element.lastActivityAt.flatMap { $0.isFinite && $0 > 0 ? $0 : nil }
            let rhs = right.element.lastActivityAt.flatMap { $0.isFinite && $0 > 0 ? $0 : nil }
            switch (lhs, rhs) {
            case let (.some(lhs), .some(rhs)) where lhs != rhs: return lhs > rhs
            case (.some, .none): return true
            case (.none, .some): return false
            default: return left.offset < right.offset
            }
        }.map(\.element)
    case .cost:
        return rows.sorted { ($0.cost.estimatedCostUsd ?? -1) > ($1.cost.estimatedCostUsd ?? -1) }
    }
}

// MARK: - Decision-status legend

/// One-line human definitions for every decision word, mirroring the daemon's
/// own criteria (task_outcome/receipt statements). Presentation only — the
/// words and their meanings stay the daemon's; this never re-derives a status.
struct DecisionLegendEntry: Identifiable {
    let key: String
    let label: String
    let definition: String
    var id: String { key }
}

enum DecisionLegend {
    /// Grouped by family: needs-you, live, proven, claimed, inferred, ambient.
    static let entries: [DecisionLegendEntry] = [
        .init(key: "blocked", label: "Blocked",
              definition: "The agent recorded a blocker it never marked resolved."),
        .init(key: "failed", label: "Failed",
              definition: "The agent recorded a step as failed."),
        .init(key: "finding", label: "Finding",
              definition: "A machine check failed and no later run of it has passed."),
        .init(key: "in_progress", label: "In progress",
              definition: "Steps are still open and the session was not seen to end."),
        .init(key: "verified", label: "Verified",
              definition: "Recorded machine evidence verifies the latest outcome."),
        .init(key: "reported", label: "Reported",
              definition: "The agent says it finished; no check proves it."),
        .init(key: "resolved", label: "Resolved",
              definition: "A later passing check reports the blocker resolved — not a verified completion."),
        .init(key: "mostly_done", label: "Mostly done",
              definition: "Steps finished, some still open — later work moved elsewhere."),
        .init(key: "handed_off", label: "Handed off",
              definition: "The agent deliberately stopped and passed the work on."),
        .init(key: "blocker_resolved_by_user", label: "Blocker resolved",
              definition: "You marked the recorded blocker resolved — not a completion claim, not machine verification."),
        .init(key: "finding_resolved_by_user", label: "Finding resolved",
              definition: "You marked the finding resolved; the failing check stays in history — not machine verification."),
        .init(key: "finding_superseded", label: "Finding superseded",
              definition: "A check failed, but a later run of the same check passed."),
        .init(key: "ended_open", label: "Ended open",
              definition: "The session ended with steps still open; the stop is inferred, not stated."),
        .init(key: "inactive", label: "Inactive",
              definition: "Open with nothing finished; work has since continued elsewhere — agentacct inferred it went quiet, it never said done."),
        .init(key: "observed", label: "Observed",
              definition: "Activity was recorded; no outcome was ever stated."),
    ]
}

/// The Work collection's status filter. One name ("Status") everywhere it
/// appears; a chosen status takes the active wash because it narrows the list.
struct WorkStatusMenu: View {
    @Binding var group: WorkGroup?
    let identifier: String

    var body: some View {
        FilterMenu(
            title: "Status",
            systemImage: "line.3.horizontal.decrease",
            value: group?.label ?? "All statuses",
            isActive: group != nil,
            help: group.map { "Showing \($0.label.lowercased()) tasks" } ?? "Filter tasks by status",
            identifier: identifier,
            selection: $group
        ) {
            Text("All statuses").tag(nil as WorkGroup?)
            Divider()
            ForEach(WorkGroup.allCases) { candidate in
                Text(candidate.label).tag(Optional(candidate))
            }
        }
    }
}

/// The Work collection's order. Sorting never hides a task, so it stays
/// neutral whatever the choice.
struct WorkSortMenu: View {
    @Binding var sort: WorkSort
    let identifier: String
    var showsValue = true
    var minHeight: CGFloat = ButtonFeedback.minimumHitDimension

    var body: some View {
        FilterMenu(
            title: "Sort",
            heading: "Sort by",
            systemImage: "arrow.up.arrow.down",
            value: sort.label,
            showsValue: showsValue,
            minHeight: minHeight,
            help: "Sorted \(sort.footerText)",
            identifier: identifier,
            selection: $sort
        ) {
            ForEach(WorkSort.allCases) { Text($0.label).tag($0) }
        }
    }
}

/// A small info affordance that opens the decision-word legend. Lives beside
/// every surface that shows decision words (table controls, record title).
struct DecisionLegendButton: View {
    @State private var shown = false

    var body: some View {
        // Popovers need live interaction; the offscreen renderer draws the
        // trigger as noise, so snapshots omit the control entirely.
        if !SnapshotMode.enabled || SnapshotMode.interactiveFixture {
            Button {
                shown.toggle()
            } label: {
                Image(systemName: "info.circle")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(Theme.muted)
                    .frame(
                        width: ButtonFeedback.minimumHitDimension,
                        height: ButtonFeedback.minimumHitDimension
                    )
                    .contentShape(Rectangle())
            }
            .buttonStyle(QuietButtonStyle(
                tint: Theme.muted,
                horizontalPadding: 0,
                verticalPadding: 0
            ))
            .help("What each status word means")
            .accessibilityLabel("Status legend")
            .accessibilityIdentifier("work.status-legend")
            .popover(isPresented: $shown, arrowEdge: .bottom) {
                VStack(alignment: .leading, spacing: Space.s) {
                    CapsLabel(text: "Status words")
                    ForEach(DecisionLegend.entries) { entry in
                        HStack(alignment: .firstTextBaseline, spacing: Space.m) {
                            DecisionBadge(key: entry.key, label: entry.label, compact: true)
                                .frame(width: 132, alignment: .leading)
                            Text(entry.definition)
                                .workFont(.caption).foregroundStyle(Theme.ink)
                                .fixedSize(horizontal: false, vertical: true)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    DisclosureGroup("Evidence grades") {
                        Text("Externally verified: external evidence. Independently checked: an independent check. Self checked: the agent checked its own work. Unchecked or claimed: no supporting check. Not gradeable: no meaningful grade is available. Counts describe captured evidence, not the probability that a task is correct.")
                            .workFont(.caption).textSelection(.enabled)
                    }.workFont(.caption)
                }
                .padding(Space.l)
                .frame(width: 440)
            }
        }
    }
}

struct WorkPane: View {
    @Environment(DashboardStore.self) var dashboard
    @Environment(AppSelection.self) var selection
    @Environment(\.savedWorkReconnect) private var reconnectSavedWork
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @State private var unresolvedSessionId: String?
    @State private var timelineFocused = false

    init(timelineFocused: Bool = false) {
        _timelineFocused = State(initialValue: timelineFocused)
    }

    private var selectionKey: String {
        if let taskId = selection.taskId { return "task:\(taskId)" }
        if let sessionId = selection.sessionId { return "session:\(sessionId)" }
        return "list"
    }

    private var resolutionKey: String {
        // Session links must retry when the first usable collection arrives.
        guard selection.sessionId != nil else { return selectionKey }
        return selectionKey + ":" + (dashboard.receiptListProjection?.state ?? "legacy")
            + ":" + (dashboard.receiptListProjection?.generation ?? "")
    }

    private var phaseKey: WorkRecordPhaseKey {
        let refreshError = workReceiptRefreshError(
            selectedTaskId: selection.taskId,
            errorTaskId: dashboard.receiptErrorTaskId,
            error: dashboard.receiptError
        )
        return workRecordPhaseKey(
            selectedTaskId: selection.taskId,
            sessionId: selection.sessionId,
            unresolvedSessionId: unresolvedSessionId,
            receiptTaskId: dashboard.receipt?.taskId,
            errorPresent: refreshError != nil
        )
    }

    private var listTransition: AnyTransition {
        guard !reduceMotion else { return .opacity }
        return .asymmetric(
            insertion: .opacity.combined(with: .offset(x: -12)),
            removal: .opacity.combined(with: .offset(x: -12))
        )
    }

    private var detailTransition: AnyTransition {
        guard !reduceMotion else { return .opacity }
        return .asymmetric(
            insertion: .opacity.combined(with: .offset(x: 12)),
            removal: .opacity.combined(with: .offset(x: 12))
        )
    }

    var body: some View {
        GeometryReader { proxy in
            let mode = workLayoutMode(
                for: proxy.size.width,
                dynamicTypeSize: dynamicTypeSize,
                hasSelection: selection.taskId != nil || selection.sessionId != nil
            )
            Group {
                switch timelineFocused && selection.taskId != nil ? .pushDetail : mode {
                case .table:
                    WorkTablePage(browse: selection.workBrowse)
                        .transition(listTransition)
                case .split:
                    splitLayout(size: proxy.size)
                        .transition(detailTransition)
                case .pushDetail:
                    recordDetail(autoFocusEntry: true)
                        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                        .transition(detailTransition)
                }
            }
            .environment(\.workCompactViewport, proxy.size.height < 720)
        }
        .animation(
            reduceMotion ? Motion.reducedCrossfade : Motion.detailNavigation,
            value: selectionKey
        )
        .task(id: resolutionKey) {
            // The fixture renderer injects the exact Work state under review.
            // Starting a live fetch here would immediately clear an injected
            // error and collapse error/loading snapshots into the same frame.
            guard !SnapshotMode.enabled else { return }
            await resolveSelection()
        }
        .task(id: selectionKey) {
            guard !SnapshotMode.enabled, !dashboard.isOfflineSnapshot, let taskId = selection.taskId else { return }
            // Refresh only this task while it is visible. The store rejects
            // obsolete responses when navigation changes during a request.
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(3)) } catch { return }
                guard !Task.isCancelled, selection.taskId == taskId else { return }
                await dashboard.fetchReceipt(taskId: taskId)
            }
        }
        .task(id: selection.workBrowse.group) {
            guard !SnapshotMode.enabled, selection.workBrowse.group == .attention else { return }
            await dashboard.fetchAttention()
        }
    }

    @ViewBuilder
    private func splitLayout(size: CGSize) -> some View {
        // HSplitView is the native resizable live control, but AppKit-backed
        // split views become a warning placeholder in ImageRenderer. The fixed
        // SwiftUI sibling uses the same ideal width for deterministic review.
        Group {
            if SnapshotMode.enabled && !SnapshotMode.interactiveFixture {
                HStack(alignment: .top, spacing: 0) {
                    WorkMasterList(browse: selection.workBrowse)
                        .frame(width: 320)
                    Rectangle().fill(Theme.rule).frame(width: 1)
                    recordDetail(autoFocusEntry: false)
                        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                }
            } else {
                HSplitView {
                    WorkMasterList(browse: selection.workBrowse)
                        .frame(minWidth: 280, idealWidth: 320, maxWidth: 360)
                    recordDetail(autoFocusEntry: false)
                        .frame(minWidth: 620, maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                }
            }
        }
        .frame(width: size.width, height: size.height, alignment: .top)
        .clipped()
    }

    /// Keep list navigation, direct Task links, and menu-bar session links on
    /// one lifecycle. The task id is part of `.task(id:)`, so changing rows
    /// cancels an obsolete Receipt request before starting the next one.
    private func resolveSelection() async {
        unresolvedSessionId = nil
        if selectionKey == "list" || dashboard.receiptTasks.isEmpty {
            await dashboard.fetchReceipts()
        }
        guard !Task.isCancelled else { return }
        if let taskId = selection.taskId {
            await dashboard.fetchReceipt(taskId: taskId)
            return
        }
        guard let sessionId = selection.sessionId else { return }
        switch workSessionResolution(for: sessionId, in: dashboard.receiptTasks, projection: dashboard.receiptListProjection) {
        case .task(let taskId):
            selection.taskId = taskId
            selection.sessionId = nil
        case .pending:
            return
        case .unresolved(let sessionId):
            // Compact Task summaries intentionally carry only the primary root.
            // Keep a continuation/subagent selection intact and explain the
            // limitation instead of silently replacing it with an empty detail.
            unresolvedSessionId = sessionId
        }
    }

    @ViewBuilder
    private func recordDetail(autoFocusEntry: Bool) -> some View {
        ZStack(alignment: .topLeading) {
            Group {
                if let receipt = dashboard.receipt, receipt.taskId == selection.taskId {
                    WorkRecordPage(
                        receipt: receipt,
                        summary: dashboard.receiptTasks.first { $0.taskId == receipt.taskId },
                        refreshError: workReceiptRefreshError(
                            selectedTaskId: selection.taskId,
                            errorTaskId: dashboard.receiptErrorTaskId,
                            error: dashboard.receiptError
                        ),
                        isRefreshing: dashboard.receiptLoadingTaskId == receipt.taskId,
                        autoFocusEntry: autoFocusEntry,
                        timelineFocused: timelineFocused,
                        onToggleTimelineFocus: { timelineFocused.toggle() }
                    )
                } else if let taskId = selection.taskId,
                          let error = workReceiptRefreshError(
                              selectedTaskId: taskId,
                              errorTaskId: dashboard.receiptErrorTaskId,
                              error: dashboard.receiptError
                          ) {
                    WorkRecordPlaceholder(
                        title: "Receipt unavailable",
                        message: error,
                        symbol: "exclamationmark.triangle",
                        retryTitle: dashboard.isOfflineSnapshot
                            ? (reconnectSavedWork == nil ? nil : "Back to recovery")
                            : (dashboard.receiptLoadingTaskId == taskId ? nil : "Retry"),
                        showsProgress: dashboard.receiptLoadingTaskId == taskId,
                        autoFocusEntry: autoFocusEntry
                    ) {
                        if dashboard.isOfflineSnapshot { reconnectSavedWork?() }
                        else { Task { await dashboard.fetchReceipt(taskId: taskId) } }
                    }
                } else if let unresolvedSessionId, selection.sessionId == unresolvedSessionId {
                    WorkRecordPlaceholder(
                        title: "Task link unavailable",
                        message: "This active session is not identified in the task summary. Select its Task from the collection to inspect the full receipt.",
                        symbol: "arrow.triangle.branch",
                        autoFocusEntry: autoFocusEntry
                    )
                    .accessibilityIdentifier("work.unresolved-session")
                } else if selection.sessionId != nil, dashboard.receiptListProjection?.needsRefresh == true {
                    WorkRecordPlaceholder(
                        title: dashboard.receiptListProjection?.statusText ?? "Preparing work receipts",
                        message: "The session link will open when its work receipt is ready.",
                        symbol: "arrow.triangle.2.circlepath",
                        showsProgress: true,
                        autoFocusEntry: autoFocusEntry
                    )
                } else if selection.taskId != nil {
                    WorkRecordPlaceholder(
                        title: dashboard.receiptProjection?.statusText ?? "Loading receipt",
                        message: dashboard.receiptProjection?.needsRefresh == true ? "The recorder is preparing a safe snapshot. This view will update automatically." : "Fetching the latest evidence and check results…",
                        symbol: "arrow.triangle.2.circlepath",
                        showsProgress: true,
                        autoFocusEntry: autoFocusEntry
                    )
                } else {
                    Color.clear
                }
            }
            .id(phaseKey)
            .transition(.opacity)
        }
        .animation(
            reduceMotion ? Motion.reducedCrossfade : Motion.phaseCrossfade,
            value: phaseKey
        )
    }
}

/// Loading and failure states keep the record shell navigable. A failed or slow
/// request must not trap compact-window and keyboard users on a blank surface.
private struct WorkRecordPlaceholder: View {
    @Environment(AppSelection.self) var selection
    @Environment(DashboardStore.self) var dashboard
    let title: String
    let message: String
    let symbol: String
    var retryTitle: String?
    var showsProgress = false
    var autoFocusEntry = false
    var retry: (() -> Void)?
    @FocusState private var backFocused: Bool
    @AccessibilityFocusState private var backAccessibilityFocused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            backButton

            Card {
                VStack(spacing: Space.m) {
                    if showsProgress && !SnapshotMode.enabled {
                        ProgressView().controlSize(.small)
                    } else {
                        Image(systemName: symbol)
                            .font(.system(size: 20, weight: .medium))
                            .foregroundStyle(showsProgress ? Theme.muted : Theme.amber)
                            .accessibilityHidden(true)
                    }
                    Text(title).workFont(.titleCard).foregroundStyle(Theme.ink)
                    Text(message)
                        .workFont(.body).foregroundStyle(Theme.muted)
                        .multilineTextAlignment(.center)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: 420)
                    if let retryTitle, let retry {
                        Button(retryTitle, action: retry)
                            .buttonStyle(QuietButtonStyle(tint: Theme.accent))
                            .accessibilityIdentifier("work.placeholder.retry")
                    }
                }
                .frame(maxWidth: .infinity)
            }
            .padding(.top, Space.xl)
            Spacer(minLength: 0)
        }
        .padding(Space.gutter)
        .frame(maxWidth: 760, maxHeight: .infinity, alignment: .topLeading)
        .frame(maxWidth: .infinity, alignment: .top)
        .onAppear {
            guard autoFocusEntry, !SnapshotMode.enabled else { return }
            DispatchQueue.main.async {
                backFocused = true
                backAccessibilityFocused = true
            }
        }
    }

    @ViewBuilder
    private var backButton: some View {
        // ImageRenderer blanks controls carrying AccessibilityFocusState.
        // The live path retains it; snapshots render the same visible button.
        if SnapshotMode.enabled {
            backButtonBase
        } else {
            backButtonBase.accessibilityFocused($backAccessibilityFocused)
        }
    }

    private var backButtonBase: some View {
        Button {
            selection.workBrowse.prepareReturnFocus(
                from: selection.taskId,
                in: dashboard.receiptTasks,
                attention: dashboard.attention
            )
            selection.taskId = nil
            selection.sessionId = nil
        } label: {
            Label("All receipts", systemImage: "chevron.left")
                .workFont(.captionSemibold)
                .foregroundStyle(Theme.accent)
        }
        .buttonStyle(QuietButtonStyle(horizontalPadding: 6, verticalPadding: 3))
        .frame(minHeight: 24)
        .focused($backFocused)
        .keyboardShortcut(.cancelAction)
        .accessibilityIdentifier("work.placeholder.back")
    }
}

// MARK: - Work receipts table

struct WorkTaskPresentation {
    let groupCounts: [WorkGroup: Int]
    let visibleTasks: [ReceiptSummary]

    init(
        tasks: [ReceiptSummary],
        attention: V1AttentionPayload? = nil,
        group: WorkGroup?,
        query: String,
        sort: WorkSort
    ) {
        var counts = Dictionary(grouping: tasks, by: WorkGroup.forTask).mapValues(\.count)
        // Attention is complete across the store and can exceed the loaded
        // receipts page; other lifecycle counts still describe that page.
        if let total = attention?.total { counts[.attention] = total }
        groupCounts = counts

        // The endpoint has already classified and operationally ordered a
        // bounded queue across every visible Task. Do not re-derive it from
        // the latest receipts page.
        let sourceTasks = group == .attention ? attention?.items ?? [] : tasks
        visibleTasks = visibleWorkReceipts(
            sourceTasks,
            query: query,
            group: group == .attention ? nil : group,
            sort: sort
        )
    }
}

private struct WorkTablePage: View {
    @Environment(DashboardStore.self) var dashboard
    @Environment(AppSelection.self) var selection
    @ObservedObject var browse: WorkBrowseState
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @FocusState private var searchFocused: Bool
    @FocusState private var focusedTaskId: String?
    @AccessibilityFocusState private var searchAccessibilityFocused: Bool
    @AccessibilityFocusState private var accessibilityFocusedTaskId: String?

    var body: some View {
        let presentation = WorkTaskPresentation(
            tasks: dashboard.receiptTasks,
            attention: dashboard.attention,
            group: browse.group,
            query: browse.query,
            sort: browse.sort
        )
        ScrollViewReader { scrollProxy in
            ScrollBox {
                VStack(alignment: .leading, spacing: 0) {
                    header
                    filterRow.padding(.top, Space.l)
                    if let projection = browse.group == .attention ? dashboard.attentionProjection : dashboard.receiptListProjection {
                        WorkProjectionNotice(projection: projection, isOffline: dashboard.isOfflineSnapshot).padding(.top, Space.m)
                    }
                    if browse.group != .attention, let error = dashboard.receiptListError {
                        listStatusBanner(error).padding(.top, Space.m)
                    }
                    tableCard(visibleTasks: presentation.visibleTasks)
                        .padding(
                            .top,
                            browse.group != .attention && dashboard.receiptListError != nil
                                ? Space.m : Space.l
                        )
                    footer(visibleTasks: presentation.visibleTasks).padding(.top, Space.m)
                }
                .padding(Space.gutter)
                .frame(maxWidth: 1172 + Space.gutter * 2, alignment: .leading)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .onMoveCommand {
                moveTableFocus($0, visibleTasks: presentation.visibleTasks, scrollProxy: scrollProxy)
            }
            .onAppear {
                restoreReturnFocus(visibleTasks: presentation.visibleTasks, scrollProxy: scrollProxy)
            }
            .onChange(of: browse.query) {
                clearInvalidPendingFocus(visibleTasks: presentation.visibleTasks)
            }
            .onChange(of: browse.group) {
                clearInvalidPendingFocus(visibleTasks: presentation.visibleTasks)
            }
        }
    }

    private func restoreReturnFocus(
        visibleTasks: [ReceiptSummary],
        scrollProxy: ScrollViewProxy
    ) {
        guard !SnapshotMode.enabled else { return }
        if let taskId = browse.pendingFocusRestorationTaskId,
           visibleTasks.contains(where: { $0.taskId == taskId }) {
            browse.pendingFocusRestorationTaskId = nil
            scrollProxy.scrollTo(taskId, anchor: .center)
            DispatchQueue.main.async {
                focusedTaskId = taskId
                accessibilityFocusedTaskId = taskId
            }
            return
        }
        if browse.shouldFocusSearchOnReturn || browse.pendingFocusRestorationTaskId != nil {
            browse.pendingFocusRestorationTaskId = nil
            browse.shouldFocusSearchOnReturn = false
            DispatchQueue.main.async {
                searchFocused = true
                searchAccessibilityFocused = true
            }
        }
    }

    private func clearInvalidPendingFocus(visibleTasks: [ReceiptSummary]) {
        guard let taskId = browse.pendingFocusRestorationTaskId,
              !visibleTasks.contains(where: { $0.taskId == taskId }) else { return }
        browse.pendingFocusRestorationTaskId = nil
    }

    private func moveTableFocus(
        _ direction: MoveCommandDirection,
        visibleTasks: [ReceiptSummary],
        scrollProxy: ScrollViewProxy
    ) {
        guard direction == .up || direction == .down, !visibleTasks.isEmpty else { return }
        let current = visibleTasks.firstIndex { $0.taskId == focusedTaskId }
        let nextIndex: Int
        switch direction {
        case .up: nextIndex = max(0, (current ?? 1) - 1)
        case .down: nextIndex = min(visibleTasks.count - 1, (current ?? -1) + 1)
        default: return
        }
        let taskId = visibleTasks[nextIndex].taskId
        focusedTaskId = taskId
        withAnimation(reduceMotion ? nil : Motion.hover) {
            scrollProxy.scrollTo(taskId, anchor: .center)
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            // The receipts collection's tab is "Sessions" (MainPane.work); the
            // page title matches it. "Work" is the sibling worksets tab.
            Text("Sessions")
                .workFont(.titlePage).tracking(Type.titlePageTracking)
                .foregroundStyle(Theme.ink)
                .accessibilityAddTraits(.isHeader)
            Text("Agent sessions and their recorded work, grouped into task receipts.")
                .workFont(.caption).foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var filterRow: some View {
        let layout = dynamicTypeSize.isAccessibilitySize
            ? AnyLayout(VStackLayout(alignment: .leading, spacing: Space.m))
            : AnyLayout(HStackLayout(spacing: Space.m))
        return layout {
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 11)).foregroundStyle(Theme.muted)
                if SnapshotMode.enabled {
                    // ImageRenderer draws a TextField as a yellow placeholder;
                    // a snapshot shows the prompt as plain text instead.
                    Text("Search tasks or projects").workFont(.caption).foregroundStyle(Theme.muted)
                } else {
                    TextField("Search tasks or projects", text: $browse.query)
                        .textFieldStyle(.plain).workFont(.caption)
                        .focused($searchFocused)
                        .accessibilityFocused($searchAccessibilityFocused)
                        .accessibilityIdentifier("work.table.search")
                }
            }
            .padding(.horizontal, Space.m)
            .frame(width: 300, height: 32)
            .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
            .overlay(
                RoundedRectangle(cornerRadius: Metrics.radius)
                    .strokeBorder(Theme.cardLine, lineWidth: Metrics.borderW)
            )
            WorkStatusMenu(group: $browse.group, identifier: "work.table.status")
                .fixedSize()
            WorkSortMenu(sort: $browse.sort, identifier: "work.table.sort", minHeight: 32)
                .fixedSize()
            DecisionLegendButton()
            Spacer()
        }
    }

    private func tableCard(visibleTasks: [ReceiptSummary]) -> some View {
        Card(padding: 0) {
            VStack(spacing: 0) {
                if !dynamicTypeSize.isAccessibilitySize {
                    columnHeader
                    Rectangle().fill(Theme.hairline).frame(height: 1).padding(.horizontal, Space.xl)
                }
                if browse.group != .attention,
                   (dashboard.isLoadingReceipts || dashboard.receiptListProjection?.needsRefresh == true),
                   dashboard.receiptTasks.isEmpty {
                    HStack(spacing: Space.m) {
                        if SnapshotMode.enabled {
                            Image(systemName: "arrow.triangle.2.circlepath")
                                .foregroundStyle(Theme.muted)
                                .accessibilityHidden(true)
                        } else {
                            ProgressView().controlSize(.small)
                        }
                        VStack(alignment: .leading, spacing: 4) {
                            Text(dashboard.receiptListProjection?.statusText ?? "Loading receipts").workFont(.rowLabel).foregroundStyle(Theme.ink)
                            Text("Reading the latest recorded work from the local store.")
                                .workFont(.caption).foregroundStyle(Theme.muted)
                        }
                    }
                    .padding(Space.xl)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel(dashboard.receiptListProjection?.needsRefresh == true ? "Preparing work receipts. Checking again automatically." : "Loading receipts from the local store")
                } else if let error = visibleError, visibleTasks.isEmpty {
                    if browse.group == .attention {
                        Text(error).workFont(.body).foregroundStyle(Theme.muted)
                            .padding(Space.xl)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    } else {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("No receipt data available")
                                .workFont(.rowLabel).foregroundStyle(Theme.ink)
                            Text("The collection will return after a successful refresh.")
                                .workFont(.caption).foregroundStyle(Theme.muted)
                                .help(error)
                        }
                        .padding(Space.xl)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                } else if browse.group == .attention, dashboard.attention == nil {
                    HStack(spacing: Space.m) {
                        if SnapshotMode.enabled {
                            Image(systemName: "arrow.triangle.2.circlepath")
                                .foregroundStyle(Theme.muted)
                                .accessibilityHidden(true)
                        } else {
                            ProgressView().controlSize(.small).tint(Theme.muted)
                        }
                        Text("Checking recorded work…")
                            .workFont(.body).foregroundStyle(Theme.muted)
                    }
                    .padding(Space.xl)
                    .frame(maxWidth: .infinity, alignment: .leading)
                } else if visibleTasks.isEmpty {
                    let attentionCopy = dashboard.attention.map {
                        WorkAttentionEmptyCopy(payload: $0, query: browse.query)
                    }
                    VStack(alignment: .leading, spacing: 4) {
                        Text(
                            browse.group == .attention
                                ? attentionCopy?.title ?? "Review status unavailable"
                                : dashboard.receiptTasks.isEmpty
                                    ? "No receipts recorded yet" : "No receipts match"
                        )
                        .workFont(.rowLabel).foregroundStyle(Theme.ink)
                        Text(
                            browse.group == .attention
                                ? attentionCopy?.detail ?? "Refresh before acting on the review queue."
                                : dashboard.receiptTasks.isEmpty
                                    ? "Recorded coding work will appear here when the local store receives it."
                                    : filteredEmptyMessage
                        )
                        .workFont(.caption).foregroundStyle(Theme.muted)
                    }
                    .padding(Space.xl)
                    .frame(maxWidth: .infinity, alignment: .leading)
                } else {
                    // Offscreen full-content renders cap the rows and name the
                    // overflow; the live app scrolls the full set.
                    let rows = SnapshotMode.enabled ? Array(visibleTasks.prefix(9)) : visibleTasks
                    ScrollContentStack(spacing: 0) {
                        ForEach(Array(rows.enumerated()), id: \.element.id) { index, task in
                            if index > 0 {
                                Rectangle().fill(Theme.hairline).frame(height: 1)
                                    .padding(.horizontal, Space.xl)
                            }
                            if SnapshotMode.enabled {
                                tableRow(task).id(task.taskId)
                            } else {
                                tableRow(task)
                                    .id(task.taskId)
                                    .accessibilityFocused(
                                        $accessibilityFocusedTaskId,
                                        equals: task.taskId
                                    )
                            }
                        }
                    }
                    if SnapshotMode.enabled, visibleTasks.count > rows.count {
                        Rectangle().fill(Theme.hairline).frame(height: 1)
                            .padding(.horizontal, Space.xl)
                        Text("… \(visibleTasks.count - rows.count) more receipts (snapshot preview)")
                            .workFont(.dataSmall).foregroundStyle(Theme.muted)
                            .padding(Space.xl)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
        }
        .accessibilityIdentifier("work.table")
    }

    @ViewBuilder
    private func tableRow(_ task: ReceiptSummary) -> some View {
        if dynamicTypeSize.isAccessibilitySize {
            WorkAccessibleTableRow(task: task, focus: $focusedTaskId) {
                selection.sessionId = nil
                selection.taskId = task.taskId
            }
        } else {
            WorkTableRow(task: task, focus: $focusedTaskId) {
                selection.sessionId = nil
                selection.taskId = task.taskId
            }
        }
    }

    private func listStatusBanner(_ error: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Space.s) {
            if dashboard.isLoadingReceipts, !SnapshotMode.enabled {
                ProgressView().controlSize(.small)
            } else {
                Image(systemName: "exclamationmark.triangle")
                    .font(.system(size: 11, weight: .semibold)).foregroundStyle(Theme.amber)
                    .accessibilityHidden(true)
            }
            Text(
                dashboard.isLoadingReceipts
                    ? "Retrying the receipt list · showing the last loaded data when available"
                    : dashboard.receiptTasks.isEmpty
                    ? "Receipt list unavailable · \(error)"
                    : "Showing the last loaded receipt list · refresh failed: \(error)"
            )
            .workFont(.caption).foregroundStyle(Theme.muted)
            .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: Space.m)
            if !SnapshotMode.enabled, !dashboard.isLoadingReceipts {
                Button("Retry") { Task { await dashboard.fetchReceipts() } }
                    .buttonStyle(QuietButtonStyle(tint: Theme.accent))
                    .accessibilityIdentifier("work.list.retry")
            }
        }
        .padding(.horizontal, Space.m)
        .padding(.vertical, Space.s)
        .background(Theme.amber.opacity(0.08), in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(
            RoundedRectangle(cornerRadius: Metrics.radius)
                .strokeBorder(Theme.amber.opacity(0.32), lineWidth: Metrics.borderW)
        )
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("work.list.status")
    }

    private var columnHeader: some View {
        HStack(spacing: Space.l) {
            CapsLabel(text: "Task / agent").frame(maxWidth: .infinity, alignment: .leading)
            CapsLabel(text: "Activity").frame(width: 90, alignment: .leading)
            CapsLabel(text: "Outcome").frame(width: 124, alignment: .leading)
            CapsLabel(text: "Evidence").frame(width: 190, alignment: .leading)
            CapsLabel(text: "Est. cost").frame(width: 76, alignment: .trailing)
        }
        .padding(.horizontal, Space.xl)
        .frame(height: Metrics.rowHeader)
    }

    private func footer(visibleTasks: [ReceiptSummary]) -> some View {
        HStack(spacing: Space.m) {
            Text(footerText(visibleTasks: visibleTasks))
                .workFont(.dataSmall).foregroundStyle(Theme.muted)
            if browse.group == .attention, let error = dashboard.attentionError {
                Text(error)
                    .workFont(.dataSmall)
                    .foregroundStyle(Theme.coral)
                    .lineLimit(1)
            }
            Spacer()
            if browse.group == .attention, dashboard.attention?.truncated == true {
                Button(dashboard.isLoadingMoreAttention ? "Loading…" : "Load more") {
                    Task { await dashboard.fetchMoreAttention() }
                }
                .buttonStyle(QuietButtonStyle())
                .disabled(dashboard.isLoadingMoreAttention)
                .accessibilityIdentifier("work.attention.load-more")
            }
        }
    }

    private var visibleError: String? {
        browse.group == .attention ? dashboard.attentionError : dashboard.receiptListError
    }

    private func footerText(visibleTasks: [ReceiptSummary]) -> String {
        if browse.group == .attention, let attention = dashboard.attention {
            let scope = attention.truncated ? "bounded operational queue" : "complete queue"
            return "\(visibleTasks.count) of \(attention.total) review items · \(scope)"
        }
        return workBrowseCountText(
            visible: visibleTasks.count,
            loaded: dashboard.receiptTasks.count,
            total: dashboard.totalReceiptTasks,
            truncated: dashboard.receiptTasksTruncated
        )
    }

    private var filteredEmptyMessage: String {
        if let total = dashboard.totalReceiptTasks,
           workReceiptCollectionIsPartial(
               loaded: dashboard.receiptTasks.count,
               total: total,
               truncated: dashboard.receiptTasksTruncated
           ) {
            return "No loaded receipts match. Search covers the latest \(dashboard.receiptTasks.count) of \(total) receipts."
        }
        if dashboard.totalReceiptTasks == nil || dashboard.receiptTasksTruncated == true {
            return "No loaded receipts match. The store did not report a complete total, so more receipts may exist."
        }
        return "Adjust the status filter or search to broaden the result."
    }


}

/// A session-oriented receipt row: task and agent/project context lead,
/// followed by activity, recorded outcome, evidence, and cost.
private struct WorkTableRow: View {
    let task: ReceiptSummary
    let focus: FocusState<String?>.Binding
    let action: () -> Void

    private var presentation: WorkReceiptRowPresentation { .init(task: task) }

    init(
        task: ReceiptSummary,
        focus: FocusState<String?>.Binding,
        action: @escaping () -> Void
    ) {
        self.task = task
        self.focus = focus
        self.action = action
    }

    var body: some View {
        Button(action: action) {
            HStack(spacing: Space.l) {
                VStack(alignment: .leading, spacing: Space.xs) {
                    Text(presentation.title)
                        .workFont(.rowLabel).foregroundStyle(Theme.ink)
                        .lineLimit(1).truncationMode(.tail)
                        .help(presentation.title)
                    Text([presentation.clientText, presentation.projectText].compactMap { $0 }.joined(separator: " · "))
                        .workFont(.caption).foregroundStyle(Theme.muted)
                        .lineLimit(1)
                        .help([presentation.clientText, presentation.projectText].compactMap { $0 }.joined(separator: " · "))
                }
                .frame(maxWidth: .infinity, alignment: .leading)

                Text(presentation.updatedText)
                    .workFont(.dataSmall).foregroundStyle(Theme.muted)
                    .frame(width: 90, alignment: .leading)
                VStack(alignment: .leading, spacing: Space.xs) {
                    DecisionBadge(
                        key: presentation.decisionKey,
                        label: presentation.decisionLabel,
                        compact: true
                    )
                    // Hover says WHY: the blocker's own words when blocked,
                    // otherwise the daemon's one-line statement.
                    .help(presentation.decisionHelp)
                    // Parallel deliberate-stop marker, only when it adds info the
                    // decision word does not already state.
                    if presentation.handedOff && presentation.decisionKey != "handed_off" {
                        Chip(text: "↗ handed off", tint: Theme.muted)
                    }
                }
                .frame(width: 124, alignment: .leading)

                VStack(alignment: .leading, spacing: Space.xs) {
                    evidenceCell
                    Text("Checks: \(presentation.compactCheckRunsText)")
                        .workFont(.caption).foregroundStyle(presentation.checkRunsAreInconsistent ? Theme.amber : Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                        .help(presentation.checkRunsText)
                }.frame(width: 190, alignment: .leading)
                costCell.frame(width: 76, alignment: .trailing)
            }
            .padding(.horizontal, Space.xl)
            .frame(minHeight: 64)
            .contentShape(Rectangle())
        }
        .buttonStyle(SurfaceButtonStyle(
            cornerRadius: 0,
            focusInset: 2
        ))
        .focused(focus, equals: task.taskId)
        .accessibilityIdentifier("work.table.task.\(task.taskId)")
        .accessibilityLabel(presentation.accessibilityLabel)
    }

    /// Checked/checkable ratio + the strongest tier's pip shape. The pip is a
    /// ceiling marker (best evidence present), the ratio is the coverage.
    @ViewBuilder
    private var evidenceCell: some View {
        let coverage = ReceiptCoveragePresentation(evidence: presentation.evidence)
        if presentation.evidence.gradeable != false,
           let checkable = presentation.evidence.checkableTotal,
           checkable > 0 {
            HStack(spacing: 7) {
                let style = EvidenceTierStyle.forGrade(presentation.evidence.strongestTier ?? "unchecked")
                EvidencePip(shape: style.pip, tint: style.tint)
                Text(coverage.rowText)
                    .workFont(.dataSmall)
                    .foregroundStyle(coverage.isInconsistent ? Theme.amber : Theme.ink)
                    .lineLimit(2)
                    .help(coverage.qualifier)
            }
        } else {
            HStack(spacing: 7) {
                EvidencePip(shape: .hollow, tint: Theme.muted)
                Text(coverage.rowText).workFont(.dataSmall).foregroundStyle(Theme.muted)
                    .lineLimit(2)
            }
            .help(coverage.qualifier)
        }
    }

    /// Cost with the ~/≈ prefix grammar; a task with no priced usage is a
    /// named state, never a dash.
    @ViewBuilder
    private var costCell: some View {
        let planText = Fmt.planPct(task.cost.planShare?.pct).map { "\($0) of weekly plan" }
        if presentation.costText == "cost unknown" {
            Text("unpriced").workFont(.dataSmall).foregroundStyle(Theme.muted)
        } else {
            Text(presentation.costText).workFont(.dataSmall).foregroundStyle(Theme.ink)
                .help(planText ?? "")
        }
    }


}

/// Accessibility text sizes trade the fixed-column task table for a
/// complete vertical record summary. No fact disappears; labels and values can
/// wrap without colliding with neighboring columns.
private struct WorkAccessibleTableRow: View {
    let task: ReceiptSummary
    let focus: FocusState<String?>.Binding
    let action: () -> Void
    private var presentation: WorkReceiptRowPresentation { .init(task: task) }

    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: Space.s) {
                Text(presentation.title)
                    .workFont(.rowLabel).foregroundStyle(Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)
                DecisionBadge(
                    key: presentation.decisionKey,
                    label: presentation.decisionLabel,
                    compact: true
                )
                if let reason = presentation.attentionReason, !reason.isEmpty {
                    Text(reason).workFont(.caption).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                labelledValue("Claims supported", presentation.coverageText)
                labelledValue("Check runs", presentation.checkRunsText)
                labelledValue("Client", presentation.clientText)
                if let project = presentation.projectText { labelledValue("Project", project) }
                labelledValue("Estimated cost", presentation.costText)
                labelledValue("Updated", presentation.updatedText)
                if presentation.handedOff {
                    Text("Handed off").workFont(.captionSemibold).foregroundStyle(Theme.muted)
                }
            }
            .padding(Space.l)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(focus.wrappedValue == task.taskId ? Theme.selected.opacity(0.5) : .clear)
            .contentShape(Rectangle())
        }
        .buttonStyle(SurfaceButtonStyle(cornerRadius: 0, focusInset: 2))
        .focused(focus, equals: task.taskId)
        .accessibilityIdentifier("work.table.task.\(task.taskId)")
        .accessibilityLabel(presentation.accessibilityLabel)
    }

    private func labelledValue(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            CapsLabel(text: label)
            Text(value).workFont(.caption).foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

// MARK: - Receipt master (wide record mode)

/// The table's compact sibling, not a separate navigation universe. It shares
/// the exact query/group/sort model and keeps enough evidence context visible
/// to compare Tasks while a receipt is open.
private struct WorkMasterList: View {
    @Environment(DashboardStore.self) var dashboard
    @Environment(AppSelection.self) var selection
    @ObservedObject var browse: WorkBrowseState
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @FocusState private var focusedTaskId: String?
    @State private var isRetryingAttention = false

    private var visibleTasks: [ReceiptSummary] {
        browse.visibleTasks(in: dashboard.receiptTasks, attention: dashboard.attention)
    }

    private var sourceTasks: [ReceiptSummary] {
        browse.group == .attention ? dashboard.attention?.items ?? [] : dashboard.receiptTasks
    }

    private var collectionError: String? {
        browse.group == .attention ? dashboard.attentionError : dashboard.receiptListError
    }

    private var isLoadingCollection: Bool {
        if browse.group == .attention {
            return isRetryingAttention || (dashboard.attention == nil && dashboard.attentionError == nil)
        }
        return dashboard.isLoadingReceipts || dashboard.receiptListProjection?.needsRefresh == true
    }

    private var collectionCount: String {
        if browse.group == .attention {
            guard let attention = dashboard.attention else {
                return collectionError == nil ? "Loading review items…" : "Review status unavailable"
            }
            if attention.truncated {
                return "\(visibleTasks.count) of \(attention.items.count) loaded · \(attention.total) review items"
            }
            return "\(visibleTasks.count) of \(attention.total) review items"
        }
        return workBrowseCountText(
            visible: visibleTasks.count,
            loaded: dashboard.receiptTasks.count,
            total: dashboard.totalReceiptTasks,
            truncated: dashboard.receiptTasksTruncated
        )
    }

    private var renderedTasks: [ReceiptSummary] {
        guard SnapshotMode.enabled else { return visibleTasks }
        return Array(visibleTasks.prefix(7))
    }

    private var selectionIsOutsideBrowse: Bool {
        workSelectionIsOutsideBrowse(
            taskId: selection.taskId,
            allTasks: sourceTasks,
            visibleTasks: visibleTasks
        )
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                Text("Tasks").workFont(.titleCard).foregroundStyle(Theme.ink)
                    .accessibilityAddTraits(.isHeader)
                Spacer(minLength: 0)
                Text(collectionCount)
                    .workFont(.dataSmall).foregroundStyle(Theme.muted)
            }
            .padding(.horizontal, Space.l)
            .padding(.top, Space.l)

            masterControls.padding(Space.l)
            if let projection = browse.group == .attention ? dashboard.attentionProjection : dashboard.receiptListProjection {
                WorkProjectionNotice(projection: projection, isOffline: dashboard.isOfflineSnapshot)
                    .padding(.horizontal, Space.l).padding(.bottom, Space.s)
            }
            if selectionIsOutsideBrowse {
                HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                    Text("Selected receipt is outside these filters")
                        .workFont(.dataSmall).foregroundStyle(Theme.muted)
                    Spacer(minLength: 0)
                    Button("Show") {
                        browse.query = ""
                        if browse.group != .attention { browse.group = nil }
                    }
                    .buttonStyle(QuietButtonStyle(tint: Theme.accent))
                    .accessibilityIdentifier("work.master.show-selection")
                }
                .padding(.horizontal, Space.l)
                .padding(.bottom, Space.s)
                .accessibilityElement(children: .contain)
            }
            if let error = collectionError, !sourceTasks.isEmpty {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    if isLoadingCollection, !SnapshotMode.enabled {
                        ProgressView().controlSize(.mini)
                    } else {
                        Image(systemName: "exclamationmark.triangle")
                            .font(.system(size: 10, weight: .semibold)).foregroundStyle(Theme.amber)
                            .accessibilityHidden(true)
                    }
                    Text(isLoadingCollection ? "Retrying · showing saved list" : "Showing saved list · refresh failed")
                        .workFont(.dataSmall).foregroundStyle(Theme.muted)
                        .help(error)
                    Spacer(minLength: 0)
                    if !SnapshotMode.enabled, !isLoadingCollection {
                        Button("Retry", action: retryCollection)
                            .buttonStyle(QuietButtonStyle(tint: Theme.accent))
                            .accessibilityIdentifier("work.master.retry")
                    }
                }
                .padding(.horizontal, Space.l)
                .padding(.bottom, Space.s)
                .accessibilityElement(children: .contain)
            }
            Rectangle().fill(Theme.rule).frame(height: 1)

            ScrollViewReader { scrollProxy in
                ScrollBox {
                    ScrollContentStack(spacing: 0) {
                    if isLoadingCollection, sourceTasks.isEmpty {
                        masterEmpty(
                            title: (browse.group == .attention ? dashboard.attentionProjection : dashboard.receiptListProjection)?.state == "pending"
                                ? "Preparing work receipts" : (browse.group == .attention ? "Loading review items" : "Loading receipts"),
                            message: nil
                        )
                    } else if let error = collectionError, sourceTasks.isEmpty {
                        VStack(alignment: .leading, spacing: Space.s) {
                            masterEmpty(
                                title: browse.group == .attention ? "Review items unavailable" : "Receipts unavailable",
                                message: error
                            )
                            if !SnapshotMode.enabled {
                                Button("Retry", action: retryCollection)
                                    .buttonStyle(QuietButtonStyle(tint: Theme.accent))
                                    .padding(.horizontal, Space.l)
                                    .accessibilityIdentifier("work.master.retry")
                            }
                        }
                    } else if visibleTasks.isEmpty {
                        if browse.group == .attention, let attention = dashboard.attention {
                            let copy = WorkAttentionEmptyCopy(payload: attention, query: browse.query)
                            masterEmpty(title: copy.title, message: copy.detail)
                            if attention.total > 0, attention.items.isEmpty, !SnapshotMode.enabled {
                                Button("Retry", action: retryCollection)
                                    .buttonStyle(QuietButtonStyle(tint: Theme.accent))
                                    .padding(.horizontal, Space.l)
                                    .accessibilityIdentifier("work.master.retry")
                            }
                        } else {
                            masterEmpty(
                                title: dashboard.receiptTasks.isEmpty ? "No receipts recorded yet" : "No matching receipts",
                                message: dashboard.receiptTasks.isEmpty
                                    ? "Recorded work will appear here."
                                    : "Clear the filter or choose another status."
                            )
                        }
                    } else {
                        ForEach(renderedTasks) { task in
                            WorkMasterRow(
                                presentation: .init(
                                    task: task,
                                    detail: task.taskId == selection.taskId ? dashboard.receipt : nil
                                ),
                                selected: task.taskId == selection.taskId,
                                focus: $focusedTaskId
                            ) {
                                selection.sessionId = nil
                                selection.taskId = task.taskId
                            }
                            .id(task.taskId)
                        }
                    }
                    if SnapshotMode.enabled, visibleTasks.count > renderedTasks.count {
                        Text("… \(visibleTasks.count - renderedTasks.count) more")
                            .workFont(.dataSmall).foregroundStyle(Theme.muted)
                            .padding(Space.l)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                }
                .onMoveCommand { direction in moveSelection(direction, scrollProxy: scrollProxy) }
                .onAppear { focusSelectedRowIfVisible() }
                .onChange(of: selection.taskId) { focusSelectedRowIfVisible() }
            }
            if browse.group == .attention, dashboard.attention?.truncated == true {
                Button(dashboard.isLoadingMoreAttention ? "Loading…" : "Load more") {
                    Task { await dashboard.fetchMoreAttention() }
                }
                .buttonStyle(QuietButtonStyle())
                .disabled(dashboard.isLoadingMoreAttention || isRetryingAttention)
                .padding(Space.l)
                .accessibilityIdentifier("work.attention.load-more")
            }
        }
        .background(Theme.chrome)
        .accessibilityIdentifier("work.master")
    }

    private var masterControls: some View {
        VStack(spacing: Space.s) {
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 11)).foregroundStyle(Theme.muted)
                if SnapshotMode.enabled {
                    Text(browse.query.isEmpty ? "Search tasks or projects" : browse.query)
                        .workFont(.caption).foregroundStyle(Theme.muted)
                } else {
                    TextField("Search tasks or projects", text: $browse.query)
                        .textFieldStyle(.plain).workFont(.caption)
                        .accessibilityIdentifier("work.master.search")
                }
            }
            .padding(.horizontal, Space.m)
            .frame(maxWidth: .infinity, minHeight: 32)
            .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
            .overlay(
                RoundedRectangle(cornerRadius: Metrics.radius)
                    .strokeBorder(Theme.cardLine, lineWidth: Metrics.borderW)
            )

            // A narrowed column or a larger Reading size keeps the status
            // words and folds the sort to its glyph (its order stays in the
            // tooltip and accessibility value) instead of eliding both.
            ViewThatFits(in: .horizontal) {
                masterFilterRow(showsSortValue: true)
                masterFilterRow(showsSortValue: false)
            }
        }
    }

    private func masterFilterRow(showsSortValue: Bool) -> some View {
        HStack(spacing: Space.s) {
            WorkStatusMenu(group: $browse.group, identifier: "work.master.status")
            WorkSortMenu(sort: $browse.sort, identifier: "work.master.sort", showsValue: showsSortValue)
            Spacer(minLength: 0)
            DecisionLegendButton()
        }
    }

    private func retryCollection() {
        if browse.group == .attention {
            guard !isRetryingAttention else { return }
            isRetryingAttention = true
            Task {
                defer { isRetryingAttention = false }
                await dashboard.fetchAttention()
            }
        } else {
            Task { await dashboard.fetchReceipts() }
        }
    }

    private func masterEmpty(title: String, message: String?) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).workFont(.rowLabel).foregroundStyle(Theme.ink)
            if let message {
                Text(message).workFont(.caption).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(Space.l)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func moveSelection(
        _ direction: MoveCommandDirection,
        scrollProxy: ScrollViewProxy
    ) {
        guard direction == .up || direction == .down, !visibleTasks.isEmpty else { return }
        let current = visibleTasks.firstIndex { $0.taskId == selection.taskId }
        let nextIndex: Int
        switch direction {
        case .up: nextIndex = max(0, (current ?? 1) - 1)
        case .down: nextIndex = min(visibleTasks.count - 1, (current ?? -1) + 1)
        default: return
        }
        let task = visibleTasks[nextIndex]
        focusedTaskId = task.taskId
        selection.sessionId = nil
        selection.taskId = task.taskId
        withAnimation(reduceMotion ? nil : Motion.hover) {
            scrollProxy.scrollTo(task.taskId, anchor: .center)
        }
    }

    private func focusSelectedRowIfVisible() {
        guard !SnapshotMode.enabled,
              let taskId = selection.taskId,
              visibleTasks.contains(where: { $0.taskId == taskId }) else { return }
        DispatchQueue.main.async { focusedTaskId = taskId }
    }
}

private struct WorkMasterRow: View {
    let presentation: WorkReceiptRowPresentation
    let selected: Bool
    let focus: FocusState<String?>.Binding
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 7) {
                HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                    Text(presentation.title)
                        .workFont(size: 13, weight: selected ? .semibold : .regular, relativeTo: .body)
                        .foregroundStyle(Theme.ink)
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                        .help(presentation.title)
                    Spacer(minLength: 4)
                    DecisionBadge(
                        key: presentation.decisionKey,
                        label: presentation.decisionLabel,
                        compact: true
                    )
                    .help(presentation.decisionHelp)
                }

                HStack(spacing: 5) {
                    Text([presentation.clientText, presentation.projectText].compactMap { $0 }.joined(separator: " · "))
                        .lineLimit(1)
                    Spacer(minLength: 4)
                    Text(presentation.updatedText)
                }
                .workFont(.dataSmall).foregroundStyle(Theme.muted)

                if let reason = presentation.attentionReason, !reason.isEmpty {
                    Text(verbatim: reason)
                        .workFont(.caption).foregroundStyle(Theme.muted)
                        .lineLimit(1)
                        .help(reason)
                }
            }
            .padding(.horizontal, Space.l)
            .padding(.vertical, Space.m)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(selected ? Theme.selected : (focus.wrappedValue == presentation.taskId ? Theme.selected.opacity(0.45) : .clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(SurfaceButtonStyle(cornerRadius: 0, focusInset: 2))
        .focused(focus, equals: presentation.taskId)
        .focusable(selected)
        // The selection bar rides as an overlay so it can never stretch the row.
        .overlay(alignment: .leading) {
            if selected { Rectangle().fill(Theme.accent).frame(width: 4) }
        }
        .overlay(alignment: .bottom) {
            Rectangle().fill(Theme.hairline).frame(height: 1).padding(.leading, Space.l)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(presentation.accessibilityLabel)
        .accessibilityAddTraits(selected ? .isSelected : [])
        .accessibilityIdentifier("work.master.task.\(presentation.taskId)")
    }
}

// MARK: - Record page

/// A task prioritizes current status and recorded activity. Supporting ledgers
/// open only after the user asks for that category of detail.
struct WorkRecordPage: View {
    let receipt: Receipt
    let summary: ReceiptSummary?
    let refreshError: String?
    let isRefreshing: Bool
    let autoFocusEntry: Bool
    var timelineFocused = false
    var onToggleTimelineFocus: (() -> Void)? = nil
    @Environment(AppSelection.self) var selection
    @Environment(DashboardStore.self) var dashboard
    @Environment(\.workCompactViewport) private var compactViewport
    @Environment(\.savedWorkReconnect) private var reconnectSavedWork
    @FocusState private var backFocused: Bool
    @AccessibilityFocusState private var backAccessibilityFocused: Bool
    // The receipt-wide overview renders immediately; only the primary step
    // spine depends on this separate session load.
    @State private var sessionDetail: V1SessionDetail?
    @State private var sessionLoading = false
    @State private var sessionFailed = false
    @State private var sessionProjection: WorkProjectionMetadata?
    @State private var loadedPrimaryKey: String?

    var body: some View {
        ScrollViewReader { proxy in
            ScrollBox {
                VStack(alignment: .leading, spacing: 0) {
                    breadcrumb
                    if timelineFocused || compactViewport {
                        HStack(spacing: Space.s) {
                            Text(receipt.title ?? receipt.taskId).workFont(.titleCard).lineLimit(1)
                            DecisionBadge(key: receipt.axes.decisionStatus.key, label: receipt.axes.decisionStatus.label ?? receipt.axes.decisionStatus.key)
                        }.padding(.top, Space.s)
                    } else { titleBlock.padding(.top, Space.m) }
                    if let projection = dashboard.receiptProjection ?? receipt.projection {
                        WorkProjectionNotice(projection: projection, isOffline: dashboard.isOfflineSnapshot).padding(.top, Space.m)
                    }
                    if let refreshError {
                        staleDetailBanner(refreshError).padding(.top, Space.m)
                    }
                    ReceiptOverview(receipt: receipt)
                        .padding(.top, compactViewport ? Space.s : Space.l)
                    sectionNavigation(proxy: proxy).padding(.top, Space.m)
                    // Stable identities retain inspector and step state when
                    // Focus timeline changes the available viewport.
                    VStack(alignment: .leading, spacing: Space.xl) {
                        ForEach(orderedSections(proxy: proxy)) { section in
                            section.view.id("work.record.\(section.id)")
                        }
                    }
                    .padding(.top, compactViewport ? Space.l : Space.xl)
                }
                .padding(timelineFocused || compactViewport ? Space.m : Space.gutter)
                .frame(maxWidth: timelineFocused ? .infinity : 1172 + Space.gutter * 2, alignment: .leading)
                .frame(maxWidth: .infinity, alignment: .leading)
                .task(id: "\(primaryKey ?? "none"):\(receipt.projection?.generation ?? "legacy")") {
                    guard !SnapshotMode.enabled, let key = primaryKey else { return }
                    if loadedPrimaryKey != key {
                        sessionDetail = nil
                        sessionProjection = nil
                        sessionFailed = false
                        loadedPrimaryKey = key
                    }
                    await loadSessionSteps(for: key)
                    while !Task.isCancelled, primaryKey == key, sessionProjection?.needsRefresh == true {
                        do { try await Task.sleep(for: .seconds(3)) } catch { return }
                        guard !Task.isCancelled else { return }
                        await loadSessionSteps(for: key)
                    }
                }
            }
            .id(receipt.taskId)  // reset the drill-down's expansion state per Task
            .onAppear {
                guard autoFocusEntry, !SnapshotMode.enabled else { return }
                DispatchQueue.main.async {
                    backFocused = true
                    backAccessibilityFocused = true
                }
            }
        }
    }

    private func sectionNavigation(proxy: ScrollViewProxy) -> some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: Space.s) { sectionLinks(proxy: proxy) }
            VStack(alignment: .leading, spacing: Space.xs) {
                HStack(spacing: Space.s) {
                    sectionLink("Activity", section: "timeline", proxy: proxy)
                    sectionLink("Steps", section: "steps", proxy: proxy)
                    sectionLink("Checks", section: "checks", proxy: proxy)
                }
                HStack(spacing: Space.s) {
                    if !otherSessionMembers.isEmpty {
                        sectionLink("Other sessions", section: "subagents", proxy: proxy)
                    }
                    sectionLink("Usage & recording", section: "supporting", proxy: proxy)
                }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Explore this receipt")
        .accessibilityIdentifier("work.receipt.navigation")
    }

    @ViewBuilder
    private func sectionLinks(proxy: ScrollViewProxy) -> some View {
        sectionLink("Activity", section: "timeline", proxy: proxy)
        sectionLink("Steps", section: "steps", proxy: proxy)
        sectionLink("Checks", section: "checks", proxy: proxy)
        if !otherSessionMembers.isEmpty {
            sectionLink("Other sessions", section: "subagents", proxy: proxy)
        }
        sectionLink("Usage & recording", section: "supporting", proxy: proxy)
    }

    private func sectionLink(_ title: String, section: String, proxy: ScrollViewProxy) -> some View {
        Button { proxy.scrollTo("work.record.\(section)", anchor: .top) } label: {
            Label(title, systemImage: "arrow.down")
                .workFont(.captionSemibold)
        }
        .buttonStyle(QuietButtonStyle(horizontalPadding: 8))
        .accessibilityIdentifier("work.receipt.jump.\(section)")
    }

    /// The timeline stays inline and mounted; hiding the session list changes
    /// available space without changing timeline navigation state.
    private func timelineView(proxy: ScrollViewProxy) -> some View {
        WorkTimelineView(receipt: receipt,
            onRevealInspector: { proxy.scrollTo("work.timeline.inspector", anchor: .top) },
            onRevealRecords: { proxy.scrollTo("work.timeline.records", anchor: .top) },
            onRevealHeading: { proxy.scrollTo("work.timeline.heading", anchor: .top) })
    }

    /// The root of the primary group is the record's main narrative.
    private var primarySessionMember: ReceiptSessionMember? {
        guard let groups = receipt.sessions, let first = groups.first else { return nil }
        return first.members.first { $0.role == "root" } ?? first.members.first
    }

    /// Everything else — the primary group's subagents, then any continuation
    /// groups and their members — kept out of the spine and shown below.
    private var otherSessionMembers: [ReceiptSessionMember] {
        guard let groups = receipt.sessions else { return [] }
        var result: [ReceiptSessionMember] = []
        if let first = groups.first {
            let primaryID = primarySessionMember?.id
            result += first.members.filter { $0.id != primaryID }
        }
        for group in groups.dropFirst() { result += group.members }
        return result
    }

    /// The primary session's narrative is explicitly scoped. Readers choose
    /// which step to expand; failed outcomes remain visible in each row.
    private var stepsSection: some View {
        ReceiptSection(title: "Steps", identifier: "steps") {
            if let member = primarySessionMember {
                Text("Primary session · \(member.client)")
                    .workFont(.caption).foregroundStyle(Theme.muted)
                    .padding(.bottom, Space.s)
            }
            stepsContent
        }
    }

    private var checksSection: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            RecordChecksCard(evidence: receipt.dimensions.evidence, taskId: receipt.taskId,
                             initiallyShowsRoutineGroups: false)
            if let blocker = receipt.axes.decisionStatus.blocker, let text = blocker.text {
                DisclosureGroup {
                    BlockerCallout(blocker: blocker, taskId: receipt.taskId)
                        .padding(.top, Space.s)
                } label: {
                    VStack(alignment: .leading, spacing: Space.xs) {
                        Text("Recorded blocker").workFont(.captionSemibold).foregroundStyle(Theme.ink)
                        Text(verbatim: text).workFont(.caption).foregroundStyle(Theme.muted).lineLimit(2)
                    }
                }
                .workFont(.caption)
                .accessibilityIdentifier("work.receipt.blocker-context")
            }
        }
    }

    private var primaryKey: String? {
        primarySessionMember.map { "\($0.client)::\($0.clientSessionId)" }
    }

    private var effectiveSessionDetail: V1SessionDetail? {
        if let sessionDetail { return sessionDetail }
        if let key = primaryKey { return dashboard.preloadedSessions[key] }
        return nil
    }

    private func loadSessionSteps(for key: String) async {
        guard let member = primarySessionMember, primaryKey == key else { return }
        sessionLoading = true
        defer { if primaryKey == key { sessionLoading = false } }
        do {
            let detail = try await dashboard.loadSession(client: member.client, sessionId: member.clientSessionId)
            guard !Task.isCancelled, primaryKey == key else { return }  // a re-key superseded this load
            sessionDetail = detail
            sessionProjection = detail.projection
            sessionFailed = false
        } catch let pending as WorkProjectionPending {
            guard primaryKey == key, !Task.isCancelled else { return }
            sessionProjection = pending.projection.retainingBuild(from: sessionProjection)
            if pending.projection.available == false { sessionDetail = nil }
            sessionFailed = false
        } catch {
            // A cancelled (superseded) load must not strand the section on a
            // false failure; only the still-current member records a failure.
            guard primaryKey == key, !Task.isCancelled else { return }
            sessionProjection = .failed(error, retaining: sessionProjection ?? sessionDetail?.projection)
            sessionFailed = true
        }
    }

    /// The step spine, or an honest load / empty / failed / offline state.
    @ViewBuilder private var stepsContent: some View {
        if let projection = sessionProjection ?? effectiveSessionDetail?.projection {
            WorkProjectionNotice(projection: projection, isOffline: dashboard.isOfflineSnapshot)
        }
        if primarySessionMember == nil {
            Text("Session details aren't available for this receipt.")
                .workFont(.caption).foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
        } else if let detail = effectiveSessionDetail {
            if detail.steps.isEmpty {
                Text("No recorded steps are linked to this session.")
                    .workFont(.caption).foregroundStyle(Theme.muted)
            } else {
                let items = SessionStepItem.make(detail.steps)
                SessionStepSpine(items: items, openedIDs: [])
            }
        } else if dashboard.isOfflineSnapshot {
            stepsOfflineNotice
        } else if sessionFailed {
            stepsRetryRow
        } else {
            stepsLoadingRow
        }
    }

    private var stepsLoadingRow: some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            Text("Loading steps…").workFont(.caption).foregroundStyle(Theme.muted)
        }
        .accessibilityElement(children: .ignore).accessibilityLabel("Loading steps")
    }

    private var stepsRetryRow: some View {
        HStack(spacing: Space.s) {
            Text(sessionLoading ? "Retrying steps…" : "Steps couldn't be loaded.")
                .workFont(.caption).foregroundStyle(Theme.amber)
            Button {
                if !sessionLoading, let key = primaryKey { Task { await loadSessionSteps(for: key) } }
            } label: {
                Text(sessionLoading ? "Retrying…" : "Retry")
                    .workFont(.captionSemibold)
                    .frame(minWidth: ButtonFeedback.minimumHitDimension, minHeight: ButtonFeedback.minimumHitDimension)
                    .contentShape(Rectangle())
            }
            .buttonStyle(SurfaceButtonStyle(focusInset: 2)).disabled(sessionLoading)
        }
    }

    @ViewBuilder private var stepsOfflineNotice: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            Text("These steps weren't saved on this Mac. Reconnect the recorder to load them.")
                .workFont(.caption).foregroundStyle(Theme.amber)
            if let reconnectSavedWork {
                Button("Back to recovery", action: reconnectSavedWork)
                    .buttonStyle(QuietButtonStyle(horizontalPadding: 8))
            }
        }
    }

    /// The task's other sessions — subagents and continuations — below the
    /// timeline so they never bury the record; omitted when there are none.
    @ViewBuilder
    private var subagentsSection: some View {
        if !otherSessionMembers.isEmpty {
            let allSubagents = otherSessionMembers.allSatisfy { $0.role != "root" }
            ReceiptSection(title: allSubagents ? "Subagents" : "Sessions", identifier: "subagents") {
                RecordSubagentsSection(members: otherSessionMembers)
            }
        }
    }

    /// Stable identities keep the reader's timeline and disclosure state.
    private struct OrderedSection: Identifiable {
        let id: String
        let view: AnyView
    }

    /// Activity leads in both layouts; detailed steps and check evidence follow.
    private func orderedSections(proxy: ScrollViewProxy) -> [OrderedSection] {
        let steps = OrderedSection(id: "steps", view: AnyView(stepsSection))
        let checks = OrderedSection(id: "checks", view: AnyView(checksSection))
        let timeline = OrderedSection(id: "timeline", view: AnyView(timelineView(proxy: proxy)))
        let subagents = OrderedSection(id: "subagents", view: AnyView(subagentsSection))
        let supporting = OrderedSection(id: "supporting", view: AnyView(supportingSections))
        return [timeline, steps, checks, subagents, supporting]
    }

    /// Detailed usage and provenance stay available without dominating the
    /// initial task overview.
    private var supportingSections: some View {
        OverflowDisclosure(label: "Usage and recording details", identifier: "work.receipt.details") {
            VStack(alignment: .leading, spacing: Space.xl) {
                ReceiptSection(
                    title: "Usage", identifier: "usage",
                    help: "Counts describe captured tool calls, not progress or success. Related paths are recorded associations, not modified files. Current receipts have no ordered action ledger, so captured call counts cannot be linked to results or timing."
                ) {
                    RecordDimensionsCard(receipt: receipt, included: [.actions, .cost],
                                         showsProvenance: false, compactDigest: true)
                }
                ReceiptSection(title: "Recording", identifier: "recording",
                               help: receipt.axes.orthogonalityNote) {
                    recordingDetails
                }
            }
        }
        .accessibilityIdentifier("work.all-captured-details")
    }

    /// An unmistakable back control (the old caps "WORK" read as a static path
    /// label, not a button) + the path itself. Esc triggers the same return.
    private var breadcrumb: some View {
        HStack(spacing: Space.m) {
            backButton
            Spacer(minLength: 0)
            if let onToggleTimelineFocus, !compactViewport || timelineFocused {
                Button(action: onToggleTimelineFocus) {
                    Label(timelineFocused ? "Show task list" : "Focus timeline", systemImage: timelineFocused ? "sidebar.left" : "arrow.up.left.and.arrow.down.right")
                }.buttonStyle(QuietButtonStyle(horizontalPadding: 8))
                .accessibilityIdentifier("work.focus-timeline")
            }
        }
    }

    @ViewBuilder
    private var backButton: some View {
        // ImageRenderer blanks controls carrying AccessibilityFocusState.
        // The live path retains it; snapshots render the same visible button.
        if SnapshotMode.enabled {
            backButtonBase
        } else {
            backButtonBase.accessibilityFocused($backAccessibilityFocused)
        }
    }

    private var backButtonBase: some View {
        Button {
            selection.workBrowse.prepareReturnFocus(
                from: receipt.taskId,
                in: dashboard.receiptTasks,
                attention: dashboard.attention
            )
            selection.taskId = nil
            selection.sessionId = nil
        } label: {
            HStack(spacing: 4) {
                Image(systemName: "chevron.left")
                    .font(.system(size: 10, weight: .semibold))
                Text("All tasks").workFont(.captionSemibold)
            }
            .foregroundStyle(Theme.accent)
        }
        .buttonStyle(QuietButtonStyle(horizontalPadding: 6, verticalPadding: 3))
        .frame(minHeight: 24)
        .focused($backFocused)
        .keyboardShortcut(.cancelAction)
        .help("Back to all tasks (Esc)")
        .accessibilityIdentifier("work.breadcrumb.back")
    }

    private var titleBlock: some View {
        HStack(alignment: .top, spacing: Space.m) {
            SourceMonogram(client: primarySessionMember?.client ?? summary?.primaryRoot?.client, size: 40)
                .padding(.top, 2)
            VStack(alignment: .leading, spacing: Space.s) {
                HStack(alignment: .center, spacing: Space.m) {
                    Text(receipt.title ?? receipt.taskId)
                        .workFont(.titlePage).tracking(Type.titlePageTracking)
                        .foregroundStyle(Theme.ink)
                        .lineLimit(2)
                        .accessibilityAddTraits(.isHeader)
                    DecisionBadge(
                        key: receipt.axes.decisionStatus.key,
                        label: receipt.axes.decisionStatus.label ?? receipt.axes.decisionStatus.key
                    )
                    if let handoff = receipt.axes.handoff, handoff.handedOff == true,
                       receipt.axes.decisionStatus.key != "handed_off" {
                        Chip(text: "↗ handed off", tint: Theme.muted)
                    }
                    DecisionLegendButton()
                    Spacer()
                }
                if !metaLine.isEmpty {
                    Text(metaLine).workFont(.dataSmall).foregroundStyle(Theme.muted)
                }
            }
        }
    }

    private func staleDetailBanner(_ error: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Space.s) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(Theme.amber)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text("Showing saved receipt · refresh failed")
                    .workFont(.rowLabel).foregroundStyle(Theme.ink)
                Text(error).workFont(.caption).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: Space.m)
            if isRefreshing {
                ProgressView()
                    .controlSize(.small)
                    .accessibilityLabel("Retrying receipt refresh")
            } else {
                Button("Retry") {
                    Task { await dashboard.fetchReceipt(taskId: receipt.taskId) }
                }
                .buttonStyle(QuietButtonStyle(tint: Theme.accent))
                .workFont(.captionSemibold)
                .accessibilityIdentifier("work.receipt.stale.retry")
            }
        }
        .padding(Space.m)
        .background(Theme.amber.opacity(0.08), in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(
            RoundedRectangle(cornerRadius: Metrics.radius)
                .strokeBorder(Theme.amber.opacity(0.32), lineWidth: Metrics.borderW)
        )
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Showing a saved receipt because refresh failed. \(error)")
        .accessibilityIdentifier("work.receipt.stale")
    }

    private var metaLine: String {
        var parts: [String] = []
        if let client = primarySessionMember?.client ?? summary?.primaryRoot?.client { parts.append(client) }
        if let model = receipt.dimensions.actors.models?.first, !model.isEmpty { parts.append(model) }
        if let secs = receipt.durationSeconds, secs > 0 { parts.append("ran \(durationText(secs))") }
        if let ago = agoText(summary?.lastActivityAt) { parts.append("updated \(ago)") }
        return parts.joined(separator: " · ")
    }

    private var topicDivider: some View {
        Rectangle().fill(Theme.hairline).frame(height: 1)
    }

    /// Identity and provenance in one place: task and agent facts, sources and
    /// gaps. Claim coverage is already visible in the receipt overview.
    private var recordingDetails: some View {
        VStack(alignment: .leading, spacing: 0) {
            RecordDimensionsCard(receipt: receipt, included: [.task, .agents],
                                 showsProvenance: false, showsGaps: false)
            receiptFactRow("Sources") {
                let sources = receipt.dimensions.provenance.sourcesPresent ?? []
                if sources.isEmpty {
                    Text("not recorded").workFont(.body).foregroundStyle(Theme.muted)
                } else {
                    HStack(spacing: 6) {
                        // Each chip's legend sentence is the daemon's own, shown
                        // on hover; the chip alone still names the source.
                        ForEach(sources, id: \.self) { source in
                            if let definition = receipt.dimensions.provenance.legend?[source],
                               !definition.isEmpty {
                                ProvenanceChip(text: source).help(definition)
                            } else {
                                ProvenanceChip(text: source)
                            }
                        }
                    }
                }
            }
            let gapsDim = receipt.dimensions.gaps
            receiptFactRow("Gaps") {
                let items = gapsDim.items ?? []
                let count = gapsDim.count ?? items.count
                if count == 0 {
                    Text("no recorded gaps").workFont(.body).foregroundStyle(Theme.muted)
                } else if items.isEmpty {
                    Text("\(count) recorded gap\(count == 1 ? "" : "s") · details not included")
                        .workFont(.caption).foregroundStyle(Theme.amber)
                } else {
                    // Detailed gaps beyond the third fold into one counted
                    // trigger; any gaps counted without detail are named too, so
                    // the remaining total is honest whichever form the extras take.
                    let undetailed = max(count - items.count, 0)
                    VStack(alignment: .leading, spacing: 4) {
                        // Index-stable identity: two gaps sharing a dimension and
                        // reason must both render, never collapse into one row.
                        ForEach(Array(items.prefix(3).enumerated()), id: \.offset) { _, item in gapRow(item) }
                        if items.count > 3 {
                            let remaining = (items.count - 3) + undetailed
                            OverflowDisclosure(
                                label: "\(remaining) more gap\(remaining == 1 ? "" : "s")",
                                identifier: "work.overflow.gaps"
                            ) {
                                VStack(alignment: .leading, spacing: 4) {
                                    ForEach(Array(items.dropFirst(3).enumerated()), id: \.offset) { _, item in gapRow(item) }
                                    if undetailed > 0 {
                                        Text("\(undetailed) more recorded gap\(undetailed == 1 ? "" : "s") · details not included")
                                            .workFont(.caption).foregroundStyle(Theme.amber)
                                    }
                                }
                                .padding(.top, 4)
                            }
                            .padding(.top, 2)
                        } else if undetailed > 0 {
                            // Same amber as every other gap fact: a counted-but-
                            // undetailed gap is still a gap.
                            Text("\(undetailed) more recorded gap\(undetailed == 1 ? "" : "s") · details not included")
                                .workFont(.caption).foregroundStyle(Theme.amber)
                        }
                    }
                }
            }
            receiptFactRow("Task ID") {
                CopyableValue(text: receipt.taskId, announce: "task ID")
            }
        }
    }

    /// One recorded gap: the hollow amber pip carries the tier, the dimension
    /// names where the blind spot is, and the reason states it.
    private func gapRow(_ item: ReceiptGapItem) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            EvidencePip(shape: .hollow, tint: Theme.amber)
            Text(item.dimension).workFont(.captionSemibold).foregroundStyle(Theme.muted)
                .frame(width: 70, alignment: .leading)
            Text(item.reason).workFont(.caption).foregroundStyle(Theme.amber)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func receiptFactRow<Content: View>(_ label: String, @ViewBuilder content: () -> Content) -> some View {
        HStack(alignment: .top, spacing: Space.l) {
            CapsLabel(text: label)
                .frame(width: 104, alignment: .leading)
                .padding(.top, 3)
            content()
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.vertical, Space.m)
        .overlay(alignment: .top) { topicDivider }
    }
}

/// The task's other sessions — the primary group's subagents and any
/// continuation sessions — each an expandable row that loads its own steps on
/// demand. Kept out of the Steps spine and below the timeline so a task with
/// many subagents never buries the record; a short preview shows first, the
/// rest fold under one counted trigger.
struct RecordSubagentsSection<Row: View>: View {
    let members: [ReceiptSessionMember]
    /// Opens the overflow fold on first render, for deterministic renders and
    /// tests; the live record always starts with it closed.
    var overflowInitiallyExpanded = false
    /// Builds one session's row. The record uses `SessionDrillRow`; a test
    /// substitutes a row that reports when it is built.
    let row: (ReceiptSessionMember) -> Row
    static var previewLimit: Int { 6 }

    private var preview: [ReceiptSessionMember] { Array(members.prefix(Self.previewLimit)) }
    private var overflow: [ReceiptSessionMember] { Array(members.dropFirst(Self.previewLimit)) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(preview.enumerated()), id: \.element.id) { index, member in
                row(member)
                if index < preview.count - 1 { hairline }
            }
            if !overflow.isEmpty {
                hairline
                OverflowDisclosure(
                    label: "\(overflow.count) more session\(overflow.count == 1 ? "" : "s")",
                    identifier: "work.overflow.subagents",
                    initiallyExpanded: overflowInitiallyExpanded
                ) {
                    // A task can carry hundreds of sessions; building every row
                    // when the fold opens stalls the window, so only the rows
                    // scrolled into view are built.
                    ScrollContentStack(alignment: .leading, spacing: 0) {
                        ForEach(Array(overflow.enumerated()), id: \.element.id) { index, member in
                            row(member)
                            if index < overflow.count - 1 { hairline }
                        }
                    }
                }
                .padding(.top, 6)
            }
        }
    }

    private var hairline: some View {
        Rectangle().fill(Theme.hairline).frame(height: 1).padding(.vertical, 2)
    }
}

extension RecordSubagentsSection where Row == SessionDrillRow {
    init(members: [ReceiptSessionMember], overflowInitiallyExpanded: Bool = false) {
        self.init(members: members, overflowInitiallyExpanded: overflowInitiallyExpanded) {
            SessionDrillRow(member: $0)
        }
    }
}

/// A caps eyebrow paired with a hairline rule: the visible header for one
/// section of the receipt document. Static — a heading, never a focus stop.
/// The optional help button is the section's one place for explanation.
private struct SectionHeader: View {
    let title: String
    var help: String? = nil
    let identifier: String

    var body: some View {
        HStack(spacing: Space.m) {
            HStack(spacing: Space.m) {
                CapsLabel(text: title)
                Rectangle().fill(Theme.hairline).frame(height: 1)
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel(title)
            .accessibilityAddTraits(.isHeader)
            .accessibilityIdentifier("work.section.\(identifier)")
            if let help {
                ContextHelp(title: "About \(title.lowercased())", message: help,
                            identifier: "work.section.\(identifier).help")
            }
        }
    }
}

/// One section of the visible receipt document: a header rule, then its rows.
/// There is no fold — the section's facts are always shown; only a genuinely
/// long list inside uses an `OverflowDisclosure`.
private struct ReceiptSection<Content: View>: View {
    let title: String
    let identifier: String
    var help: String? = nil
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHeader(title: title, help: help, identifier: identifier)
                .padding(.bottom, Space.s)
            content()
        }
    }
}

/// The only fold in the document: a quiet, counted trigger that reveals a
/// genuinely long list in place. One level deep — its content never folds
/// again. Muted until hovered (then ink); the chevron carries the affordance
/// and nudges on hover. Internal so the Usage digest can reuse it.
struct OverflowDisclosure<Content: View>: View {
    let label: String
    let identifier: String?
    let content: () -> Content
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var expanded: Bool
    @State private var hovering = false

    init(
        label: String,
        identifier: String? = nil,
        initiallyExpanded: Bool = false,
        @ViewBuilder content: @escaping () -> Content
    ) {
        self.label = label
        self.identifier = identifier
        self.content = content
        _expanded = State(initialValue: initiallyExpanded)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            Button {
                withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.15)) { expanded.toggle() }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 10, weight: .semibold))
                        .rotationEffect(.degrees(expanded ? 90 : 0))
                        .offset(x: hovering && !expanded ? 1 : 0)
                    Text(expanded ? "Show less" : label)
                        .workFont(.captionSemibold)
                }
                .foregroundStyle(hovering || expanded ? Theme.ink : Theme.muted)
                .padding(.vertical, 6).padding(.horizontal, 4)
                .frame(minHeight: 24, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(SurfaceButtonStyle())
            .onHover { inside in
                withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.12)) { hovering = inside }
            }
            .accessibilityLabel(label)
            .accessibilityValue(expanded ? "Expanded" : "Collapsed")
            .accessibilityIdentifier(identifier ?? "work.overflow")
            if expanded { content() }
        }
    }
}

/// A monospaced identifier the reader can copy. The value stays selectable; a
/// copy glyph fades in on hover and is a keyboard focus stop of its own,
/// turning to a checkmark for 1.5 s with a VoiceOver announcement on copy.
private struct CopyableValue: View {
    let text: String
    /// What was copied, for the tooltip and the announcement ("task ID").
    let announce: String
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var hovering = false
    @State private var copied = false
    @FocusState private var focused: Bool

    var body: some View {
        HStack(spacing: Space.s) {
            Text(text)
                .workFont(.dataSmall)
                .foregroundStyle(Theme.muted)
                .textSelection(.enabled)
            Button(action: copy) {
                Image(systemName: copied ? "checkmark" : "doc.on.doc")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(copied ? Theme.green : Theme.accent)
                    .frame(minWidth: 24, minHeight: 24)
                    .contentShape(Rectangle())
            }
            .buttonStyle(SurfaceButtonStyle())
            .focused($focused)
            // Revealed on hover, while focused (a sighted keyboard user must
            // see the stop they landed on and its checkmark), and after a copy.
            .opacity(hovering || focused || copied ? 1 : 0)
            .help("Copy \(announce)")
            .accessibilityLabel(copied ? "Copied \(announce)" : "Copy \(announce)")
            Spacer(minLength: 0)
        }
        .onHover { inside in
            withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.12)) { hovering = inside }
        }
    }

    private func copy() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.12)) { copied = true }
        if !SnapshotMode.enabled, let window = NSApp.keyWindow ?? NSApp.mainWindow {
            NSAccessibility.post(
                element: window,
                notification: .announcementRequested,
                userInfo: [
                    .announcement: "Copied \(announce)",
                    .priority: NSAccessibilityPriorityLevel.high.rawValue,
                ]
            )
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.12)) { copied = false }
        }
    }
}

/// The short, distinguishing tail of an opaque session identity.
func sessionDistinguishingID(_ clientSessionId: String) -> String {
    if let last = clientSessionId.split(separator: ":").last, last.count < clientSessionId.count {
        return String(last)
    }
    return clientSessionId
}

// MARK: - Session drill-down

struct SessionDrillAccessibilityPresentation {
    let title: String
    let distinguishingId: String
    let project: String?
    let role: String?
    let sessionKind: String?
    let expanded: Bool
    let detailSummary: String?
    let loading: Bool
    let failed: Bool
    let lastActivity: String?

    var label: String {
        var parts = [title, "session \(distinguishingId)"]
        if let project = nonempty(project) { parts.append("project \(project)") }
        return parts.joined(separator: ", ")
    }

    var value: String {
        var parts = [expanded ? "Expanded" : "Collapsed"]
        switch role {
        case "subagent": parts.append(nonempty(sessionKind) ?? "subagent")
        case "root": parts.append("root")
        default: parts.append("role unknown")
        }
        if let detailSummary = nonempty(detailSummary) { parts.append(detailSummary) }
        if let lastActivity = nonempty(lastActivity) { parts.append("updated \(lastActivity)") }
        if loading {
            parts.append(failed ? "Retrying session steps" : "Loading session steps")
        } else if failed {
            parts.append("Session steps unavailable")
        }
        return parts.joined(separator: ", ")
    }

    private func nonempty(_ value: String?) -> String? {
        guard let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines),
              !trimmed.isEmpty
        else { return nil }
        return trimmed
    }
}

/// One session in the drill-down: a header row (role/kind + title) that expands
/// to lazily load `/v1/session` and render its steps — reusing StepCard — and
/// its subagent sessions. Each row owns its own loaded detail (the shared store
/// slot would clobber across several expanded sessions).
struct SessionDrillRow: View {
    let member: ReceiptSessionMember
    let initiallyExpanded: Bool
    @Environment(DashboardStore.self) var dashboard
    @Environment(\.savedWorkReconnect) private var reconnectSavedWork
    @State private var expanded: Bool
    @State private var detail: V1SessionDetail?
    @State private var loading = false
    @State private var failed = false
    @State private var detailProjection: WorkProjectionMetadata?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    init(
        member: ReceiptSessionMember,
        initiallyExpanded: Bool = false,
        initiallyLoading: Bool = false,
        initiallyFailed: Bool = false
    ) {
        self.member = member
        self.initiallyExpanded = initiallyExpanded
        _expanded = State(initialValue: initiallyExpanded)
        _loading = State(initialValue: initiallyLoading)
        _failed = State(initialValue: initiallyFailed)
    }

    /// The lazily-loaded detail, or a snapshot-preloaded one. The load guards
    /// use this value so deterministic renderers never start a redundant task.
    private var effectiveDetail: V1SessionDetail? {
        detail ?? dashboard.preloadedSessions["\(member.client)::\(member.clientSessionId)"]
    }

    private var label: String {
        if let title = member.title, !title.isEmpty { return title }
        if let loaded = effectiveDetail?.session.displayTitle, !loaded.isEmpty { return loaded }
        return "\(member.client) · \(distinguishingId)"
    }

    /// Subagent ids share the root's uuid prefix ("<root>:agent-<id>"), so the
    /// DISTINGUISHING part is the component after the last colon — a row list
    /// where every child shows the parent's prefix identifies nothing.
    private var distinguishingId: String {
        let id = member.clientSessionId
        if let last = id.split(separator: ":").last, last.count < id.count {
            return String(last.prefix(16))
        }
        return String(id.prefix(8))
    }

    private var detailSummary: String? {
        guard let detail = effectiveDetail else { return nil }
        let checkDigest = StepCheckDigest(checks: detail.steps.flatMap { $0.checks ?? [] })
        var parts = ["\(detail.steps.count) step\(detail.steps.count == 1 ? "" : "s")"]
        if !checkDigest.all.isEmpty { parts.append(checkDigest.summary) }
        return parts.joined(separator: " · ")
    }

    private var roleLabel: String {
        switch member.role {
        case "subagent": return member.sessionKind ?? "subagent"
        case "root": return "root"
        default: return "role unknown"
        }
    }

    private var accessibilityPresentation: SessionDrillAccessibilityPresentation {
        SessionDrillAccessibilityPresentation(
            title: label,
            distinguishingId: distinguishingId,
            project: member.project,
            role: member.role,
            sessionKind: member.sessionKind,
            expanded: expanded,
            detailSummary: detailSummary,
            loading: loading,
            failed: failed,
            lastActivity: agoText(member.lastActivityAt)
        )
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                expanded.toggle()
            } label: {
                VStack(alignment: .leading, spacing: Space.xs) {
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: expanded ? "chevron.down" : "chevron.forward")
                            .font(.system(size: 8, weight: .semibold))
                            .foregroundStyle(Theme.muted)
                            .frame(width: 10, height: 18)
                        Text(label)
                            .workFont(.body)
                            .foregroundStyle(Theme.ink)
                            .lineLimit(expanded ? nil : 2)
                            .fixedSize(horizontal: false, vertical: expanded)
                            .layoutPriority(1)
                        Spacer(minLength: 8)
                        if let detailSummary {
                            Text(detailSummary)
                                .workFont(.dataSmall)
                                .foregroundStyle(Theme.muted)
                        }
                    }
                    HStack(spacing: 8) {
                        Chip(
                            text: roleLabel,
                            tint: member.role == "root" ? Theme.accent : Theme.muted
                        )
                        if let project = member.project, member.role == "root" {
                            Text(project).workFont(.caption).foregroundStyle(Theme.muted)
                        }
                        if let ago = agoText(member.lastActivityAt) {
                            Text(ago).workFont(.dataSmall).foregroundStyle(Theme.muted)
                        }
                    }
                    .padding(.leading, 18)
                }
                .padding(.horizontal, 10).padding(.vertical, 7).contentShape(Rectangle())
            }
            .buttonStyle(SurfaceButtonStyle(focusInset: 2))
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(accessibilityPresentation.label)
            .accessibilityValue(accessibilityPresentation.value)
            .accessibilityHint(expanded ? "Hides session steps" : "Shows session steps")

            if expanded {
                expandedBody
                    .padding(.horizontal, 12).padding(.bottom, 10).padding(.leading, 6)
                    .transition(.opacity)
            }
        }
        .animation(reduceMotion ? nil : Motion.contentUpdate, value: expanded)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Theme.cardLine, lineWidth: Metrics.borderW))
        .task(id: "\(expanded):\(member.id):\(dashboard.receiptProjection?.generation ?? "legacy")") {
            guard expanded, !SnapshotMode.enabled else { return }
            await load()
            while !Task.isCancelled, expanded, detailProjection?.needsRefresh == true {
                do { try await Task.sleep(for: .seconds(3)) } catch { return }
                guard !Task.isCancelled else { return }
                await load()
            }
        }
    }

    @ViewBuilder
    private var expandedBody: some View {
        if let projection = detailProjection ?? effectiveDetail?.projection {
            WorkProjectionNotice(projection: projection, isOffline: dashboard.isOfflineSnapshot)
        }
        if let detail = effectiveDetail {
            VStack(alignment: .leading, spacing: 6) {
                if let savedAt = dashboard.sessionSavedAt(client: member.client, sessionID: member.clientSessionId) {
                    Text("\(detail.projection == nil ? "Session copy saved" : "Session evidence as of"): \(savedAt.ISO8601Format())")
                        .workFont(.caption).foregroundStyle(Theme.muted).textSelection(.enabled)
                }
                if detail.steps.isEmpty {
                    Text("No recorded steps are linked to this session.")
                        .workFont(.caption)
                        .foregroundStyle(Theme.muted)
                } else {
                    let stepItems = SessionStepItem.make(detail.steps)
                    // A snapshot opens the first couple of check-bearing steps
                    // (so their check evidence — command + exit code — shows) plus
                    // one un-checked step (so an honest "no passing check" step is
                    // visible too); the live app opens every step collapsed.
                    let opened: Set<String> = SnapshotMode.enabled
                        ? SessionStepItem.snapshotOpenedIDs(stepItems)
                        : []
                    ScrollContentStack(alignment: .leading, spacing: 6) {
                        ForEach(stepItems) { item in
                            StepCard(
                                step: item.step,
                                initiallyExpanded: opened.contains(item.id),
                                accessibilityContext: item.id
                            )
                        }
                    }
                }
                // The Task's subagents are already listed once, flat and
                // expandable, as sibling SessionDrillRows in this group's member
                // list. Re-listing this session's descendants here showed the
                // same subagents a second time (a confusing "41 and 41"), so the
                // member list is the single source of truth for the tree.
            }
        } else if dashboard.isOfflineSnapshot {
            VStack(alignment: .leading, spacing: Space.s) {
                Text("This session detail was not saved on this Mac. Reconnect the recorder to load it.")
                    .workFont(.caption).foregroundStyle(Theme.amber)
                if let reconnectSavedWork {
                    Button("Back to recovery", action: reconnectSavedWork)
                        .buttonStyle(QuietButtonStyle(horizontalPadding: 8))
                }
            }
        } else if failed {
            HStack(spacing: Space.s) {
                Text(loading ? "Retrying session steps…" : "Session steps couldn't be loaded.")
                    .workFont(.caption)
                    .foregroundStyle(Theme.amber)
                Button {
                    if !loading { Task { await load() } }
                } label: {
                    Text(loading ? "Retrying…" : "Retry")
                        .workFont(.captionSemibold)
                        .frame(
                            minWidth: ButtonFeedback.minimumHitDimension,
                            minHeight: ButtonFeedback.minimumHitDimension
                        )
                        .contentShape(Rectangle())
                }
                .buttonStyle(SurfaceButtonStyle(focusInset: 2))
                .disabled(loading)
                .accessibilityHint(loading ? "Retry is in progress" : "Loads this session's steps again")
            }
        } else if loading {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("Loading session steps…").workFont(.caption).foregroundStyle(Theme.muted)
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Loading session steps")
        }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            let loaded = try await dashboard.loadSession(client: member.client, sessionId: member.clientSessionId)
            guard !Task.isCancelled else { return }
            detail = loaded
            detailProjection = loaded.projection
            failed = false
        } catch let pending as WorkProjectionPending {
            guard !Task.isCancelled else { return }
            detailProjection = pending.projection.retainingBuild(from: detailProjection)
            if pending.projection.available == false { detail = nil }
            failed = false
        } catch {
            guard !requestWasCancelled(error, taskIsCancelled: Task.isCancelled) else { return }
            detailProjection = .failed(error, retaining: detailProjection ?? detail?.projection)
            failed = true
        }
    }
}
