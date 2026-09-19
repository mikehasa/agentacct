import Foundation
import SwiftUI

/// One recorded-cost fact for a usage-cube bucket (a client, model or range
/// total), shared by the Usage strip, the capacity ledger and accessibility.
/// The amount keeps the shared cost grammar; the basis is the payload's
/// `cost_confidence_display`. Absence and partial coverage are mutually
/// exclusive: "Partial subtotal" appears only beside a priced figure whose
/// cube state is `partial`, and a bucket with nothing priced carries exactly
/// one named absence instead of a figure.
struct UsageCostPresentation: Equatable {
    /// The grammar-prefixed amount, or nil when nothing is priced.
    let figure: String?
    /// The single named state shown when `figure` is nil.
    let absence: String
    /// The payload basis label, or its named absence.
    let basis: String
    let isPartial: Bool
    /// The reducer's `cost_total_label`: `total`, `Partial subtotal · N of M
    /// usage records unpriced`, or the named absence. Never rebuilt in Swift.
    let totalLabel: String

    init(
        costText: String,
        hasFigure: Bool,
        costState: String?,
        costComplete: Bool?,
        rows: Int?,
        pricedRows: Int?,
        unpricedRows: Int?,
        costConfidenceDisplay: String?,
        costTotalLabel: String? = nil
    ) {
        figure = hasFigure ? costText : nil
        basis = PayloadAbsence.text(costConfidenceDisplay) ?? PayloadAbsence.costBasis
        isPartial = hasFigure && costState == "partial"
        totalLabel = PayloadAbsence.text(costTotalLabel) ?? PayloadAbsence.costLabel
        // Nothing priced: the reducer's label IS the named absence.
        absence = PayloadAbsence.text(costTotalLabel) ?? costText
    }

    init(bucket: UsageBucket) {
        self.init(
            costText: bucket.costText,
            hasFigure: bucket.estimatedCostUsd != nil || bucket.knownAdditiveCostUsd != nil,
            costState: bucket.costState,
            costComplete: bucket.costComplete,
            rows: bucket.rows,
            pricedRows: bucket.pricedRows,
            unpricedRows: bucket.unpricedRows,
            costConfidenceDisplay: bucket.costConfidenceDisplay,
            costTotalLabel: bucket.costTotalLabel
        )
    }

    init(totals: UsageTotals) {
        self.init(
            costText: totals.costText,
            hasFigure: totals.estimatedCostUsd != nil || totals.knownAdditiveCostUsd != nil,
            costState: totals.costState,
            costComplete: totals.costComplete,
            rows: totals.rows,
            pricedRows: totals.pricedRows,
            unpricedRows: totals.unpricedRows,
            costConfidenceDisplay: totals.costConfidenceDisplay,
            costTotalLabel: totals.costTotalLabel
        )
    }

    /// The amount, or the named absence.
    var valueText: String { figure ?? absence }

    /// The basis line under a priced figure; nil when nothing is priced (the
    /// absence already is the whole fact).
    var qualifier: String? {
        guard figure != nil else { return nil }
        return isPartial ? "\(totalLabel) · \(basis)" : basis
    }

    var accessibilityText: String {
        guard let figure else { return absence }
        return [figure, qualifier].compactMap { $0 }.joined(separator: ", ")
    }
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
            parts.append(usage.sessions.map { Fmt.count($0, "session") } ?? "sessions not reported")
            parts.append(UsageCostPresentation(bucket: usage).accessibilityText)
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

    /// The reducer's window name (`5-hour limit`, `7-day limit`), or its
    /// neutral named absence.
    var name: String { window.windowLabelText }

    /// Threshold markers drawn on the meter (fractions of the limit).
    static let thresholds: [Double] = [0.75, 0.9]

    /// True once the window's reset passed: the share is history (K32).
    var resetPassed: Bool { window.resetPassed == true }

    /// The reducer's value phrase (`99% used` / `last reported 3%`). The
    /// window name already names its span, so no span text repeats it.
    var statusText: String { window.valueLabelText }

    /// Threshold text color (ink / amber / coral); muted for a passed reset.
    var statusColor: Color {
        guard !resetPassed, let used = validUsedPercent else { return Theme.muted }
        return Theme.limitTextColor(usedPercent: used)
    }

    /// The reducer's reset phrase (`resets in 4d 3h`, `reset passed Sep 15,
    /// 3:36 AM`, `reset time not reported`), capitalized only because it
    /// starts its own line here. The words are never rebuilt in Swift.
    var resetText: String {
        Self.sentenceStart(window.resetLabelText)
    }

    static func sentenceStart(_ text: String) -> String {
        guard let first = text.first else { return text }
        return first.uppercased() + text.dropFirst()
    }

    var accessibilityText: String {
        var parts = [name, statusText, resetText]
        if stale { parts.append("stale reading") }
        return parts.joined(separator: ", ")
    }
}

/// Compact, truthful plan-share detail for the secondary disclosure. Plan
/// estimates never become a competing page mode or a capacity meter, but the
/// daemon's calibration, daily-series, and per-model facts remain inspectable.
struct UsagePlanPresentation {
    let client: V1PlanClient
    let days: Int

    /// The plain-language conclusion first (reducer `headline`), then the
    /// calibrated share figures. The technical fit detail is `basisText`,
    /// shown only behind a disclosure (K113).
    var detailText: String {
        var parts: [String] = [PayloadAbsence.text(client.headline) ?? PayloadAbsence.planShare]
        // A progress fraction is only meaningful while calibration is still
        // short of its interval target ("160 of 3" is not progress).
        if let used = client.intervalsUsed, let needed = client.intervalsNeeded, used < needed {
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
        return parts.joined(separator: " · ")
    }

    /// The reducer's technical fit detail (fit ratio, bands), or nil.
    var basisText: String? {
        PayloadAbsence.text(client.basisText)
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
            let tokensText = Self.tokenText(share.totalTokens, label: client.modelTokensLabel)
            return "\(model) · \(shareText) · \(tokensText)"
        }
    }

    /// The payload chip (`plan share ready`, `calibrating`, …) from the one
    /// vocabulary table; Swift keeps no copy of the words.
    var chipText: String {
        PayloadAbsence.text(client.chipText) ?? PayloadAbsence.planShare
    }

    /// Styling only (never text): the state's chip tint.
    static func chipTint(calibrationState state: String) -> Color {
        switch state {
        case "calibrating": return Theme.amber
        default: return Theme.muted
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

    private static func tokenText(_ value: Double?, label: String?) -> String {
        let measure = PayloadAbsence.text(label) ?? PayloadAbsence.tokens
        guard let value else { return "\(measure) not reported" }
        guard value.isFinite, value >= 0,
              let whole = Int(exactly: value.rounded()) else {
            return "invalid token total"
        }
        return "\(UsageTotals.compact(whole)) \(measure)"
    }
}

struct UsageCapacityLedger: View {
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    let rows: [UsageCapacityRow]
    let days: Int
    let usageLoaded: Bool

    var body: some View {
        Card(padding: 0) {
            VStack(spacing: 0) {
                if !dynamicTypeSize.isAccessibilitySize {
                    HStack(spacing: Space.l) {
                        CapsLabel(text: "Client").frame(width: 160, alignment: .leading)
                        CapsLabel(text: "Provider window").frame(maxWidth: .infinity, alignment: .leading)
                        CapsLabel(text: RecordedUsageVocabulary.columnLabel(days: days)).frame(width: 230, alignment: .leading)
                    }
                    .padding(.horizontal, Space.xl)
                    .frame(height: Metrics.rowHeader)
                    .accessibilityHidden(true)

                    Rectangle().fill(Theme.hairline).frame(height: 1).padding(.horizontal, Space.xl)

                }

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
                    Text("Provider windows").workFont(.captionSemibold).foregroundStyle(Theme.muted)
                    capacityLane
                    Rectangle().fill(Theme.hairline).frame(height: 1)
                    Text(RecordedUsageVocabulary.columnLabel(days: days)).workFont(.captionSemibold).foregroundStyle(Theme.muted)
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
                .workFont(.rowLabel)
                .foregroundStyle(Theme.ink)
                .lineLimit(2)
                .truncationMode(.middle)
                .help(row.client)
            if !row.planTypes.isEmpty {
                Text(row.planTypes.joined(separator: " · "))
                    .workFont(.dataSmall)
                    .foregroundStyle(Theme.muted)
            }
            if let plan = row.plan, let state = plan.calibrationState {
                // The column is 160pt: a long chip wraps inside it rather
                // than spilling into the provider-window caption.
                FittingChip(
                    text: UsagePlanPresentation(client: plan, days: days).chipText,
                    tint: UsagePlanPresentation.chipTint(calibrationState: state)
                )
            }
        }
    }

    @ViewBuilder
    private var capacityLane: some View {
        if row.readings.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                Text(row.hasHiddenStaleReading ? "No live provider limit" : "Provider limit not reported")
                    .workFont(.captionSemibold)
                    .foregroundStyle(Theme.muted)
                if row.hasHiddenStaleReading {
                    Text("A stale reading is hidden")
                        .workFont(.caption)
                        .foregroundStyle(Theme.muted)
                }
            }
        } else {
            VStack(alignment: .leading, spacing: Space.m) {
                ForEach(row.readings) { reading in
                    if (reading.entry.windows ?? []).isEmpty {
                        HStack(spacing: Space.s) {
                            Text("Reading contained no quota windows")
                                .workFont(.captionSemibold).foregroundStyle(Theme.muted)
                            if reading.isStale { Chip(text: "stale", tint: Theme.amber) }
                        }
                    } else {
                        ForEach(Array((reading.entry.windows ?? []).enumerated()), id: \.offset) { _, window in
                            UsageCapacityWindowRow(
                                presentation: LimitWindowPresentation(window: window, stale: reading.isStale),
                                dataAgeText: PayloadAbsence.text(reading.entry.dataAgeText)
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
                        .workFont(.dataSmallSemibold)
                        .foregroundStyle(usage.freshTokens == nil ? Theme.muted : Theme.ink)
                    Text("fresh tokens").workFont(.caption).foregroundStyle(Theme.muted)
                }
                Text(usage.sessions.map { Fmt.count($0, "session") } ?? "Sessions not reported")
                    .workFont(.dataSmall).foregroundStyle(Theme.muted)
                let cost = UsageCostPresentation(bucket: usage)
                VStack(alignment: .leading, spacing: 4) {
                    Text(cost.valueText)
                        .workFont(cost.figure == nil ? .caption : .dataSmallSemibold)
                        .foregroundStyle(cost.figure == nil ? Theme.muted : Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                    if let qualifier = cost.qualifier {
                        Text(qualifier)
                            .workFont(.caption).foregroundStyle(Theme.muted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        } else {
            Text(usageLoaded ? "No recorded usage in this range" : "Recorded usage not loaded")
                .workFont(.captionSemibold)
                .foregroundStyle(Theme.muted)
        }
    }

}

private struct UsageCapacityWindowRow: View {
    let presentation: LimitWindowPresentation
    var dataAgeText: String? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: Space.s) {
                Text(presentation.name).workFont(.captionSemibold).foregroundStyle(Theme.ink)
                if let dataAgeText {
                    // How old the provider reading is (K32), not the poll time.
                    Text(dataAgeText).workFont(.dataSmall).foregroundStyle(Theme.muted)
                }
                if presentation.stale { Chip(text: "stale", tint: Theme.amber) }
            }
            if let used = presentation.validUsedPercent {
                LimitMeter(usedPercent: used, resetPassed: presentation.resetPassed).accessibilityHidden(true)
            } else {
                // Same footprint as a meter with its threshold ticks.
                HatchedTrack()
                    .padding(.vertical, MeterBar.tickOverhang)
                    .accessibilityHidden(true)
            }
            HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                Text(presentation.statusText)
                    .workFont(.dataSmallSemibold)
                    .foregroundStyle(presentation.statusColor)
                Spacer(minLength: Space.s)
                Text(presentation.resetText).workFont(FieldFont.resetText).foregroundStyle(Theme.muted)
            }
        }
    }
}

/// Provider percentage meter: the shared `MeterBar` with the fill-weight limit
/// color and the 75%/90% markers drawn as ink ticks outside the bar. The fill
/// caps visually at 100%, while the text beside it preserves an over-limit
/// value exactly.
struct LimitMeter: View {
    let usedPercent: Double
    /// A passed-reset share draws in the muted fill (history, not a threshold).
    var resetPassed: Bool = false

    var body: some View {
        Theme.MeterBar(
            fraction: usedPercent / 100,
            tint: resetPassed ? Theme.muted : Theme.limitFillColor(usedPercent: usedPercent),
            thresholds: LimitWindowPresentation.thresholds
        )
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
