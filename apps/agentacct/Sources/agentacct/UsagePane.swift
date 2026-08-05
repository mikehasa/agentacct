import SwiftUI

// Usage: plan-share view first (the subscription user's real question),
// dollars as the reference view — a toggle, not a merge. The plan view only
// exists when the daemon says the client is calibrated (calibrated-or-
// nothing); otherwise the pane opens on the cost view with an honest note.

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
        chosenMode ?? (calibratedClients.isEmpty ? .dollars : .plan)
    }

    var body: some View {
        ScrollBox {
            VStack(alignment: .leading, spacing: Space.l) {
                HStack(spacing: 10) {
                    SectionCaption(tone: Theme.textMuted, text: "Usage · last \(dashboard.usageDays) days")
                    Spacer()
                    // One range control for the whole pane: it drives the daily
                    // bars AND the per-model breakdown depth. The today/7d
                    // headline windows stay fixed regardless.
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
                    .foregroundStyle(Theme.textMuted)
                ForEach(dashboard.planClients) { client in
                    if client.calibrationState == "calibrating" {
                        Text("\(client.client): calibrating from your own limit history")
                            .font(Type.small)
                            .foregroundStyle(Theme.textFaint)
                    } else if client.calibrationState == "never" {
                        Text("\(client.client): a weekly plan % is undefined for this client's rolling meter")
                            .font(Type.small)
                            .foregroundStyle(Theme.textFaint)
                    }
                }
            }
        } else {
            ForEach(calibratedClients) { client in
                VStack(alignment: .leading, spacing: Space.s) {
                    HStack(spacing: 8) {
                        StatusDot(color: Theme.clientColor(client.client), size: 6)
                        SectionCaption(tone: Theme.textMuted, text: "\(client.client) · % of the weekly plan")
                        Spacer()
                        if let today = Fmt.planPct(client.windowPcts?["today"] ?? nil) {
                            Text("today \(today)")
                                .font(Type.numeric)
                                .foregroundStyle(Theme.accent)
                        }
                        if let week = Fmt.planPct(client.windowPcts?["7d"] ?? nil) {
                            Text("7d \(week)")
                                .font(Type.numeric)
                                .foregroundStyle(Theme.textMuted)
                        }
                    }
                    if let daily = client.daily, daily.count > 1 {
                        PlanDailyChart(days: daily, tint: Theme.clientColor(client.client))
                    }
                    if let byModel = client.byModel, !byModel.isEmpty {
                        modelShares(byModel)
                    }
                    if let unknown = Fmt.planPct(client.unknownTimePct) {
                        Text("+ \(unknown) from rows with unusable timestamps (outside the daily bars)")
                            .font(Type.tiny)
                            .foregroundStyle(Theme.textFaint)
                    }
                    if let basis = client.basis {
                        Text("estimate basis: \(basis)")
                            .font(Type.tiny)
                            .foregroundStyle(Theme.textFaint)
                    }
                }
            }
        }
    }

    private func modelShares(_ shares: [V1PlanModelShare]) -> some View {
        let maxPct = shares.compactMap(\.pct).max() ?? 1
        return VStack(alignment: .leading, spacing: Space.s) {
            HStack(spacing: 6) {
                SectionCaption(tone: Theme.textMuted, text: "Which model eats the plan")
                Spacer()
                // Disclose that these are estimates AND the window they sum over,
                // so a 30/90-day breakdown reading >100% of a weekly plan is
                // clearly a multi-week accumulation, not a broken number.
                Text("estimated · last \(dashboard.usageDays)d")
                    .font(Type.tiny)
                    .foregroundStyle(Theme.textFaint)
            }
            Card(padding: 6) {
                VStack(spacing: 0) {
                    ForEach(Array(shares.enumerated()), id: \.element.id) { index, share in
                        HStack(spacing: 10) {
                            Text(share.model ?? "unknown")
                                .font(.system(size: 12, weight: .medium))
                                .foregroundStyle(Theme.text)
                                .lineLimit(1)
                                .frame(width: 168, alignment: .leading)
                            MeterBar(fraction: (share.pct ?? 0) / max(maxPct, 0.0001),
                                     tint: Theme.accent, height: 7)
                            Text(Fmt.planPct(share.pct) ?? "—")
                                .font(Type.numeric)
                                .foregroundStyle(Theme.text)
                                .frame(width: 64, alignment: .trailing)
                            Text(share.totalTokens.map { UsageTotals.compact(Int($0)) } ?? "—")
                                .font(.system(size: 11))
                                .monospacedDigit()
                                .foregroundStyle(Theme.textMuted)
                                .frame(width: 58, alignment: .trailing)
                        }
                        .padding(.horizontal, 9)
                        .padding(.vertical, 7)
                        if index < shares.count - 1 {
                            Rectangle().fill(Theme.border.opacity(0.6)).frame(height: 1)
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
                    PanelTile(label: "\(dashboard.usageDays)d cost", value: totals.costText, accent: Theme.blue)
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
                (bucket.client ?? "?", Theme.clientColor(bucket.client))
            }
            bucketSection(title: "By model", buckets: usage.byModel) { bucket in
                (bucket.model ?? "?", Theme.clientColor(bucket.client))
            }
        } else {
            VStack(spacing: 10) {
                Image(systemName: "chart.bar.xaxis")
                    .font(.system(size: 32, weight: .light))
                    .foregroundStyle(Theme.textFaint)
                Text("No usage loaded")
                    .foregroundStyle(Theme.textMuted)
            }
            .frame(maxWidth: .infinity)
            .padding(.top, 90)
        }
    }

    private func bucketSection(
        title: String,
        buckets: [UsageBucket],
        nameOf: @escaping (UsageBucket) -> (String, Color)
    ) -> some View {
        let sorted = buckets.sorted { ($0.freshTokens ?? 0) > ($1.freshTokens ?? 0) }
        let maxFresh = Double(sorted.first?.freshTokens ?? 1)
        return VStack(alignment: .leading, spacing: 8) {
            SectionCaption(tone: Theme.textMuted, text: title + " · last \(dashboard.usageDays) days")
            Card(padding: 6) {
                VStack(spacing: 0) {
                    ForEach(Array(sorted.enumerated()), id: \.element.id) { index, bucket in
                        let (name, tint) = nameOf(bucket)
                        HStack(spacing: 10) {
                            StatusDot(color: tint, size: 6)
                            Text(name)
                                .font(.system(size: 12, weight: .medium))
                                .foregroundStyle(Theme.text)
                                .lineLimit(1)
                                .frame(width: 168, alignment: .leading)
                            MeterBar(fraction: Double(bucket.freshTokens ?? 0) / max(maxFresh, 1),
                                     tint: tint, height: 7)
                            Text(bucket.freshTokens.map(UsageTotals.compact) ?? "—")
                                .font(.system(size: 11.5))
                                .monospacedDigit()
                                .foregroundStyle(Theme.textMuted)
                                .frame(width: 58, alignment: .trailing)
                            Text(bucket.costText)
                                .font(Type.metricS)
                                .foregroundStyle(Theme.text)
                                .frame(width: 82, alignment: .trailing)
                        }
                        .padding(.horizontal, 9)
                        .padding(.vertical, 7)
                        if index < sorted.count - 1 {
                            Rectangle().fill(Theme.border.opacity(0.6)).frame(height: 1)
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
                            .fill(day.pct > 0 ? AnyShapeStyle(tint.gradient) : AnyShapeStyle(Theme.border.opacity(0.5)))
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
                .font(.system(size: 9))
                .foregroundStyle(Theme.textFaint)
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
                .font(Type.tiny.weight(.semibold))
                .foregroundStyle(Theme.text)
            Text(Fmt.planPct(day.pct) ?? "≈0%")
                .font(Type.tiny.monospacedDigit())
                .foregroundStyle(Theme.accent)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 5)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous)
            .strokeBorder(Theme.border, lineWidth: 1))
        .padding(4)
        .fixedSize()
    }
}

/// The stacked daily bars, client-colored (cost/tokens view + the dashboard's
/// rhythm strip). Interactive: hover a bar for the day's per-client split,
/// click a legend entry to hide/show that agent's slices, and (when wired)
/// switch the trailing-days range.
struct DailyChart: View {
    let periods: [PeriodBucket]
    /// Present → the range switcher renders (7/30/90) and calls back.
    var selectedDays: Int? = nil
    var onSelectDays: ((Int) -> Void)? = nil

    @State private var hovered: PeriodBucket?
    @State private var hidden: Set<String> = []

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

    private func visibleTotal(_ period: PeriodBucket) -> Double {
        visibleClients.reduce(0.0) { partial, client in
            partial + Double(period.byClient?[client]?.freshTokens ?? 0)
        }
    }

    private var maxTokens: Double {
        max(periods.map(visibleTotal).max() ?? 1, 1)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                SectionCaption(tone: Theme.textMuted, text: "Daily fresh tokens")
                if let selectedDays, let onSelectDays {
                    Picker("", selection: Binding(get: { selectedDays }, set: onSelectDays)) {
                        Text("7d").tag(7)
                        Text("30d").tag(30)
                        Text("90d").tag(90)
                    }
                    .pickerStyle(.segmented)
                    .frame(width: 130)
                }
                Spacer()
                ForEach(clients, id: \.self) { client in
                    Button {
                        if hidden.contains(client) { hidden.remove(client) } else { hidden.insert(client) }
                    } label: {
                        HStack(spacing: 4) {
                            StatusDot(color: hidden.contains(client)
                                      ? Theme.textFaint.opacity(0.4)
                                      : Theme.clientColor(client), size: 5)
                            Text(client)
                                .font(.system(size: 9.5))
                                .foregroundStyle(hidden.contains(client) ? Theme.textFaint : Theme.textMuted)
                                .strikethrough(hidden.contains(client))
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help(hidden.contains(client) ? "Show \(client)" : "Hide \(client)")
                }
            }
            Card(padding: 12) {
                VStack(spacing: 6) {
                    HStack(alignment: .bottom, spacing: 3) {
                        ForEach(periods) { period in
                            let total = visibleTotal(period)
                            let height = 110 * total / maxTokens
                            VStack(spacing: 0.5) {
                                ForEach(visibleClients, id: \.self) { client in
                                    let slice = Double(period.byClient?[client]?.freshTokens ?? 0)
                                    if slice > 0, total > 0 {
                                        RoundedRectangle(cornerRadius: 1)
                                            .fill(Theme.clientColor(client).gradient)
                                            .frame(height: max(1.5, height * slice / total))
                                    }
                                }
                            }
                            .frame(maxWidth: .infinity, alignment: .bottom)
                            .frame(maxHeight: .infinity, alignment: .bottom)
                            .contentShape(Rectangle())
                            .opacity(hovered == nil || hovered?.id == period.id ? 1 : 0.55)
                            .onHover { inside in
                                if inside { hovered = period } else if hovered?.id == period.id { hovered = nil }
                            }
                        }
                    }
                    .frame(height: 112, alignment: .bottom)
                    .overlay(alignment: .topLeading) {
                        if let hovered {
                            // Cost has no per-client breakdown, so a day cost is
                            // only honest when nothing is hidden; otherwise the
                            // header would out-total the rows it sits above.
                            DayTooltip(period: hovered, clients: visibleClients,
                                       dayCostText: hidden.isEmpty ? hovered.costText : nil)
                        }
                    }
                    HStack {
                        Text(periods.first?.shortLabel ?? "")
                        Spacer()
                        Text(periods[periods.count / 2].shortLabel)
                        Spacer()
                        Text(periods.last?.shortLabel ?? "")
                    }
                    .font(.system(size: 9))
                    .foregroundStyle(Theme.textFaint)
                }
            }
        }
    }
}

/// The hover card: one day's totals and per-client split. The header total is
/// summed over the VISIBLE clients so it always equals the stacked bar and the
/// rows below it; the day cost is only shown when the caller vouches it covers
/// the same (unfiltered) set.
struct DayTooltip: View {
    let period: PeriodBucket
    let clients: [String]
    var dayCostText: String? = nil

    private var visibleFresh: Int {
        clients.reduce(0) { $0 + (period.byClient?[$1]?.freshTokens ?? 0) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(period.shortLabel)
                    .font(Type.tiny.weight(.semibold))
                    .foregroundStyle(Theme.text)
                Text(UsageTotals.compact(visibleFresh))
                    .font(Type.tiny.monospacedDigit())
                    .foregroundStyle(Theme.textMuted)
                if let dayCostText {
                    Text(dayCostText)
                        .font(Type.tiny.monospacedDigit())
                        .foregroundStyle(Theme.textMuted)
                }
            }
            ForEach(clients, id: \.self) { client in
                if let slice = period.byClient?[client], let fresh = slice.freshTokens, fresh > 0 {
                    HStack(spacing: 5) {
                        StatusDot(color: Theme.clientColor(client), size: 4)
                        Text(client)
                            .font(Type.tiny)
                            .foregroundStyle(Theme.textMuted)
                        Spacer(minLength: 6)
                        Text(UsageTotals.compact(fresh))
                            .font(Type.tiny.monospacedDigit())
                            .foregroundStyle(Theme.text)
                    }
                }
            }
        }
        .padding(8)
        .frame(minWidth: 140, alignment: .leading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous)
            .strokeBorder(Theme.border, lineWidth: 1))
        .shadow(color: .black.opacity(0.12), radius: 6, y: 2)
        .padding(4)
        .allowsHitTesting(false)
    }
}
