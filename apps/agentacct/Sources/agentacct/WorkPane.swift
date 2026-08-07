import SwiftUI

// The Work surface — one place for "insights into agent work". A Task list on
// the left; selecting a Task shows its Work Receipt (the verdict: 8 dimensions
// + 2 axes) and, below it, an expandable drill-down of the Task's constituent
// sessions and their steps (the evidence trail). A Task is the converged unit
// (root session + its continuations + subagents); this folds the former
// Receipts + Sessions panes into one Task-primary view.

struct WorkPane: View {
    @EnvironmentObject var dashboard: DashboardStore
    @EnvironmentObject var selection: AppSelection
    @State private var query = ""
    @State private var sort: WorkSort = .latest

    enum WorkSort: String, CaseIterable, Identifiable {
        case latest, cost
        var id: String { rawValue }
    }

    private var visibleTasks: [ReceiptSummary] {
        var rows = dashboard.receiptTasks
        if !query.isEmpty {
            let needle = query.lowercased()
            rows = rows.filter {
                ($0.title ?? "").lowercased().contains(needle) || $0.taskId.lowercased().contains(needle)
            }
        }
        switch sort {
        case .latest: return rows  // server order: recency
        case .cost: return rows.sorted { ($0.cost.estimatedCostUsd ?? -1) > ($1.cost.estimatedCostUsd ?? -1) }
        }
    }

    var body: some View {
        HStack(spacing: 0) {
            taskList.frame(width: 340)
            Rectangle().fill(Theme.border).frame(width: 1)
            detail.frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .task {
            await dashboard.fetchReceipts()
            resolveDeepLink()
        }
    }

    /// A menu-bar recent-session click lands here with `selection.sessionId`
    /// set; the Work surface is Task-primary, so resolve that session to its
    /// Task by primary-root match and select the Task instead.
    private func resolveDeepLink() {
        guard let sessionId = selection.sessionId, selection.taskId == nil else { return }
        if let match = dashboard.receiptTasks.first(where: { $0.primaryRoot?.sessionKey == sessionId }) {
            select(match.taskId)
        }
        selection.sessionId = nil
    }

    private var taskList: some View {
        VStack(spacing: 0) {
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass").font(.system(size: 10)).foregroundStyle(Theme.textFaint)
                TextField("Filter tasks", text: $query).textFieldStyle(.plain).font(Type.body)
                Picker("", selection: $sort) {
                    ForEach(WorkSort.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.menu).fixedSize()
            }
            .padding(.horizontal, 8).padding(.vertical, 7)
            Rectangle().fill(Theme.border.opacity(0.6)).frame(height: 1)
            if let error = dashboard.receiptError, dashboard.receiptTasks.isEmpty {
                Text(error).font(.callout).foregroundStyle(Theme.textMuted)
                    .padding().frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollBox {
                    VStack(spacing: 2) {
                        ForEach(visibleTasks) { task in
                            ReceiptRow(task: task, selected: task.taskId == selection.taskId)
                                .contentShape(Rectangle())
                                .onTapGesture { select(task.taskId) }
                        }
                    }
                    .padding(6)
                }
            }
        }
        .background(Theme.surface.opacity(0.4))
    }

    @ViewBuilder
    private var detail: some View {
        if let receipt = dashboard.receipt {
            WorkDetail(receipt: receipt)
        } else if let error = dashboard.receiptError, selection.taskId != nil {
            Text(error).font(.callout).foregroundStyle(Theme.textMuted).padding()
        } else {
            VStack(spacing: 8) {
                Image(systemName: "checklist").font(.largeTitle).foregroundStyle(Theme.textFaint)
                Text("Select a Task to see its work").foregroundStyle(Theme.textMuted)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func select(_ taskId: String) {
        selection.taskId = taskId
        Task { await dashboard.fetchReceipt(taskId: taskId) }
    }
}

/// The right pane: the Receipt's cards, then the Sessions & steps drill-down,
/// scrolling as one.
private struct WorkDetail: View {
    let receipt: Receipt

    var body: some View {
        ScrollBox {
            VStack(alignment: .leading, spacing: 14) {
                ReceiptCards(receipt: receipt)
                if let groups = receipt.sessions, !groups.isEmpty {
                    sessionsSection(groups)
                }
            }
            .padding(16)
            .frame(maxWidth: 760, alignment: .leading)
        }
        .id(receipt.taskId)  // reset the drill-down's expansion state per Task
    }

    private func sessionsSection(_ groups: [ReceiptSessionGroup]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionCaption(tone: Theme.textMuted, text: "Sessions & steps")
            ForEach(groups) { group in
                VStack(alignment: .leading, spacing: 5) {
                    // Only label the group when there's more than one root (a
                    // Task with continuations); a single-root Task is just its
                    // sessions.
                    if groups.count > 1 {
                        HStack(spacing: 6) {
                            Chip(text: group.role == "continuation" ? "continuation" : "primary",
                                 tint: group.role == "continuation" ? Theme.blue : Theme.accent)
                            if let count = group.supportingCount, count > 0 {
                                Text("\(count) subagent\(count == 1 ? "" : "s")")
                                    .font(Type.tiny).foregroundStyle(Theme.textFaint)
                            }
                        }
                    }
                    ForEach(group.members) { member in
                        SessionDrillRow(member: member)
                    }
                }
            }
        }
    }
}

/// One session in the drill-down: a header row (role/kind + title) that expands
/// to lazily load `/v1/session` and render its steps — reusing StepCard — and
/// its subagent sessions. Each row owns its own loaded detail (the shared store
/// slot would clobber across several expanded sessions).
private struct SessionDrillRow: View {
    let member: ReceiptSessionMember
    @EnvironmentObject var dashboard: DashboardStore
    @State private var expanded = false
    @State private var detail: V1SessionDetail?
    @State private var loading = false
    @State private var failed = false

    private var label: String {
        if let title = member.title, !title.isEmpty { return title }
        if let loaded = detail?.session.displayTitle, !loaded.isEmpty { return loaded }
        return "\(member.client) · \(String(member.clientSessionId.prefix(8)))"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(.easeInOut(duration: 0.15)) { expanded.toggle() }
                if expanded, detail == nil, !loading { Task { await load() } }
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 8, weight: .semibold)).foregroundStyle(Theme.textFaint).frame(width: 10)
                    Chip(text: member.role == "subagent" ? (member.sessionKind ?? "subagent") : "root",
                         tint: member.role == "subagent" ? Theme.purple : Theme.accent)
                    Text(label).font(Type.body).foregroundStyle(Theme.text).lineLimit(1).truncationMode(.tail)
                    Spacer(minLength: 8)
                    if let project = member.project, member.role == "root" {
                        Text(project).font(Type.tiny).foregroundStyle(Theme.textFaint)
                    }
                    if let ago = agoText(member.lastActivityAt) {
                        Text(ago).font(Type.tiny).foregroundStyle(Theme.textFaint)
                    }
                }
                .padding(.horizontal, 10).padding(.vertical, 7).contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if expanded {
                expandedBody.padding(.horizontal, 12).padding(.bottom, 10).padding(.leading, 6)
            }
        }
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 9, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous).strokeBorder(Theme.border, lineWidth: 1))
    }

    @ViewBuilder
    private var expandedBody: some View {
        if loading {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("loading steps…").font(Type.small).foregroundStyle(Theme.textFaint)
            }
        } else if failed {
            Text("couldn't load this session").font(Type.small).foregroundStyle(Theme.orange)
        } else if let detail {
            VStack(alignment: .leading, spacing: 6) {
                if detail.steps.isEmpty {
                    Text("no recorded steps for this session").font(Type.small).foregroundStyle(Theme.textFaint)
                } else {
                    ForEach(detail.steps) { StepCard(step: $0) }
                }
                if !detail.descendants.isEmpty {
                    SectionCaption(tone: Theme.textFaint, text: "Subagent sessions · \(detail.descendants.count)")
                    ForEach(detail.descendants) { DescendantRow(child: $0) }
                }
            }
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
