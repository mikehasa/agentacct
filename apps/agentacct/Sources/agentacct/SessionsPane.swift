import SwiftUI

// Sessions: the server-paginated roots list (plan share first, cost as the
// reference) and the one-session deep view — expandable steps with their
// machine checks, the evidence enum in confidence colors, attributed model
// lanes, descendants, and the plan why-this-number block. The daemon's
// honesty semantics render verbatim; nothing is re-derived here.

struct SessionsPane: View {
    @EnvironmentObject var dashboard: DashboardStore
    @EnvironmentObject var selection: AppSelection
    @State private var query = ""
    @State private var clientFilter: String?
    @State private var sort: SessionSort = .latest

    enum SessionSort: String, CaseIterable, Identifiable {
        case latest = "latest"
        case plan = "plan %"
        case cost = "cost"
        var id: String { rawValue }
    }

    /// The loaded rows through the toolbar: search (title/project/id),
    /// client filter, sort. Client-side over the paginated walk — the footer
    /// still discloses how much of the store is loaded.
    private var visibleRows: [V1SessionRow] {
        var rows = dashboard.sessions
        if let clientFilter {
            rows = rows.filter { $0.client == clientFilter }
        }
        if !query.isEmpty {
            let needle = query.lowercased()
            rows = rows.filter { row in
                row.displayTitle.lowercased().contains(needle)
                    || (row.project ?? "").lowercased().contains(needle)
                    || row.clientSessionId.lowercased().contains(needle)
            }
        }
        switch sort {
        case .latest:
            return rows  // server order: recency
        case .plan:
            return rows.sorted { ($0.planPct ?? -1) > ($1.planPct ?? -1) }
        case .cost:
            return rows.sorted { ($0.usage?.estimatedCostUsd ?? -1) > ($1.usage?.estimatedCostUsd ?? -1) }
        }
    }

    private var loadedClients: [String] {
        var seen: [String] = []
        for row in dashboard.sessions where !seen.contains(row.client) {
            seen.append(row.client)
        }
        return seen.sorted()
    }

    var body: some View {
        HStack(spacing: 0) {
            VStack(spacing: 0) {
                toolbar
                Rectangle().fill(Theme.border.opacity(0.6)).frame(height: 1)
                ScrollBox {
                    VStack(spacing: 2) {
                        // Offscreen renders have no scrolling: cap the list so
                        // the snapshot shows the top of it plus the detail.
                        let rows = SnapshotMode.enabled ? Array(visibleRows.prefix(8)) : visibleRows
                        ForEach(rows) { row in
                            SessionRow(row: row, selected: selection.sessionId == row.id)
                                .onTapGesture { selection.sessionId = row.id }
                        }
                        if dashboard.truncated {
                            Button {
                                Task { await dashboard.loadMore() }
                            } label: {
                                HStack(spacing: 6) {
                                    if dashboard.isLoadingMore {
                                        ProgressView().controlSize(.small)
                                    } else {
                                        Image(systemName: "arrow.down.circle")
                                            .font(.system(size: 11))
                                    }
                                    Text("Load more")
                                        .font(Type.body)
                                }
                                .foregroundStyle(Theme.accent)
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 8)
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .padding(10)
                }
                Rectangle().fill(Theme.border).frame(height: 1)
                Text(footerText)
                    .font(.system(size: 10))
                    .foregroundStyle(Theme.textFaint)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 7)
            }
            .frame(width: 430)
            Rectangle().fill(Theme.border).frame(width: 1)
            detail
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Theme.surface.opacity(0.4))
        }
        .overlay(alignment: .bottom) {
            if let error = dashboard.errorText {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(Theme.red)
                    .padding(8)
                    .background(Theme.card, in: RoundedRectangle(cornerRadius: 8))
                    .padding(.bottom, 10)
            }
        }
    }

    private var toolbar: some View {
        HStack(spacing: 8) {
            HStack(spacing: 5) {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 10))
                    .foregroundStyle(Theme.textFaint)
                TextField("Search title · project · id", text: $query)
                    .textFieldStyle(.plain)
                    .font(Type.body)
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .background(Theme.card, in: RoundedRectangle(cornerRadius: 7, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 7, style: .continuous)
                .strokeBorder(Theme.border, lineWidth: 1))

            Menu {
                Button("All agents") { clientFilter = nil }
                Divider()
                ForEach(loadedClients, id: \.self) { client in
                    Button(client) { clientFilter = client }
                }
            } label: {
                HStack(spacing: 4) {
                    StatusDot(color: clientFilter.map { Theme.clientColor($0) } ?? Theme.textFaint, size: 5)
                    Text(clientFilter ?? "all")
                        .font(Type.small)
                }
            }
            .menuStyle(.borderlessButton)
            .fixedSize()

            Picker("", selection: $sort) {
                ForEach(SessionSort.allCases) { option in
                    Text(option.rawValue).tag(option)
                }
            }
            .pickerStyle(.menu)
            .fixedSize()
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
    }

    private var footerText: String {
        let shown = visibleRows.count
        let loaded = dashboard.sessions.count
        let roots = dashboard.totalRootSessions.map(String.init) ?? "?"
        let total = dashboard.totalSessions.map(String.init) ?? "?"
        if shown != loaded {
            return "\(shown) shown of \(loaded) loaded · \(roots) roots · \(total) total in the store"
        }
        return "\(loaded) of \(roots) root sessions · \(total) total in the store"
    }

    @ViewBuilder
    private var detail: some View {
        if let id = selection.sessionId, let row = selectedRow(id) {
            SessionDetail(row: row)
                .id(row.id)  // re-fetch when the selection changes
        } else {
            VStack(spacing: 10) {
                Image(systemName: "rectangle.stack")
                    .font(.system(size: 34, weight: .light))
                    .foregroundStyle(Theme.textFaint)
                Text("Select a session")
                    .font(.system(size: 13))
                    .foregroundStyle(Theme.textMuted)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    /// The selected row from the list — or from the loaded detail when a
    /// refresh dropped it off the current page (an open detail must never
    /// blank mid-read; review finding).
    private func selectedRow(_ id: String) -> V1SessionRow? {
        dashboard.sessions.first { $0.id == id }
            ?? (dashboard.detail?.session.id == id ? dashboard.detail?.session : nil)
    }
}

// MARK: - List row (plan share first, cost as the reference)

struct SessionRow: View {
    let row: V1SessionRow
    let selected: Bool
    @State private var hovering = false

    /// The TUI's steps cell: "6 · 4✓ 1▶ 1⚠" from the work counts.
    private var stepsSummary: String? {
        guard let counts = row.work?.counts, let total = counts.total, total > 0 else { return nil }
        var parts: [String] = []
        let done = (counts.completed ?? 0) + (counts.resolved ?? 0)
        if done > 0 { parts.append("\(done)✓") }
        if let active = counts.active, active > 0 { parts.append("\(active)▶") }
        if let blocked = counts.blocked, blocked > 0 { parts.append("\(blocked)⚠") }
        return "\(total) step\(total == 1 ? "" : "s")\(parts.isEmpty ? "" : " · " + parts.joined(separator: " "))"
    }

    private var statusWord: String? {
        row.status?.replacingOccurrences(of: "_", with: " ")
    }

    var body: some View {
        HStack(spacing: 10) {
            StatusDot(color: Theme.statusColor(row.status), size: 7)
            VStack(alignment: .leading, spacing: 3) {
                Text(row.displayTitle)
                    .font(Type.rowTitle)
                    .foregroundStyle(Theme.text)
                    .lineLimit(1)
                    .truncationMode(.tail)
                // One metadata line: the load-bearing bits (client, status,
                // steps, sub count) never wrap; the project name is the one
                // that yields, truncating in the middle.
                HStack(spacing: 6) {
                    Text(row.client)
                        .font(.system(size: 9.5, weight: .semibold))
                        .foregroundStyle(Theme.clientColor(row.client))
                        .fixedSize()
                    if let statusWord {
                        Text(statusWord)
                            .font(.system(size: 9.5, weight: .medium))
                            .foregroundStyle(Theme.statusColor(row.status))
                            .fixedSize()
                    }
                    if let stepsSummary {
                        Text(stepsSummary)
                            .font(.system(size: 9.5))
                            .monospacedDigit()
                            .foregroundStyle(Theme.textMuted)
                            .fixedSize()
                    }
                    if let project = row.project {
                        Text(project)
                            .font(.system(size: 10))
                            .foregroundStyle(Theme.textFaint)
                            .lineLimit(1)
                            .truncationMode(.middle)
                            .layoutPriority(-1)
                    }
                    if let count = row.related?.childSessionCount, count > 0 {
                        Text("×\(count) sub")
                            .font(.system(size: 9.5))
                            .foregroundStyle(Theme.textFaint)
                            .fixedSize()
                    }
                }
                .lineLimit(1)
            }
            Spacer(minLength: 10)
            VStack(alignment: .trailing, spacing: 3) {
                if let pct = Fmt.planPct(row.planPct) {
                    Text(pct)
                        .font(Type.metricS)
                        .foregroundStyle(Theme.accent)
                    Text(row.usage?.costText ?? "—")
                        .font(.system(size: 9.5))
                        .monospacedDigit()
                        .foregroundStyle(Theme.textFaint)
                } else {
                    Text(row.usage?.costText ?? "—")
                        .font(Type.metricS)
                        .foregroundStyle(Theme.text)
                    if let ts = row.lastActivityAt, let ago = agoText(ts) {
                        Text(ago)
                            .font(.system(size: 9.5))
                            .foregroundStyle(Theme.textFaint)
                    }
                }
            }
        }
        .padding(.horizontal, 11)
        .padding(.vertical, 8)
        .background(
            selected ? AnyShapeStyle(Theme.cardAlt) : (hovering ? AnyShapeStyle(Theme.cardAlt.opacity(0.5)) : AnyShapeStyle(.clear)),
            in: RoundedRectangle(cornerRadius: 8, style: .continuous)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .strokeBorder(selected ? Theme.accent.opacity(0.45) : .clear, lineWidth: 1)
        )
        .contentShape(Rectangle())
        .onHover { hovering = $0 }
    }
}

// MARK: - Deep detail (/v1/session)

struct SessionDetail: View {
    let row: V1SessionRow
    @EnvironmentObject var dashboard: DashboardStore
    @State private var showAllDescendants = false

    var body: some View {
        ScrollBox {
            VStack(alignment: .leading, spacing: Space.l) {
                header
                statGrid
                planBlock
                stepsSection
                descendantsSection
                attributionSection
            }
            .padding(Space.l)
        }
        .task(id: row.id) {
            await dashboard.fetchDetail(client: row.client, sessionId: row.clientSessionId)
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(row.displayTitle)
                .font(.system(size: 16, weight: .semibold))
                .foregroundStyle(Theme.text)
                .lineLimit(3)
            HStack(spacing: 6) {
                Chip(text: row.client, tint: Theme.clientColor(row.client))
                if let project = row.project { Chip(text: project, tint: Theme.textMuted) }
                if let duration = row.durationSeconds {
                    Chip(text: durationText(duration), tint: Theme.textMuted)
                }
                if let models = row.observedModels, !models.isEmpty {
                    Chip(text: models.joined(separator: " · "), tint: Theme.purple)
                }
                if row.instrumentationState == "pre_instrumentation" {
                    Chip(text: "pre-instrumentation", tint: Theme.textFaint)
                }
            }
            if let note = row.usageNote {
                Text(note)
                    .font(Type.small)
                    .foregroundStyle(Theme.textFaint)
            }
        }
    }

    @ViewBuilder
    private var statGrid: some View {
        if let usage = row.usage {
            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 8), count: 4), spacing: 8) {
                if let pct = Fmt.planPct(row.planPct) {
                    PanelTile(label: "weekly plan", value: pct,
                              detail: planSplitDetail, accent: Theme.accent)
                    PanelTile(label: "cost", value: usage.costText,
                              detail: usage.costConfidence?.replacingOccurrences(of: "_", with: " "))
                } else {
                    PanelTile(label: "cost", value: usage.costText,
                              detail: usage.costConfidence?.replacingOccurrences(of: "_", with: " "),
                              accent: Theme.blue)
                    PanelTile(label: "fresh", value: usage.freshTokens.map(UsageTotals.compact) ?? "—")
                }
                PanelTile(label: "cache read", value: usage.cacheReadTokens.map(UsageTotals.compact) ?? "—")
                PanelTile(label: "turns", value: usage.turnsTotal.map(String.init) ?? "—")
            }
        }
    }

    private var planSplitDetail: String? {
        guard let own = Fmt.planPct(row.planPctOwn) else { return nil }
        if let children = Fmt.planPct(row.planPctChildren) {
            return "own \(own) · subagents \(children)"
        }
        return "own \(own)"
    }

    /// The store-global detail, only when it belongs to THIS row — a stale
    /// payload from the previously selected row must never flash under the
    /// new header (review finding).
    private var loadedDetail: V1SessionDetail? {
        guard let detail = dashboard.detail, detail.session.id == row.id else { return nil }
        return detail
    }

    @ViewBuilder
    private var planBlock: some View {
        if let basis = loadedDetail?.plan?.basis {
            Text("plan estimate: \(basis)")
                .font(Type.tiny)
                .foregroundStyle(Theme.textFaint)
        }
    }

    // MARK: steps

    @ViewBuilder
    private var stepsSection: some View {
        if let error = dashboard.detailError {
            Text(error)
                .font(Type.small)
                .foregroundStyle(Theme.orange)
        } else if let detail = loadedDetail {
            if !detail.steps.isEmpty {
                VStack(alignment: .leading, spacing: Space.s) {
                    SectionCaption(tone: Theme.textMuted, text: "Steps · \(detail.steps.count)")
                    VStack(spacing: 6) {
                        ForEach(detail.steps) { step in
                            StepCard(step: step)
                        }
                    }
                }
            }
        } else {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("loading steps…")
                    .font(Type.small)
                    .foregroundStyle(Theme.textFaint)
            }
        }
    }

    // MARK: descendants

    @ViewBuilder
    private var descendantsSection: some View {
        if let descendants = loadedDetail?.descendants, !descendants.isEmpty {
            VStack(alignment: .leading, spacing: Space.s) {
                SectionCaption(tone: Theme.textMuted, text: "Subagent sessions · \(descendants.count)")
                Card(padding: 4) {
                    VStack(spacing: 0) {
                        let shown = showAllDescendants ? descendants : Array(descendants.prefix(8))
                        ForEach(Array(shown.enumerated()), id: \.element.id) { index, child in
                            DescendantRow(child: child)
                            if index < shown.count - 1 {
                                Rectangle().fill(Theme.border.opacity(0.6)).frame(height: 1)
                            }
                        }
                        if descendants.count > 8 {
                            Button {
                                withAnimation(.easeInOut(duration: 0.15)) { showAllDescendants.toggle() }
                            } label: {
                                Text(showAllDescendants
                                     ? "Show fewer"
                                     : "Show all \(descendants.count)")
                                    .font(Type.small)
                                    .foregroundStyle(Theme.accent)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .padding(.horizontal, 9)
                                    .padding(.vertical, 6)
                                    .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var attributionSection: some View {
        if let join = row.join {
            VStack(alignment: .leading, spacing: Space.s) {
                SectionCaption(tone: Theme.textMuted, text: "Attribution")
                // Attribution ≠ evidence: this answers "which recorded work
                // does this session's USAGE belong to" (the money↔work join);
                // evidence lives on each step (what the machine checks prove).
                Text("how this session's usage maps onto its recorded work — separate from each step's evidence")
                    .font(Type.tiny)
                    .foregroundStyle(Theme.textFaint)
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Chip(text: join.state ?? "unknown", tint: joinTint(join.state))
                    if let reason = join.reason {
                        Text(reason)
                            .font(Type.small)
                            .foregroundStyle(Theme.textMuted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                if let rows = join.rowStates, !rows.isEmpty {
                    HStack(spacing: 8) {
                        ForEach(rows.sorted(by: { $0.key < $1.key }), id: \.key) { state, count in
                            if count > 0 {
                                Text("\(state.replacingOccurrences(of: "_", with: " ")): \(count)")
                                    .font(Type.tiny.monospacedDigit())
                                    .foregroundStyle(Theme.textMuted)
                            }
                        }
                        if let vetoed = join.vetoedRows, vetoed > 0 {
                            Text("vetoed: \(vetoed)")
                                .font(Type.tiny.monospacedDigit())
                                .foregroundStyle(Theme.orange)
                        }
                    }
                }
                if let attributed = join.attributedWork, !attributed.isEmpty {
                    VStack(alignment: .leading, spacing: 3) {
                        // Position-keyed: a malformed row with no work_id/section_id
                        // must keep stable identity across renders (an id that
                        // minted a fresh UUID each access churned the list).
                        ForEach(Array(attributed.enumerated()), id: \.offset) { _, work in
                            HStack(spacing: 6) {
                                Image(systemName: "arrow.right")
                                    .font(.system(size: 8))
                                    .foregroundStyle(Theme.textFaint)
                                Text(work.title ?? work.sectionId ?? "?")
                                    .font(Type.tiny)
                                    .foregroundStyle(Theme.textMuted)
                                    .lineLimit(1)
                                if let confidence = work.joinConfidence {
                                    Chip(text: confidence.replacingOccurrences(of: "_", with: " "),
                                         tint: Theme.textFaint)
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

/// One subagent row: role-aware label (Task first line > title > agent type),
/// an agent-type chip, plan share and fresh tokens.
struct DescendantRow: View {
    let child: V1Descendant

    var body: some View {
        HStack(spacing: 9) {
            StatusDot(color: Theme.statusColor(child.status), size: 6)
            Text(child.displayTitle)
                .font(Type.body)
                .foregroundStyle(Theme.text)
                .lineLimit(1)
                .truncationMode(.tail)
            if let agentType = child.agentType {
                Chip(text: agentType, tint: Theme.purple)
            }
            Spacer(minLength: 8)
            if let pct = Fmt.planPct(child.planPct) {
                Text(pct)
                    .font(Type.numeric)
                    .foregroundStyle(Theme.accent)
            }
            if let tokens = child.usage?.freshTokens {
                Text(UsageTotals.compact(Int(tokens)))
                    .font(Type.tiny.monospacedDigit())
                    .foregroundStyle(Theme.textFaint)
                    .frame(width: 48, alignment: .trailing)
            }
        }
        .padding(.horizontal, 9)
        .padding(.vertical, 6)
        .help(child.task ?? child.displayTitle)
    }
}

// MARK: - One expandable step

struct StepCard: View {
    let step: V1Step
    @State private var expanded = false

    /// WHY this confidence level — mirrors the daemon's evidence_status
    /// derivation (failed: any live failing check; strong: any pass; weak:
    /// claims without a pass; none: nothing recorded) using the same inputs
    /// it used, so the label never needs to be taken on faith.
    private var evidenceExplanation: String? {
        guard let status = step.evidenceStatus else { return nil }
        let checks = step.checks ?? []
        let live = checks.filter { $0.supersessionState != "superseded" }
        let passed = live.filter { $0.result == "passed" }.count
        let failed = live.filter { $0.result == "failed" || $0.result == "error" }.count
        let files = step.files?.count ?? 0
        switch status {
        case "failed":
            return "failed — \(failed) failing check\(failed == 1 ? "" : "s") with no later pass superseding \(failed == 1 ? "it" : "them")"
        case "strong":
            return "strong — \(passed) passing machine check\(passed == 1 ? "" : "s") recorded"
        case "weak":
            if checks.isEmpty && files > 0 {
                return "weak — \(files) file\(files == 1 ? "" : "s") claimed, but no machine check proves the work"
            }
            return "weak — checks were recorded but none passed"
        case "none":
            return "none — no machine checks and no file claims were recorded for this step"
        default:
            return nil
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(.easeInOut(duration: 0.15)) { expanded.toggle() }
            } label: {
                HStack(spacing: 9) {
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 8, weight: .semibold))
                        .foregroundStyle(Theme.textFaint)
                        .frame(width: 10)
                    StatusDot(color: Theme.statusColor(step.latestStatus))
                    Text(step.title ?? step.sectionId ?? "untitled")
                        .font(Type.body)
                        .foregroundStyle(Theme.text)
                        .lineLimit(expanded ? nil : 1)
                        .fixedSize(horizontal: false, vertical: expanded)
                    Spacer(minLength: 8)
                    if let kind = step.kind, kind != "unknown" {
                        Chip(text: kind, tint: Theme.textMuted)
                    }
                    if let checks = step.checks, !checks.isEmpty {
                        Text("\(checks.count) check\(checks.count == 1 ? "" : "s")")
                            .font(Type.tiny)
                            .foregroundStyle(Theme.textFaint)
                    }
                    if let evidence = step.evidenceStatus {
                        Chip(text: evidence, tint: evidenceTint(evidence))
                    }
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if expanded {
                // The expanded step is the DETAILED view: nothing here is
                // truncated — the user opened it to see everything.
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 8) {
                        if let status = step.latestStatus {
                            Chip(text: status.replacingOccurrences(of: "_", with: " "),
                                 tint: Theme.statusColor(step.latestStatus))
                        }
                        if let ago = agoText(step.updatedAt) {
                            Text("updated \(ago)")
                                .font(Type.tiny)
                                .foregroundStyle(Theme.textFaint)
                        }
                        if let usage = step.usage, let tokens = usage.totalTokens, tokens > 0 {
                            Text("\(UsageTotals.compact(Int(tokens))) tok · \(usage.costText)")
                                .font(Type.tiny.monospacedDigit())
                                .foregroundStyle(Theme.textMuted)
                        }
                        if let models = step.models, !models.isEmpty {
                            Text(models.compactMap { $0.model ?? "unknown model" }.joined(separator: " · "))
                                .font(Type.tiny)
                                .foregroundStyle(Theme.purple)
                        }
                    }
                    if let why = evidenceExplanation {
                        Text(why)
                            .font(Type.tiny)
                            .foregroundStyle(evidenceTint(step.evidenceStatus))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let summary = step.summary, !summary.isEmpty {
                        Text(summary)
                            .font(Type.small)
                            .foregroundStyle(Theme.textMuted)
                            .fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled)
                    }
                    if let checks = step.checks, !checks.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            ForEach(checks) { check in
                                CheckRow(check: check)
                            }
                        }
                    }
                    if let blocker = step.blocker, !blocker.isEmpty {
                        Label(blocker, systemImage: "hand.raised.fill")
                            .font(Type.small)
                            .foregroundStyle(Theme.orange)
                            .fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled)
                    }
                    if let next = step.nextStep, !next.isEmpty {
                        Label(next, systemImage: "arrow.turn.down.right")
                            .font(Type.small)
                            .foregroundStyle(Theme.textMuted)
                            .fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled)
                    }
                    if let files = step.files, !files.isEmpty {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("\(files.count) file\(files.count == 1 ? "" : "s")")
                                .font(Type.tiny.weight(.semibold))
                                .foregroundStyle(Theme.textFaint)
                            ForEach(files, id: \.self) { file in
                                Text(file)
                                    .font(Type.tiny.monospaced())
                                    .foregroundStyle(Theme.textFaint)
                                    .fixedSize(horizontal: false, vertical: true)
                                    .textSelection(.enabled)
                            }
                        }
                    }
                }
                .padding(.horizontal, 12)
                .padding(.bottom, 10)
                .padding(.leading, 18)
            }
        }
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 9, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .strokeBorder(Theme.border, lineWidth: 1)
        )
    }
}

/// One machine check: the TUI's ✓/✗/»/• marks with type, summary, exit code.
struct CheckRow: View {
    let check: V1Check

    private var mark: (String, Color) {
        switch check.result {
        case "passed": return ("checkmark", Theme.green)
        case "failed", "error": return ("xmark", Theme.red)
        case "skipped": return ("chevron.right.2", Theme.orange)
        default: return ("circle.fill", Theme.textFaint)
        }
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 7) {
            Image(systemName: mark.0)
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(mark.1)
                .frame(width: 12)
            Text(check.evidenceType ?? "check")
                .font(Type.tiny.weight(.semibold))
                .foregroundStyle(Theme.textMuted)
            Text(check.summary ?? "")
                .font(Type.tiny)
                .foregroundStyle(Theme.textMuted)
                .lineLimit(2)
            if let exit = check.exitCode {
                Text("(exit \(exit))")
                    .font(Type.tiny.monospacedDigit())
                    .foregroundStyle(Theme.textFaint)
            }
            if check.supersessionState == "superseded" {
                Chip(text: "superseded", tint: Theme.textFaint)
            }
            Spacer(minLength: 0)
        }
    }
}
