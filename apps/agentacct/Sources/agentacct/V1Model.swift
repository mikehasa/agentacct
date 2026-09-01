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
    private let fallbackId = UUID().uuidString

    var id: String { workId ?? sectionId ?? fallbackId }

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
    let costConfidence: String?

    enum CodingKeys: String, CodingKey {
        case totalTokens = "total_tokens"
        case freshTokens = "fresh_tokens"
        case cacheReadTokens = "cache_read_tokens"
        case cacheCreationTokens = "cache_creation_tokens"
        case estimatedCostUsd = "estimated_cost_usd"
        case linkedUsageRecords = "linked_usage_records"
        case pricedUsageRecords = "priced_usage_records"
        case unpricedUsageRecords = "unpriced_usage_records"
        case costConfidence = "cost_confidence"
    }

    /// The shared cost honesty rule: None-never-$0; a value with unpriced rows
    /// alongside is a partial subtotal (~$); a complete figure is exact ("$")
    /// only when its priced records are all reported/billed — an estimated
    /// (token-priced) step reads "≈$" rather than over-claiming exactness.
    var costText: String {
        guard let cost = estimatedCostUsd else { return "—" }
        if (unpricedUsageRecords ?? 0) > 0 { return Fmt.dollars(cost, prefix: "~$") }
        let reported = costConfidence == "client_reported" || costConfidence == "provider_billed"
        return Fmt.dollars(cost, prefix: reported ? "$" : "≈$")
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
    let supersededByEventId: String?
    let resolutionScope: String?
    let resolutionSummary: String?
    let resolvesBlockedEventId: String?
    let files: [String]?
    let artifactRef: String?
    let artifactPath: String?
    let artifactUrl: String?
    let commandRedacted: Bool?
    let artifactPathRedacted: Bool?
    let artifactUrlRedacted: Bool?
    private let fallbackId = UUID().uuidString

    var id: String { eventId ?? fallbackId }

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
        case supersededByEventId = "superseded_by_event_id"
        case resolutionScope = "resolution_scope"
        case resolutionSummary = "resolution_summary"
        case resolvesBlockedEventId = "resolves_blocked_event_id"
        case artifactRef = "artifact_ref"
        case artifactPath = "artifact_path"
        case artifactUrl = "artifact_url"
        case commandRedacted = "command_redacted"
        case artifactPathRedacted = "artifact_path_redacted"
        case artifactUrlRedacted = "artifact_url_redacted"
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
    private let fallbackId = UUID().uuidString

    var id: String { "\(client ?? "?")::\(clientSessionId ?? fallbackId)" }

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
    /// Exact all-store attention count plus a bounded Dashboard preview.
    /// Optional so the app can fail closed against an older daemon.
    let attention: ReceiptAttentionPayload?
}

struct ReceiptAttentionPayload: Decodable {
    let tasks: [ReceiptSummary]
    let total: Int
    let limit: Int?
    let truncated: Bool
}

/// Complete review classification plus one page from `/v1/attention`. Unlike
/// `/v1/tasks`, `total` and `counts` classify every visible Task before paging,
/// so a client can make an honest empty or aggregate claim without scanning a
/// recent-work page locally.
struct V1AttentionPayload: Decodable {
    let schema: String
    let items: [ReceiptSummary]
    let total: Int
    let counts: V1AttentionCounts
    let snapshot: String?
    let offset: Int
    let limit: Int
    let truncated: Bool

    init(
        schema: String,
        items: [ReceiptSummary],
        total: Int,
        counts: V1AttentionCounts,
        snapshot: String?,
        offset: Int,
        limit: Int,
        truncated: Bool
    ) {
        self.schema = schema
        self.items = items
        self.total = total
        self.counts = counts
        self.snapshot = snapshot
        self.offset = offset
        self.limit = limit
        self.truncated = truncated
    }

    private enum CodingKeys: String, CodingKey {
        case schema, items, total, counts, snapshot, offset, limit, truncated
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        schema = try container.decode(String.self, forKey: .schema)
        items = try container.decode([ReceiptSummary].self, forKey: .items)
        total = try container.decode(Int.self, forKey: .total)
        counts = try container.decode(V1AttentionCounts.self, forKey: .counts)
        snapshot = try container.decodeIfPresent(String.self, forKey: .snapshot)
        offset = try container.decodeIfPresent(Int.self, forKey: .offset) ?? 0
        limit = try container.decode(Int.self, forKey: .limit)
        truncated = try container.decode(Bool.self, forKey: .truncated)
    }
}

struct V1AttentionCounts: Decodable, Equatable {
    let failedCheck: Int
    let failedStep: Int
    let blocker: Int

    enum CodingKeys: String, CodingKey {
        case failedCheck = "failed_check"
        case failedStep = "failed_step"
        case blocker
    }
}

/// The server-selected leading reason for one attention Task. The summary and
/// next step are recorded evidence; a missing `next_step` deliberately remains
/// nil so the UI cannot turn a generic suggestion into an agent claim.
struct ReceiptAttention: Decodable {
    let kind: String
    let summary: String
    let nextStep: String?
    let observedAt: Double?
    let source: String?

    enum CodingKeys: String, CodingKey {
        case kind, summary, source
        case nextStep = "next_step"
        case observedAt = "observed_at"
    }
}

struct ReceiptSummary: Decodable, Identifiable {
    let taskId: String
    let title: String?
    /// Present on the attention projection; optional for older `/v1/tasks`
    /// payloads and older daemons.
    let project: String?
    /// Present only on `/v1/attention` rows.
    let attention: ReceiptAttention?
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
        case title, project, attention
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
        let presentation = ReceiptCoveragePresentation(evidence: self)
        if presentation.isInconsistent {
            return "\(presentation.value) (\(presentation.qualifier))"
        }
        guard gradeable != false, let total = checkableTotal, total > 0 else {
            return "\(presentation.value) (\(presentation.qualifier))"
        }
        var parts: [String] = []
        if let value = byTier?.externallyVerified, value > 0 { parts.append("\(value)/\(total) externally-verified") }
        if let value = byTier?.independentlyChecked, value > 0 { parts.append("\(value)/\(total) independently-checked") }
        if let value = byTier?.selfChecked, value > 0 { parts.append("\(value)/\(total) self-checked") }
        if let value = byTier?.unchecked, value > 0 { parts.append("\(value) unchecked") }
        return parts.isEmpty
            ? "\(presentation.value) \(presentation.qualifier)"
            : parts.joined(separator: " · ")
    }

    /// A compact dashboard form. "Supported" names claim coverage without
    /// conflating it with recorded check runs, while fitting the small column.
    var compactHeadline: String {
        let presentation = ReceiptCoveragePresentation(evidence: self)
        if presentation.isInconsistent { return presentation.rowText }
        if gradeable != false,
           let checked = checkedTotal,
           let total = checkableTotal,
           total > 0 {
            return "\(checked)/\(total) supported"
        }
        return presentation.rowText
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

/// One honest rendering contract for claim coverage across summaries, Work,
/// and Receipt detail. Only an explicit `gradeable: false` becomes "Not
/// gradeable"; missing counts stay missing and supplied partial counts remain
/// visible.
struct ReceiptCoveragePresentation {
    let value: String
    let qualifier: String
    let rowText: String
    let isInconsistent: Bool
    let tierBreakdownAvailable: Bool
    let tierBreakdownNotice: String?

    init(evidence: ReceiptEvidence) {
        let total = evidence.checkableTotal
        let checked = evidence.checkedTotal

        let tierCounts = evidence.byTier.map {
            [
                $0.externallyVerified ?? 0,
                $0.independentlyChecked ?? 0,
                $0.selfChecked ?? 0,
                $0.unchecked ?? 0,
            ]
        }
        let tierTotal = tierCounts?.reduce(0, +)
        let checkedByTier = evidence.byTier.map {
            ($0.externallyVerified ?? 0)
                + ($0.independentlyChecked ?? 0)
                + ($0.selfChecked ?? 0)
        }
        let negativeTierCounts = tierCounts?.contains(where: { $0 < 0 }) == true
        let tierTotalConflict = if let total, let tierTotal {
            total != tierTotal
        } else {
            false
        }
        let checkedTierConflict = if let checked, let checkedByTier {
            checked != checkedByTier
        } else {
            false
        }
        if evidence.byTier == nil {
            tierBreakdownAvailable = false
            tierBreakdownNotice = "Evidence-tier breakdown not reported."
        } else if negativeTierCounts {
            tierBreakdownAvailable = false
            tierBreakdownNotice = "Evidence-tier breakdown contains invalid negative counts."
        } else if tierTotalConflict, let total, let tierTotal {
            tierBreakdownAvailable = false
            tierBreakdownNotice = "Evidence-tier breakdown reports \(tierTotal) of \(total) checkable claims."
        } else if checkedTierConflict, let checked, let checkedByTier {
            tierBreakdownAvailable = false
            tierBreakdownNotice = "Evidence tiers report \(checkedByTier) supported claims; the summary reports \(checked)."
        } else {
            tierBreakdownAvailable = true
            tierBreakdownNotice = nil
        }

        let supportedExceedsCheckable = if let total, let checked {
            checked > total
        } else {
            false
        }
        let primaryCountsConflict = total.map { $0 < 0 } == true
            || checked.map { $0 < 0 } == true
            || supportedExceedsCheckable
            || (evidence.gradeable == false && ((total ?? 0) > 0 || (checked ?? 0) > 0))
            || (evidence.gradeable == true && total == 0)
        let tierCountsConflict = evidence.byTier != nil
            && (negativeTierCounts || tierTotalConflict || checkedTierConflict)
        isInconsistent = primaryCountsConflict || tierCountsConflict

        if primaryCountsConflict {
            value = "Inconsistent counts"
            switch (checked, total) {
            case let (.some(checked), .some(total)):
                qualifier = "\(checked) supported · \(total) checkable reported"
                rowText = "inconsistent coverage · \(checked) supported of \(total) reported"
            case let (.some(checked), .none):
                qualifier = "\(checked) supported · checkable total unavailable"
                rowText = "inconsistent coverage · \(checked) supported · total not reported"
            case let (.none, .some(total)):
                qualifier = "support count unavailable · \(total) checkable reported"
                rowText = "inconsistent coverage · support count missing · \(total) checkable"
            case (.none, .none):
                qualifier = "coverage fields conflict"
                rowText = "inconsistent coverage counts"
            }
        } else if tierCountsConflict {
            value = "Inconsistent counts"
            qualifier = "tier breakdown conflicts with reported coverage"
            rowText = "inconsistent coverage · tier breakdown conflicts"
        } else if evidence.gradeable == false {
            value = "Not gradeable"
            qualifier = "no checkable claims recorded"
            rowText = "not gradeable"
        } else if let total, total > 0, let checked {
            value = "\(checked) of \(total)"
            qualifier = evidence.gradeable == nil
                ? "claims supported · gradeability not reported"
                : "claims supported"
            rowText = "\(checked)/\(total) claims supported"
        } else if let total, total > 0 {
            value = "Not reported"
            qualifier = "support count unavailable · \(total) checkable claims"
                + (evidence.gradeable == nil ? " · gradeability not reported" : "")
            rowText = "support count not reported · \(total) checkable claims"
        } else if total == 0 {
            value = "No checkable claims"
            qualifier = "zero checkable claims reported"
                + (evidence.gradeable == nil ? " · gradeability not reported" : "")
            rowText = "no checkable claims"
        } else if let checked {
            value = "Total not reported"
            qualifier = "\(checked) supported reported · checkable total unavailable"
                + (evidence.gradeable == nil ? " · gradeability not reported" : "")
            rowText = "\(checked) supported · checkable total not reported"
        } else {
            value = "Not reported"
            qualifier = "claim-coverage counts unavailable"
                + (evidence.gradeable == nil ? " · gradeability not reported" : "")
            rowText = "claim coverage not reported"
        }
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

    /// The dedicated "Weekly plan" receipt row. Calibrated → the percentage
    /// (≈0% when calibrated-but-negligible, never a bare "—"); otherwise a
    /// named calibration state, never a fabricated number. Mirrors
    /// receipt.plan_share_headline so every surface reads identically.
    var rowSummary: String {
        if calibrationState == "calibrated", let pct {
            return (Fmt.planPct(pct) ?? "≈0%") + " of weekly plan"
        }
        switch calibrationState {
        case "calibrating": return "calibrating — not enough 7-day history yet"
        case "never": return "undefined for this client"
        default: return "—"
        }
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

enum ReceiptCheckGroup: String, CaseIterable {
    case attention
    case other
    case passed
    case history
}

/// Stable, readable semantics for one itemized check run. The daemon may emit
/// exact duplicate rows, so the collection adds an occurrence ordinal to the
/// content identity instead of attaching disclosure state to an array index.
struct ReceiptCheckRowPresentation: Identifiable {
    let id: String
    let accessibilityIdentifier: String
    let check: ReceiptCheck
    let title: String
    let resultLabel: String
    let sourceLabel: String?
    let group: ReceiptCheckGroup

    var scope: String? { Self.nonEmpty(check.scope) }

    var collapsedExitText: String? {
        guard let exitCode = check.exitCode, exitCode != 0 else { return nil }
        return "exit \(exitCode)"
    }

    var runDetailText: String {
        var parts = [resultLabel]
        if let exitCode = check.exitCode { parts.append("exit \(exitCode)") }
        if let sourceLabel { parts.append(sourceLabel) }
        return parts.joined(separator: " · ")
    }

    init(check: ReceiptCheck, occurrence: Int) {
        self.check = check
        title = Self.nonEmpty(check.name) ?? Self.nonEmpty(check.kind) ?? "Unnamed check"
        resultLabel = Self.resultLabel(check.result)
        sourceLabel = Self.sourceLabel(check.source)
        group = Self.group(check)

        let fingerprint = Self.fingerprint(check)
        id = "\(fingerprint)#\(occurrence)"
        accessibilityIdentifier = "receipt.check.\(Self.stableDigest(fingerprint)).\(occurrence)"
    }

    func accessibilityValue(isExpanded: Bool) -> String {
        var parts = [resultLabel]
        if let sourceLabel { parts.append("source \(sourceLabel)") }
        if let exitCode = check.exitCode { parts.append("exit \(exitCode)") }
        if let scope { parts.append("scope \(scope)") }
        if check.superseded == true { parts.append("superseded by a later passing run") }
        if let findingState = Self.nonEmpty(check.finding?.state), findingState != "open" {
            parts.append("marked \(findingState) by you")
        }
        parts.append(isExpanded ? "expanded" : "collapsed")
        return parts.joined(separator: ", ")
    }

    private static func resultLabel(_ result: String?) -> String {
        switch result {
        case "passed": return "Passed"
        case "failed": return "Failed"
        case "error": return "Error"
        case "skipped": return "Skipped"
        default: return "Unknown"
        }
    }

    private static func sourceLabel(_ source: String?) -> String? {
        guard let source = nonEmpty(source) else { return nil }
        switch source {
        case "ci": return "CI"
        case "hook": return "Hook"
        case "client_hook": return "Client hook"
        case "mcp": return "Connected tool"
        case "agent_report": return "Agent report"
        case "external": return "External"
        case "provider": return "Provider"
        default:
            return source.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    private static func group(_ check: ReceiptCheck) -> ReceiptCheckGroup {
        if check.superseded == true { return .history }
        if check.finding?.attentionOpen == false { return .history }
        if let state = nonEmpty(check.finding?.state), state != "open" { return .history }
        switch check.result {
        case "failed", "error": return .attention
        case "passed": return .passed
        default: return .other
        }
    }

    private static func fingerprint(_ check: ReceiptCheck) -> String {
        // A run's disclosure identity must survive later enrichment and human
        // disposition changes. Keep mutable detail (summary, files, artifact,
        // supersession, finding state) out of this key.
        let fields = [
            check.kind, check.name, check.result, check.exitCode.map { String($0) },
            check.scope, check.source, check.at.map { String($0) },
        ]
        return fields.map { value in
            let value = value ?? ""
            return "\(value.utf8.count):\(value)"
        }.joined(separator: "|")
    }

    private static func stableDigest(_ value: String) -> String {
        var hash: UInt64 = 14_695_981_039_346_656_037
        for byte in value.utf8 {
            hash = (hash ^ UInt64(byte)) &* 1_099_511_628_211
        }
        return String(hash, radix: 16)
    }

    private static func nonEmpty(_ value: String?) -> String? {
        guard let value, !value.isEmpty else { return nil }
        return value
    }
}

struct ReceiptCheckCollectionPresentation {
    static let routineExpansionLimit = 10

    let rows: [ReceiptCheckRowPresentation]
    let sharedScope: String?
    let aggregateNotice: String?
    let itemizedNotice: String?

    init(evidence: ReceiptEvidenceDim) {
        var occurrences: [String: Int] = [:]
        rows = (evidence.checks ?? []).map { check in
            let probe = ReceiptCheckRowPresentation(check: check, occurrence: 0)
            let occurrence = occurrences[probe.id, default: 0]
            occurrences[probe.id] = occurrence + 1
            return ReceiptCheckRowPresentation(check: check, occurrence: occurrence)
        }

        let scopes = rows.compactMap(\.scope)
        sharedScope = rows.count > 1 && scopes.count == rows.count && Set(scopes).count == 1
            ? scopes[0]
            : nil

        aggregateNotice = Self.aggregateNotice(evidence)
        if let total = evidence.checksTotal, total > 0, !rows.isEmpty, total != rows.count {
            let noun = rows.count == 1 ? "entry is" : "entries are"
            itemizedNotice = "\(rows.count) itemized \(noun) available for \(total) reported check runs."
        } else {
            itemizedNotice = nil
        }

    }

    var initiallyExpandsRoutineGroups: Bool {
        rows.count <= Self.routineExpansionLimit
    }

    func routineGroupExpanded(
        userOverride: Bool?,
        forcedDefault: Bool? = nil
    ) -> Bool {
        userOverride ?? forcedDefault ?? initiallyExpandsRoutineGroups
    }

    func rows(in group: ReceiptCheckGroup) -> [ReceiptCheckRowPresentation] {
        rows.filter { $0.group == group }
    }

    private static func aggregateNotice(_ evidence: ReceiptEvidenceDim) -> String? {
        let supplied = [evidence.checksTotal, evidence.checksPassed, evidence.checksFailed]
            .compactMap { $0 }
        if supplied.contains(where: { $0 < 0 }) {
            return "Reported check tallies contain a negative value."
        }
        guard let total = evidence.checksTotal, total >= 0 else { return nil }
        let passed = evidence.checksPassed ?? 0
        let failed = evidence.checksFailed ?? 0
        if passed > total || failed > total || passed + failed > total {
            return "Reported passed and failed tallies conflict with the total."
        }
        return nil
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

/// Recorded check runs are independent from claim coverage. This presentation
/// preserves any passed/failed tally even when the total is absent or
/// inconsistent, instead of silently converting missing values to zero.
struct ReceiptCheckRunsPresentation {
    let value: String
    let qualifier: String
    let rowText: String
    let headerText: String
    let isInconsistent: Bool

    init(total: Int?, passed: Int?, failed: Int?) {
        let genericCountsConflict = Self.genericCountsConflict(
            total: total,
            passed: passed,
            failed: failed
        )
        let zeroTotalConflict = total == 0 && ((passed ?? 0) != 0 || (failed ?? 0) != 0)
        isInconsistent = genericCountsConflict || zeroTotalConflict
        if genericCountsConflict {
            value = "Inconsistent counts"
            let tallies = Self.tallies(passed: passed, failed: failed)
            let totalText = total.map { "\($0) total reported" } ?? "total not reported"
            qualifier = "\(tallies) · \(totalText)"
            rowText = "inconsistent check runs · \(tallies) · "
                + (total.map { "\($0) total" } ?? "total not reported")
            headerText = "inconsistent · \(tallies) · "
                + (total.map { "\($0) total" } ?? "total not reported")
        } else if let total, total > 0 {
            if let passed {
                value = "\(passed) of \(total)"
                qualifier = "check runs passed" + Self.failedSuffix(failed)
                rowText = "\(passed)/\(total) check runs passed" + Self.failedSuffix(failed)
                headerText = "\(passed)/\(total) passed" + Self.failedSuffix(failed)
            } else {
                value = "Not reported"
                qualifier = "passes unavailable · \(total) runs" + Self.failedSuffix(failed)
                rowText = "passes not reported · \(total) check runs" + Self.failedSuffix(failed)
                headerText = "passes not reported · \(total) total" + Self.failedSuffix(failed)
            }
        } else if total == 0, (passed ?? 0) == 0, (failed ?? 0) == 0 {
            value = "None"
            qualifier = "no check runs recorded"
            rowText = "no check runs"
            headerText = "no check runs"
        } else if total == 0 {
            value = "0 total reported"
            let tallies = Self.tallies(passed: passed, failed: failed)
            qualifier = tallies + " · tallies conflict with total"
            rowText = "0 total reported · \(tallies)"
            headerText = rowText
        } else {
            value = "Total not reported"
            let tallies = Self.tallies(passed: passed, failed: failed)
            qualifier = tallies == "no tallies reported"
                ? "check-run totals unavailable"
                : tallies
            rowText = tallies == "no tallies reported"
                ? "check runs not reported"
                : "total not reported · \(tallies)"
            headerText = rowText
        }
    }

    private static func failedSuffix(_ failed: Int?) -> String {
        guard let failed, failed > 0 else { return "" }
        return " · \(failed) failed"
    }

    private static func tallies(passed: Int?, failed: Int?) -> String {
        var parts: [String] = []
        if let passed { parts.append("\(passed) passed") }
        if let failed { parts.append("\(failed) failed") }
        return parts.isEmpty ? "no tallies reported" : parts.joined(separator: " · ")
    }

    private static func genericCountsConflict(
        total: Int?,
        passed: Int?,
        failed: Int?
    ) -> Bool {
        if total.map({ $0 < 0 }) == true
            || passed.map({ $0 < 0 }) == true
            || failed.map({ $0 < 0 }) == true {
            return true
        }
        guard let total else { return false }
        // The dedicated zero-total branch preserves supplied tallies and says
        // they conflict with the reported total in more concrete language.
        if total == 0 { return false }
        if passed.map({ $0 > total }) == true || failed.map({ $0 > total }) == true {
            return true
        }
        if let passed, let failed, passed + failed > total { return true }
        return false
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
