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
    /// The reducer's one headline window (`limits[].windows[].limit_key`):
    /// the most constrained live reading every glance surface leads with.
    var headlineLimitKey: String? = nil

    enum CodingKeys: String, CodingKey {
        case schema
        case generatedAt = "generated_at"
        case daemon, usage, limits, plan
        case recentSessions = "recent_sessions"
        case headlineLimitKey = "headline_limit_key"
    }

    /// The headline window with its limit entry, or nil when none qualifies.
    var headlineLimit: (entry: LimitEntry, window: LimitWindow)? {
        guard let key = headlineLimitKey else { return nil }
        for entry in limits {
            if let window = (entry.windows ?? []).first(where: { $0.limitKey == key }) {
                return (entry, window)
            }
        }
        return nil
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
    /// The cost-prefix legend (`~$ partial subtotal · ≈$ estimate · $ reported
    /// or billed`), verbatim from the payload.
    var costLegend: String? = nil

    enum CodingKeys: String, CodingKey {
        case windows
        case usageRecordCount = "usage_record_count"
        case byClient = "by_client"
        case costLegend = "cost_legend"
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
    // Cube state and labels (additive).
    let rows: Int?
    let pricedRows: Int?
    let unpricedRows: Int?
    let cacheReadTokens: Int?
    /// `available` / `partial` / `held` / `unknown`.
    let usageAvailability: String?
    /// `none_recorded` / `held` / `unpriced` / `partial` / `complete`.
    let costState: String?
    /// The one human basis label, e.g. `mixed · mostly pricing estimate`.
    let costConfidenceDisplay: String?

    var id: String { client }

    enum CodingKeys: String, CodingKey {
        case client, sessions, rows
        case freshTokens = "fresh_tokens"
        case estimatedCostUsd = "estimated_cost_usd"
        case knownAdditiveCostUsd = "known_additive_cost_usd"
        case costComplete = "cost_complete"
        case costConfidence = "cost_confidence"
        case pricedRows = "priced_rows"
        case unpricedRows = "unpriced_rows"
        case cacheReadTokens = "cache_read_tokens"
        case usageAvailability = "usage_availability"
        case costState = "cost_state"
        case costConfidenceDisplay = "cost_confidence_display"
    }

    /// The cube's cost state as its named absence (`no usage recorded` /
    /// `unpriced`), nil when a priced figure exists or the state is missing.
    var costAbsenceText: String? {
        usageCostAbsenceText(costState: costState, rows: rows, hasFigure: costText != nil)
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
    // Cube state and labels (additive).
    let rows: Int?
    let pricedRows: Int?
    let unpricedRows: Int?
    let cacheReadTokens: Int?
    /// `available` / `partial` / `held` / `unknown`.
    let usageAvailability: String?
    /// `none_recorded` / `held` / `unpriced` / `partial` / `complete`.
    let costState: String?
    let costConfidenceDisplay: String?
    /// What this window's cost may call itself (reducer `cost_total_label`).
    var costTotalLabel: String? = nil

    enum CodingKeys: String, CodingKey {
        case costTotalLabel = "cost_total_label"
        case freshTokens = "fresh_tokens"
        case totalTokensIncludingCached = "total_tokens_including_cached"
        case estimatedCostUsd = "estimated_cost_usd"
        case costComplete = "cost_complete"
        case knownAdditiveCostUsd = "known_additive_cost_usd"
        case costConfidence = "cost_confidence"
        case rows
        case pricedRows = "priced_rows"
        case unpricedRows = "unpriced_rows"
        case cacheReadTokens = "cache_read_tokens"
        case usageAvailability = "usage_availability"
        case costState = "cost_state"
        case costConfidenceDisplay = "cost_confidence_display"
    }

    /// True when the cube recorded no usage rows for this window.
    var hasNoUsage: Bool {
        usageAvailability == "unknown" || costState == "none_recorded" || rows == 0
    }

    /// The one shared cost rule: a bare dollar figure only for a complete
    /// figure whose confidence is reported/billed; a complete ESTIMATE keeps
    /// its ≈; a partial subtotal is ~. Nothing priced is the cube state's
    /// named absence (`no usage recorded` / `unpriced`) — never $0, never a dash.
    var costText: String {
        let figure = Fmt.costDisplay(
            usd: estimatedCostUsd,
            knownAdditive: knownAdditiveCostUsd,
            complete: costComplete,
            confidence: costConfidence
        )
        return figure
            ?? usageCostAbsenceText(costState: costState, rows: rows, hasFigure: false)
            ?? PayloadAbsence.cost
    }

    var tokensText: String {
        guard let tokens = freshTokens else { return PayloadAbsence.tokens }
        return Self.compact(tokens)
    }

    static func compact(_ value: Int) -> String {
        compact(Double(value))
    }

    /// Compact a chart-scale value without converting it back through `Int`.
    /// `Double(Int.max)` rounds just beyond `Int.max`, so that conversion can
    /// trap even when the original decoded token count was a valid integer.
    static func compact(_ value: Double) -> String {
        guard value.isFinite else { return PayloadAbsence.tokens }
        // Preserve the historical Int-backed toward-zero display semantics
        // without performing a potentially trapping Double-to-Int conversion.
        let whole = value.rounded(.towardZero)
        let magnitude = abs(whole)
        switch magnitude {
        case 1_000_000_000_000_000...:
            return String(format: "%.0e", whole).replacingOccurrences(of: "e+", with: "e")
        case 1_000_000_000_000...: return String(format: "%.1fT", whole / 1_000_000_000_000)
        case 1_000_000_000...: return String(format: "%.1fB", whole / 1_000_000_000)
        case 1_000_000...: return String(format: "%.1fM", whole / 1_000_000)
        case 1_000...: return String(format: "%.1fk", whole / 1_000)
        default: return String(format: "%.0f", whole)
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
    /// The client's plan-share state and its reducer headline.
    let planShare: GlancePlanShare?
    /// How old the reading is, from its capture time (`as of 1d 15h ago`).
    var dataAgeText: String? = nil

    enum CodingKeys: String, CodingKey {
        case client, origin, org
        case dataAgeText = "data_age_text"
        case streamID = "stream_id"
        case planType = "plan_type"
        case planShare = "plan_share"
        case stale, windows
    }
}

struct LimitWindow: Decodable {
    let kind: String?
    let usedPercent: Double?
    let windowMinutes: Double?
    let resetsAt: Double?
    /// `5-hour limit` / `7-day limit` (reducer window-name map).
    let windowLabel: String?
    /// `resets in 4d 3h` / `reset passed Sep 13, 10:50 PM` / `reset time not reported`.
    let resetText: String?
    /// The window's stable key within this payload (headline selection).
    var limitKey: String? = nil
    /// True once the reported reset instant has passed: the share is history.
    var resetPassed: Bool? = nil
    /// `99% used` / `last reported 3%` (reducer value phrase).
    var valueText: String? = nil

    enum CodingKeys: String, CodingKey {
        case kind
        case usedPercent = "used_percent"
        case windowMinutes = "window_minutes"
        case resetsAt = "resets_at"
        case windowLabel = "window_label"
        case resetText = "reset_text"
        case limitKey = "limit_key"
        case resetPassed = "reset_passed"
        case valueText = "value_text"
    }

    /// The reducer's value phrase, or its named absence.
    var valueLabelText: String { PayloadAbsence.text(valueText) ?? PayloadAbsence.limitValue }

    /// The window name, or its neutral named absence.
    var windowLabelText: String { PayloadAbsence.text(windowLabel) ?? PayloadAbsence.windowLabel }

    /// The reset phrase, or its named absence when the payload carries none.
    var resetLabelText: String { PayloadAbsence.text(resetText) ?? PayloadAbsence.reset }
}

/// A plan-share state with the reducer's one headline
/// (`≈0.2% of weekly plan`, `plan share unavailable · won't calibrate at current ratio`).
struct GlancePlanShare: Decodable, Equatable {
    let pct: Double?
    let calibrationState: String?
    let headline: String?
    var chipText: String? = nil
    var sentenceText: String? = nil

    enum CodingKeys: String, CodingKey {
        case pct, headline
        case calibrationState = "calibration_state"
        case chipText = "chip_text"
        case sentenceText = "sentence_text"
    }

    /// The headline, or the named absence when the payload carries none.
    var headlineText: String { PayloadAbsence.text(headline) ?? PayloadAbsence.planShare }
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
    /// The client-level plan-share headline (reducer text).
    let headline: String?
    var chipText: String? = nil
    var sentenceText: String? = nil
    /// The technical fit detail, shown only behind a disclosure.
    var basisText: String? = nil

    enum CodingKeys: String, CodingKey {
        case client
        case confidence, headline
        case chipText = "chip_text"
        case sentenceText = "sentence_text"
        case basisText = "basis_text"
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
    /// The session's plan-share state with the reducer's headline.
    var planShare: GlancePlanShare? = nil
    /// The recorded WORK status word (`In progress`, `Completed` — the
    /// agent's report); nil when no step status was recorded.
    var statusLabel: String? = nil
    /// The session's Task decision, joined by the daemon from the same
    /// reducers the Work table uses; nil when no visible Task contains it.
    var decisionKey: String? = nil
    var decisionLabel: String? = nil
    var attentionOpen: Bool? = nil

    enum CodingKeys: String, CodingKey {
        case client
        case sessionId = "session_id"
        case title, status
        case statusLabel = "status_label"
        case decisionKey = "decision_key"
        case decisionLabel = "decision_label"
        case attentionOpen = "attention_open"
        case lastActivityAt = "last_activity_at"
        case planPct = "plan_pct"
        case planShare = "plan_share"
    }

    /// A short id cut at a token boundary: at most 8 characters, cut at the
    /// last `-` / `_` inside that prefix when the prefix would split a token,
    /// and never ending in a separator (`live-fx-1234` → `live-fx`).
    var shortSessionId: String { Self.shortId(sessionId) }

    static func shortId(_ id: String, limit: Int = 8) -> String {
        let separators: Set<Character> = ["-", "_"]
        func trimmed(_ text: Substring) -> String {
            var text = text
            while let last = text.last, separators.contains(last) { text = text.dropLast() }
            return String(text)
        }
        let characters = Array(id)
        guard characters.count > limit else {
            let short = trimmed(Substring(id))
            return short.isEmpty ? id : short
        }
        let prefix = characters[0..<limit]
        var candidate = Substring(String(prefix))
        // The prefix already ends on a token boundary when the next character
        // is a separator; otherwise cut back to the last separator inside it.
        if !separators.contains(characters[limit]),
           let cut = prefix.lastIndex(where: { separators.contains($0) }), cut > 0 {
            candidate = Substring(String(characters[0..<cut]))
        }
        let short = trimmed(candidate)
        if !short.isEmpty { return short }
        let fallback = String(id.filter { !separators.contains($0) }.prefix(limit))
        return fallback.isEmpty ? String(id.prefix(limit)) : fallback
    }

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
    // Self-update fields — all optional so an older daemon (which omits them)
    // still decodes. `current` is the clean package version; `version` stays the
    // fingerprinted build-id handshake key.
    let current: String?
    let latest: String?
    let updateAvailable: Bool?
    let isDevInstall: Bool?

    enum CodingKeys: String, CodingKey {
        case version
        case glanceSchema = "glance_schema"
        case storeDir = "store_dir"
        case current
        case latest
        case updateAvailable = "update_available"
        case isDevInstall = "is_dev_install"
    }

    /// The version to show the user: the clean package version when the daemon
    /// reports it, else the fingerprinted build id.
    var displayVersion: String? { current ?? version }

    /// Whether the Diagnostics pane offers the one-click Update button: a newer
    /// release is published AND this is not a dev/editable build. Single-sources
    /// the "notify + one-click, never silent, never for dev" rule.
    var offersInAppUpdate: Bool {
        (updateAvailable == true) && (isDevInstall != true)
    }
}

/// The named absence for a cube bucket's cost when nothing is priced:
/// `no usage recorded` (no rows) or `unpriced` (rows, none priced or all held).
/// Nil when a priced figure exists or the payload carries no state to name.
func usageCostAbsenceText(costState: String?, rows: Int?, hasFigure: Bool) -> String? {
    guard !hasFigure else { return nil }
    switch costState {
    case "none_recorded": return PayloadAbsence.noUsage
    case "unpriced", "held": return PayloadAbsence.unpriced
    case "partial", "complete": return PayloadAbsence.unpriced
    default:
        guard let rows else { return nil }
        return rows == 0 ? PayloadAbsence.noUsage : PayloadAbsence.unpriced
    }
}
