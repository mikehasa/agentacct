import Foundation

// Decodables for the /v1 native-shell lane beyond the glance:
// /v1/sessions (paginated roots list), /v1/session (one-session deep view),
// /v1/plan (attributed plan aggregates). Contract: additive-only schemas —
// every struct tolerates unknown keys, every field the daemon may omit is
// Optional, and honesty semantics ride the payload (calibrated-or-nothing
// plan numbers, None-never-$0 costs) rather than being re-derived here.

struct V1SessionsPayload: Decodable {
    let schema: String
    let generatedAt: Double?
    let totalSessions: Int?
    let totalRootSessions: Int?
    let filteredTotal: Int?
    let offset: Int?
    let limit: Int?
    let returned: Int?
    let truncated: Bool?
    let plan: [V1PlanStatus]?
    let sessions: [V1SessionRow]

    enum CodingKeys: String, CodingKey {
        case schema
        case generatedAt = "generated_at"
        case totalSessions = "total_sessions"
        case totalRootSessions = "total_root_sessions"
        case filteredTotal = "filtered_total"
        case offset, limit, returned, truncated, plan, sessions
    }
}

struct V1PlanStatus: Decodable {
    let client: String
    let confidence: String?
    let calibrationState: String?
    let calibratable: Bool?
    let basis: String?
    let scale: Double?
    let intervalsUsed: Int?

    enum CodingKeys: String, CodingKey {
        case client, confidence, calibratable, basis, scale
        case calibrationState = "calibration_state"
        case intervalsUsed = "intervals_used"
    }
}

struct V1SessionRow: Decodable, Identifiable {
    let sessionKey: String?
    let client: String
    let clientSessionId: String
    let clientSessionIdShort: String?
    let sessionKind: String?
    let title: String?
    let project: String?
    let status: String?
    let firstActivityAt: Double?
    let lastActivityAt: Double?
    let durationSeconds: Double?
    let instrumentationState: String?
    let observedModels: [String]?
    let usage: SessionUsage?
    let usageNote: String?
    let join: SessionJoin?
    let work: SessionWork?
    let related: SessionRelated?
    let planPctOwn: Double?
    let planPctChildren: Double?
    let planPct: Double?

    var id: String { sessionKey ?? "\(client)::\(clientSessionId)" }

    var displayTitle: String {
        if let title, !title.isEmpty { return title }
        return "\(client) · \(clientSessionIdShort ?? String(clientSessionId.prefix(8)))"
    }

    enum CodingKeys: String, CodingKey {
        case client, title, project, status, usage, join, work, related
        case sessionKey = "session_key"
        case clientSessionId = "client_session_id"
        case clientSessionIdShort = "client_session_id_short"
        case sessionKind = "session_kind"
        case firstActivityAt = "first_activity_at"
        case lastActivityAt = "last_activity_at"
        case durationSeconds = "duration_seconds"
        case instrumentationState = "instrumentation_state"
        case observedModels = "observed_models"
        case usageNote = "usage_note"
        case planPctOwn = "plan_pct_own"
        case planPctChildren = "plan_pct_children"
        case planPct = "plan_pct"
    }
}

// MARK: - /v1/session detail

struct V1SessionDetail: Decodable {
    let schema: String
    let generatedAt: Double?
    let session: V1SessionRow
    let steps: [V1Step]
    let descendants: [V1Descendant]
    let plan: V1SessionPlan?

    enum CodingKeys: String, CodingKey {
        case schema, session, steps, descendants, plan
        case generatedAt = "generated_at"
    }
}

struct V1Step: Decodable, Identifiable {
    let workId: String?
    let sectionId: String?
    let title: String?
    let latestStatus: String?
    let kind: String?
    let phase: String?
    let startedAt: Double?
    let updatedAt: Double?
    let summary: String?
    let files: [String]?
    let blocker: String?
    let nextStep: String?
    let usage: V1StepUsage?
    let joinConfidence: String?
    let evidenceStatus: String?
    let models: [V1ModelLane]?
    let checks: [V1Check]?

    var id: String { workId ?? sectionId ?? UUID().uuidString }

    enum CodingKeys: String, CodingKey {
        case title, kind, phase, summary, files, blocker, usage, models, checks
        case workId = "work_id"
        case sectionId = "section_id"
        case latestStatus = "latest_status"
        case startedAt = "started_at"
        case updatedAt = "updated_at"
        case nextStep = "next_step"
        case joinConfidence = "join_confidence"
        case evidenceStatus = "evidence_status"
    }
}

struct V1StepUsage: Decodable {
    let totalTokens: Double?
    let freshTokens: Double?
    let cacheReadTokens: Double?
    let cacheCreationTokens: Double?
    let estimatedCostUsd: Double?
    let linkedUsageRecords: Int?
    let pricedUsageRecords: Int?
    let unpricedUsageRecords: Int?

    enum CodingKeys: String, CodingKey {
        case totalTokens = "total_tokens"
        case freshTokens = "fresh_tokens"
        case cacheReadTokens = "cache_read_tokens"
        case cacheCreationTokens = "cache_creation_tokens"
        case estimatedCostUsd = "estimated_cost_usd"
        case linkedUsageRecords = "linked_usage_records"
        case pricedUsageRecords = "priced_usage_records"
        case unpricedUsageRecords = "unpriced_usage_records"
    }

    /// The shared cost honesty rule: None-never-$0; a value with unpriced
    /// rows alongside is a partial subtotal (~$).
    var costText: String {
        guard let cost = estimatedCostUsd else { return "—" }
        let partial = (unpricedUsageRecords ?? 0) > 0
        return Fmt.dollars(cost, prefix: partial ? "~$" : "$")
    }
}

struct V1ModelLane: Decodable, Identifiable {
    let model: String?
    let provider: String?
    let totalTokens: Double?

    var id: String { "\(provider ?? "?")/\(model ?? "unknown")" }

    enum CodingKeys: String, CodingKey {
        case model, provider
        case totalTokens = "total_tokens"
    }
}

struct V1Check: Decodable, Identifiable {
    let eventId: String?
    let createdAt: Double?
    let evidenceType: String?
    let result: String?
    let summary: String?
    let exitCode: Int?
    let checkIdentity: String?
    let supersessionState: String?
    let resolutionScope: String?
    let resolutionSummary: String?
    let files: [String]?
    let artifactRef: String?
    let artifactPath: String?
    let artifactUrl: String?
    let commandRedacted: Bool?

    var id: String { eventId ?? UUID().uuidString }

    enum CodingKeys: String, CodingKey {
        case summary, files
        case eventId = "event_id"
        case createdAt = "created_at"
        case evidenceType = "evidence_type"
        case result
        case exitCode = "exit_code"
        case checkIdentity = "check_identity"
        case supersessionState = "supersession_state"
        case resolutionScope = "resolution_scope"
        case resolutionSummary = "resolution_summary"
        case artifactRef = "artifact_ref"
        case artifactPath = "artifact_path"
        case artifactUrl = "artifact_url"
        case commandRedacted = "command_redacted"
    }
}

struct V1Descendant: Decodable, Identifiable {
    let client: String?
    let clientSessionId: String?
    let clientSessionIdShort: String?
    let title: String?
    let status: String?
    let lastActivityAt: Double?
    let usage: V1DescendantUsage?
    let planPct: Double?

    var id: String { "\(client ?? "?")::\(clientSessionId ?? UUID().uuidString)" }

    enum CodingKeys: String, CodingKey {
        case client, title, status, usage
        case clientSessionId = "client_session_id"
        case clientSessionIdShort = "client_session_id_short"
        case lastActivityAt = "last_activity_at"
        case planPct = "plan_pct"
    }
}

struct V1DescendantUsage: Decodable {
    let totalTokens: Double?
    let freshTokens: Double?
    let estimatedCostUsd: Double?
    let costConfidence: String?

    enum CodingKeys: String, CodingKey {
        case totalTokens = "total_tokens"
        case freshTokens = "fresh_tokens"
        case estimatedCostUsd = "estimated_cost_usd"
        case costConfidence = "cost_confidence"
    }
}

struct V1SessionPlan: Decodable {
    let client: String?
    let confidence: String?
    let calibrationState: String?
    let basis: String?
    let scale: Double?
    let pctOwn: Double?
    let pctChildren: Double?
    let pct: Double?
    let byModel: [V1PlanModelShare]?

    enum CodingKeys: String, CodingKey {
        case client, confidence, basis, scale, pct
        case calibrationState = "calibration_state"
        case pctOwn = "pct_own"
        case pctChildren = "pct_children"
        case byModel = "by_model"
    }
}

struct V1PlanModelShare: Decodable, Identifiable {
    let model: String?
    let totalTokens: Double?
    let pct: Double?

    var id: String { model ?? "unknown" }

    enum CodingKeys: String, CodingKey {
        case model, pct
        case totalTokens = "total_tokens"
    }
}

// MARK: - /v1/plan

struct V1PlanPayload: Decodable {
    let schema: String
    let generatedAt: Double?
    let days: Int?
    let clients: [V1PlanClient]

    enum CodingKeys: String, CodingKey {
        case schema, days, clients
        case generatedAt = "generated_at"
    }
}

struct V1PlanClient: Decodable, Identifiable {
    let client: String
    let confidence: String?
    let calibrationState: String?
    let calibratable: Bool?
    let basis: String?
    let scale: Double?
    let intervalsUsed: Int?
    let windowPcts: [String: Double?]?
    let daily: [V1PlanDay]?
    let byModel: [V1PlanModelShare]?
    let unknownTimePct: Double?

    var id: String { client }

    enum CodingKeys: String, CodingKey {
        case client, confidence, calibratable, basis, scale, daily
        case calibrationState = "calibration_state"
        case intervalsUsed = "intervals_used"
        case windowPcts = "window_pcts"
        case byModel = "by_model"
        case unknownTimePct = "unknown_time_pct"
    }
}

struct V1PlanDay: Decodable, Identifiable {
    let date: String
    let pct: Double

    var id: String { date }
}

// MARK: - shared plan formatting

extension Fmt {
    /// The TUI's plan-share rule: ≈X.X% with a <0.1% band, never a claimed
    /// exact zero for a nonzero share. nil in → nil out (calibrated-or-nothing).
    static func planPct(_ pct: Double?) -> String? {
        guard let pct, pct > 0 else { return nil }
        return pct >= 0.1 ? String(format: "≈%.1f%%", pct) : "≈<0.1%"
    }
}
