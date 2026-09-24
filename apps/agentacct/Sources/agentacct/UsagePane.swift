import SwiftUI

// Recorded token traffic is the primary surface. Provider quota windows stay
// separate, below the date/client/model ledger.
struct UsagePane: View {
    @Environment(DashboardStore.self) var dashboard
    @Environment(GlanceState.self) var glance
    @AppStorage("usage.tokenBasis") private var savedTokenBasis: UsageTokenBasis = .fresh

    private var tokenBasis: UsageTokenBasis {
        SnapshotMode.enabled && !SnapshotMode.interactiveFixture ? SnapshotMode.usageTokenBasis : savedTokenBasis
    }

    @State private var showStale = false
    @State private var showAbout = false
    @State private var showCapacity = false

    var body: some View {
        ScrollBox {
            VStack(alignment: .leading, spacing: 0) {
                header
                UsageFilterBar().padding(.top, Space.l)
                recordedUsageSection.padding(.top, Space.l)
                DisclosureGroup(isExpanded: $showCapacity) {
                    capacitySection.padding(.top, Space.m)
                } label: {
                    HStack {
                        Text("Provider limits").workFont(.rowLabel).foregroundStyle(Theme.ink)
                        Spacer()
                        Text("Live capacity · separate provider windows").workFont(.caption).foregroundStyle(Theme.muted)
                    }
                }
                .accessibilityIdentifier("usage.capacity.disclosure")
                .padding(.top, Space.xl)
                aboutSection.padding(.top, Space.xl)
            }
            .padding(Space.gutter)
            .frame(maxWidth: 1172 + Space.gutter * 2, alignment: .leading)
            // Left-align the capped content column to match Work and Sources
            // (WorkPane .leading, SourcesPane .leading). Centering here made the
            // whole pane slide sideways on wide windows when switching tabs.
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .environment(\.usageTokenBasis, tokenBasis)
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: Space.m) {
            Text("Usage").workFont(.titlePage).tracking(Type.titlePageTracking).foregroundStyle(Theme.ink)
            Text("Recorded tokens and cost").workFont(.caption).foregroundStyle(Theme.muted)
            Spacer()
        }
    }

    /// The capacity help carries the two finer freshness stamps the title
    /// row used to print beside the toolbar's page-level stamp.
    private var capacityHelpMessage: String {
        var parts = ["Provider-reported usage allowance. agentacct does not enforce a spending budget or stop work."]
        if let updated = glance.lastUpdated {
            parts.append("Capacity refreshed \(dashboardFreshnessText(updated)).")
        }
        if dashboard.usage != nil {
            parts.append(dashboard.usageLastUpdated.map {
                "Recorded use refreshed \(dashboardFreshnessText($0))."
            } ?? "Recorded use update time unavailable.")
        }
        return parts.joined(separator: " ")
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
                    // The toolbar already stamps the page's freshness; the two
                    // finer stamps (capacity vs recorded use) live in the help
                    // where a reader who needs them looks, not beside the title.
                    ContextHelp(title: "About current capacity", message: capacityHelpMessage, identifier: "usage.capacity-help")
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
                        days: dashboard.usagePlanDays,
                        recordedUseRange: recordedUseRangeText,
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
                if SnapshotMode.enabled && !SnapshotMode.interactiveFixture {
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

    /// The capacity ledger's recorded-use column is scoped to the page's
    /// filter, so its header names that filter — an all-time or narrowed read
    /// must not wear the plan lane's day count.
    private var recordedUseRangeText: String {
        UsageFilterSummary.build(filter: dashboard.usageFilter, echo: dashboard.usage?.filtersEcho).text
    }

    private func scopedCapacityState(title: String, detail: String) -> some View {
        VStack(alignment: .leading, spacing: Space.m) {
            HStack(alignment: .firstTextBaseline, spacing: Space.m) {
                Text("Current capacity")
                    .workFont(.titleSection).tracking(Type.titleSectionTracking)
                    .foregroundStyle(Theme.ink)
                // The definition lives in help here too, matching the loaded
                // state, instead of a caption under the title.
                ContextHelp(title: "About current capacity", message: capacityHelpMessage, identifier: "usage.capacity-help")
            }
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
            Text(tokenBasis.value(totals).map(UsageTotals.compact) ?? "Tokens not reported")
                .workFont(.dataSmallSemibold)
                .foregroundStyle(tokenBasis.value(totals) == nil ? Theme.muted : Theme.ink)
            if tokenBasis.value(totals) != nil {
                Text(tokenBasis.label.lowercased()).workFont(.caption).foregroundStyle(Theme.muted)
            }
            Rectangle().fill(Theme.hairline).frame(width: 1, height: 20)
            Text(totals.costText == "—" ? "Cost unpriced" : totals.costText)
                .workFont(.dataSmallSemibold)
                .foregroundStyle(totals.costText == "—" ? Theme.muted : Theme.ink)
            Text(Fmt.costConfidenceLabel(totals.costConfidence) ?? "cost basis not reported")
                .workFont(.caption).foregroundStyle(Theme.muted)
            Spacer()
        }
        .padding(.horizontal, Space.l)
        .frame(minHeight: 44)
        .background(Theme.tintNeutral.opacity(0.45), in: RoundedRectangle(cornerRadius: Metrics.radius))
    }

    @ViewBuilder
    private var recordedUsageSection: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            if let error = dashboard.errorText {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .workFont(.caption).foregroundStyle(Theme.coral)
            }
            if let usage = dashboard.usage {
                filterSummaryLine(usage)
                if let mismatch = dashboard.usageFilterMismatch {
                    filteredNumbersHidden(mismatch)
                } else if usage.hasNoSavedRows {
                    emptyFilterState(usage)
                } else {
                    UsageRecordedExplorer(usage: usage, rangeLabel: dashboard.usageRangeLabel,
                        tokenBasis: Binding(get: { tokenBasis }, set: { savedTokenBasis = $0 }))
                }
            } else {
                Text("Recorded usage not loaded").workFont(.rowLabel).foregroundStyle(Theme.ink)
                Text("The local recorder is loading token and cost records.")
                    .workFont(.caption).foregroundStyle(Theme.muted)
            }
        }
    }

    /// What every number below is scoped to, in the filter bar's own order.
    /// The store owns the wording (see ``UsageFilterSummary``); this only shows
    /// it, so a filtered page can never be mistaken for an unfiltered one.
    private func filterSummaryLine(_ usage: UsageSummary) -> some View {
        let summary = UsageFilterSummary.build(filter: dashboard.usageFilter, echo: usage.filtersEcho)
        return HStack(alignment: .firstTextBaseline, spacing: Space.s) {
            HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                CapsLabel(text: "Showing")
                Text(summary.text).workFont(.captionSemibold).foregroundStyle(Theme.ink)
            }
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("usage.filters.summary")
            ContextHelp(
                title: "About these filters",
                message: filterHelpMessage(usage, summary: summary),
                identifier: "usage.filters.help"
            )
            Spacer()
        }
    }

    private func filterHelpMessage(_ usage: UsageSummary, summary: UsageFilterSummary) -> String {
        var parts = [
            "Every number below comes from one /usage/summary read with these filters: date range, agent, model and provider, each applied independently and combined with AND. Nothing here is re-added or rescaled by the app.",
            "This page is showing \(summary.text).",
        ]
        if let resolved = usage.filtersEcho?.resolvedRange {
            parts.append("The recorder resolved the date range to \(resolved).")
        } else {
            parts.append("The recorder did not report the window it resolved.")
        }
        if dashboard.usageFilter.model != UsageFilter.anyValue {
            parts.append(echoVerdict("model", usage.filtersEcho?.modelMatchesSavedRows, value: dashboard.usageFilter.model))
        }
        if dashboard.usageFilter.provider != UsageFilter.anyValue {
            parts.append(echoVerdict("provider", usage.filtersEcho?.providerMatchesSavedRows, value: dashboard.usageFilter.provider))
        }
        return parts.joined(separator: " ")
    }

    /// The daemon's own verdict on a value filter, quoted as given: an absent
    /// flag is named absent rather than read as a match.
    private func echoVerdict(_ field: String, _ matches: Bool?, value: String) -> String {
        switch matches {
        case true: return "The recorder's filter echo confirms the \(field) filter “\(value)” matches saved rows."
        case false: return "The recorder's filter echo reports the \(field) filter “\(value)” matches no saved row."
        case nil: return "The recorder did not report whether the \(field) filter “\(value)” matches saved rows."
        }
    }

    /// The payload's own `filters_echo` did not confirm this filter, so its
    /// numbers may cover a wider set of rows than the filter asks for. They are
    /// named as hidden rather than shown under a label they do not match.
    private func filteredNumbersHidden(_ mismatch: UsageFilterMismatch) -> some View {
        Card {
            VStack(alignment: .leading, spacing: 4) {
                Text("Filtered numbers unavailable").workFont(.rowLabel).foregroundStyle(Theme.ink)
                Text(mismatch.detail)
                    .workFont(.caption).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityIdentifier("usage.filters.mismatch")
    }

    /// No saved row matches the committed filter. The copy names the value the
    /// recorder itself reported missing, and never renders a zero total as if
    /// the range had data.
    private func emptyFilterState(_ usage: UsageSummary) -> some View {
        let state = UsageFilterEmptyState.build(filter: dashboard.usageFilter, echo: usage.filtersEcho)
        return Card {
            VStack(alignment: .leading, spacing: 4) {
                Text(state.title).workFont(.rowLabel).foregroundStyle(Theme.ink)
                Text(state.detail)
                    .workFont(.caption).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityIdentifier("usage.filters.empty")
    }

    /// The basis facts for the loaded range. They used to trail the page as
    /// a fourth disclaimer line; they now open the About disclosure, where
    /// the rest of the numbers' definitions already live.
    private func basisText(_ usage: UsageSummary) -> String {
        let parts: [String] = [
            Fmt.costConfidenceLabel(usage.totals?.costConfidence).map { "cost: \($0)" },
            "token counts come from client usage records",
            "\(tokenBasis.label.lowercased()): \(tokenBasis.qualifier)",
        ].compactMap { $0 }
        return parts.joined(separator: " · ")
    }

    @ViewBuilder
    private var aboutSection: some View {
        if SnapshotMode.enabled && !SnapshotMode.interactiveFixture {
            Card {
                VStack(alignment: .leading, spacing: 0) {
                    HStack {
                        Image(systemName: SnapshotMode.expandsUsageAbout ? "chevron.down" : "chevron.right")
                            .font(.system(size: 10, weight: .semibold))
                        Text("About these numbers").workFont(.rowLabel).foregroundStyle(Theme.ink)
                        Spacer()
                        Text("cost, windows, and plan calibration")
                            .workFont(.dataSmall).foregroundStyle(Theme.muted)
                    }
                    if SnapshotMode.expandsUsageAbout {
                        aboutDetails.padding(.top, Space.l)
                    }
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
                .accessibilityIdentifier("usage.about")
            }
        }
    }

    private var aboutDetails: some View {
        VStack(alignment: .leading, spacing: Space.l) {
            if let usage = dashboard.usage {
                VStack(alignment: .leading, spacing: 6) {
                    CapsLabel(text: "This range")
                    Text(basisText(usage))
                        .workFont(.caption).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Rectangle().fill(Theme.hairline).frame(height: 1)
            }
            VStack(alignment: .leading, spacing: 6) {
                CapsLabel(text: "Token counting")
                Text(UsageTokenBasis.explanation)
                    .workFont(.caption).foregroundStyle(Theme.muted)
            }
            Rectangle().fill(Theme.hairline).frame(height: 1)
            VStack(alignment: .leading, spacing: 6) {
                CapsLabel(text: "Cost grammar")
                Text("$ complete reported or billed · ≈$ estimate · ~$ known partial subtotal · unpriced when no amount is available")
                    .workFont(.caption).foregroundStyle(Theme.muted)
            }
            Rectangle().fill(Theme.hairline).frame(height: 1)
            VStack(alignment: .leading, spacing: 6) {
                CapsLabel(text: "Provider windows")
                Text("Rolling windows follow each client's activity; fixed windows reset at the provider's stated time. Attention markers sit at 75% and 90% used.")
                    .workFont(.caption).foregroundStyle(Theme.muted)
            }
            if !dashboard.planClients.isEmpty {
                Rectangle().fill(Theme.hairline).frame(height: 1)
                VStack(alignment: .leading, spacing: Space.m) {
                    CapsLabel(text: "Weekly plan-share estimates")
                    Text(planShareNote)
                        .workFont(.caption).foregroundStyle(Theme.muted)
                    ForEach(Array(dashboard.planClients.enumerated()), id: \.element.id) { index, client in
                        if index > 0 { Rectangle().fill(Theme.hairline).frame(height: 1) }
                        UsagePlanClientDetail(client: client, days: dashboard.usagePlanDays)
                    }
                }
            }
        }
    }

    /// The plan endpoint's own window, stated as such: the plan lane is not the
    /// usage filter, and the two only coincide while the filter fits inside its
    /// 1...90-day bound.
    private var planShareNote: String {
        var note = "Today and 7d estimates stay fixed. Each model accumulation below covers the plan endpoint's own window — the last \(dashboard.usagePlanDays) days (it accepts at most 90)."
        if !dashboard.usageFilter.range.fitsPlanWindow {
            note += " That window is narrower than the usage filter above (\(dashboard.usageRangeLabel.long)), which this lane cannot cover in full."
        }
        return note
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
            ($0.totalTokensIncludingCached ?? $0.freshTokens ?? 0) > 0 || $0.estimatedCostUsd != nil
        }.count
        value = periods.isEmpty ? nil : "\(activeCount)/\(periods.count)"
    }

    var costChartTitle: String { "Cost per \(unit)" }

    func tokenChartTitle(group: String?, basis: UsageTokenBasis = .fresh) -> String {
        let title = "\(basis.label) per \(unit)"
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

    private var presentation: UsagePlanPresentation {
        UsagePlanPresentation(client: client, days: days)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            HStack(alignment: .firstTextBaseline, spacing: Space.m) {
                Text(client.client).workFont(.rowLabel).foregroundStyle(Theme.ink)
                    .frame(width: 140, alignment: .leading)
                Text(presentation.detailText)
                    .workFont(.caption).foregroundStyle(Theme.muted)
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
                                    Text(qualifier).workFont(.dataSmall).foregroundStyle(Theme.muted)
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
/// appears (v7 chart discipline). Periods without a priced value render as flat
/// neutral stubs and their tooltip says so; heights are strictly proportional.
/// When the chart prints its peak label. The tooltip already names the bar
/// it is on, so the label yields whenever that bar is the peak; one value is
/// never printed twice.
enum UsageChartPeakLabel {
    static func isShown(peakIndex: Int?, activeIndex: Int?, peakValue: Double?) -> Bool {
        guard let peakIndex, let peakValue, peakValue > 0 else { return false }
        return activeIndex != peakIndex
    }
}

struct UsagePeriodChart: View {
    @Environment(\.usageTokenBasis) private var tokenBasis
    let periods: [PeriodBucket]
    let presentation: UsagePeriodPresentation

    enum Series: String, CaseIterable, Identifiable {
        case cost = "Cost"
        case tokens = "Tokens"
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
        let showsTokens = SnapshotMode.enabled && SnapshotMode.usageTokenBasis == .all
        let initialSeries: Series = !showsTokens && periods.contains { $0.estimatedCostUsd != nil } ? .cost : .tokens
        _series = State(initialValue: initialSeries)
        // Deterministic renders may pin the selection to show the tooltip on
        // a chosen bar; the live app starts on the newest period.
        let pinned = SnapshotMode.enabled ? SnapshotMode.usageChartSelectedIndex : nil
        _selectedIndex = State(initialValue: pinned.flatMap { periods.indices.contains($0) ? $0 : nil } ?? periods.indices.last)
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
                return tokenBasis.value(period.byClient?[group]).map(Double.init)
            }
            return tokenBasis.value(period).map(Double.init)
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
            if let tokens = value(period) { return UsageTotals.compact(tokens) }
            return "not reported"
        }
    }

    private func axisText(_ fraction: Double) -> String {
        switch series {
        case .cost:
            return "$" + String(format: maxValue * fraction >= 10 ? "%.0f" : "%.2f", maxValue * fraction)
        case .tokens:
            return UsageTotals.compact(maxValue * fraction)
        }
    }

    /// Plot height: the top gridline is exactly the max value's line.
    private static let plotHeight: CGFloat = 128

    /// The bar whose tooltip is showing (hover wins, then keyboard focus,
    /// then the click selection).
    private var activeIndex: Int? { hoveredIndex ?? focusedIndex ?? selectedIndex }

    /// The peak annotation, centered over the peak bar in its own band. It
    /// yields to the tooltip when that tooltip is already showing the peak
    /// bar, so one value is never printed twice.
    @ViewBuilder
    private var peakBand: some View {
        HStack(alignment: .bottom, spacing: 3) {
            ForEach(Array(periods.enumerated()), id: \.offset) { index, period in
                Group {
                    if index == peakIndex,
                       UsageChartPeakLabel.isShown(peakIndex: peakIndex, activeIndex: activeIndex, peakValue: value(period)) {
                        Text("peak \(valueText(period))")
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
        .frame(height: 20, alignment: .bottom)
        .padding(.bottom, 2)
    }

    private var chartTitle: String {
        switch series {
        case .cost: return presentation.costChartTitle
        case .tokens: return presentation.tokenChartTitle(group: group, basis: tokenBasis)
        }
    }

    var body: some View {
        Card(padding: 0) {
            VStack(spacing: 0) {
                HStack(spacing: Space.m) {
                    Text(chartTitle).workFont(.titleCard).foregroundStyle(Theme.ink)
                    Spacer()
                    if let selectedIndex, periods.indices.contains(selectedIndex), (!SnapshotMode.enabled || SnapshotMode.interactiveFixture) {
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
                            .workFont(.dataSmallSemibold)
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
                    if SnapshotMode.enabled && !SnapshotMode.interactiveFixture {
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
                        .workFont(.dataSmall)
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
                                        .workFont(.dataSmallSemibold).foregroundStyle(Theme.ink)
                                    Text(valueText(hovered))
                                        .workFont(.dataSmall).foregroundStyle(Theme.muted)
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
                    .workFont(.dataSmall)
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
/// by the selected token basis, share bars strictly proportional to the table's own total.
struct UsageBreakdownTable: View {
    @Environment(\.usageTokenBasis) private var tokenBasis
    let title: String
    let nameHeader: String
    let days: Int
    let rows: [(name: String, bucket: UsageBucket)]

    private var sorted: [(name: String, bucket: UsageBucket)] {
        rows.sorted { left, right in
            switch (tokenBasis.value(left.bucket), tokenBasis.value(right.bucket)) {
            case let (lhs?, rhs?) where lhs != rhs: return lhs > rhs
            case (_?, nil): return true
            case (nil, _?): return false
            default: return left.name.localizedStandardCompare(right.name) == .orderedAscending
            }
        }
    }

    private var totalTokens: Int {
        rows.reduce(0) { $0 + (tokenBasis.value($1.bucket) ?? 0) }
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
                    CapsLabel(text: tokenBasis.label).frame(width: 96, alignment: .trailing)
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
        let share = tokenBasis.value(bucket).flatMap { tokens in
            totalTokens > 0 ? Double(tokens) / Double(totalTokens) : 0
        }
        return HStack(spacing: Space.l) {
            Text(name)
                .workFont(.rowLabel).foregroundStyle(Theme.ink)
                .lineLimit(1).truncationMode(.middle)
                .help(name)
                .frame(maxWidth: .infinity, alignment: .leading)
            Text(bucket.sessions.map(String.init) ?? "not reported")
                .workFont(.dataSmall).foregroundStyle(bucket.sessions == nil ? Theme.muted : Theme.ink)
                .frame(width: 76, alignment: .trailing)
            Text(tokenBasis.value(bucket).map(UsageTotals.compact) ?? "not reported")
                .workFont(.dataSmall).foregroundStyle(tokenBasis.value(bucket) == nil ? Theme.muted : Theme.ink)
                .frame(width: 96, alignment: .trailing)
            Group {
                if let share {
                    HStack(spacing: Space.s) {
                        MeterBar(fraction: share, tint: Theme.chartBar, height: 6)
                            .frame(width: 80)
                        Text("\(Int((share * 100).rounded()))%")
                            .workFont(.dataSmall).foregroundStyle(Theme.ink)
                            .frame(width: 40, alignment: .leading)
                    }
                } else {
                    Text("not reported")
                        .workFont(.dataSmall).foregroundStyle(Theme.muted)
                }
            }
            .frame(width: 140, alignment: .leading)
            Text(bucket.costText == "—" ? "unpriced" : bucket.costText)
                .workFont(.dataSmall)
                .foregroundStyle(bucket.costText == "—" ? Theme.muted : Theme.ink)
                .frame(width: 90, alignment: .trailing)
        }
        .padding(.horizontal, Space.xl)
        .frame(minHeight: Metrics.rowTable)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(
            [
                name,
                bucket.sessions.map { Fmt.count($0, "session") } ?? "sessions not reported",
                tokenBasis.value(bucket).map { "\($0) \(tokenBasis.label.lowercased())" } ?? "tokens not reported",
                share.map { "\(Int(($0 * 100).rounded())) percent of known \(tokenBasis.label.lowercased())" }
                    ?? "token share not reported",
                bucket.costText == "—" ? "cost unpriced" : bucket.costText,
                Fmt.costConfidenceLabel(bucket.costConfidence),
            ].compactMap { $0 }.joined(separator: ", ")
        )
    }
}
