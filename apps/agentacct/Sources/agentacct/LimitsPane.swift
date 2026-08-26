import SwiftUI

// Limits — windows and quotas exactly as each client reported them. Per-client
// cards carry one v7 limit meter per window (notches at the 75/90% attention
// thresholds, absolute reset times); a client that reports usage but no limits
// gets a named "no limits reported" card, never an empty space or a fabricated
// meter. The calibration ledger explains how plan-share estimates warm up.

struct LimitsPane: View {
    @EnvironmentObject var glance: GlanceState
    @EnvironmentObject var dashboard: DashboardStore
    @State private var showStale = false

    var body: some View {
        ScrollBox {
            VStack(alignment: .leading, spacing: 0) {
                header
                content.padding(.top, Space.xl)
            }
            .padding(Space.gutter)
            .frame(maxWidth: 1172 + Space.gutter * 2, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var header: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Limits")
                    .font(Type.titlePage).tracking(Type.titlePageTracking)
                    .foregroundStyle(Theme.ink)
                Text("windows and quotas as reported by each client")
                    .font(Type.dataSmall).foregroundStyle(Theme.muted)
            }
            Spacer()
            if !SnapshotMode.enabled {
                Toggle(isOn: $showStale) {
                    Text("Show stale accounts").font(Type.caption).foregroundStyle(Theme.muted)
                }
                .toggleStyle(.checkbox)
            }
        }
    }

    @ViewBuilder
    private var content: some View {
        switch glance.phase {
        case .connected(let snapshot):
            let limits = snapshot.glance.limits.filter { showStale || $0.stale != true }
            let limitClients = Set(snapshot.glance.limits.compactMap(\.client))
            // Clients with recorded usage but no limit reading get a named card.
            let silentClients = (dashboard.usage?.byClient ?? [])
                .compactMap(\.client)
                .filter { !limitClients.contains($0) }
                .sorted()

            if limits.isEmpty && silentClients.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text("No live limit readings").font(Type.rowLabel).foregroundStyle(Theme.ink)
                    Text(snapshot.glance.limits.isEmpty
                         ? "No client has reported a quota window to this store."
                         : "Every reading is stale — show stale accounts to inspect them.")
                        .font(Type.caption).foregroundStyle(Theme.muted)
                }
            } else {
                LazyVGrid(
                    columns: [GridItem(.adaptive(minimum: 420), spacing: Space.xl, alignment: .top)],
                    alignment: .leading,
                    spacing: Space.xl
                ) {
                    ForEach(Array(limits.enumerated()), id: \.offset) { _, limit in
                        limitCard(limit)
                    }
                    ForEach(silentClients, id: \.self) { client in
                        noLimitsCard(client)
                    }
                }
            }
            if !showStale {
                let hiddenEntries = snapshot.glance.limits.filter { $0.stale == true }
                if !hiddenEntries.isEmpty {
                    let names = hiddenEntries.compactMap(\.client).joined(separator: ", ")
                    Text("\(hiddenEntries.count) stale reading\(hiddenEntries.count == 1 ? "" : "s") hidden (\(names)) — Show stale accounts to inspect \(hiddenEntries.count == 1 ? "it" : "them")")
                        .font(Type.dataSmall).foregroundStyle(Theme.muted)
                        .padding(.top, Space.m)
                }
            }
            calibrationSection.padding(.top, Space.xl)
            definitionsCard.padding(.top, Space.xl)
        case .connecting:
            Text("Connecting to the agentacct daemon…").font(Type.body).foregroundStyle(Theme.muted)
        case .disconnected, .incompatible:
            Text("Daemon not connected.").font(Type.body).foregroundStyle(Theme.muted)
        }
    }

    // MARK: per-client cards

    private func limitCard(_ limit: LimitEntry) -> some View {
        let windows = limit.windows ?? []
        return Card(padding: Space.xl) {
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: Space.s) {
                    Text(limit.client ?? "unknown client")
                        .font(Type.titleCard).foregroundStyle(Theme.ink)
                    if let plan = limit.planType {
                        Chip(text: plan, tint: Theme.muted)
                    }
                    if limit.stale == true {
                        Chip(text: "stale reading", tint: Theme.amber)
                    }
                    Spacer()
                    Text("\(windows.count) window\(windows.count == 1 ? "" : "s") reported")
                        .font(Type.dataSmall).foregroundStyle(Theme.muted)
                }
                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.vertical, Space.m)
                if windows.isEmpty {
                    Text("The reading carried no quota windows")
                        .font(Type.body).foregroundStyle(Theme.muted)
                } else {
                    ForEach(Array(windows.enumerated()), id: \.offset) { index, window in
                        limitWindowRow(window)
                            .padding(.top, index > 0 ? Space.l : 0)
                    }
                    Text("Notches mark the 75% and 90% attention thresholds")
                        .font(Type.dataSmall).foregroundStyle(Theme.muted)
                        .padding(.top, Space.l)
                }
            }
        }
    }

    @ViewBuilder
    private func limitWindowRow(_ window: LimitWindow) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: Space.s) {
                Text(windowName(window)).font(Type.rowLabel).foregroundStyle(Theme.ink)
                if let minutes = window.windowMinutes {
                    Text(windowSpanText(minutes)).font(Type.dataSmall).foregroundStyle(Theme.muted)
                }
                Spacer()
                if window.usedPercent == nil {
                    // The client reported the window without a fill fraction —
                    // a named state, never a fabricated meter.
                    Text("used % unreported").font(Type.dataSmallSemibold).foregroundStyle(Theme.amber)
                }
            }
            if let used = window.usedPercent {
                LimitMeter(usedPercent: used)
                HStack {
                    Text(String(format: "%.0f%%", used) + (used >= 75 ? " · above notify threshold" : ""))
                        .font(Type.dataSmallSemibold)
                        .foregroundStyle(Theme.limitColor(usedPercent: used))
                    Spacer()
                    if let resets = resetsAtText(window.resetsAt) {
                        Text(resets).font(Type.dataSmall).foregroundStyle(Theme.muted)
                    } else {
                        Text("Reset time unreported").font(Type.dataSmall).foregroundStyle(Theme.muted)
                    }
                }
            } else {
                HatchedTrack()
            }
        }
    }

    private func noLimitsCard(_ client: String) -> some View {
        Card(padding: Space.xl) {
            VStack(alignment: .leading, spacing: 0) {
                Text(client).font(Type.titleCard).foregroundStyle(Theme.ink)
                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.vertical, Space.m)
                Text("No limits reported by this client")
                    .font(Type.rowLabel).foregroundStyle(Theme.ink)
                Text("Usage is still recorded and attributed to receipts.")
                    .font(Type.caption).foregroundStyle(Theme.muted)
                    .padding(.top, 4)
            }
        }
    }

    private func windowName(_ window: LimitWindow) -> String {
        switch window.kind {
        case "5h": return "5-hour window"
        case "7d": return "Weekly"
        case .some(let kind): return kind
        case nil: return "window"
        }
    }

    private func windowSpanText(_ minutes: Double) -> String {
        let whole = Int(minutes.rounded())
        if whole % 1440 == 0 { return "\(whole / 1440)d span" }
        if whole % 60 == 0 { return "\(whole / 60)h span" }
        return "\(whole)m span"
    }

    /// Absolute reset time as reported: "Resets today 12:40", "Resets Mon
    /// 09:00", or a date when further out. Absolute beats a countdown here —
    /// quota planning is calendar work.
    private func resetsAtText(_ resetsAt: Double?, now: Date = SnapshotMode.currentDate) -> String? {
        guard let resetsAt, resetsAt > now.timeIntervalSince1970 else { return nil }
        let date = Date(timeIntervalSince1970: resetsAt)
        let calendar = Calendar.current
        let time = date.formatted(.dateTime.hour(.twoDigits(amPM: .omitted)).minute())
        if calendar.isDate(date, inSameDayAs: now) {
            return "Resets today \(time)"
        }
        if let days = calendar.dateComponents([.day], from: now, to: date).day, days < 7 {
            let weekday = date.formatted(.dateTime.weekday(.abbreviated))
            return "Resets \(weekday) \(time)"
        }
        return "Resets \(date.formatted(.dateTime.month(.abbreviated).day())) \(time)"
    }

    // MARK: calibration ledger

    @ViewBuilder
    private var calibrationSection: some View {
        if !dashboard.planClients.isEmpty {
            Card(padding: 0) {
                VStack(spacing: 0) {
                    HStack {
                        Text("Plan estimate calibration").font(Type.titleCard).foregroundStyle(Theme.ink)
                        Spacer()
                        Text("plan-share estimates warm up from your own limit history")
                            .font(Type.dataSmall).foregroundStyle(Theme.muted)
                    }
                    .padding(.horizontal, Space.xl)
                    .frame(height: 52)
                    Rectangle().fill(Theme.hairline).frame(height: 1).padding(.horizontal, Space.xl)
                    ForEach(Array(dashboard.planClients.enumerated()), id: \.element.id) { index, client in
                        if index > 0 {
                            Rectangle().fill(Theme.hairline).frame(height: 1)
                                .padding(.horizontal, Space.xl)
                        }
                        HStack(spacing: Space.l) {
                            Text(client.client)
                                .font(Type.rowLabel).foregroundStyle(Theme.ink)
                                .frame(width: 128, alignment: .leading)
                            Chip(text: client.calibrationState ?? "unknown",
                                 tint: calibrationTint(client.calibrationState))
                            Text(client.stateDetail ?? client.basis ?? "")
                                .font(Type.caption).foregroundStyle(Theme.muted)
                                .lineLimit(2)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .padding(.horizontal, Space.xl)
                        .frame(minHeight: Metrics.rowTable)
                    }
                }
            }
        }
    }

    private func calibrationTint(_ state: String?) -> Color {
        switch state {
        // Accent, not green: a calibration fit warmed from the client's own
        // history is self-derived state, not independently verified evidence.
        case "calibrated": return Theme.accent
        case "calibrating": return Theme.amber
        default: return Theme.muted
        }
    }

    // MARK: definitions

    private var definitionsCard: some View {
        Card(padding: Space.xl) {
            VStack(alignment: .leading, spacing: 0) {
                HStack {
                    Text("How windows are measured").font(Type.titleCard).foregroundStyle(Theme.ink)
                    Spacer()
                    Text("definitions come from each client")
                        .font(Type.dataSmall).foregroundStyle(Theme.muted)
                }
                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.vertical, Space.m)
                HStack(alignment: .top, spacing: Space.xl) {
                    HStack(alignment: .firstTextBaseline, spacing: Space.m) {
                        CapsLabel(text: "Rolling")
                        Text("Anchored to your own activity and re-anchored as the client reports.")
                            .font(Type.caption).foregroundStyle(Theme.muted)
                    }
                    HStack(alignment: .firstTextBaseline, spacing: Space.m) {
                        CapsLabel(text: "Fixed")
                        Text("Resets at the provider's stated time.")
                            .font(Type.caption).foregroundStyle(Theme.muted)
                    }
                }
            }
        }
    }
}

// MARK: - Limit meter

/// The v7 limit meter: h8 rx2 neutral track, fill in the threshold color
/// (accent < 75% ≤ amber < 100% ≤ coral), notch ticks at 75% and 90%.
struct LimitMeter: View {
    let usedPercent: Double

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 2).fill(Theme.tintNeutral)
                RoundedRectangle(cornerRadius: 2)
                    .fill(Theme.limitColor(usedPercent: usedPercent))
                    .frame(width: max(4, proxy.size.width * min(max(usedPercent / 100, 0), 1)))
                ForEach([0.75, 0.9], id: \.self) { notch in
                    Rectangle()
                        .fill(Theme.chipLine)
                        .frame(width: 1.5, height: Metrics.meterH + 4)
                        .offset(x: proxy.size.width * notch, y: -2)
                }
            }
        }
        .frame(height: Metrics.meterH)
        .accessibilityElement()
        .accessibilityLabel("\(Int(usedPercent.rounded())) percent used")
    }
}

/// A hatched track for a window whose fill fraction was not reported — the
/// named absence of a meter, never a fake percentage.
struct HatchedTrack: View {
    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 2)
                    .strokeBorder(Theme.chipLine, lineWidth: 1)
                HStack(spacing: 4) {
                    ForEach(0..<Int(proxy.size.width / 6), id: \.self) { _ in
                        Rectangle()
                            .fill(Theme.chipLine)
                            .frame(width: 1)
                            .rotationEffect(.degrees(45))
                    }
                }
                .clipShape(RoundedRectangle(cornerRadius: 2))
            }
        }
        .frame(height: Metrics.meterH)
        .accessibilityLabel("limit unreported")
    }
}
