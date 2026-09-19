import Foundation

struct MenuLimitItem: Identifiable, Equatable {
    let id: String
    /// The client slug (`claude-code`, `codex`) — the one client identity every
    /// surface shows, rendered in mono (C54). Never title-cased here.
    let client: String
    /// The window's raw kind key (`7d`, `5h`).
    let kind: String
    /// The reducer's window name (`7-day limit`), or its named absence.
    let windowLabel: String
    let usedPercent: Double?
    /// The reducer's reset phrase (`resets in 4d 3h` / `reset passed …` /
    /// `reset time not reported`). Never nil: a past reset is never absence.
    let resetText: String
    /// The reducer's value phrase (`99% used` / `last reported 3%`).
    var valueText: String = PayloadAbsence.limitValue
    /// True once the window's reset passed: its share is history, not current.
    var resetPassed: Bool = false
    /// How old the reading is (`as of 1d 15h ago`), from its capture time.
    var dataAgeText: String? = nil

    var sourceLabel: String { "\(client) · \(windowLabel)" }
}

/// Keeps menu copy deterministic and honest without coupling presentation
/// rules to view geometry. Every display word comes from the glance payload
/// (`window_label`, `reset_text`, `value_text`), and the headline is the
/// reducer's `headline_limit_key` (the most constrained live window) — this
/// type only looks it up and orders the rest.
struct MenuLimitPresentation {
    let primary: MenuLimitItem?
    let secondary: [MenuLimitItem]
    let hiddenSecondaryCount: Int
    let hasStaleLimits: Bool

    init(glance: Glance) {
        var items: [MenuLimitItem] = []
        var primaryID: String?
        for (limitIndex, limit) in glance.limits.enumerated() where limit.stale != true {
            let client = limit.client ?? "unknown"
            let streamID = limit.streamID ?? "legacy-stream-\(limitIndex)"
            for (windowIndex, window) in (limit.windows ?? []).enumerated() {
                let kind = window.kind ?? "window"
                let id = [streamID, client, limit.org ?? "", limit.origin ?? "", kind, "\(windowIndex)"]
                    .joined(separator: "|")
                if let key = glance.headlineLimitKey, window.limitKey == key { primaryID = id }
                items.append(MenuLimitItem(
                    id: id,
                    client: client,
                    kind: kind,
                    windowLabel: window.windowLabelText,
                    usedPercent: window.usedPercent,
                    resetText: window.resetLabelText,
                    valueText: window.valueLabelText,
                    resetPassed: window.resetPassed == true,
                    dataAgeText: PayloadAbsence.text(limit.dataAgeText)
                ))
            }
        }

        primary = items.first { $0.id == primaryID }
        let remaining = items.filter { $0.id != primaryID }
        secondary = Array(remaining.prefix(3))
        hiddenSecondaryCount = max(0, remaining.count - secondary.count)
        hasStaleLimits = glance.limits.contains { $0.stale == true }
    }
}

struct MenuUsageRow: Identifiable, Equatable {
    let days: Int
    let label: String
    let costText: String
    /// The fresh-token figure; nil when the window recorded no usage at all,
    /// so the row names that absence ONCE (in `costText`) and never prints a
    /// fabricated `0` beside it.
    let tokenText: String?
    /// True when `costText` is a priced figure rather than a named absence.
    let isPriced: Bool
    /// The cube's human basis label (`mixed · mostly pricing estimate`) for a
    /// priced row; nil for an absence.
    let basisText: String?

    var id: Int { days }
}

struct MenuUsagePresentation {
    let rows: [MenuUsageRow]
    /// `<basis> · <legend>` when any row is priced; nil otherwise. The legend
    /// is the glance payload's `usage.cost_legend`; without it only the basis
    /// shows (Swift keeps no copy of the grammar).
    let legendText: String?

    init(usage: Usage) {
        let definitions = [(1, "Today"), (7, "Last 7 days"), (30, "Last 30 days")]
        rows = definitions.map { days, label in
            let window = usage.windows.first(where: { $0.days == days })
                ?? usage.windows.first(where: { Self.normalizedDays($0.label) == days })
            guard let totals = window?.totals else {
                return MenuUsageRow(
                    days: days,
                    label: label,
                    costText: PayloadAbsence.cost,
                    tokenText: PayloadAbsence.tokens,
                    isPriced: false,
                    basisText: nil
                )
            }
            let figure = Fmt.costDisplay(
                usd: totals.estimatedCostUsd,
                knownAdditive: totals.knownAdditiveCostUsd,
                complete: totals.costComplete,
                confidence: totals.costConfidence
            )
            let costText: String
            if let figure {
                costText = figure
            } else if totals.hasNoUsage {
                // Nothing recorded is its own named state, never "unpriced".
                costText = PayloadAbsence.noUsage
            } else {
                costText = totals.costText
            }
            return MenuUsageRow(
                days: days,
                label: label,
                costText: costText,
                tokenText: totals.hasNoUsage ? nil : totals.tokensText,
                isPriced: figure != nil,
                basisText: figure == nil
                    ? nil
                    : (PayloadAbsence.text(totals.costConfidenceDisplay) ?? PayloadAbsence.costBasis)
            )
        }

        let priced = rows.filter(\.isPriced)
        if priced.isEmpty {
            legendText = nil
        } else {
            // One basis for the ledger: when the priced windows agree, that
            // label; otherwise the widest priced window's label, whose rows
            // include the narrower windows' rows (it reads `mixed · …`).
            let bases = priced.compactMap(\.basisText)
            let distinct = Array(Set(bases))
            let basis = distinct.count == 1
                ? distinct[0]
                : (priced.max(by: { $0.days < $1.days })?.basisText ?? PayloadAbsence.costBasis)
            legendText = [basis, PayloadAbsence.text(usage.costLegend)]
                .compactMap { $0 }
                .joined(separator: " · ")
        }
    }

    private static func normalizedDays(_ label: String) -> Int? {
        switch label.lowercased().replacingOccurrences(of: " ", with: "") {
        case "today", "1d", "last1day": return 1
        case "7d", "last7days": return 7
        case "30d", "last30days": return 30
        default: return nil
        }
    }
}

struct MenuCalibrationPresentation: Equatable {
    /// The client slug, rendered mono by the view.
    let client: String
    /// The reducer's plain-language conclusion (`Won't calibrate: recorded
    /// usage doesn't track the weekly % closely enough`).
    let headline: String
    /// `used/needed intervals`, only while calibration is still progressing
    /// (used < needed). Never shown for an out-of-band fit.
    let progressText: String?
    /// The reducer's technical fit detail, behind the help affordance.
    let detail: String?

    /// `<slug> <headline> · <progress>`.
    var summary: String {
        (["\(client) \(headline)"] + [progressText].compactMap { $0 })
            .joined(separator: " · ")
    }

    init?(_ plan: [PlanEntry]) {
        guard let entry = plan.first(where: {
            $0.calibrationState == "calibrating" || $0.calibrationState == "out_of_band"
        }) else {
            return nil
        }
        client = entry.client
        headline = PayloadAbsence.text(entry.headline) ?? PayloadAbsence.planShare
        if entry.calibrationState == "calibrating",
           let used = entry.intervalsUsed,
           let needed = entry.intervalsNeeded,
           used < needed {
            progressText = "\(used)/\(needed) intervals"
        } else {
            progressText = nil
        }
        detail = PayloadAbsence.text(entry.basisText) ?? PayloadAbsence.text(entry.stateDetail)
    }
}
