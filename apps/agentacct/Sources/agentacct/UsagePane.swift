import SwiftUI

// One decision surface: provider-reported capacity first, independently ranged
// recorded usage second. The two lanes share client rows but never a denominator,
// freshness claim, error state, or range control.
struct UsagePane: View {
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
            .padding(Space.gutter)
            .frame(maxWidth: 1172 + Space.gutter * 2, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .center)
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Usage & limits")
                .font(Type.titlePage).tracking(Type.titlePageTracking)
                .foregroundStyle(Theme.ink)
            Text("Provider-reported capacity and locally recorded usage")
                .font(Type.dataSmall).foregroundStyle(Theme.muted)
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
                        .font(Type.titleSection).tracking(Type.titleSectionTracking)
                        .foregroundStyle(Theme.ink)
                    if let updated = glance.lastUpdated {
                        Text("capacity refreshed \(dashboardFreshnessText(updated))")
                            .font(Type.dataSmall).foregroundStyle(Theme.muted)
                    }
                    if dashboard.usage != nil {
                        Text(dashboard.usageLastUpdated.map {
                            "recorded use refreshed \(dashboardFreshnessText($0))"
                        } ?? "recorded use update time unavailable")
                            .font(Type.dataSmall).foregroundStyle(Theme.muted)
                    }
                    Spacer()
                    staleControl(count: snapshot.glance.limits.filter { $0.stale == true }.count)
                }
                Text("Provider-reported usage allowance. agentacct does not enforce a spending budget or stop work.")
                    .font(Type.caption).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)

                if let today = snapshot.glance.usage.windows.first(where: { $0.label == "today" })?.totals {
                    todayStrip(today)
                }

                if presentation.rows.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(dashboard.usage == nil
                             ? "No live capacity reported yet"
                             : "No live capacity or recorded usage in this range")
                            .font(Type.rowLabel).foregroundStyle(Theme.ink)
                        Text(capacityEmptyDetail(presentation: presentation))
                            .font(Type.caption).foregroundStyle(Theme.muted)
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
            scopedCapacityState(
                title: "Live limits unavailable — daemon disconnected",
                detail: message
            )
        case .incompatible(let message):
            scopedCapacityState(title: "Live limits unavailable — incompatible daemon", detail: message)
        }
    }

    private func staleControl(count: Int) -> some View {
        Group {
            if count > 0 {
                if SnapshotMode.enabled {
                    Chip(text: "\(count) stale hidden", tint: Theme.amber)
                } else {
                    Toggle("Show \(count) stale capacity reading\(count == 1 ? "" : "s")", isOn: $showStale)
                        .toggleStyle(.checkbox)
                        .font(Type.caption)
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
                .font(Type.titleSection).tracking(Type.titleSectionTracking)
                .foregroundStyle(Theme.ink)
            Text("Provider-reported usage allowance. agentacct does not enforce a spending budget or stop work.")
                .font(Type.caption).foregroundStyle(Theme.muted)
            Card {
                VStack(alignment: .leading, spacing: 4) {
                    Text(title).font(Type.rowLabel).foregroundStyle(Theme.ink)
                    Text(detail).font(Type.caption).foregroundStyle(Theme.muted)
                }
            }
        }
    }

    private func todayStrip(_ totals: UsageTotals) -> some View {
        HStack(spacing: Space.l) {
            CapsLabel(text: "Today · all agents")
            Text(totals.freshTokens.map(UsageTotals.compact) ?? "Tokens not reported")
                .font(Type.dataSmallSemibold)
                .foregroundStyle(totals.freshTokens == nil ? Theme.muted : Theme.ink)
            if totals.freshTokens != nil {
                Text("fresh tokens").font(Type.caption).foregroundStyle(Theme.muted)
            }
            Rectangle().fill(Theme.hairline).frame(width: 1, height: 20)
            Text(totals.costText == "—" ? "Cost unpriced" : totals.costText)
                .font(Type.dataSmallSemibold)
                .foregroundStyle(totals.costText == "—" ? Theme.muted : Theme.ink)
            Text(Fmt.costConfidenceLabel(totals.costConfidence) ?? "cost basis not reported")
                .font(Type.caption).foregroundStyle(Theme.muted)
            Spacer()
        }
        .padding(.horizontal, Space.l)
        .frame(minHeight: 44)
        .background(Theme.tintNeutral.opacity(0.45), in: RoundedRectangle(cornerRadius: Metrics.radius))
    }

    @ViewBuilder
    private var recordedUsageSection: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            HStack(alignment: .center, spacing: Space.m) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Recorded usage")
                        .font(Type.titleSection).tracking(Type.titleSectionTracking)
                        .foregroundStyle(Theme.ink)
                    HStack(spacing: 0) {
                        Text("Range applies only to totals, history, and attribution")
                            .font(Type.dataSmall).foregroundStyle(Theme.muted)
                        if let updated = dashboard.usageLastUpdated {
                            Text(" · usage refreshed \(dashboardFreshnessText(updated))")
                                .font(Type.dataSmall).foregroundStyle(Theme.muted)
                        }
                    }
                }
                Spacer()
                usageRangeControl
            }

            Text("Cost is usage reporting, not a provider invoice or balance due. Verify charges with your provider.")
                .font(Type.caption).foregroundStyle(Theme.muted)

            if let error = dashboard.errorText {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(Type.caption).foregroundStyle(Theme.coral)
            }

            if let usage = dashboard.usage {
                summaryStrip(usage)
                if let periods = usage.byPeriod, periods.count > 1 {
                    UsagePeriodChart(
                        periods: periods,
                        presentation: UsagePeriodPresentation(usage: usage)
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
                    rows: usage.byModel.map { ($0.model ?? "Unattributed model", $0) }
                )
                basisFooter(usage)
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Recorded usage not loaded").font(Type.rowLabel).foregroundStyle(Theme.ink)
                    Text("Capacity may still be available above while the usage summary loads.")
                        .font(Type.caption).foregroundStyle(Theme.muted)
                }
                .padding(.vertical, Space.s)
            }
        }
    }

    private var capacityIsConnected: Bool {
        if case .connected = glance.phase { return true }
        return false
    }

    @ViewBuilder
    private var usageRangeControl: some View {
        if SnapshotMode.enabled {
            HStack(spacing: Space.s) {
                CapsLabel(text: "Usage range")
                Chip(text: "\(dashboard.usageDays)d", tint: Theme.accent)
            }
        } else {
            Picker("Usage range", selection: Binding(
                get: { dashboard.usageDays },
                set: { days in Task { await dashboard.setUsageDays(days) } }
            )) {
                Text("7d").tag(7)
                Text("30d").tag(30)
                Text("90d").tag(90)
            }
            .pickerStyle(.segmented)
            .frame(width: 190)
            .accessibilityIdentifier("usage.history.range")
        }
    }

    private func summaryStrip(_ usage: UsageSummary) -> some View {
        let totals = usage.totals
        let activity = UsagePeriodPresentation(usage: usage)

        return StripRow(cells: [
            StripRow.Cell(
                id: "tokens",
                label: "Tokens",
                value: totals?.freshTokens.map(UsageTotals.compact),
                qualifier: "fresh · client-reported",
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
                value: totals.flatMap { $0.costText == "—" ? nil : $0.costText },
                qualifier: Fmt.costConfidenceLabel(totals?.costConfidence),
                absent: "no priced usage"
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
            Fmt.costConfidenceLabel(usage.totals?.costConfidence).map { "cost: \($0)" },
            "token counts come from client usage records",
            usage.totals?.cacheReadTokens.map {
                "fresh tokens exclude \(UsageTotals.compact($0)) cache-read tokens"
            },
        ].compactMap { $0 }
        Text(parts.joined(separator: " · "))
            .font(Type.dataSmall).foregroundStyle(Theme.muted)
            .fixedSize(horizontal: false, vertical: true)
    }

    @ViewBuilder
    private var aboutSection: some View {
        if SnapshotMode.enabled {
            Card {
                HStack {
                    Image(systemName: "chevron.right").font(.system(size: 10, weight: .semibold))
                    Text("About these numbers").font(Type.rowLabel).foregroundStyle(Theme.ink)
                    Spacer()
                    Text("cost, windows, and plan calibration")
                        .font(Type.dataSmall).foregroundStyle(Theme.muted)
                }
            }
        } else {
            Card {
                DisclosureGroup(isExpanded: $showAbout) {
                    aboutDetails.padding(.top, Space.l)
                } label: {
                    HStack {
                        Text("About these numbers").font(Type.rowLabel).foregroundStyle(Theme.ink)
                        Spacer()
                        Text("cost, windows, and plan calibration")
                            .font(Type.dataSmall).foregroundStyle(Theme.muted)
                    }
                }
                .accessibilityIdentifier("usage.about")
            }
        }
    }

    private var aboutDetails: some View {
        VStack(alignment: .leading, spacing: Space.l) {
            VStack(alignment: .leading, spacing: 6) {
                CapsLabel(text: "Cost grammar")
                Text("$ complete reported or billed · ≈$ estimate · ~$ known partial subtotal · unpriced when no amount is available")
                    .font(Type.caption).foregroundStyle(Theme.muted)
            }
            Rectangle().fill(Theme.hairline).frame(height: 1)
            VStack(alignment: .leading, spacing: 6) {
                CapsLabel(text: "Provider windows")
                Text("Rolling windows follow each client's activity; fixed windows reset at the provider's stated time. Attention markers sit at 75% and 90% used.")
                    .font(Type.caption).foregroundStyle(Theme.muted)
            }
            if !dashboard.planClients.isEmpty {
                Rectangle().fill(Theme.hairline).frame(height: 1)
                VStack(alignment: .leading, spacing: Space.m) {
                    CapsLabel(text: "Weekly plan-share estimates")
                    Text("Today and 7d estimates stay fixed; the selected usage range applies only to each model accumulation below.")
                        .font(Type.caption).foregroundStyle(Theme.muted)
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

    init(usage: UsageSummary) {
        switch usage.filtersEcho?.granularity {
        case "daily":
            unit = "day"
            label = "Active days"
            absent = "no daily series"
        case "weekly":
            unit = "week"
            label = "Active weeks"
            absent = "no weekly series"
        default:
            unit = "period"
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
}

private struct UsagePlanClientDetail: View {
    let client: V1PlanClient
    let days: Int
    @State private var showDaily = false

    private var presentation: UsagePlanPresentation {
        UsagePlanPresentation(client: client, days: days)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            HStack(alignment: .firstTextBaseline, spacing: Space.m) {
                Text(client.client).font(Type.rowLabel).foregroundStyle(Theme.ink)
                    .frame(width: 140, alignment: .leading)
                Text(presentation.detailText)
                    .font(Type.caption).foregroundStyle(Theme.muted)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            if let daily = presentation.dailyText {
                if presentation.dailyRows.isEmpty {
                    Text(daily).font(Type.caption).foregroundStyle(Theme.muted)
                } else {
                    DisclosureGroup(isExpanded: $showDaily) {
                        LazyVStack(alignment: .leading, spacing: 5) {
                            ForEach(Array(presentation.dailyRows.enumerated()), id: \.offset) { _, row in
                                Text(row).font(Type.caption).foregroundStyle(Theme.muted)
                            }
                        }
                        .padding(.top, Space.s)
                    } label: {
                        Text(daily).font(Type.captionSemibold).foregroundStyle(Theme.ink)
                    }
                    .accessibilityIdentifier("usage.plan.daily.\(client.client)")
                }
            }
            if !presentation.modelRows.isEmpty {
                CapsLabel(text: presentation.modelHeading)
                ScrollContentStack(alignment: .leading, spacing: Space.s) {
                    ForEach(Array(presentation.modelRows.enumerated()), id: \.offset) { _, row in
                        Text(row).font(Type.caption).foregroundStyle(Theme.muted)
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
                                Text(value).font(Type.kpi).foregroundStyle(Theme.ink)
                                if let qualifier = cell.qualifier {
                                    Text(qualifier).font(Type.dataSmall).foregroundStyle(Theme.muted)
                                        .lineLimit(1)
                                }
                            }
                        } else {
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

// MARK: - Period chart (one series per chart)

/// The usage chart: estimated cost or fresh tokens per API-reported period, with
/// an optional single-client group filter. Always ONE series — a stack never
/// appears (v7 chart discipline). Periods without a priced value render as flat
/// neutral stubs and their tooltip says so; heights are strictly proportional.
struct UsagePeriodChart: View {
    let periods: [PeriodBucket]
    let presentation: UsagePeriodPresentation

    enum Series: String, CaseIterable, Identifiable {
        case cost = "Cost"
        case tokens = "Fresh tokens"
        var id: String { rawValue }
    }

    @State private var series: Series
    /// nil = all clients; set = one client's token series (tokens mode only).
    @State private var group: String?
    @State private var hoveredIndex: Int?
    @State private var selectedIndex: Int?
    @FocusState private var focusedIndex: Int?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    init(periods: [PeriodBucket], presentation: UsagePeriodPresentation) {
        self.periods = periods
        self.presentation = presentation
        let initialSeries: Series = periods.contains { $0.estimatedCostUsd != nil } ? .cost : .tokens
        _series = State(initialValue: initialSeries)
        _selectedIndex = State(initialValue: periods.indices.last)
    }

    private var clients: [String] {
        var seen: Set<String> = []
        for period in periods {
            seen.formUnion((period.byClient ?? [:]).keys)
        }
        return seen.sorted()
    }

    /// The plotted value for a period, or nil when it has no value to plot
    /// (an unpriced period is NOT a $0 period).
    private func value(_ period: PeriodBucket) -> Double? {
        switch series {
        case .cost:
            return period.estimatedCostUsd
        case .tokens:
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

    private func valueText(_ period: PeriodBucket) -> String {
        switch series {
        case .cost:
            return period.costText == "—" ? "unpriced" : period.costText
        case .tokens:
            if let tokens = value(period) { return UsageTotals.compact(Int(tokens)) }
            return "none recorded"
        }
    }

    private func axisText(_ fraction: Double) -> String {
        switch series {
        case .cost:
            return "$" + String(format: maxValue * fraction >= 10 ? "%.0f" : "%.2f", maxValue * fraction)
        case .tokens:
            return UsageTotals.compact(Int(maxValue * fraction))
        }
    }

    /// Plot height: the top gridline is exactly the max value's line.
    private static let plotHeight: CGFloat = 128

    /// The peak annotation, centered over the peak bar in its own band.
    @ViewBuilder
    private var peakBand: some View {
        HStack(alignment: .bottom, spacing: 3) {
            ForEach(Array(periods.enumerated()), id: \.offset) { index, period in
                Group {
                    if index == peakIndex, let peak = value(period), peak > 0 {
                        Text("peak \(valueText(period))")
                            .font(Type.dataSmall)
                            .foregroundStyle(Theme.muted)
                            .fixedSize()
                    } else {
                        Color.clear.frame(height: 1)
                    }
                }
                .frame(maxWidth: .infinity)
            }
        }
        .frame(height: 20, alignment: .bottom)
        .padding(.bottom, 2)
    }

    private var chartTitle: String {
        switch series {
        case .cost: return presentation.costChartTitle
        case .tokens: return presentation.tokenChartTitle(group: group)
        }
    }

    var body: some View {
        Card(padding: 0) {
            VStack(spacing: 0) {
                HStack(spacing: Space.m) {
                    Text(chartTitle).font(Type.titleCard).foregroundStyle(Theme.ink)
                    Spacer()
                    if let selectedIndex, periods.indices.contains(selectedIndex), !SnapshotMode.enabled {
                        Button {
                            self.selectedIndex = max(0, selectedIndex - 1)
                        } label: {
                            Image(systemName: "chevron.left")
                                .frame(
                                    width: ButtonFeedback.minimumHitDimension,
                                    height: ButtonFeedback.minimumHitDimension
                                )
                        }
                        .buttonStyle(QuietButtonStyle(
                            horizontalPadding: 2,
                            verticalPadding: 2
                        ))
                        .disabled(selectedIndex == periods.startIndex)
                        .accessibilityLabel(presentation.previousAccessibilityLabel)

                        Text("\(periods[selectedIndex].shortLabel) · \(valueText(periods[selectedIndex]))")
                            .font(Type.dataSmallSemibold)
                            .foregroundStyle(Theme.ink)
                            .frame(minWidth: 112)

                        Button {
                            self.selectedIndex = min(periods.index(before: periods.endIndex), selectedIndex + 1)
                        } label: {
                            Image(systemName: "chevron.right")
                                .frame(
                                    width: ButtonFeedback.minimumHitDimension,
                                    height: ButtonFeedback.minimumHitDimension
                                )
                        }
                        .buttonStyle(QuietButtonStyle(
                            horizontalPadding: 2,
                            verticalPadding: 2
                        ))
                        .disabled(selectedIndex == periods.index(before: periods.endIndex))
                        .accessibilityLabel(presentation.nextAccessibilityLabel)
                    }
                    if SnapshotMode.enabled {
                        Chip(text: series.rawValue, tint: Theme.accent)
                    } else {
                        CapsLabel(text: "Measure")
                        Picker("Chart measure", selection: $series) {
                            ForEach(Series.allCases) { Text($0.rawValue).tag($0) }
                        }
                        .pickerStyle(.segmented)
                        .fixedSize()
                        .labelsHidden()
                        .accessibilityIdentifier("usage.history.measure")
                        if series == .tokens, clients.count > 1 {
                            Picker("Group", selection: $group) {
                                Text("All clients").tag(String?.none)
                                ForEach(clients, id: \.self) { Text($0).tag(String?.some($0)) }
                            }
                            .pickerStyle(.menu)
                            .fixedSize()
                        }
                    }
                }
                .padding(.horizontal, Space.xl)
                .padding(.vertical, Space.l)

                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.horizontal, Space.xl)

                VStack(spacing: 7) {
                    HStack(alignment: .bottom, spacing: Space.s) {
                        // Axis labels sit centered ON their gridlines: one
                        // linear scale where the max value IS the top line.
                        ZStack(alignment: .bottomTrailing) {
                            Text(axisText(1)).offset(y: -Self.plotHeight + 7)
                            Text(axisText(0.5)).offset(y: -Self.plotHeight / 2 + 7)
                            Text("0")
                        }
                        .font(Type.dataSmall)
                        .foregroundStyle(Theme.muted)
                        .frame(width: 44, height: Self.plotHeight, alignment: .bottomTrailing)

                        VStack(spacing: 0) {
                            // A reserved band above the plot keeps the peak
                            // annotation from stealing plot height.
                            peakBand
                            ZStack(alignment: .bottomLeading) {
                                Rectangle().fill(Theme.hairline).frame(height: 1)
                                    .offset(y: -Self.plotHeight + 0.5)
                                Rectangle().fill(Theme.hairline).frame(height: 1)
                                    .offset(y: -Self.plotHeight / 2)
                                Rectangle().fill(Theme.hairline).frame(height: 1)

                                HStack(alignment: .bottom, spacing: 3) {
                                    ForEach(Array(periods.enumerated()), id: \.offset) { index, period in
                                        Button {
                                            selectedIndex = index
                                        } label: {
                                            Group {
                                                if let dayValue = value(period) {
                                                    // Strictly linear: the peak bar
                                                    // touches the line its label names.
                                                    RoundedRectangle(cornerRadius: 2)
                                                        .fill((hoveredIndex ?? focusedIndex ?? selectedIndex) == nil
                                                              || (hoveredIndex ?? focusedIndex ?? selectedIndex) == index
                                                              ? Theme.chartBar : Theme.chartBarDim)
                                                        .frame(height: max(1, Self.plotHeight * dayValue / maxValue))
                                                } else {
                                                    // A period with no plottable value: a flat
                                                    // neutral stub, never a zero-height lie.
                                                    RoundedRectangle(cornerRadius: 1)
                                                        .fill(Theme.tintNeutral)
                                                        .frame(height: 3)
                                                }
                                            }
                                            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
                                            .contentShape(Rectangle())
                                        }
                                        // Preserve proportional 7/30/90-day geometry. The
                                        // 28pt Previous/Next controls above are equivalent
                                        // controls for reaching every period at a large target.
                                        .buttonStyle(TransparentButtonStyle(cornerRadius: 2))
                                        .focused($focusedIndex, equals: index)
                                        .onHover { inside in
                                            if inside {
                                                hoveredIndex = index
                                            } else if hoveredIndex == index {
                                                hoveredIndex = nil
                                            }
                                        }
                                        .accessibilityLabel(
                                            "\(period.period ?? period.shortLabel), \(valueText(period))"
                                        )
                                        .accessibilityHint(presentation.selectionAccessibilityHint)
                                        .accessibilityAddTraits(selectedIndex == index ? .isSelected : [])
                                        .accessibilityIdentifier("usage.history.day.\(index)")
                                    }
                                }
                            }
                            .frame(height: Self.plotHeight)
                        }
                        .animation(chartGeometryAnimation, value: series)
                        .animation(chartGeometryAnimation, value: group)
                        .overlay(alignment: .topLeading) {
                            if let activeIndex = hoveredIndex ?? focusedIndex ?? selectedIndex,
                               periods.indices.contains(activeIndex) {
                                let hovered = periods[activeIndex]
                                HStack(spacing: 6) {
                                    Text(hovered.shortLabel)
                                        .font(Type.dataSmallSemibold).foregroundStyle(Theme.ink)
                                    Text(valueText(hovered))
                                        .font(Type.dataSmall).foregroundStyle(Theme.muted)
                                }
                                .padding(.horizontal, 8)
                                .padding(.vertical, 5)
                                .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
                                .overlay(RoundedRectangle(cornerRadius: Metrics.radius)
                                    .strokeBorder(Theme.cardLine, lineWidth: Metrics.borderW))
                                .padding(4)
                                .allowsHitTesting(false)
                            }
                        }
                    }

                    HStack(spacing: Space.s) {
                        Color.clear.frame(width: 44, height: 1)
                        HStack {
                            Text(periods.first?.shortLabel ?? "")
                            Spacer()
                            Text(periods[periods.count / 2].shortLabel)
                            Spacer()
                            Text(periods.last?.shortLabel ?? "")
                        }
                    }
                    .font(Type.dataSmall)
                    .foregroundStyle(Theme.muted)
                }
                .padding(Space.xl)
                .animation(Motion.hover, value: hoveredIndex)
            }
        }
        .onChange(of: periods.map(\.period)) {
            hoveredIndex = nil
            focusedIndex = nil
            selectedIndex = periods.indices.last
            if series == .cost && !periods.contains(where: { $0.estimatedCostUsd != nil }) {
                series = .tokens
            }
        }
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
                    Text(title).font(Type.titleCard).foregroundStyle(Theme.ink)
                    Text("last \(days) days").font(Type.dataSmall).foregroundStyle(Theme.muted)
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
                        .font(Type.body).foregroundStyle(Theme.muted)
                        .padding(Space.xl)
                        .frame(maxWidth: .infinity, alignment: .leading)
                } else {
                    ScrollContentStack(spacing: 0) { populatedRows }
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
        return HStack(spacing: Space.l) {
            Text(name)
                .font(Type.rowLabel).foregroundStyle(Theme.ink)
                .lineLimit(1).truncationMode(.middle)
                .help(name)
                .frame(maxWidth: .infinity, alignment: .leading)
            Text(bucket.sessions.map(String.init) ?? "not reported")
                .font(Type.dataSmall).foregroundStyle(bucket.sessions == nil ? Theme.muted : Theme.ink)
                .frame(width: 76, alignment: .trailing)
            Text(bucket.freshTokens.map(UsageTotals.compact) ?? "not reported")
                .font(Type.dataSmall).foregroundStyle(bucket.freshTokens == nil ? Theme.muted : Theme.ink)
                .frame(width: 76, alignment: .trailing)
            Group {
                if let share {
                    HStack(spacing: Space.s) {
                        MeterBar(fraction: share, tint: Theme.chartBar, height: 6)
                            .frame(width: 80)
                        Text("\(Int((share * 100).rounded()))%")
                            .font(Type.dataSmall).foregroundStyle(Theme.ink)
                            .frame(width: 40, alignment: .leading)
                    }
                } else {
                    Text("not reported")
                        .font(Type.dataSmall).foregroundStyle(Theme.muted)
                }
            }
            .frame(width: 140, alignment: .leading)
            Text(bucket.costText == "—" ? "unpriced" : bucket.costText)
                .font(Type.dataSmall)
                .foregroundStyle(bucket.costText == "—" ? Theme.muted : Theme.ink)
                .frame(width: 90, alignment: .trailing)
        }
        .padding(.horizontal, Space.xl)
        .frame(minHeight: Metrics.rowTable)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(
            [
                name,
                bucket.sessions.map { "\($0) sessions" } ?? "sessions not reported",
                bucket.freshTokens.map { "\($0) fresh tokens" } ?? "tokens not reported",
                share.map { "\(Int(($0 * 100).rounded())) percent of known fresh tokens" }
                    ?? "token share not reported",
                bucket.costText == "—" ? "cost unpriced" : bucket.costText,
                Fmt.costConfidenceLabel(bucket.costConfidence),
            ].compactMap { $0 }.joined(separator: ", ")
        )
    }
}
