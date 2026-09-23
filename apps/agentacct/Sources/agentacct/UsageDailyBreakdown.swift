import SwiftUI

/// A recorded subtotal stays useful without claiming unreported categories are zero.
struct UsageTokenMetric: Equatable {
    let value: Int?
    var partial = false
    var text: String { value.map { UsageTotals.compact($0) + (partial ? "*" : "") } ?? "—" }
    var exactText: String { value.map { $0.formatted() + (partial ? " recorded; coverage is incomplete" : " tokens") } ?? "Not reported" }
}

enum UsageTokenColumn: String, CaseIterable {
    case fresh = "Fresh"
    case cache = "Cache"
    case cacheRead = "Cache read"
    case cacheWrite = "Cache write"
    case total = "Total"

    func metric(_ bucket: UsageBucket?) -> UsageTokenMetric {
        guard let bucket else { return .init(value: nil) }
        if bucket.rows == 0 { return .init(value: 0) }
        if bucket.usageAvailability == "held" { return .init(value: nil) }
        let partial = bucket.usageAvailability == "partial"
        func cache(_ value: Int?, reporting: String?) -> UsageTokenMetric {
            guard let value, value >= 0 else { return .init(value: nil) }
            if reporting == "reported" { return .init(value: value, partial: partial) }
            guard value > 0 else { return .init(value: nil) }
            return .init(value: value, partial: true)
        }
        switch self {
        case .fresh:
            return .init(value: bucket.freshTokens.flatMap { $0 >= 0 ? $0 : nil }, partial: partial)
        case .total:
            return .init(value: bucket.totalTokensIncludingCached.flatMap { $0 >= 0 ? $0 : nil },
                         partial: partial || [bucket.cacheReadReporting, bucket.cacheCreationReporting].contains { $0 != nil && $0 != "reported" })
        case .cacheRead: return cache(bucket.cacheReadTokens, reporting: bucket.cacheReadReporting)
        case .cacheWrite: return cache(bucket.cacheCreationTokens, reporting: bucket.cacheCreationReporting)
        case .cache:
            let read = Self.cacheRead.metric(bucket), write = Self.cacheWrite.metric(bucket)
            guard read.value != nil || write.value != nil else { return .init(value: nil) }
            let sum = (read.value ?? 0).addingReportingOverflow(write.value ?? 0)
            return .init(value: sum.overflow ? nil : sum.partialValue,
                         partial: read.partial || write.partial || read.value == nil || write.value == nil)
        }
    }
}

struct UsageClientModelGroup: Identifiable {
    let client: String
    let totals: UsageBucket?
    let models: [UsageBucket]
    var id: String { client }
}

enum UsageDaySelection {
    static func latestActive(in usage: UsageSummary) -> String? {
        let periods = usage.byPeriod ?? []
        return periods.last(where: { ($0.totalTokensIncludingCached ?? $0.freshTokens ?? 0) > 0 || ($0.usage.rows ?? 0) > 0 })?.period
            ?? periods.last?.period
    }

    static func groups(in usage: UsageSummary, period: String?) -> [UsageClientModelGroup] {
        let selected = period.flatMap { key in usage.byPeriod?.first { $0.period == key } }
        // Do not silently substitute the entire range when a selected day has
        // no model attribution (including a response from an older daemon).
        let clients: [String: UsageBucket]
        let models: [UsageBucket]
        if period != nil {
            clients = selected?.byClient ?? [:]
            models = selected?.byModel ?? []
        } else {
            clients = Dictionary(usage.byClient.map { ($0.client ?? "Unattributed client", $0) }, uniquingKeysWith: { first, _ in first })
            models = usage.byModel
        }
        let names = Set(clients.keys).union(models.map { $0.client ?? "Unattributed client" })
        return names.sorted().map { client in
            .init(client: client, totals: clients[client], models: models.filter { ($0.client ?? "Unattributed client") == client }
                .sorted { ($0.model ?? "", $0.provider ?? "") < ($1.model ?? "", $1.provider ?? "") })
        }
    }
}

enum UsageHistoryMeasure: String, CaseIterable {
    case cost = "Cost"
    case tokens = "Tokens"

    func value(_ period: PeriodBucket, basis: UsageTokenBasis) -> Double? {
        if self == .cost { return period.usage.knownAdditiveCostUsd ?? period.estimatedCostUsd }
        return (basis == .fresh ? UsageTokenColumn.fresh : .total).metric(period.usage).value.map(Double.init)
    }
    func text(_ period: PeriodBucket, basis: UsageTokenBasis) -> String {
        if self == .cost { return period.costText == "—" ? "Unpriced" : period.costText }
        return (basis == .fresh ? UsageTokenColumn.fresh : .total).metric(period.usage).exactText
    }
}

struct UsageRecordedExplorer: View {
    let usage: UsageSummary
    let days: Int
    @Binding var tokenBasis: UsageTokenBasis
    @State private var selectedPeriod: String?
    @State private var chartMeasure: UsageHistoryMeasure = .tokens

    init(usage: UsageSummary, days: Int, tokenBasis: Binding<UsageTokenBasis>) {
        self.usage = usage
        self.days = days
        _tokenBasis = tokenBasis
        let pinned = SnapshotMode.enabled ? SnapshotMode.usageChartSelectedIndex : nil
        let periods = usage.byPeriod ?? []
        _selectedPeriod = State(initialValue: pinned.flatMap { periods.indices.contains($0) ? periods[$0].period : nil }
                                ?? UsageDaySelection.latestActive(in: usage))
    }

    private var periods: [PeriodBucket] { usage.byPeriod ?? [] }
    private var periodLabel: String { usage.filtersEcho?.granularity == "weekly" ? "Week" : usage.filtersEcho?.granularity == "daily" ? "Date" : "Period" }
    private var selected: PeriodBucket? { periods.first { $0.period == selectedPeriod } }
    private var groups: [UsageClientModelGroup] { UsageDaySelection.groups(in: usage, period: selectedPeriod) }
    private let dailyColumns: [UsageTokenColumn] = [.fresh, .cache, .total]
    private let detailColumns: [UsageTokenColumn] = [.fresh, .cacheRead, .cacheWrite, .total]

    var body: some View {
        VStack(alignment: .leading, spacing: Space.l) {
            summary
            if !periods.isEmpty {
                history
                dayTable
            }
            clientTable
            Text("— not reported · * recorded subtotal with incomplete coverage. Hover a number for its exact count.")
                .workFont(.caption).foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
        .onChange(of: days) { selectedPeriod = UsageDaySelection.latestActive(in: usage) }
        .onChange(of: periods.map(\.period)) {
            if let selectedPeriod, !periods.contains(where: { $0.period == selectedPeriod }) {
                self.selectedPeriod = UsageDaySelection.latestActive(in: usage)
            }
        }
    }

    private var summary: some View {
        StripRow(cells: [.fresh, .cache, .total].map { (column: UsageTokenColumn) in
            let value = column.metric(usage.totals)
            return .init(id: column.rawValue, label: column == .total ? "Total tokens" : column.rawValue + " tokens",
                         value: value.value == nil ? nil : value.text,
                         qualifier: column == .fresh ? "input + output" : column == .cache ? "reads + writes" : "fresh + cache",
                         absent: "not reported")
        } + [.init(id: "cost", label: "Recorded cost", value: usage.totals.flatMap { $0.costText == "—" ? nil : $0.costText },
                    qualifier: usage.totals?.costComplete == true ? (Fmt.costConfidenceLabel(usage.totals?.costConfidence) ?? "basis not reported") : "partial or unpriced",
                    absent: "unpriced")])
    }

    private var history: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            HStack {
                Text("History").workFont(.rowLabel).foregroundStyle(Theme.ink)
                Spacer()
                HStack(spacing: 2) {
                    ForEach(UsageHistoryMeasure.allCases, id: \.self) { measure in
                        Button { chartMeasure = measure } label: {
                            Text(measure.rawValue).workFont(.captionSemibold)
                                .foregroundStyle(chartMeasure == measure ? Theme.ink : Theme.muted)
                                .padding(.horizontal, Space.s).frame(minHeight: 26)
                                .background(chartMeasure == measure ? Theme.card : .clear, in: RoundedRectangle(cornerRadius: 5))
                        }
                        .buttonStyle(QuietButtonStyle(horizontalPadding: 0, verticalPadding: 0))
                        .accessibilityAddTraits(chartMeasure == measure ? .isSelected : [])
                        .accessibilityIdentifier("usage.history.measure.\(measure.rawValue.lowercased())")
                    }
                }.padding(3).background(Theme.tintNeutral, in: RoundedRectangle(cornerRadius: 7))
                if chartMeasure == .tokens {
                    UsageTokenBasisControl(selection: $tokenBasis)
                    ContextHelp(title: "How tokens are counted", message: UsageTokenBasis.explanation, identifier: "usage.tokens.help")
                }
            }
            let maxValue = max(periods.compactMap { chartMeasure.value($0, basis: tokenBasis) }.max() ?? 1, 1)
            HStack(alignment: .bottom, spacing: 3) {
                ForEach(Array(periods.enumerated()), id: \.offset) { index, period in
                    let value = chartMeasure.value(period, basis: tokenBasis)
                    Button { selectedPeriod = period.period } label: {
                        RoundedRectangle(cornerRadius: 2)
                            .fill(value == nil ? Theme.tintNeutral : selectedPeriod == period.period ? Theme.accent : Theme.chartBarDim)
                            .frame(height: value.map { max(2, 76 * $0 / maxValue) } ?? 3)
                            .frame(maxWidth: .infinity, maxHeight: 80, alignment: .bottom)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(TransparentButtonStyle(cornerRadius: 2))
                    .help("\(period.period ?? "Unknown date") · \(chartMeasure.text(period, basis: tokenBasis))")
                    .accessibilityLabel("\(period.period ?? "Unknown date"), \(chartMeasure.text(period, basis: tokenBasis))")
                    .accessibilityHint("Shows client and model usage for this date")
                    .accessibilityAddTraits(selectedPeriod == period.period ? .isSelected : [])
                    .accessibilityIdentifier("usage.history.day.\(index)")
                }
            }
            .frame(height: 80)
            HStack {
                Text(periods.first?.shortLabel ?? "")
                Spacer()
                Text("Select a bar or a date below")
                Spacer()
                Text(periods.last?.shortLabel ?? "")
            }.workFont(.dataSmall).foregroundStyle(Theme.muted)
        }
    }

    private var dayTable: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            HStack(alignment: .firstTextBaseline) {
                Text(usage.periodAttribution?.label ?? "Session totals by activity date").workFont(.rowLabel).foregroundStyle(Theme.ink)
                Spacer()
                Button("All \(days)d") { selectedPeriod = nil }
                    .buttonStyle(QuietButtonStyle())
                    .foregroundStyle(selectedPeriod == nil ? Theme.accent : Theme.muted)
                    .accessibilityAddTraits(selectedPeriod == nil ? .isSelected : [])
                    .accessibilityIdentifier("usage.days.all")
            }
            Text(usage.periodAttribution?.description ?? "Session totals are assigned to a recorded activity date; sessions spanning days are not split into daily spend.")
                .workFont(.caption).foregroundStyle(Theme.muted).fixedSize(horizontal: false, vertical: true)
            Card(padding: 0) {
                VStack(spacing: 0) {
                    tableHeader(name: periodLabel, columns: dailyColumns).padding(.horizontal, Space.m).padding(.vertical, Space.s)
                    Rectangle().fill(Theme.hairline).frame(height: 1)
                    if SnapshotMode.enabled && !SnapshotMode.interactiveFixture {
                        // ImageRenderer cannot paint native scroll-view contents.
                        // Show the same bounded row window for deterministic review.
                        let rows = Array(periods.reversed())
                        let selectedIndex = rows.firstIndex { $0.period == selectedPeriod } ?? 0
                        let start = min(max(0, selectedIndex - 1), max(0, rows.count - 4))
                        VStack(spacing: 0) {
                            ForEach(Array(rows.dropFirst(start).prefix(4).enumerated()), id: \.offset) { _, period in dayRow(period) }
                        }
                    } else {
                        ScrollViewReader { proxy in
                            ScrollView(.vertical) {
                                VStack(spacing: 0) {
                                    ForEach(Array(periods.reversed().enumerated()), id: \.offset) { _, period in dayRow(period) }
                                }
                            }
                            .frame(height: min(CGFloat(periods.count) * 34, 136))
                            .onChange(of: selectedPeriod) { if let selectedPeriod { withAnimation(Motion.contentUpdate) { proxy.scrollTo(selectedPeriod, anchor: .center) } } }
                        }
                    }
                }
            }
        }
    }

    private func dayRow(_ period: PeriodBucket) -> some View {
        Button { selectedPeriod = period.period } label: {
            HStack(spacing: Space.m) {
                HStack(spacing: Space.s) {
                    Image(systemName: selectedPeriod == period.period ? "circle.inset.filled" : "circle")
                        .foregroundStyle(selectedPeriod == period.period ? Theme.accent : Theme.muted)
                    Text(period.period ?? "Unknown date").foregroundStyle(Theme.ink)
                }.frame(maxWidth: .infinity, alignment: .leading)
                tokenCells(period.usage, columns: dailyColumns)
                costCell(period.usage)
            }
            .workFont(.dataSmall)
            .padding(.horizontal, Space.m).frame(height: 34)
            .background(selectedPeriod == period.period ? Theme.tintAccent : .clear)
            .contentShape(Rectangle())
        }
        .buttonStyle(TransparentButtonStyle(cornerRadius: 0))
        .accessibilityAddTraits(selectedPeriod == period.period ? .isSelected : [])
        .accessibilityIdentifier("usage.days.\(period.period ?? "unknown")")
        .id(period.period ?? "unknown")
    }

    private var clientTable: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            HStack(alignment: .firstTextBaseline) {
                Text(selectedPeriod.map { "\($0) · by client and model" } ?? "All \(days)d · by client and model")
                    .workFont(.titleSection).foregroundStyle(Theme.ink)
                Spacer()
                let count = selectedPeriod == nil ? usage.totals?.sessions : selected?.usage.sessions
                if let count { Text("\(count) sessions").workFont(.caption).foregroundStyle(Theme.muted) }
            }
            Card(padding: 0) {
                VStack(alignment: .leading, spacing: 0) {
                    tableHeader(name: "Client / model", columns: detailColumns).padding(Space.m)
                    Rectangle().fill(Theme.hairline).frame(height: 1)
                    if groups.isEmpty {
                        Text(selectedPeriod == nil ? "No client or model usage reported in this range." : "No client or model breakdown reported for this date.")
                            .workFont(.caption).foregroundStyle(Theme.muted).padding(Space.m)
                    }
                    ForEach(groups) { group in
                        HStack(spacing: Space.m) {
                            Text(group.client).workFont(.rowLabel).foregroundStyle(Theme.ink)
                                .frame(maxWidth: .infinity, alignment: .leading)
                            tokenCells(group.totals, columns: detailColumns)
                            costCell(group.totals)
                        }
                        .padding(.horizontal, Space.m).padding(.vertical, Space.s)
                        .background(Theme.tintNeutral.opacity(0.5))
                        ForEach(group.models) { model in
                            HStack(alignment: .firstTextBaseline, spacing: Space.m) {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(model.model ?? "Unattributed model").workFont(.captionSemibold).foregroundStyle(Theme.ink)
                                    if let provider = model.provider { Text(provider).workFont(.dataSmall).foregroundStyle(Theme.muted) }
                                }
                                .padding(.leading, Space.m).frame(maxWidth: .infinity, alignment: .leading)
                                tokenCells(model, columns: detailColumns)
                                costCell(model)
                            }
                            .padding(.horizontal, Space.m).padding(.vertical, Space.s)
                            .accessibilityIdentifier("usage.models.\(model.id)")
                        }
                        if group.models.isEmpty {
                            Text("Model breakdown not reported").workFont(.caption).foregroundStyle(Theme.muted)
                                .padding(.horizontal, Space.l).padding(.bottom, Space.s)
                        }
                    }
                }
            }
        }
    }

    private func tableHeader(name: String, columns: [UsageTokenColumn]) -> some View {
        HStack(spacing: Space.m) {
            Text(name).frame(maxWidth: .infinity, alignment: .leading)
            ForEach(columns, id: \.self) { column in Text(column.rawValue).frame(width: 86, alignment: .trailing) }
            Text("Cost").frame(width: 82, alignment: .trailing)
        }
        .workFont(.captionSemibold).foregroundStyle(Theme.muted)
    }

    private func tokenCells(_ bucket: UsageBucket?, columns: [UsageTokenColumn]) -> some View {
        ForEach(columns, id: \.self) { column in
            let metric = column.metric(bucket)
            Text(metric.text).workFont(.dataSmall).foregroundStyle(metric.value == nil ? Theme.muted : Theme.ink)
                .frame(width: 86, alignment: .trailing)
                .help(column == .fresh ? "\(metric.exactText). Non-cached input: \(bucket?.inputTokens.map { $0.formatted() } ?? "not reported"); output: \(bucket?.outputTokens.map { $0.formatted() } ?? "not reported")." : metric.exactText)
                .accessibilityLabel("\(column.rawValue): \(metric.exactText)")
        }
    }

    private func costCell(_ bucket: UsageBucket?) -> some View {
        Text(bucket?.costText == "—" ? "Unpriced" : bucket?.costText ?? "Unpriced")
            .workFont(.dataSmall).foregroundStyle(bucket?.costText == "—" || bucket == nil ? Theme.muted : Theme.ink)
            .frame(width: 82, alignment: .trailing)
            .help(bucket?.costConfidenceLabel ?? "Cost basis not reported")
    }
}
