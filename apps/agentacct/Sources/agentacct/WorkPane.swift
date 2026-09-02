import SwiftUI

// The Work surface — one receipts collection in three adaptive presentations:
//
// * No Task selected → the full comparison table.
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
    in tasks: [ReceiptSummary]
) -> WorkSessionResolution {
    if let match = tasks.first(where: { $0.primaryRoot?.sessionKey == sessionId }) {
        return .task(match.taskId)
    }
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
    case attention, latest, cost
    var id: String { rawValue }

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

    func visibleTasks(in tasks: [ReceiptSummary]) -> [ReceiptSummary] {
        visibleWorkReceipts(tasks, query: query, group: group, sort: sort)
    }

    func prepareReturnFocus(from taskId: String?, in tasks: [ReceiptSummary]) {
        let visible = visibleTasks(in: tasks)
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
    return "\(visible) of \(total) receipts"
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

/// One semantic row contract for both the comparison table and compact master.
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
    let costText: String
    let updatedText: String
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
        updatedText = agoText(task.lastActivityAt) ?? "no activity"
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
        parts.append(costText)
        parts.append("updated \(updatedText)")
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
            detail = "The complete attention projection reports no failed checks, failed steps, or unresolved blockers."
        } else if !query.isEmpty, !payload.items.isEmpty {
            title = "No review items match this filter"
            detail = "The bounded queue has \(payload.items.count) of \(payload.total) review items; adjust the filter to inspect them."
        } else {
            title = "Review queue details unavailable"
            detail = "The complete projection reports \(payload.total) review items, but no bounded queue rows were returned. Refresh before acting."
        }
    }
}

/// Shared ordering for the receipts table and master — one algorithm, so the
/// two surfaces can never disagree. `.latest` is the daemon's own order
/// (last_activity_at desc); `.attention` is a stable partition that keeps that
/// recency inside each half.
func sortedReceipts(_ rows: [ReceiptSummary], by sort: WorkSort) -> [ReceiptSummary] {
    switch sort {
    case .attention:
        let attention = rows.filter { WorkGroup.forTask($0) == .attention }
        return attention + rows.filter { WorkGroup.forTask($0) != .attention }
    case .latest:
        return rows  // server order: recency
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

/// A small info affordance that opens the decision-word legend. Lives beside
/// every surface that shows decision words (table controls, record title).
struct DecisionLegendButton: View {
    @State private var shown = false

    var body: some View {
        // Popovers need live interaction; the offscreen renderer draws the
        // trigger as noise, so snapshots omit the control entirely.
        if !SnapshotMode.enabled {
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
                    Text("Words come from the daemon's receipt — the legend explains, it never re-grades.")
                        .workFont(.dataSmall).foregroundStyle(Theme.muted)
                        .padding(.top, Space.xs)
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
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @State private var unresolvedSessionId: String?

    private var selectionKey: String {
        if let taskId = selection.taskId { return "task:\(taskId)" }
        if let sessionId = selection.sessionId { return "session:\(sessionId)" }
        return "list"
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
                hasSelection: selection.taskId != nil || unresolvedSessionId != nil
            )
            Group {
                switch mode {
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
        }
        .animation(
            reduceMotion ? Motion.reducedCrossfade : Motion.detailNavigation,
            value: selectionKey
        )
        .task(id: selectionKey) {
            // The fixture renderer injects the exact Work state under review.
            // Starting a live fetch here would immediately clear an injected
            // error and collapse error/loading snapshots into the same frame.
            guard !SnapshotMode.enabled else { return }
            await resolveSelection()
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
            if SnapshotMode.enabled {
                HStack(alignment: .top, spacing: 0) {
                    WorkMasterList(browse: selection.workBrowse)
                        .frame(width: 368)
                    Rectangle().fill(Theme.rule).frame(width: 1)
                    recordDetail(autoFocusEntry: false)
                        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                }
            } else {
                HSplitView {
                    WorkMasterList(browse: selection.workBrowse)
                        .frame(minWidth: 320, idealWidth: 368, maxWidth: 480)
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
        if let taskId = selection.taskId {
            await dashboard.fetchReceipt(taskId: taskId)
            return
        }
        guard let sessionId = selection.sessionId else { return }
        switch workSessionResolution(for: sessionId, in: dashboard.receiptTasks) {
        case .task(let taskId):
            selection.taskId = taskId
            selection.sessionId = nil
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
                        autoFocusEntry: autoFocusEntry
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
                        retryTitle: dashboard.receiptLoadingTaskId == taskId ? nil : "Retry",
                        showsProgress: dashboard.receiptLoadingTaskId == taskId,
                        autoFocusEntry: autoFocusEntry
                    ) {
                        Task { await dashboard.fetchReceipt(taskId: taskId) }
                    }
                } else if let unresolvedSessionId, selection.sessionId == unresolvedSessionId {
                    WorkRecordPlaceholder(
                        title: "Task link unavailable",
                        message: "This active session is not identified in the task summary. Select its Task from the collection to inspect the full receipt.",
                        symbol: "arrow.triangle.branch",
                        autoFocusEntry: autoFocusEntry
                    )
                    .accessibilityIdentifier("work.unresolved-session")
                } else if selection.taskId != nil {
                    WorkRecordPlaceholder(
                        title: "Loading receipt",
                        message: "Fetching the latest evidence and check results…",
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
                in: dashboard.receiptTasks
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
                    tabs(
                        groupCounts: presentation.groupCounts,
                        visibleCount: presentation.visibleTasks.count
                    )
                    .padding(.top, Space.xl)
                    filterRow.padding(.top, Space.m)
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
                    legend.padding(.top, Space.l)
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
            Text("Work receipts")
                .workFont(.titlePage).tracking(Type.titlePageTracking)
                .foregroundStyle(Theme.ink)
                .accessibilityAddTraits(.isHeader)
            HStack(spacing: 0) {
                Text(receiptCollectionHeaderText)
                    .workFont(.dataSmall).foregroundStyle(Theme.muted)
                if let updated = dashboard.receiptListLastUpdated {
                    Text(" · refreshed \(dashboardFreshnessText(updated))")
                        .workFont(.dataSmall).foregroundStyle(Theme.muted)
                }
            }
        }
    }

    private func tabs(
        groupCounts: [WorkGroup: Int],
        visibleCount: Int
    ) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            if dynamicTypeSize.isAccessibilitySize {
                HStack(spacing: Space.m) {
                    if SnapshotMode.enabled {
                        Chip(text: browse.group?.rawValue ?? "All statuses", tint: Theme.accent)
                    } else {
                        Picker("Lifecycle", selection: $browse.group) {
                            Text("All statuses").tag(nil as WorkGroup?)
                            ForEach(WorkGroup.allCases) { group in
                                Text(group.rawValue).tag(Optional(group))
                            }
                        }
                        .pickerStyle(.menu)
                        .accessibilityIdentifier("work.table.status")
                    }
                    Text("\(visibleCount) shown")
                        .workFont(.dataSmall).foregroundStyle(Theme.muted)
                    Spacer(minLength: 0)
                }
                .padding(.bottom, Space.s)
            } else {
                HStack(spacing: Space.xl) {
                    let partialOrUnknown = workReceiptCollectionIsPartial(
                        loaded: dashboard.receiptTasks.count,
                        total: dashboard.totalReceiptTasks,
                        truncated: dashboard.receiptTasksTruncated
                    ) || dashboard.totalReceiptTasks == nil
                    tabButton(nil, label: partialOrUnknown ? "Loaded" : "All", count: dashboard.receiptTasks.count)
                    ForEach(WorkGroup.allCases) { candidate in
                        let count: Int? = candidate == .attention
                            ? dashboard.attention?.total
                            : (groupCounts[candidate] ?? 0)
                        // Attention is complete across the store and can exceed
                        // Loaded; other lifecycle counts describe that page.
                        // "Other" appears only for an unmapped decision key.
                        if candidate != .other || (count ?? 0) > 0 {
                            tabButton(candidate, label: candidate.rawValue, count: count)
                        }
                    }
                }
            }
            Rectangle().fill(Theme.hairline).frame(height: 1)
        }
    }

    private func tabButton(_ candidate: WorkGroup?, label: String, count: Int?) -> some View {
        let active = browse.group == candidate
        return Button {
            browse.group = candidate
        } label: {
            VStack(spacing: 0) {
                HStack(spacing: 6) {
                    Text(label)
                        .workFont(size: 13, weight: active ? .semibold : .medium, relativeTo: .body)
                        .foregroundStyle(active ? Theme.accent : Theme.ink)
                    Text(count.map(String.init) ?? "—")
                        .workFont(.dataSmall)
                        .foregroundStyle(candidate == .attention && (count ?? 0) > 0 ? Theme.coral : Theme.muted)
                }
                .padding(.bottom, 10)
                Rectangle()
                    .fill(active ? Theme.accent : .clear)
                    .frame(height: 2)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(SurfaceButtonStyle())
        .accessibilityAddTraits(active ? .isSelected : [])
        .accessibilityIdentifier("work.tab.\(label.lowercased().replacingOccurrences(of: " ", with: "-"))")
    }

    private var filterRow: some View {
        HStack(spacing: Space.m) {
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 11)).foregroundStyle(Theme.muted)
                if SnapshotMode.enabled {
                    // ImageRenderer draws a TextField / .menu Picker as a yellow
                    // placeholder; a snapshot shows plain stand-ins instead.
                    Text("Filter by task, client, or id").workFont(.caption).foregroundStyle(Theme.muted)
                } else {
                    TextField("Filter by task, client, or id", text: $browse.query)
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
            if SnapshotMode.enabled {
                Chip(text: "sort: \(browse.sort.rawValue)", tint: Theme.accent)
            } else {
                Picker("Sort", selection: $browse.sort) {
                    ForEach(WorkSort.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.menu)
                .fixedSize()
            }
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
                   dashboard.isLoadingReceipts,
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
                            Text("Loading receipts").workFont(.rowLabel).foregroundStyle(Theme.ink)
                            Text("Reading the latest recorded work from the local store.")
                                .workFont(.caption).foregroundStyle(Theme.muted)
                        }
                    }
                    .padding(Space.xl)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("Loading receipts from the local store")
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
                        Text("Checking the complete review projection…")
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
            CapsLabel(text: "Task").frame(maxWidth: .infinity, alignment: .leading)
            CapsLabel(text: "Claims supported").frame(width: 150, alignment: .leading)
            CapsLabel(text: "Client").frame(width: 124, alignment: .leading)
            CapsLabel(text: "Check runs").frame(width: 130, alignment: .trailing)
            CapsLabel(text: "Est. cost").frame(width: 76, alignment: .trailing)
            CapsLabel(text: "Updated").frame(width: 72, alignment: .trailing)
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
        ) + " · \(browse.sort.footerText)"
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
        return "Adjust the lifecycle tab or the filter to broaden the result."
    }

    private var receiptCollectionHeaderText: String {
        if dashboard.isLoadingReceipts, dashboard.receiptTasks.isEmpty {
            return "local store · loading receipts"
        }
        if let total = dashboard.totalReceiptTasks {
            if workReceiptCollectionIsPartial(
                loaded: dashboard.receiptTasks.count,
                total: total,
                truncated: dashboard.receiptTasksTruncated
            ) {
                return "local store · \(total) receipts · latest \(dashboard.receiptTasks.count) loaded"
            }
            return "local store · \(total) receipts"
        }
        let suffix = dashboard.receiptTasksTruncated == true ? " · more may exist" : ""
        return "local store · \(dashboard.receiptTasks.count) loaded · total not reported\(suffix)"
    }

    private var legend: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: Space.l) {
                legendItems
                Spacer()
                Text("Evidence counts checkable steps · claim ≠ proof")
                    .workFont(.dataSmall).foregroundStyle(Theme.muted)
            }
            VStack(alignment: .leading, spacing: Space.s) {
                HStack(spacing: Space.l) { legendItems }
                Text("Evidence counts checkable steps · claim ≠ proof")
                    .workFont(.dataSmall).foregroundStyle(Theme.muted)
            }
        }
    }

    @ViewBuilder
    private var legendItems: some View {
        ForEach(
            ["externally_verified", "independently_checked", "self_checked", "unchecked"],
            id: \.self
        ) { grade in
            let style = EvidenceTierStyle.forGrade(grade)
            HStack(spacing: 6) {
                EvidencePip(shape: style.pip, tint: style.tint)
                // The amber hollow pip carries both approved words (the
                // aggregate "unchecked" and the per-step grade "claimed").
                Text(grade == "unchecked" ? "unchecked · claimed" : style.label)
                    .workFont(.dataSmall).foregroundStyle(Theme.muted)
                }
            }
        HStack(spacing: 6) {
            EvidencePip(shape: .hollow, tint: Theme.muted)
            Text("not gradeable").workFont(.dataSmall).foregroundStyle(Theme.muted)
        }
    }
}

/// One receipts-table row (52pt): task + decision badge, evidence tier pip and
/// ratio, client chip, check runs, cost, and recency.
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
                HStack(spacing: Space.m) {
                    Text(presentation.title)
                        .workFont(.rowLabel).foregroundStyle(Theme.ink)
                        .lineLimit(1).truncationMode(.tail)
                        .help(presentation.title)
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
                .frame(maxWidth: .infinity, alignment: .leading)

                evidenceCell.frame(width: 150, alignment: .leading)
                clientCell.frame(width: 124, alignment: .leading)
                checksCell.frame(width: 130, alignment: .trailing)
                costCell.frame(width: 76, alignment: .trailing)
                Text(presentation.updatedText)
                    .workFont(.dataSmall).foregroundStyle(Theme.muted)
                    .frame(width: 72, alignment: .trailing)
            }
            .padding(.horizontal, Space.xl)
            .frame(minHeight: Metrics.rowTable)
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

    @ViewBuilder
    private var clientCell: some View {
        if presentation.clientText != "unattributed" {
            ProvenanceChip(text: presentation.clientText)
        } else {
            Text("unattributed").workFont(.dataSmall).foregroundStyle(Theme.muted)
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

    /// The standard table uses the same honest compact presentation as the
    /// master list, including missing and contradictory tally states.
    @ViewBuilder
    private var checksCell: some View {
        if presentation.checkRunsAreInconsistent {
            VStack(alignment: .trailing, spacing: 2) {
                Text(presentation.checkRunsValue)
                    .workFont(.dataSmall).foregroundStyle(Theme.amber)
                Text(presentation.checkRunsQualifier)
                    .workFont(.dataSmall).foregroundStyle(Theme.muted)
            }
            .lineLimit(1)
            .help(presentation.checkRunsText)
            .frame(maxWidth: .infinity, alignment: .trailing)
        } else {
            let text = presentation.compactCheckRunsText
            Text(text)
                .workFont(.dataSmall)
                .foregroundStyle(
                    text.contains("failed")
                        ? Theme.coral
                        : (text.contains("not reported")
                            ? Theme.amber
                            : text == "no check runs" ? Theme.muted : Theme.ink)
                )
                .lineLimit(2)
                .multilineTextAlignment(.trailing)
                .frame(maxWidth: .infinity, alignment: .trailing)
        }
    }
}

/// Accessibility text sizes trade the fixed six-column comparison rail for a
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
                    Text(reason).workFont(.caption).foregroundStyle(Theme.coral)
                        .fixedSize(horizontal: false, vertical: true)
                }
                labelledValue("Claims supported", presentation.coverageText)
                labelledValue("Check runs", presentation.checkRunsText)
                labelledValue("Client", presentation.clientText)
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

    private var visibleTasks: [ReceiptSummary] {
        browse.visibleTasks(in: dashboard.receiptTasks)
    }

    private var renderedTasks: [ReceiptSummary] {
        guard SnapshotMode.enabled else { return visibleTasks }
        return Array(visibleTasks.prefix(7))
    }

    private var selectionIsOutsideBrowse: Bool {
        workSelectionIsOutsideBrowse(
            taskId: selection.taskId,
            allTasks: dashboard.receiptTasks,
            visibleTasks: visibleTasks
        )
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                Text("Work receipts").workFont(.titleCard).foregroundStyle(Theme.ink)
                    .accessibilityAddTraits(.isHeader)
                Spacer(minLength: 0)
                Text(
                    workBrowseCountText(
                        visible: visibleTasks.count,
                        loaded: dashboard.receiptTasks.count,
                        total: dashboard.totalReceiptTasks,
                        truncated: dashboard.receiptTasksTruncated
                    )
                )
                    .workFont(.dataSmall).foregroundStyle(Theme.muted)
            }
            .padding(.horizontal, Space.l)
            .padding(.top, Space.l)

            masterControls.padding(Space.l)
            if selectionIsOutsideBrowse {
                HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                    Text("Selected receipt is outside these filters")
                        .workFont(.dataSmall).foregroundStyle(Theme.muted)
                    Spacer(minLength: 0)
                    Button("Show") {
                        browse.query = ""
                        browse.group = nil
                    }
                    .buttonStyle(QuietButtonStyle(tint: Theme.accent))
                    .accessibilityIdentifier("work.master.show-selection")
                }
                .padding(.horizontal, Space.l)
                .padding(.bottom, Space.s)
                .accessibilityElement(children: .contain)
            }
            if let error = dashboard.receiptListError, !dashboard.receiptTasks.isEmpty {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    if dashboard.isLoadingReceipts, !SnapshotMode.enabled {
                        ProgressView().controlSize(.mini)
                    } else {
                        Image(systemName: "exclamationmark.triangle")
                            .font(.system(size: 10, weight: .semibold)).foregroundStyle(Theme.amber)
                            .accessibilityHidden(true)
                    }
                    Text(dashboard.isLoadingReceipts ? "Retrying · showing saved list" : "Showing saved list · refresh failed")
                        .workFont(.dataSmall).foregroundStyle(Theme.muted)
                        .help(error)
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, Space.l)
                .padding(.bottom, Space.s)
                .accessibilityLabel("Showing the last loaded receipt list. Refresh failed. \(error)")
            }
            Rectangle().fill(Theme.rule).frame(height: 1)

            ScrollViewReader { scrollProxy in
                ScrollBox {
                    ScrollContentStack(spacing: 0) {
                    if dashboard.isLoadingReceipts, dashboard.receiptTasks.isEmpty {
                        masterEmpty(
                            title: "Loading receipts",
                            message: "Reading the latest recorded work from the local store."
                        )
                        .accessibilityLabel("Loading receipts from the local store")
                    } else if let error = dashboard.receiptListError, dashboard.receiptTasks.isEmpty {
                        VStack(alignment: .leading, spacing: Space.s) {
                            masterEmpty(title: "Receipts unavailable", message: error)
                            if !SnapshotMode.enabled, !dashboard.isLoadingReceipts {
                                Button("Retry") { Task { await dashboard.fetchReceipts() } }
                                    .buttonStyle(QuietButtonStyle(tint: Theme.accent))
                                    .padding(.horizontal, Space.l)
                                    .accessibilityIdentifier("work.master.retry")
                            }
                        }
                    } else if visibleTasks.isEmpty {
                        masterEmpty(
                            title: dashboard.receiptTasks.isEmpty ? "No receipts recorded yet" : "No matching receipts",
                            message: dashboard.receiptTasks.isEmpty
                                ? "Recorded work will appear here."
                                : "Clear the filter or choose another status."
                        )
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
                    Text(browse.query.isEmpty ? "Filter receipts" : browse.query)
                        .workFont(.caption).foregroundStyle(Theme.muted)
                } else {
                    TextField("Filter receipts", text: $browse.query)
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

            HStack(spacing: Space.s) {
                if SnapshotMode.enabled {
                    Chip(text: browse.group?.rawValue ?? "All statuses", tint: Theme.accent)
                    Chip(text: browse.sort.rawValue, tint: Theme.muted)
                } else {
                    Picker("Status", selection: $browse.group) {
                        Text("All statuses").tag(nil as WorkGroup?)
                        ForEach(WorkGroup.allCases) { group in
                            Text(group.rawValue).tag(Optional(group))
                        }
                    }
                    .pickerStyle(.menu)
                    .accessibilityIdentifier("work.master.status")
                    Picker("Sort", selection: $browse.sort) {
                        ForEach(WorkSort.allCases) { Text($0.rawValue).tag($0) }
                    }
                    .pickerStyle(.menu)
                    .accessibilityIdentifier("work.master.sort")
                }
                DecisionLegendButton()
                Spacer(minLength: 0)
            }
        }
    }

    private func masterEmpty(title: String, message: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).workFont(.rowLabel).foregroundStyle(Theme.ink)
            Text(message).workFont(.caption).foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
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

                if let reason = presentation.attentionReason, !reason.isEmpty {
                    Text(verbatim: reason)
                        .workFont(.caption).foregroundStyle(Theme.coral)
                        .lineLimit(2)
                }

                HStack(alignment: .top, spacing: Space.l) {
                    VStack(alignment: .leading, spacing: 3) {
                        CapsLabel(text: "Claims supported")
                        Text(presentation.compactCoverageText)
                            .workFont(.dataSmall)
                            .foregroundStyle(presentation.coverageIsInconsistent ? Theme.amber : Theme.ink)
                            .lineLimit(1)
                            .help(presentation.coverageQualifier)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    VStack(alignment: .leading, spacing: 3) {
                        CapsLabel(text: "Check runs")
                        Text(presentation.compactCheckRunsText)
                            .workFont(.dataSmall)
                            .foregroundStyle(
                                presentation.checkRunsAreInconsistent
                                    ? Theme.amber
                                    : presentation.checkRunsText.contains("failed") ? Theme.coral : Theme.ink
                            )
                            .lineLimit(1)
                            .help(presentation.checkRunsQualifier)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }

                HStack(spacing: 5) {
                    Text(presentation.clientText)
                    Text("·")
                    Text(presentation.compactCostText)
                    Spacer(minLength: 4)
                    Text(presentation.updatedText)
                }
                .workFont(.dataSmall).foregroundStyle(Theme.muted)
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

enum WorkRecordColumnMode: Equatable {
    case stacked
    case sideBySide
}

private enum WorkRecordColumnMetrics {
    static let minimumMainWidth: CGFloat = 456
    static let sideWidth: CGFloat = 344
    static let spacing: CGFloat = Space.xl
    static let sideBySideMinimumWidth = minimumMainWidth + sideWidth + spacing
}

func workRecordColumnMode(for availableWidth: CGFloat) -> WorkRecordColumnMode {
    availableWidth >= WorkRecordColumnMetrics.sideBySideMinimumWidth ? .sideBySide : .stacked
}

/// One Work Receipt as an enterprise record page.
struct WorkRecordPage: View {
    let receipt: Receipt
    let summary: ReceiptSummary?
    let refreshError: String?
    let isRefreshing: Bool
    let autoFocusEntry: Bool
    @Environment(AppSelection.self) var selection
    @Environment(DashboardStore.self) var dashboard
    @FocusState private var backFocused: Bool
    @AccessibilityFocusState private var backAccessibilityFocused: Bool

    var body: some View {
        ScrollBox {
            VStack(alignment: .leading, spacing: 0) {
                breadcrumb
                titleBlock.padding(.top, Space.m)
                if let refreshError {
                    staleDetailBanner(refreshError).padding(.top, Space.m)
                }
                RecordDecisionCard(receipt: receipt)
                    .padding(.top, Space.l)
                RecordSummaryStrip(receipt: receipt, summary: summary)
                    .padding(.top, Space.l)
                columns.padding(.top, Space.xl)
                sessionsSection.padding(.top, Space.xl)
            }
            .padding(Space.gutter)
            .frame(maxWidth: 1172 + Space.gutter * 2, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .leading)
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

    /// An unmistakable back control (the old caps "WORK" read as a static path
    /// label, not a button) + the path itself. Esc triggers the same return.
    private var breadcrumb: some View {
        HStack(spacing: Space.m) {
            backButton
            HStack(spacing: 6) {
                CapsLabel(text: "Work")
                CapsLabel(text: "/ \(shortTaskRef)")
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
                in: dashboard.receiptTasks
            )
            selection.taskId = nil
            selection.sessionId = nil
        } label: {
            HStack(spacing: 4) {
                Image(systemName: "chevron.left")
                    .font(.system(size: 10, weight: .semibold))
                Text("All receipts").workFont(.captionSemibold)
            }
            .foregroundStyle(Theme.accent)
        }
        .buttonStyle(QuietButtonStyle(horizontalPadding: 6, verticalPadding: 3))
        .frame(minHeight: 24)
        .focused($backFocused)
        .keyboardShortcut(.cancelAction)
        .help("Back to the receipts list (Esc)")
        .accessibilityIdentifier("work.breadcrumb.back")
    }

    private var titleBlock: some View {
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
            Text(metaLine).workFont(.dataSmall).foregroundStyle(Theme.muted)
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

    /// Breadcrumb-sized task reference: "task_" + the id's first 8 hex chars.
    /// The full id stays in the meta line below the title.
    private var shortTaskRef: String {
        let id = receipt.taskId
        guard id.hasPrefix("task_"), id.count > 13 else { return id }
        return String(id.prefix(13))
    }

    private var metaLine: String {
        var parts: [String] = [receipt.taskId]
        if let client = summary?.primaryRoot?.client { parts.append(client) }
        if let models = receipt.dimensions.actors.models, !models.isEmpty {
            parts.append(models.joined(separator: ", "))
        }
        if let ago = agoText(summary?.lastActivityAt) { parts.append("updated \(ago)") }
        return parts.joined(separator: " · ")
    }

    private var columns: some View {
        ViewThatFits(in: .horizontal) {
            WorkRecordSplitLayout(
                minimumMainWidth: WorkRecordColumnMetrics.minimumMainWidth,
                sideWidth: WorkRecordColumnMetrics.sideWidth,
                spacing: WorkRecordColumnMetrics.spacing
            ) {
                mainColumn
                sideColumn
            }
            // Force the same explicit breakpoint exercised by interaction and
            // visual tests. The custom layout measures dense action text at
            // its final column width, so intrinsic content cannot silently
            // reject the horizontal candidate at the reference viewport.
            .frame(minWidth: WorkRecordColumnMetrics.sideBySideMinimumWidth)
            VStack(alignment: .leading, spacing: Space.xl) {
                mainColumn
                sideColumn
            }
        }
    }

    private var mainColumn: some View {
        VStack(alignment: .leading, spacing: Space.xl) {
            RecordDimensionsCard(receipt: receipt)
            RecordChecksCard(evidence: receipt.dimensions.evidence, taskId: receipt.taskId)
        }
    }

    private var sideColumn: some View {
        VStack(alignment: .leading, spacing: Space.xl) {
            RecordCoverageCard(
                evidence: receipt.axes.evidenceStrength,
                schemaVersion: receipt.schemaVersion
            )
            RecordSourcesCard(provenance: receipt.dimensions.provenance)
            RecordGapsCard(gaps: receipt.dimensions.gaps)
            if let orthogonality = receipt.axes.orthogonalityNote {
                Text(orthogonality).workFont(.dataSmall).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    @ViewBuilder
    private var sessionsSection: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                SectionCaption(tone: Theme.muted, text: "Sessions & steps")
                    .accessibilityHeading(.h2)
                if let groups = receipt.sessions, !groups.isEmpty {
                    let count = groups.reduce(0) { $0 + $1.members.count }
                    Text("\(count) session\(count == 1 ? "" : "s")")
                        .workFont(.dataSmall)
                        .foregroundStyle(Theme.muted)
                }
            }
            if let groups = receipt.sessions, !groups.isEmpty {
                ForEach(groups) { group in
                    VStack(alignment: .leading, spacing: 5) {
                        // Only label the group when there's more than one root (a
                        // Task with continuations); a single-root Task is just its
                        // sessions.
                        if groups.count > 1 {
                            HStack(spacing: 6) {
                                Chip(text: group.role == "continuation" ? "continuation" : "primary",
                                     tint: group.role == "continuation" ? Theme.muted : Theme.accent)
                                if let count = group.supportingCount, count > 0 {
                                    Text("\(count) subagent\(count == 1 ? "" : "s")")
                                        .workFont(.dataSmall).foregroundStyle(Theme.muted)
                                }
                            }
                        }
                        ScrollContentStack(alignment: .leading, spacing: 5) {
                            ForEach(group.members) { member in
                                SessionDrillRow(
                                    member: member,
                                    initiallyExpanded: group.role == "primary" && member.role == "root"
                                )
                            }
                        }
                    }
                }
            } else {
                Text("Session details aren't available for this receipt.")
                    .workFont(.caption)
                    .foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

/// The first card answers the user's decision question before the forensic
/// ledger begins: why the status exists, what claims have support, and what
/// machine checks actually ran.
private struct RecordDecisionCard: View {
    let receipt: Receipt

    private var presentation: WorkReceiptDecisionPresentation {
        .init(receipt: receipt)
    }

    var body: some View {
        Card(padding: Space.l) {
            VStack(alignment: .leading, spacing: Space.m) {
                HStack(alignment: .top, spacing: Space.m) {
                    RoundedRectangle(cornerRadius: 2)
                        .fill(presentation.isAttention ? Theme.coral : Theme.accent)
                        .frame(width: 4, height: 36)
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(presentation.headline)
                            .workFont(.titleCard).foregroundStyle(Theme.ink)
                            .accessibilityAddTraits(.isHeader)
                        Text(verbatim: presentation.explanation)
                            .workFont(.body).foregroundStyle(Theme.muted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }

                if let blocker = receipt.axes.decisionStatus.blocker, blocker.text != nil {
                    BlockerCallout(blocker: blocker, taskId: receipt.taskId)
                }

                Rectangle().fill(Theme.hairline).frame(height: 1)

                ViewThatFits(in: .horizontal) {
                    HStack(alignment: .top, spacing: Space.xl) {
                        metric(
                            label: "Claims supported",
                            value: presentation.coverageValue,
                            qualifier: presentation.coverageQualifier
                        )
                        Rectangle().fill(Theme.hairline).frame(width: 1, height: 42)
                        metric(
                            label: "Check runs",
                            value: presentation.checksValue,
                            qualifier: presentation.checksQualifier
                        )
                    }
                    .frame(minWidth: 460, alignment: .leading)
                    VStack(alignment: .leading, spacing: Space.m) {
                        metric(
                            label: "Claims supported",
                            value: presentation.coverageValue,
                            qualifier: presentation.coverageQualifier
                        )
                        metric(
                            label: "Check runs",
                            value: presentation.checksValue,
                            qualifier: presentation.checksQualifier
                        )
                    }
                }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("work.receipt.decision-summary")
    }

    private func metric(label: String, value: String, qualifier: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            CapsLabel(text: label)
            Text(value).workFont(.kpi).foregroundStyle(Theme.ink)
            Text(qualifier).workFont(.dataSmall).foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct WorkRecordSplitLayout: Layout {
    let minimumMainWidth: CGFloat
    let sideWidth: CGFloat
    let spacing: CGFloat

    func sizeThatFits(
        proposal: ProposedViewSize,
        subviews: Subviews,
        cache: inout ()
    ) -> CGSize {
        guard subviews.count == 2 else { return .zero }
        // ViewThatFits first asks for the candidate's ideal width without a
        // concrete proposal. Returning the dense ledger's intrinsic width here
        // makes the candidate look too large even though every row wraps at the
        // final column width. Report the real responsive minimum instead.
        let width = proposal.width ?? (minimumMainWidth + spacing + sideWidth)
        let widths = columnWidths(for: width)
        let mainSize = subviews[0].sizeThatFits(.init(width: widths.main, height: nil))
        let sideSize = subviews[1].sizeThatFits(.init(width: widths.side, height: nil))
        return CGSize(width: width, height: max(mainSize.height, sideSize.height))
    }

    func placeSubviews(
        in bounds: CGRect,
        proposal: ProposedViewSize,
        subviews: Subviews,
        cache: inout ()
    ) {
        guard subviews.count == 2 else { return }
        let widths = columnWidths(for: bounds.width)
        subviews[0].place(
            at: bounds.origin,
            proposal: .init(width: widths.main, height: nil)
        )
        subviews[1].place(
            at: CGPoint(x: bounds.minX + widths.main + spacing, y: bounds.minY),
            proposal: .init(width: widths.side, height: nil)
        )
    }

    private func columnWidths(for width: CGFloat) -> (main: CGFloat, side: CGFloat) {
        let available = max(0, width - spacing)
        let resolvedSide = min(sideWidth, available)
        return (max(0, available - resolvedSide), resolvedSide)
    }
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
    @State private var expanded: Bool
    @State private var detail: V1SessionDetail?
    @State private var loading = false
    @State private var failed = false
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
                if expanded, effectiveDetail == nil, !loading, !failed { Task { await load() } }
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
        .task {
            // The root session opens expanded — its step-by-step is the point;
            // load its steps up front.
            if expanded, effectiveDetail == nil, !loading, !failed { await load() }
        }
    }

    @ViewBuilder
    private var expandedBody: some View {
        if let detail = effectiveDetail {
            VStack(alignment: .leading, spacing: 6) {
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
                        ? Set(
                            stepItems.filter { !($0.step.checks?.isEmpty ?? true) }.prefix(2).map(\.id)
                            + stepItems.filter { ($0.step.checks?.isEmpty ?? true) }.prefix(1).map(\.id)
                          )
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
            detail = try await dashboard.loadSession(client: member.client, sessionId: member.clientSessionId)
            failed = false
        } catch {
            failed = true
        }
    }
}
