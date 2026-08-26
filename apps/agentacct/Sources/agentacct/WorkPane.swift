import SwiftUI

// The Work surface — the receipts workbench. Two states, one selection model:
//
// * No Task selected → the Work receipts TABLE: lifecycle tabs with counts, a
//   filter, and a six-column ledger (task + decision, evidence, client, checks,
//   cost, recency). The browse surface.
// * A Task selected → the RECORD page: a 204pt receipt rail on the left and
//   the full Work Receipt on the right (summary strip, dimensions ledger,
//   evidence coverage, checks, sources, gaps, and the sessions/steps drill-
//   down). The breadcrumb walks back to the table.
//
// A Task is the converged unit (root session + continuations + subagents).
// Honesty rides the payload: decision words come from the daemon, evidence
// tiers keep their pip shapes, and absent facts are named, never zeroed.

enum WorkSessionResolution: Equatable {
    case task(String)
    case unresolved(String)
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
    case handedOff = "Handed off"
    case other = "Other"

    var id: String { rawValue }

    /// Buckets never upgrade a claim: "Verified" holds only the machine-
    /// asserted key; agent claims of done-ness group under their own word
    /// ("Reported"); ambient activity stays "Observed".
    static func forKey(_ key: String?) -> WorkGroup {
        switch key {
        case "finding", "failed", "blocked": return .attention
        case "verified": return .verified
        case "reported", "resolved", "mostly_done", "finding_superseded": return .reported
        case "in_progress", "started", "checkpoint": return .inProgress
        case "observed": return .observed
        case "handed_off": return .handedOff
        default: return .other
        }
    }
}

struct WorkPane: View {
    @EnvironmentObject var dashboard: DashboardStore
    @EnvironmentObject var selection: AppSelection
    @State private var unresolvedSessionId: String?

    private var selectionKey: String {
        if let taskId = selection.taskId { return "task:\(taskId)" }
        if let sessionId = selection.sessionId { return "session:\(sessionId)" }
        return "list"
    }

    var body: some View {
        Group {
            if selection.taskId != nil || unresolvedSessionId != nil {
                recordLayout
            } else {
                WorkTablePage()
            }
        }
        .task(id: selectionKey) {
            await resolveSelection()
        }
    }

    private var recordLayout: some View {
        // Top-aligned: in snapshot mode the rail renders its full content and
        // would otherwise center the record column against it.
        HStack(alignment: .top, spacing: 0) {
            WorkRail()
            Rectangle().fill(Theme.rule).frame(width: 1)
            recordDetail.frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        }
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
    private var recordDetail: some View {
        if let receipt = dashboard.receipt, receipt.taskId == selection.taskId {
            WorkRecordPage(
                receipt: receipt,
                summary: dashboard.receiptTasks.first { $0.taskId == receipt.taskId }
            )
        } else if let error = dashboard.receiptError, selection.taskId != nil {
            Text(error).font(Type.body).foregroundStyle(Theme.muted).padding()
        } else if let unresolvedSessionId, selection.sessionId == unresolvedSessionId {
            VStack(spacing: 8) {
                Image(systemName: "arrow.triangle.branch")
                    .font(.largeTitle)
                    .foregroundStyle(Theme.muted)
                    .accessibilityHidden(true)
                Text("Task link unavailable")
                    .font(Type.rowLabel)
                    .foregroundStyle(Theme.ink)
                Text("This active session is not identified in the task summary. "
                    + "Select its Task from the list to inspect the full receipt.")
                    .font(Type.body)
                    .foregroundStyle(Theme.muted)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 360)
            }
            .padding()
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .accessibilityIdentifier("work.unresolved-session")
        } else if selection.taskId != nil {
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            Color.clear
        }
    }
}

// MARK: - Work receipts table

private struct WorkTablePage: View {
    @EnvironmentObject var dashboard: DashboardStore
    @EnvironmentObject var selection: AppSelection
    @State private var query = ""
    @State private var group: WorkGroup?
    @State private var sort: WorkSort = .attention

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

    private var groupCounts: [WorkGroup: Int] {
        Dictionary(grouping: dashboard.receiptTasks) { WorkGroup.forKey($0.decisionStatus.key) }
            .mapValues(\.count)
    }

    private var visibleTasks: [ReceiptSummary] {
        var rows = dashboard.receiptTasks
        if let group {
            rows = rows.filter { WorkGroup.forKey($0.decisionStatus.key) == group }
        }
        if !query.isEmpty {
            let needle = query.lowercased()
            rows = rows.filter {
                ($0.title ?? "").lowercased().contains(needle)
                    || $0.taskId.lowercased().contains(needle)
                    || ($0.primaryRoot?.client ?? "").lowercased().contains(needle)
            }
        }
        switch sort {
        case .attention:
            // Stable partition: attention receipts first, server recency inside
            // each partition.
            let attention = rows.filter { WorkGroup.forKey($0.decisionStatus.key) == .attention }
            return attention + rows.filter { WorkGroup.forKey($0.decisionStatus.key) != .attention }
        case .latest:
            return rows  // server order: recency
        case .cost:
            return rows.sorted { ($0.cost.estimatedCostUsd ?? -1) > ($1.cost.estimatedCostUsd ?? -1) }
        }
    }

    var body: some View {
        ScrollBox {
            VStack(alignment: .leading, spacing: 0) {
                header
                tabs.padding(.top, Space.xl)
                filterRow.padding(.top, Space.m)
                tableCard.padding(.top, Space.l)
                footer.padding(.top, Space.m)
                legend.padding(.top, Space.l)
            }
            .padding(Space.gutter)
            .frame(maxWidth: 1172 + Space.gutter * 2, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Work receipts")
                .font(Type.titlePage).tracking(Type.titlePageTracking)
                .foregroundStyle(Theme.ink)
            HStack(spacing: 0) {
                Text("local store · \(dashboard.totalReceiptTasks ?? dashboard.receiptTasks.count) receipts")
                    .font(Type.dataSmall).foregroundStyle(Theme.muted)
                // The list endpoint caps at 200 rows; when the store holds
                // more, the tab counts cover the loaded slice — say so.
                if let total = dashboard.totalReceiptTasks, total > dashboard.receiptTasks.count {
                    Text(" · latest \(dashboard.receiptTasks.count) loaded")
                        .font(Type.dataSmall).foregroundStyle(Theme.muted)
                }
                if let updated = dashboard.lastUpdated {
                    Text(" · refreshed \(dashboardFreshnessText(updated))")
                        .font(Type.dataSmall).foregroundStyle(Theme.muted)
                }
            }
        }
    }

    private var tabs: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: Space.xl) {
                let truncated = (dashboard.totalReceiptTasks ?? 0) > dashboard.receiptTasks.count
                tabButton(nil, label: truncated ? "Loaded" : "All", count: dashboard.receiptTasks.count)
                ForEach(WorkGroup.allCases) { candidate in
                    let count = groupCounts[candidate] ?? 0
                    // "Other" appears only when an unmapped decision key exists,
                    // so the visible tab counts always sum to All.
                    if candidate != .other || count > 0 {
                        tabButton(candidate, label: candidate.rawValue, count: count)
                    }
                }
            }
            Rectangle().fill(Theme.hairline).frame(height: 1)
        }
    }

    private func tabButton(_ candidate: WorkGroup?, label: String, count: Int) -> some View {
        let active = group == candidate
        return Button {
            group = candidate
        } label: {
            VStack(spacing: 0) {
                HStack(spacing: 6) {
                    Text(label)
                        .font(Face.sansFont(13, active ? .semibold : .medium))
                        .foregroundStyle(active ? Theme.accent : Theme.ink)
                    Text("\(count)")
                        .font(Type.dataSmall)
                        .foregroundStyle(candidate == .attention && count > 0 ? Theme.coral : Theme.muted)
                }
                .padding(.bottom, 10)
                Rectangle()
                    .fill(active ? Theme.accent : .clear)
                    .frame(height: 2)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
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
                    Text("Filter by task, client, or id").font(Type.caption).foregroundStyle(Theme.muted)
                } else {
                    TextField("Filter by task, client, or id", text: $query)
                        .textFieldStyle(.plain).font(Type.caption)
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
                Chip(text: "sort: \(sort.rawValue)", tint: Theme.accent)
            } else {
                Picker("Sort", selection: $sort) {
                    ForEach(WorkSort.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.menu)
                .fixedSize()
            }
            Spacer()
        }
    }

    private var tableCard: some View {
        Card(padding: 0) {
            VStack(spacing: 0) {
                columnHeader
                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.horizontal, Space.xl)
                if let error = dashboard.receiptListError, dashboard.receiptTasks.isEmpty {
                    Text(error).font(Type.body).foregroundStyle(Theme.muted)
                        .padding(Space.xl)
                        .frame(maxWidth: .infinity, alignment: .leading)
                } else if visibleTasks.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("No receipts match").font(Type.rowLabel).foregroundStyle(Theme.ink)
                        Text("Adjust the lifecycle tab or the filter — nothing is hidden beyond them.")
                            .font(Type.caption).foregroundStyle(Theme.muted)
                    }
                    .padding(Space.xl)
                    .frame(maxWidth: .infinity, alignment: .leading)
                } else {
                    // Offscreen full-content renders cap the rows and name the
                    // overflow; the live app scrolls the full set.
                    let rows = SnapshotMode.enabled ? Array(visibleTasks.prefix(9)) : visibleTasks
                    ForEach(Array(rows.enumerated()), id: \.element.id) { index, task in
                        if index > 0 {
                            Rectangle().fill(Theme.hairline).frame(height: 1)
                                .padding(.horizontal, Space.xl)
                        }
                        WorkTableRow(task: task) {
                            selection.sessionId = nil
                            selection.taskId = task.taskId
                        }
                    }
                    if SnapshotMode.enabled, visibleTasks.count > rows.count {
                        Rectangle().fill(Theme.hairline).frame(height: 1)
                            .padding(.horizontal, Space.xl)
                        Text("… \(visibleTasks.count - rows.count) more receipts (snapshot preview)")
                            .font(Type.dataSmall).foregroundStyle(Theme.muted)
                            .padding(Space.xl)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
        }
    }

    private var columnHeader: some View {
        HStack(spacing: Space.l) {
            CapsLabel(text: "Task").frame(maxWidth: .infinity, alignment: .leading)
            CapsLabel(text: "Evidence").frame(width: 150, alignment: .leading)
            CapsLabel(text: "Client").frame(width: 124, alignment: .leading)
            CapsLabel(text: "Checks passed").frame(width: 130, alignment: .trailing)
            CapsLabel(text: "Est. cost").frame(width: 76, alignment: .trailing)
            CapsLabel(text: "Updated").frame(width: 72, alignment: .trailing)
        }
        .padding(.horizontal, Space.xl)
        .frame(height: Metrics.rowHeader)
    }

    private var footer: some View {
        Text("\(visibleTasks.count) of \(dashboard.totalReceiptTasks ?? dashboard.receiptTasks.count) receipts · \(sort.footerText)")
            .font(Type.dataSmall).foregroundStyle(Theme.muted)
    }

    private var legend: some View {
        HStack(spacing: Space.l) {
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
                        .font(Type.dataSmall).foregroundStyle(Theme.muted)
                }
            }
            HStack(spacing: 6) {
                EvidencePip(shape: .hollow, tint: Theme.muted)
                Text("not gradeable").font(Type.dataSmall).foregroundStyle(Theme.muted)
            }
            Spacer()
            Text("Evidence counts checkable steps · claim ≠ proof")
                .font(Type.dataSmall).foregroundStyle(Theme.muted)
        }
    }
}

/// One receipts-table row (52pt): task + decision badge, evidence tier pip and
/// ratio, client chip, checks-passed rail, cost, recency.
private struct WorkTableRow: View {
    let task: ReceiptSummary
    let action: () -> Void
    @State private var hovering = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var evidence: ReceiptEvidence { task.evidenceStrength }

    var body: some View {
        Button(action: action) {
            HStack(spacing: Space.l) {
                HStack(spacing: Space.m) {
                    Text(task.title ?? task.taskId)
                        .font(Type.rowLabel).foregroundStyle(Theme.ink)
                        .lineLimit(1).truncationMode(.tail)
                    DecisionBadge(
                        key: task.decisionStatus.key,
                        label: task.decisionStatus.label ?? task.decisionStatus.key,
                        compact: true
                    )
                    // Parallel deliberate-stop marker, only when it adds info the
                    // decision word does not already state.
                    if task.handedOff == true && task.decisionStatus.key != "handed_off" {
                        Chip(text: "↗ handed off", tint: Theme.muted)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)

                evidenceCell.frame(width: 150, alignment: .leading)
                clientCell.frame(width: 124, alignment: .leading)
                checksCell.frame(width: 130, alignment: .trailing)
                costCell.frame(width: 76, alignment: .trailing)
                Text(agoText(task.lastActivityAt) ?? "no activity")
                    .font(Type.dataSmall).foregroundStyle(Theme.muted)
                    .frame(width: 72, alignment: .trailing)
            }
            .padding(.horizontal, Space.xl)
            .frame(minHeight: Metrics.rowTable)
            .background(hovering ? Theme.selected.opacity(0.5) : .clear)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { inside in
            withAnimation(reduceMotion ? nil : Motion.hover) { hovering = inside }
        }
        .accessibilityIdentifier("work.table.task.\(task.taskId)")
        .accessibilityLabel(
            "\(task.title ?? task.taskId), \(task.decisionStatus.label ?? task.decisionStatus.key), "
            + "evidence \(evidence.compactHeadline)"
        )
    }

    /// Checked/checkable ratio + the strongest tier's pip shape. The pip is a
    /// ceiling marker (best evidence present), the ratio is the coverage.
    @ViewBuilder
    private var evidenceCell: some View {
        if evidence.gradeable == true, let checkable = evidence.checkableTotal, checkable > 0 {
            HStack(spacing: 7) {
                let style = EvidenceTierStyle.forGrade(evidence.strongestTier ?? "unchecked")
                EvidencePip(shape: style.pip, tint: style.tint)
                Text("\(evidence.checkedTotal ?? 0)/\(checkable) checked")
                    .font(Type.dataSmall).foregroundStyle(Theme.ink)
            }
        } else {
            HStack(spacing: 7) {
                EvidencePip(shape: .hollow, tint: Theme.muted)
                Text("not gradeable").font(Type.dataSmall).foregroundStyle(Theme.muted)
            }
            .help(
                (evidence.checksTotal ?? 0) > 0
                    ? "No checkable steps — the recorded checks attach to no claim step"
                    : "No checkable steps recorded for this task"
            )
        }
    }

    @ViewBuilder
    private var clientCell: some View {
        if let client = task.primaryRoot?.client {
            ProvenanceChip(text: client)
        } else {
            Text("unattributed").font(Type.dataSmall).foregroundStyle(Theme.muted)
        }
    }

    /// Cost with the ~/≈ prefix grammar; a task with no priced usage is a
    /// named state, never a dash.
    @ViewBuilder
    private var costCell: some View {
        let cost = DashboardWorkItem(task: task).cost
        if cost == "—" {
            Text("unpriced").font(Type.dataSmall).foregroundStyle(Theme.muted)
        } else {
            Text(cost).font(Type.dataSmall).foregroundStyle(Theme.ink)
        }
    }

    /// Checks fractions share one right-aligned rail; failure annotations ride
    /// to the left of the rail so every fraction's right edge lines up.
    @ViewBuilder
    private var checksCell: some View {
        let failed = evidence.checksFailed ?? 0
        HStack(spacing: 6) {
            Spacer(minLength: 0)
            if failed > 0 {
                Text("\(failed) failed").font(Type.dataSmall).foregroundStyle(Theme.coral)
            }
            if let total = evidence.checksTotal {
                if total > 0 {
                    Text("\(evidence.checksPassed ?? 0)/\(total)")
                        .font(Type.dataSmall).foregroundStyle(Theme.ink)
                        .frame(width: 44, alignment: .trailing)
                } else {
                    Text("none").font(Type.dataSmall).foregroundStyle(Theme.muted)
                        .frame(width: 44, alignment: .trailing)
                }
            } else {
                // An older daemon's list rows carry no tallies — absent, not zero.
                Text("not reported").font(Type.dataSmall).foregroundStyle(Theme.muted)
            }
        }
    }
}

// MARK: - Receipt rail (record mode)

/// The 204pt receipt rail beside an open record: every receipt, current one
/// washed + accent-barred, each row carrying its decision word and cost.
private struct WorkRail: View {
    @EnvironmentObject var dashboard: DashboardStore
    @EnvironmentObject var selection: AppSelection

    /// Offscreen full-content renders would let a long rail stretch (and then
    /// clip) the whole record page; a snapshot shows a bounded slice and names
    /// the overflow instead.
    private var railTasks: [ReceiptSummary] {
        guard SnapshotMode.enabled else { return dashboard.receiptTasks }
        return Array(dashboard.receiptTasks.prefix(8))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 3) {
                CapsLabel(text: "Work receipts · \(dashboard.totalReceiptTasks ?? dashboard.receiptTasks.count)")
                if let total = dashboard.totalReceiptTasks, total > dashboard.receiptTasks.count {
                    Text("latest \(dashboard.receiptTasks.count) loaded")
                        .font(Type.dataSmall).foregroundStyle(Theme.muted)
                }
            }
            .padding(.horizontal, Space.l)
            .padding(.vertical, Space.l)
            ScrollBox {
                VStack(spacing: 0) {
                    ForEach(railTasks) { task in
                        WorkRailRow(task: task, selected: task.taskId == selection.taskId) {
                            selection.sessionId = nil
                            selection.taskId = task.taskId
                        }
                    }
                    if SnapshotMode.enabled, dashboard.receiptTasks.count > railTasks.count {
                        Text("… \(dashboard.receiptTasks.count - railTasks.count) more")
                            .font(Type.dataSmall).foregroundStyle(Theme.muted)
                            .padding(Space.l)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
        }
        .frame(width: 204)
        .background(Theme.chrome)
    }
}

private struct WorkRailRow: View {
    let task: ReceiptSummary
    let selected: Bool
    let action: () -> Void

    private var statusTint: Color {
        let group = WorkGroup.forKey(task.decisionStatus.key)
        if group == .attention { return Theme.coral }
        return selected ? Theme.ink : Theme.muted
    }

    var body: some View {
        Button(action: action) {
            HStack(spacing: 0) {
                VStack(alignment: .leading, spacing: 5) {
                    Text(task.title ?? task.taskId)
                        .font(Face.sansFont(13, selected ? .semibold : .regular))
                        .foregroundStyle(selected ? Theme.ink : Theme.muted)
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                    HStack(spacing: 6) {
                        Text((task.decisionStatus.label ?? task.decisionStatus.key).uppercased())
                            .font(Type.labelCaps).tracking(Type.labelCapsTracking)
                            .foregroundStyle(statusTint)
                            .lineLimit(1)
                        Spacer(minLength: 4)
                        let cost = DashboardWorkItem(task: task).cost
                        Text(cost == "—" ? "unpriced" : cost)
                            .font(Type.dataSmall).foregroundStyle(Theme.muted)
                    }
                }
                .padding(.leading, Space.m + 4)
                .padding(.trailing, Space.m)
                .padding(.vertical, Space.m)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(selected ? Theme.selected : .clear)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        // The selection bar rides as an overlay so it can never stretch the row.
        .overlay(alignment: .leading) {
            if selected {
                Rectangle().fill(Theme.accent).frame(width: 4)
            }
        }
        .overlay(alignment: .bottom) {
            Rectangle().fill(Theme.hairline).frame(height: 1).padding(.leading, Space.l)
        }
        .accessibilityIdentifier("work.rail.task.\(task.taskId)")
    }
}

// MARK: - Record page

/// One Work Receipt as an enterprise record page.
struct WorkRecordPage: View {
    let receipt: Receipt
    let summary: ReceiptSummary?
    @EnvironmentObject var selection: AppSelection

    var body: some View {
        ScrollBox {
            VStack(alignment: .leading, spacing: 0) {
                breadcrumb
                titleBlock.padding(.top, Space.m)
                RecordSummaryStrip(receipt: receipt, summary: summary)
                    .padding(.top, Space.xl)
                columns.padding(.top, Space.xl)
                sessionsSection.padding(.top, Space.xl)
            }
            .padding(Space.gutter)
            .frame(maxWidth: 1172 + Space.gutter * 2, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .id(receipt.taskId)  // reset the drill-down's expansion state per Task
    }

    private var breadcrumb: some View {
        HStack(spacing: 6) {
            Button {
                selection.taskId = nil
                selection.sessionId = nil
            } label: {
                CapsLabel(text: "Work", tone: Theme.accent)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("work.breadcrumb.back")
            CapsLabel(text: "/ \(shortTaskRef)")
        }
    }

    private var titleBlock: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            HStack(alignment: .center, spacing: Space.m) {
                Text(receipt.title ?? receipt.taskId)
                    .font(Type.titlePage).tracking(Type.titlePageTracking)
                    .foregroundStyle(Theme.ink)
                    .lineLimit(2)
                DecisionBadge(
                    key: receipt.axes.decisionStatus.key,
                    label: receipt.axes.decisionStatus.label ?? receipt.axes.decisionStatus.key
                )
                if let handoff = receipt.axes.handoff, handoff.handedOff == true,
                   receipt.axes.decisionStatus.key != "handed_off" {
                    Chip(text: "↗ handed off", tint: Theme.muted)
                }
                Spacer()
            }
            Text(metaLine).font(Type.dataSmall).foregroundStyle(Theme.muted)
            if let statement = receipt.axes.decisionStatus.statement {
                Text(verbatim: statementLine(statement))
                    .font(Type.caption).foregroundStyle(Theme.muted)
            }
        }
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

    private func statementLine(_ statement: String) -> String {
        if let assertedBy = assertedByLabel(receipt.axes.decisionStatus.assertedBy) {
            return "\(statement) — \(assertedBy)"
        }
        return statement
    }

    private var columns: some View {
        ViewThatFits(in: .horizontal) {
            HStack(alignment: .top, spacing: Space.xl) {
                mainColumn.frame(maxWidth: .infinity)
                sideColumn.frame(width: 372)
            }
            .frame(minWidth: 780)
            VStack(alignment: .leading, spacing: Space.xl) {
                mainColumn
                sideColumn
            }
        }
    }

    private var mainColumn: some View {
        VStack(alignment: .leading, spacing: Space.xl) {
            RecordDimensionsCard(receipt: receipt)
            RecordChecksCard(evidence: receipt.dimensions.evidence)
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
                Text(orthogonality).font(Type.dataSmall).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    @ViewBuilder
    private var sessionsSection: some View {
        if let groups = receipt.sessions, !groups.isEmpty {
            VStack(alignment: .leading, spacing: Space.s) {
                SectionCaption(tone: Theme.muted, text: "Sessions & steps")
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
                                        .font(Type.dataSmall).foregroundStyle(Theme.muted)
                                }
                            }
                        }
                        ForEach(group.members) { member in
                            SessionDrillRow(
                                member: member,
                                initiallyExpanded: group.role == "primary" && member.role == "root"
                            )
                        }
                    }
                }
            }
        }
    }
}

// MARK: - Session drill-down

/// One session in the drill-down: a header row (role/kind + title) that expands
/// to lazily load `/v1/session` and render its steps — reusing StepCard — and
/// its subagent sessions. Each row owns its own loaded detail (the shared store
/// slot would clobber across several expanded sessions).
private struct SessionDrillRow: View {
    let member: ReceiptSessionMember
    let initiallyExpanded: Bool
    @EnvironmentObject var dashboard: DashboardStore
    @State private var expanded: Bool
    @State private var detail: V1SessionDetail?
    @State private var loading = false
    @State private var failed = false

    init(member: ReceiptSessionMember, initiallyExpanded: Bool = false) {
        self.member = member
        self.initiallyExpanded = initiallyExpanded
        _expanded = State(initialValue: initiallyExpanded)
    }

    /// The lazily-loaded detail, or a snapshot-preloaded one (the offscreen
    /// renderer never runs the `.task` that would load it live).
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

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(Motion.contentUpdate) { expanded.toggle() }
                if expanded, detail == nil, !loading { Task { await load() } }
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 8, weight: .semibold)).foregroundStyle(Theme.muted).frame(width: 10)
                    Chip(text: member.role == "subagent" ? (member.sessionKind ?? "subagent") : "root",
                         tint: member.role == "subagent" ? Theme.muted : Theme.accent)
                    Text(label).font(Type.body).foregroundStyle(Theme.ink).lineLimit(1).truncationMode(.tail)
                    Spacer(minLength: 8)
                    if let project = member.project, member.role == "root" {
                        Text(project).font(Type.caption).foregroundStyle(Theme.muted)
                    }
                    if let ago = agoText(member.lastActivityAt) {
                        Text(ago).font(Type.dataSmall).foregroundStyle(Theme.muted)
                    }
                }
                .padding(.horizontal, 10).padding(.vertical, 7).contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if expanded {
                expandedBody.padding(.horizontal, 12).padding(.bottom, 10).padding(.leading, 6)
            }
        }
        .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Theme.cardLine, lineWidth: Metrics.borderW))
        .task {
            // The root session opens expanded — its step-by-step is the point;
            // load its steps up front.
            if expanded, detail == nil, !loading { await load() }
        }
    }

    @ViewBuilder
    private var expandedBody: some View {
        if let detail = effectiveDetail {
            VStack(alignment: .leading, spacing: 6) {
                if detail.steps.isEmpty {
                    Text("no recorded steps for this session").font(Type.caption).foregroundStyle(Theme.muted)
                } else {
                    // A snapshot opens the first couple of check-bearing steps
                    // (so their check evidence — command + exit code — shows) plus
                    // one un-checked step (so an honest "no passing check" step is
                    // visible too); the live app opens every step collapsed.
                    let opened: Set<String> = SnapshotMode.enabled
                        ? Set(
                            detail.steps.filter { !($0.checks?.isEmpty ?? true) }.prefix(2).map(\.id)
                            + detail.steps.filter { ($0.checks?.isEmpty ?? true) }.prefix(1).map(\.id)
                          )
                        : []
                    ForEach(detail.steps) { step in
                        StepCard(step: step, initiallyExpanded: opened.contains(step.id))
                    }
                }
                // The Task's subagents are already listed once, flat and
                // expandable, as sibling SessionDrillRows in this group's member
                // list. Re-listing this session's descendants here showed the
                // same subagents a second time (a confusing "41 and 41"), so the
                // member list is the single source of truth for the tree.
            }
        } else if loading {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("loading steps…").font(Type.caption).foregroundStyle(Theme.muted)
            }
        } else if failed {
            Text("couldn't load this session").font(Type.caption).foregroundStyle(Theme.amber)
        }
    }

    private func load() async {
        loading = true
        failed = false
        defer { loading = false }
        do {
            detail = try await dashboard.loadSession(client: member.client, sessionId: member.clientSessionId)
        } catch {
            failed = true
        }
    }
}
