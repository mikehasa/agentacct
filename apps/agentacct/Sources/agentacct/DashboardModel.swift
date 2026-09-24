import Foundation

// Shared decodables for the daemon's session sub-blocks (usage/join/work/
// related — embedded verbatim in /v1/sessions rows, see V1Model.swift) and
// the /usage/summary cube that feeds the cost charts. Same decoding stance
// as the glance: tolerant, selective, never inventing a number an absent
// field does not carry. (The legacy /sessions list decoders left with the
// /v1 migration.)

struct SessionUsage: Decodable {
    let rows: Int?
    let freshTokens: Int?
    let cacheCreationTokens: Int?
    let cacheReadTokens: Int?
    let totalTokens: Int?
    let estimatedCostUsd: Double?
    let costConfidence: String?
    let turnsTotal: Int?
    let excludedNonAdditiveRows: Int?

    enum CodingKeys: String, CodingKey {
        case rows
        case freshTokens = "fresh_tokens"
        case cacheCreationTokens = "cache_creation_tokens"
        case cacheReadTokens = "cache_read_tokens"
        case totalTokens = "total_tokens"
        case estimatedCostUsd = "estimated_cost_usd"
        case costConfidence = "cost_confidence"
        case turnsTotal = "turns_total"
        case excludedNonAdditiveRows = "excluded_non_additive_rows"
    }

    /// Per-session cost honesty: an estimate renders as an estimate, an
    /// unpriced session renders as an em-dash — never $0.00. A reported OR billed
    /// figure is exact; matches Fmt.costDisplay / receiptCostDisplay so the same
    /// cost never reads exact at the task level and estimated at the step level.
    var costText: String {
        guard let cost = estimatedCostUsd else { return "—" }
        let reported = costConfidence == "client_reported" || costConfidence == "provider_billed"
        return Fmt.dollars(cost, prefix: reported ? "$" : "≈$")
    }
}

struct SessionJoin: Decodable {
    let state: String?
    let reason: String?
    /// Per-row join outcome counts (attributed / ambiguous /
    /// context_matched_unallocated / unjoined) + veto count.
    let rowStates: [String: Int]?
    let vetoedRows: Int?
    let attributedFreshTokens: Int?
    let attributedTotalTokens: Int?
    let attributedWork: [AttributedWork]?
    let ambiguousCandidateWorkIds: [String]?

    enum CodingKeys: String, CodingKey {
        case state, reason
        case rowStates = "row_states"
        case vetoedRows = "vetoed_rows"
        case attributedFreshTokens = "attributed_fresh_tokens"
        case attributedTotalTokens = "attributed_total_tokens"
        case attributedWork = "attributed_work"
        case ambiguousCandidateWorkIds = "ambiguous_candidate_work_ids"
    }
}

struct AttributedWork: Decodable, Identifiable {
    let workId: String?
    let sectionId: String?
    let title: String?
    let joinStrategy: String?
    let joinConfidence: String?
    private let fallbackId = UUID().uuidString

    var id: String { workId ?? sectionId ?? fallbackId }

    enum CodingKeys: String, CodingKey {
        case title
        case workId = "work_id"
        case sectionId = "section_id"
        case joinStrategy = "join_strategy"
        case joinConfidence = "join_confidence"
    }
}

struct SessionWork: Decodable {
    let items: [WorkItem]?
    let counts: WorkCounts?
    let evidence: EvidenceCounts?
}

struct WorkCounts: Decodable {
    let total: Int?
    let completed: Int?
    let resolved: Int?
    let active: Int?
    let blocked: Int?
}

struct EvidenceCounts: Decodable {
    let strong: Int?
    let weak: Int?
    let failed: Int?
    let none: Int?
}

struct WorkItem: Decodable, Identifiable {
    let workId: String?
    let sectionId: String?
    let title: String?
    let latestStatus: String?
    let evidenceStatus: String?
    private let fallbackId = UUID().uuidString

    var id: String { workId ?? sectionId ?? fallbackId }

    enum CodingKeys: String, CodingKey {
        case workId = "work_id"
        case sectionId = "section_id"
        case title
        case latestStatus = "latest_status"
        case evidenceStatus = "evidence_status"
    }

    var statusGlyph: String {
        switch latestStatus {
        case "blocked": return "⚠"
        case "handed_off": return "↗"
        case "started", "checkpoint": return "▶"
        case "completed": return "✓"
        default: return "·"
        }
    }
}

struct SessionRelated: Decodable {
    let parent: ParentRef?
    let childSessionCount: Int?
    let childrenUsage: ChildrenUsage?

    enum CodingKeys: String, CodingKey {
        case parent
        case childSessionCount = "child_session_count"
        case childrenUsage = "children_usage"
    }
}

struct ParentRef: Decodable {
    let clientSessionId: String?
    let label: String?

    enum CodingKeys: String, CodingKey {
        case clientSessionId = "client_session_id"
        case label
    }
}

struct ChildrenUsage: Decodable {
    let sessions: Int?
    let freshTokens: Int?

    enum CodingKeys: String, CodingKey {
        case sessions
        case freshTokens = "fresh_tokens"
    }
}

// /usage/summary

struct UsageSummary: Decodable {
    let byClient: [UsageBucket]
    let byModel: [UsageBucket]
    let byPeriod: [PeriodBucket]?
    let totals: UsageBucket?
    let filtersEcho: UsageFiltersEcho?
    let periodAttribution: UsagePeriodAttribution?

    enum CodingKeys: String, CodingKey {
        case byClient = "by_client"
        case byModel = "by_model"
        case byPeriod = "by_period"
        case totals
        case filtersEcho = "filters_echo"
        case periodAttribution = "period_attribution"
    }
}

struct UsagePeriodAttribution: Decodable {
    let label: String?
    let description: String?
    let exactDailyUsage: Bool?
    enum CodingKeys: String, CodingKey {
        case label, description
        case exactDailyUsage = "exact_daily_usage"
    }
}

/// What the cube says it applied. Every field is optional: an older daemon
/// echoes only `granularity`/`days`, and the pane treats a missing field as
/// "not confirmed" rather than assuming it was honored.
struct UsageFiltersEcho: Decodable {
    let granularity: String?
    let client: String?
    let model: String?
    let provider: String?
    let days: String?
    /// The daemon's own verdict on a model/provider filter: false means no
    /// saved row carries that value, and the payload is the empty result.
    let modelMatchesSavedRows: Bool?
    let providerMatchesSavedRows: Bool?
    let rangeMode: String?
    let resolvedStart: String?
    let resolvedEnd: String?

    /// The window the daemon resolved for an explicit range, as the results
    /// summary prints it; nil when it did not report one.
    var resolvedRange: String? {
        guard let resolvedStart, let resolvedEnd else { return nil }
        return "\(resolvedStart) – \(resolvedEnd)"
    }

    enum CodingKeys: String, CodingKey {
        case granularity, client, model, provider, days
        case modelMatchesSavedRows = "model_matches_saved_rows"
        case providerMatchesSavedRows = "provider_matches_saved_rows"
        case rangeMode = "range_mode"
        case resolvedStart = "resolved_start"
        case resolvedEnd = "resolved_end"
    }
}

struct PeriodBucket: Decodable {
    let period: String?
    let byClient: [String: PeriodClientSlice]?
    let byModel: [UsageBucket]?
    let usage: UsageBucket

    var freshTokens: Int? { usage.freshTokens }
    var totalTokensIncludingCached: Int? { usage.totalTokensIncludingCached }
    var estimatedCostUsd: Double? { usage.estimatedCostUsd }
    var costComplete: Bool? { usage.costComplete }
    var costConfidence: String? { usage.costConfidence }
    var costText: String {
        // Before full period buckets, legacy daemons supplied a subtotal only
        // as estimated_cost_usd. Preserve its partial marker.
        if usage.costComplete != true, usage.knownAdditiveCostUsd == nil,
           let subtotal = usage.estimatedCostUsd { return Fmt.dollars(subtotal, prefix: "~$") }
        return usage.costText
    }
    var shortLabel: String {
        guard let period, period.count >= 10 else { return period ?? "Unknown date" }
        return String(period.dropFirst(5))
    }

    enum CodingKeys: String, CodingKey {
        case period
        case byClient = "by_client"
        case byModel = "by_model"
    }
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        period = try container.decodeIfPresent(String.self, forKey: .period)
        byClient = try container.decodeIfPresent([String: PeriodClientSlice].self, forKey: .byClient)
        byModel = try container.decodeIfPresent([UsageBucket].self, forKey: .byModel)
        usage = try UsageBucket(from: decoder)
    }
}

/// Old daemons send only tokens here; all new fields remain optional.
typealias PeriodClientSlice = UsageBucket

struct UsageBucket: Decodable, Identifiable {
    let client: String?
    let model: String?
    let provider: String?
    let rows: Int?
    let inputTokens: Int?
    let outputTokens: Int?
    let cacheCreationTokens: Int?
    let cacheCreationReporting: String?
    let cacheReadReporting: String?
    let usageAvailability: String?
    let sessions: Int?
    let freshTokens: Int?
    let totalTokensIncludingCached: Int?
    let cacheReadTokens: Int?
    let estimatedCostUsd: Double?
    let costComplete: Bool?
    let knownAdditiveCostUsd: Double?
    let costConfidence: String?
    let costConfidenceLabel: String?

    var id: String { [client ?? "?", provider ?? "?", model ?? "*"].map { "\($0.utf8.count):\($0)" }.joined() }

    enum CodingKeys: String, CodingKey {
        case client, model, provider, rows, sessions
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case cacheCreationTokens = "cache_creation_tokens"
        case cacheCreationReporting = "cache_creation_reporting"
        case cacheReadReporting = "cache_read_reporting"
        case usageAvailability = "usage_availability"
        case freshTokens = "fresh_tokens"
        case totalTokensIncludingCached = "total_tokens_including_cached"
        case cacheReadTokens = "cache_read_tokens"
        case estimatedCostUsd = "estimated_cost_usd"
        case costComplete = "cost_complete"
        case knownAdditiveCostUsd = "known_additive_cost_usd"
        case costConfidence = "cost_confidence"
        case costConfidenceLabel = "cost_confidence_label"
    }

    /// The shared cost grammar ($ reported / ≈$ estimate / ~$ partial / —).
    var costText: String {
        Fmt.costDisplay(
            usd: costComplete == true ? estimatedCostUsd : nil,
            knownAdditive: knownAdditiveCostUsd,
            complete: costComplete,
            confidence: costConfidence
        ) ?? "—"
    }
}
