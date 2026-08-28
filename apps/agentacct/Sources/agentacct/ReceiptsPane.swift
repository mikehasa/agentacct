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

enum ReceiptActionIntegrity: Equatable {
    case unavailable
    case captureUnknown
    case totalOnly
    case exact
    case totalUnavailable
    case unrecognizedCategories
    case mismatch
    case invalid
}

/// One same-unit tool-call category in the Actions dimension. Labels describe
/// only the observed category; they never imply success, effect, importance,
/// or risk. Unknown future categories stay visible as one bounded aggregate.
struct ReceiptActionMetric: Equatable, Identifiable {
    let key: String
    let label: String
    let detail: String
    let count: Int

    var id: String { key }
}

struct ReceiptActionSynopsis: Equatable {
    let integrity: ReceiptActionIntegrity
    let metrics: [ReceiptActionMetric]
    let headline: String
    let integrityDetail: String?
    let storedTotal: Int?
    let categorizedTotal: Int?
    let shareDenominator: Int?
    let captureBoundary: String?

    /// A quantitative distribution is honest only when the displayed types
    /// reconcile to a positive stored total. Other integrity states keep the
    /// exact counts but omit proportions rather than drawing a misleading
    /// chart against a missing or conflicting denominator.
    var canShowDistribution: Bool {
        guard let shareDenominator, shareDenominator > 0, !metrics.isEmpty else {
            return false
        }
        return integrity == .exact
    }
}

struct ReceiptActionKPI: Equatable {
    let value: String?
    let qualifier: String?
    let absent: String?
}

private struct ReceiptActionMetricDerivation {
    let metrics: [ReceiptActionMetric]
    let overflowed: Bool
    let invalidCategoryCount: Int
    let unrecognizedCategoryCount: Int
}

private func deriveReceiptActionMetrics(_ counts: [String: Int]?) -> ReceiptActionMetricDerivation {
    let known: [(key: String, label: String, detail: String)] = [
        ("read", "Read", "File or context read tool calls"),
        ("edit", "Edit", "Edit or write tool calls"),
        ("execute", "Execute", "Command or process tool calls"),
        ("search", "Search", "File or text search tool calls"),
        ("network", "Network", "Network access tool calls"),
        ("agent", "Agent", "Agent coordination tool calls"),
        ("plan", "Plan", "Planning tool calls"),
        ("mcp", "Connected tools", "Connected-tool calls"),
        ("other", "Other", "Tool calls outside named categories"),
    ]
    let knownKeys = Set(known.map(\.key))
    let familiar = known.compactMap { item -> ReceiptActionMetric? in
        guard let count = counts?[item.key], count > 0 else { return nil }
        return ReceiptActionMetric(
            key: item.key,
            label: item.label,
            detail: item.detail,
            count: count
        )
    }
    var invalidCategoryCount = 0
    var unrecognizedCategoryCount = 0
    var unknownTotal = 0
    var overflowed = false
    for (key, count) in counts ?? [:] {
        if count < 0 || key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            invalidCategoryCount += 1
            continue
        }
        guard count > 0, !knownKeys.contains(key) else { continue }
        unrecognizedCategoryCount += 1
        guard !overflowed else { continue }
        let result = unknownTotal.addingReportingOverflow(count)
        if result.overflow {
            overflowed = true
        } else {
            unknownTotal = result.partialValue
        }
    }
    guard unrecognizedCategoryCount > 0, !overflowed else {
        return ReceiptActionMetricDerivation(
            metrics: familiar,
            overflowed: overflowed,
            invalidCategoryCount: invalidCategoryCount,
            unrecognizedCategoryCount: unrecognizedCategoryCount
        )
    }
    let categoryWord = unrecognizedCategoryCount == 1 ? "category" : "categories"
    return ReceiptActionMetricDerivation(
        metrics: familiar + [
            ReceiptActionMetric(
                key: "__unknown_types__",
                label: "Unrecognized types",
                detail: "\(unrecognizedCategoryCount) unrecognized \(categoryWord)",
                count: unknownTotal
            )
        ],
        overflowed: false,
        invalidCategoryCount: invalidCategoryCount,
        unrecognizedCategoryCount: unrecognizedCategoryCount
    )
}

func receiptActionMetrics(_ counts: [String: Int]?) -> [ReceiptActionMetric] {
    deriveReceiptActionMetrics(counts).metrics
}

func receiptActionSynopsis(
    counts: [String: Int]?,
    storedTotal: Int?
) -> ReceiptActionSynopsis {
    let derivation = deriveReceiptActionMetrics(counts)
    let metrics = derivation.metrics
    var categorizedTotal = 0
    var categorizedOverflow = derivation.overflowed
    if !categorizedOverflow {
        for metric in metrics {
            let result = categorizedTotal.addingReportingOverflow(metric.count)
            if result.overflow {
                categorizedOverflow = true
                break
            }
            categorizedTotal = result.partialValue
        }
    }
    let invalidCategoryCount = derivation.invalidCategoryCount
    let invalidStoredTotal = (storedTotal ?? 0) < 0
    let captureBoundary = "No ordered action ledger; captured tool-call counts cannot be linked to results or timing."

    if invalidCategoryCount > 0 || invalidStoredTotal || categorizedOverflow {
        var details: [String] = []
        if invalidCategoryCount > 0 {
            details.append(
                "\(invalidCategoryCount) invalid \(invalidCategoryCount == 1 ? "category was" : "categories were") omitted"
            )
        }
        if invalidStoredTotal { details.append("the stored total is invalid") }
        if categorizedOverflow {
            details.append("the categorized tool-call sum overflowed")
        } else if categorizedTotal > 0 {
            details.append("\(categorizedTotal) valid \(categorizedTotal == 1 ? "tool call remains" : "tool calls remain") categorized")
        }
        if let storedTotal, storedTotal >= 0 { details.append("stored total is \(storedTotal)") }
        return ReceiptActionSynopsis(
            integrity: .invalid,
            metrics: metrics,
            headline: "Tool-call data incomplete",
            integrityDetail: details.joined(separator: " · "),
            storedTotal: storedTotal,
            categorizedTotal: categorizedOverflow ? nil : categorizedTotal,
            shareDenominator: nil,
            captureBoundary: categorizedTotal > 0 || (storedTotal ?? 0) > 0 || categorizedOverflow
                ? captureBoundary : nil
        )
    }

    guard let storedTotal else {
        if categorizedTotal > 0 {
            let hasUnrecognizedCategories = derivation.unrecognizedCategoryCount > 0
            return ReceiptActionSynopsis(
                integrity: hasUnrecognizedCategories ? .unrecognizedCategories : .totalUnavailable,
                metrics: metrics,
                headline: "\(categorizedTotal) categorized \(categorizedTotal == 1 ? "tool call" : "tool calls")",
                integrityDetail: hasUnrecognizedCategories
                    ? "\(derivation.unrecognizedCategoryCount) unrecognized tool-call \(derivation.unrecognizedCategoryCount == 1 ? "type" : "types") · stored total unavailable"
                    : "Stored tool-call total unavailable",
                storedTotal: nil,
                categorizedTotal: categorizedTotal,
                shareDenominator: nil,
                captureBoundary: captureBoundary
            )
        }
        return ReceiptActionSynopsis(
            integrity: counts == nil ? .unavailable : .totalUnavailable,
            metrics: [],
            headline: counts == nil ? "not instrumented" : "stored total unavailable",
            integrityDetail: counts == nil ? nil : "No categorized tool-call counts",
            storedTotal: nil,
            categorizedTotal: 0,
            shareDenominator: nil,
            captureBoundary: nil
        )
    }

    if storedTotal == 0, categorizedTotal == 0 {
        return ReceiptActionSynopsis(
            integrity: .captureUnknown,
            metrics: [],
            headline: "No captured tool calls",
            integrityDetail: "Capture coverage unknown",
            storedTotal: 0,
            categorizedTotal: 0,
            shareDenominator: nil,
            captureBoundary: nil
        )
    }

    if storedTotal > 0, counts == nil {
        return ReceiptActionSynopsis(
            integrity: .totalOnly,
            metrics: [],
            headline: "\(storedTotal) \(storedTotal == 1 ? "tool call" : "tool calls") in stored total",
            integrityDetail: "Tool-call type breakdown unavailable",
            storedTotal: storedTotal,
            categorizedTotal: 0,
            shareDenominator: nil,
            captureBoundary: captureBoundary
        )
    }

    if storedTotal != categorizedTotal {
        var detail = "category counts sum to \(categorizedTotal) · stored total is \(storedTotal)"
        if derivation.unrecognizedCategoryCount > 0 {
            detail += " · \(derivation.unrecognizedCategoryCount) unrecognized tool-call \(derivation.unrecognizedCategoryCount == 1 ? "type" : "types")"
        }
        return ReceiptActionSynopsis(
            integrity: .mismatch,
            metrics: metrics,
            headline: "Tool-call totals conflict",
            integrityDetail: detail,
            storedTotal: storedTotal,
            categorizedTotal: categorizedTotal,
            shareDenominator: nil,
            captureBoundary: captureBoundary
        )
    }
    if derivation.unrecognizedCategoryCount > 0 {
        return ReceiptActionSynopsis(
            integrity: .unrecognizedCategories,
            metrics: metrics,
            headline: "\(storedTotal) \(storedTotal == 1 ? "tool call" : "tool calls") captured",
            integrityDetail: "\(derivation.unrecognizedCategoryCount) unrecognized tool-call \(derivation.unrecognizedCategoryCount == 1 ? "type was" : "types were") aggregated · update the app to interpret \(derivation.unrecognizedCategoryCount == 1 ? "it" : "them")",
            storedTotal: storedTotal,
            categorizedTotal: categorizedTotal,
            shareDenominator: nil,
            captureBoundary: captureBoundary
        )
    }
    return ReceiptActionSynopsis(
        integrity: .exact,
        metrics: metrics,
        headline: "\(storedTotal) \(storedTotal == 1 ? "tool call" : "tool calls") captured",
        integrityDetail: nil,
        storedTotal: storedTotal,
        categorizedTotal: categorizedTotal,
        shareDenominator: storedTotal,
        captureBoundary: captureBoundary
    )
}

func receiptActionKPI(_ synopsis: ReceiptActionSynopsis) -> ReceiptActionKPI {
    switch synopsis.integrity {
    case .captureUnknown:
        return ReceiptActionKPI(value: nil, qualifier: nil, absent: "capture unknown")
    case .totalOnly, .exact:
        return ReceiptActionKPI(
            value: synopsis.storedTotal.map(String.init),
            qualifier: "tool calls",
            absent: nil
        )
    case .totalUnavailable:
        if let categorized = synopsis.categorizedTotal, categorized > 0 {
            return ReceiptActionKPI(value: "\(categorized)", qualifier: "categorized calls", absent: nil)
        }
        return ReceiptActionKPI(value: nil, qualifier: nil, absent: "stored total unavailable")
    case .unrecognizedCategories:
        if let stored = synopsis.storedTotal {
            return ReceiptActionKPI(value: "\(stored)", qualifier: "tool calls · types changed", absent: nil)
        }
        if let categorized = synopsis.categorizedTotal, categorized > 0 {
            return ReceiptActionKPI(value: "\(categorized)", qualifier: "categorized calls · types changed", absent: nil)
        }
        return ReceiptActionKPI(value: nil, qualifier: nil, absent: "tool-call types changed")
    case .mismatch:
        return ReceiptActionKPI(value: nil, qualifier: nil, absent: "tool-call totals conflict")
    case .invalid:
        return ReceiptActionKPI(value: nil, qualifier: nil, absent: "tool-call data incomplete")
    case .unavailable:
        return ReceiptActionKPI(value: nil, qualifier: nil, absent: "not instrumented")
    }
}

func receiptActionScope(relatedPathCount: Int?) -> String {
    guard let pathCount = relatedPathCount, pathCount >= 0 else { return "" }
    return "\(pathCount) unique \(pathCount == 1 ? "path" : "paths") from recorded work, machine checks, or captured edit tool calls"
}

func receiptActionSourceText(_ sources: [String]?) -> String {
    var labels: [String] = []
    var seen = Set<String>()
    for source in sources ?? [] {
        let label: String
        switch source {
        case "", "none": continue
        case "mcp": label = "MCP"
        case "transcript_scan": label = "Transcript scan"
        case "client_log": label = "Client log"
        case "agent_report": label = "Agent report"
        case "pricing_table": label = "Pricing table"
        case "ci": label = "CI"
        case "hook": label = "Client hook"
        default:
            let words = source.replacingOccurrences(of: "_", with: " ")
            label = words.prefix(1).uppercased() + words.dropFirst()
        }
        if seen.insert(label).inserted { labels.append(label) }
    }
    return labels.joined(separator: ", ")
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

        let actionKPI = receiptActionKPI(
            receiptActionSynopsis(
                counts: actions.toolCategoryCounts,
                storedTotal: actions.toolCategoryTotal
            )
        )
        let actionCell = Cell(
            id: "actions",
            label: "Actions",
            value: actionKPI.value,
            qualifier: actionKPI.qualifier,
            absent: actionKPI.absent
        )

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

/// The aggregate-only Actions view. It is intentionally static: current
/// receipts do not contain canonical per-action rows, so no metric, source, or
/// disclosure may look clickable. Exact text remains primary and the layout has
/// one deterministic two-column-to-one-column transition.
struct ReceiptActionsDigest: View {
    let synopsis: ReceiptActionSynopsis
    let relatedPathCount: Int?
    let provenance: [String]?
    let gaps: [String]?

    // The app's fixed type ramp keeps dense dashboard geometry stable. This
    // focused digest still has to honor accessibility text sizes, so its four
    // existing roles scale relative to their semantic text styles without
    // changing the surrounding receipt ledger.
    @ScaledMetric(relativeTo: .body) private var bodyTypeSize: CGFloat = 14
    @ScaledMetric(relativeTo: .caption) private var captionTypeSize: CGFloat = 12
    @ScaledMetric(relativeTo: .caption) private var captionIconSize: CGFloat = 10

    private var scope: String { receiptActionScope(relatedPathCount: relatedPathCount) }
    private var sourceText: String { receiptActionSourceText(provenance) }
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
        .padding(.vertical, Space.m)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Actions")
        .accessibilityIdentifier("receipt.actions.summary")
    }

    private var actionsLabel: some View {
        Text("Actions")
            .font(rowLabelFont)
            .foregroundStyle(Theme.ink)
            .accessibilityHidden(true)
    }

    private var digestContent: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            Text(synopsis.headline)
                .font(bodyFont)
                .foregroundStyle(synopsis.integrity == .unavailable ? Theme.muted : Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
            if let detail = synopsis.integrityDetail {
                Text(detail)
                    .font(captionFont)
                    .foregroundStyle(integrityTone)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !synopsis.metrics.isEmpty {
                if synopsis.canShowDistribution {
                    actionDistribution
                } else {
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
            if !scope.isEmpty {
                metadataLine(label: "Related paths", value: scope)
            }
            if !sourceText.isEmpty {
                metadataLine(label: "Action sources", value: sourceText)
            }
            if let boundary = synopsis.captureBoundary {
                metadataLine(label: "Detail", value: boundary)
            }
            ForEach(Array((gaps ?? []).enumerated()), id: \.offset) { _, gap in
                noticeLine(prefix: "Evidence gap", text: gap, tone: Theme.amber)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var actionDistribution: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                Text("Tool calls by type")
                    .font(captionSemiboldFont)
                    .foregroundStyle(Theme.ink)
                Spacer(minLength: Space.s)
                Text("Shared scale")
                    .font(dataSmallFont)
                    .foregroundStyle(Theme.muted)
            }
            .padding(.top, Space.xs)
            .accessibilityHidden(true)

            Text("Counts describe captured tool calls, not progress or success.")
                .font(captionFont)
                .foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)

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
            Text(metric.detail)
                .font(captionFont)
                .foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
            GeometryReader { proxy in
                Rectangle()
                    .fill(Theme.accent)
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
                "\(metric.count) of \($0). \(metric.detail)"
            } ?? "\(metric.count). \(metric.detail)"
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
                .font(.system(size: captionIconSize, weight: .semibold))
                .foregroundStyle(tone)
                .frame(width: captionIconSize + 2, alignment: .center)
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

    // V1 carries aggregates, independently collected path scope, and row-wide
    // provenance—not canonical action rows. Keep the daily receipt bounded to
    // facts that can be related honestly.
    private var actionsRow: some View {
        let dim = receipt.dimensions.actions
        return ReceiptActionsDigest(
            synopsis: receiptActionSynopsis(
                counts: dim.toolCategoryCounts,
                storedTotal: dim.toolCategoryTotal
            ),
            relatedPathCount: dim.touchedFileCount,
            provenance: dim.provenance,
            gaps: dim.gaps
        )
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
        // the merged Usage & limits pane, never a number here).
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
