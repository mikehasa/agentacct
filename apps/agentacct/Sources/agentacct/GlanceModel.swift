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
    // The 7-day per-client cube slice the daemon already serves (same numbers
    // as the Usage pane's cube). Optional so an older payload still decodes.
    let byClient: [GlanceClientUsage]?

    enum CodingKeys: String, CodingKey {
        case windows
        case usageRecordCount = "usage_record_count"
        case byClient = "by_client"
    }
}

/// One client's 7-day usage slice from the glance cube — what a per-agent
/// dashboard row can honestly state for ANY recording client, limits or not.
struct GlanceClientUsage: Decodable, Identifiable {
    let client: String
    let freshTokens: Int?
    let estimatedCostUsd: Double?
    let knownAdditiveCostUsd: Double?
    let costComplete: Bool?
    let costConfidence: String?
    let sessions: Int?

    var id: String { client }

    enum CodingKeys: String, CodingKey {
        case client, sessions
        case freshTokens = "fresh_tokens"
        case estimatedCostUsd = "estimated_cost_usd"
        case knownAdditiveCostUsd = "known_additive_cost_usd"
        case costComplete = "cost_complete"
        case costConfidence = "cost_confidence"
    }

    /// The app-wide cost grammar; nil when nothing is priced (callers name it).
    var costText: String? {
        Fmt.costDisplay(
            usd: estimatedCostUsd,
            knownAdditive: knownAdditiveCostUsd,
            complete: costComplete,
            confidence: costConfidence
        )
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
    let costConfidence: String?

    enum CodingKeys: String, CodingKey {
        case freshTokens = "fresh_tokens"
        case totalTokensIncludingCached = "total_tokens_including_cached"
        case estimatedCostUsd = "estimated_cost_usd"
        case costComplete = "cost_complete"
        case knownAdditiveCostUsd = "known_additive_cost_usd"
        case costConfidence = "cost_confidence"
    }

    /// The one shared cost rule: a bare dollar figure only for a complete
    /// figure whose confidence is reported/billed; a complete ESTIMATE keeps
    /// its ≈; a partial subtotal is ~; nothing priced is an em-dash — never $0.
    var costText: String {
        Fmt.costDisplay(
            usd: estimatedCostUsd,
            knownAdditive: knownAdditiveCostUsd,
            complete: costComplete,
            confidence: costConfidence
        ) ?? "—"
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
    let streamID: String?
    let origin: String?
    let org: String?
    let planType: String?
    let stale: Bool?
    let windows: [LimitWindow]?

    enum CodingKeys: String, CodingKey {
        case client, origin, org
        case streamID = "stream_id"
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
    /// Three-state display semantic from the daemon: "calibrated",
    /// "calibrating" (can calibrate, warming up), or "never" (this client's
    /// meter cannot yield a weekly plan % — codex). Optional so a pre-field
    /// daemon still decodes; absent means "don't claim calibrating".
    let calibrationState: String?
    let intervalsUsed: Int?
    let intervalsNeeded: Int?
    let stateDetail: String?

    enum CodingKeys: String, CodingKey {
        case client
        case confidence
        case calibrationState = "calibration_state"
        case intervalsUsed = "intervals_used"
        case intervalsNeeded = "intervals_needed"
        case stateDetail = "state_detail"
    }
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
    /// estimate is withheld (uncalibrated). One shared rule with the window
    /// (Fmt.planPct) so the two surfaces can never disagree on a zero share.
    var planPctText: String? { Fmt.planPct(planPct) }

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
