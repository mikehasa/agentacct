import SwiftUI

// One decision surface: provider-reported capacity first, independently ranged
// recorded usage second. The two lanes share client rows but never a denominator,
// freshness claim, error state, or range control.
/// The ONE name for the locally recorded-usage lane, wherever it is titled:
/// the Usage pane section, the range caption, the capacity table's column and
/// the menu's section caption. It mirrors
/// `display_vocabulary.RECORDED_USAGE_TITLE`, which the TUI prints and a
/// Python test pins to this line — the app said "Tracked usage" in the menu
/// and "Recorded use · 7d" in the table, three names for one lane (K51).
enum RecordedUsageVocabulary {
    static let title = "Recorded usage"
    static var rangeCaption: String { "\(title) range" }
    static func columnLabel(days: Int) -> String { "\(title) · \(days)d" }
}

struct UsagePane: View {
    /// The visible caption for the recorded-usage range control; the picker
    /// hides its own label and uses this as its accessibility label.
    static let rangeCaption = RecordedUsageVocabulary.rangeCaption
    /// Today's single named state when the cube recorded no usage rows.
    static let noUsageTodayText = "No usage recorded today"

    @Environment(DashboardStore.self) var dashboard
    @Environment(GlanceState.self) var glance
    @State private var showStale = false
    @State private var showAbout = false

    var body: some View {
        ScrollBox {
            VStack(alignment: .leading, spacing: 0) {
                header
                capacitySection.padding(.top, Space.xl)
                recordedUsageSection.padding(.top, Space.xl)
                aboutSection.padding(.top, Space.xl)
            }
            // `.pageFrame()` owns both the width cap AND the leading
            // alignment, so no pane can leave the alignment off and slide
            // sideways relative to its neighbours on wide windows (K112).
            .padding(Space.gutter)
            .pageFrame()
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                Text("Usage & limits")
                    .workFont(.titlePage).tracking(Type.titlePageTracking)
                    .foregroundStyle(Theme.ink)
                ContextHelp(title: "About usage and limits", message: "Provider-reported capacity and locally recorded usage have separate time windows. Changing the recorded usage range updates client totals, history and attribution; it does not change provider quota windows or today's summary.", identifier: "usage.range-help")
            }
            HStack(spacing: Space.m) {
                Text(Self.rangeCaption).workFont(.caption).foregroundStyle(Theme.muted)
                usageRangeControl
            }
        }
    }

    @ViewBuilder
    private var capacitySection: some View {
        switch glance.phase {
        case .connected(let snapshot):
            let presentation = UsageCapacitySnapshot.build(
                usage: dashboard.usage?.byClient ?? [],
                limits: snapshot.glance.limits,
                plans: dashboard.planClients,
                showStale: showStale
            )
            VStack(alignment: .leading, spacing: Space.m) {
                HStack(alignment: .firstTextBaseline, spacing: Space.m) {
                    Text("Current capacity")
                        .workFont(.titleSection).tracking(Type.titleSectionTracking)
                        .foregroundStyle(Theme.ink)
                    ContextHelp(title: "About current capacity", message: "Provider-reported usage allowance. agentacct does not enforce a spending budget or stop work.", identifier: "usage.capacity-help")
                    if let freshness = freshnessLine {
                        Text(freshness)
                            .workFont(FieldFont.subtitle).foregroundStyle(Theme.muted)
                            .lineLimit(1)
                    }
                    Spacer()
                    staleControl(count: snapshot.glance.limits.filter { $0.stale == true }.count)
                }

                if let today = snapshot.glance.usage.windows.first(where: { $0.label == "today" })?.totals {
                    todayStrip(today)
                }

                if presentation.rows.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(dashboard.usage == nil
                             ? "No live capacity reported yet"
                             : "No live capacity or recorded usage in this range")
                            .workFont(.rowLabel).foregroundStyle(Theme.ink)
                        Text(capacityEmptyDetail(presentation: presentation))
                            .workFont(.caption).foregroundStyle(Theme.muted)
                    }
                    .padding(.vertical, Space.s)
                } else {
                    UsageCapacityLedger(
                        rows: presentation.rows,
                        days: dashboard.usageDays,
                        usageLoaded: dashboard.usage != nil
                    )
                }
            }
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Current capacity. Provider reported usage allowance; agentacct does not enforce a spending budget or stop work.")
        case .connecting:
            scopedCapacityState(
                title: "Connecting to local data…",
                detail: "Provider capacity is loading. Recorded usage remains a separate section."
            )
        case .disconnected(let message):
            // "Recorder", never "daemon": one noun for the thing a reviewer
            // installs, connects and reads about everywhere else (K53).
            scopedCapacityState(
                title: "Live limits unavailable — recorder not reachable",
                detail: message
            )
        case .incompatible(let message):
            scopedCapacityState(
                title: "Live limits unavailable — recorder version not compatible",
                detail: message
            )
        }
    }

    /// Two independent freshness facts, one per source, joined with ` · `
    /// (never merged: capacity and recorded usage refresh separately). Each
    /// provider reading's own data age sits on its window row.
    private var freshnessLine: String? {
        var parts: [String] = []
        if let updated = glance.lastUpdated {
            parts.append("\(FreshnessVocabulary.capacityCheckedLabel) \(dashboardFreshnessText(updated))")
        }
        if dashboard.usage != nil {
            parts.append(dashboard.usageLastUpdated.map {
                "\(FreshnessVocabulary.recordedUsageRefreshedLabel) \(dashboardFreshnessText($0))"
            } ?? "\(FreshnessVocabulary.recordedUsageRefreshedLabel) time unavailable")
        }
        return parts.isEmpty ? nil : parts.joined(separator: FreshnessVocabulary.separator)
    }

    private func staleControl(count: Int) -> some View {
        Group {
            if count > 0 {
                if SnapshotMode.enabled {
                    Chip(text: "\(count) stale hidden", tint: Theme.amber)
                } else {
                    Toggle("Show \(count) stale capacity reading\(count == 1 ? "" : "s")", isOn: $showStale)
                        .toggleStyle(.checkbox)
                        .workFont(.caption)
                        .foregroundStyle(Theme.muted)
                        .accessibilityIdentifier("usage.capacity.show-stale")
                }
            }
        }
    }

    private func capacityEmptyDetail(presentation: UsageCapacitySnapshot) -> String {
        if presentation.hiddenStaleCount > 0 {
            return "Every provider reading is stale. Show stale capacity readings to inspect them."
        }
        if dashboard.usage == nil {
            return "Recorded usage is still loading or unavailable; no client has reported a live quota window."
        }
        return "No client has reported a live quota window or usage in the selected range."
    }

    private func scopedCapacityState(title: String, detail: String) -> some View {
        VStack(alignment: .leading, spacing: Space.m) {
            Text("Current capacity")
                .workFont(.titleSection).tracking(Type.titleSectionTracking)
                .foregroundStyle(Theme.ink)
            Text("Provider-reported usage allowance. agentacct does not enforce a spending budget or stop work.")
                .workFont(.caption).foregroundStyle(Theme.muted)
            Card {
                VStack(alignment: .leading, spacing: 4) {
                    Text(title).workFont(.rowLabel).foregroundStyle(Theme.ink)
                    Text(detail).workFont(.caption).foregroundStyle(Theme.muted)
                }
            }
        }
    }

    private func todayStrip(_ totals: UsageTotals) -> some View {
        HStack(spacing: Space.l) {
            CapsLabel(text: "Today · all agents")
            if totals.hasNoUsage {
                // No rows today: one named state, never a 0 or an "unpriced"
                // cost for usage that does not exist.
                Text(Self.noUsageTodayText)
                    .workFont(.dataSmallSemibold)
                    .foregroundStyle(Theme.muted)
            } else {
                let cost = UsageCostPresentation(totals: totals)
                Text(totals.freshTokens.map(UsageTotals.compact) ?? "Tokens not reported")
                    .workFont(.dataSmallSemibold)
                    .foregroundStyle(totals.freshTokens == nil ? Theme.muted : Theme.ink)
                if totals.freshTokens != nil {
                    Text("fresh tokens").workFont(.caption).foregroundStyle(Theme.muted)
                }
                Rectangle().fill(Theme.hairline).frame(width: 1, height: 20)
                Text(cost.valueText)
                    .workFont(.dataSmallSemibold)
                    .foregroundStyle(cost.figure == nil ? Theme.muted : Theme.ink)
                if let qualifier = cost.qualifier {
                    Text(qualifier).workFont(.caption).foregroundStyle(Theme.muted)
                }
            }
            Spacer()
        }
        .padding(.horizontal, Space.l)
        .frame(minHeight: 44)
        .background(Theme.tintNeutralOnCanvas, in: RoundedRectangle(cornerRadius: Metrics.radius))
    }

    @ViewBuilder
    private var recordedUsageSection: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            VStack(alignment: .leading, spacing: Space.s) {
                HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                    Text(RecordedUsageVocabulary.title)
                        .workFont(.titleSection).tracking(Type.titleSectionTracking)
                        .foregroundStyle(Theme.ink)
                    ContextHelp(title: "About recorded cost", message: "Cost is usage reporting, not a provider invoice or balance due. Verify charges with your provider. Cost basis and completeness are shown beside each total.", identifier: "usage.cost-help")
                }
            }

            if let error = dashboard.errorText {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .workFont(.caption).foregroundStyle(Theme.coral)
            }

            if let usage = dashboard.usage {
                summaryStrip(usage)
                if let periods = usage.byPeriod, periods.count > 1 {
                    UsagePeriodChart(
                        periods: periods,
                        presentation: UsagePeriodPresentation(usage: usage),
                        rangeTotal: usage.totals,
                        vocabulary: UsageChartVocabulary(usage: usage)
                    )
                }
                if !capacityIsConnected {
                    UsageBreakdownTable(
                        title: "By client",
                        nameHeader: "Client",
                        days: dashboard.usageDays,
                        rows: usage.byClient.map { ($0.client ?? "Unattributed client", $0) }
                    )
                }
                UsageBreakdownTable(
                    title: "By model",
                    nameHeader: "Model",
                    days: dashboard.usageDays,
                    rows: usage.byModel.map { ($0.model ?? "Unattributed model", $0) },
                    footnote: PayloadAbsence.text(usage.byModelSessionsFootnote)
                )
                basisFooter(usage)
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Recorded usage not loaded").workFont(.rowLabel).foregroundStyle(Theme.ink)
                    Text("Capacity may still be available above while the usage summary loads.")
                        .workFont(.caption).foregroundStyle(Theme.muted)
                }
                .padding(.vertical, Space.s)
            }
        }
    }

    private var capacityIsConnected: Bool {
        if case .connected = glance.phase { return true }
        return false
    }

    /// The app's ONE segmented control, so the range picker speaks the same
    /// cobalt as the rest of the app and renders with every option visible —
    /// the native segmented picker painted its selection in the system accent
    /// and had to be replaced by a single Chip in review renders (K108).
    private var usageRangeControl: some View {
        SegmentedChoice(
            options: [(7, "7d"), (30, "30d"), (90, "90d")],
            selection: Binding(
                get: { dashboard.usageDays },
                set: { days in Task { await dashboard.setUsageDays(days) } }
            ),
            // The visible caption beside the control is its only label.
            accessibilityLabel: Self.rangeCaption,
            accessibilityIdentifier: "usage.history.range"
        )
    }

    private func summaryStrip(_ usage: UsageSummary) -> some View {
        let totals = usage.totals
        let activity = UsagePeriodPresentation(usage: usage)
        let cost = totals.map(UsageCostPresentation.init(bucket:))

        return StripRow(cells: [
            StripRow.Cell(
                id: "tokens",
                label: "Tokens",
                value: totals?.freshTokens.map(UsageTotals.compact),
                qualifier: "fresh · \(PayloadAbsence.text(usage.tokenBasisLabel) ?? PayloadAbsence.costBasis)",
                absent: "none recorded"
            ),
            StripRow.Cell(
                id: "sessions",
                label: "Sessions",
                value: totals?.sessions.map(String.init),
                qualifier: nil,
                absent: "not reported"
            ),
            StripRow.Cell(
                id: "cost",
                label: "Cost",
                value: cost?.figure,
                qualifier: cost?.qualifier,
                absent: cost?.absence ?? PayloadAbsence.cost
            ),
            StripRow.Cell(
                id: "periods",
                label: activity.label,
                value: activity.value,
                qualifier: "with recorded usage",
                absent: activity.absent
            ),
        ])
    }

    @ViewBuilder
    private func basisFooter(_ usage: UsageSummary) -> some View {
        let parts: [String] = [
            "cost: \(PayloadAbsence.text(usage.totals?.costConfidenceDisplay) ?? PayloadAbsence.costBasis)",
            "token counts come from client usage records",
            usage.totals?.cacheReadTokens.map {
                "fresh tokens exclude \(UsageTotals.compact($0)) cache-read tokens"
            },
        ].compactMap { $0 }
        Text(parts.joined(separator: " · "))
            .workFont(.dataSmall).foregroundStyle(Theme.muted)
            .fixedSize(horizontal: false, vertical: true)
    }

    @ViewBuilder
    private var aboutSection: some View {
        if SnapshotMode.enabled {
            Card {
                HStack {
                    Image(systemName: "chevron.right").font(.system(size: 10, weight: .semibold))
                    Text("About these numbers").workFont(.rowLabel).foregroundStyle(Theme.ink)
                    Spacer()
                    Text("cost, windows, and plan calibration")
                        .workFont(.dataSmall).foregroundStyle(Theme.muted)
                }
            }
        } else {
            Card {
                DisclosureGroup(isExpanded: $showAbout) {
                    aboutDetails.padding(.top, Space.l)
                } label: {
                    HStack {
                        Text("About these numbers").workFont(.rowLabel).foregroundStyle(Theme.ink)
                        Spacer()
                        Text("cost, windows, and plan calibration")
                            .workFont(.dataSmall).foregroundStyle(Theme.muted)
                    }
                }
                .disclosureGroupStyle(FullRowDisclosureStyle())
                .accessibilityIdentifier("usage.about")
            }
        }
    }

    private var aboutDetails: some View {
        VStack(alignment: .leading, spacing: Space.l) {
            VStack(alignment: .leading, spacing: 6) {
                CapsLabel(text: "Cost grammar")
                // The payload's `cost_legend`, verbatim (Swift keeps no copy).
                Text(verbatim: PayloadAbsence.text(dashboard.usage?.costLegend) ?? PayloadAbsence.costLegend)
                    .workFont(.caption).foregroundStyle(Theme.muted)
            }
            Rectangle().fill(Theme.hairline).frame(height: 1)
            VStack(alignment: .leading, spacing: 6) {
                CapsLabel(text: "Provider windows")
                Text("Rolling windows follow each client's activity; fixed windows reset at the provider's stated time. Markers sit at 75% and 90% used.")
                    .workFont(.caption).foregroundStyle(Theme.muted)
            }
            if !dashboard.planClients.isEmpty {
                Rectangle().fill(Theme.hairline).frame(height: 1)
                VStack(alignment: .leading, spacing: Space.m) {
                    CapsLabel(text: "Weekly plan-share estimates")
                    Text("Today and 7d estimates stay fixed; the selected usage range applies only to each model accumulation below.")
                        .workFont(.caption).foregroundStyle(Theme.muted)
                    ForEach(Array(dashboard.planClients.enumerated()), id: \.element.id) { index, client in
                        if index > 0 { Rectangle().fill(Theme.hairline).frame(height: 1) }
                        UsagePlanClientDetail(client: client, days: dashboard.usageDays)
                    }
                }
            }
        }
    }
}

struct UsagePeriodPresentation {
    let label: String
    let value: String?
    let absent: String
    private let unit: String
    private let bucketDescription: String?

    init(usage: UsageSummary) {
        switch usage.filtersEcho?.granularity {
        case "daily":
            unit = "day"
            bucketDescription = nil
            label = "Active days"
            absent = "no daily series"
        case "weekly":
            unit = "week"
            bucketDescription = "weekly buckets"
            label = "Active weeks"
            absent = "no weekly series"
        default:
            unit = "period"
            bucketDescription = "period buckets"
            label = "Active periods"
            absent = "no period series"
        }

        let periods = usage.byPeriod ?? []
        let activeCount = periods.filter {
            ($0.freshTokens ?? 0) > 0 || $0.estimatedCostUsd != nil
        }.count
        value = periods.isEmpty ? nil : "\(activeCount)/\(periods.count)"
    }

    var costChartTitle: String { "Cost per \(unit)" }

    func tokenChartTitle(group: String?) -> String {
        let title = "Fresh tokens per \(unit)"
        return group.map { "\(title) · \($0)" } ?? title
    }

    var previousAccessibilityLabel: String { "Previous usage \(unit)" }
    var nextAccessibilityLabel: String { "Next usage \(unit)" }
    var selectionAccessibilityHint: String { "Selects this \(unit)'s value" }
    var pinAccessibilityHint: String { "Pins or clears this \(unit)'s value" }

    func historyRangeDescription(days: Int) -> String {
        let range = "last \(days) days"
        return bucketDescription.map { "\(range) · \($0)" } ?? range
    }
}

private struct UsagePlanClientDetail: View {
    let client: V1PlanClient
    let days: Int
    @State private var showDaily = false
    @State private var showBasis = false

    private var presentation: UsagePlanPresentation {
        UsagePlanPresentation(client: client, days: days)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            HStack(alignment: .firstTextBaseline, spacing: Space.m) {
                Text(client.client).workFont(.rowLabel).foregroundStyle(Theme.ink)
                    .frame(width: 140, alignment: .leading)
                VStack(alignment: .leading, spacing: 4) {
                    Text(presentation.detailText)
                        .workFont(.caption).foregroundStyle(Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                    if let basis = presentation.basisText {
                        // The technical fit detail stays behind a disclosure.
                        DisclosureGroup(isExpanded: $showBasis) {
                            Text(basis).workFont(.caption).foregroundStyle(Theme.muted)
                                .fixedSize(horizontal: false, vertical: true)
                                .padding(.top, 2)
                        } label: {
                            Text("Fit details").workFont(.caption).foregroundStyle(Theme.muted)
                        }
                        .disclosureGroupStyle(FullRowDisclosureStyle())
                        .accessibilityIdentifier("usage.plan.basis.\(client.client)")
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            if let daily = presentation.dailyText {
                if presentation.dailyRows.isEmpty {
                    Text(daily).workFont(.caption).foregroundStyle(Theme.muted)
                } else {
                    DisclosureGroup(isExpanded: $showDaily) {
                        LazyVStack(alignment: .leading, spacing: 5) {
                            ForEach(Array(presentation.dailyRows.enumerated()), id: \.offset) { _, row in
                                Text(row).workFont(.caption).foregroundStyle(Theme.muted)
                            }
                        }
                        .padding(.top, Space.s)
                    } label: {
                        Text(daily).workFont(.captionSemibold).foregroundStyle(Theme.ink)
                    }
                    .disclosureGroupStyle(FullRowDisclosureStyle())
                    .accessibilityIdentifier("usage.plan.daily.\(client.client)")
                }
            }
            if !presentation.modelRows.isEmpty {
                CapsLabel(text: presentation.modelHeading)
                ScrollContentStack(alignment: .leading, spacing: Space.s) {
                    ForEach(Array(presentation.modelRows.enumerated()), id: \.offset) { _, row in
                        Text(row).workFont(.caption).foregroundStyle(Theme.muted)
                    }
                }
            }
        }
    }
}

// MARK: - Summary strip row (shared cell layout)

/// A v7 summary strip: caps captions over 18/700 mono values, cells divided by
/// fixed-height hairlines, a bottom hairline under the whole strip. Absent
/// facts are named at the value position — never a fabricated 0 or a dash.
struct StripRow: View {
    struct Cell: Identifiable {
        let id: String
        let label: String
        let value: String?
        let qualifier: String?
        let absent: String?
    }

    let cells: [Cell]

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 0) {
                ForEach(Array(cells.enumerated()), id: \.element.id) { index, cell in
                    if index > 0 {
                        Rectangle().fill(Theme.hairline).frame(width: 1, height: 46)
                    }
                    VStack(alignment: .leading, spacing: 6) {
                        CapsLabel(text: cell.label)
                        if let value = cell.value {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(value).workFont(.kpi).foregroundStyle(Theme.ink)
                                if let qualifier = cell.qualifier {
                                    Text(qualifier).workFont(FieldFont.qualifier).foregroundStyle(Theme.muted)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                        } else {
                            Text(cell.absent ?? "not recorded")
                                .workFont(.body).foregroundStyle(Theme.muted)
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

// MARK: - Period chart (one series per chart)

/// The usage chart: estimated cost or fresh tokens per API-reported period, with
/// an optional single-client group filter. Always ONE series — a stack never
/// appears (v7 chart discipline). Heights are strictly proportional. A period
/// without a plottable value is a flat stub whose state is named: a hairline
/// stub for "no recorded usage", a neutral stub for usage that is unpriced or
/// excluded. Bars rest in the full chart color; the others dim only while the
/// user points at, focuses or pins one bar.
struct UsagePeriodChart: View {
    let periods: [PeriodBucket]
    let presentation: UsagePeriodPresentation
    /// The range totals the resting readout names when no bar is selected.
    let rangeTotal: UsageBucket?
    /// The payload's chart words: measure labels/order/default, legend, unit.
    let vocabulary: UsageChartVocabulary

    typealias Series = UsageChartSeries

    static let noRecordedUsageText = "no recorded usage"
    static let noneRecordedText = "none recorded"
    static let rangeTotalLabel = "Range total"

    /// The persisted measure key ("" = never chosen: the payload default).
    @AppStorage("usage.series.usage") private var storedSeries: String = ""
    /// nil = all clients; set = one client's token series (tokens mode only).
    @State private var group: String?
    @State private var hoveredIndex: Int?
    /// nil until the user pins a bar: the resting chart names the range total.
    @State private var selectedIndex: Int?
    @FocusState private var focusedIndex: Int?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    init(
        periods: [PeriodBucket],
        presentation: UsagePeriodPresentation,
        rangeTotal: UsageBucket? = nil,
        vocabulary: UsageChartVocabulary = UsageChartVocabulary()
    ) {
        self.periods = periods
        self.presentation = presentation
        self.rangeTotal = rangeTotal
        self.vocabulary = vocabulary
        _selectedIndex = State(initialValue: nil)
    }

    private var series: Series {
        vocabulary.resolvedSeries(stored: storedSeries, periods: periods)
    }

    /// The priced amount a period plots: the complete figure, else the known
    /// partial subtotal. nil when nothing in the period is priced.
    static func pricedAmount(_ period: PeriodBucket) -> Double? {
        period.estimatedCostUsd ?? period.knownAdditiveCostUsd
    }

    /// True when the cube recorded no usage rows at all for the period.
    static func hasNoRecordedUsage(_ period: PeriodBucket) -> Bool {
        if let state = period.costState { return state == "none_recorded" }
        return period.rows == 0
    }

    /// The cost readout for one period, keyed on the cube's `cost_state`:
    /// no rows, excluded rows, unpriced rows, a partial subtotal and a
    /// complete figure are five different named states.
    static func costValueText(_ period: PeriodBucket) -> String {
        let hasFigure = pricedAmount(period) != nil
        switch period.costState {
        case "none_recorded":
            return noRecordedUsageText
        case "partial":
            // The reducer's label names the unpriced share (`Partial subtotal
            // · 1 of 3 usage records unpriced`).
            return hasFigure
                ? "\(period.costText) · \(PayloadAbsence.text(period.costTotalLabel) ?? PayloadAbsence.costLabel)"
                : (PayloadAbsence.text(period.costTotalLabel) ?? PayloadAbsence.unpriced)
        case "complete":
            return hasFigure ? period.costText : PayloadAbsence.unpriced
        default:
            // held / unpriced / an older payload: the reducer's named state.
            if hasFigure { return period.costText }
            return PayloadAbsence.text(period.costTotalLabel)
                ?? (period.rows == 0 ? noRecordedUsageText : PayloadAbsence.unpriced)
        }
    }

    private var clients: [String] {
        var seen: Set<String> = []
        for period in periods {
            seen.formUnion((period.byClient ?? [:]).keys)
        }
        return seen.sorted()
    }

    private var groupOptions: [(String?, String)] {
        [(String?.none, "All clients")] + clients.map { (String?.some($0), $0) }
    }

    /// The plotted value for a period, or nil when it has no value to plot
    /// (an unpriced period is NOT a $0 period; a period with no rows is not a
    /// zero-token period).
    private func value(_ period: PeriodBucket) -> Double? {
        switch series {
        case .cost:
            return Self.pricedAmount(period)
        case .tokens:
            guard !Self.hasNoRecordedUsage(period) else { return nil }
            if let group {
                return (period.byClient?[group]?.freshTokens).map(Double.init)
            }
            return period.freshTokens.map(Double.init)
        }
    }

    private var maxValue: Double {
        max(periods.compactMap(value).max() ?? 1, 0.0001)
    }

    private var chartGeometryAnimation: Animation? {
        Motion.animatesChartGeometry(
            bucketCount: periods.count,
            reduceMotion: reduceMotion
        ) ? Motion.contentUpdate : nil
    }

    private var peakIndex: Int? {
        periods.indices.max { (value(periods[$0]) ?? -1) < (value(periods[$1]) ?? -1) }
    }

    private var activeIndex: Int? {
        hoveredIndex ?? focusedIndex ?? selectedIndex
    }

    private func valueText(_ period: PeriodBucket) -> String {
        switch series {
        case .cost:
            return Self.costValueText(period)
        case .tokens:
            if let tokens = value(period) { return UsageTotals.compact(tokens) }
            return Self.noneRecordedText
        }
    }

    /// Axis ticks are plain rounded scale values with no cost glyph (K39);
    /// the caption names the unit and basis once.
    private func axisText(_ amount: Double) -> String {
        switch series {
        case .cost: return Fmt.axisAmount(amount, scale: maxValue)
        case .tokens: return Fmt.axisTokens(amount)
        }
    }

    /// The peak annotation is a measured figure: the period's own cost in the
    /// shared grammar plus its basis in words (`peak ≈$1,163.49 · pricing
    /// estimate`).
    private func peakText(_ period: PeriodBucket, peak: Double) -> String {
        switch series {
        case .cost:
            let basis = PayloadAbsence.text(period.costConfidenceDisplay) ?? PayloadAbsence.costBasis
            return "peak \(period.costText) · \(basis)"
        case .tokens: return "peak \(axisText(peak))"
        }
    }

    /// `USD · mixed · mostly pricing estimate` — the unit and basis, once.
    private var costCaption: String {
        let unit = PayloadAbsence.text(vocabulary.costUnit) ?? PayloadAbsence.measure
        let basis = PayloadAbsence.text(rangeTotal?.costConfidenceDisplay) ?? PayloadAbsence.costBasis
        return "\(unit) · \(basis)"
    }

    private var hasPartialBar: Bool {
        series == .cost && periods.contains { $0.costState == "partial" && Self.pricedAmount($0) != nil }
    }

    private var axisLabels: [String] {
        [axisText(maxValue), axisText(maxValue / 2), axisText(0)]
    }

    /// The range total named by the resting readout.
    private var rangeTotalText: String {
        switch series {
        case .cost:
            return rangeTotal.map { UsageCostPresentation(bucket: $0).valueText } ?? PayloadAbsence.cost
        case .tokens:
            if group == nil, let fresh = rangeTotal?.freshTokens {
                return UsageTotals.compact(fresh)
            }
            let values = periods.compactMap(value)
            guard !values.isEmpty else { return Self.noneRecordedText }
            return UsageTotals.compact(values.reduce(0, +))
        }
    }

    private var readoutText: String {
        if let selectedIndex, periods.indices.contains(selectedIndex) {
            return "\(periods[selectedIndex].displayLabel) · \(valueText(periods[selectedIndex]))"
        }
        if series == .cost, let rangeTotal {
            // The figure with the reducer's own label: `total` only when
            // complete, `Partial subtotal · N of M usage records unpriced`
            // otherwise — Swift never appends "total" (K105).
            let cost = UsageCostPresentation(bucket: rangeTotal)
            guard let figure = cost.figure else { return cost.absence }
            return "\(figure) · \(cost.totalLabel)"
        }
        return "\(Self.rangeTotalLabel) · \(rangeTotalText)"
    }

    /// Plot height: the top gridline is exactly the max value's line.
    private static let plotHeight: CGFloat = 128
    /// The reserved band above the plot: the peak annotation at rest, the
    /// shared readout while a bar is active.
    private static let peakBandHeight: CGFloat = 22
    /// The date band under the plot.
    private static let dateBandHeight: CGFloat = 16

    /// The peak annotation, centered over the peak bar in its own band.
    @ViewBuilder
    private var peakBand: some View {
        HStack(alignment: .bottom, spacing: 3) {
            ForEach(Array(periods.enumerated()), id: \.offset) { index, period in
                Group {
                    if index == peakIndex, let peak = value(period), peak > 0 {
                        Text(peakText(period, peak: peak))
                            .workFont(.dataSmall)
                            .foregroundStyle(Theme.muted)
                            .fixedSize()
                    } else {
                        Color.clear.frame(height: 1)
                    }
                }
                .frame(maxWidth: .infinity)
            }
        }
        .frame(height: Self.peakBandHeight - 2, alignment: .bottom)
        .padding(.bottom, 2)
    }

    private var chartTitle: String {
        switch series {
        case .cost: return presentation.costChartTitle
        case .tokens: return presentation.tokenChartTitle(group: group)
        }
    }

    private var lastIndex: Int { periods.index(before: periods.endIndex) }

    var body: some View {
        Card(padding: 0) {
            VStack(spacing: 0) {
                header
                    .padding(.horizontal, Space.xl)
                    .padding(.vertical, Space.l)

                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.horizontal, Space.xl)

                HStack(alignment: .top, spacing: Space.s) {
                    // Labels center ON their gridlines: one linear scale where
                    // the max value IS the top line. The column is exactly the
                    // plot's height; the date band sits outside it.
                    PeriodChartAxis(labels: axisLabels, plotHeight: Self.plotHeight)
                        .padding(.top, Self.peakBandHeight)

                    GeometryReader { proxy in
                        VStack(spacing: 0) {
                            VStack(spacing: 0) {
                                // The reserved band above the plot carries the
                                // peak annotation at rest and the ONE readout
                                // while a bar is active — the readout is
                                // anchored over that bar, not pinned to the
                                // plot's far corner (K79).
                                band(plotWidth: proxy.size.width)
                                plot.frame(height: Self.plotHeight)
                            }
                            .animation(chartGeometryAnimation, value: series)
                            .animation(chartGeometryAnimation, value: group)

                            dateBand(plotWidth: proxy.size.width)
                                .padding(.top, 7)
                        }
                    }
                    .frame(height: Self.peakBandHeight + Self.plotHeight + 7 + Self.dateBandHeight)
                }
                .padding(Space.xl)
                .animation(Motion.hover, value: hoveredIndex)

                if series == .cost {
                    CostChartLegendRow(
                        caption: costCaption,
                        legend: hasPartialBar ? vocabulary.costLegend : nil
                    )
                    .padding(.horizontal, Space.xl)
                    .padding(.bottom, Space.l)
                }
            }
        }
        .onChange(of: periods.map(\.period)) {
            hoveredIndex = nil
            focusedIndex = nil
            selectedIndex = nil
        }
    }

    @ViewBuilder
    private var header: some View {
        HStack(spacing: Space.m) {
            Text(chartTitle).workFont(.titleCard).foregroundStyle(Theme.ink)
            Spacer()
            if !SnapshotMode.enabled, !periods.isEmpty {
                IconButton(
                    systemName: "chevron.left",
                    label: presentation.previousAccessibilityLabel
                ) {
                    selectedIndex = selectedIndex.map { max(periods.startIndex, $0 - 1) } ?? lastIndex
                }
                .disabled(selectedIndex == periods.startIndex)
            }
            Text(readoutText)
                .workFont(.dataSmallSemibold)
                .foregroundStyle(Theme.ink)
                .lineLimit(1)
                .frame(minWidth: 112)
            if !SnapshotMode.enabled, !periods.isEmpty {
                IconButton(
                    systemName: "chevron.right",
                    label: presentation.nextAccessibilityLabel
                ) {
                    selectedIndex = selectedIndex.map { min(lastIndex, $0 + 1) } ?? periods.startIndex
                }
                .disabled(selectedIndex == lastIndex)
            }
            CapsLabel(text: "Measure")
            SegmentedChoice(
                options: vocabulary.orderedSeries.map { ($0, vocabulary.label(for: $0)) },
                selection: Binding(
                    get: { series },
                    set: { storedSeries = $0.rawValue }
                ),
                accessibilityLabel: "Chart measure",
                accessibilityIdentifier: "usage.history.measure"
            )
            if !SnapshotMode.enabled {
                if series == .tokens, clients.count > 1 {
                    Text("Group").workFont(.caption).foregroundStyle(Theme.muted)
                    AppMenuPicker(
                        title: "Group",
                        selection: $group,
                        options: groupOptions,
                        accessibilityIdentifier: "usage.history.group"
                    )
                }
            }
        }
    }

    private var plot: some View {
        ZStack(alignment: .bottomLeading) {
            Rectangle().fill(Theme.hairline).frame(height: 1)
                .offset(y: -Self.plotHeight + 0.5)
            Rectangle().fill(Theme.hairline).frame(height: 1)
                .offset(y: -Self.plotHeight / 2)
            Rectangle().fill(Theme.hairline).frame(height: 1)

            HStack(alignment: .bottom, spacing: 3) {
                ForEach(Array(periods.enumerated()), id: \.offset) { index, period in
                    bar(index: index, period: period)
                }
            }
        }
    }

    private func bar(index: Int, period: PeriodBucket) -> some View {
        Button {
            selectedIndex = selectedIndex == index ? nil : index
        } label: {
            Group {
                if let periodValue = value(period) {
                    // Strictly linear: the peak bar touches the line its label names.
                    // A partial-cost bucket wears the open cap at rest (K106).
                    PeriodValueBar(
                        color: Theme.periodBarColor(
                            isActive: activeIndex == index,
                            hasUserSelection: activeIndex != nil
                        ),
                        height: max(1, Self.plotHeight * periodValue / maxValue),
                        partial: series == .cost && period.costState == "partial"
                    )
                } else {
                    // No plottable value: the SHARED absence mark, never a
                    // zero-height lie. The two states differ by shape and
                    // weight — the Usage chart used to change only the fill,
                    // which is ΔE 1.1 apart in dark (K46).
                    PeriodAbsenceMark(
                        kind: Self.hasNoRecordedUsage(period) ? .noUsage : .unpriced
                    )
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
            .contentShape(Rectangle())
        }
        // Preserve proportional 7/30/90-day geometry. The Previous/Next
        // controls above are equivalent controls for reaching every period
        // at a large target.
        .buttonStyle(TransparentButtonStyle(cornerRadius: 2))
        .focused($focusedIndex, equals: index)
        .onHover { inside in
            if inside {
                hoveredIndex = index
            } else if hoveredIndex == index {
                hoveredIndex = nil
            }
        }
        .accessibilityLabel("\(period.displayLabel), \(valueText(period))")
        .accessibilityHint(presentation.pinAccessibilityHint)
        .accessibilityAddTraits(selectedIndex == index ? .isSelected : [])
        .accessibilityIdentifier("usage.history.day.\(index)")
    }

    /// The band above the plot: the readout over the focused bar while one is
    /// active, otherwise the peak annotation. One band, never both (K79).
    @ViewBuilder
    private func band(plotWidth: CGFloat) -> some View {
        if let activeIndex, periods.indices.contains(activeIndex) {
            let gap: CGFloat = 3
            let count = max(periods.count, 1)
            let columnWidth = max(1, (plotWidth - gap * CGFloat(count - 1)) / CGFloat(count))
            let center = columnWidth / 2 + CGFloat(activeIndex) * (columnWidth + gap)
            ZStack(alignment: .bottomLeading) {
                Color.clear
                PeriodChartReadout(
                    text: PeriodChartReadout.text(
                        period: periods[activeIndex].displayLabel,
                        value: valueText(periods[activeIndex])
                    )
                )
                .modifier(PeriodChartReadoutAnchor(columnCenter: center, plotWidth: plotWidth))
            }
            .frame(width: plotWidth, height: Self.peakBandHeight, alignment: .bottomLeading)
        } else {
            peakBand
        }
    }

    /// The date band: one density rule shared with the Dashboard chart — label
    /// every Nth bucket, where N is the smallest stride whose labels fit.
    private func dateBand(plotWidth: CGFloat) -> some View {
        let gap: CGFloat = 3
        let count = max(periods.count, 1)
        let columnWidth = max(1, (plotWidth - gap * CGFloat(count - 1)) / CGFloat(count))
        let stride = PeriodChartDateBand.stride(
            labelWidth: Self.dateLabelWidth(periods),
            count: periods.count,
            plotWidth: plotWidth
        )
        let labelled = PeriodChartDateBand.labelledIndices(count: periods.count, stride: stride)
        return HStack(spacing: gap) {
            ForEach(Array(periods.enumerated()), id: \.offset) { index, period in
                Text(labelled.contains(index) ? period.displayLabel : "")
                    .lineLimit(1)
                    .fixedSize(horizontal: true, vertical: false)
                    .frame(width: columnWidth)
            }
        }
        .workFont(.dataSmall)
        .foregroundStyle(Theme.muted)
        .frame(height: Self.dateBandHeight)
        .accessibilityHidden(true)
    }

    /// Widest date label plus breathing room (the stride rule's input).
    static func dateLabelWidth(_ periods: [PeriodBucket]) -> CGFloat {
        let longest = periods.map(\.displayLabel.count).max() ?? 5
        // `.dataSmall` is a 12pt mono face: ~7.2pt per advance. Round UP, and
        // add a gutter, so the stride can never be one step too small and let
        // two labels touch.
        return CGFloat(longest) * 8 + 12
    }
}


// MARK: - Breakdown tables

/// A v7 breakdown table: NAME · SESSIONS · TOKENS · SHARE · COST, ranked
/// by fresh tokens, share bars strictly proportional to the table's own total.
struct UsageBreakdownTable: View {
    let title: String
    let nameHeader: String
    let days: Int
    let rows: [(name: String, bucket: UsageBucket)]
    /// A note on how the rows relate to the range total (by-model sessions
    /// overlap), from the payload.
    var footnote: String? = nil

    private var sorted: [(name: String, bucket: UsageBucket)] {
        rows.sorted { left, right in
            switch (left.bucket.freshTokens, right.bucket.freshTokens) {
            case let (lhs?, rhs?) where lhs != rhs: return lhs > rhs
            case (_?, nil): return true
            case (nil, _?): return false
            default: return left.name.localizedStandardCompare(right.name) == .orderedAscending
            }
        }
    }

    private var totalFresh: Int {
        rows.reduce(0) { $0 + ($1.bucket.freshTokens ?? 0) }
    }

    var body: some View {
        Card(padding: 0) {
            VStack(spacing: 0) {
                HStack(spacing: Space.s) {
                    Text(title).workFont(.titleCard).foregroundStyle(Theme.ink)
                    Text("last \(days) days").workFont(.dataSmall).foregroundStyle(Theme.muted)
                    Spacer()
                }
                .padding(.horizontal, Space.xl)
                .frame(height: 52)

                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.horizontal, Space.xl)

                HStack(spacing: Space.l) {
                    CapsLabel(text: nameHeader).frame(maxWidth: .infinity, alignment: .leading)
                    CapsLabel(text: "Sessions").frame(width: 76, alignment: .trailing)
                    CapsLabel(text: "Tokens").frame(width: 76, alignment: .trailing)
                    CapsLabel(text: "Share").frame(width: 140, alignment: .leading)
                    CapsLabel(text: "Cost").frame(width: 90, alignment: .trailing)
                }
                .padding(.horizontal, Space.xl)
                .frame(height: Metrics.rowHeader)

                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.horizontal, Space.xl)

                if sorted.isEmpty {
                    Text("No recorded usage in this window")
                        .workFont(.body).foregroundStyle(Theme.muted)
                        .padding(Space.xl)
                        .frame(maxWidth: .infinity, alignment: .leading)
                } else {
                    ScrollContentStack(spacing: 0) { populatedRows }
                    if let footnote {
                        Rectangle().fill(Theme.hairline).frame(height: 1).padding(.horizontal, Space.xl)
                        Text(footnote)
                            .workFont(.caption).foregroundStyle(Theme.muted)
                            .padding(.horizontal, Space.xl)
                            .padding(.vertical, Space.m)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var populatedRows: some View {
        ForEach(Array(sorted.enumerated()), id: \.offset) { index, row in
            if index > 0 {
                Rectangle().fill(Theme.hairline).frame(height: 1)
                    .padding(.horizontal, Space.xl)
            }
            tableRow(row.name, row.bucket)
        }
    }

    private func tableRow(_ name: String, _ bucket: UsageBucket) -> some View {
        let share = bucket.freshTokens.flatMap { tokens in
            totalFresh > 0 ? Double(tokens) / Double(totalFresh) : 0
        }
        let cost = UsageCostPresentation(bucket: bucket)
        return HStack(spacing: Space.l) {
            Text(name)
                .workFont(.rowLabel).foregroundStyle(Theme.ink)
                .lineLimit(1).truncationMode(.middle)
                .help(name)
                .frame(maxWidth: .infinity, alignment: .leading)
            Text(bucket.sessions.map(String.init) ?? "not reported")
                .workFont(.dataSmall).foregroundStyle(bucket.sessions == nil ? Theme.muted : Theme.ink)
                .frame(width: 76, alignment: .trailing)
            Text(bucket.freshTokens.map(UsageTotals.compact) ?? "not reported")
                .workFont(.dataSmall).foregroundStyle(bucket.freshTokens == nil ? Theme.muted : Theme.ink)
                .frame(width: 76, alignment: .trailing)
            Group {
                if let share {
                    HStack(spacing: Space.s) {
                        MeterBar(fraction: share, tint: Theme.chartBar, height: 6)
                            .frame(width: 80)
                        Text(Fmt.percentShare(share))
                            .workFont(.dataSmall).foregroundStyle(Theme.ink)
                            .frame(width: 40, alignment: .leading)
                    }
                } else {
                    Text("not reported")
                        .workFont(.dataSmall).foregroundStyle(Theme.muted)
                }
            }
            .frame(width: 140, alignment: .leading)
            // The narrow column shows the short named absence; the full
            // state ("no priced usage · N of N rows unpriced") rides the
            // help text and the accessibility label.
            Text(cost.figure ?? bucket.costText)
                .workFont(.dataSmall)
                .foregroundStyle(cost.figure == nil ? Theme.muted : Theme.ink)
                .lineLimit(1)
                .help(cost.accessibilityText)
                .frame(width: 90, alignment: .trailing)
        }
        .padding(.horizontal, Space.xl)
        .frame(minHeight: Metrics.rowTable)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(
            [
                name,
                bucket.sessions.map { Fmt.count($0, "session") } ?? "sessions not reported",
                bucket.freshTokens.map { "\($0) fresh tokens" } ?? "tokens not reported",
                share.map { "\(Fmt.percentShare($0)) of known fresh tokens" }
                    ?? "token share not reported",
                cost.accessibilityText,
            ].compactMap { $0 }.joined(separator: ", ")
        )
    }
}


// MARK: - Shared chart vocabulary and marks

/// A usage chart's measure keys. Labels, order and the resting measure come
/// from the payload (`usage_series`, `usage_series_default`).
enum UsageChartSeries: String, CaseIterable, Identifiable, Hashable {
    case tokens
    case cost
    var id: String { rawValue }
}

/// The payload's chart words, shared by the Usage and Dashboard charts so the
/// two can never differ in measure names, order or resting measure (K109).
struct UsageChartVocabulary {
    var options: [UsageSeriesOption] = []
    var defaultKey: String? = nil
    var costLegend: String? = nil
    var costUnit: String? = nil
    var tokenBasis: String? = nil

    init() {}

    init(usage: UsageSummary?) {
        options = usage?.usageSeries ?? []
        defaultKey = usage?.usageSeriesDefault
        costLegend = usage?.costChartLegend
        costUnit = usage?.costChartUnit
        tokenBasis = usage?.tokenBasisLabel
    }

    var orderedSeries: [UsageChartSeries] {
        let keyed = options.compactMap { UsageChartSeries(rawValue: $0.key) }
        return keyed.isEmpty ? UsageChartSeries.allCases : keyed
    }

    func label(for series: UsageChartSeries) -> String {
        PayloadAbsence.text(options.first { $0.key == series.rawValue }?.label) ?? PayloadAbsence.measure
    }

    /// A persisted choice wins; otherwise the payload's resting measure. A
    /// cost choice with nothing priced to chart rests on tokens.
    func resolvedSeries(stored: String, periods: [PeriodBucket]) -> UsageChartSeries {
        let priced = periods.contains { ($0.estimatedCostUsd ?? $0.knownAdditiveCostUsd) != nil }
        let chosen = UsageChartSeries(rawValue: stored)
            ?? defaultKey.flatMap(UsageChartSeries.init(rawValue:))
            ?? (priced ? .cost : .tokens)
        return chosen == .cost && !priced ? .tokens : chosen
    }
}

/// One charted bar. A partial-cost bucket keeps the chart fill and adds an
/// OPEN CAP: its top 4pt in the card color inside a 1.5pt chart-color outline,
/// so "this is a lower bound" reads at rest without a new hue (K106).
struct PeriodValueBar: View {
    let color: Color
    let height: CGFloat
    let partial: Bool

    /// The cap's open interior is the top 4pt; the 1.5pt outline sits
    /// around it, so the cap frame is 4 + 2 × 1.5.
    static let capStroke: CGFloat = 1.5
    static let capHeight: CGFloat = 4 + 2 * capStroke

    var body: some View {
        ZStack(alignment: .top) {
            if partial, height < Self.capHeight + 2 {
                // Too short for a cap: the whole bar is the open outline.
                RoundedRectangle(cornerRadius: 1, style: .continuous)
                    .fill(Theme.card)
                    .overlay(
                        RoundedRectangle(cornerRadius: 1, style: .continuous)
                            .strokeBorder(color, lineWidth: min(Self.capStroke, height / 2))
                    )
            } else {
                RoundedRectangle(cornerRadius: 2, style: .continuous)
                    .fill(color)
            }
            if partial, height >= Self.capHeight + 2 {
                RoundedRectangle(cornerRadius: 2, style: .continuous)
                    .fill(Theme.card)
                    .overlay(
                        RoundedRectangle(cornerRadius: 2, style: .continuous)
                            .strokeBorder(color, lineWidth: Self.capStroke)
                    )
                    .frame(height: Self.capHeight)
            }
        }
        .frame(height: height)
    }
}

/// The caption and legend row under a cost chart: the unit and basis once,
/// and — when a partial bar is drawn — the payload legend naming the cap.
struct CostChartLegendRow: View {
    let caption: String?
    let legend: String?

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Space.s) {
            Text([caption, legend].compactMap { PayloadAbsence.text($0) }.joined(separator: " · "))
                .workFont(.dataSmall).foregroundStyle(Theme.muted)
            Spacer(minLength: 0)
        }
        .fixedSize(horizontal: false, vertical: true)
        .accessibilityElement(children: .combine)
    }
}
