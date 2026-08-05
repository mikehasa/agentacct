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
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    SectionCaption(tone: Theme.textMuted, text: "Provider limits")
                    Spacer()
                    Toggle("Show stale accounts", isOn: $showStale)
                        .toggleStyle(.checkbox)
                        .font(.system(size: 11))
                        .foregroundStyle(Theme.textMuted)
                }
                if case .connected(let snapshot) = glance.phase {
                    let limits = snapshot.glance.limits.filter { showStale || $0.stale != true }
                    if limits.isEmpty {
                        Text("No live limit readings.")
                            .font(.system(size: 12))
                            .foregroundStyle(Theme.textMuted)
                    }
                    ForEach(Array(limits.enumerated()), id: \.offset) { _, limit in
                        limitCard(limit)
                    }
                } else {
                    Text("Daemon not connected.")
                        .font(.system(size: 12))
                        .foregroundStyle(Theme.textMuted)
                }
                calibrationSection
            }
            .padding(16)
        }
    }

    private func limitCard(_ limit: LimitEntry) -> some View {
        Card {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 7) {
                    StatusDot(color: Theme.clientColor(limit.client))
                    Text(limit.client ?? "?")
                        .font(.system(size: 12.5, weight: .semibold))
                        .foregroundStyle(Theme.text)
                    if let plan = limit.planType {
                        Chip(text: plan, tint: Theme.clientColor(limit.client))
                    }
                    if limit.stale == true {
                        Chip(text: "stale", tint: Theme.textMuted)
                    }
                    Spacer()
                }
                ForEach(Array((limit.windows ?? []).enumerated()), id: \.offset) { _, window in
                    if let used = window.usedPercent {
                        HStack(spacing: 10) {
                            Text(window.kind ?? "")
                                .font(.system(size: 11))
                                .foregroundStyle(Theme.textMuted)
                                .frame(width: 26, alignment: .leading)
                            MeterBar(fraction: used / 100.0,
                                     tint: Theme.limitColor(usedPercent: used), height: 8)
                            Text(String(format: "%.0f%%", used))
                                .font(Type.numeric)
                                .foregroundStyle(Theme.text)
                                .frame(width: 36, alignment: .trailing)
                            Text(Theme.resetsIn(window.resetsAt).map { "resets in \($0)" } ?? "")
                                .font(.system(size: 10))
                                .foregroundStyle(Theme.textFaint)
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
                SectionCaption(tone: Theme.textMuted, text: "Plan estimate calibration")
                Card(padding: 4) {
                    VStack(spacing: 0) {
                        ForEach(Array(dashboard.planClients.enumerated()), id: \.element.id) { index, client in
                            HStack(alignment: .firstTextBaseline, spacing: 9) {
                                StatusDot(color: Theme.clientColor(client.client), size: 6)
                                Text(client.client)
                                    .font(.system(size: 12, weight: .medium))
                                    .foregroundStyle(Theme.text)
                                    .frame(width: 110, alignment: .leading)
                                Chip(text: client.calibrationState ?? "unknown",
                                     tint: calibrationTint(client.calibrationState))
                                // The progress/why sentence beats the raw
                                // basis: "48 intervals recorded; the fit
                                // (x0.38) is outside the trusted band…"
                                if let detail = client.stateDetail ?? client.basis {
                                    Text(detail)
                                        .font(Type.tiny)
                                        .foregroundStyle(Theme.textFaint)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                                Spacer(minLength: 0)
                            }
                            .padding(.horizontal, 9)
                            .padding(.vertical, 7)
                            if index < dashboard.planClients.count - 1 {
                                Rectangle().fill(Theme.border.opacity(0.6)).frame(height: 1)
                            }
                        }
                    }
                }
            }
        }
    }

    private func calibrationTint(_ state: String?) -> Color {
        switch state {
        case "calibrated": return Theme.green
        case "calibrating": return Theme.orange
        default: return Theme.textMuted
        }
    }
}
