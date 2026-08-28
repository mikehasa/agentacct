import Foundation
import SwiftUI

func usageResetClockText(
    _ date: Date,
    timeZone: TimeZone = .current
) -> String {
    let formatter = DateFormatter()
    formatter.calendar = Calendar(identifier: .gregorian)
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.timeZone = timeZone
    formatter.dateFormat = "HH:mm"
    return formatter.string(from: date)
}

/// Presentation-only join for the merged Usage surface. Provider capacity and
/// ranged receipt usage remain separate facts; this type only gives them one
/// stable row identity and deterministic reading order.
struct UsageCapacitySnapshot {
    let rows: [UsageCapacityRow]
    let hiddenStaleCount: Int

    static func build(
        usage: [UsageBucket],
        limits: [LimitEntry],
        plans: [V1PlanClient],
        showStale: Bool
    ) -> Self {
        let planByClient = Dictionary(plans.map { ($0.client, $0) }) { first, _ in first }
        let namedUsage = usage.compactMap { bucket -> (String, UsageBucket)? in
            guard let client = bucket.client, !client.isEmpty else { return nil }
            return (client, bucket)
        }
        let usageByClient = Dictionary(namedUsage) { first, _ in first }

        var indexedLimitsByClient: [String: [UsageCapacityReading]] = [:]
        var unnamedReadings: [UsageCapacityReading] = []
        for (index, limit) in limits.enumerated() {
            let reading = UsageCapacityReading(id: "limit-\(index)", entry: limit)
            if let client = limit.client, !client.isEmpty {
                indexedLimitsByClient[client, default: []].append(reading)
            } else {
                unnamedReadings.append(reading)
            }
        }

        let namedClients = Set(usageByClient.keys).union(indexedLimitsByClient.keys)
        var rows = namedClients.compactMap { client -> UsageCapacityRow? in
            let allReadings = indexedLimitsByClient[client] ?? []
            let visibleReadings = allReadings.filter { showStale || !$0.isStale }
            let hasUsage = usageByClient[client] != nil
            // A stale-only, limit-only account stays hidden until requested.
            guard hasUsage || !visibleReadings.isEmpty else { return nil }
            return UsageCapacityRow(
                id: "client:\(client)",
                client: client,
                usage: usageByClient[client],
                readings: visibleReadings,
                plan: planByClient[client],
                hasHiddenStaleReading: !showStale && allReadings.contains(where: \.isStale)
            )
        }

        // An absent identity is not the literal client name "unknown" and may
        // not be joined to unattributed usage. Keep each unnamed limit distinct.
        rows += unnamedReadings
            .filter { showStale || !$0.isStale }
            .map { reading in
                UsageCapacityRow(
                    id: reading.id,
                    client: "Client name not reported",
                    usage: nil,
                    readings: [reading],
                    plan: nil,
                    hasHiddenStaleReading: false
                )
            }

        // The summary endpoint normally emits one unattributed aggregate. If
        // it emits more, stable fixture order keeps them separate and truthful.
        rows += usage.enumerated().compactMap { index, bucket in
            guard bucket.client == nil || bucket.client?.isEmpty == true else { return nil }
            return UsageCapacityRow(
                id: "usage:unattributed:\(index)",
                client: "Unattributed client",
                usage: bucket,
                readings: [],
                plan: nil,
                hasHiddenStaleReading: false
            )
        }

        rows.sort(by: UsageCapacityRow.precedes)
        return Self(
            rows: rows,
            hiddenStaleCount: showStale ? 0 : limits.filter { $0.stale == true }.count
        )
    }
}

struct UsageCapacityReading: Identifiable {
    let id: String
    let entry: LimitEntry

    var isStale: Bool { entry.stale == true }
}

struct UsageCapacityRow: Identifiable {
    let id: String
    let client: String
    let usage: UsageBucket?
    let readings: [UsageCapacityReading]
    let plan: V1PlanClient?
    let hasHiddenStaleReading: Bool

    var highestFreshValidUsedPercent: Double? {
        readings
            .filter { !$0.isStale }
            .flatMap { $0.entry.windows ?? [] }
            .compactMap { window in
                guard let used = window.usedPercent, used.isFinite, used >= 0 else { return nil }
                return used
            }
            .max()
    }

    var isStaleOnly: Bool {
        !readings.isEmpty && readings.allSatisfy(\.isStale)
    }

    var planTypes: [String] {
        Array(Set(readings.compactMap { $0.entry.planType })).sorted()
    }

    static func precedes(_ left: Self, _ right: Self) -> Bool {
        switch (left.highestFreshValidUsedPercent, right.highestFreshValidUsedPercent) {
        case let (lhs?, rhs?) where lhs != rhs:
            return lhs > rhs
        case (_?, nil):
            return true
        case (nil, _?):
            return false
        default:
            let lhsTokens = left.usage?.freshTokens
            let rhsTokens = right.usage?.freshTokens
            switch (lhsTokens, rhsTokens) {
            case let (lhs?, rhs?) where lhs != rhs: return lhs > rhs
            case (_?, nil): return true
            case (nil, _?): return false
            default: return left.client.localizedStandardCompare(right.client) == .orderedAscending
            }
        }
    }

    func accessibilitySummary(days: Int, usageLoaded: Bool = true) -> String {
        var parts = [client]
        if let plan {
            parts.append(UsagePlanPresentation(client: plan, days: days).detailText)
        }
        if readings.isEmpty {
            parts.append(hasHiddenStaleReading
                ? "no live provider limit; stale reading hidden"
                : "provider limit not reported")
        } else {
            for reading in readings {
                let windows = reading.entry.windows ?? []
                if windows.isEmpty {
                    parts.append("provider reading contained no quota windows")
                    if reading.isStale { parts.append("stale reading") }
                }
                for window in windows {
                    parts.append(
                        LimitWindowPresentation(window: window, stale: reading.isStale)
                            .accessibilityText
                    )
                }
            }
        }
        if let usage {
            parts.append("last \(days) days")
            parts.append(usage.freshTokens.map { "\($0) fresh tokens" } ?? "tokens not reported")
            parts.append(usage.sessions.map { "\($0) sessions" } ?? "sessions not reported")
            parts.append(usage.costText == "—" ? "cost unpriced" : usage.costText)
            if let confidence = Fmt.costConfidenceLabel(usage.costConfidence) {
                parts.append(confidence)
            }
        } else {
            parts.append(usageLoaded
                ? "no recorded usage in this range"
                : "recorded usage not loaded")
        }
        return parts.joined(separator: ", ")
    }
}

struct LimitWindowPresentation {
    let window: LimitWindow
    let stale: Bool

    var validUsedPercent: Double? {
        guard let used = window.usedPercent, used.isFinite, used >= 0 else { return nil }
        return used
    }

    var name: String {
        switch window.kind {
        case "5h": return "5-hour window"
        case "7d": return "Weekly"
        case .some(let kind) where !kind.isEmpty: return "Provider window: \(kind)"
        default: return "Provider window"
        }
    }

    var spanText: String? {
        guard let minutes = window.windowMinutes, minutes.isFinite, minutes >= 0 else { return nil }
        guard let whole = Int(exactly: minutes.rounded()) else { return "Invalid span" }
        if whole == 0 { return "0m span" }
        if whole % 1_440 == 0 { return "\(whole / 1_440)d span" }
        if whole % 60 == 0 { return "\(whole / 60)h span" }
        return "\(whole)m span"
    }

    var statusText: String {
        guard let reported = window.usedPercent else { return "Used percent not reported" }
        guard reported.isFinite, reported >= 0 else {
            return "Invalid provider percentage (\(Self.percent(reported)))"
        }
        if reported > 100 { return "\(Self.percent(reported)) used · limit exceeded" }
        if reported == 100 { return "100% used · limit reached" }
        if reported >= 90 { return "\(Self.percent(reported)) used · high attention" }
        if reported == 75 { return "75% used · at attention threshold" }
        if reported > 75 { return "\(Self.percent(reported)) used · above attention threshold" }
        return "\(Self.percent(reported)) used"
    }

    var resetText: String {
        guard let resetsAt = window.resetsAt else { return "Reset time not reported" }
        guard resetsAt.isFinite,
              (Date.distantPast.timeIntervalSince1970 ... Date.distantFuture.timeIntervalSince1970)
              .contains(resetsAt)
        else {
            return "Invalid reset time"
        }
        let date = Date(timeIntervalSince1970: resetsAt)
        let now = SnapshotMode.currentDate
        let time = usageResetClockText(date)
        guard resetsAt > now.timeIntervalSince1970 else {
            return "Reported reset passed \(date.formatted(.dateTime.month(.abbreviated).day())) \(time)"
        }
        let calendar = Calendar.current
        if calendar.isDate(date, inSameDayAs: now) { return "Resets today \(time)" }
        if let days = calendar.dateComponents([.day], from: now, to: date).day, days < 7 {
            return "Resets \(date.formatted(.dateTime.weekday(.abbreviated))) \(time)"
        }
        let dateText = calendar.component(.year, from: date) == calendar.component(.year, from: now)
            ? date.formatted(.dateTime.month(.abbreviated).day())
            : date.formatted(.dateTime.year().month(.abbreviated).day())
        return "Resets \(dateText) \(time)"
    }

    var accessibilityText: String {
        var parts = [name]
        if let spanText { parts.append(spanText) }
        parts.append(statusText)
        parts.append(resetText)
        if stale { parts.append("stale reading") }
        return parts.joined(separator: ", ")
    }

    private static func percent(_ value: Double) -> String {
        value.formatted(.number.precision(.fractionLength(0))) + "%"
    }
}

/// Compact, truthful plan-share detail for the secondary disclosure. Plan
/// estimates never become a competing page mode or a capacity meter, but the
/// daemon's calibration, daily-series, and per-model facts remain inspectable.
struct UsagePlanPresentation {
    let client: V1PlanClient
    let days: Int

    var detailText: String {
        var parts: [String] = []
        switch client.calibrationState {
        case "calibrated": parts.append("calibrated weekly plan-share estimate")
        case "calibrating": parts.append("calibrating from provider limit history")
        case "never": parts.append("weekly plan share unavailable for this meter")
        case .some(let state): parts.append("calibration status: \(state)")
        case nil: parts.append("calibration status not reported by this daemon")
        }
        if let used = client.intervalsUsed, let needed = client.intervalsNeeded {
            parts.append("\(used) of \(needed) clean intervals observed")
        }
        if client.calibrationState == "calibrated" {
            if let today = Self.percentText(client.windowPcts?["today"] ?? nil) {
                parts.append("today \(today) of weekly plan")
            }
            if let week = Self.percentText(client.windowPcts?["7d"] ?? nil) {
                parts.append("7d \(week) of weekly plan")
            }
            if let unknown = Self.percentText(client.unknownTimePct) {
                parts.append("\(unknown) from unusable timestamps, excluded from daily estimates")
            }
        }
        if let detail = client.stateDetail, !detail.isEmpty { parts.append(detail) }
        if let basis = client.basis, !basis.isEmpty, basis != client.stateDetail {
            parts.append("basis: \(basis)")
        }
        return parts.joined(separator: " · ")
    }

    var dailyText: String? {
        guard client.calibrationState == "calibrated" else { return nil }
        guard let daily = client.daily else { return "Daily plan series not reported" }
        guard !daily.isEmpty else { return "Daily plan series reported no days" }
        let dates = daily.map(\.date).sorted()
        let range = dates.count == 1 ? dates[0] : "\(dates[0]) to \(dates[dates.count - 1])"
        let validPeak = daily.map(\.pct).filter { $0.isFinite && $0 >= 0 }.max()
        let peak = Self.percentText(validPeak).map { " · peak \($0)" } ?? ""
        return "Daily estimates · \(daily.count) reported day\(daily.count == 1 ? "" : "s") · \(range)\(peak) · unreported dates are not zero"
    }

    var dailyRows: [String] {
        guard client.calibrationState == "calibrated", let daily = client.daily else { return [] }
        return daily.sorted { $0.date < $1.date }.map { day in
            let share = Self.percentText(day.pct) ?? "invalid share"
            return "\(day.date) · \(share) of weekly plan"
        }
    }

    var modelRows: [String] {
        guard client.calibrationState == "calibrated" else { return [] }
        guard let shares = client.byModel else {
            return ["Model plan-share breakdown not reported"]
        }
        guard !shares.isEmpty else { return ["Model plan-share breakdown reported no rows"] }
        return shares.map { share in
            let model = share.model.flatMap { $0.isEmpty ? nil : $0 } ?? "Model name not reported"
            let shareText = Self.percentText(share.pct) ?? Self.invalidOrMissingPercent(share.pct)
            let tokensText = Self.tokenText(share.totalTokens)
            return "\(model) · \(shareText) · \(tokensText)"
        }
    }

    var modelHeading: String {
        "Model plan-share estimates · accumulated over last \(days)d"
    }

    static func percentText(_ value: Double?) -> String? {
        guard let value, value.isFinite, value >= 0 else { return nil }
        if value == 0 { return "≈0%" }
        return value >= 0.1 ? String(format: "≈%.1f%%", value) : "≈<0.1%"
    }

    private static func invalidOrMissingPercent(_ value: Double?) -> String {
        value == nil ? "share not reported" : "invalid share"
    }

    private static func tokenText(_ value: Double?) -> String {
        guard let value else { return "tokens not reported" }
        guard value.isFinite, value >= 0,
              let whole = Int(exactly: value.rounded()) else {
            return "invalid token total"
        }
        return "\(UsageTotals.compact(whole)) tokens"
    }
}

struct UsageCapacityLedger: View {
    let rows: [UsageCapacityRow]
    let days: Int
    let usageLoaded: Bool

    var body: some View {
        Card(padding: 0) {
            VStack(spacing: 0) {
                HStack(spacing: Space.l) {
                    CapsLabel(text: "Client").frame(width: 160, alignment: .leading)
                    CapsLabel(text: "Provider window").frame(maxWidth: .infinity, alignment: .leading)
                    CapsLabel(text: "Recorded use · \(days)d").frame(width: 230, alignment: .leading)
                }
                .padding(.horizontal, Space.xl)
                .frame(height: Metrics.rowHeader)
                .accessibilityHidden(true)

                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.horizontal, Space.xl)

                if SnapshotMode.enabled {
                    VStack(spacing: 0) { ledgerRows }
                } else {
                    LazyVStack(spacing: 0) { ledgerRows }
                }
            }
        }
    }

    @ViewBuilder
    private var ledgerRows: some View {
        ForEach(Array(rows.enumerated()), id: \.element.id) { index, row in
            if index > 0 {
                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.horizontal, Space.xl)
            }
            UsageCapacityLedgerRow(row: row, days: days, usageLoaded: usageLoaded)
        }
    }
}

private struct UsageCapacityLedgerRow: View {
    let row: UsageCapacityRow
    let days: Int
    let usageLoaded: Bool
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        Group {
            if dynamicTypeSize.isAccessibilitySize {
                VStack(alignment: .leading, spacing: Space.l) {
                    clientLane
                    capacityLane
                    Rectangle().fill(Theme.hairline).frame(height: 1)
                    usageLane
                }
            } else {
                HStack(alignment: .top, spacing: Space.l) {
                    clientLane.frame(width: 160, alignment: .leading)
                    capacityLane.frame(maxWidth: .infinity, alignment: .leading)
                    Rectangle().fill(Theme.hairline).frame(width: 1)
                    usageLane.frame(width: 230, alignment: .leading)
                }
            }
        }
        .padding(.horizontal, Space.xl)
        .padding(.vertical, Space.l)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(row.accessibilitySummary(days: days, usageLoaded: usageLoaded))
    }

    private var clientLane: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(row.client)
                .font(Type.rowLabel)
                .foregroundStyle(Theme.ink)
                .lineLimit(2)
                .truncationMode(.middle)
                .help(row.client)
            if !row.planTypes.isEmpty {
                Text(row.planTypes.joined(separator: " · "))
                    .font(Type.dataSmall)
                    .foregroundStyle(Theme.muted)
            }
            if let state = row.plan?.calibrationState {
                Chip(text: calibrationLabel(state), tint: calibrationTint(state))
            }
        }
    }

    @ViewBuilder
    private var capacityLane: some View {
        if row.readings.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                Text(row.hasHiddenStaleReading ? "No live provider limit" : "Provider limit not reported")
                    .font(Type.captionSemibold)
                    .foregroundStyle(Theme.muted)
                if row.hasHiddenStaleReading {
                    Text("A stale reading is hidden")
                        .font(Type.caption)
                        .foregroundStyle(Theme.muted)
                }
            }
        } else {
            VStack(alignment: .leading, spacing: Space.m) {
                ForEach(row.readings) { reading in
                    if (reading.entry.windows ?? []).isEmpty {
                        HStack(spacing: Space.s) {
                            Text("Reading contained no quota windows")
                                .font(Type.captionSemibold).foregroundStyle(Theme.muted)
                            if reading.isStale { Chip(text: "stale", tint: Theme.amber) }
                        }
                    } else {
                        ForEach(Array((reading.entry.windows ?? []).enumerated()), id: \.offset) { _, window in
                            UsageCapacityWindowRow(
                                presentation: LimitWindowPresentation(window: window, stale: reading.isStale)
                            )
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var usageLane: some View {
        if let usage = row.usage {
            VStack(alignment: .leading, spacing: 5) {
                HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                    Text(usage.freshTokens.map(UsageTotals.compact) ?? "Tokens not reported")
                        .font(Type.dataSmallSemibold)
                        .foregroundStyle(usage.freshTokens == nil ? Theme.muted : Theme.ink)
                    Text("fresh tokens").font(Type.caption).foregroundStyle(Theme.muted)
                }
                Text(usage.sessions.map { "\($0) sessions" } ?? "Sessions not reported")
                    .font(Type.dataSmall).foregroundStyle(Theme.muted)
                HStack(spacing: 6) {
                    Text(usage.costText == "—" ? "Cost unpriced" : usage.costText)
                        .font(Type.dataSmallSemibold)
                        .foregroundStyle(usage.costText == "—" ? Theme.muted : Theme.ink)
                    if let confidence = Fmt.costConfidenceLabel(usage.costConfidence) {
                        Text(confidence).font(Type.caption).foregroundStyle(Theme.muted)
                    }
                }
            }
        } else {
            Text(usageLoaded ? "No recorded usage in this range" : "Recorded usage not loaded")
                .font(Type.captionSemibold)
                .foregroundStyle(Theme.muted)
        }
    }

    private func calibrationLabel(_ state: String) -> String {
        switch state {
        case "calibrated": return "plan share ready"
        case "calibrating": return "calibrating"
        case "never": return "no weekly share"
        default: return "calibration \(state)"
        }
    }

    private func calibrationTint(_ state: String) -> Color {
        switch state {
        case "calibrated": return Theme.accent
        case "calibrating": return Theme.amber
        default: return Theme.muted
        }
    }
}

private struct UsageCapacityWindowRow: View {
    let presentation: LimitWindowPresentation

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: Space.s) {
                Text(presentation.name).font(Type.captionSemibold).foregroundStyle(Theme.ink)
                if let span = presentation.spanText {
                    Text(span).font(Type.dataSmall).foregroundStyle(Theme.muted)
                }
                if presentation.stale { Chip(text: "stale", tint: Theme.amber) }
            }
            if let used = presentation.validUsedPercent {
                LimitMeter(usedPercent: used).accessibilityHidden(true)
            } else {
                HatchedTrack().accessibilityHidden(true)
            }
            HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                Text(presentation.statusText)
                    .font(Type.dataSmallSemibold)
                    .foregroundStyle(
                        presentation.validUsedPercent.map { Theme.limitColor(usedPercent: $0) }
                            ?? Theme.amber
                    )
                Spacer(minLength: Space.s)
                Text(presentation.resetText).font(Type.dataSmall).foregroundStyle(Theme.muted)
            }
        }
    }
}

/// Provider percentage meter. The fill caps visually at 100%, while the text
/// beside it preserves an over-limit value exactly.
struct LimitMeter: View {
    let usedPercent: Double

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 2).fill(Theme.tintNeutral)
                if usedPercent > 0 {
                    RoundedRectangle(cornerRadius: 2)
                        .fill(Theme.limitColor(usedPercent: usedPercent))
                        .frame(width: max(1, proxy.size.width * min(usedPercent / 100, 1)))
                }
            }
            .overlay {
                ZStack {
                    ForEach([0.75, 0.9], id: \.self) { notch in
                        Rectangle()
                            .fill(Theme.rule)
                            .frame(width: 1.5, height: Metrics.meterH + 4)
                            .position(
                                x: proxy.size.width * notch,
                                y: proxy.size.height / 2
                            )
                    }
                }
            }
        }
        .frame(height: Metrics.meterH)
    }
}

/// A visible absence state for a reported window without a usable percentage.
struct HatchedTrack: View {
    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 2)
                    .strokeBorder(Theme.chipLine, lineWidth: Metrics.borderW)
                HStack(spacing: 4) {
                    ForEach(0..<Int(proxy.size.width / 6), id: \.self) { _ in
                        Rectangle().fill(Theme.chipLine).frame(width: 1).rotationEffect(.degrees(45))
                    }
                }
                .clipShape(RoundedRectangle(cornerRadius: 2))
            }
        }
        .frame(height: Metrics.meterH)
    }
}
