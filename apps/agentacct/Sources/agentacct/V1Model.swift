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
    let alpha: Double?
    let intervalsUsed: Int?
    let intervalsNeeded: Int?
    let rawScale: Double?
    let stateDetail: String?

    enum CodingKeys: String, CodingKey {
        case client, confidence, calibratable, basis, scale, alpha
        case calibrationState = "calibration_state"
        case intervalsUsed = "intervals_used"
        case intervalsNeeded = "intervals_needed"
        case rawScale = "raw_scale"
        case stateDetail = "state_detail"
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
    let evidenceGrade: String?
    let evidenceGradeReason: String?
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
        case evidenceGrade = "evidence_grade"
        case evidenceGradeReason = "evidence_grade_reason"
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
    let sourceType: String?
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

    /// How independent of the agent this check is — the honest counter to a
    /// check whose free-text summary claims "CI green" while its source is only
    /// the agent's own report.
    var independence: String {
        switch sourceType {
        case "ci", "external", "provider": return "CI"
        case "client_hook": return "hook"
        default: return "agent-reported"
        }
    }

    enum CodingKeys: String, CodingKey {
        case summary, files
        case eventId = "event_id"
        case createdAt = "created_at"
        case evidenceType = "evidence_type"
        case result
        case exitCode = "exit_code"
        case sourceType = "source_type"
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
    /// Subagent role read from its transcript (daemon-enriched): the agent
    /// type (Explore / Plan / workflow-subagent / …) and its Task prompt.
    let agentType: String?
    let task: String?

    var id: String { "\(client ?? "?")::\(clientSessionId ?? UUID().uuidString)" }

    /// The best human label: Task prompt first line > recorded title >
    /// agent type > short id.
    var displayTitle: String {
        if let task, let first = task.split(separator: "\n").first, !first.isEmpty {
            return String(first)
        }
        if let title, !title.isEmpty { return title }
        if let agentType, !agentType.isEmpty { return agentType }
        return clientSessionIdShort ?? clientSessionId ?? "?"
    }

    enum CodingKeys: String, CodingKey {
        case client, title, status, usage, task
        case clientSessionId = "client_session_id"
        case clientSessionIdShort = "client_session_id_short"
        case lastActivityAt = "last_activity_at"
        case planPct = "plan_pct"
        case agentType = "agent_type"
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
    let alpha: Double?
    let intervalsUsed: Int?
    let intervalsNeeded: Int?
    let rawScale: Double?
    let stateDetail: String?
    let windowPcts: [String: Double?]?
    let daily: [V1PlanDay]?
    let byModel: [V1PlanModelShare]?
    let unknownTimePct: Double?

    var id: String { client }

    enum CodingKeys: String, CodingKey {
        case client, confidence, calibratable, basis, scale, alpha, daily
        case calibrationState = "calibration_state"
        case intervalsUsed = "intervals_used"
        case intervalsNeeded = "intervals_needed"
        case rawScale = "raw_scale"
        case stateDetail = "state_detail"
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

// MARK: - Receipt (agentacct.receipt.v1)
//
// One converged Task's Work Receipt: the 8 questions, the two orthogonal axes
// (decision status × evidence strength), per-field provenance, and gaps. As
// with every /v1 decodable: additive-only, unknown keys tolerated, honesty
// rides the payload (the app never re-derives an axis or invents a number).

struct ReceiptTasksPayload: Decodable {
    let schema: String
    let tasks: [ReceiptSummary]
    let total: Int?
    let truncated: Bool?
}

struct ReceiptSummary: Decodable, Identifiable {
    let taskId: String
    let title: String?
    let decisionStatus: ReceiptDecision
    let evidenceStrength: ReceiptEvidence
    let cost: ReceiptCost
    let sessionCount: Int?
    let primaryRoot: ReceiptSessionRef?
    let lastActivityAt: Double?
    // Recency-aware handoff lifecycle marker (parallel to the decision word).
    // Optional so an older daemon payload without the field still decodes.
    let handedOff: Bool?

    var id: String { taskId }

    enum CodingKeys: String, CodingKey {
        case taskId = "task_id"
        case title
        case decisionStatus = "decision_status"
        case evidenceStrength = "evidence_strength"
        case cost
        case sessionCount = "session_count"
        case primaryRoot = "primary_root"
        case lastActivityAt = "last_activity_at"
        case handedOff = "handed_off"
    }
}

/// A {client, client_session_id} pointer — the pair `/v1/session` consumes.
struct ReceiptSessionRef: Decodable, Equatable {
    let client: String
    let clientSessionId: String

    var sessionKey: String { "\(client)::\(clientSessionId)" }

    enum CodingKeys: String, CodingKey {
        case client
        case clientSessionId = "client_session_id"
    }
}

/// One constituent session of a Task, as listed on its Receipt: enough to label
/// and address it; the full steps/checks load from `/v1/session` on expand.
struct ReceiptSessionMember: Decodable, Identifiable {
    let client: String
    let clientSessionId: String
    let sessionKind: String?
    let role: String?        // "root" | "subagent"
    let title: String?
    let project: String?
    let lastActivityAt: Double?

    var id: String { "\(client)::\(clientSessionId)" }
    var ref: ReceiptSessionRef { ReceiptSessionRef(client: client, clientSessionId: clientSessionId) }

    enum CodingKeys: String, CodingKey {
        case client, title, project, role
        case clientSessionId = "client_session_id"
        case sessionKind = "session_kind"
        case lastActivityAt = "last_activity_at"
    }
}

/// A Task's sessions grouped by root (primary or continuation), root listed
/// first then its subagents — the drill-down tree under a Receipt.
struct ReceiptSessionGroup: Decodable, Identifiable {
    let root: ReceiptSessionRef
    let role: String?        // "primary" | "continuation"
    let lineageState: String?
    let supportingCount: Int?
    let members: [ReceiptSessionMember]

    var id: String { root.sessionKey }

    enum CodingKeys: String, CodingKey {
        case root, role, members
        case lineageState = "lineage_state"
        case supportingCount = "supporting_count"
    }
}

struct ReceiptDecision: Decodable {
    let key: String
    let label: String?
    let statement: String?
    let assertedBy: String?
    // The newest blocker's own words (blocked/failed only; nil elsewhere and on
    // older daemon payloads). Daemon-computed — the app never re-derives it.
    let blocker: ReceiptBlocker?

    enum CodingKeys: String, CodingKey {
        case key, label, statement, blocker
        case assertedBy = "asserted_by"
    }
}

/// Why a Task reads blocked: the newest agent-recorded blocker (preferring the
/// newest one that carries text), plus the staleness facts beside it — and the
/// write handle for a human disposition on that exact blocker.
struct ReceiptBlocker: Decodable {
    let stepTitle: String?
    let sectionId: String?
    let text: String?
    let nextStep: String?
    let updatedAt: Double?
    let blockedStepCount: Int?
    let laterCompletedSteps: Int?
    // Disposition write handle: the exact blocked event + the optimistic
    // revision a POST /v1/disposition must echo.
    let blockedEventId: String?
    let dispositionRevision: Int?
    let disposition: ReceiptDispositionState?

    enum CodingKeys: String, CodingKey {
        case text, disposition
        case stepTitle = "step_title"
        case sectionId = "section_id"
        case nextStep = "next_step"
        case updatedAt = "updated_at"
        case blockedStepCount = "blocked_step_count"
        case laterCompletedSteps = "later_completed_steps"
        case blockedEventId = "blocked_event_id"
        case dispositionRevision = "disposition_revision"
    }
}

/// One human attention disposition (finding or blocker): append-only chain
/// state served by the daemon. Never machine verification.
struct ReceiptDispositionState: Decodable {
    let state: String?
    let revision: Int?
    let note: String?
    let updatedAt: Double?

    enum CodingKeys: String, CodingKey {
        case state, revision, note
        case updatedAt = "updated_at"
    }
}

/// The disposition handle a failing check row carries when it is a surfaced
/// finding episode of this store.
struct ReceiptCheckFinding: Decodable {
    let targetDigest: String?
    let state: String?
    let revision: Int?
    let attentionOpen: Bool?
    let note: String?

    enum CodingKeys: String, CodingKey {
        case state, revision, note
        case targetDigest = "target_digest"
        case attentionOpen = "attention_open"
    }
}

struct ReceiptByTier: Decodable {
    let externallyVerified: Int?
    let independentlyChecked: Int?
    let selfChecked: Int?
    let unchecked: Int?

    enum CodingKeys: String, CodingKey {
        case externallyVerified = "externally_verified"
        case independentlyChecked = "independently_checked"
        case selfChecked = "self_checked"
        case unchecked
    }
}

/// Evidence COVERAGE (M2): per-tier ratios over the checkable steps — the counts
/// ARE the headline, never a single collapsed grade word. ``key`` is a coarse
/// tier ordinal used only for colour. Mirrors the daemon's
/// ``evidence_coverage_headline`` / ``evidence_coverage_ledger`` so no surface
/// words the same evidence differently.
struct ReceiptEvidence: Decodable {
    let key: String
    let gradeable: Bool?
    let strongestTier: String?
    let checkableTotal: Int?
    let checkedTotal: Int?
    let byTier: ReceiptByTier?
    let notCheckable: Int?
    let openOrIncomplete: Int?
    let hiddenInSubagents: Int?
    let unattributedChecks: Int?
    let totalSteps: Int?
    let checksTotal: Int?
    let checksPassed: Int?
    let checksFailed: Int?
    let definition: String?

    enum CodingKeys: String, CodingKey {
        case key, gradeable, definition
        case strongestTier = "strongest_tier"
        case checkableTotal = "checkable_total"
        case checkedTotal = "checked_total"
        case byTier = "by_tier"
        case notCheckable = "not_checkable"
        case openOrIncomplete = "open_or_incomplete"
        case hiddenInSubagents = "hidden_in_subagents"
        case unattributedChecks = "unattributed_checks"
        case totalSteps = "total_steps"
        case checksTotal = "checks_total"
        case checksPassed = "checks_passed"
        case checksFailed = "checks_failed"
    }

    /// The coverage ratio, tier by tier. The one non-ratio case is
    /// ``Not gradeable`` (no checkable step — a 0/0 ratio is meaningless).
    var headline: String {
        if gradeable != true { return "Not gradeable (no verifiable steps recorded)" }
        let total = checkableTotal ?? 0
        var parts: [String] = []
        if let value = byTier?.externallyVerified, value > 0 { parts.append("\(value)/\(total) externally-verified") }
        if let value = byTier?.independentlyChecked, value > 0 { parts.append("\(value)/\(total) independently-checked") }
        if let value = byTier?.selfChecked, value > 0 { parts.append("\(value)/\(total) self-checked") }
        if let value = byTier?.unchecked, value > 0 { parts.append("\(value) unchecked") }
        return parts.isEmpty ? "0/\(total) checked" : parts.joined(separator: " · ")
    }

    /// A compact list-row form: "checked/checkable" tinted by the strongest tier.
    var compactHeadline: String {
        if gradeable != true { return "not gradeable" }
        return "\(checkedTotal ?? 0)/\(checkableTotal ?? 0) checked"
    }

    /// The honest ledger: where the evidence is, and what the ratio does not cover.
    var ledger: String? {
        var bits: [String] = []
        if let value = hiddenInSubagents, value > 0 { bits.append("\(value) step(s) ran in subagents") }
        if let value = notCheckable, value > 0 { bits.append("\(value) non-verifiable (research/docs)") }
        if let value = unattributedChecks, value > 0 { bits.append("\(value) check(s) attach to no step") }
        if let value = openOrIncomplete, value > 0 { bits.append("\(value) step(s) still open") }
        return bits.isEmpty ? nil : bits.joined(separator: " · ")
    }
}

/// A Task's share of its client's weekly plan — daemon-computed sum of the
/// member sessions' calibrated per-session percentages. Calibrated-or-nothing:
/// ``pct`` is nil (never 0) until the fit is calibrated, and
/// ``calibrationState`` names why.
struct ReceiptPlanShare: Decodable {
    let pct: Double?
    let client: String?
    let calibrationState: String?
    let coveredSessions: Int?
    let sessionCount: Int?

    enum CodingKeys: String, CodingKey {
        case pct, client
        case calibrationState = "calibration_state"
        case coveredSessions = "covered_sessions"
        case sessionCount = "session_count"
    }

    /// "≈X.X% of weekly plan" — nil when not calibrated (absence stays named
    /// by the calibration state, never rendered as a number).
    var text: String? {
        guard let formatted = Fmt.planPct(pct) else { return nil }
        return "\(formatted) of weekly plan"
    }
}

struct ReceiptCost: Decodable {
    let estimatedCostUsd: Double?
    let costBasis: String?
    let costConfidence: String?
    let costComplete: Bool?
    let planShare: ReceiptPlanShare?

    enum CodingKeys: String, CodingKey {
        case estimatedCostUsd = "estimated_cost_usd"
        case costBasis = "cost_basis"
        case costConfidence = "cost_confidence"
        case costComplete = "cost_complete"
        case planShare = "plan_share"
    }

    /// None-never-$0: an absent estimate is "—", never a fabricated zero.
    var text: String {
        guard let estimatedCostUsd else { return "—" }
        let basis = costBasis ?? "unknown basis"
        return String(format: "$%.2f · %@", estimatedCostUsd, basis)
    }
}

struct Receipt: Decodable {
    let schemaVersion: String
    let taskId: String
    let title: String?
    let axes: ReceiptAxes
    let dimensions: ReceiptDimensions
    let sessions: [ReceiptSessionGroup]?
    /// Task wall-clock span as the daemon computed it (nil when the store
    /// cannot bound it — the record page names that absence).
    let durationSeconds: Double?

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case taskId = "task_id"
        case durationSeconds = "duration_seconds"
        case title, axes, dimensions, sessions
    }
}

struct ReceiptAxes: Decodable {
    let decisionStatus: ReceiptDecision
    let evidenceStrength: ReceiptEvidence
    // A third, orthogonal signal: the deliberate-stop lifecycle marker, kept out
    // of decisionStatus so a handoff shows BESIDE a finding/blocked headline.
    // Optional so an older daemon payload without the field still decodes.
    let handoff: ReceiptHandoff?
    let orthogonalityNote: String?

    enum CodingKeys: String, CodingKey {
        case decisionStatus = "decision_status"
        case evidenceStrength = "evidence_strength"
        case handoff
        case orthogonalityNote = "orthogonality_note"
    }
}

/// The handoff lifecycle marker. ``handedOff`` is the recency-aware disposition
/// from the daemon — true only when the handoff is the Task's frontier (nothing
/// still-open is newer), so a resumed Task does not carry it.
struct ReceiptHandoff: Decodable {
    let handedOff: Bool?
    let statement: String?
    let assertedBy: String?

    enum CodingKeys: String, CodingKey {
        case handedOff = "handed_off"
        case statement
        case assertedBy = "asserted_by"
    }
}

struct ReceiptDimensions: Decodable {
    let task: ReceiptTaskDim
    let actors: ReceiptActorsDim
    let actions: ReceiptActionsDim
    let cost: ReceiptCostDim
    let evidence: ReceiptEvidenceDim
    let outcome: ReceiptOutcomeDim
    let gaps: ReceiptGapsDim
    let provenance: ReceiptProvenanceDim
}

struct ReceiptBoundary: Decodable {
    let project: String?
    let identityScope: String?
    let sessionCount: Int?

    enum CodingKeys: String, CodingKey {
        case project
        case identityScope = "identity_scope"
        case sessionCount = "session_count"
    }
}

struct ReceiptTaskDim: Decodable {
    let objectives: [String]?
    let boundary: ReceiptBoundary?
    let provenance: [String]?
    let gaps: [String]?
}

struct ReceiptActorsDim: Decodable {
    let primaryAgent: String?
    let models: [String]?
    let subagentSessionCount: Int?
    let childSessionCount: Int?
    let provenance: [String]?
    let gaps: [String]?

    enum CodingKeys: String, CodingKey {
        case primaryAgent = "primary_agent"
        case models
        case subagentSessionCount = "subagent_session_count"
        case childSessionCount = "child_session_count"
        case provenance, gaps
    }
}

struct ReceiptActionsDim: Decodable {
    let toolCategoryCounts: [String: Int]?
    let toolCategoryTotal: Int?
    let touchedFileCount: Int?
    let provenance: [String]?
    let gaps: [String]?

    enum CodingKeys: String, CodingKey {
        case toolCategoryCounts = "tool_category_counts"
        case toolCategoryTotal = "tool_category_total"
        case touchedFileCount = "touched_file_count"
        case provenance, gaps
    }
}

/// The receipt cost dimension's token tally (daemon-computed; the app never
/// sums usage rows itself). Optional so an older payload still decodes.
struct ReceiptCostTokens: Decodable {
    let fresh: Int?
    let cacheCreation: Int?
    let cacheRead: Int?
    let total: Int?

    enum CodingKeys: String, CodingKey {
        case fresh, total
        case cacheCreation = "cache_creation"
        case cacheRead = "cache_read"
    }
}

struct ReceiptCostDim: Decodable {
    let estimatedCostUsd: Double?
    let costBasis: String?
    let costConfidence: String?
    let costComplete: Bool?
    let planShare: ReceiptPlanShare?
    let tokens: ReceiptCostTokens?
    let provenance: [String]?
    let gaps: [String]?

    enum CodingKeys: String, CodingKey {
        case estimatedCostUsd = "estimated_cost_usd"
        case costBasis = "cost_basis"
        case costConfidence = "cost_confidence"
        case costComplete = "cost_complete"
        case planShare = "plan_share"
        case tokens
        case provenance, gaps
    }
}

struct ReceiptCheck: Decodable, Identifiable {
    let kind: String?
    let name: String?
    let result: String?
    let exitCode: Int?
    let scope: String?
    let source: String?
    // Detail-on-expand fields; every one optional so an older payload decodes.
    let superseded: Bool?
    let at: Double?
    let summary: String?
    let files: [String]?
    // The store NEVER records command text (privacy: names/categories yes,
    // args no); true says a command existed but was deliberately not captured.
    let commandRedacted: Bool?
    let artifactRef: String?
    let artifactUrl: String?
    // Present only on a failing check that is a surfaced finding episode —
    // the handle the disposition controls post with.
    let finding: ReceiptCheckFinding?

    var id: String { "\(name ?? "check")-\(result ?? "")-\(exitCode ?? 0)" }

    enum CodingKeys: String, CodingKey {
        case kind, name, result, scope, source, superseded, at, summary, files, finding
        case exitCode = "exit_code"
        case commandRedacted = "command_redacted"
        case artifactRef = "artifact_ref"
        case artifactUrl = "artifact_url"
    }
}

struct ReceiptEvidenceDim: Decodable {
    let checks: [ReceiptCheck]?
    let checksTotal: Int?
    let checksPassed: Int?
    let checksFailed: Int?
    let provenance: [String]?
    let gaps: [String]?

    enum CodingKeys: String, CodingKey {
        case checks
        case checksTotal = "checks_total"
        case checksPassed = "checks_passed"
        case checksFailed = "checks_failed"
        case provenance, gaps
    }
}

struct ReceiptOutcomeDim: Decodable {
    let decisionStatus: String?
    let statement: String?
    let assertedBy: String?
    let provenance: [String]?
    let gaps: [String]?

    enum CodingKeys: String, CodingKey {
        case decisionStatus = "decision_status"
        case statement
        case assertedBy = "asserted_by"
        case provenance, gaps
    }
}

struct ReceiptGapItem: Decodable, Identifiable {
    let dimension: String
    let reason: String
    var id: String { "\(dimension)-\(reason)" }
}

struct ReceiptGapsDim: Decodable {
    let items: [ReceiptGapItem]?
    let count: Int?
}

struct ReceiptProvenanceDim: Decodable {
    let byDimension: [String: [String]]?
    let sourcesPresent: [String]?
    let legend: [String: String]?

    enum CodingKeys: String, CodingKey {
        case byDimension = "by_dimension"
        case sourcesPresent = "sources_present"
        case legend
    }
}
