import SwiftUI

/// The selected range's own totals for every agent and every model — the
/// answer to "what did each agent and each model cost in this range" without
/// opening a date. It renders the range's `by_client` / `by_model` buckets
/// directly (the server keeps them strictly scoped to the requested range),
/// never a re-sum of the period buckets, and it leaves the per-day chart and
/// the date table below it untouched.
struct UsageRangeSummary: View {
    let usage: UsageSummary
    let days: Int

    private var presentation: UsageRangePresentation { UsageRangePresentation(days: days) }
    private var agents: [UsageRangeLedger.Row] { UsageRangeLedger.agentRows(in: usage) }
    private var models: [UsageRangeLedger.Row] { UsageRangeLedger.modelRows(in: usage) }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                Text(presentation.title)
                    .workFont(.titleSection).tracking(Type.titleSectionTracking)
                    .foregroundStyle(Theme.ink)
                Text(presentation.caption).workFont(.caption).foregroundStyle(Theme.muted)
                ContextHelp(
                    title: presentation.helpTitle,
                    message: presentation.helpMessage,
                    identifier: "usage.range.help"
                )
                Spacer()
            }
            Text(presentation.attributionNote)
                .workFont(.caption).foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)

            if agents.isEmpty && models.isEmpty {
                Card {
                    Text(presentation.emptyText)
                        .workFont(.body).foregroundStyle(Theme.muted)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .accessibilityIdentifier("usage.range.empty")
            } else {
                UsageRangeTable(
                    title: presentation.agentTitle,
                    nameHeader: "Agent",
                    rows: agents,
                    emptyText: presentation.agentEmptyText,
                    identifier: "usage.range.agents"
                )
                UsageRangeTable(
                    title: presentation.modelTitle,
                    nameHeader: "Model",
                    rows: models,
                    emptyText: presentation.modelEmptyText,
                    identifier: "usage.range.models"
                )
            }
        }
    }
}

/// The range block's copy. Kept out of the view so the two tables' vocabulary
/// (agent, model, range totals) has one definition a test can assert.
struct UsageRangePresentation {
    let days: Int

    var title: String { "This range" }
    var caption: String { "last \(days) days · by agent and by model" }
    var agentTitle: String { "By agent" }
    var modelTitle: String { "By model" }
    var emptyText: String { "No agent or model usage reported in this range." }
    var agentEmptyText: String { "No agent usage reported in this range." }
    var modelEmptyText: String { "No model attribution reported in this range." }
    var helpTitle: String { "About these range totals" }

    /// The one fact a reader could otherwise read as a contradiction: the
    /// range's own totals and the dated rows describe the same records, but a
    /// session spanning days sits on one activity date instead of a split.
    var attributionNote: String {
        "Counted from the same saved session rows as the date table below: every row belongs to one recorded activity date, so a multi-day session sits on a single day rather than splitting across days."
    }

    var helpMessage: String {
        """
        Every saved session row in the selected range counts once here, at its recorded activity date. The date table below shows the same rows one day at a time, so a multi-day session is one day's row rather than a split series.

        Cost grammar: $ complete reported or billed · ≈$ estimate · ~$ known partial subtotal · unpriced when no amount is available. Hover a cost for its basis.

        Token columns are fixed: Fresh (non-cached input + output), Cache (reads + writes) and Total (fresh + cache). The chart's Fresh/All choice changes the chart, not these columns. — names an unreported category; * marks a recorded subtotal whose coverage is incomplete.
        """
    }
}

/// Rows for the range tables: the payload's own buckets, ranked by recorded
/// tokens, with identity kept per bucket so the same model under two agents
/// never merges into one row.
enum UsageRangeLedger {
    struct Row: Identifiable {
        let id: String
        let name: String
        /// The model row's agent and provider; nil for an agent row.
        let detail: String?
        let bucket: UsageBucket
    }

    static func agentRows(in usage: UsageSummary) -> [Row] {
        var seen: Set<String> = []
        let rows = usage.byClient.compactMap { bucket -> Row? in
            // A duplicate client entry keeps the first, exactly as the date
            // table's grouping does, so no row is counted twice.
            let name = bucket.client ?? "Unattributed agent"
            guard seen.insert(name).inserted else { return nil }
            return Row(id: "agent:\(name)", name: name, detail: nil, bucket: bucket)
        }
        return ranked(rows)
    }

    static func modelRows(in usage: UsageSummary) -> [Row] {
        var seen: Set<String> = []
        let rows = usage.byModel.compactMap { bucket -> Row? in
            guard seen.insert(bucket.id).inserted else { return nil }
            return Row(
                id: "model:\(bucket.id)",
                name: bucket.model ?? "Unattributed model",
                detail: identity(bucket),
                bucket: bucket
            )
        }
        return ranked(rows)
    }

    private static func identity(_ bucket: UsageBucket) -> String? {
        let parts = [bucket.client, bucket.provider].compactMap { $0 }.filter { !$0.isEmpty }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    /// Most recorded tokens first — the order the cube itself returns — with a
    /// bucket that has no recorded total (held or unreported) ranked last
    /// instead of reading as zero. Name then id break ties, so the order is a
    /// total order and equal totals cannot shuffle between renders.
    private static func ranked(_ rows: [Row]) -> [Row] {
        rows.sorted { left, right in
            let lhs = UsageTokenColumn.total.metric(left.bucket).value ?? -1
            let rhs = UsageTokenColumn.total.metric(right.bucket).value ?? -1
            if lhs != rhs { return lhs > rhs }
            let order = left.name.localizedStandardCompare(right.name)
            if order != .orderedSame { return order == .orderedAscending }
            return left.id < right.id
        }
    }
}

/// One range table: NAME · SESSIONS · FRESH · CACHE · TOTAL · COST. Tokens use
/// the page's `UsageTokenColumn` metrics (an unreported category stays named,
/// a partial subtotal keeps its marker) and the cost keeps the shared grammar
/// with its basis in the hover text.
private struct UsageRangeTable: View {
    let title: String
    let nameHeader: String
    let rows: [UsageRangeLedger.Row]
    let emptyText: String
    let identifier: String

    private let columns: [UsageTokenColumn] = [.fresh, .cache, .total]
    private let sessionsW: CGFloat = 76
    private let tokenW: CGFloat = 86
    private let costW: CGFloat = 90

    var body: some View {
        Card(padding: 0) {
            VStack(spacing: 0) {
                HStack(spacing: Space.s) {
                    Text(title).workFont(.titleCard).foregroundStyle(Theme.ink)
                    Spacer()
                }
                .padding(.horizontal, Space.xl)
                .frame(height: 52)

                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.horizontal, Space.xl)

                header.padding(.horizontal, Space.xl).frame(height: Metrics.rowHeader)

                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.horizontal, Space.xl)

                if rows.isEmpty {
                    Text(emptyText)
                        .workFont(.body).foregroundStyle(Theme.muted)
                        .padding(Space.xl)
                        .frame(maxWidth: .infinity, alignment: .leading)
                } else {
                    ScrollContentStack(spacing: 0) { populatedRows }
                }
            }
        }
        .accessibilityIdentifier(identifier)
    }

    private var header: some View {
        HStack(spacing: Space.m) {
            CapsLabel(text: nameHeader).frame(maxWidth: .infinity, alignment: .leading)
            CapsLabel(text: "Sessions").frame(width: sessionsW, alignment: .trailing)
            ForEach(columns, id: \.self) { column in
                CapsLabel(text: column.rawValue).frame(width: tokenW, alignment: .trailing)
            }
            CapsLabel(text: "Cost").frame(width: costW, alignment: .trailing)
        }
        .accessibilityHidden(true)
    }

    @ViewBuilder
    private var populatedRows: some View {
        ForEach(Array(rows.enumerated()), id: \.element.id) { index, row in
            if index > 0 {
                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.horizontal, Space.xl)
            }
            rowView(row)
        }
    }

    private func rowView(_ row: UsageRangeLedger.Row) -> some View {
        HStack(spacing: Space.m) {
            VStack(alignment: .leading, spacing: 2) {
                Text(row.name)
                    .workFont(.rowLabel).foregroundStyle(Theme.ink)
                    .lineLimit(1).truncationMode(.middle)
                    .help(row.name)
                if let detail = row.detail {
                    Text(detail).workFont(.dataSmall).foregroundStyle(Theme.muted)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Text(row.bucket.sessions.map(String.init) ?? "not reported")
                .workFont(.dataSmall)
                .foregroundStyle(row.bucket.sessions == nil ? Theme.muted : Theme.ink)
                .frame(width: sessionsW, alignment: .trailing)
            ForEach(columns, id: \.self) { column in
                tokenCell(column, row.bucket)
            }
            Text(row.bucket.costText == "—" ? "Unpriced" : row.bucket.costText)
                .workFont(.dataSmall)
                .foregroundStyle(row.bucket.costText == "—" ? Theme.muted : Theme.ink)
                .frame(width: costW, alignment: .trailing)
                .help(row.bucket.costConfidenceLabel ?? "Cost basis not reported")
        }
        .padding(.horizontal, Space.xl)
        .frame(minHeight: Metrics.rowTable)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityLabel(row))
    }

    private func tokenCell(_ column: UsageTokenColumn, _ bucket: UsageBucket) -> some View {
        let metric = column.metric(bucket)
        return Text(metric.text)
            .workFont(.dataSmall)
            .foregroundStyle(metric.value == nil ? Theme.muted : Theme.ink)
            .frame(width: tokenW, alignment: .trailing)
            .help(column == .fresh
                  ? "\(metric.exactText). Non-cached input: \(bucket.inputTokens.map { $0.formatted() } ?? "not reported"); output: \(bucket.outputTokens.map { $0.formatted() } ?? "not reported")."
                  : metric.exactText)
            .accessibilityLabel("\(column.rawValue): \(metric.exactText)")
    }

    private func accessibilityLabel(_ row: UsageRangeLedger.Row) -> String {
        let cache = UsageTokenColumn.cache.metric(row.bucket)
        return [
            row.name,
            row.detail,
            row.bucket.sessions.map { Fmt.count($0, "session") } ?? "sessions not reported",
            UsageTokenColumn.fresh.metric(row.bucket).value.map { "\($0) fresh tokens" }
                ?? "fresh tokens not reported",
            cache.value.map { "\($0) cached tokens" } ?? "cached tokens not reported",
            cache.partial ? "cache coverage incomplete" : nil,
            UsageTokenColumn.total.metric(row.bucket).value.map { "\($0) tokens in total" }
                ?? "total tokens not reported",
            row.bucket.costText == "—" ? "cost unpriced" : row.bucket.costText,
            Fmt.costConfidenceLabel(row.bucket.costConfidence),
        ].compactMap { $0 }.joined(separator: ", ")
    }
}
