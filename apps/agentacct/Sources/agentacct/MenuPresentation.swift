import Foundation

struct MenuLimitItem: Identifiable, Equatable {
    let id: String
    let client: String
    let clientLabel: String
    let windowLabel: String
    let usedPercent: Double?
    let resetText: String?

    var percentageText: String {
        guard let usedPercent else { return "Not reported" }
        guard usedPercent.isFinite,
              usedPercent >= 0,
              let rounded = Int(exactly: usedPercent.rounded())
        else {
            return "Invalid percentage"
        }
        if usedPercent > 0, usedPercent < 1 { return "<1%" }
        return "\(rounded)%"
    }

    var sourceLabel: String { "\(clientLabel) · \(windowLabel)" }
}

/// Keeps menu copy deterministic and honest without coupling presentation
/// rules to view geometry.
struct MenuLimitPresentation {
    let primary: MenuLimitItem?
    let secondary: [MenuLimitItem]
    let hiddenSecondaryCount: Int
    let hasStaleLimits: Bool

    init(glance: Glance) {
        var items: [MenuLimitItem] = []
        for (limitIndex, limit) in glance.limits.enumerated() where limit.stale != true {
            let client = limit.client ?? "unknown"
            let streamID = limit.streamID ?? "legacy-stream-\(limitIndex)"
            for (windowIndex, window) in (limit.windows ?? []).enumerated() {
                let kind = window.kind ?? "window"
                items.append(MenuLimitItem(
                    id: [streamID, client, limit.org ?? "", limit.origin ?? "", kind, "\(windowIndex)"]
                        .joined(separator: "|"),
                    client: client,
                    clientLabel: Self.clientLabel(client),
                    windowLabel: Self.windowLabel(kind),
                    usedPercent: window.usedPercent,
                    resetText: Theme.resetsIn(window.resetsAt)
                ))
            }
        }

        let weekly = items.filter { $0.windowLabel == "7-day limit" && $0.usedPercent != nil }
        let selectedPrimary = weekly.filter { $0.client == "claude-code" }
            .max(by: { ($0.usedPercent ?? 0) < ($1.usedPercent ?? 0) })
            ?? weekly.max(by: { ($0.usedPercent ?? 0) < ($1.usedPercent ?? 0) })
        primary = selectedPrimary

        let remaining = items.filter { $0.id != selectedPrimary?.id }
        secondary = Array(remaining.prefix(3))
        hiddenSecondaryCount = max(0, remaining.count - secondary.count)
        hasStaleLimits = glance.limits.contains { $0.stale == true }
    }

    static func clientLabel(_ client: String) -> String {
        switch client {
        case "codex": return "Codex"
        case "claude-code": return "Claude Code"
        default:
            return client
                .replacingOccurrences(of: "-", with: " ")
                .split(separator: " ")
                .map { $0.prefix(1).uppercased() + $0.dropFirst() }
                .joined(separator: " ")
        }
    }

    static func windowLabel(_ kind: String) -> String {
        switch kind.lowercased() {
        case "7d": return "7-day limit"
        case "5h": return "5-hour limit"
        case "1d", "today": return "daily limit"
        default: return "\(kind) limit"
        }
    }
}

struct MenuUsageRow: Identifiable, Equatable {
    let days: Int
    let label: String
    let costText: String
    let tokenText: String

    var id: Int { days }
}

struct MenuUsagePresentation {
    let rows: [MenuUsageRow]
    let legendText: String?

    init(usage: Usage) {
        let definitions = [(1, "Today"), (7, "Last 7 days"), (30, "Last 30 days")]
        rows = definitions.map { days, label in
            let window = usage.windows.first(where: { $0.days == days })
                ?? usage.windows.first(where: { Self.normalizedDays($0.label) == days })
            let rawCost = window?.totals.costText ?? "—"
            let rawTokens = window?.totals.tokensText ?? "—"
            return MenuUsageRow(
                days: days,
                label: label,
                costText: rawCost == "—" ? "Unpriced" : rawCost,
                tokenText: rawTokens == "—" ? "Not reported" : rawTokens
            )
        }

        let costTexts = rows.map(\.costText)
        var legend: [String] = []
        if costTexts.contains(where: { $0.hasPrefix("≈$") }) {
            legend.append("≈ estimate")
        }
        if costTexts.contains(where: { $0.hasPrefix("~$") }) {
            legend.append("~ priced subtotal")
        }
        legendText = legend.isEmpty ? nil : legend.joined(separator: " · ")
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
    let summary: String
    let detail: String?

    init?(_ plan: [PlanEntry]) {
        guard let entry = plan.first(where: { $0.calibrationState == "calibrating" }) else {
            return nil
        }
        let client = MenuLimitPresentation.clientLabel(entry.client)
        if let used = entry.intervalsUsed, let needed = entry.intervalsNeeded {
            summary = "\(client) session share calibrating · \(used)/\(needed) intervals"
        } else {
            summary = "\(client) session share calibrating"
        }
        detail = entry.stateDetail
    }
}
