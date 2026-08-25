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
    /// unpriced session renders as an em-dash — never $0.00.
    var costText: String {
        guard let cost = estimatedCostUsd else { return "—" }
        return Fmt.dollars(cost, prefix: costConfidence == "client_reported" ? "$" : "≈$")
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

    var id: String { workId ?? sectionId ?? UUID().uuidString }

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

    var id: String { workId ?? sectionId ?? UUID().uuidString }

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

    enum CodingKeys: String, CodingKey {
        case byClient = "by_client"
        case byModel = "by_model"
        case byPeriod = "by_period"
        case totals
    }
}

struct PeriodBucket: Decodable {
    let period: String?
    let freshTokens: Int?
    let estimatedCostUsd: Double?
    let costComplete: Bool?
    let byClient: [String: PeriodClientSlice]?

    /// "08-05" from "2026-08-05" for axis labels.
    var shortLabel: String {
        guard let period, period.count >= 10 else { return period ?? "" }
        return String(period.dropFirst(5))
    }

    /// The shared cost honesty rule (complete-$ / partial-~$ / em-dash).
    var costText: String {
        guard let cost = estimatedCostUsd else { return "—" }
        return Fmt.dollars(cost, prefix: costComplete == true ? "$" : "~$")
    }

    enum CodingKeys: String, CodingKey {
        case period
        case freshTokens = "fresh_tokens"
        case estimatedCostUsd = "estimated_cost_usd"
        case costComplete = "cost_complete"
        case byClient = "by_client"
    }
}

struct PeriodClientSlice: Decodable {
    let freshTokens: Int?

    enum CodingKeys: String, CodingKey {
        case freshTokens = "fresh_tokens"
    }
}

struct UsageBucket: Decodable, Identifiable {
    let client: String?
    let model: String?
    let sessions: Int?
    let freshTokens: Int?
    let cacheReadTokens: Int?
    let estimatedCostUsd: Double?
    let costComplete: Bool?
    let knownAdditiveCostUsd: Double?
    let costConfidenceLabel: String?

    var id: String { "\(client ?? "?")::\(model ?? "*")" }

    enum CodingKeys: String, CodingKey {
        case client, model, sessions
        case freshTokens = "fresh_tokens"
        case cacheReadTokens = "cache_read_tokens"
        case estimatedCostUsd = "estimated_cost_usd"
        case costComplete = "cost_complete"
        case knownAdditiveCostUsd = "known_additive_cost_usd"
        case costConfidenceLabel = "cost_confidence_label"
    }

    /// The shared cube cost rule: complete `$` / partial `~$` / `—`.
    var costText: String {
        if costComplete == true, let cost = estimatedCostUsd {
            return Fmt.dollars(cost)
        }
        if let known = knownAdditiveCostUsd {
            return Fmt.dollars(known, prefix: "~$")
        }
        return "—"
    }
}
