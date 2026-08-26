import SwiftUI

// Usage — the v7 record layout: a summary strip (tokens · sessions · est.
// cost · active days), one single-series daily chart (cost or tokens, with an
// optional per-client group filter for tokens — one series per chart, never a
// stack), ranked By-client and By-model tables with proportional share bars,
// and a basis footer. The plan-share view stays a separate mode (the
// subscription user's real question) and only exists when the daemon says a
// client is calibrated — calibrated-or-nothing.

struct UsagePane: View {
    @EnvironmentObject var dashboard: DashboardStore
    // nil = no explicit user choice: the default follows the DATA (plan view
    // when a calibrated client exists) and re-evaluates as /v1/plan loads —
    // pinning a default in onAppear froze the pane on $ when it rendered
    // before the first plan response (review finding).
    @State private var chosenMode: UsageMode?

    enum UsageMode: String, CaseIterable, Identifiable {
        case plan = "plan %"
        case dollars = "$"
        var id: String { rawValue }
    }

    private var calibratedClients: [V1PlanClient] {
        dashboard.planClients.filter { $0.calibrationState == "calibrated" }
    }

    private var mode: UsageMode {
        // The live pane opens on the plan view when a client is calibrated (the
        // subscription user's real question). The README snapshot instead shows
        // the dollars view — the by-agent / by-model breakdown the docs
        // describe; the plan view is single-client by design (only a
        // clean-meter client calibrates), so it can't fill a marketing shot.
        if SnapshotMode.enabled { return chosenMode ?? .dollars }
        return chosenMode ?? (calibratedClients.isEmpty ? .dollars : .plan)
    }

    var body: some View {
        ScrollBox {
            VStack(alignment: .leading, spacing: 0) {
                header
                if mode == .plan {
                    planView.padding(.top, Space.xl)
                } else {
                    dollarsView
                }
            }
            .padding(Space.gutter)
            .frame(maxWidth: 1172 + Space.gutter * 2, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var header: some View {
        HStack(alignment: .top, spacing: Space.m) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Usage")
                    .font(Type.titlePage).tracking(Type.titlePageTracking)
                    .foregroundStyle(Theme.ink)
                HStack(spacing: 0) {
                    Text("client-reported tokens")
                        .font(Type.dataSmall).foregroundStyle(Theme.muted)
                    if let updated = dashboard.lastUpdated {
                        Text(" · refreshed \(dashboardFreshnessText(updated))")
                            .font(Type.dataSmall).foregroundStyle(Theme.muted)
                    }
                }
            }
            Spacer()
            // One range control for the whole pane: it drives the daily bars
            // AND the breakdown depth.
            if SnapshotMode.enabled {
                // ImageRenderer can't draw a segmented Picker (it comes out a
                // yellow placeholder), so a snapshot shows the current
                // selection as plain chips instead.
                Chip(text: "\(dashboard.usageDays)d", tint: Theme.accent)
                Chip(text: mode.rawValue, tint: Theme.accent)
            } else {
                Picker("", selection: Binding(get: { dashboard.usageDays },
                                              set: { days in Task { await dashboard.setUsageDays(days) } })) {
                    Text("7d").tag(7)
                    Text("30d").tag(30)
                    Text("90d").tag(90)
                }
                .pickerStyle(.segmented)
                .frame(width: 130)
                Picker("", selection: Binding(get: { mode }, set: { chosenMode = $0 })) {
                    ForEach(UsageMode.allCases) { mode in
                        Text(mode.rawValue).tag(mode)
                    }
                }
                .pickerStyle(.segmented)
                .frame(width: 110)
            }
        }
    }

    // MARK: dollars view (the v7 layout)

    @ViewBuilder
    private var dollarsView: some View {
        if let usage = dashboard.usage {
            summaryStrip(usage).padding(.top, Space.xl)
            if let periods = usage.byPeriod, periods.count > 1 {
                UsageDailyChart(periods: periods).padding(.top, Space.xl)
            }
            UsageBreakdownTable(
                title: "By client",
                nameHeader: "Client",
                days: dashboard.usageDays,
                rows: usage.byClient.map { ($0.client ?? "unknown", $0) }
            )
            .padding(.top, Space.xl)
            UsageBreakdownTable(
                title: "By model",
                nameHeader: "Model",
                days: dashboard.usageDays,
                rows: usage.byModel.map { ($0.model ?? "unknown", $0) }
            )
            .padding(.top, Space.xl)
            basisFooter(usage).padding(.top, Space.l)
        } else {
            VStack(alignment: .leading, spacing: 6) {
                Text("No usage loaded").font(Type.rowLabel).foregroundStyle(Theme.ink)
                Text("The daemon has not returned a usage summary yet.")
                    .font(Type.caption).foregroundStyle(Theme.muted)
            }
            .padding(.top, Space.xl)
        }
    }

    private func summaryStrip(_ usage: UsageSummary) -> some View {
        let totals = usage.totals
        let periods = usage.byPeriod ?? []
        let activeDays = periods.filter { ($0.freshTokens ?? 0) > 0 || $0.estimatedCostUsd != nil }.count

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
                label: "Est. cost",
                value: totals.flatMap { $0.costText == "—" ? nil : $0.costText },
                qualifier: Fmt.costConfidenceLabel(totals?.costConfidence),
                absent: "no priced usage"
            ),
            StripRow.Cell(
                id: "days",
                label: "Active days",
                value: periods.isEmpty ? nil : "\(activeDays)/\(periods.count)",
                qualifier: "with recorded usage",
                absent: "no daily series"
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

    // MARK: plan view

    @ViewBuilder
    private var planView: some View {
        if calibratedClients.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                Text("No calibrated plan estimate yet.")
                    .font(Type.body)
                    .foregroundStyle(Theme.muted)
                ForEach(dashboard.planClients) { client in
                    if client.calibrationState == "calibrating" {
                        Text("\(client.client): calibrating from your own limit history")
                            .font(Type.caption)
                            .foregroundStyle(Theme.muted)
                    } else if client.calibrationState == "never" {
                        Text("\(client.client): a weekly plan % is undefined for this client's rolling meter")
                            .font(Type.caption)
                            .foregroundStyle(Theme.muted)
                    }
                }
            }
        } else {
            ForEach(calibratedClients) { client in
                VStack(alignment: .leading, spacing: Space.s) {
                    HStack(spacing: 8) {
                        SectionCaption(tone: Theme.muted, text: "\(client.client) · % of the weekly plan")
                        Spacer()
                        if let today = Fmt.planPct(client.windowPcts?["today"] ?? nil) {
                            Text("today \(today)")
                                .font(Type.dataSmallSemibold)
                                .foregroundStyle(Theme.accent)
                        }
                        if let week = Fmt.planPct(client.windowPcts?["7d"] ?? nil) {
                            Text("7d \(week)")
                                .font(Type.dataSmallSemibold)
                                .foregroundStyle(Theme.muted)
                        }
                    }
                    if let daily = client.daily, daily.count > 1 {
                        PlanDailyChart(days: daily, tint: Theme.chartBar)
                    }
                    if let byModel = client.byModel, !byModel.isEmpty {
                        modelShares(byModel)
                    }
                    if let unknown = Fmt.planPct(client.unknownTimePct) {
                        Text("+ \(unknown) from rows with unusable timestamps (outside the daily bars)")
                            .font(Type.caption)
                            .foregroundStyle(Theme.muted)
                    }
                    if let basis = client.basis {
                        Text("estimate basis: \(basis)")
                            .font(Type.caption)
                            .foregroundStyle(Theme.muted)
                    }
                }
                .padding(.bottom, Space.l)
            }
        }
    }

    private func modelShares(_ shares: [V1PlanModelShare]) -> some View {
        let maxPct = shares.compactMap(\.pct).max() ?? 1
        return VStack(alignment: .leading, spacing: Space.s) {
            HStack(spacing: 6) {
                SectionCaption(tone: Theme.muted, text: "Which model eats the plan")
                Spacer()
                // Disclose that these are estimates AND the window they sum over,
                // so a 30/90-day breakdown reading >100% of a weekly plan is
                // clearly a multi-week accumulation, not a broken number.
                Text("estimated · last \(dashboard.usageDays)d")
                    .font(Type.caption)
                    .foregroundStyle(Theme.muted)
            }
            Card(padding: 6) {
                VStack(spacing: 0) {
                    ForEach(Array(shares.enumerated()), id: \.element.id) { index, share in
                        HStack(spacing: 10) {
                            Text(share.model ?? "unknown")
                                .font(Type.captionSemibold)
                                .foregroundStyle(Theme.ink)
                                .lineLimit(1)
                                .frame(width: 168, alignment: .leading)
                            MeterBar(fraction: (share.pct ?? 0) / max(maxPct, 0.0001),
                                     tint: Theme.chartBar, height: 7)
                            Text(Fmt.planPct(share.pct) ?? "—")
                                .font(Type.dataSmallSemibold)
                                .foregroundStyle(Theme.ink)
                                .frame(width: 64, alignment: .trailing)
                            Text(share.totalTokens.map { UsageTotals.compact(Int($0)) } ?? "—")
                                .font(Type.dataSmall)
                                .foregroundStyle(Theme.muted)
                                .frame(width: 58, alignment: .trailing)
                        }
                        .padding(.horizontal, 9)
                        .padding(.vertical, 7)
                        if index < shares.count - 1 {
                            Rectangle().fill(Theme.hairline).frame(height: 1)
                        }
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

// MARK: - Daily chart (one series per chart)

/// The daily usage chart: estimated cost per day, or fresh tokens per day with
/// an optional single-client group filter. Always ONE series — a stack never
/// appears (v7 chart discipline). Days without a priced value render as flat
/// neutral stubs and their tooltip says so; heights are strictly proportional.
struct UsageDailyChart: View {
    let periods: [PeriodBucket]

    enum Series: String, CaseIterable, Identifiable {
        case cost = "Est. cost"
        case tokens = "Fresh tokens"
        var id: String { rawValue }
    }

    @State private var series: Series = .cost
    /// nil = all clients; set = one client's token series (tokens mode only).
    @State private var group: String?
    @State private var hoveredIndex: Int?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var clients: [String] {
        var seen: Set<String> = []
        for period in periods {
            seen.formUnion((period.byClient ?? [:]).keys)
        }
        return seen.sorted()
    }

    /// The plotted value for a day, or nil when the day has no value to plot
    /// (an unpriced day is NOT a $0 day).
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
        case .cost: return "Estimated cost per day"
        case .tokens: return group.map { "Fresh tokens per day · \($0)" } ?? "Fresh tokens per day"
        }
    }

    var body: some View {
        Card(padding: 0) {
            VStack(spacing: 0) {
                HStack(spacing: Space.m) {
                    Text(chartTitle).font(Type.titleCard).foregroundStyle(Theme.ink)
                    Spacer()
                    if SnapshotMode.enabled {
                        Chip(text: series.rawValue, tint: Theme.accent)
                    } else {
                        Picker("", selection: $series) {
                            ForEach(Series.allCases) { Text($0.rawValue).tag($0) }
                        }
                        .pickerStyle(.segmented)
                        .fixedSize()
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
                                        Group {
                                            if let dayValue = value(period) {
                                                // Strictly linear: the peak bar
                                                // touches the line its label names.
                                                RoundedRectangle(cornerRadius: 2)
                                                    .fill(hoveredIndex == nil || hoveredIndex == index
                                                          ? Theme.chartBar : Theme.chartBarDim)
                                                    .frame(height: max(1, Self.plotHeight * dayValue / maxValue))
                                            } else {
                                                // A day with no plottable value: a flat
                                                // neutral stub, never a zero-height lie.
                                                RoundedRectangle(cornerRadius: 1)
                                                    .fill(Theme.tintNeutral)
                                                    .frame(height: 3)
                                            }
                                        }
                                        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
                                        .contentShape(Rectangle())
                                        .onHover { inside in
                                            if inside {
                                                hoveredIndex = index
                                            } else if hoveredIndex == index {
                                                hoveredIndex = nil
                                            }
                                        }
                                        .accessibilityElement()
                                        .accessibilityLabel(
                                            "\(period.period ?? period.shortLabel), \(valueText(period))"
                                        )
                                    }
                                }
                            }
                            .frame(height: Self.plotHeight)
                        }
                        .overlay(alignment: .topLeading) {
                            if let hoveredIndex, periods.indices.contains(hoveredIndex) {
                                let hovered = periods[hoveredIndex]
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
                .animation(reduceMotion ? nil : Motion.hover, value: hoveredIndex)
            }
        }
        .onChange(of: periods.map(\.period)) {
            hoveredIndex = nil
        }
    }
}

// MARK: - Breakdown tables

/// A v7 breakdown table: NAME · SESSIONS · TOKENS · SHARE · EST. COST, ranked
/// by fresh tokens, share bars strictly proportional to the table's own total.
struct UsageBreakdownTable: View {
    let title: String
    let nameHeader: String
    let days: Int
    let rows: [(name: String, bucket: UsageBucket)]

    private var sorted: [(name: String, bucket: UsageBucket)] {
        rows.sorted { ($0.bucket.freshTokens ?? 0) > ($1.bucket.freshTokens ?? 0) }
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
                    CapsLabel(text: "Est. cost").frame(width: 90, alignment: .trailing)
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
                    ForEach(Array(sorted.enumerated()), id: \.element.bucket.id) { index, row in
                        if index > 0 {
                            Rectangle().fill(Theme.hairline).frame(height: 1)
                                .padding(.horizontal, Space.xl)
                        }
                        tableRow(row.name, row.bucket)
                    }
                }
            }
        }
    }

    private func tableRow(_ name: String, _ bucket: UsageBucket) -> some View {
        let share = totalFresh > 0 ? Double(bucket.freshTokens ?? 0) / Double(totalFresh) : 0
        return HStack(spacing: Space.l) {
            Text(name)
                .font(Type.rowLabel).foregroundStyle(Theme.ink)
                .lineLimit(1).truncationMode(.middle)
                .frame(maxWidth: .infinity, alignment: .leading)
            Text(bucket.sessions.map(String.init) ?? "not reported")
                .font(Type.dataSmall).foregroundStyle(bucket.sessions == nil ? Theme.muted : Theme.ink)
                .frame(width: 76, alignment: .trailing)
            Text(bucket.freshTokens.map(UsageTotals.compact) ?? "none")
                .font(Type.dataSmall).foregroundStyle(Theme.ink)
                .frame(width: 76, alignment: .trailing)
            HStack(spacing: Space.s) {
                MeterBar(fraction: share, tint: Theme.chartBar, height: 6)
                    .frame(width: 80)
                Text(totalFresh > 0 ? "\(Int((share * 100).rounded()))%" : "–%")
                    .font(Type.dataSmall).foregroundStyle(Theme.ink)
                    .frame(width: 40, alignment: .leading)
            }
            .frame(width: 140, alignment: .leading)
            Text(bucket.costText == "—" ? "unpriced" : bucket.costText)
                .font(Type.dataSmall)
                .foregroundStyle(bucket.costText == "—" ? Theme.muted : Theme.ink)
                .frame(width: 90, alignment: .trailing)
        }
        .padding(.horizontal, Space.xl)
        .frame(minHeight: Metrics.rowTable)
    }
}

// MARK: - Plan charts (plan mode)

/// The daily plan-share bars for one calibrated client (aligned to the same
/// local calendar days as the cost chart).
struct PlanDailyChart: View {
    let days: [V1PlanDay]
    var tint: Color

    @State private var hovered: V1PlanDay?

    private var maxPct: Double {
        max(days.map(\.pct).max() ?? 0.0001, 0.0001)
    }

    var body: some View {
        Card(padding: 12) {
            VStack(spacing: 6) {
                HStack(alignment: .bottom, spacing: 3) {
                    ForEach(days) { day in
                        RoundedRectangle(cornerRadius: 1)
                            .fill(day.pct > 0 ? tint : Theme.tintNeutral)
                            .frame(height: day.pct > 0 ? max(2, 110 * day.pct / maxPct) : 1.5)
                            .frame(maxWidth: .infinity, alignment: .bottom)
                            .contentShape(Rectangle())
                            .opacity(hovered == nil || hovered?.id == day.id ? 1 : 0.55)
                            .onHover { inside in
                                if inside { hovered = day } else if hovered?.id == day.id { hovered = nil }
                            }
                    }
                }
                .frame(height: 112, alignment: .bottom)
                .overlay(alignment: .topLeading) {
                    if let hovered {
                        PlanDayTooltip(day: hovered)
                    }
                }
                HStack {
                    Text(shortDate(days.first?.date))
                    Spacer()
                    Text(shortDate(days[days.count / 2].date))
                    Spacer()
                    Text(shortDate(days.last?.date))
                }
                .font(Type.dataSmall)
                .foregroundStyle(Theme.muted)
            }
        }
    }

    private func shortDate(_ iso: String?) -> String {
        guard let iso, iso.count >= 10 else { return "" }
        return String(iso.dropFirst(5))  // YYYY-MM-DD -> MM-DD
    }
}

/// Hover card for the plan-share chart: one day's date + estimated plan %.
struct PlanDayTooltip: View {
    let day: V1PlanDay

    var body: some View {
        HStack(spacing: 6) {
            Text(day.date.count >= 10 ? String(day.date.dropFirst(5)) : day.date)
                .font(Type.dataSmallSemibold)
                .foregroundStyle(Theme.ink)
            Text(Fmt.planPct(day.pct) ?? "≈0%")
                .font(Type.dataSmall)
                .foregroundStyle(Theme.accent)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 5)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radius)
            .strokeBorder(Theme.cardLine, lineWidth: Metrics.borderW))
        .padding(4)
        .fixedSize()
    }
}
