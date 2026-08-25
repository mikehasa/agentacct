import SwiftUI

// The Receipts pane: Task list (left) -> one Task's Work Receipt (right).
//
// A Receipt answers the 8 questions for one converged Task and keeps the two
// honesty axes visibly SEPARATE: decision status (what a human/agent SAYS) and
// evidence strength (how well that is PROVEN). Both colors and both labels are
// distinct so an agent's "done" never reads as machine verification. Honesty
// rides the payload — nothing here re-derives an axis or invents a number.

/// Decision-status color (what is CLAIMED). Deliberately a different function
/// from ``evidenceStrengthTint`` so the two axes can never share a color.
func receiptDecisionTint(_ key: String?) -> Color {
    switch key {
    case "verified": return Theme.green
    case "finding", "failed", "blocked": return Theme.red
    case "reported", "mostly_done", "in_progress": return Theme.orange
    case "handed_off", "resolved", "finding_superseded": return Theme.blue
    default: return Theme.textMuted
    }
}

/// Evidence-coverage color, keyed on the strongest tier present (most
/// independent is warmest). A failing check is never positive proof — it lands
/// on the decision axis as a finding, not here.
func receiptEvidenceTint(_ key: String?) -> Color {
    switch key {
    case "externally_verified": return Theme.green
    case "independently_checked": return Theme.cyan
    case "self_checked": return Theme.blue
    case "unchecked": return Theme.orange
    default: return Theme.textMuted  // undefined / not gradeable
    }
}

/// Per-step evidence-grade color (adds ``claimed`` / ``none`` to the tier ramp).
func stepGradeTint(_ grade: String?) -> Color {
    switch grade {
    case "externally_verified": return Theme.green
    case "independently_checked": return Theme.cyan
    case "self_checked": return Theme.blue
    case "claimed": return Theme.orange
    default: return Theme.textMuted  // none
    }
}

/// Short chip label for a per-step grade.
func stepGradeLabel(_ grade: String?) -> String {
    switch grade {
    case "externally_verified": return "external"
    case "independently_checked": return "independent"
    case "self_checked": return "self-checked"
    case "claimed": return "unchecked"
    case "none": return "none"
    default: return grade ?? "none"
    }
}

/// Provenance-source chip color: the strongest source is warmest.
func receiptSourceTint(_ source: String) -> Color {
    switch source {
    case "ci": return Theme.green
    case "hook": return Theme.blue
    case "mcp": return Theme.accent
    case "git": return Theme.orange
    case "human": return Theme.orange
    case "client_log": return Theme.textMuted
    default: return Theme.textFaint
    }
}

// Reusable Task-row and Receipt cards for the Work surface (WorkPane.swift).
// The former standalone Receipts pane was folded into WorkPane.

struct ReceiptRow: View {
    let task: ReceiptSummary
    let selected: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(task.title ?? task.taskId)
                .font(.callout).fontWeight(.medium)
                .foregroundStyle(Theme.text)
                .lineLimit(2)
            HStack(spacing: 6) {
                AxisChip(text: task.decisionStatus.key, tint: receiptDecisionTint(task.decisionStatus.key))
                AxisChip(
                    text: task.evidenceStrength.compactHeadline,
                    tint: receiptEvidenceTint(task.evidenceStrength.key)
                )
                // Parallel deliberate-stop marker, shown only when it adds info the
                // decision word does not already state. Purple unifies with the
                // step-level handoff chip (Theme.statusColor("handed_off")).
                if task.handedOff == true && task.decisionStatus.key != "handed_off" {
                    AxisChip(text: "↗ handed off", tint: Theme.purple)
                }
                Spacer()
                Text(task.cost.text).font(.caption).foregroundStyle(Theme.textMuted)
            }
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            selected ? AnyShapeStyle(Theme.cardAlt) : AnyShapeStyle(.clear),
            in: RoundedRectangle(cornerRadius: 7, style: .continuous)
        )
        // The whole card is the tap target, not just the text — an unselected
        // row's background is `.clear`, which SwiftUI does not hit-test, so
        // without this the blank areas of the card swallow the tap (SessionRow
        // already does this).
        .contentShape(Rectangle())
    }
}

struct AxisChip: View {
    static let backgroundOpacity = 0.08

    let text: String
    let tint: Color

    var body: some View {
        Text(text)
            .font(.caption2).fontWeight(.semibold)
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(tint.opacity(Self.backgroundOpacity), in: Capsule())
            .foregroundStyle(tint)
    }
}

/// The Receipt's cards (axes, dimensions, gaps, provenance) as a plain stack —
/// no ScrollBox of its own, so WorkPane can scroll it together with the session
/// drill-down that follows it.
struct ReceiptCards: View {
    let receipt: Receipt

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            header
            axesCard
            dimensionsCard
            gapsCard
            provenanceCard
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(receipt.title ?? "Task").font(.title3).fontWeight(.semibold)
                .foregroundStyle(Theme.text)
            Text(receipt.taskId).font(.caption).foregroundStyle(Theme.textFaint)
        }
    }

    private var axesCard: some View {
        Card {
            VStack(alignment: .leading, spacing: 10) {
                axisRow(
                    label: "Decision status",
                    key: receipt.axes.decisionStatus.key,
                    tint: receiptDecisionTint(receipt.axes.decisionStatus.key),
                    detail: assertedText(receipt.axes.decisionStatus.assertedBy),
                    note: receipt.axes.decisionStatus.statement
                )
                // Lifecycle marker, shown BESIDE the decision word (not instead of
                // it) when it adds information the decision word does not state.
                if let handoff = receipt.axes.handoff, handoff.handedOff == true,
                   receipt.axes.decisionStatus.key != "handed_off" {
                    axisRow(
                        label: "Lifecycle",
                        key: "↗ handed off",
                        tint: Theme.purple,
                        detail: assertedText(handoff.assertedBy),
                        note: handoff.statement
                    )
                }
                evidenceCoverageRow(receipt.axes.evidenceStrength)
                if let orthogonality = receipt.axes.orthogonalityNote {
                    Text(orthogonality).font(.caption).foregroundStyle(Theme.textFaint)
                }
            }
        }
    }

    private func axisRow(label: String, key: String, tint: Color, detail: String?, note: String?) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 8) {
                Text(label).font(.caption).foregroundStyle(Theme.textMuted).frame(width: 130, alignment: .leading)
                Text(key.uppercased()).font(.callout).fontWeight(.bold).foregroundStyle(tint)
                if let detail { Text(detail).font(.caption).foregroundStyle(Theme.textMuted) }
            }
            if let note {
                Text(note).font(.caption).foregroundStyle(Theme.textFaint)
                    .padding(.leading, 138)
            }
        }
    }

    /// Evidence coverage: the ratio headline (NOT uppercased — it is counts) with
    /// the honest ledger and the one-line definition beneath it.
    private func evidenceCoverageRow(_ evidence: ReceiptEvidence) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text("Evidence coverage").font(.caption).foregroundStyle(Theme.textMuted)
                    .frame(width: 130, alignment: .leading)
                Text(evidence.headline).font(.callout).fontWeight(.bold)
                    .foregroundStyle(receiptEvidenceTint(evidence.key))
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let ledger = evidence.ledger {
                Text(ledger).font(.caption).foregroundStyle(Theme.textMuted).padding(.leading, 138)
            }
            if let definition = evidence.definition {
                Text(definition).font(.caption).foregroundStyle(Theme.textFaint).padding(.leading, 138)
            }
        }
    }

    private var dimensionsCard: some View {
        Card {
            VStack(alignment: .leading, spacing: 8) {
                dimensionRow("Task", taskSummary, receipt.dimensions.task.provenance)
                dimensionRow("Actors", actorsSummary, receipt.dimensions.actors.provenance)
                actionsRow
                dimensionRow("Cost", costSummary, receipt.dimensions.cost.provenance)
                dimensionRow("Evidence", evidenceSummary, receipt.dimensions.evidence.provenance)
                dimensionRow("Outcome", outcomeSummary, receipt.dimensions.outcome.provenance)
            }
        }
    }

    private func dimensionRow(_ name: String, _ summary: String, _ provenance: [String]?) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Text(name).font(.caption).fontWeight(.semibold).foregroundStyle(Theme.textMuted)
                .frame(width: 74, alignment: .leading)
            Text(summary).font(.callout).foregroundStyle(Theme.text)
                .frame(maxWidth: .infinity, alignment: .leading)
            HStack(spacing: 4) {
                ForEach(provenance ?? [], id: \.self) { source in
                    Text(source).font(.caption2)
                        .padding(.horizontal, 5).padding(.vertical, 1)
                        .background(receiptSourceTint(source).opacity(0.16), in: Capsule())
                        .foregroundStyle(receiptSourceTint(source))
                }
            }
        }
    }

    // Actions gets its own row so the touched artifact PATHS render beneath the
    // category summary. The paths were always captured; only the count was shown.
    private var actionsRow: some View {
        VStack(alignment: .leading, spacing: 2) {
            dimensionRow("Actions", actionsSummary, receipt.dimensions.actions.provenance)
            let preview = actionsTouchedPreview
            if !preview.shown.isEmpty {
                VStack(alignment: .leading, spacing: 1) {
                    ForEach(preview.shown, id: \.self) { path in
                        Text(path).font(.caption).foregroundStyle(Theme.textFaint)
                            .lineLimit(1).truncationMode(.middle)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    if preview.elided > 0 {
                        Text("… +\(preview.elided) more").font(.caption).foregroundStyle(Theme.textFaint)
                    }
                }
                .padding(.leading, 82)  // align under the summary column (74 label + 8 spacing)
            }
            let commands = actionsCommandsPreview
            if !commands.shown.isEmpty {
                VStack(alignment: .leading, spacing: 1) {
                    ForEach(commands.shown, id: \.self) { cmd in
                        // verbatim: a command is untrusted text — never interpret it as
                        // markdown (Text's default LocalizedStringKey init would).
                        Text(verbatim: "$ \(cmd)").font(.caption).foregroundStyle(Theme.textFaint)
                            .lineLimit(1).truncationMode(.middle)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    if commands.elided > 0 {
                        Text("… +\(commands.elided) more").font(.caption).foregroundStyle(Theme.textFaint)
                    }
                }
                .padding(.leading, 82)  // align the commands under the summary column
            }
        }
    }

    // The daemon computes the preview slice + overflow (the single source of truth
    // for the cap); the app renders those directly so it can never drift from the
    // CLI/TUI. Fallback (older payload without the fields): show the full list.
    private var actionsTouchedPreview: (shown: [String], elided: Int) {
        let dim = receipt.dimensions.actions
        if let preview = dim.touchedFilesPreview {
            return (preview, dim.touchedFilesElided ?? 0)
        }
        return (dim.touchedFiles ?? [], 0)
    }

    // Same daemon-capped preview + disclosed overflow for the commands an execute tool
    // ran (fallback to the full list for an older payload without the preview fields).
    private var actionsCommandsPreview: (shown: [String], elided: Int) {
        let dim = receipt.dimensions.actions
        if let preview = dim.commandsPreview {
            return (preview, dim.commandsElided ?? 0)
        }
        return (dim.commands ?? [], 0)
    }

    @ViewBuilder
    private var gapsCard: some View {
        let items = receipt.dimensions.gaps.items ?? []
        if !items.isEmpty {
            Card {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Gaps (\(items.count)) — what could not be proven")
                        .font(.callout).fontWeight(.semibold).foregroundStyle(Theme.text)
                    ForEach(items) { item in
                        HStack(alignment: .top, spacing: 6) {
                            Text(item.dimension).font(.caption2).foregroundStyle(Theme.textFaint)
                                .frame(width: 70, alignment: .leading)
                            Text(item.reason).font(.caption).foregroundStyle(Theme.textMuted)
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var provenanceCard: some View {
        let legend = receipt.dimensions.provenance.legend ?? [:]
        if !legend.isEmpty {
            Card {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Provenance").font(.callout).fontWeight(.semibold).foregroundStyle(Theme.text)
                    ForEach(legend.keys.sorted(), id: \.self) { source in
                        HStack(alignment: .top, spacing: 6) {
                            Text(source).font(.caption2).fontWeight(.semibold)
                                .foregroundStyle(receiptSourceTint(source))
                                .frame(width: 78, alignment: .leading)
                            Text(legend[source] ?? "").font(.caption).foregroundStyle(Theme.textMuted)
                        }
                    }
                }
            }
        }
    }

    // MARK: - dimension summaries

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
        return parts.isEmpty ? "—" : parts.joined(separator: " · ")
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
        // The specific tools/connectors the agent used (daemon-ranked), with the
        // disclosed overflow — surfaced beneath the coarse category counts.
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
        guard let cost = dim.estimatedCostUsd else { return "—" }
        let basis = dim.costBasis ?? "unknown basis"
        return String(format: "$%.2f · %@%@", cost, basis, (dim.costComplete ?? true) ? "" : " (partial)")
    }

    private var evidenceSummary: String {
        let dim = receipt.dimensions.evidence
        return "\(dim.checksTotal ?? 0) checks · \(dim.checksPassed ?? 0) passed · \(dim.checksFailed ?? 0) failed"
    }

    private var outcomeSummary: String {
        let dim = receipt.dimensions.outcome
        return "\(dim.decisionStatus ?? "—") · asserted by \(dim.assertedBy ?? "none")"
    }

    private func assertedText(_ assertedBy: String?) -> String? {
        guard let assertedBy else { return nil }
        return "asserted by \(assertedBy)"
    }
}
