import SwiftUI

// Work Receipt record components — the cards that render one Task's Receipt
// as an enterprise record page (summary strip, dimensions ledger, evidence
// coverage, checks, gaps, evidence sources). Consumed by WorkPane.swift.
//
// A Receipt answers the 8 questions for one converged Task and keeps the two
// honesty axes visibly SEPARATE: decision status (what a human/agent SAYS) and
// evidence strength (how well that is PROVEN). Both colors and both labels are
// distinct so an agent's "done" never reads as machine verification. Honesty
// rides the payload — nothing here re-derives an axis or invents a number.

/// Decision-status color (what is CLAIMED). Delegates to the shared
/// ``DecisionTintClass`` — deliberately a different lookup from the evidence
/// tiers so the two axes can never share a palette: the decision axis never
/// wears green for claims ("completed" stays ink), and coral is failure-only.
func receiptDecisionTint(_ key: String?) -> Color {
    DecisionTintClass.forKey(key).text
}

/// Evidence-coverage color, keyed on the strongest tier present. Delegates to
/// the shared ``EvidenceTierStyle`` ramp (green only for externally-verified
/// independent evidence). A failing check is never positive proof — it lands
/// on the decision axis as a finding, not here.
func receiptEvidenceTint(_ key: String?) -> Color {
    EvidenceTierStyle.forGrade(key).tint
}

/// Per-step evidence-grade color — the same shared tier ramp, so step rows and
/// receipt headlines can never disagree.
func stepGradeTint(_ grade: String?) -> Color {
    EvidenceTierStyle.forGrade(grade).tint
}

/// Short chip label for a per-step grade (the daemon's own tier words).
func stepGradeLabel(_ grade: String?) -> String {
    EvidenceTierStyle.forGrade(grade).label
}

/// Provenance-source chip color: v7 provenance chips are neutral — the chip
/// TEXT names the source; color never ranks provenance.
func receiptSourceTint(_ source: String) -> Color {
    Theme.muted
}

/// The app-wide cost prefix grammar, applied wherever a receipt cost renders:
/// bare `$` only for a complete client-reported figure, `~$` for a knowingly
/// partial one, `≈$` for every other estimate. One prefix per basis — the
/// same number never appears with and without its estimate marker.
func receiptCostDisplay(_ usd: Double, complete: Bool?, confidence: String?) -> String {
    if complete == true && confidence == "client_reported" { return Fmt.dollars(usd) }
    if complete == false { return Fmt.dollars(usd, prefix: "~$") }
    return Fmt.dollars(usd, prefix: "≈$")
}

/// Human phrasing for a cost basis key (raw keys stay in provenance chips).
func costBasisLabel(_ basis: String?) -> String {
    switch basis {
    case "pricing_table": return "pricing estimate"
    case "local_client_session": return "client-reported"
    case "provider_invoice": return "provider billed"
    case "user_subscription": return "subscription equivalent"
    case "mixed": return "mixed basis"
    case nil, "none": return "basis unknown"
    case .some(let other): return other.replacingOccurrences(of: "_", with: " ")
    }
}

/// Human phrasing for an asserted-by key (same map the dashboard uses).
func assertedByLabel(_ raw: String?) -> String? {
    switch raw {
    case "agent_report": return "agent reported"
    case "machine": return "machine checked"
    case "human": return "human reviewed"
    case "inferred": return "state inferred"
    case nil: return nil
    case .some(let other): return other.replacingOccurrences(of: "_", with: " ")
    }
}

// MARK: - Record summary strip

/// The record page's summary strip: Actions · Est. cost · Elapsed · Checks ·
/// Sessions, each a caps caption over an 18/700 mono value with 1px verticals
/// between the cells. Absent facts are named ("not recorded"), never zeroed.
struct RecordSummaryStrip: View {
    let receipt: Receipt
    let summary: ReceiptSummary?

    private struct Cell: Identifiable {
        let id: String
        let label: String
        let value: String?
        let qualifier: String?
        let absent: String?
    }

    private var cells: [Cell] {
        let actions = receipt.dimensions.actions
        let cost = receipt.dimensions.cost
        let evidence = receipt.dimensions.evidence

        let actionCell: Cell
        if let total = actions.toolCategoryTotal {
            actionCell = Cell(id: "actions", label: "Actions", value: "\(total)", qualifier: "events", absent: nil)
        } else {
            actionCell = Cell(id: "actions", label: "Actions", value: nil, qualifier: nil, absent: "not instrumented")
        }

        let costCell: Cell
        if let usd = cost.estimatedCostUsd {
            var qualifier = costBasisLabel(cost.costBasis)
            if cost.costComplete == false { qualifier += " · partial" }
            if let pct = Fmt.planPct(cost.planShare?.pct) { qualifier += " · \(pct) wkly" }
            costCell = Cell(
                id: "cost",
                label: "Est. cost",
                value: receiptCostDisplay(usd, complete: cost.costComplete, confidence: cost.costConfidence),
                qualifier: qualifier,
                absent: nil
            )
        } else {
            costCell = Cell(id: "cost", label: "Est. cost", value: nil, qualifier: nil, absent: "no priced usage")
        }

        let elapsedCell: Cell
        if let seconds = receipt.durationSeconds, seconds > 0 {
            elapsedCell = Cell(id: "elapsed", label: "Elapsed", value: durationText(seconds), qualifier: nil, absent: nil)
        } else {
            elapsedCell = Cell(id: "elapsed", label: "Elapsed", value: nil, qualifier: nil, absent: "not recorded")
        }

        let checksCell: Cell
        if let total = evidence.checksTotal, total > 0 {
            let failed = evidence.checksFailed ?? 0
            checksCell = Cell(
                id: "checks",
                label: "Checks",
                value: "\(evidence.checksPassed ?? 0)/\(total)",
                qualifier: failed > 0 ? "passed · \(failed) failed" : "passed",
                absent: nil
            )
        } else {
            checksCell = Cell(id: "checks", label: "Checks", value: nil, qualifier: nil, absent: "none recorded")
        }

        let sessionsCell: Cell
        if let count = summary?.sessionCount ?? receipt.dimensions.task.boundary?.sessionCount {
            let roots = receipt.sessions?.count ?? 0
            sessionsCell = Cell(
                id: "sessions",
                label: "Sessions",
                value: "\(count)",
                qualifier: roots > 1 ? "\(roots) roots" : nil,
                absent: nil
            )
        } else {
            sessionsCell = Cell(id: "sessions", label: "Sessions", value: nil, qualifier: nil, absent: "not recorded")
        }

        return [actionCell, costCell, elapsedCell, checksCell, sessionsCell]
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 0) {
                ForEach(Array(cells.enumerated()), id: \.element.id) { index, cell in
                    if index > 0 {
                        // Fixed-height vertical: an unbounded Rectangle would
                        // stretch the strip to the page height.
                        Rectangle().fill(Theme.hairline).frame(width: 1, height: 46)
                    }
                    VStack(alignment: .leading, spacing: 6) {
                        CapsLabel(text: cell.label)
                        if let value = cell.value {
                            // Qualifier under the value: cells are narrow and a
                            // basis word must never wrap the number itself.
                            VStack(alignment: .leading, spacing: 2) {
                                Text(value).font(Type.kpi).foregroundStyle(Theme.ink)
                                if let qualifier = cell.qualifier {
                                    Text(qualifier).font(Type.dataSmall).foregroundStyle(Theme.muted)
                                        .lineLimit(1)
                                }
                            }
                        } else {
                            // Absence is a named state at value position — never "0".
                            Text(cell.absent ?? "not recorded")
                                .font(Type.body).foregroundStyle(Theme.muted)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.leading, index > 0 ? Space.l : 0)
                }
            }
            Rectangle().fill(Theme.hairline).frame(height: 1).padding(.top, Space.m)
        }
    }
}

// MARK: - Receipt dimensions

/// The receipt-dimensions ledger: one row per dimension — name, value,
/// provenance chips, and the dimension's own gaps inline as named amber facts.
struct RecordDimensionsCard: View {
    let receipt: Receipt

    var body: some View {
        Card(padding: Space.xl) {
            VStack(alignment: .leading, spacing: 0) {
                Text("Receipt dimensions").font(Type.titleCard).foregroundStyle(Theme.ink)
                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.top, Space.m)
                dimensionRow("Task", taskSummary,
                             provenance: receipt.dimensions.task.provenance,
                             gaps: receipt.dimensions.task.gaps)
                hairline
                dimensionRow("Actors", actorsSummary,
                             provenance: receipt.dimensions.actors.provenance,
                             gaps: receipt.dimensions.actors.gaps)
                hairline
                actionsRow
                hairline
                dimensionRow("Cost", costSummary,
                             provenance: receipt.dimensions.cost.provenance,
                             gaps: receipt.dimensions.cost.gaps)
                hairline
                dimensionRow("Checks", evidenceSummary,
                             provenance: receipt.dimensions.evidence.provenance,
                             gaps: receipt.dimensions.evidence.gaps)
                hairline
                dimensionRow("Outcome", outcomeSummary,
                             provenance: receipt.dimensions.outcome.provenance,
                             gaps: receipt.dimensions.outcome.gaps,
                             verbatimValue: true)
            }
        }
    }

    private var hairline: some View {
        Rectangle().fill(Theme.hairline).frame(height: 1)
    }

    private func dimensionRow(
        _ name: String,
        _ summary: String,
        provenance: [String]?,
        gaps: [String]?,
        verbatimValue: Bool = false
    ) -> some View {
        HStack(alignment: .top, spacing: Space.l) {
            Text(name).font(Type.rowLabel).foregroundStyle(Theme.ink)
                .frame(width: 128, alignment: .leading)
            VStack(alignment: .leading, spacing: 6) {
                if verbatimValue {
                    // Outcome statements quote agent text — never parse as markdown.
                    Text(verbatim: summary).font(Type.body).foregroundStyle(Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    Text(summary).font(Type.body).foregroundStyle(Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let provenance, !provenance.isEmpty {
                    HStack(spacing: 6) {
                        ForEach(provenance, id: \.self) { source in
                            ProvenanceChip(text: source)
                        }
                    }
                }
                ForEach(gaps ?? [], id: \.self) { gap in
                    // A dimension's own blind spot, named where the value lives.
                    HStack(spacing: 6) {
                        EvidencePip(shape: .hollow, tint: Theme.amber)
                        Text(gap).font(Type.caption).foregroundStyle(Theme.amber)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.vertical, Space.m)
    }

    // Actions renders touched paths + commands beneath the category summary.
    // Each list is an in-place disclosure: collapsed shows the daemon's honest
    // preview exactly as before; "Show all N" grows a height-capped scroll
    // region in place (no second-level page, no unbounded page growth).
    private var actionsRow: some View {
        VStack(alignment: .leading, spacing: 2) {
            dimensionRow("Actions", actionsSummary,
                         provenance: receipt.dimensions.actions.provenance,
                         gaps: receipt.dimensions.actions.gaps)
            let dim = receipt.dimensions.actions
            let files = actionsTouchedPreview
            let commands = actionsCommandsPreview
            let toolRows = fullToolRows
            // The tools disclosure must stay reachable for read/search-only
            // receipts (tool names without any touched file or command).
            let hasElidedTools = (dim.toolNamesElided ?? 0) > 0 && !toolRows.isEmpty
            if !files.shown.isEmpty || !commands.shown.isEmpty || hasElidedTools {
                VStack(alignment: .leading, spacing: 2) {
                    ActionListDisclosure(
                        preview: files.shown,
                        elided: files.elided,
                        full: dim.touchedFiles ?? files.shown,
                        noun: "files",
                        rowPrefix: ""
                    )
                    ActionListDisclosure(
                        preview: commands.shown,
                        elided: commands.elided,
                        full: dim.commands ?? commands.shown,
                        noun: "commands",
                        rowPrefix: "$ "
                    )
                    if hasElidedTools {
                        ActionListDisclosure(
                            preview: [],
                            elided: dim.toolNamesElided ?? 0,
                            full: toolRows,
                            noun: "tools",
                            rowPrefix: ""
                        )
                    }
                }
                .padding(.leading, 128 + Space.l)
                .padding(.bottom, Space.m)
                .padding(.top, -Space.s)
            }
        }
    }

    /// The full tool tally as display rows, daemon order (count desc, name asc)
    /// — the same ordering rule the daemon's preview uses.
    private var fullToolRows: [String] {
        let counts = receipt.dimensions.actions.toolNameCounts ?? [:]
        return counts
            .filter { $0.value > 0 }
            .sorted { lhs, rhs in
                if lhs.value != rhs.value { return lhs.value > rhs.value }
                return lhs.key < rhs.key
            }
            .map { "\($0.key)×\($0.value)" }
    }

    // The daemon computes the preview slice + overflow (the single source of
    // truth for the cap); the app renders those directly so it can never drift
    // from the CLI/TUI. Fallback (older payload): the full list.
    private var actionsTouchedPreview: (shown: [String], elided: Int) {
        let dim = receipt.dimensions.actions
        if let preview = dim.touchedFilesPreview {
            return (preview, dim.touchedFilesElided ?? 0)
        }
        return (dim.touchedFiles ?? [], 0)
    }

    private var actionsCommandsPreview: (shown: [String], elided: Int) {
        let dim = receipt.dimensions.actions
        if let preview = dim.commandsPreview {
            return (preview, dim.commandsElided ?? 0)
        }
        return (dim.commands ?? [], 0)
    }

    // MARK: dimension summaries

    private var taskSummary: String {
        let dim = receipt.dimensions.task
        var parts = (dim.objectives ?? []).prefix(2).joined(separator: "; ")
        if parts.isEmpty { parts = "no objective recorded" }
        if let project = dim.boundary?.project { parts += " · project \(project)" }
        return parts
    }

    private var actorsSummary: String {
        let dim = receipt.dimensions.actors
        var parts: [String] = []
        if let agent = dim.primaryAgent { parts.append(agent) }
        if let models = dim.models, !models.isEmpty { parts.append(models.joined(separator: ", ")) }
        if let subagents = dim.subagentSessionCount, subagents > 0 { parts.append("\(subagents) subagents") }
        return parts.isEmpty ? "no actor recorded" : parts.joined(separator: " · ")
    }

    private var actionsSummary: String {
        let dim = receipt.dimensions.actions
        let counts = dim.toolCategoryCounts ?? [:]
        let categories = counts.isEmpty
            ? "not instrumented"
            : counts.sorted { $0.key < $1.key }.map { "\($0.key)×\($0.value)" }.joined(separator: " ")
        var summary = "\(categories) · touched \(dim.touchedFileCount ?? 0) file(s)"
        if let commandCount = dim.commandCount, commandCount > 0 {
            summary += " · ran \(commandCount) command(s)"
        }
        if let preview = dim.toolNamesPreview, !preview.isEmpty {
            var tools = preview.map { "\($0.name)×\($0.count)" }.joined(separator: "  ")
            let elided = dim.toolNamesElided ?? 0
            if elided > 0 { tools += "  … +\(elided) more" }
            summary += "\ntools: \(tools)"
        }
        return summary
    }

    private var costSummary: String {
        let dim = receipt.dimensions.cost
        // Token tally — same daemon-computed numbers, so a user can see the
        // volume behind (or despite the absence of) the dollar estimate. Zero
        // components stay silent; an older payload without the block shows
        // nothing. Rendered even when nothing is priced: recorded volume is a
        // fact, and "no priced usage" only names the missing dollars.
        var tokensLine: String?
        if let tokens = dim.tokens, let total = tokens.total, total > 0 {
            var parts = ["\(UsageTotals.compact(total)) total"]
            if let fresh = tokens.fresh, fresh > 0 {
                parts.append("\(UsageTotals.compact(fresh)) fresh")
            }
            if let cacheCreation = tokens.cacheCreation, cacheCreation > 0 {
                parts.append("\(UsageTotals.compact(cacheCreation)) cache write")
            }
            if let cacheRead = tokens.cacheRead, cacheRead > 0 {
                parts.append("\(UsageTotals.compact(cacheRead)) cache read")
            }
            tokensLine = "tokens: " + parts.joined(separator: " · ")
        }
        guard let cost = dim.estimatedCostUsd else {
            // The share is token-derived, not dollar-derived — an unpriced
            // task with a calibrated share still states it.
            var absent = "no priced usage"
            if let share = dim.planShare?.text { absent += " · \(share)" }
            guard let tokensLine else { return absent }
            return absent + "\n" + tokensLine
        }
        let display = receiptCostDisplay(cost, complete: dim.costComplete, confidence: dim.costConfidence)
        var line = "\(display) · \(costBasisLabel(dim.costBasis))\((dim.costComplete ?? true) ? "" : " (partial)")"
        // The task's share of the weekly plan — shown only once calibrated
        // (the daemon sends null until then; absence stays a named state on
        // the Limits pane, never a number here).
        if let share = dim.planShare?.text {
            line += " · \(share)"
        }
        if let tokensLine { line += "\n" + tokensLine }
        return line
    }

    private var evidenceSummary: String {
        let dim = receipt.dimensions.evidence
        return "\(dim.checksTotal ?? 0) checks · \(dim.checksPassed ?? 0) passed · \(dim.checksFailed ?? 0) failed"
    }

    private var outcomeSummary: String {
        let dim = receipt.dimensions.outcome
        var line = "\(dim.decisionStatus ?? "unknown") · \(assertedByLabel(dim.assertedBy) ?? "unattested")"
        if let statement = dim.statement, !statement.isEmpty {
            line += "\n“\(statement)”"
        }
        return line
    }
}

// MARK: - Action list disclosure

/// One Actions sub-list (files / commands / tools) as an in-place disclosure.
///
/// Collapsed = the daemon's capped preview verbatim (the cap stays the daemon's
/// single source of truth) with the overflow line turned into a control.
/// Expanded = the FULL list — already riding the payload, no extra fetch — in a
/// height-capped internal scroll region, so 1,000 commands never stretch the
/// record page and never need a second-level page. Rows wrap and are selectable
/// when expanded: the point of expanding is to actually read and copy them.
/// Snapshot mode renders the collapsed static text exactly as before (the
/// offscreen renderer can't drive buttons or ScrollViews).
private struct ActionListDisclosure: View {
    let preview: [String]
    let elided: Int
    let full: [String]
    let noun: String
    let rowPrefix: String
    @State private var expanded = false

    var body: some View {
        if expanded {
            expandedList
        } else {
            collapsedList
        }
    }

    @ViewBuilder
    private var collapsedList: some View {
        ForEach(Array(preview.enumerated()), id: \.offset) { _, row in
            // verbatim: commands/paths are untrusted text — never interpret as
            // markdown (Text's LocalizedStringKey init would).
            Text(verbatim: rowPrefix + row).font(Type.dataSmall).foregroundStyle(Theme.muted)
                .lineLimit(1).truncationMode(.middle)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        if elided > 0 {
            if SnapshotMode.enabled {
                // Static overflow only under a rendered preview; a preview-less
                // list (tools) would otherwise print a dangling duplicate of
                // the summary's own "+N more".
                if !preview.isEmpty {
                    Text("… +\(elided) more \(noun)").font(Type.dataSmall).foregroundStyle(Theme.muted)
                }
            } else {
                Button {
                    withAnimation(Motion.contentUpdate) { expanded = true }
                } label: {
                    HStack(spacing: 4) {
                        Image(systemName: "chevron.down")
                            .font(.system(size: 8, weight: .semibold))
                        Text("Show all \(full.count) \(noun)").font(Type.dataSmall)
                    }
                    .foregroundStyle(Theme.accent)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("receipt.actions.expand.\(noun)")
            }
        }
    }

    @ViewBuilder
    private var expandedList: some View {
        VStack(alignment: .leading, spacing: 0) {
            Rectangle().fill(Theme.hairline).frame(height: 1)
            ScrollView(showsIndicators: true) {
                LazyVStack(alignment: .leading, spacing: 3) {
                    ForEach(Array(full.enumerated()), id: \.offset) { _, row in
                        Text(verbatim: rowPrefix + row).font(Type.dataSmall).foregroundStyle(Theme.muted)
                            .fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .padding(.vertical, Space.s)
                .textSelection(.enabled)
            }
            .frame(maxHeight: 340)
            Rectangle().fill(Theme.hairline).frame(height: 1)
        }
        Button {
            withAnimation(Motion.contentUpdate) { expanded = false }
        } label: {
            HStack(spacing: 4) {
                Image(systemName: "chevron.up").font(.system(size: 8, weight: .semibold))
                Text("Collapse \(noun)").font(Type.dataSmall)
            }
            .foregroundStyle(Theme.accent)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("receipt.actions.collapse.\(noun)")
    }
}

// MARK: - Disposition controls

/// The human attention controls for one finding or blocker: Mark reviewed,
/// Resolve… (note REQUIRED — recorded as your assertion, never machine
/// verification), Reopen. Posts the append-only disposition through the
/// daemon and surfaces its own conflict copy verbatim ("blocker changed…"),
/// so optimistic-concurrency refusals read as facts, not mystery failures.
struct DispositionControls: View {
    let kind: String
    let state: String
    let revision: Int
    let taskId: String
    var targetDigest: String? = nil
    var blockedEventId: String? = nil
    @EnvironmentObject var dashboard: DashboardStore
    @State private var resolvePopoverShown = false
    @State private var note = ""
    @State private var busy = false
    @State private var errorText: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: Space.s) {
                if busy {
                    ProgressView().controlSize(.small)
                } else {
                    if state == "open" {
                        actionButton("Mark reviewed") { post("mark_reviewed", note: nil) }
                    }
                    if state != "resolved" {
                        Button {
                            resolvePopoverShown = true
                        } label: {
                            Text("Resolve…").font(Type.captionSemibold).foregroundStyle(Theme.accent)
                        }
                        .buttonStyle(QuietButtonStyle())
                        .accessibilityIdentifier("disposition.resolve.\(kind)")
                        .popover(isPresented: $resolvePopoverShown, arrowEdge: .bottom) {
                            resolveSheet
                        }
                    }
                    if state != "open" {
                        actionButton("Reopen") { post("reopen", note: nil) }
                    }
                }
            }
            if let errorText {
                Text(verbatim: errorText)
                    .font(Type.dataSmall).foregroundStyle(Theme.coral)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func actionButton(_ label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(label).font(Type.captionSemibold).foregroundStyle(Theme.accent)
        }
        .buttonStyle(QuietButtonStyle())
        .accessibilityIdentifier("disposition.\(label.lowercased().replacingOccurrences(of: " ", with: "-")).\(kind)")
    }

    private var resolveSheet: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            CapsLabel(text: "Resolve \(kind)")
            Text("Say what resolved it — recorded as your assertion, never machine verification.")
                .font(Type.caption).foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
            TextField("e.g. fixed by hand in a later commit", text: $note, axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .font(Type.body)
                .lineLimit(2...4)
                .frame(width: 300)
            HStack {
                Spacer()
                Button {
                    resolvePopoverShown = false
                    post("resolve", note: note)
                } label: {
                    Text("Record resolve").font(Type.captionSemibold)
                }
                .disabled(note.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .accessibilityIdentifier("disposition.record-resolve.\(kind)")
            }
        }
        .padding(Space.l)
    }

    private func post(_ action: String, note: String?) {
        busy = true
        errorText = nil
        Task {
            do {
                try await dashboard.postDisposition(
                    kind: kind,
                    action: action,
                    expectedRevision: revision,
                    note: note,
                    targetDigest: targetDigest,
                    blockedEventId: blockedEventId,
                    refreshTaskId: taskId
                )
            } catch {
                errorText = error.localizedDescription
            }
            busy = false
        }
    }
}

// MARK: - Blocker callout

/// WHY a Task is blocked, in the agent's own words, right under the headline —
/// previously this lived three clicks deep in an expanded step card. Shows the
/// newest blocker (text, next step, when), and states staleness as a fact when
/// later steps completed after it (never re-grading the sticky blocked word).
struct BlockerCallout: View {
    let blocker: ReceiptBlocker
    let taskId: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: "hand.raised")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Theme.coral)
                    .accessibilityHidden(true)
                Text(blocker.stepTitle ?? "Blocked step")
                    .font(Type.rowLabel).foregroundStyle(Theme.ink)
                    .lineLimit(2)
                Spacer(minLength: Space.s)
                // "last updated": the projection carries the step's last-activity
                // time, which later non-terminal events can bump past the moment
                // the blocker itself was recorded.
                if let ago = agoText(blocker.updatedAt) {
                    Text("last updated \(ago)").font(Type.dataSmall).foregroundStyle(Theme.muted)
                }
            }
            if let text = blocker.text {
                // verbatim: the blocker is agent-authored text, never markdown.
                Text(verbatim: text)
                    .font(Type.body).foregroundStyle(Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            if let next = blocker.nextStep {
                Text(verbatim: "next: \(next)")
                    .font(Type.caption).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            if let later = blocker.laterCompletedSteps, later > 0 {
                // The count IS the fact; whether it cleared the blocker is not
                // in the data, so the copy never speculates.
                HStack(spacing: 6) {
                    EvidencePip(shape: .hollow, tint: Theme.amber)
                    Text("\(later) step\(later == 1 ? "" : "s") completed after this blocker's last update.")
                        .font(Type.caption).foregroundStyle(Theme.amber)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if let count = blocker.blockedStepCount, count > 1 {
                // "steps with recorded blockers": sticky blocker text keeps a
                // step in this count even when its latest status moved on.
                Text("+\(count - 1) more step\(count == 2 ? "" : "s") with recorded blockers in Sessions & steps below")
                    .font(Type.dataSmall).foregroundStyle(Theme.muted)
            }
            if let state = blocker.disposition?.state, state != "open" {
                Text(verbatim: "marked \(state) by you"
                     + (blocker.disposition?.note.map { " — \($0)" } ?? ""))
                    .font(Type.dataSmall).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // Resolve/review this exact blocker. Snapshots skip the controls
            // (the offscreen renderer cannot drive popovers or POSTs).
            if !SnapshotMode.enabled, let blockedEventId = blocker.blockedEventId {
                DispositionControls(
                    kind: "blocker",
                    state: blocker.disposition?.state ?? "open",
                    revision: blocker.dispositionRevision ?? 0,
                    taskId: taskId,
                    blockedEventId: blockedEventId
                )
            }
        }
        .padding(Space.l)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.tintCoral, in: RoundedRectangle(cornerRadius: Metrics.radius))
        .accessibilityIdentifier("receipt.blocker")
    }
}

// MARK: - Evidence coverage

/// The evidence-coverage card: the checked/checkable headline, a coverage bar
/// whose segment widths are strictly proportional to the tier counts, a
/// counted legend wearing the pip shapes, and the honesty ledger.
struct RecordCoverageCard: View {
    let evidence: ReceiptEvidence
    let schemaVersion: String

    private var tiers: [(grade: String, count: Int)] {
        let byTier = evidence.byTier
        return [
            ("externally_verified", byTier?.externallyVerified ?? 0),
            ("independently_checked", byTier?.independentlyChecked ?? 0),
            ("self_checked", byTier?.selfChecked ?? 0),
            ("unchecked", byTier?.unchecked ?? 0),
        ]
    }

    var body: some View {
        Card(padding: Space.xl) {
            VStack(alignment: .leading, spacing: 0) {
                HStack {
                    Text("Evidence coverage").font(Type.titleCard).foregroundStyle(Theme.ink)
                    Spacer()
                    Text(schemaVersion).font(Type.dataSmall).foregroundStyle(Theme.muted)
                }
                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.vertical, Space.m)

                if evidence.gradeable == true, let checkable = evidence.checkableTotal, checkable > 0 {
                    Text("\(evidence.checkedTotal ?? 0) of \(checkable) checkable steps checked")
                        .font(Face.sansFont(16, .semibold))
                        .foregroundStyle(Theme.ink)
                    CoverageBar(segments: tiers.map { CoverageSegment(count: $0.count, grade: $0.grade) })
                        .padding(.top, Space.m)
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(tiers.filter { $0.count > 0 }, id: \.grade) { tier in
                            let style = EvidenceTierStyle.forGrade(tier.grade)
                            HStack(spacing: 7) {
                                EvidencePip(shape: style.pip, tint: style.tint)
                                Text(style.label).font(Type.caption).foregroundStyle(Theme.ink)
                                Text("\(tier.count)").font(Type.dataSmall).foregroundStyle(Theme.muted)
                            }
                        }
                    }
                    .padding(.top, Space.m)
                    if tiers.first(where: { $0.grade == "externally_verified" })?.count == 0 {
                        Text("No independent verifier evidence on this receipt")
                            .font(Type.caption).foregroundStyle(Theme.muted)
                            .padding(.top, Space.s)
                    }
                } else {
                    // No verifiable steps: a named state, never a fabricated 0/0.
                    Text("Not gradeable").font(Face.sansFont(16, .semibold)).foregroundStyle(Theme.muted)
                    Text("No verifiable steps recorded for this task.")
                        .font(Type.caption).foregroundStyle(Theme.muted)
                        .padding(.top, 4)
                }

                if let ledger = evidence.ledger {
                    Text(ledger).font(Type.caption).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, Space.s)
                }
                Text("Counts show how many checkable steps carry a passing check, and how independent that check is — counts, not a probability of correctness.")
                    .font(Type.dataSmall).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, Space.s)
            }
        }
    }
}

// MARK: - Checks

/// Every check the store holds for this receipt, with its result mark and
/// source. A pass wears green only when a machine observed it (hook/CI);
/// an agent-reported pass stays ink — the source chip says why.
struct RecordChecksCard: View {
    let evidence: ReceiptEvidenceDim
    let taskId: String

    var body: some View {
        Card(padding: Space.xl) {
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: 8) {
                    Text("Checks").font(Type.titleCard).foregroundStyle(Theme.ink)
                    if let total = evidence.checksTotal, total > 0 {
                        Text("\(evidence.checksPassed ?? 0)/\(total) passed")
                            .font(Type.dataSmall).foregroundStyle(Theme.muted)
                    }
                    Spacer()
                }
                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.top, Space.m)
                let checks = evidence.checks ?? []
                if checks.isEmpty {
                    Text("No checks recorded").font(Type.body).foregroundStyle(Theme.muted)
                        .padding(.top, Space.m)
                    Text("Machine checks land here when a hook or CI reports one.")
                        .font(Type.caption).foregroundStyle(Theme.muted)
                        .padding(.top, 2)
                } else {
                    ForEach(Array(checks.enumerated()), id: \.offset) { index, check in
                        if index > 0 {
                            Rectangle().fill(Theme.hairline).frame(height: 1)
                        }
                        RecordCheckRow(check: check, taskId: taskId)
                    }
                }
            }
        }
    }
}

/// One check: the scannable one-liner, now expandable in place for the detail
/// behind it (summary, files, timestamp, superseded state, artifact refs — and
/// an honest note that command text is never captured). Snapshot mode keeps
/// the plain one-liner: the offscreen renderer can't drive the toggle.
private struct RecordCheckRow: View {
    let check: ReceiptCheck
    let taskId: String
    @State private var expanded = false

    private var mark: (symbol: String, tint: Color) {
        let machineObserved = ["hook", "ci"].contains(check.source ?? "")
        switch check.result {
        case "passed": return ("checkmark", machineObserved ? Theme.green : Theme.ink)
        case "failed", "error": return ("xmark", Theme.coral)
        case "skipped": return ("chevron.right.2", Theme.amber)
        default: return ("circle.fill", Theme.muted)
        }
    }

    private var headerLine: some View {
        HStack(alignment: .firstTextBaseline, spacing: Space.s) {
            if !SnapshotMode.enabled {
                Image(systemName: expanded ? "chevron.down" : "chevron.right")
                    .font(.system(size: 8, weight: .semibold))
                    .foregroundStyle(Theme.muted)
                    .frame(width: 10)
                    .accessibilityHidden(true)
            }
            Image(systemName: mark.symbol)
                .font(.system(size: 10, weight: .bold))
                .foregroundStyle(mark.tint)
                .frame(width: 14)
                .accessibilityHidden(true)  // the row label names the result
            Text(check.name ?? check.kind ?? "check")
                .font(Type.body).foregroundStyle(Theme.ink)
                .lineLimit(1).truncationMode(.middle)
            if check.superseded == true {
                Chip(text: "superseded", tint: Theme.muted)
                    .help("A later run of the same-scope check passed; kept in history.")
            }
            if let scope = check.scope {
                Text(scope).font(Type.dataSmall).foregroundStyle(Theme.muted)
            }
            Spacer(minLength: Space.s)
            if let exit = check.exitCode {
                Text("exit \(exit)").font(Type.dataSmall).foregroundStyle(Theme.muted)
            }
            if let source = check.source {
                ProvenanceChip(text: source)
            }
        }
        .padding(.vertical, 10)
        .contentShape(Rectangle())
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if SnapshotMode.enabled {
                headerLine
            } else {
                Button {
                    withAnimation(Motion.contentUpdate) { expanded.toggle() }
                } label: {
                    headerLine
                }
                .buttonStyle(.plain)
                .accessibilityLabel(
                    "\(check.name ?? check.kind ?? "check"), \(check.result ?? "unknown")"
                    + ", \(expanded ? "expanded" : "collapsed")"
                )
                .accessibilityIdentifier("receipt.check.\(check.name ?? "check")")
            }
            if expanded {
                expandedBody
                    .padding(.leading, 10 + Space.s + 14 + Space.s)
                    .padding(.bottom, 10)
            }
        }
    }

    @ViewBuilder
    private var expandedBody: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let summary = check.summary {
                // verbatim: agent/hook-authored text, never markdown.
                Text(verbatim: summary)
                    .font(Type.caption).foregroundStyle(Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            if let at = check.at, let ago = agoText(at) {
                Text("recorded \(ago)").font(Type.dataSmall).foregroundStyle(Theme.muted)
            }
            if let files = check.files, !files.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(Array(files.enumerated()), id: \.offset) { _, path in
                        Text(verbatim: path)
                            .font(Type.dataSmall).foregroundStyle(Theme.muted)
                            .lineLimit(1).truncationMode(.middle)
                    }
                }
                .textSelection(.enabled)
            }
            if check.commandRedacted == true {
                // Absence is a named state: a command ran, and agentacct
                // deliberately never records command text for checks.
                Text("command text not captured for checks — agentacct records the check's name, result, and files, not the command itself")
                    .font(Type.dataSmall).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let artifact = check.artifactUrl ?? check.artifactRef {
                Text(verbatim: "artifact: \(artifact)")
                    .font(Type.dataSmall).foregroundStyle(Theme.muted)
                    .lineLimit(1).truncationMode(.middle)
                    .textSelection(.enabled)
            }
            if check.summary == nil, check.at == nil, (check.files ?? []).isEmpty,
               check.commandRedacted != true, check.artifactUrl == nil, check.artifactRef == nil {
                Text("no further detail recorded for this check")
                    .font(Type.dataSmall).foregroundStyle(Theme.muted)
            }
            // A surfaced failing check carries its human attention handle:
            // review/resolve it here instead of leaving the red forever.
            if let finding = check.finding {
                if let state = finding.state, state != "open" {
                    Text(verbatim: "marked \(state) by you"
                         + (finding.note.map { " — \($0)" } ?? ""))
                        .font(Type.dataSmall).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !SnapshotMode.enabled, let digest = finding.targetDigest {
                    DispositionControls(
                        kind: "finding",
                        state: finding.state ?? "open",
                        revision: finding.revision ?? 0,
                        taskId: taskId,
                        targetDigest: digest
                    )
                }
            }
        }
    }
}

// MARK: - Gaps

struct RecordGapsCard: View {
    let gaps: ReceiptGapsDim

    var body: some View {
        let items = gaps.items ?? []
        if !items.isEmpty {
            Card(padding: Space.xl) {
                VStack(alignment: .leading, spacing: 0) {
                    Text("Gaps (\(items.count)) — what could not be proven")
                        .font(Type.titleCard).foregroundStyle(Theme.ink)
                    Rectangle().fill(Theme.hairline).frame(height: 1).padding(.vertical, Space.m)
                    VStack(alignment: .leading, spacing: Space.s) {
                        ForEach(items) { item in
                            HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                                Text(item.dimension).font(Type.captionSemibold).foregroundStyle(Theme.muted)
                                    .frame(width: 70, alignment: .leading)
                                Text(item.reason).font(Type.caption).foregroundStyle(Theme.muted)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                }
            }
        }
    }
}

// MARK: - Evidence sources

/// The receipt's evidence sources: which source kinds are present on this
/// record, each with the daemon's legend sentence. When no independent
/// verifier (CI) evidence exists, that is stated as a fact — never a meter.
struct RecordSourcesCard: View {
    let provenance: ReceiptProvenanceDim

    private var presentSources: [String] {
        if let present = provenance.sourcesPresent, !present.isEmpty { return present }
        return (provenance.legend ?? [:]).keys.sorted()
    }

    var body: some View {
        let legend = provenance.legend ?? [:]
        if !presentSources.isEmpty {
            Card(padding: Space.xl) {
                VStack(alignment: .leading, spacing: 0) {
                    Text("Evidence sources").font(Type.titleCard).foregroundStyle(Theme.ink)
                    Rectangle().fill(Theme.hairline).frame(height: 1).padding(.top, Space.m)
                    ForEach(Array(presentSources.enumerated()), id: \.element) { index, source in
                        if index > 0 {
                            Rectangle().fill(Theme.hairline).frame(height: 1)
                        }
                        HStack(alignment: .top, spacing: Space.m) {
                            Text(source).font(Type.rowLabel).foregroundStyle(Theme.ink)
                                .frame(width: 96, alignment: .leading)
                            Text(legend[source] ?? "recorded on this receipt")
                                .font(Type.caption).foregroundStyle(Theme.muted)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .padding(.vertical, Space.m)
                    }
                    if !presentSources.contains("ci") {
                        Rectangle().fill(Theme.hairline).frame(height: 1)
                        VStack(alignment: .leading, spacing: 4) {
                            Text("No CI evidence on this receipt")
                                .font(Type.rowLabel).foregroundStyle(Theme.muted)
                            Text("Independent evidence upgrades self-checked claims to verified.")
                                .font(Type.caption).foregroundStyle(Theme.muted)
                        }
                        .padding(.top, Space.m)
                    }
                }
            }
        }
    }
}
