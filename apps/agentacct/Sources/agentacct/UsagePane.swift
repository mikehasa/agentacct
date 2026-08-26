import SwiftUI

// Usage: plan-share view first (the subscription user's real question),
// dollars as the reference view — a toggle, not a merge. The plan view only
// exists when the daemon says the client is calibrated (calibrated-or-
// nothing); otherwise the pane opens on the cost view with an honest note.

/// Per-series chart colors for the stacked daily chart, its legend, tooltip
/// rows, and bucket rows. Keyed by the client's index in a stable sorted
/// client list. Interim helper until the Usage structural redesign lands.
private func seriesColor(_ index: Int) -> Color {
    [Theme.chartBar, Theme.chartBarDim, Theme.amber, Theme.muted][index % 4]
}

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

    /// Stable sorted client names for the plan view's per-client accents.
    private var calibratedClientOrder: [String] {
        calibratedClients.map(\.client).sorted()
    }

    private var mode: UsageMode {
        // The live pane opens on the plan view when a client is calibrated (the
        // subscription user's real question). The README snapshot instead shows
        // the dollars view — the by-agent / by-model / cache-read breakdown the
        // docs describe; the plan view is single-client by design (only a
        // clean-meter client calibrates), so it can't fill a marketing shot.
        if SnapshotMode.enabled { return chosenMode ?? .dollars }
        return chosenMode ?? (calibratedClients.isEmpty ? .dollars : .plan)
    }

    var body: some View {
        ScrollBox {
            VStack(alignment: .leading, spacing: Space.l) {
                HStack(spacing: 10) {
                    SectionCaption(tone: Theme.muted, text: "Usage · last \(dashboard.usageDays) days")
                    Spacer()
                    // One range control for the whole pane: it drives the daily
                    // bars AND the per-model breakdown depth. The today/7d
                    // headline windows stay fixed regardless.
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
                if mode == .plan {
                    planView
                } else {
                    dollarsView
                }
            }
            .padding(Space.l)
        }
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
                        StatusDot(
                            color: seriesColor(calibratedClientOrder.firstIndex(of: client.client) ?? 0),
                            size: 6
                        )
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

    // MARK: dollars view (the previous pane, unchanged semantics)

    @ViewBuilder
    private var dollarsView: some View {
        if let usage = dashboard.usage {
            if let totals = usage.totals {
                HStack(spacing: 8) {
                    PanelTile(label: "\(dashboard.usageDays)d cost", value: totals.costText, accent: Theme.accent)
                    PanelTile(label: "\(dashboard.usageDays)d fresh tokens",
                              value: totals.freshTokens.map(UsageTotals.compact) ?? "—")
                    PanelTile(label: "cache read",
                              value: totals.cacheReadTokens.map(UsageTotals.compact) ?? "—")
                    PanelTile(label: "sessions",
                              value: totals.sessions.map(String.init) ?? "—")
                }
            }
            if let periods = usage.byPeriod, periods.count > 1 {
                // Range lives in the pane header now; the chart shows no picker.
                DailyChart(periods: periods)
            }
            bucketSection(title: "By agent", buckets: usage.byClient) { bucket in
                (bucket.client ?? "?",
                 seriesColor(agentClientOrder(usage.byClient).firstIndex(of: bucket.client ?? "?") ?? 0))
            }
            bucketSection(title: "By model", buckets: usage.byModel) { bucket in
                (bucket.model ?? "?", Theme.chartBar)
            }
        } else {
            VStack(spacing: 10) {
                Image(systemName: "chart.bar.xaxis")
                    .font(.system(size: 32, weight: .light))
                    .foregroundStyle(Theme.muted)
                Text("No usage loaded")
                    .foregroundStyle(Theme.muted)
            }
            .frame(maxWidth: .infinity)
            .padding(.top, 90)
        }
    }

    /// Stable sorted client names for the by-agent bucket accents.
    private func agentClientOrder(_ buckets: [UsageBucket]) -> [String] {
        buckets.map { $0.client ?? "?" }.sorted()
    }

    private func bucketSection(
        title: String,
        buckets: [UsageBucket],
        nameOf: @escaping (UsageBucket) -> (String, Color)
    ) -> some View {
        let sorted = buckets.sorted { ($0.freshTokens ?? 0) > ($1.freshTokens ?? 0) }
        let maxFresh = Double(sorted.first?.freshTokens ?? 1)
        return VStack(alignment: .leading, spacing: 8) {
            SectionCaption(tone: Theme.muted, text: title + " · last \(dashboard.usageDays) days")
            Card(padding: 6) {
                VStack(spacing: 0) {
                    ForEach(Array(sorted.enumerated()), id: \.element.id) { index, bucket in
                        let (name, tint) = nameOf(bucket)
                        HStack(spacing: 10) {
                            StatusDot(color: Theme.muted, size: 6)
                            Text(name)
                                .font(Type.captionSemibold)
                                .foregroundStyle(Theme.ink)
                                .lineLimit(1)
                                .frame(width: 168, alignment: .leading)
                            MeterBar(fraction: Double(bucket.freshTokens ?? 0) / max(maxFresh, 1),
                                     tint: tint, height: 7)
                            Text(bucket.freshTokens.map(UsageTotals.compact) ?? "—")
                                .font(Type.dataSmall)
                                .foregroundStyle(Theme.muted)
                                .frame(width: 58, alignment: .trailing)
                            Text(bucket.costText)
                                .font(Type.dataSmallSemibold)
                                .foregroundStyle(Theme.ink)
                                .frame(width: 82, alignment: .trailing)
                        }
                        .padding(.horizontal, 9)
                        .padding(.vertical, 7)
                        if index < sorted.count - 1 {
                            Rectangle().fill(Theme.hairline).frame(height: 1)
                        }
                    }
                }
            }
        }
    }
}

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

/// Per-series daily bars in the detailed Usage view. Hover a bar for the
/// day's per-client split; click a legend entry to hide or show that agent.
struct DailyChart: View {
    let periods: [PeriodBucket]

    @State private var hoveredIndex: Int?
    @State private var hidden: Set<String> = []
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var clients: [String] {
        var seen: [String] = []
        for period in periods {
            for client in (period.byClient ?? [:]).keys where !seen.contains(client) {
                seen.append(client)
            }
        }
        return seen.sorted()
    }

    private var visibleClients: [String] {
        clients.filter { !hidden.contains($0) }
    }

    /// Series accent keyed by the client's index in the stable sorted list.
    private func clientColor(_ client: String) -> Color {
        seriesColor(clients.firstIndex(of: client) ?? 0)
    }

    private func visibleTotal(_ period: PeriodBucket) -> Double {
        visibleClients.reduce(0.0) { partial, client in
            partial + Double(period.byClient?[client]?.freshTokens ?? 0)
        }
    }

    private var maxTokens: Double {
        max(periods.map(visibleTotal).max() ?? 1, 1)
    }

    private var visibleGrandTotal: Int {
        Int(periods.reduce(0.0) { $0 + visibleTotal($1) })
    }

    var body: some View {
        Card(padding: 0) {
            VStack(spacing: 0) {
                HStack(spacing: 10) {
                    SectionCaption(tone: Theme.muted, text: "Daily fresh tokens")
                    Spacer()
                    HStack(spacing: 5) {
                        Text("\(periods.count)-day total")
                            .font(Type.caption)
                            .foregroundStyle(Theme.muted)
                        Text(UsageTotals.compact(visibleGrandTotal))
                            .font(Type.dataSmallSemibold)
                            .foregroundStyle(Theme.ink)
                    }
                    ForEach(clients, id: \.self) { client in
                        Button {
                            if hidden.contains(client) { hidden.remove(client) } else { hidden.insert(client) }
                        } label: {
                            HStack(spacing: 4) {
                                StatusDot(color: hidden.contains(client)
                                          ? Theme.muted.opacity(0.4)
                                          : clientColor(client), size: 5)
                                Text(client)
                                    .font(Type.caption)
                                    .foregroundStyle(Theme.muted)
                                    .strikethrough(hidden.contains(client))
                            }
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(QuietButtonStyle(
                            tint: clientColor(client),
                            horizontalPadding: 4,
                            verticalPadding: 3
                        ))
                        .help(hidden.contains(client) ? "Show \(client)" : "Hide \(client)")
                    }
                }
                .padding(.leading, 14)
                .padding(.trailing, 12)
                .padding(.vertical, 10)

                Rectangle().fill(Theme.hairline).frame(height: 1)

                VStack(spacing: 7) {
                    HStack(alignment: .top, spacing: 8) {
                        VStack(alignment: .trailing, spacing: 0) {
                            Text(UsageTotals.compact(Int(maxTokens)))
                            Spacer()
                            Text(UsageTotals.compact(Int(maxTokens / 2)))
                            Spacer()
                            Text("0")
                        }
                        .font(Type.dataSmall)
                        .foregroundStyle(Theme.muted)
                        .frame(width: 34, height: 116)

                        ZStack(alignment: .bottom) {
                            VStack(spacing: 0) {
                                Rectangle().fill(Theme.hairline.opacity(0.65)).frame(height: 1)
                                Spacer()
                                Rectangle().fill(Theme.hairline.opacity(0.45)).frame(height: 1)
                                Spacer()
                                Rectangle().fill(Theme.hairline.opacity(0.65)).frame(height: 1)
                            }

                            HStack(alignment: .bottom, spacing: 3) {
                                ForEach(Array(periods.enumerated()), id: \.offset) { index, period in
                                    let total = visibleTotal(period)
                                    let height = 108 * total / maxTokens
                                    ZStack(alignment: .bottom) {
                                        VStack(spacing: 1) {
                                            ForEach(visibleClients, id: \.self) { client in
                                                let slice = Double(period.byClient?[client]?.freshTokens ?? 0)
                                                if slice > 0, total > 0 {
                                                    RoundedRectangle(cornerRadius: 1.5, style: .continuous)
                                                        .fill(clientColor(client))
                                                        .frame(height: max(1.5, height * slice / total))
                                                }
                                            }
                                        }
                                        if hoveredIndex == index {
                                            Rectangle()
                                                .fill(Theme.muted.opacity(0.45))
                                                .frame(width: 1)
                                                .frame(maxHeight: .infinity)
                                        }
                                    }
                                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
                                    .contentShape(Rectangle())
                                    .opacity(hoveredIndex == nil || hoveredIndex == index ? 1 : 0.72)
                                    .onHover { inside in
                                        if inside {
                                            hoveredIndex = index
                                        } else if hoveredIndex == index {
                                            hoveredIndex = nil
                                        }
                                    }
                                    .accessibilityElement()
                                    .accessibilityLabel(
                                        "\(period.period ?? period.shortLabel), "
                                            + "\(UsageTotals.compact(Int(total))) fresh tokens"
                                    )
                                }
                            }
                        }
                        .frame(height: 116)
                        .overlay(alignment: .topLeading) {
                            if let hoveredIndex, periods.indices.contains(hoveredIndex) {
                                let hovered = periods[hoveredIndex]
                                // Cost has no per-client breakdown, so a day
                                // cost is honest only when every client is shown.
                                DayTooltip(
                                    period: hovered,
                                    clients: visibleClients,
                                    allClients: clients,
                                    dayCostText: hidden.isEmpty ? hovered.costText : nil
                                )
                            }
                        }
                    }

                    HStack(spacing: 8) {
                        Color.clear.frame(width: 34, height: 1)
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
                .padding(12)
                .animation(reduceMotion ? nil : Motion.hover, value: hoveredIndex)
            }
        }
        .onChange(of: periods.map(\.period)) {
            hoveredIndex = nil
        }
    }
}

/// The hover card: one day's totals and per-client split. The header total is
/// summed over the VISIBLE clients so it always equals the stacked bar and the
/// rows below it; the day cost is only shown when the caller vouches it covers
/// the same (unfiltered) set. `allClients` keeps series colors keyed to the
/// stable sorted client list even while some clients are hidden.
struct DayTooltip: View {
    let period: PeriodBucket
    let clients: [String]
    var allClients: [String] = []
    var dayCostText: String? = nil

    private var visibleFresh: Int {
        clients.reduce(0) { $0 + (period.byClient?[$1]?.freshTokens ?? 0) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(period.shortLabel)
                    .font(Type.dataSmallSemibold)
                    .foregroundStyle(Theme.ink)
                Text(UsageTotals.compact(visibleFresh))
                    .font(Type.dataSmall)
                    .foregroundStyle(Theme.muted)
                if let dayCostText {
                    Text(dayCostText)
                        .font(Type.dataSmall)
                        .foregroundStyle(Theme.muted)
                }
            }
            ForEach(clients, id: \.self) { client in
                if let slice = period.byClient?[client], let fresh = slice.freshTokens, fresh > 0 {
                    HStack(spacing: 5) {
                        StatusDot(color: seriesColor(allClients.firstIndex(of: client) ?? 0), size: 4)
                        Text(client)
                            .font(Type.caption)
                            .foregroundStyle(Theme.muted)
                        Spacer(minLength: 6)
                        Text(UsageTotals.compact(fresh))
                            .font(Type.dataSmall)
                            .foregroundStyle(Theme.ink)
                    }
                }
            }
        }
        .padding(8)
        .frame(minWidth: 140, alignment: .leading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radius)
            .strokeBorder(Theme.cardLine, lineWidth: Metrics.borderW))
        .padding(4)
        .allowsHitTesting(false)
    }
}
