import SwiftUI

// Limits: per-account provider meters plus the calibration ledger — the
// three-state honesty (calibrated / calibrating / never) with the
// why-this-number basis, straight from the daemon.

struct LimitsPane: View {
    @EnvironmentObject var glance: GlanceState
    @EnvironmentObject var dashboard: DashboardStore
    @State private var showStale = false

    var body: some View {
        ScrollBox {
            VStack(alignment: .leading, spacing: Space.m) {
                HStack {
                    SectionCaption(tone: Theme.muted, text: "Provider limits")
                    Spacer()
                    Toggle("Show stale accounts", isOn: $showStale)
                        .toggleStyle(.checkbox)
                        .font(Type.caption)
                        .foregroundStyle(Theme.muted)
                }
                if case .connected(let snapshot) = glance.phase {
                    let limits = snapshot.glance.limits.filter { showStale || $0.stale != true }
                    if limits.isEmpty {
                        Text("No live limit readings.")
                            .font(Type.caption)
                            .foregroundStyle(Theme.muted)
                    }
                    ForEach(Array(limits.enumerated()), id: \.offset) { _, limit in
                        limitCard(limit)
                    }
                } else {
                    Text("Daemon not connected.")
                        .font(Type.caption)
                        .foregroundStyle(Theme.muted)
                }
                calibrationSection
            }
            .padding(Space.l)
        }
    }

    private func limitCard(_ limit: LimitEntry) -> some View {
        Card {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 7) {
                    StatusDot(color: Theme.muted)
                    Text(limit.client ?? "?")
                        .font(Face.sansFont(13, .semibold))
                        .foregroundStyle(Theme.ink)
                    if let plan = limit.planType {
                        Chip(text: plan, tint: Theme.muted)
                    }
                    if limit.stale == true {
                        Chip(text: "stale", tint: Theme.muted)
                    }
                    Spacer()
                }
                ForEach(Array((limit.windows ?? []).enumerated()), id: \.offset) { _, window in
                    if let used = window.usedPercent {
                        HStack(spacing: 10) {
                            Text(window.kind ?? "")
                                .font(Type.dataSmall)
                                .foregroundStyle(Theme.muted)
                                .frame(width: 26, alignment: .leading)
                            MeterBar(fraction: used / 100.0,
                                     tint: Theme.limitColor(usedPercent: used), height: 8)
                            Text(String(format: "%.0f%%", used))
                                .font(Type.dataSmallSemibold)
                                .foregroundStyle(Theme.ink)
                                .frame(width: 36, alignment: .trailing)
                            Text(Theme.resetsIn(window.resetsAt).map { "resets in \($0)" } ?? "")
                                .font(Type.dataSmall)
                                .foregroundStyle(Theme.muted)
                                .frame(width: 100, alignment: .trailing)
                        }
                    }
                }
            }
        }
    }

    /// The plan-estimate calibration ledger: what state each plan-bearing
    /// client is in and why — the "why this number" disclosure surfaced.
    @ViewBuilder
    private var calibrationSection: some View {
        if !dashboard.planClients.isEmpty {
            VStack(alignment: .leading, spacing: Space.s) {
                SectionCaption(tone: Theme.muted, text: "Plan estimate calibration")
                Card(padding: 4) {
                    VStack(spacing: 0) {
                        ForEach(Array(dashboard.planClients.enumerated()), id: \.element.id) { index, client in
                            HStack(alignment: .firstTextBaseline, spacing: 9) {
                                StatusDot(color: Theme.muted, size: 6)
                                Text(client.client)
                                    .font(Type.captionSemibold)
                                    .foregroundStyle(Theme.ink)
                                    .frame(width: 110, alignment: .leading)
                                Chip(text: client.calibrationState ?? "unknown",
                                     tint: calibrationTint(client.calibrationState))
                                // The progress/why sentence beats the raw
                                // basis: "48 intervals recorded; the fit
                                // (x0.38) is outside the trusted band…"
                                if let detail = client.stateDetail ?? client.basis {
                                    Text(detail)
                                        .font(Type.caption)
                                        .foregroundStyle(Theme.muted)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                                Spacer(minLength: 0)
                            }
                            .padding(.horizontal, 9)
                            .padding(.vertical, 7)
                            if index < dashboard.planClients.count - 1 {
                                Rectangle().fill(Theme.hairline).frame(height: 1)
                            }
                        }
                    }
                }
            }
        }
    }

    /// Calibrated is a machine fact (derived from provider-reported readings),
    /// so it may wear green; calibrating is the attention amber; the rest muted.
    private func calibrationTint(_ state: String?) -> Color {
        switch state {
        case "calibrated": return Theme.green
        case "calibrating": return Theme.amber
        default: return Theme.muted
        }
    }
}
