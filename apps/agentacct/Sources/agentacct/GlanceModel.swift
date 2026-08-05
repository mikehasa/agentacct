import Foundation

// The /v1/glance payload (schema agentacct.glance.v1). Additive-only on the
// wire: decode tolerantly, ignore unknown keys, never invent a number a field
// does not carry — an absent cost renders as absent, not $0.00.

struct Glance: Decodable {
    let schema: String
    let generatedAt: Double?
    let daemon: DaemonInfo?
    let usage: Usage
    let limits: [LimitEntry]
    let plan: [PlanEntry]
    let recentSessions: [RecentSession]

    enum CodingKeys: String, CodingKey {
        case schema
        case generatedAt = "generated_at"
        case daemon, usage, limits, plan
        case recentSessions = "recent_sessions"
    }
}

struct DaemonInfo: Decodable {
    let version: String?
    let pid: Int?
}

struct Usage: Decodable {
    let windows: [UsageWindow]
    let usageRecordCount: Int?

    enum CodingKeys: String, CodingKey {
        case windows
        case usageRecordCount = "usage_record_count"
    }
}

struct UsageWindow: Decodable {
    let label: String
    let days: Int?
    let totals: UsageTotals
}

struct UsageTotals: Decodable {
    let freshTokens: Int?
    let totalTokensIncludingCached: Int?
    let estimatedCostUsd: Double?
    let costComplete: Bool?
    let knownAdditiveCostUsd: Double?

    enum CodingKeys: String, CodingKey {
        case freshTokens = "fresh_tokens"
        case totalTokensIncludingCached = "total_tokens_including_cached"
        case estimatedCostUsd = "estimated_cost_usd"
        case costComplete = "cost_complete"
        case knownAdditiveCostUsd = "known_additive_cost_usd"
    }

    /// The one shared cost rule (mirrors usage_snapshot.cost_text): a plain
    /// dollar figure ONLY when the cube vouches completeness; a partial subtotal
    /// is marked approximate; nothing priced renders as an em-dash — never $0.
    var costText: String {
        if costComplete == true, let cost = estimatedCostUsd {
            return Fmt.dollars(cost)
        }
        if let known = knownAdditiveCostUsd {
            return Fmt.dollars(known, prefix: "~$")
        }
        return "—"
    }

    var tokensText: String {
        guard let tokens = freshTokens else { return "—" }
        return Self.compact(tokens)
    }

    static func compact(_ value: Int) -> String {
        let magnitude = abs(value)
        switch magnitude {
        case 1_000_000_000...: return String(format: "%.1fB", Double(value) / 1_000_000_000)
        case 1_000_000...: return String(format: "%.1fM", Double(value) / 1_000_000)
        case 1_000...: return String(format: "%.1fk", Double(value) / 1_000)
        default: return "\(value)"
        }
    }
}

struct LimitEntry: Decodable {
    let client: String?
    let planType: String?
    let stale: Bool?
    let windows: [LimitWindow]?

    enum CodingKeys: String, CodingKey {
        case client
        case planType = "plan_type"
        case stale, windows
    }
}

struct LimitWindow: Decodable {
    let kind: String?
    let usedPercent: Double?
    let windowMinutes: Double?
    let resetsAt: Double?

    enum CodingKeys: String, CodingKey {
        case kind
        case usedPercent = "used_percent"
        case windowMinutes = "window_minutes"
        case resetsAt = "resets_at"
    }
}

struct PlanEntry: Decodable {
    let client: String
    let confidence: String
}

struct RecentSession: Decodable {
    let client: String
    let sessionId: String
    let title: String?
    let status: String?
    let lastActivityAt: Double?
    let planPct: Double?

    enum CodingKeys: String, CodingKey {
        case client
        case sessionId = "session_id"
        case title, status
        case lastActivityAt = "last_activity_at"
        case planPct = "plan_pct"
    }

    var shortSessionId: String { String(sessionId.prefix(8)) }

    /// TUI-parity plan share formatting: one decimal with the approximation
    /// marker, a "<0.1%" band instead of a fake exact zero, nothing when the
    /// estimate is withheld (uncalibrated).
    var planPctText: String? {
        guard let pct = planPct else { return nil }
        if pct > 0 && pct < 0.1 { return "≈<0.1%" }
        return String(format: "≈%.1f%%", pct)
    }

    var statusGlyph: String {
        switch status {
        case "blocked": return "⚠"
        case "handed_off": return "↗"
        case "in_progress": return "▶"
        case "completed": return "✓"
        default: return "·"
        }
    }
}

struct VersionInfo: Decodable {
    let version: String
    let glanceSchema: String
    let storeDir: String?

    enum CodingKeys: String, CodingKey {
        case version
        case glanceSchema = "glance_schema"
        case storeDir = "store_dir"
    }
}
