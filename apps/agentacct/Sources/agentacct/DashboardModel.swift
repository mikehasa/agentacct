import Foundation

// Models for the full window: the /sessions rollup and /usage/summary cube.
// Same decoding stance as the glance: tolerant, selective, never inventing a
// number an absent field does not carry.

struct SessionsPayload: Decodable {
    let sessions: [SessionEntry]
    let totalSessions: Int?

    enum CodingKeys: String, CodingKey {
        case sessions
        case totalSessions = "total_sessions"
    }
}

struct SessionEntry: Decodable, Identifiable {
    let client: String
    let clientSessionId: String
    let shortId: String?
    let title: String?
    let sessionKind: String?
    let project: String?
    let durationSeconds: Double?
    let lastActivityAt: Double?
    let usage: SessionUsage?
    let join: SessionJoin?
    let work: SessionWork?
    let related: SessionRelated?
    let usageNote: String?
    let observedModels: [String]?

    var id: String { "\(client)::\(clientSessionId)" }
    var isRoot: Bool { (related?.parent ?? nil) == nil }
    var displayTitle: String { title ?? "\(client) · \(shortId ?? String(clientSessionId.prefix(8)))" }

    enum CodingKeys: String, CodingKey {
        case client
        case clientSessionId = "client_session_id"
        case shortId = "client_session_id_short"
        case title = "client_session_title"
        case sessionKind = "session_kind"
        case project
        case durationSeconds = "duration_seconds"
        case lastActivityAt = "last_activity_at"
        case usage, join, work, related
        case usageNote = "usage_note"
        case observedModels = "observed_models"
    }
}

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
        let prefix = costConfidence == "client_reported" ? "$" : "≈$"
        return String(format: "%@%.2f", prefix, cost)
    }
}

struct SessionJoin: Decodable {
    let state: String?
    let reason: String?
}

struct SessionWork: Decodable {
    let items: [WorkItem]?
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
    let parent: String?
    let childSessionCount: Int?
    let childrenUsage: ChildrenUsage?

    enum CodingKeys: String, CodingKey {
        case parent
        case childSessionCount = "child_session_count"
        case childrenUsage = "children_usage"
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
    let totals: UsageBucket?

    enum CodingKeys: String, CodingKey {
        case byClient = "by_client"
        case byModel = "by_model"
        case totals
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
            return String(format: "$%.2f", cost)
        }
        if let known = knownAdditiveCostUsd {
            return String(format: "~$%.2f", known)
        }
        return "—"
    }
}
