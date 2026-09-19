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

// Cost grammar, basis words and asserted-by words are reducer-owned
// (`cost.display_text`, `cost.basis_label`, `asserted_by_label`); this file
// renders those strings and keeps no label map of its own (C17/C20/C38).

/// Builds the verbatim Outcome-row text for the Receipt DETAIL: the daemon's
/// decision word + who asserted it, the honest statement quoted beneath, and —
/// only when the daemon attached them (the `inactive`/`mostly_done` keys) — one
/// factual sub-line naming when the Task went quiet and when a newer session
/// started. The app is a pure renderer: it never derives the honesty logic, it
/// only shows the timestamps the daemon already decided to attach. `quietSince`
/// absent means "no quiet fact", never a completion signal.
func receiptOutcomeSummary(_ dim: ReceiptOutcomeDim) -> String {
    // The reducer's asserted-by label; a key it did not label is shown
    // de-snaked (never renamed), and a missing assertion is named.
    let assertedBy = PayloadAbsence.text(dim.assertedByLabel)
        ?? PayloadAbsence.text(dim.assertedBy)?.replacingOccurrences(of: "_", with: " ")
        ?? PayloadAbsence.source
    var line = "\(dim.decisionStatus ?? "unknown") · \(assertedBy)"
    if let statement = dim.statement, !statement.isEmpty {
        line += "\n“\(statement)”"
    }
    if let quietSince = dim.quietSince, let quietAgo = agoText(quietSince) {
        var sub = "Quiet since \(quietAgo)"
        if let newer = dim.newerSessionStartedAt, let newerAgo = agoText(newer) {
            sub += " · a newer session started \(newerAgo)"
        }
        line += "\n\(sub)"
    }
    return line
}

/// Renders only check tallies that the receipt actually carries. A missing
/// total is named explicitly so an older or partial payload never reads as a
/// measured zero.
func receiptCheckSummary(total: Int?, passed: Int?, failed: Int?) -> String {
    var parts = [total.map { Fmt.count($0, "check") } ?? "check total not reported"]
    if let passed { parts.append("\(passed) passed") }
    if let failed { parts.append("\(failed) failed") }
    return parts.joined(separator: " · ")
}

/// Absence of the external tier says nothing about independently checked or
/// self-checked evidence. Legacy receipts can omit zero-valued tier keys, so
/// an absent external key carries the same presentation as an explicit zero.
func receiptExternalEvidenceNotice(byTier: ReceiptByTier?) -> String? {
    guard (byTier?.externallyVerified ?? 0) == 0 else { return nil }
    return "No externally verified evidence on this receipt"
}

struct ReceiptCIEvidenceNotice: Equatable {
    let headline: String
    let detail: String
}

/// CI provenance can strengthen supporting evidence, but is not itself a
/// decision transition. Keep the provenance and decision axes orthogonal.
func receiptCIEvidenceNotice(sources: [String]) -> ReceiptCIEvidenceNotice? {
    guard !sources.contains("ci") else { return nil }
    return ReceiptCIEvidenceNotice(
        headline: "No CI evidence on this receipt",
        detail: "CI can strengthen supporting evidence, but it does not change the separately recorded decision status."
    )
}

struct ReceiptEmptyCheckDetailsCopy: Equatable {
    let title: String
    let detail: String
}

func receiptEmptyCheckDetailsCopy(
    total: Int?,
    passed: Int?,
    failed: Int?
) -> ReceiptEmptyCheckDetailsCopy {
    let hasSummaryTallies = total != nil || passed != nil || failed != nil
    return ReceiptEmptyCheckDetailsCopy(
        title: hasSummaryTallies ? "No itemized check details recorded" : "No check runs recorded",
        detail: hasSummaryTallies
            ? "Summary counts are available above; this payload did not include per-run details."
            : "Machine checks land here when a hook or CI reports one."
    )
}

/// The reducer's tool-call integrity state (`actions_synopsis.state`). The app
/// maps the key to a tone only; every word it shows rides the payload.
enum ReceiptActionIntegrity: String, Equatable {
    case notInstrumented = "not_instrumented"
    case noToolCalls = "no_tool_calls"
    case captureUnknown = "capture_unknown"
    case totalOnly = "total_only"
    case exact
    /// The capture ran, but the reducer proved it did not cover the whole
    /// task (it holds records the capture never saw). Counted evidence, not a
    /// named absence — without this arm the state fell through to
    /// `captureUnknown` and a real count was drawn as if nothing was captured.
    case partial
    case totalUnavailable = "total_unavailable"
    case unrecognizedCategories = "unrecognized_categories"
    case mismatch
    case invalid

    init(payload: String?) {
        self = payload.flatMap(ReceiptActionIntegrity.init(rawValue:)) ?? .captureUnknown
    }

    /// A named absence: nothing counted, so the headline reads muted.
    var isAbsence: Bool { [.notInstrumented, .noToolCalls, .captureUnknown].contains(self) }
}

/// One same-unit tool-call category in the Actions dimension, labelled by the
/// reducer (`TOOL_CATEGORY_LABELS`). Labels describe only the observed
/// category; they never imply success, effect, importance, or risk.
struct ReceiptActionMetric: Decodable, Equatable, Identifiable {
    let key: String
    let label: String
    let detail: String
    let count: Int

    var id: String { key }
}

/// The reducer's tool-call synopsis (`dimensions.actions.actions_synopsis`):
/// the headline, integrity detail, labelled metrics, whether a proportional
/// distribution is honest, the capture boundary and the tile. The app renders
/// these strings; it never recounts, reconciles or words a state itself.
struct ReceiptActionSynopsis: Decodable, Equatable {
    let state: String?
    let headline: String?
    let integrityDetail: String?
    let metrics: [ReceiptActionMetric]
    let canShowDistribution: Bool
    let captureBoundary: String?
    let storedTotal: Int?
    let categorizedTotal: Int?
    let tile: ReceiptTileText?

    enum CodingKeys: String, CodingKey {
        case state, headline, metrics, tile
        case integrityDetail = "integrity_detail"
        case canShowDistribution = "can_show_distribution"
        case captureBoundary = "capture_boundary"
        case storedTotal = "stored_total"
        case categorizedTotal = "categorized_total"
    }

    init(state: String?, headline: String?, integrityDetail: String? = nil,
         metrics: [ReceiptActionMetric] = [], canShowDistribution: Bool = false,
         captureBoundary: String? = nil, storedTotal: Int? = nil, categorizedTotal: Int? = nil,
         tile: ReceiptTileText? = nil) {
        self.state = state
        self.headline = headline
        self.integrityDetail = integrityDetail
        self.metrics = metrics
        self.canShowDistribution = canShowDistribution
        self.captureBoundary = captureBoundary
        self.storedTotal = storedTotal
        self.categorizedTotal = categorizedTotal
        self.tile = tile
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        state = try container.decodeIfPresent(String.self, forKey: .state)
        headline = try container.decodeIfPresent(String.self, forKey: .headline)
        integrityDetail = try container.decodeIfPresent(String.self, forKey: .integrityDetail)
        metrics = try container.decodeIfPresent([ReceiptActionMetric].self, forKey: .metrics) ?? []
        canShowDistribution = try container.decodeIfPresent(Bool.self, forKey: .canShowDistribution) ?? false
        captureBoundary = try container.decodeIfPresent(String.self, forKey: .captureBoundary)
        storedTotal = try container.decodeIfPresent(Int.self, forKey: .storedTotal)
        categorizedTotal = try container.decodeIfPresent(Int.self, forKey: .categorizedTotal)
        tile = try container.decodeIfPresent(ReceiptTileText.self, forKey: .tile)
    }

    var integrity: ReceiptActionIntegrity { ReceiptActionIntegrity(payload: state) }

    /// The headline, or the tile's named absence, or the generic absence.
    var headlineText: String {
        PayloadAbsence.text(headline) ?? PayloadAbsence.text(tile?.absent) ?? PayloadAbsence.toolCalls
    }

    /// The share denominator exists only when the reducer says the displayed
    /// types reconcile to a positive stored total.
    var shareDenominator: Int? {
        guard canShowDistribution, let storedTotal, storedTotal > 0, !metrics.isEmpty else { return nil }
        return storedTotal
    }
}

// MARK: - Record summary strip

/// The record page's tile strip is DELETED, and with it `RecordSummaryStrip`.
///
/// Five tiles, 198 px on every record, and each one answered a question that
/// already had a better home: COVERAGE and CHECKS are the Evidence section's
/// heading line, TOOL CALLS and COST are two nouns in its one absence line, and
/// SESSIONS read `1` on four of the five records measured — a figure set in the
/// metric face for a number that never varied.
///
/// `RecordSummaryPresentation` (V1Model.swift) is NOT deleted: it is the model
/// the task list still reads, and its tests still pin it.


/// Equal-width tile columns. The column count is the largest that gives every
/// tile at least its minimum width (its unbreakable value and longest
/// qualifier word); tiles then share one height. Hairline dividers sit between
/// tiles of the same row and collapse at a row break.
struct RecordSummaryTileGrid: Layout {
    struct IsDivider: LayoutValueKey { static let defaultValue = false }

    var columnGap: CGFloat
    var rowGap: CGFloat

    struct Plan {
        var columns: Int
        var columnWidth: CGFloat
        var tileHeight: CGFloat
        var rows: Int
    }

    private func tiles(_ subviews: Subviews) -> [LayoutSubview] {
        subviews.filter { !$0[IsDivider.self] }
    }

    private func plan(width proposed: CGFloat?, subviews: Subviews) -> Plan {
        let tiles = tiles(subviews)
        guard !tiles.isEmpty else { return Plan(columns: 1, columnWidth: 0, tileHeight: 0, rows: 0) }
        let minimum = tiles.map { $0.sizeThatFits(ProposedViewSize(width: 0, height: nil)).width }.max() ?? 0
        let ideal = tiles.map { $0.sizeThatFits(.unspecified).width }.max() ?? 0
        let count = tiles.count
        let width = proposed ?? (CGFloat(count) * ideal + CGFloat(count - 1) * columnGap)
        var columns = count
        while columns > 1, CGFloat(columns) * minimum + CGFloat(columns - 1) * columnGap > width {
            columns -= 1
        }
        let columnWidth = max(minimum, (width - CGFloat(columns - 1) * columnGap) / CGFloat(columns))
        let tileHeight = tiles.map {
            $0.sizeThatFits(ProposedViewSize(width: columnWidth, height: nil)).height
        }.max() ?? 0
        let rows = (count + columns - 1) / columns
        return Plan(columns: columns, columnWidth: columnWidth, tileHeight: tileHeight, rows: rows)
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let plan = plan(width: proposal.width, subviews: subviews)
        let width = CGFloat(plan.columns) * plan.columnWidth + CGFloat(plan.columns - 1) * columnGap
        let height = CGFloat(plan.rows) * plan.tileHeight + CGFloat(max(0, plan.rows - 1)) * rowGap
        return CGSize(width: width, height: height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let plan = plan(width: bounds.width, subviews: subviews)
        var tileIndex = 0
        for subview in subviews {
            if subview[IsDivider.self] {
                // The divider precedes tile `tileIndex`; it shows only when
                // that tile continues the same row.
                guard tileIndex % plan.columns != 0 else {
                    subview.place(at: bounds.origin, proposal: .zero)
                    continue
                }
                let row = tileIndex / plan.columns
                let column = tileIndex % plan.columns
                let x = bounds.minX + CGFloat(column) * (plan.columnWidth + columnGap) - (columnGap + 1) / 2
                let y = bounds.minY + CGFloat(row) * (plan.tileHeight + rowGap)
                subview.place(
                    at: CGPoint(x: x, y: y),
                    proposal: ProposedViewSize(width: 1, height: plan.tileHeight)
                )
            } else {
                let row = tileIndex / plan.columns
                let column = tileIndex % plan.columns
                subview.place(
                    at: CGPoint(
                        x: bounds.minX + CGFloat(column) * (plan.columnWidth + columnGap),
                        y: bounds.minY + CGFloat(row) * (plan.tileHeight + rowGap)
                    ),
                    proposal: ProposedViewSize(width: plan.columnWidth, height: plan.tileHeight)
                )
                tileIndex += 1
            }
        }
    }
}

// MARK: - Receipt dimensions

/// The aggregate-only Actions view. It is intentionally static: current
/// receipts do not contain canonical per-action rows, so no metric, source, or
/// disclosure may look clickable. Exact text remains primary and the layout has
/// one deterministic two-column-to-one-column transition.
struct ReceiptActionsDigest: View {
    let synopsis: ReceiptActionSynopsis
    /// The reducer's related-path scope (`related_paths_text`): a count, or
    /// its named absence.
    let relatedPathsText: String?
    /// The reducer's capture-source words for the counts (`action_sources_text`).
    let sourceText: String?
    let gaps: [String]?
    /// Topic use: keep the facts, drop the explanatory copy — the definitions
    /// live in the topic's help instead of under every bar.
    var compact = false
    /// The dimension's label (`field_labels.actions`).
    var label: String = "Tool calls"
    /// The captured ledger itself — tool names, command text and associated
    /// paths. The CLI has always printed these and the app could not see them
    /// (they were absent from `ReceiptActionsDim`'s coding keys entirely).
    var ledger: ReceiptActionsDim? = nil

    // The app's fixed type ramp keeps dense dashboard geometry stable. This
    // focused digest still has to honor accessibility text sizes, so its four
    // existing roles scale relative to their semantic text styles without
    // changing the surrounding receipt ledger.
    @ScaledMetric(relativeTo: .body) private var bodyTypeSize: CGFloat = 14
    @ScaledMetric(relativeTo: .caption) private var captionTypeSize: CGFloat = 12

    private var scope: String { PayloadAbsence.text(relatedPathsText) ?? "" }
    private var bodyFont: Font { Face.sansFont(bodyTypeSize, .regular) }
    private var rowLabelFont: Font { Face.sansFont(bodyTypeSize, .semibold) }
    private var captionFont: Font { Face.sansFont(captionTypeSize, .regular) }
    private var captionSemiboldFont: Font { Face.sansFont(captionTypeSize, .semibold) }
    private var dataSmallFont: Font { Face.monoFont(captionTypeSize, .regular) }
    private var dataSmallSemiboldFont: Font { Face.monoFont(captionTypeSize, .semibold) }

    private var integrityTone: Color {
        switch synopsis.integrity {
        case .invalid, .mismatch: return Theme.amber
        default: return Theme.muted
        }
    }

    var body: some View {
        Group {
            if compact {
                // Work page: a caps label matching the sibling rows, and a
                // full-width content column so the 100% bar spans its track.
                HStack(alignment: .top, spacing: Space.l) {
                    CapsLabel(text: label)
                        .frame(width: 104, alignment: .leading)
                        .padding(.top, 3)
                        .accessibilityHidden(true)
                    digestContent
                }
            } else {
                ViewThatFits(in: .horizontal) {
                    HStack(alignment: .top, spacing: Space.l) {
                        actionsLabel.frame(width: 128, alignment: .leading)
                        digestContent
                    }
                    .frame(width: 620, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    VStack(alignment: .leading, spacing: Space.m) {
                        actionsLabel
                        digestContent
                    }
                }
            }
        }
        .padding(.vertical, Space.m)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(label)
        .accessibilityIdentifier("receipt.actions.summary")
    }

    private var actionsLabel: some View {
        Text(label)
            .font(rowLabelFont)
            .foregroundStyle(Theme.ink)
            .accessibilityHidden(true)
    }

    private var digestContent: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            Text(synopsis.headlineText)
                .font(bodyFont)
                .foregroundStyle(synopsis.integrity.isAbsence ? Theme.muted : Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
            if let detail = synopsis.integrityDetail {
                Text(detail)
                    .font(captionFont)
                    .foregroundStyle(integrityTone)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !synopsis.metrics.isEmpty {
                if synopsis.shareDenominator != nil {
                    // Work page: one 100% stacked bar + a wrapping legend, with
                    // the full itemized list one click away. The multi-row
                    // shared-scale chart stays the full-receipt presentation.
                    if compact {
                        stackedBar
                    } else {
                        actionDistribution
                    }
                } else {
                    // Not a reconciled partition: keep exact counts, never draw
                    // a proportional bar against a missing/conflicting total.
                    Text("Captured tool-call types")
                        .font(captionSemiboldFont)
                        .foregroundStyle(Theme.ink)
                        .padding(.top, Space.xs)
                    ViewThatFits(in: .horizontal) {
                        metricGrid(columns: 2)
                            .frame(minWidth: 340)
                        metricGrid(columns: 1)
                    }
                }
            }
            capturedLedger
            if !scope.isEmpty {
                Text(scope)
                    .font(dataSmallFont)
                    .foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // On the Work page these facts already have a home elsewhere: the
            // source identities in the Recording section's Sources row (the
            // whole-task union), the capture boundary in this section's help,
            // and the actions dimension's gaps in the Recording Gaps row (the
            // daemon rolls every dimension's gaps into that list). Repeating
            // them would duplicate facts, so the compact digest omits them.
            if !compact, let sourceText = PayloadAbsence.text(sourceText) {
                metadataLine(label: "Action sources", value: sourceText)
            }
            if !compact, let boundary = synopsis.captureBoundary {
                metadataLine(label: "Detail", value: boundary)
            }
            ForEach(Array((compact ? [] : (gaps ?? [])).enumerated()), id: \.offset) { _, gap in
                // Caveat prose is muted (C24); amber stays rationed.
                noticeLine(prefix: "Evidence gap", text: gap, tone: Theme.muted)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// The captured ledger, in three parts: which TOOLS ran and how often,
    /// the command text that was kept, and the paths the capture associated
    /// with the task. Each part names what the reducer withheld rather than
    /// showing a shorter list as if it were the whole one.
    @ViewBuilder private var capturedLedger: some View {
        if let ledger {
            let toolRows = ledger.toolNameRows
            let commands = ledger.commandLines
            let files = ledger.touchedFileLines
            if !toolRows.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Tools").font(captionSemiboldFont).foregroundStyle(Theme.ink)
                    ForEach(toolRows, id: \.name) { tool in
                        HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                            Text(tool.name).font(dataSmallFont).foregroundStyle(Theme.ink)
                                .lineLimit(1).truncationMode(.middle)
                                .frame(maxWidth: .infinity, alignment: .leading)
                            Text("\(tool.count)").font(dataSmallSemiboldFont).foregroundStyle(Theme.ink)
                        }
                    }
                    if let elided = ledger.toolNamesElided, elided > 0 {
                        Text("\(elided) more tool \(elided == 1 ? "name" : "names") not shown")
                            .font(captionFont).foregroundStyle(Theme.muted)
                    }
                }
                .padding(.top, Space.xs)
                .accessibilityElement(children: .contain)
                .accessibilityIdentifier("receipt.actions.tools")
            }
            if !commands.isEmpty {
                OverflowDisclosure(label: "Commands · \(ledger.commandCount ?? commands.count)",
                                   identifier: "receipt.actions.commands") {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(Array(commands.enumerated()), id: \.offset) { _, command in
                            Text(command).font(dataSmallFont).foregroundStyle(Theme.ink)
                                .fixedSize(horizontal: false, vertical: true)
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        if let elided = ledger.commandsElided, elided > 0 {
                            Text("\(elided) more \(elided == 1 ? "command" : "commands") not shown")
                                .font(captionFont).foregroundStyle(Theme.muted)
                        }
                    }
                }
            }
            if !files.isEmpty {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(files, id: \.self) { file in
                        Text(file).font(dataSmallFont).foregroundStyle(Theme.muted)
                            .lineLimit(1).truncationMode(.middle)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    if let elided = ledger.touchedFilesElided, elided > 0 {
                        Text("\(elided) more \(elided == 1 ? "path" : "paths") not shown")
                            .font(captionFont).foregroundStyle(Theme.muted)
                    }
                }
                .padding(.top, Space.xs)
                .accessibilityElement(children: .contain)
                .accessibilityIdentifier("receipt.actions.touched-files")
            }
        }
    }

    // MARK: compact stacked bar (Work page)

    /// One displayed slice of the 100% bar and its legend entry.
    private struct BarSegment: Identifiable {
        let id: String
        let label: String
        let count: Int
        let fraction: Double
        let color: Color
        let tooltip: String
        let axValue: String
    }

    /// Identity hue per tool-call category — fixed by ENTITY, never by share
    /// rank, so a category keeps its color whether it is the largest slice or
    /// the smallest. Three validated hues (see `Theme.chartCat*`); every other
    /// named type folds into the neutral "Other" slice, with its exact count
    /// one click away in the full type list.
    private static let categoryTint: [String: Color] = [
        "read": Theme.chartCatRead,
        "execute": Theme.chartCatExecute,
        "edit": Theme.chartCatEdit,
    ]

    /// The displayed segments: one slice per category that owns a validated
    /// identity hue, plus a muted "Other" carrying the exact remainder. Callers
    /// guarantee a reconciled positive denominator (`canShowDistribution`).
    private var barSegments: [BarSegment] {
        guard let denom = synopsis.shareDenominator, denom > 0 else { return [] }
        func pct(_ count: Int) -> String {
            (Double(count) / Double(denom)).formatted(.percent.precision(.fractionLength(0...1)))
        }
        var otherCount = 0
        var segments: [BarSegment] = []
        for metric in synopsis.metrics {  // taxonomy order, matching the legend
            guard let tint = Self.categoryTint[metric.key] else {
                otherCount += metric.count
                continue
            }
            segments.append(BarSegment(
                id: metric.key,
                label: metric.label,
                count: metric.count,
                fraction: Double(metric.count) / Double(denom),
                color: tint,
                tooltip: "\(metric.label) · \(metric.count) call\(metric.count == 1 ? "" : "s") · \(pct(metric.count)) — \(metric.detail)",
                axValue: "\(metric.count) call\(metric.count == 1 ? "" : "s"), \(pct(metric.count)). \(metric.detail)"
            ))
        }
        if otherCount > 0 {
            segments.append(BarSegment(
                id: "__other__",
                label: "Other",
                count: otherCount,
                fraction: Double(otherCount) / Double(denom),
                // Neutral chart token for the "Other" slot — no alpha-derived
                // color, so it reads the same weight in both modes (C42).
                color: Theme.chartNeutral,
                tooltip: "Other · \(otherCount) call\(otherCount == 1 ? "" : "s") · \(pct(otherCount)) — remaining captured tool-call types",
                axValue: "\(otherCount) call\(otherCount == 1 ? "" : "s"), \(pct(otherCount)). Remaining captured tool-call types"
            ))
        }
        return segments
    }

    private var stackedBar: some View {
        let segments = barSegments
        return VStack(alignment: .leading, spacing: Space.s) {
            GeometryReader { proxy in
                // A 2 pt surface gap between segments so adjacent categories
                // are separated by geometry as well as hue (secondary encoding).
                HStack(spacing: 2) {
                    ForEach(segments) { segment in
                        // A 2 pt floor keeps a tiny nonzero share visible; it
                        // bends strict proportionality by at most ~2 pt per
                        // slice (≤ 7 slices), absorbed by the clip at the end.
                        segment.color
                            .frame(width: max(proxy.size.width * segment.fraction, segment.fraction > 0 ? 2 : 0))
                            .help(segment.tooltip)
                    }
                }
            }
            .frame(height: 6)
            .clipShape(RoundedRectangle(cornerRadius: 2))
            // The legend entries below are the accessible proxy for each
            // slice (C35), so the 6 pt bar is not read a second time.
            .accessibilityHidden(true)

            legend(segments)

            // The full itemized list — with exact counts, shares and each
            // type's definition — is always one click away, so no fact is
            // tooltip-only (C35).
            OverflowDisclosure(
                label: "All tool types",
                identifier: "work.overflow.tool-types"
            ) {
                fullTypeList
            }
        }
    }

    /// A swatch + label + exact count per displayed slice; wraps at any width.
    /// Each entry is the largest visual proxy of its datum, so it carries the
    /// type's definition as help and is one accessibility element (C35).
    private func legend(_ segments: [BarSegment]) -> some View {
        LazyVGrid(
            columns: [GridItem(.adaptive(minimum: 150), spacing: Space.m, alignment: .leading)],
            alignment: .leading,
            spacing: 6
        ) {
            ForEach(segments) { segment in
                HStack(spacing: 6) {
                    RoundedRectangle(cornerRadius: 1.5)
                        .fill(segment.color)
                        .frame(width: 8, height: 8)
                    // One line per key: a wrapped label ("Connected\ntools")
                    // misaligns every count in the row.
                    Text(segment.label).font(dataSmallFont).foregroundStyle(Theme.muted)
                        .lineLimit(1).fixedSize(horizontal: true, vertical: false)
                    Text("\(segment.count)").font(dataSmallSemiboldFont)
                        .foregroundStyle(Theme.ink).monospacedDigit()
                    Spacer(minLength: 0)
                }
                .contentShape(Rectangle())
                .help(segment.tooltip)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(segment.label)
                .accessibilityValue(segment.axValue)
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Tool calls by type")
    }

    /// Every captured type with its exact count and share — the overflow body.
    private var fullTypeList: some View {
        let denom = synopsis.shareDenominator ?? 1
        return VStack(alignment: .leading, spacing: Space.s) {
            ForEach(synopsis.metrics) { metric in
                let percent = (Double(metric.count) / Double(denom))
                    .formatted(.percent.precision(.fractionLength(0...1)))
                VStack(alignment: .leading, spacing: 2) {
                    HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                        Text(metric.label).font(captionSemiboldFont).foregroundStyle(Theme.ink)
                        Spacer(minLength: Space.s)
                        Text("\(metric.count) · \(percent)").font(dataSmallSemiboldFont)
                            .foregroundStyle(Theme.ink).monospacedDigit()
                    }
                    Text(metric.detail).font(captionFont).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(metric.label)
                .accessibilityValue("\(metric.count), \(percent). \(metric.detail)")
            }
        }
    }

    private var actionDistribution: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                Text("Tool calls by type")
                    .font(captionSemiboldFont)
                    .foregroundStyle(Theme.ink)
                Spacer(minLength: Space.s)
                if !compact {
                    Text("Shared scale")
                        .font(dataSmallFont)
                        .foregroundStyle(Theme.muted)
                }
            }
            .padding(.top, Space.xs)
            .accessibilityHidden(true)

            if !compact {
                Text("Counts describe captured tool calls, not progress or success.")
                    .font(captionFont)
                    .foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }

            ForEach(synopsis.metrics) { metric in
                actionDistributionRow(metric)
            }

            if let denominator = synopsis.shareDenominator {
                HStack(alignment: .firstTextBaseline) {
                    Text("0")
                    Spacer(minLength: Space.s)
                    Text("\(denominator) tool calls")
                }
                .font(dataSmallFont)
                .foregroundStyle(Theme.muted)
                .monospacedDigit()
                .accessibilityHidden(true)
            }
        }
    }

    private func actionDistributionRow(_ metric: ReceiptActionMetric) -> some View {
        let denominator = synopsis.shareDenominator ?? 1
        let fraction = min(max(CGFloat(metric.count) / CGFloat(denominator), 0), 1)
        return VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                Text(metric.label)
                    .font(captionSemiboldFont)
                    .foregroundStyle(Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: Space.s)
                Text(distributionValue(metric))
                    .font(dataSmallSemiboldFont)
                    .foregroundStyle(Theme.ink)
                    .monospacedDigit()
            }
            if !compact {
                Text(metric.detail)
                    .font(captionFont)
                    .foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            GeometryReader { proxy in
                Rectangle()
                    // One series, one chart bar token — never the interactive
                    // accent, which would read as a control (K04).
                    .fill(Theme.chartBar)
                    .frame(width: proxy.size.width * fraction, height: 4)
                    .frame(maxHeight: .infinity, alignment: .center)
            }
            .frame(height: 6)
            .accessibilityHidden(true)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(metric.label)
        .accessibilityValue(
            synopsis.shareDenominator.map {
                compact ? "\(metric.count) of \($0)" : "\(metric.count) of \($0). \(metric.detail)"
            } ?? (compact ? "\(metric.count)" : "\(metric.count). \(metric.detail)")
        )
    }

    private func metricGrid(columns: Int) -> some View {
        LazyVGrid(
            columns: Array(
                repeating: GridItem(.flexible(minimum: 0), spacing: Space.l, alignment: .leading),
                count: columns
            ),
            alignment: .leading,
            spacing: Space.s
        ) {
            ForEach(synopsis.metrics) { metric in
                VStack(alignment: .leading, spacing: 2) {
                    HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                        Text(metric.label)
                            .font(captionSemiboldFont)
                            .foregroundStyle(Theme.ink)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: Space.xs)
                        Text(metricValue(metric))
                            .font(dataSmallSemiboldFont)
                            .foregroundStyle(Theme.ink)
                            .monospacedDigit()
                            .fixedSize(horizontal: true, vertical: false)
                    }
                    Text(metric.detail)
                        .font(captionFont)
                        .foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(metric.label)
                .accessibilityValue(
                    synopsis.shareDenominator.map {
                        "\(metric.count) of \($0). \(metric.detail)"
                    } ?? "\(metric.count). \(metric.detail)"
                )
            }
        }
    }

    private func metricValue(_ metric: ReceiptActionMetric) -> String {
        guard let denominator = synopsis.shareDenominator else { return "\(metric.count)" }
        return "\(metric.count) of \(denominator)"
    }

    private func distributionValue(_ metric: ReceiptActionMetric) -> String {
        guard let denominator = synopsis.shareDenominator, denominator > 0 else {
            return "\(metric.count)"
        }
        let percent = Double(metric.count) / Double(denominator)
        return "\(metric.count) · \(percent.formatted(.percent.precision(.fractionLength(0...1))))"
    }

    private func metadataLine(label: String, value: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Space.s) {
            Text("\(label):")
                .font(captionSemiboldFont)
                .foregroundStyle(Theme.ink)
            Text(value)
                .font(dataSmallFont)
                .foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(label)
        .accessibilityValue(value)
    }

    private func noticeLine(prefix: String, text: String, tone: Color) -> some View {
        HStack(alignment: .top, spacing: Space.s) {
            Image(systemName: "exclamationmark.triangle")
                .workFont(.icon)
                .foregroundStyle(tone)
                .padding(.top, 2)
                .accessibilityHidden(true)
            Text("\(prefix): \(text)")
                .font(captionFont)
                .foregroundStyle(tone)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(prefix)
        .accessibilityValue(text)
    }
}

/// The receipt-dimensions ledger: one row per dimension — name, value,
/// provenance chips, and the dimension's own gaps inline as named amber facts.
struct RecordDimensionsCard: View {
    /// Which parts of the captured ledger this instance shows. Callers split
    /// the dimensions so every fact has exactly one home on the page.
    enum Dimension: CaseIterable {
        case task, agents, actions, cost, checks, outcome
    }

    let receipt: Receipt
    var included: Set<Dimension> = Set(Dimension.allCases)
    /// Whole-task facts (Sources, Gaps) already own these; scoped topic rows
    /// hide the per-dimension repeats.
    var showsProvenance = true
    var showsGaps = true
    /// Topic use drops the digest's explanatory copy into help.
    var compactDigest = false
    /// The record page deletes the WEEKLY PLAN row: its absence is one noun in
    /// the collapsed absence line, and its figure rides the cost row.
    var showsPlanShare = true
    /// When true a dimension whose only value is a named absence is dropped —
    /// the record page's absence budget states it once instead, in one line for
    /// the whole page. Off by default, so every other caller is unchanged.
    var dropsAbsentValues = false

    private var ordered: [Dimension] { Dimension.allCases.filter(included.contains).filter(carriesAFact) }

    /// Whether a dimension row has anything to say that the page does not
    /// already say louder.
    ///
    /// The TASK row prints the recorded objectives. On a Task whose single
    /// objective IS its title, that row restated the page heading word for word
    /// at the very bottom of the record — the heading's third printing (F1).
    /// The row is dropped ONLY when it would carry nothing else: a second
    /// objective, a recorded gap or a provenance chip all keep it, because each
    /// of those is a fact the heading does not hold.
    private func carriesAFact(_ dimension: Dimension) -> Bool {
        // A cost row whose only content is "we recorded no usage" is an ABSENCE,
        // and the record page states its absences once, together. A recorded
        // token volume or a priced figure is a FACT about the work — often the
        // only one on a thin record — so the row stays whenever either exists.
        if dropsAbsentValues, dimension == .cost {
            let dim = receipt.dimensions.cost
            if (dim.tokens?.total ?? 0) > 0 { return true }
            return dim.estimatedCostUsd != nil
        }
        guard dimension == .task else { return true }
        let objectives = receipt.dimensions.task.objectives ?? []
        guard objectives.count == 1, restatesPayloadText(objectives[0], receipt.title) else { return true }
        if showsGaps, !(receipt.dimensions.task.gaps ?? []).isEmpty { return true }
        if showsProvenance, !(receipt.dimensions.task.provenance ?? []).isEmpty { return true }
        return false
    }
    /// Row names come from the reducer's `field_labels` (C39).
    private var labels: ReceiptFieldLabels { receipt.fieldLabels ?? ReceiptFieldLabels() }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(ordered.enumerated()), id: \.offset) { index, dimension in
                if index > 0 { hairline }
                row(for: dimension)
            }
        }
    }

    @ViewBuilder
    private func row(for dimension: Dimension) -> some View {
        switch dimension {
        case .task:
            dimensionRow(labels.taskLabel, taskSummary,
                         provenance: receipt.dimensions.task.provenance,
                         gaps: receipt.dimensions.task.gaps)
        case .agents:
            dimensionRow(labels.agentsLabel, actorsSummary,
                         provenance: receipt.dimensions.actors.provenance,
                         gaps: receipt.dimensions.actors.gaps)
        case .actions:
            actionsRow
        case .cost:
            dimensionRow(labels.costLabel, costSummary,
                         provenance: receipt.dimensions.cost.provenance,
                         gaps: receipt.dimensions.cost.gaps)
            if showsPlanShare,
               receipt.dimensions.cost.planShare != nil
                || PayloadAbsence.text(receipt.dimensions.cost.planShareHeadline) != nil {
                hairline
                // The reducer's plan-share headline, or its named absence —
                // never a dash (C17).
                dimensionRow(labels.weeklyPlanLabel, receipt.dimensions.cost.planShareText, provenance: nil, gaps: nil)
            }
        case .checks:
            dimensionRow(labels.checksLabel, evidenceSummary,
                         provenance: receipt.dimensions.evidence.provenance,
                         gaps: receipt.dimensions.evidence.gaps)
        case .outcome:
            dimensionRow(labels.decisionLabel, outcomeSummary,
                         provenance: receipt.dimensions.outcome.provenance,
                         gaps: receipt.dimensions.outcome.gaps,
                         verbatimValue: true)
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
            CapsLabel(text: name)
                .frame(width: 104, alignment: .leading)
                .padding(.top, 3)
            VStack(alignment: .leading, spacing: 6) {
                if verbatimValue {
                    // Outcome statements quote agent text — never parse as markdown.
                    Text(verbatim: summary).workFont(.body).foregroundStyle(Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    Text(summary).workFont(.body).foregroundStyle(Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if showsProvenance, let provenance, !provenance.isEmpty {
                    HStack(spacing: 6) {
                        ForEach(provenance, id: \.self) { source in
                            // The reducer's source label, never the raw key (C38).
                            ProvenanceChip(text: receipt.dimensions.provenance.sourceLabel(for: source))
                        }
                    }
                }
                if showsGaps {
                    // A gap sentence that only restates this row's own value
                    // (`no usage recorded` beside `No usage was recorded…`) is
                    // not drawn twice.
                    ForEach((gaps ?? []).filter { !gapRestatesValue($0, summary) }, id: \.self) { gap in
                        // A dimension's own blind spot, named where the value lives.
                        // Caveat prose is muted; a gap is not a tier, so it
                        // carries the caveat marker, never a pip (C24/K05).
                        HStack(spacing: 6) {
                            CaveatMarker()
                            Text(gap).workFont(.caption).foregroundStyle(Theme.muted)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.vertical, Space.m)
        // A field name and its value are one fact, as the summary tiles above
        // already are: two loose siblings left VoiceOver users to pair "COST"
        // with "no usage recorded" themselves (K124).
        .accessibilityElement(children: .combine)
    }

    /// True when a gap sentence carries no word beyond the row's value (case
    /// and filler words aside): it restates the value rather than adding a fact.
    private func gapRestatesValue(_ gap: String, _ value: String) -> Bool {
        let filler: Set<String> = ["a", "an", "the", "was", "were", "is", "for", "this", "task", "of", "no", "not"]
        func words(_ text: String) -> Set<String> {
            Set(text.lowercased().split(whereSeparator: { !$0.isLetter && !$0.isNumber }).map(String.init))
        }
        let valueWords = words(value.components(separatedBy: "\n").first ?? value)
        let gapWords = words(gap).subtracting(filler)
        guard !gapWords.isEmpty, !valueWords.isEmpty else { return false }
        // `recorded` / `record` share a stem with the value's absence word.
        let stems = Set(valueWords.map { String($0.prefix(6)) })
        return gapWords.allSatisfy { valueWords.contains($0) || stems.contains(String($0.prefix(6))) }
    }

    // V1 carries aggregates, independently collected path scope, and row-wide
    // provenance—not canonical action rows. Keep the daily receipt bounded to
    // facts that can be related honestly.
    private var actionsRow: some View {
        let dim = receipt.dimensions.actions
        return ReceiptActionsDigest(
            synopsis: dim.synopsis,
            relatedPathsText: dim.relatedPathsText,
            sourceText: dim.actionSourcesText,
            gaps: dim.gaps,
            compact: compactDigest,
            label: labels.actionsLabel,
            ledger: dim
        )
    }

    // MARK: dimension summaries

    /// The primary objective as one clean sentence. Further objectives are
    /// counted, not concatenated — a semicolon-joined run-on reads as a wall,
    /// not a summary. The project is meta (it rides the header meta line),
    /// never appended into the objective sentence.
    private var taskSummary: String {
        let objectives = receipt.dimensions.task.objectives ?? []
        guard let first = objectives.first, !first.isEmpty else { return "no objective recorded" }
        let more = objectives.count - 1
        return more > 0 ? "\(first)  ·  +\(Fmt.count(more, "more objective"))" : first
    }

    private var actorsSummary: String {
        let dim = receipt.dimensions.actors
        var parts: [String] = []
        if let agent = dim.primaryAgent { parts.append(agent) }
        if let models = dim.models, !models.isEmpty { parts.append(models.joined(separator: ", ")) }
        if let subagents = dim.subagentsText { parts.append(subagents) }
        return parts.isEmpty ? "no agent recorded" : parts.joined(separator: " · ")
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
        // The reducer's cost line: `display_text · basis_label`, or the named
        // absence it sent (C17/C20). Recorded token volume stays a fact
        // beside it, priced or not.
        let line = dim.text
        guard let tokensLine else { return line }
        return line + "\n" + tokensLine
    }

    /// The reducer's check tally (`check_tally_text`), with supersession and
    /// earlier failures named (C10); an older payload keeps its supplied
    /// counts in the reducer's grammar.
    private var evidenceSummary: String {
        ReceiptCheckRunsPresentation(evidence: receipt.dimensions.evidence).rowText
    }

    private var outcomeSummary: String {
        receiptOutcomeSummary(receipt.dimensions.outcome)
    }
}

// MARK: - Disposition controls

/// The human attention controls for one finding or blocker: Mark reviewed,
/// Resolve… (note REQUIRED — recorded as your assertion, never machine
/// verification), Reopen. Posts the append-only disposition through the
/// daemon and surfaces its own conflict copy verbatim ("blocker changed…"),
/// so optimistic-concurrency refusals read as facts, not mystery failures.
///
/// Each action states its effect BEFORE it is taken: the reducer's
/// `attention.effects` sentence rides the button's help and the resolve
/// popover (C86). No sentence is invented here when the payload has none.
struct DispositionControls: View {
    let kind: String
    let state: String
    let revision: Int
    let taskId: String
    var targetDigest: String? = nil
    var blockedEventId: String? = nil
    /// The reducer's effect sentence for each action (nil on older payloads).
    var effects: ReceiptDispositionEffects? = nil
    @Environment(DashboardStore.self) var dashboard
    @State private var resolvePopoverShown = false
    @State private var note = ""
    @State private var busy = false
    @State private var errorText: String?

    private var reviewedEffect: String? { PayloadAbsence.text(effects?.reviewed) }
    private var resolvedEffect: String? { PayloadAbsence.text(effects?.resolved) }
    private var reopenEffect: String? { PayloadAbsence.text(effects?.reopen) }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: Space.s) {
                if busy {
                    ProgressView().controlSize(.small)
                } else {
                    if state == "open" {
                        actionButton("Mark reviewed", effect: reviewedEffect) { post("mark_reviewed", note: nil) }
                    }
                    if state != "resolved" {
                        Button {
                            resolvePopoverShown = true
                        } label: {
                            Text("Resolve…").workFont(.captionSemibold).foregroundStyle(Theme.accent)
                        }
                        .buttonStyle(QuietButtonStyle())
                        .keyboardStop { resolvePopoverShown = true }
                        .modifier(OptionalHelp(text: resolvedEffect))
                        .accessibilityHint(resolvedEffect ?? "")
                        .accessibilityIdentifier("disposition.resolve.\(kind)")
                        .popover(isPresented: $resolvePopoverShown, arrowEdge: .bottom) {
                            resolveSheet
                        }
                    }
                    if state != "open" {
                        actionButton("Reopen", effect: reopenEffect) { post("reopen", note: nil) }
                    }
                }
            }
            // The quiet actions start this text column: their labels sit on
            // the text edge and the hover wash hangs into the margin (K27).
            .hangingLeading()
            if let errorText {
                Text(verbatim: errorText)
                    .workFont(.dataSmall).foregroundStyle(Theme.coral)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .disabled(dashboard.isOfflineSnapshot)
        // Offline only: the read-only reason. Online, each button carries its
        // own effect sentence instead of an empty container help.
        .modifier(OptionalHelp(
            text: dashboard.isOfflineSnapshot ? "Saved work is read-only. Reconnect to change a finding." : nil
        ))
    }

    private func actionButton(_ label: String, effect: String?, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(label).workFont(.captionSemibold).foregroundStyle(Theme.accent)
        }
        .buttonStyle(QuietButtonStyle())
        .keyboardStop(activate: action)
        .modifier(OptionalHelp(text: effect))
        .accessibilityHint(effect ?? "")
        .accessibilityIdentifier("disposition.\(label.lowercased().replacingOccurrences(of: " ", with: "-")).\(kind)")
    }

    private var resolveSheet: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            CapsLabel(text: "Resolve \(kind)")
            if let resolvedEffect {
                // The same effect sentence the button's help states.
                Text(verbatim: resolvedEffect)
                    .workFont(.body).foregroundStyle(Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text("Say what resolved it — recorded as your assertion, never machine verification.")
                .workFont(.caption).foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
            AppTextField(
                placeholder: "e.g. fixed by hand in a later commit",
                text: $note,
                font: .body,
                axis: .vertical,
                lineLimit: 2...4
            )
            HStack {
                Spacer()
                Button {
                    resolvePopoverShown = false
                    post("resolve", note: note)
                } label: {
                    Text("Record resolution").workFont(.captionSemibold)
                }
                .foregroundStyle(Theme.accent)
                .buttonStyle(QuietButtonStyle(prominent: true))
                .disabled(note.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .accessibilityIdentifier("disposition.record-resolve.\(kind)")
            }
        }
        .padding(Space.l)
        // Pin the popover width. Without a definite width the two
        // .fixedSize(vertical:) Text rows wrap into a tall tower during the
        // popover's width negotiation (measured 949pt), ballooning the sheet;
        // a fixed width holds it at a stable ~130pt. Mirrors the status legend
        // popover, which pins .frame(width: 440) for the same reason.
        .frame(width: 340)
        // Opaque card ground: tile text never bleeds behind the field (K84).
        .popoverSurface()
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

// MARK: - Attention callout

/// The ONE attention callout for the whole danger family — a failed check, a
/// blocker, a failed step — right under the verdict (C03). Everything it
/// says is the reducer's attention block: the eyebrow is `reason_label`, the
/// body is the verbatim `summary`, the mono identity line is `label`
/// (`Failed test check · pytest · exit 2`), then the recorded next step, when
/// it was last updated, and the disposition controls posting with the block's
/// own `action_token` and `revision`. `attention.kind` is only the internal
/// key that picks which disposition endpoint to post to; it is never shown.
///
/// A dispositioned or closed item keeps showing (so the change is auditable
/// and reopenable) but drops the coral "needs you" tone for a neutral wash.
struct AttentionCallout: View {
    let attention: ReceiptAttention
    let taskId: String
    /// Blocker-only facts the attention block does not carry (later completed
    /// steps, more blocked steps). Optional; nil renders none of them.
    var blocker: ReceiptBlocker? = nil

    private var dispositionState: String {
        PayloadAbsence.text(attention.dispositionState)
            ?? PayloadAbsence.text(blocker?.disposition?.state)
            ?? "open"
    }

    /// Whether the item is still open for you.
    private var needsYou: Bool {
        if let open = attention.open { return open }
        return dispositionState == "open"
    }

    /// Coral means a recorded failure that still needs you. A check that
    /// could not run (the reducer's `not_run` tone) proved nothing, so it
    /// stays muted even while open — never the failure color.
    private var failureTone: Bool {
        needsYou && CheckResultTone(payload: attention.resultTone ?? "failure") == .failure
    }

    /// The disposition endpoint's kind for this attention key: a check item
    /// (a failed check, or one that could not run) posts to the finding
    /// endpoint with its target digest; a blocker or failed step to the blocker one.
    private var dispositionKind: String {
        ["failed_check", "check_not_run"].contains(attention.kind) ? "finding" : "blocker"
    }

    /// The id the disposition endpoint keys on, when the block carries one.
    private var dispositionHandle: String? {
        dispositionKind == "finding"
            ? PayloadAbsence.text(attention.targetDigest)
            : PayloadAbsence.text(attention.actionToken)
    }

    private var tone: Color { failureTone ? Theme.coral : Theme.muted }

    private var icon: String {
        if !needsYou {
            // A REVIEWED finding is still unresolved — a checkmark would read
            // as fixed, and the decision badge beside it still says Finding.
            // Only a RESOLVED disposition wears the check (C4).
            return dispositionState == "resolved" ? "checkmark.circle" : "eye.circle"
        }
        return failureTone ? "hand.raised" : CheckResultTone.notRun.symbol
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            // The shared attention block: the same order and weights the
            // Dashboard's card uses (K21). "last updated" is the recorded time
            // of the attention item, which later non-terminal events can bump
            // past the moment it was first recorded.
            AttentionBlockBody(
                reasonLabel: PayloadAbsence.text(attention.reasonLabel),
                summary: PayloadAbsence.text(attention.summary),
                label: PayloadAbsence.text(attention.label),
                noteText: PayloadAbsence.text(attention.noteText),
                nextStep: attention.nextStep,
                variant: .record,
                tone: tone,
                icon: icon,
                recency: agoText(attention.observedAt ?? blocker?.updatedAt).map { "last updated \($0)" }
            )
            if let later = blocker?.laterCompletedSteps, later > 0 {
                // The count IS the fact; whether it cleared the blocker is not
                // in the data, so the copy never speculates. Caveat prose is
                // muted with the caveat marker, never a pip (C24/K05).
                HStack(spacing: 6) {
                    CaveatMarker()
                    Text("\(later) step\(later == 1 ? "" : "s") completed after this blocker's last update.")
                        .workFont(.caption).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if let more = PayloadAbsence.text(attention.moreText) {
                // The reducer's count of every other open item behind this
                // lead one (findings, blockers, checks that could not run).
                Text(verbatim: more)
                    .workFont(.dataSmall).foregroundStyle(Theme.muted)
            }
            if dispositionState != "open" {
                let note = PayloadAbsence.text(attention.dispositionNote)
                    ?? PayloadAbsence.text(blocker?.disposition?.note)
                Text(verbatim: "marked \(dispositionState) by you" + (note.map { " — \($0)" } ?? ""))
                    .workFont(.dataSmall).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // Review or resolve this exact item. A finding posts with its
            // target digest, a blocker with its event (`action_token`); an
            // item without that handle offers no controls.
            if let handle = dispositionHandle {
                DispositionControls(
                    kind: dispositionKind,
                    state: dispositionState,
                    revision: attention.revision ?? blocker?.dispositionRevision ?? 0,
                    taskId: taskId,
                    targetDigest: dispositionKind == "finding" ? handle : nil,
                    blockedEventId: dispositionKind == "blocker" ? handle : nil,
                    effects: attention.effects
                )
            }
        }
        .padding(Space.l)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            failureTone ? Theme.tintCoral : Theme.tintNeutral,
            in: RoundedRectangle(cornerRadius: Metrics.radius)
        )
        // A named group: its own caption (the reducer's reason noun) tells a
        // VoiceOver user what this block is on entry, instead of an unnamed
        // container between the verdict and the evidence (K121).
        .accessibilityElement(children: .contain)
        .accessibilityLabel(PayloadAbsence.text(attention.reasonLabel) ?? "Attention")
        .accessibilityIdentifier("receipt.attention")
    }
}

// MARK: - Standing proof

/// The strongest STANDING proof on a receipt — the passing run a reviewer
/// would point at, with the reducer's own sentence for why that grade is the
/// grade. A history run proves nothing standing (a later run replaced it) and
/// a check that could not run proved nothing at all, so neither can be it.
///
/// Every string here is the reducer's; this only chooses which to show.
struct ReceiptStandingProof {
    let check: ReceiptCheck
    /// `evidence_grade_reason` — the reducer's sentence for the graded step
    /// ("the agent reported a check passed (…) — the agent's own, not
    /// independent"). Nil when the payload carried none.
    let gradeReason: String?
    /// The receipt's strongest tier key and the payload's word for it.
    let tierKey: String?
    let tierLabel: String?

    /// Whether a recorded run is a standing pass. `result_tone` is the
    /// reducer's key; an older payload without one falls back to `result`.
    static func isStandingPass(_ check: ReceiptCheck) -> Bool {
        guard check.historyRun != true, check.superseded != true else { return false }
        if PayloadAbsence.text(check.resultTone) != nil {
            return CheckResultTone(payload: check.resultTone) == .pass
        }
        return check.result == "passed"
    }

    init?(receipt: Receipt) {
        let passes = (receipt.dimensions.evidence.checks ?? []).filter(Self.isStandingPass)
        guard let newest = passes.max(by: { ($0.at ?? 0) < ($1.at ?? 0) }) else { return nil }
        check = newest
        let strength = receipt.axes.evidenceStrength
        tierKey = PayloadAbsence.text(strength.strongestTier)
        tierLabel = PayloadAbsence.text(
            strength.tierLegend?.first(where: { $0.key == strength.strongestTier })?.label
        )
        // The grade sentence belongs to a graded STEP, not to the check row.
        gradeReason = receipt.timeline?.events
            .compactMap { PayloadAbsence.text($0.evidenceGradeReason) }
            .first
    }

    /// The recorded identity of the proving run, its own facts joined in the
    /// order the checks table prints them.
    var identityLine: String {
        joinedRecordedSentences([
            PayloadAbsence.text(check.title) ?? PayloadAbsence.text(check.name),
            PayloadAbsence.text(check.resultLabel),
            check.exitCode.map { "Exit \($0)" },
            PayloadAbsence.text(check.evidenceType),
            PayloadAbsence.text(check.sourceLabel),
        ])
    }

    /// The revision line for the proving run, when it ran on a tree with
    /// uncommitted changes — the qualifier a `Verified` hero owes (C5). The
    /// wording is the reducer's `revision_label`, never assembled here.
    var dirtyRevisionLabel: String? {
        guard check.revision?.dirty == true else { return nil }
        return PayloadAbsence.text(check.revisionLabel)
    }
}

/// The standing-proof callout is DELETED, and with it `EvidenceCallout`.
///
/// The slot it filled is not gone — the record's hero card carries ONE exhibit,
/// filled by an open attention item or, when nothing needs you, by the standing
/// proof's own identity line. What is gone is this block's duplication: on the
/// flagship record it was a verbatim copy of one Checks row (the same headline,
/// the same meta line, the same pip, the same stamped revision and the same
/// contradiction sentence) plus a prose restatement of the tier. That prose is
/// not lost either: it is now the tier's context help, on the Evidence heading
/// where the tier is stated.


// MARK: - Evidence coverage

/// The evidence-coverage card: the checked/checkable headline, a coverage bar
/// whose segment widths are strictly proportional to the tier counts, a
/// counted legend wearing the pip shapes, and the honesty ledger.
// MARK: - Checks

/// Every check the store holds for this receipt, with its result mark and
/// source. A pass takes its source's evidence-tier color (the reducer's
/// `tier_key`), never green by source name (C27); the source chip says why.
struct RecordChecksCard: View {
    let evidence: ReceiptEvidenceDim
    let taskId: String
    private let initiallyExpandedCheckIDs: Set<String>
    private let initiallyShowsRoutineGroups: Bool?
    private let collection: ReceiptCheckCollectionPresentation
    /// Source key → the reducer's evidence-tier key for checks from it.
    private let sourceTiers: [String: String]

    @State private var passedExpansionOverride: Bool?
    @State private var historyExpansionOverride: Bool?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// The reducer's tally (`check_tally_text`): supersession and earlier
    /// failures are named remainders, so the counts add up (C10).
    private var presentation: ReceiptCheckRunsPresentation {
        ReceiptCheckRunsPresentation(evidence: evidence)
    }

    init(
        evidence: ReceiptEvidenceDim,
        taskId: String,
        sources: [ReceiptSourceEntry]? = nil,
        initiallyShowsRoutineGroups: Bool? = nil,
        initiallyExpandedCheckIDs: Set<String> = []
    ) {
        self.evidence = evidence
        self.taskId = taskId
        self.initiallyExpandedCheckIDs = initiallyExpandedCheckIDs
        self.initiallyShowsRoutineGroups = initiallyShowsRoutineGroups
        self.collection = ReceiptCheckCollectionPresentation(evidence: evidence)
        var tiers: [String: String] = [:]
        for entry in sources ?? [] {
            if let tier = PayloadAbsence.text(entry.tierKey) { tiers[entry.key] = tier }
        }
        self.sourceTiers = tiers
        _passedExpansionOverride = State(initialValue: nil)
        _historyExpansionOverride = State(initialValue: nil)
    }

    var body: some View {
        Card(padding: Space.xl) {
            VStack(alignment: .leading, spacing: 0) {
                ViewThatFits(in: .horizontal) {
                    HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                        checksHeading
                        Spacer(minLength: Space.m)
                        checksTally
                    }
                    VStack(alignment: .leading, spacing: Space.xs) {
                        checksHeading
                        checksTally
                    }
                }
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("Checks, \(presentation.headerText)")
                .accessibilityAddTraits(.isHeader)

                if let scope = collection.sharedScope {
                    HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                        CapsLabel(text: "Scope")
                        Text(verbatim: scope)
                            .workFont(.dataSmall)
                            .foregroundStyle(Theme.muted)
                            .fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled)
                    }
                    .padding(.top, Space.s)
                }

                if let notice = collection.aggregateNotice {
                    HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                        Image(systemName: "exclamationmark.triangle")
                            .foregroundStyle(Theme.amber)
                            .accessibilityHidden(true)
                        Text(notice)
                            .workFont(.caption)
                            .foregroundStyle(Theme.amber)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(.top, Space.s)
                }

                if let notice = collection.itemizedNotice {
                    Text(notice)
                        .workFont(.dataSmall)
                        .foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, Space.s)
                }

                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.top, Space.m)
                if collection.rows.isEmpty {
                    let copy = receiptEmptyCheckDetailsCopy(
                        total: evidence.checksTotal,
                        passed: evidence.checksPassed,
                        failed: evidence.checksFailed
                    )
                    Text(copy.title)
                        .workFont(.body).foregroundStyle(Theme.muted)
                        .padding(.top, Space.m)
                    Text(copy.detail)
                        .workFont(.caption).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, Space.xs)
                } else if collection.rows.count == 1, let row = collection.rows.first {
                    checkRow(row)
                } else {
                    let attention = collection.rows(in: .attention)
                    let other = collection.rows(in: .other)
                    let passed = collection.rows(in: .passed)
                    let history = collection.rows(in: .history)

                    if !attention.isEmpty {
                        fixedGroup(title: "Needs attention", rows: attention, tone: Theme.coral)
                    }
                    if !other.isEmpty {
                        if !attention.isEmpty { sectionDivider }
                        // Checks that proved nothing either way: muted, never
                        // the amber reserved for tiers and thresholds.
                        fixedGroup(title: "Other results", rows: other, tone: Theme.muted)
                    }
                    if !passed.isEmpty {
                        if !attention.isEmpty || !other.isEmpty { sectionDivider }
                        disclosureGroup(
                            title: "Passed checks",
                            rows: passed,
                            expanded: routineGroupExpansion($passedExpansionOverride)
                        )
                    }
                    if !history.isEmpty {
                        if !attention.isEmpty || !other.isEmpty || !passed.isEmpty { sectionDivider }
                        disclosureGroup(
                            title: "History",
                            rows: history,
                            expanded: routineGroupExpansion($historyExpansionOverride)
                        )
                    }
                }
            }
        }
    }

    private var checksHeading: some View {
        Text("Checks")
            .workFont(.titleCard)
            .foregroundStyle(Theme.ink)
    }

    private var checksTally: some View {
        Text(presentation.headerText)
            .workFont(.dataSmall)
            .foregroundStyle(Theme.muted)
            .fixedSize(horizontal: false, vertical: true)
    }

    private var sectionDivider: some View {
        Rectangle().fill(Theme.hairline).frame(height: 1)
    }

    private func routineGroupExpansion(_ override: Binding<Bool?>) -> Binding<Bool> {
        Binding(
            get: {
                collection.routineGroupExpanded(
                    userOverride: override.wrappedValue,
                    forcedDefault: initiallyShowsRoutineGroups
                )
            },
            set: { override.wrappedValue = $0 }
        )
    }

    private func fixedGroup(
        title: String,
        rows: [ReceiptCheckRowPresentation],
        tone: Color
    ) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                Text(title)
                    .workFont(.captionSemibold)
                    .foregroundStyle(tone)
                Text("\(rows.count)")
                    .workFont(.dataSmall)
                    .foregroundStyle(Theme.muted)
            }
            .padding(.top, Space.m)
            rowsView(rows)
        }
    }

    private func disclosureGroup(
        title: String,
        rows: [ReceiptCheckRowPresentation],
        expanded: Binding<Bool>
    ) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                expanded.wrappedValue.toggle()
            } label: {
                HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                    Image(systemName: "chevron.right")
                        .workFont(.icon)
                        .foregroundStyle(Theme.muted)
                        .rotationEffect(.degrees(expanded.wrappedValue ? 90 : 0))
                        .accessibilityHidden(true)
                    Text(title)
                        .workFont(.captionSemibold)
                        .foregroundStyle(Theme.ink)
                    Text("\(rows.count)")
                        .workFont(.dataSmall)
                        .foregroundStyle(Theme.muted)
                    Spacer(minLength: Space.s)
                    Text(expanded.wrappedValue ? "Hide" : "Show")
                        .workFont(.dataSmallSemibold)
                        .foregroundStyle(Theme.accent)
                }
                .padding(.vertical, Space.m)
                .contentShape(Rectangle())
            }
            .buttonStyle(SurfaceButtonStyle(focusInset: 2))
            .accessibilityLabel("\(title), \(rows.count)")
            .accessibilityValue(expanded.wrappedValue ? "Expanded" : "Collapsed")
            .accessibilityHint(expanded.wrappedValue ? "Hides these check runs" : "Shows these check runs")
            .accessibilityIdentifier("receipt.check-group.\(title.lowercased().replacingOccurrences(of: " ", with: "-"))")

            if expanded.wrappedValue {
                rowsView(rows)
                    .transition(.opacity)
            }
        }
        .animation(reduceMotion ? nil : Motion.contentUpdate, value: expanded.wrappedValue)
    }

    private func rowsView(_ rows: [ReceiptCheckRowPresentation]) -> some View {
        ScrollContentStack(alignment: .leading, spacing: 0) {
            ForEach(Array(rows.enumerated()), id: \.element.id) { index, row in
                if index > 0 {
                    Rectangle().fill(Theme.hairline).frame(height: 1)
                }
                checkRow(row)
            }
        }
    }

    private func checkRow(_ row: ReceiptCheckRowPresentation) -> some View {
        RecordCheckRow(
            row: row,
            taskId: taskId,
            showsScope: collection.sharedScope == nil,
            tierKey: row.check.source.flatMap { sourceTiers[$0] },
            initiallyExpanded: initiallyExpandedCheckIDs.contains(row.id)
        )
    }
}

/// One check: a scannable summary that expands in place to the full recorded
/// detail (result, source, scope, files, timestamp, disposition, artifact refs,
/// and an honest note when command text was intentionally not captured).
private struct RecordCheckRow: View {
    let row: ReceiptCheckRowPresentation
    let taskId: String
    let showsScope: Bool
    /// The reducer's evidence-tier key for this check's source, when known.
    let tierKey: String?

    @State private var expanded: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var check: ReceiptCheck { row.check }

    /// The check's short recorded name is its title (C08); the presentation
    /// title (the reducer's name-first `title`) covers a check without one.
    private var title: String { PayloadAbsence.text(check.name) ?? row.title }

    init(
        row: ReceiptCheckRowPresentation,
        taskId: String,
        showsScope: Bool,
        tierKey: String? = nil,
        initiallyExpanded: Bool = false
    ) {
        self.row = row
        self.taskId = taskId
        self.showsScope = showsScope
        self.tierKey = tierKey
        _expanded = State(initialValue: initiallyExpanded)
    }

    /// The reducer's tone key picks the mark: a pass wears its evidence
    /// tier's color (green only for externally verified; an unknown tier stays
    /// ink, C27), a recorded failure is a coral cross, and a check that could
    /// not run is a muted minus — never a failure mark.
    private var mark: (symbol: String, tint: Color) {
        let tone = CheckResultTone(payload: check.resultTone)
        return (tone.symbol, tone.tint(pass: tierKey.map { EvidenceTierStyle.forGrade($0).tint } ?? Theme.ink))
    }

    private var headerLine: some View {
        HStack(alignment: .top, spacing: Space.s) {
            Image(systemName: "chevron.right")
                .workFont(.icon)
                .foregroundStyle(Theme.muted)
                .frame(minWidth: 12, minHeight: 20)
                .rotationEffect(.degrees(expanded ? 90 : 0))
                .accessibilityHidden(true)
            Image(systemName: mark.symbol)
                .workFont(.icon)
                .foregroundStyle(mark.tint)
                .frame(minWidth: 16, minHeight: 20)
                .accessibilityHidden(true)  // the row label names the result

            VStack(alignment: .leading, spacing: Space.s) {
                HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                    Text(row.resultLabel)
                        .workFont(.dataSmallSemibold)
                        .foregroundStyle(mark.tint)
                        .fixedSize(horizontal: true, vertical: false)
                    Text(verbatim: title)
                        .workFont(.body)
                        .foregroundStyle(Theme.ink)
                        .lineLimit(expanded ? nil : 2)
                        .truncationMode(.tail)
                        .fixedSize(horizontal: false, vertical: true)
                        .layoutPriority(1)
                }

                if hasCollapsedMetadata {
                    VStack(alignment: .leading, spacing: Space.xs) {
                        if showsScope, let scope = row.scope {
                            HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                                Text("Scope")
                                    .workFont(.dataSmallSemibold)
                                    .foregroundStyle(Theme.muted)
                                Text(verbatim: scope)
                                    .workFont(.dataSmall)
                                    .foregroundStyle(Theme.muted)
                                    .lineLimit(1)
                                    .truncationMode(.middle)
                                    .help(scope)
                            }
                        }
                        ViewThatFits(in: .horizontal) {
                            HStack(alignment: .center, spacing: Space.s) {
                                collapsedMetadataItems
                            }
                            VStack(alignment: .leading, spacing: Space.xs) {
                                collapsedMetadataItems
                            }
                        }
                    }
                }
            }
        }
        .padding(.vertical, Space.m)
        .contentShape(Rectangle())
        .help(title)
    }

    private var hasCollapsedMetadata: Bool {
        (showsScope && row.scope != nil)
            || row.collapsedExitText != nil
            || row.sourceLabel != nil
            || check.superseded == true
            || (check.finding?.state != nil && check.finding?.state != "open")
    }

    @ViewBuilder
    private var collapsedMetadataItems: some View {
        if let exitText = row.collapsedExitText {
            Text(exitText)
                .workFont(.dataSmallSemibold)
                .foregroundStyle(mark.tint)
        }
        if let sourceLabel = row.sourceLabel {
            ProvenanceChip(text: sourceLabel)
                .lineLimit(1)
                .truncationMode(.middle)
                .help(sourceLabel)
        }
        if check.superseded == true {
            // The definition is keyboard-reachable in the expanded detail
            // (ContextHelp), never hover-only on this chip (C75).
            Chip(text: "superseded", tint: Theme.muted)
        }
        if let state = check.finding?.state, state != "open" {
            Chip(text: state, tint: Theme.muted)
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                expanded.toggle()
            } label: {
                headerLine
            }
            .buttonStyle(SurfaceButtonStyle(focusInset: 2))
            .accessibilityLabel(title)
            .accessibilityValue(row.accessibilityValue(isExpanded: expanded))
            .accessibilityHint(expanded ? "Hides full check details" : "Shows full check details")
            .accessibilityIdentifier(row.accessibilityIdentifier)

            if expanded {
                expandedBody
                    .padding(.leading, 12 + Space.s + 16 + Space.s)
                    .padding(.bottom, Space.m)
                    .transition(.opacity)
            }
        }
        .animation(reduceMotion ? nil : Motion.contentUpdate, value: expanded)
    }

    /// One Artifact row: url, else ref, else path; a withheld pointer is a
    /// named state, never silently dropped (C69).
    private var artifactText: String? {
        if let url = PayloadAbsence.text(check.artifactUrl) { return url }
        if let ref = PayloadAbsence.text(check.artifactRef) { return ref }
        if let path = PayloadAbsence.text(check.artifactPath) { return path }
        if check.artifactUrlRedacted == true || check.artifactPathRedacted == true {
            return PayloadAbsence.text(check.artifactUrlStateText) ?? PayloadAbsence.text(check.artifactPathStateText)
                ?? PayloadAbsence.artifact
        }
        return nil
    }

    @ViewBuilder
    private var expandedBody: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            CheckDetailField(label: "Check name") {
                Text(verbatim: title)
                    .workFont(.caption)
                    .foregroundStyle(Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            CheckDetailField(label: "Run") {
                Text(row.runDetailText)
                    .workFont(.dataSmall)
                    .foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            CheckDetailField(label: "Revision") {
                // `at <sha> · <branch> · uncommitted changes`, or its named
                // absence (C11).
                Text(verbatim: check.revisionText)
                    .workFont(.dataSmall).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            if check.superseded == true {
                CheckDetailField(label: "History") {
                    HStack(alignment: .center, spacing: Space.xs) {
                        Chip(text: "superseded", tint: Theme.muted)
                        // The reducer's definition (`superseded_definition`);
                        // Swift keeps no copy of the sentence.
                        if let meaning = PayloadAbsence.text(check.supersededDefinition) {
                            ContextHelp(
                                title: "Superseded check",
                                message: meaning,
                                identifier: "receipt.check.superseded-help"
                            )
                        }
                    }
                }
            }
            if let note = PayloadAbsence.text(check.noteText) {
                // The reducer's named disagreement between the recorded result
                // and the exit code (display only; the result is not re-graded).
                CheckDetailField(label: "Result") {
                    Text(verbatim: note)
                        .workFont(.dataSmall).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
            }
            if let sourceLabel = row.sourceLabel {
                CheckDetailField(label: "Source") {
                    Text(verbatim: sourceLabel)
                        .workFont(.dataSmall).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
            }
            // The summary shows once, as the body — never a repeat of the title.
            if let summary = PayloadAbsence.text(check.summary), summary != title {
                CheckDetailField(label: "Summary") {
                    // verbatim: agent/hook-authored text, never markdown.
                    Text(verbatim: summary)
                        .workFont(.caption).foregroundStyle(Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
            }
            if let scope = row.scope {
                CheckDetailField(label: "Scope") {
                    Text(verbatim: scope)
                        .workFont(.dataSmall).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
            }
            if let at = check.at, let ago = agoText(at) {
                CheckDetailField(label: "Recorded") {
                    Text(ago).workFont(.dataSmall).foregroundStyle(Theme.muted)
                }
            }
            if let files = check.files, !files.isEmpty {
                CheckDetailField(label: "Files") {
                    ScrollContentStack(alignment: .leading, spacing: Space.xs) {
                        ForEach(Array(files.enumerated()), id: \.offset) { _, path in
                            Text(verbatim: path)
                                .workFont(.dataSmall).foregroundStyle(Theme.muted)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .textSelection(.enabled)
                }
            }
            if let commandState = PayloadAbsence.text(check.commandStateText) {
                CheckDetailField(label: "Command") {
                    // The reducer's sentence for what happened to the command
                    // text (C07); hidden when it sent none.
                    Text(verbatim: commandState)
                        .workFont(.dataSmall).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if let artifact = artifactText {
                CheckDetailField(label: "Artifact") {
                    Text(verbatim: artifact)
                        .workFont(.dataSmall).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
            }
            if check.summary == nil, check.at == nil, (check.files ?? []).isEmpty,
               PayloadAbsence.text(check.commandStateText) == nil, artifactText == nil {
                Text("No additional detail was recorded for this check.")
                    .workFont(.dataSmall).foregroundStyle(Theme.muted)
            }
            // A surfaced failing check carries its human attention handle:
            // review/resolve it here instead of leaving the red forever.
            if let finding = check.finding {
                if let state = finding.state, state != "open" {
                    Text(verbatim: "marked \(state) by you"
                         + (finding.note.map { " — \($0)" } ?? ""))
                        .workFont(.dataSmall).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let digest = finding.targetDigest {
                    // No per-check effect sentence rides the check row; the
                    // attention callout carries the reducer's effects.
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

private struct CheckDetailField<Content: View>: View {
    let label: String
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            CapsLabel(text: label)
            content
        }
    }
}

// MARK: - Gaps

// MARK: - Evidence sources

/// The receipt's evidence sources: which source kinds are present on this
/// record, each with the daemon's legend sentence. When no independent
/// verifier (CI) evidence exists, that is stated as a fact — never a meter.
