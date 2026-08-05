import SwiftUI

// Sessions: the server-paginated roots list (plan share first, cost as the
// reference) and the one-session deep view — expandable steps with their
// machine checks, the evidence enum in confidence colors, attributed model
// lanes, descendants, and the plan why-this-number block. The daemon's
// honesty semantics render verbatim; nothing is re-derived here.

struct SessionsPane: View {
    @EnvironmentObject var dashboard: DashboardStore
    @EnvironmentObject var selection: AppSelection

    var body: some View {
        HStack(spacing: 0) {
            VStack(spacing: 0) {
                ScrollBox {
                    VStack(spacing: 2) {
                        // Offscreen renders have no scrolling: cap the list so
                        // the snapshot shows the top of it plus the detail.
                        let rows = SnapshotMode.enabled ? Array(dashboard.sessions.prefix(9)) : dashboard.sessions
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

    private var footerText: String {
        let shown = dashboard.sessions.count
        let roots = dashboard.totalRootSessions.map(String.init) ?? "?"
        let total = dashboard.totalSessions.map(String.init) ?? "?"
        return "\(shown) of \(roots) root sessions · \(total) total in the store"
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

    var body: some View {
        HStack(spacing: 10) {
            StatusDot(color: Theme.statusColor(row.status), size: 7)
            VStack(alignment: .leading, spacing: 3) {
                Text(row.displayTitle)
                    .font(Type.rowTitle)
                    .foregroundStyle(Theme.text)
                    .lineLimit(1)
                    .truncationMode(.tail)
                HStack(spacing: 6) {
                    Text(row.client)
                        .font(.system(size: 9.5, weight: .semibold))
                        .foregroundStyle(Theme.clientColor(row.client))
                    if let project = row.project {
                        Text(project)
                            .font(.system(size: 10))
                            .foregroundStyle(Theme.textFaint)
                    }
                    if let count = row.related?.childSessionCount, count > 0 {
                        Text("×\(count) sub")
                            .font(.system(size: 9.5))
                            .foregroundStyle(Theme.textFaint)
                    }
                }
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
                        let shown = Array(descendants.prefix(8))
                        ForEach(Array(shown.enumerated()), id: \.element.id) { index, child in
                            HStack(spacing: 9) {
                                StatusDot(color: Theme.statusColor(child.status), size: 6)
                                Text(child.title ?? child.clientSessionIdShort ?? child.clientSessionId ?? "?")
                                    .font(Type.body)
                                    .foregroundStyle(Theme.text)
                                    .lineLimit(1)
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
                            if index < shown.count - 1 {
                                Rectangle().fill(Theme.border.opacity(0.6)).frame(height: 1)
                            }
                        }
                        if descendants.count > 8 {
                            Text("+ \(descendants.count - 8) more")
                                .font(Type.tiny)
                                .foregroundStyle(Theme.textFaint)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.horizontal, 9)
                                .padding(.vertical, 6)
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
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Chip(text: join.state ?? "unknown", tint: joinTint(join.state))
                    if let reason = join.reason {
                        Text(reason)
                            .font(Type.small)
                            .foregroundStyle(Theme.textMuted)
                    }
                }
            }
        }
    }
}

// MARK: - One expandable step

struct StepCard: View {
    let step: V1Step
    @State private var expanded = false

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
                        .lineLimit(expanded ? 3 : 1)
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
                    if let summary = step.summary, !summary.isEmpty {
                        Text(summary)
                            .font(Type.small)
                            .foregroundStyle(Theme.textMuted)
                            .fixedSize(horizontal: false, vertical: true)
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
                    }
                    if let next = step.nextStep, !next.isEmpty {
                        Label(next, systemImage: "arrow.turn.down.right")
                            .font(Type.small)
                            .foregroundStyle(Theme.textMuted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let files = step.files, !files.isEmpty {
                        Text("\(files.count) file\(files.count == 1 ? "" : "s"): \(files.prefix(4).joined(separator: ", "))\(files.count > 4 ? " …" : "")")
                            .font(Type.tiny)
                            .foregroundStyle(Theme.textFaint)
                            .lineLimit(2)
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
