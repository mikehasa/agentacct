import SwiftUI

// The home: "how is my week going" answered in one screen, plan-first.
//
// Two DIFFERENT plan quantities, always labeled apart (the product plan's
// core honesty rule):
// * the ACCOUNT truth — the provider-reported 7d used %, account-wide,
//   includes untracked usage (glance limits[]); this is the hero ring;
// * the ATTRIBUTED estimate — calibrated-or-nothing per-session shares and
//   window aggregates (/v1/plan); shown as "tracked ≈X%" beside the truth,
//   never merged with it.
// Below: today, the 30-day rhythm, live sessions, and what needs attention —
// the work-intelligence strip (blocked sessions, failed evidence), because
// this product unravels what agents DID, not just what they cost.

struct DashboardPane: View {
    @EnvironmentObject var dashboard: DashboardStore
    @EnvironmentObject var glance: GlanceState
    @EnvironmentObject var selection: AppSelection

    var body: some View {
        ScrollBox {
            VStack(alignment: .leading, spacing: Space.l) {
                planHeroRow
                todayRow
                if let periods = dashboard.usage?.byPeriod, periods.count > 1 {
                    DailyChart(periods: periods)
                }
                activeSessions
                attentionStrip
            }
            .padding(Space.l)
        }
        .overlay(alignment: .bottom) {
            if let error = dashboard.errorText {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(Theme.red)
                    .padding(Space.s)
                    .background(Theme.card, in: RoundedRectangle(cornerRadius: 8))
                    .padding(.bottom, 10)
            }
        }
    }

    // MARK: plan hero

    private var liveLimits: [LimitEntry] {
        guard case .connected(let snapshot) = glance.phase else { return [] }
        return snapshot.glance.limits.filter { $0.stale != true }
    }

    @ViewBuilder
    private var planHeroRow: some View {
        if liveLimits.isEmpty {
            Card {
                Text("No live provider limit readings — the weekly plan hero appears once a tracked client reports one.")
                    .font(Type.small)
                    .foregroundStyle(Theme.textMuted)
            }
        } else {
            HStack(spacing: Space.s) {
                ForEach(Array(liveLimits.enumerated()), id: \.offset) { _, limit in
                    PlanHeroCard(
                        limit: limit,
                        planClient: dashboard.planClients.first { $0.client == limit.client }
                    )
                }
            }
        }
    }

    // MARK: today

    private var todayRow: some View {
        let windows: [String: UsageTotals] = {
            guard case .connected(let snapshot) = glance.phase else { return [:] }
            return Dictionary(uniqueKeysWithValues: snapshot.glance.usage.windows.map { ($0.label, $0.totals) })
        }()
        let todayPlan = dashboard.planClients
            .compactMap { client -> Double? in
                guard client.calibrationState == "calibrated" else { return nil }
                return client.windowPcts?["today"] ?? nil
            }
            .reduce(nil as Double?) { partial, pct in (partial ?? 0) + pct }
        return HStack(spacing: Space.s) {
            PanelTile(
                label: "today · tracked plan",
                value: Fmt.planPct(todayPlan) ?? "—",
                detail: todayPlan == nil ? "calibration pending" : "attributed estimate",
                accent: Theme.accent
            )
            PanelTile(
                label: "today · cost",
                value: windows["today"]?.costText ?? "—",
                detail: "reference"
            )
            PanelTile(
                label: "today · fresh tokens",
                value: windows["today"]?.tokensText ?? "—"
            )
            PanelTile(
                label: "root sessions",
                value: dashboard.totalRootSessions.map(String.init) ?? "—",
                detail: dashboard.totalSessions.map { "\($0) with subagents" }
            )
        }
    }

    // MARK: active sessions

    @ViewBuilder
    private var activeSessions: some View {
        if case .connected(let snapshot) = glance.phase, !snapshot.glance.recentSessions.isEmpty {
            VStack(alignment: .leading, spacing: Space.s) {
                SectionCaption(tone: Theme.textMuted, text: "Active sessions")
                Card(padding: 4) {
                    VStack(spacing: 0) {
                        let rows = Array(snapshot.glance.recentSessions.prefix(6))
                        ForEach(Array(rows.enumerated()), id: \.offset) { index, session in
                            Button {
                                selection.pane = .sessions
                                selection.sessionId = "\(session.client)::\(session.sessionId)"
                            } label: {
                                HStack(spacing: 9) {
                                    StatusDot(color: Theme.statusColor(session.status))
                                    Text(session.title ?? "\(session.client) · \(session.shortSessionId)")
                                        .font(Type.body)
                                        .foregroundStyle(Theme.text)
                                        .lineLimit(1)
                                    Spacer(minLength: 8)
                                    if let pct = session.planPctText {
                                        Text(pct)
                                            .font(Type.numeric)
                                            .foregroundStyle(Theme.accent)
                                    }
                                    Text(session.client)
                                        .font(Type.tiny)
                                        .foregroundStyle(Theme.clientColor(session.client))
                                }
                                .padding(.horizontal, 9)
                                .padding(.vertical, 7)
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            if index < rows.count - 1 {
                                Rectangle().fill(Theme.border.opacity(0.6)).frame(height: 1)
                            }
                        }
                    }
                }
            }
        }
    }

    // MARK: needs attention

    private var attentionRows: [V1SessionRow] {
        dashboard.sessions.filter { row in
            row.status == "blocked" || (row.work?.evidence?.failed ?? 0) > 0
        }
    }

    @ViewBuilder
    private var attentionStrip: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            SectionCaption(tone: Theme.textMuted, text: "Needs attention")
            if attentionRows.isEmpty {
                HStack(spacing: 7) {
                    Image(systemName: "checkmark.circle")
                        .font(.system(size: 12))
                        .foregroundStyle(Theme.green)
                    Text("Nothing blocked, no failed checks in the loaded sessions.")
                        .font(Type.small)
                        .foregroundStyle(Theme.textMuted)
                }
            } else {
                Card(padding: 4) {
                    VStack(spacing: 0) {
                        ForEach(Array(attentionRows.prefix(5).enumerated()), id: \.element.id) { index, row in
                            Button {
                                selection.pane = .sessions
                                selection.sessionId = row.id
                            } label: {
                                HStack(spacing: 9) {
                                    Image(systemName: row.status == "blocked"
                                          ? "hand.raised.fill" : "xmark.octagon.fill")
                                        .font(.system(size: 11))
                                        .foregroundStyle(row.status == "blocked" ? Theme.orange : Theme.red)
                                    Text(row.displayTitle)
                                        .font(Type.body)
                                        .foregroundStyle(Theme.text)
                                        .lineLimit(1)
                                    Spacer(minLength: 8)
                                    if row.status == "blocked" {
                                        Chip(text: "blocked", tint: Theme.orange)
                                    }
                                    if let failed = row.work?.evidence?.failed, failed > 0 {
                                        Chip(text: "\(failed) failed check\(failed == 1 ? "" : "s")", tint: Theme.red)
                                    }
                                }
                                .padding(.horizontal, 9)
                                .padding(.vertical, 7)
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            if index < min(attentionRows.count, 5) - 1 {
                                Rectangle().fill(Theme.border.opacity(0.6)).frame(height: 1)
                            }
                        }
                    }
                }
            }
        }
    }
}

// MARK: - The plan hero card (one per live limit account)

/// Account truth front and center (the provider's own 7d %), the attributed
/// estimate beside it — different quantities, labeled apart, never merged.
struct PlanHeroCard: View {
    let limit: LimitEntry
    let planClient: V1PlanClient?

    private var sevenDay: LimitWindow? {
        (limit.windows ?? []).first { $0.kind == "7d" }
    }
    private var fiveHour: LimitWindow? {
        (limit.windows ?? []).first { $0.kind == "5h" }
    }

    var body: some View {
        Card {
            HStack(spacing: Space.m) {
                if let used = sevenDay?.usedPercent {
                    PlanRing(fraction: used / 100.0, tint: Theme.limitColor(usedPercent: used))
                        .frame(width: 74, height: 74)
                        .overlay(
                            VStack(spacing: 0) {
                                Text(String(format: "%.0f%%", used))
                                    .font(Font.system(size: 17, weight: .bold, design: .rounded).monospacedDigit())
                                    .foregroundStyle(Theme.text)
                                Text("7d")
                                    .font(Type.tiny)
                                    .foregroundStyle(Theme.textFaint)
                            }
                        )
                }
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 6) {
                        StatusDot(color: Theme.clientColor(limit.client), size: 6)
                        Text(limit.client ?? "?")
                            .font(.system(size: 12.5, weight: .semibold))
                            .foregroundStyle(Theme.text)
                        if let plan = limit.planType {
                            Chip(text: plan, tint: Theme.clientColor(limit.client))
                        }
                    }
                    Text("account truth · provider-reported")
                        .font(Type.tiny)
                        .foregroundStyle(Theme.textFaint)
                    if let resets = Theme.resetsIn(sevenDay?.resetsAt) {
                        Text("resets in \(resets)")
                            .font(Type.small)
                            .foregroundStyle(Theme.textMuted)
                    }
                    if let tracked = trackedLine {
                        Text(tracked)
                            .font(Type.small)
                            .foregroundStyle(Theme.accent)
                    } else if planClient?.calibrationState == "calibrating" {
                        Text("tracked share calibrating from your own limit history")
                            .font(Type.tiny)
                            .foregroundStyle(Theme.textFaint)
                    }
                    if let five = fiveHour?.usedPercent {
                        HStack(spacing: 6) {
                            Text("5h")
                                .font(Type.tiny)
                                .foregroundStyle(Theme.textFaint)
                            MeterBar(fraction: five / 100.0,
                                     tint: Theme.limitColor(usedPercent: five), height: 4)
                                .frame(width: 90)
                            Text(String(format: "%.0f%%", five))
                                .font(Type.tiny.monospacedDigit())
                                .foregroundStyle(Theme.textMuted)
                        }
                    }
                }
                Spacer(minLength: 0)
            }
        }
        .frame(maxWidth: .infinity)
    }

    /// "tracked ≈X% of this week" — the attributed estimate, only when the
    /// daemon says it is calibrated (calibrated-or-nothing).
    private var trackedLine: String? {
        guard planClient?.calibrationState == "calibrated",
              let pct = planClient?.windowPcts?["7d"] ?? nil,
              let text = Fmt.planPct(pct)
        else { return nil }
        return "tracked \(text) of this week"
    }
}

/// A thin ring gauge (the 7d dial).
struct PlanRing: View {
    let fraction: Double
    var tint: Color

    var body: some View {
        ZStack {
            Circle()
                .stroke(Theme.border, lineWidth: 7)
            Circle()
                .trim(from: 0, to: min(max(fraction, 0), 1))
                .stroke(tint.gradient, style: StrokeStyle(lineWidth: 7, lineCap: .round))
                .rotationEffect(.degrees(-90))
        }
    }
}
