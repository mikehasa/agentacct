import Foundation

// Decodables for the /v1 native-shell lane beyond the glance:
// /v1/sessions (paginated roots list), /v1/session (one-session deep view),
// /v1/plan (attributed plan aggregates). Contract: additive-only schemas —
// every struct tolerates unknown keys, every field the daemon may omit is
// Optional, and honesty semantics ride the payload (calibrated-or-nothing
// plan numbers, None-never-$0 costs) rather than being re-derived here.

/// The neutral named absences a model falls back to ONLY when a reducer
/// display field is missing from the payload (an older daemon, a partial
/// fixture). The reducer owns every present-value string; these never stand in
/// for a value it sent, and no model ever renders a bare dash.
enum PayloadAbsence {
    static let cost = "cost not reported"
    static let costBasis = "cost basis not reported"
    static let noUsage = "no usage recorded"
    static let unpriced = "unpriced"
    static let coverage = "coverage not reported"
    static let checks = "checks not reported"
    static let planShare = "plan share not reported"
    static let tokens = "not reported"
    static let revision = "revision not captured"
    /// The cost grammar legend when the payload did not carry one.
    static let costLegend = "cost legend not reported"
    /// A check or fact whose source label the payload did not carry.
    static let source = "source not reported"
    static let reset = "reset time not reported"
    /// A Task whose last recorded activity carries no usable timestamp.
    static let activityTime = "Activity time unavailable"
    static let windowLabel = "limit window"
    static let notGradeable = "not gradeable"
    /// A check whose result words the payload did not carry.
    static let checkResult = "result not reported"
    /// A withheld command whose redaction sentence the payload did not carry.
    static let command = "command redaction not described"
    /// A withheld artifact whose redaction sentence the payload did not carry.
    static let artifact = "artifact redaction not described"
    /// A tool-call synopsis the payload did not carry.
    static let toolCalls = "tool calls not reported"
    /// A limit window whose value phrase the payload did not carry.
    static let limitValue = "used percent not reported"
    /// A cost figure whose total/partial label the payload did not carry.
    static let costLabel = "cost label not reported"
    /// A chart measure label the payload did not carry.
    static let measure = "measure not reported"

    /// A trimmed non-empty payload string, or nil.
    static func text(_ value: String?) -> String? {
        guard let value else { return nil }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : value
    }
}

/// A reducer-built `{value, absent, qualifier}` tile (`coverage_tile`,
/// `checks_tile`): `value` is a measured figure only; a named state such as
/// `not gradeable` arrives as `absent`, never in the metric face.
struct ReceiptTileText: Decodable, Equatable {
    let value: String?
    var absent: String? = nil
    let qualifier: String?

    init(value: String?, absent: String? = nil, qualifier: String?) {
        self.value = value
        self.absent = absent
        self.qualifier = qualifier
    }
}

/// One row of the reducer's evidence-tier table: the tier key, its label and
/// the sentence naming who ran the check and where.
struct ReceiptTierDefinition: Decodable, Equatable, Identifiable {
    let key: String
    let label: String?
    let definition: String?

    var id: String { key }
}

/// One header per receipt field, shared by CLI, Markdown and the app. Missing
/// labels fall back to the same words the reducer's table carries.
struct ReceiptFieldLabels: Decodable, Equatable {
    var decision: String? = nil
    var coverage: String? = nil
    var checks: String? = nil
    var cost: String? = nil
    var agents: String? = nil
    var task: String? = nil
    var actions: String? = nil
    var weeklyPlan: String? = nil
    var client: String? = nil
    var updated: String? = nil
    var attention: String? = nil
    /// The four names that head a record-page SECTION rather than a receipt
    /// dimension (`RECORD_SECTION_LABEL_KEYS`). Before these existed the record
    /// page spelled its own headings, so a wording change in Python left the
    /// app disagreeing with the CLI.
    var goal: String? = nil
    var outcomeSection: String? = nil
    var evidenceSection: String? = nil
    var nextSection: String? = nil

    enum CodingKeys: String, CodingKey {
        case decision, coverage, checks, cost, agents, task, actions
        case client, updated, attention, goal
        case weeklyPlan = "weekly_plan"
        case outcomeSection = "outcome_section"
        case evidenceSection = "evidence_section"
        case nextSection = "next_section"
    }

    var decisionLabel: String { PayloadAbsence.text(decision) ?? "Decision" }
    var coverageLabel: String { PayloadAbsence.text(coverage) ?? "Coverage" }
    var checksLabel: String { PayloadAbsence.text(checks) ?? "Checks" }
    var costLabel: String { PayloadAbsence.text(cost) ?? "Cost" }
    var agentsLabel: String { PayloadAbsence.text(agents) ?? "Agents" }
    var taskLabel: String { PayloadAbsence.text(task) ?? "Task" }
    var actionsLabel: String { PayloadAbsence.text(actions) ?? "Tool calls" }
    var weeklyPlanLabel: String { PayloadAbsence.text(weeklyPlan) ?? "Weekly plan" }
    var clientLabel: String { PayloadAbsence.text(client) ?? "Client" }
    var updatedLabel: String { PayloadAbsence.text(updated) ?? "Updated" }
    var attentionLabel: String { PayloadAbsence.text(attention) ?? "Attention" }
    var goalLabel: String { PayloadAbsence.text(goal) ?? "Goal" }
    var outcomeSectionLabel: String { PayloadAbsence.text(outcomeSection) ?? "Outcome" }
    var evidenceSectionLabel: String { PayloadAbsence.text(evidenceSection) ?? "Evidence" }
    var nextSectionLabel: String { PayloadAbsence.text(nextSection) ?? "Next" }
}

/// One provenance source as the reducer labels it: `{key, label, legend,
/// tier_key, tier_label}`.
struct ReceiptSourceEntry: Decodable, Equatable, Identifiable {
    let key: String
    let label: String?
    let legend: String?
    let tierKey: String?
    let tierLabel: String?

    var id: String { key }

    enum CodingKeys: String, CodingKey {
        case key, label, legend
        case tierKey = "tier_key"
        case tierLabel = "tier_label"
    }
}

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
        return "\(client) · \(clientSessionIdShort ?? RecentSession.shortId(clientSessionId))"
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
    /// Every step's check runs as the reducer's one tally.
    var checkTallyText: String? = nil

    enum CodingKeys: String, CodingKey {
        case schema, session, steps, descendants, plan
        case generatedAt = "generated_at"
        case checkTallyText = "check_tally_text"
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
    var latestEventId: String? = nil
    /// The reducer's words for ``evidenceGrade`` (the timeline's same label).
    var evidenceGradeLabel: String? = nil
    /// The step's check tally in the receipt's grammar
    /// (`3/4 passed · 1 could not run · 1 superseded`).
    var checkTallyText: String? = nil
    private let fallbackId = UUID().uuidString

    var id: String { workId ?? sectionId ?? fallbackId }

    /// The reducer's check tally for this step, or its named absence.
    var checkTallyDisplay: String {
        PayloadAbsence.text(checkTallyText)
            ?? ((checks ?? []).isEmpty ? "no checks recorded" : PayloadAbsence.checks)
    }

    enum CodingKeys: String, CodingKey {
        case title, kind, phase, summary, files, blocker, usage, models, checks
        case checkTallyText = "check_tally_text"
        case latestEventId = "latest_event_id"
        case evidenceGradeLabel = "evidence_grade_label"
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
        guard let cost = estimatedCostUsd else {
            // Named absence, never a dash: no linked rows vs rows none priced.
            guard let linked = linkedUsageRecords else { return PayloadAbsence.cost }
            return linked == 0 ? PayloadAbsence.noUsage : PayloadAbsence.unpriced
        }
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
    /// The agent's own short check name (projected by the ledger).
    var name: String? = nil
    /// Who recorded the check, as the payload's display label (`Agent-reported`,
    /// `Hook-captured`, `CI or provider`) — the one vocabulary every surface
    /// prints. Swift never maps `source_type` keys itself.
    var sourceLabel: String? = nil
    /// The reducer's result words (`Passed`, `Failed`, `Could not run`).
    var resultLabel: String? = nil
    /// The reducer's tone key (`pass` / `failure` / `not_run`).
    var resultTone: String? = nil
    /// A named result/exit-code disagreement (nil when they agree).
    var noteText: String? = nil
    /// The reducer's redaction sentences (nil when nothing was withheld).
    var commandStateText: String? = nil
    var artifactPathStateText: String? = nil
    var artifactUrlStateText: String? = nil
    private let fallbackId = UUID().uuidString

    var id: String { eventId ?? fallbackId }

    enum CodingKeys: String, CodingKey {
        case summary, files, name
        case sourceLabel = "source_label"
        case resultLabel = "result_label"
        case resultTone = "result_tone"
        case noteText = "note_text"
        case commandStateText = "command_state_text"
        case artifactPathStateText = "artifact_path_state_text"
        case artifactUrlStateText = "artifact_url_state_text"
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
    /// Plan-share words from the one vocabulary table: the chip, the row
    /// sentence, the plain conclusion, and the technical fit detail that
    /// stays behind a disclosure.
    var chipText: String? = nil
    var sentenceText: String? = nil
    var headline: String? = nil
    var basisText: String? = nil
    /// The by-model token measure (`tokens incl. cache-read`).
    var modelTokensLabel: String? = nil

    var id: String { client }

    enum CodingKeys: String, CodingKey {
        case client, confidence, calibratable, basis, scale, alpha, daily, headline
        case chipText = "chip_text"
        case sentenceText = "sentence_text"
        case basisText = "basis_text"
        case modelTokensLabel = "model_tokens_label"
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
    /// The status legend (decision words and filter groups) from the vocabulary.
    var decisionLegend: DecisionLegendPayload? = nil
    /// The review queue's words (noun, count, open action, sort rule).
    var queue: AttentionQueueCopy? = nil
    /// One header per receipt field, for list tables.
    var fieldLabels: ReceiptFieldLabels? = nil

    enum CodingKeys: String, CodingKey {
        case schema, tasks, total, truncated, attention, queue
        case decisionLegend = "decision_legend"
        case fieldLabels = "field_labels"
    }
}

/// The vocabulary's status legend: every decision word with its definition
/// and filter group, and each group's definition. The app keeps no copy.
struct DecisionLegendPayload: Decodable, Equatable {
    struct Decision: Decodable, Equatable, Identifiable {
        let key: String
        let label: String
        let definition: String
        let groupKey: String?
        var id: String { key }

        enum CodingKeys: String, CodingKey {
            case key, label, definition
            case groupKey = "group_key"
        }
    }

    struct Group: Decodable, Equatable, Identifiable {
        let key: String
        let label: String
        let definition: String
        var id: String { key }
    }

    let decisions: [Decision]
    let groups: [Group]
}

/// The review queue's words: one noun for the tab, counts, links and effects.
struct AttentionQueueCopy: Decodable, Equatable {
    let noun: String?
    let countText: String?
    let openAction: String?
    let sortText: String?

    enum CodingKeys: String, CodingKey {
        case noun
        case countText = "count_text"
        case openAction = "open_action"
        case sortText = "sort_text"
    }
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

    /// The queue's words for this count (absent on older daemons).
    var queue: AttentionQueueCopy? = nil

    private enum CodingKeys: String, CodingKey {
        case schema, items, total, counts, snapshot, offset, limit, truncated, queue
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
        queue = try container.decodeIfPresent(AttentionQueueCopy.self, forKey: .queue)
    }
}

struct V1AttentionCounts: Decodable, Equatable {
    let failedCheck: Int
    let failedStep: Int
    let blocker: Int
    /// Tasks whose lead item is a check that could not run (absent on older
    /// payloads, which never had that kind).
    var checkNotRun: Int = 0

    enum CodingKeys: String, CodingKey {
        case failedCheck = "failed_check"
        case failedStep = "failed_step"
        case blocker
        case checkNotRun = "check_not_run"
    }

    init(failedCheck: Int, failedStep: Int, blocker: Int, checkNotRun: Int = 0) {
        self.failedCheck = failedCheck
        self.failedStep = failedStep
        self.blocker = blocker
        self.checkNotRun = checkNotRun
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        failedCheck = try container.decode(Int.self, forKey: .failedCheck)
        failedStep = try container.decode(Int.self, forKey: .failedStep)
        blocker = try container.decode(Int.self, forKey: .blocker)
        checkNotRun = try container.decodeIfPresent(Int.self, forKey: .checkNotRun) ?? 0
    }
}

/// The server-selected leading reason for one attention Task. The summary and
/// next step are recorded evidence; a missing `next_step` deliberately remains
/// nil so the UI cannot turn a generic suggestion into an agent claim.
struct ReceiptAttention: Decodable {
    /// Internal sort key (`failed_check` / `blocker` / `failed_step`) — never a
    /// display word; ``reasonLabel`` is.
    let kind: String
    let summary: String
    let nextStep: String?
    let observedAt: Double?
    let source: String?
    // The ONE attention block's display and write fields (all additive).
    let reasonLabel: String?
    let checkName: String?
    let evidenceType: String?
    let result: String?
    let exitCode: Int?
    let sectionTitle: String?
    /// `Failed build check · <name> · exit 0` / `Blocker · <step>`.
    let label: String?
    let sourceLabel: String?
    let actionToken: String?
    let targetDigest: String?
    let revision: Int?
    let dispositionState: String?
    let dispositionNote: String?
    /// The reducer's attention-open predicate: the only "still needs you" signal.
    let open: Bool?
    let effects: ReceiptDispositionEffects?
    /// The reducer's tone key for a check item (`failure` / `not_run`); nil
    /// for a blocker or failed step.
    var resultTone: String? = nil
    /// A named result/exit-code disagreement on the lead check.
    var noteText: String? = nil
    /// `1 more open finding` / `2 more open items`: every other open item
    /// behind this lead one, as the reducer's count sentence.
    var moreText: String? = nil

    enum CodingKeys: String, CodingKey {
        case kind, summary, source, result, label, revision, open, effects
        case resultTone = "result_tone"
        case noteText = "note_text"
        case moreText = "more_text"
        case nextStep = "next_step"
        case observedAt = "observed_at"
        case reasonLabel = "reason_label"
        case checkName = "check_name"
        case evidenceType = "evidence_type"
        case exitCode = "exit_code"
        case sectionTitle = "section_title"
        case sourceLabel = "source_label"
        case actionToken = "action_token"
        case targetDigest = "target_digest"
        case dispositionState = "disposition_state"
        case dispositionNote = "disposition_note"
    }
}

/// The effect sentence of each disposition action, stated before the user
/// takes it (nil for an action the attention kind does not offer).
struct ReceiptDispositionEffects: Decodable, Equatable {
    let reviewed: String?
    let resolved: String?
    /// What Reopen does (absent on older payloads).
    var reopen: String? = nil
}

struct ReceiptSummary: Decodable, Identifiable {
    let taskId: String
    let title: String?
    // The compact verdict (headline + gap; no health window on a list row).
    // Optional so older `/v1/tasks` payloads still decode.
    let verdict: ReceiptVerdict?
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
    /// The reducer's attention-open predicate for this row (optional on older
    /// payloads).
    let attentionOpen: Bool?
    /// The reducer's attention order class for an open item (nil otherwise).
    var attentionOrder: Int? = nil
    /// The vocabulary's filter group for this row.
    var groupKey: String? = nil
    /// The handoff marker's words, present only when they add to the decision.
    var lifecycleMarkerText: String? = nil

    var id: String { taskId }

    enum CodingKeys: String, CodingKey {
        case taskId = "task_id"
        case title, verdict, project, attention
        case attentionOpen = "attention_open"
        case attentionOrder = "attention_order"
        case groupKey = "group_key"
        case lifecycleMarkerText = "lifecycle_marker_text"
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
    /// The sentence-case decision label from the reducer's one label table.
    let label: String?
    let statement: String?
    let assertedBy: String?
    let assertedByLabel: String?
    let findingAttentionState: String?
    // The newest blocker's own words (blocked/failed only; nil elsewhere and on
    // older daemon payloads). Daemon-computed — the app never re-derives it.
    let blocker: ReceiptBlocker?

    enum CodingKeys: String, CodingKey {
        case key, label, statement, blocker
        case assertedBy = "asserted_by"
        case assertedByLabel = "asserted_by_label"
        case findingAttentionState = "finding_attention_state"
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

    /// How many tiers the record actually reached. The record page hoists its
    /// tier pip to the Evidence heading only at exactly ONE: pip SHAPE carries
    /// the tier, so a single pip must never stand for two.
    var nonEmptyTierCount: Int {
        [externallyVerified, independentlyChecked, selfChecked, unchecked]
            .filter { ($0 ?? 0) > 0 }.count
    }

    enum CodingKeys: String, CodingKey {
        case externallyVerified = "externally_verified"
        case independentlyChecked = "independently_checked"
        case selfChecked = "self_checked"
        case unchecked
    }
}

/// Evidence COVERAGE (M2): per-tier ratios over the checkable steps — the counts
/// ARE the headline, never a single collapsed grade word. ``key`` is a coarse
/// tier ordinal used only for colour. Every display string (hero, row, tiles,
/// tally, tier legend) is built by the daemon's reducer and rendered as-is, so
/// no surface words the same evidence differently.
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
    // Split ledger buckets: still open (started/checkpoint) vs named terminal
    // stops, and each bucket's subagent share.
    let stillOpen: Int?
    let stoppedBlocked: Int?
    let stoppedHandedOff: Int?
    let stoppedFailed: Int?
    let subagentsByBucket: [String: Int]?
    let checksSuperseded: Int?
    let checksEarlierFailed: Int?
    // Reducer display strings.
    let coverageHero: String?
    let coverageRow: String?
    let coverageTile: ReceiptTileText?
    let checksTile: ReceiptTileText?
    let checkTallyText: String?
    /// `failed` / `passed` / `not_reported` / `none` — the one key a tint maps.
    let checkRunsState: String?
    let tierLegend: [ReceiptTierDefinition]?
    /// `2 checks could not run` (nil when every check ran).
    var checksNotRunText: String? = nil
    /// What the ratio does not cover (`1 step handed off · 2 not
    /// check-relevant`), owing no proof claim; nil when nothing is outside it.
    var coverageLedger: String? = nil
    /// The scope term's one definition (`Not check-relevant: review, …`).
    var scopeDefinition: String? = nil

    enum CodingKeys: String, CodingKey {
        case key, gradeable, definition
        case checksNotRunText = "checks_not_run_text"
        case coverageLedger = "coverage_ledger"
        case scopeDefinition = "scope_definition"
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
        case stillOpen = "still_open"
        case stoppedBlocked = "stopped_blocked"
        case stoppedHandedOff = "stopped_handed_off"
        case stoppedFailed = "stopped_failed"
        case subagentsByBucket = "subagents_by_bucket"
        case checksSuperseded = "checks_superseded"
        case checksEarlierFailed = "checks_earlier_failed"
        case coverageHero = "coverage_hero"
        case coverageRow = "coverage_row"
        case coverageTile = "coverage_tile"
        case checksTile = "checks_tile"
        case checkTallyText = "check_tally_text"
        case checkRunsState = "check_runs_state"
        case tierLegend = "tier_legend"
    }

    /// The reducer's tier label for a tier key, from the payload's tier table.
    func tierLabel(for tierKey: String?) -> String? {
        guard let tierKey else { return nil }
        return PayloadAbsence.text(tierLegend?.first(where: { $0.key == tierKey })?.label)
    }

    /// The strongest tier's label (nil when the task has no proven step or the
    /// payload carries no tier table).
    var strongestTierLabel: String? { tierLabel(for: strongestTier) }

    /// The coverage headline (`coverage_hero`), tier by tier. A malformed count
    /// set names its inconsistency; a payload without the hero names its absence.
    var headline: String {
        let presentation = ReceiptCoveragePresentation(evidence: self)
        if presentation.isInconsistent {
            return "\(presentation.value) (\(presentation.qualifier))"
        }
        if let hero = PayloadAbsence.text(coverageHero) { return hero }
        return presentation.rowText
    }

    /// The compact one-line coverage form (`coverage_row`).
    var compactHeadline: String {
        ReceiptCoveragePresentation(evidence: self).rowText
    }
}

/// One honest rendering contract for step coverage across summaries, Work,
/// and Receipt detail. The reducer's `coverage_tile` / `coverage_row` are the
/// values; a malformed count set still names its inconsistency, and a payload
/// without the strings names what is missing rather than inventing a ratio.
struct ReceiptCoveragePresentation {
    let value: String
    let qualifier: String
    let rowText: String
    /// Whether this coverage reading is a MEASURED figure (a ratio the reader
    /// tracks) or a named state — a named absence (`not gradeable`) or a named
    /// conflict. The single source of the face rule (K10): every surface asks
    /// `FieldFont.value(_:isMetric:)` with this, instead of re-deriving its own
    /// `if gradeable` and disagreeing with the next surface.
    let valueIsMetric: Bool
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
            tierBreakdownNotice = "Evidence-tier breakdown reports \(tierTotal) of \(total) checkable steps."
        } else if checkedTierConflict, let checked, let checkedByTier {
            tierBreakdownAvailable = false
            tierBreakdownNotice = "Evidence tiers report \(checkedByTier) checked steps; the summary reports \(checked)."
        } else {
            tierBreakdownAvailable = true
            tierBreakdownNotice = nil
        }

        let checkedExceedsCheckable = if let total, let checked {
            checked > total
        } else {
            false
        }
        let primaryCountsConflict = total.map { $0 < 0 } == true
            || checked.map { $0 < 0 } == true
            || checkedExceedsCheckable
            || (evidence.gradeable == false && ((total ?? 0) > 0 || (checked ?? 0) > 0))
            || (evidence.gradeable == true && total == 0)
        let tierCountsConflict = evidence.byTier != nil
            && (negativeTierCounts || tierTotalConflict || checkedTierConflict)
        isInconsistent = primaryCountsConflict || tierCountsConflict

        let gradeabilityNote = evidence.gradeable == nil ? " · gradeability not reported" : ""
        if primaryCountsConflict {
            // A named conflict, not a figure.
            valueIsMetric = false
            value = "Inconsistent counts"
            switch (checked, total) {
            case let (.some(checked), .some(total)):
                qualifier = "\(checked) checked · \(total) checkable reported"
                rowText = "inconsistent coverage · \(checked) checked of \(total) reported"
            case let (.some(checked), .none):
                qualifier = "\(checked) checked · checkable total unavailable"
                rowText = "inconsistent coverage · \(checked) checked · total not reported"
            case let (.none, .some(total)):
                qualifier = "checked count unavailable · \(total) checkable reported"
                rowText = "inconsistent coverage · checked count missing · \(total) checkable"
            case (.none, .none):
                qualifier = "coverage fields conflict"
                rowText = "inconsistent coverage counts"
            }
        } else if tierCountsConflict {
            valueIsMetric = false
            value = "Inconsistent counts"
            qualifier = "tier breakdown conflicts with reported coverage"
            rowText = "inconsistent coverage · tier breakdown conflicts"
        } else if let tileValue = PayloadAbsence.text(evidence.coverageTile?.value) {
            // The reducer's own tile and row — rendered verbatim. The tile
            // carries a value (never an absence) in this branch, so it is the
            // measured figure.
            valueIsMetric = true
            value = tileValue
            qualifier = PayloadAbsence.text(evidence.coverageTile?.qualifier) ?? ""
            rowText = PayloadAbsence.text(evidence.coverageRow)
                ?? [tileValue, qualifier].filter { !$0.isEmpty }.joined(separator: " ")
        } else if let absent = PayloadAbsence.text(evidence.coverageTile?.absent) {
            // The reducer's named absence and its reason — rendered verbatim.
            valueIsMetric = false
            value = absent
            qualifier = PayloadAbsence.text(evidence.coverageTile?.qualifier) ?? ""
            rowText = PayloadAbsence.text(evidence.coverageRow) ?? absent
        } else if evidence.gradeable == false || total == 0 {
            valueIsMetric = false
            value = PayloadAbsence.notGradeable
            qualifier = String(gradeabilityNote.dropFirst(3))
            rowText = PayloadAbsence.text(evidence.coverageRow) ?? PayloadAbsence.notGradeable
        } else if let row = PayloadAbsence.text(evidence.coverageRow) {
            valueIsMetric = true
            value = row
            qualifier = gradeabilityNote.isEmpty ? "" : String(gradeabilityNote.dropFirst(3))
            rowText = row
        } else if let total, total > 0, let checked {
            // Counts without the reducer strings: the reducer's ratio grammar.
            valueIsMetric = true
            value = "\(checked)/\(total)"
            qualifier = Self.tierWord(evidence) + gradeabilityNote
            rowText = "\(checked)/\(total) \(Self.tierWord(evidence))"
        } else if let total, total > 0 {
            valueIsMetric = false
            value = "Not reported"
            qualifier = "checked count unavailable · \(total) checkable steps" + gradeabilityNote
            rowText = "checked count not reported · \(total) checkable steps"
        } else if let checked {
            valueIsMetric = false
            value = "Total not reported"
            qualifier = "\(checked) checked reported · checkable total unavailable" + gradeabilityNote
            rowText = "\(checked) checked · checkable total not reported"
        } else {
            valueIsMetric = false
            value = "Not reported"
            qualifier = "coverage counts unavailable" + gradeabilityNote
            rowText = PayloadAbsence.coverage
        }
    }

    /// The one tier word a compact coverage form can carry: the tier's label
    /// when every checked step sits at one tier, else plain `checked` (the
    /// reducer's `_coverage_tier_word`, labels from the payload tier table).
    private static func tierWord(_ evidence: ReceiptEvidence) -> String {
        guard let tiers = evidence.byTier else { return "checked" }
        let present = [
            ("externally_verified", tiers.externallyVerified ?? 0),
            ("independently_checked", tiers.independentlyChecked ?? 0),
            ("self_checked", tiers.selfChecked ?? 0),
        ].filter { $0.1 > 0 }
        guard present.count == 1, let label = evidence.tierLabel(for: present[0].0) else {
            return "checked"
        }
        return label
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
    /// The reducer's one plan-share headline (`plan_share.headline`, or the
    /// cost dimension's `plan_share_headline` injected by ``ReceiptCostDim``).
    var headline: String? = nil

    enum CodingKeys: String, CodingKey {
        case pct, client, headline
        case calibrationState = "calibration_state"
        case coveredSessions = "covered_sessions"
        case sessionCount = "session_count"
    }

    /// "≈X.X% of weekly plan" — nil when not calibrated (absence stays named
    /// by the calibration state, never rendered as a number).
    var text: String? {
        guard calibrationState == nil || calibrationState == "calibrated",
              Fmt.planPct(pct) != nil else { return nil }
        return PayloadAbsence.text(headline)
    }

    /// The dedicated "Weekly plan" receipt row: the reducer's headline, or
    /// its named absence. Swift keeps no copy of the plan-share table.
    var rowSummary: String {
        PayloadAbsence.text(headline) ?? PayloadAbsence.planShare
    }
}

/// A task-list row's cost: the raw figure plus the reducer's display strings.
struct ReceiptCost: Decodable {
    let estimatedCostUsd: Double?
    let costBasis: String?
    let costConfidence: String?
    let costComplete: Bool?
    let planShare: ReceiptPlanShare?
    /// `no_usage` / `unpriced` / `partial` / `complete`.
    let state: String?
    /// `$1,554.67` / `≈$10.77` / `~$1,635.57`, or the named absence
    /// (`no usage recorded`, `unpriced`).
    let displayText: String?
    let basisLabel: String?
    let legend: String?
    let gapText: String?

    enum CodingKeys: String, CodingKey {
        case estimatedCostUsd = "estimated_cost_usd"
        case costBasis = "cost_basis"
        case costConfidence = "cost_confidence"
        case costComplete = "cost_complete"
        case planShare = "plan_share"
        case state, legend
        case displayText = "display_text"
        case basisLabel = "basis_label"
        case gapText = "gap_text"
    }

    /// True when the reducer names an absence rather than a priced figure.
    var isAbsent: Bool {
        state == "no_usage" || state == "unpriced" || PayloadAbsence.text(displayText) == nil
    }

    /// The cost line: the reducer's figure with its basis, or its named
    /// absence. A payload without the display string names that absence.
    var text: String {
        guard let display = PayloadAbsence.text(displayText) else { return PayloadAbsence.cost }
        if state == "no_usage" || state == "unpriced" { return display }
        return "\(display) · \(PayloadAbsence.text(basisLabel) ?? PayloadAbsence.costBasis)"
    }
}

/// The one honest leading line the daemon computes once for every surface:
/// what an agent claimed joined with how well it is proven, the typed gap, and
/// a time-bounded proof claim. All optional so an older daemon payload without
/// a verdict still decodes.
struct ReceiptHealthWindow: Decodable {
    let sinceAt: Double?
    let sinceIso: String?
    /// Local calendar day, e.g. `Sep 14`.
    let sinceDate: String?
    let proven: Int?
    let checkable: Int?
    let tierWord: String?
    /// `Since Sep 14: 1 of 1 completed step self-checked`.
    let text: String?

    enum CodingKeys: String, CodingKey {
        case sinceAt = "since_at"
        case sinceIso = "since_iso"
        case sinceDate = "since_date"
        case tierWord = "tier_word"
        case proven, checkable, text
    }
}

struct ReceiptVerdict: Decodable {
    let headline: String?
    let decisionKey: String?
    let decisionLabel: String?
    let evidenceKey: String?
    let assertedBy: String?
    let assertedByLabel: String?
    let gapEvidence: [String]?
    let gapCost: [String]?
    /// `Not yet proven` only for an unproven part; nil otherwise.
    let gapLabel: String?
    /// The unproven part (`1 completed step unchecked`), nil when none.
    let gapText: String?
    let healthWindow: ReceiptHealthWindow?
    /// The proof clause without the decision prefix, for a surface that shows
    /// the decision badge beside it (`0/1 checked`).
    var proofClause: String? = nil
    /// What the ratio does not cover, owing no proof claim (`1 step handed
    /// off · 2 not check-relevant`); nil when nothing is outside it.
    var ledgerText: String? = nil

    enum CodingKeys: String, CodingKey {
        // The payload's legacy joined `gap` is not decoded: the app renders
        // `gap_label` + `gap_text` and leaves cost absence to the Cost row.
        case headline
        case decisionKey = "decision_key"
        case decisionLabel = "decision_label"
        case evidenceKey = "evidence_key"
        case assertedBy = "asserted_by"
        case assertedByLabel = "asserted_by_label"
        case gapEvidence = "gap_evidence"
        case gapCost = "gap_cost"
        case gapLabel = "gap_label"
        case gapText = "gap_text"
        case healthWindow = "health_window"
        case proofClause = "proof_clause"
        case ledgerText = "ledger_text"
    }

    /// The proof clause beside a decision badge: the reducer's clause, or —
    /// from an older payload — the full headline.
    var badgeClause: String? {
        PayloadAbsence.text(proofClause) ?? PayloadAbsence.text(headline)
    }

    /// The gap line as the reducer shapes it: `<gap_label> — <gap_text>` for an
    /// evidence gap, the evidence text alone otherwise (nil when none).
    var gapLine: String? {
        guard let text = PayloadAbsence.text(gapText) else { return nil }
        if let label = PayloadAbsence.text(gapLabel) { return "\(label) — \(text)" }
        return text
    }

    /// Whether the gap is the reducer's typed unproven part: `gap_label` is
    /// sent ONLY for an evidence gap (K05), so the key — never the copy —
    /// decides whether a tier pip may mark it.
    var gapIsUnproven: Bool {
        PayloadAbsence.text(gapText) != nil && PayloadAbsence.text(gapLabel) != nil
    }
}

struct Receipt: Decodable {
    let schemaVersion: String
    let taskId: String
    let title: String?
    // The one honest leading line (optional; older payloads omit it).
    let verdict: ReceiptVerdict?
    let axes: ReceiptAxes
    let dimensions: ReceiptDimensions
    let sessions: [ReceiptSessionGroup]?
    let timeline: TaskTimelinePage?
    /// Task wall-clock span as the daemon computed it (nil when the store
    /// cannot bound it — the record page names that absence).
    let durationSeconds: Double?
    /// The ONE attention block for this Task (nil when nothing needs you).
    let attention: ReceiptAttention?
    let attentionOpen: Bool?
    /// One header per receipt field (Decision / Coverage / Checks / Cost / Agents).
    let fieldLabels: ReceiptFieldLabels?
    /// The vocabulary's filter group for this Task.
    var groupKey: String? = nil
    /// The handoff marker's words, present only when they add to the decision.
    var lifecycleMarkerText: String? = nil

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case taskId = "task_id"
        case durationSeconds = "duration_seconds"
        case title, verdict, axes, dimensions, sessions, timeline, attention
        case attentionOpen = "attention_open"
        case fieldLabels = "field_labels"
        case groupKey = "group_key"
        case lifecycleMarkerText = "lifecycle_marker_text"
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
    /// The daemon-rendered one-line marker (`Handed off · <statement>`).
    let markerLine: String?
    let assertedBy: String?

    enum CodingKeys: String, CodingKey {
        case handedOff = "handed_off"
        case statement
        case markerLine = "marker_line"
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
    let projectIdentityState: String?
    let rootCount: Int?
    let isContinuation: Bool?
    /// The project-binding gap sentence (nil when the project is declared and
    /// consistent), e.g. `Sessions in this Task report different projects.`
    let gapText: String?

    enum CodingKeys: String, CodingKey {
        case project
        case identityScope = "identity_scope"
        case sessionCount = "session_count"
        case projectIdentityState = "project_identity_state"
        case rootCount = "root_count"
        case isContinuation = "is_continuation"
        case gapText = "gap_text"
    }
}

struct ReceiptTaskDim: Decodable {
    let objectives: [String]?
    let boundary: ReceiptBoundary?
    let provenance: [String]?
    let gaps: [String]?
    /// The task-level GOAL, recorded once by the agent (`task_goal`). It cannot
    /// be derived from `objectives`, which are the recorded SECTION TITLES —
    /// steps, and usually the Task title over again.
    var goal: String? = nil
    /// The reducer's named absence when no goal was recorded. One of the two
    /// absences exempt from the record page's absence budget.
    var goalAbsentText: String? = nil

    enum CodingKeys: String, CodingKey {
        case objectives, boundary, provenance, gaps, goal
        case goalAbsentText = "goal_absent_text"
    }
}

struct ReceiptActorsDim: Decodable {
    let primaryAgent: String?
    let models: [String]?
    let subagentSessionCount: Int?
    let childSessionCount: Int?
    let provenance: [String]?
    let gaps: [String]?

    /// The subagent count as a phrase. ONE composition, used by both the place
    /// the count is written and the record page's meta line, so deleting the
    /// `Agents` row does not take the subagent count off the resting page.
    var subagentsText: String? {
        guard let count = subagentSessionCount, count > 0 else { return nil }
        return "\(count) subagent\(count == 1 ? "" : "s")"
    }

    enum CodingKeys: String, CodingKey {
        case primaryAgent = "primary_agent"
        case models
        case subagentSessionCount = "subagent_session_count"
        case childSessionCount = "child_session_count"
        case provenance, gaps
    }
}

/// One arithmetic shortfall the reducer found between what the ledger HOLDS
/// and what the capture SAW (`captured < recorded`). The labels are the
/// reducer's; the app never words the comparison itself.
struct ReceiptActionsShortfall: Decodable, Equatable, Identifiable {
    let callLabel: String?
    let recordLabel: String?
    let captured: Int?
    let recorded: Int?

    var id: String { callLabel ?? recordLabel ?? "shortfall" }

    enum CodingKeys: String, CodingKey {
        case captured, recorded
        case callLabel = "call_label"
        case recordLabel = "record_label"
    }
}

/// What the hook capture actually covered (`dimensions.actions.capture_coverage`):
/// the captured window, the task's own activity window, and the record
/// shortfalls that downgrade an "exact" claim to partial coverage.
struct ReceiptActionsCoverage: Decodable, Equatable {
    let capturedFirstAt: Double?
    let capturedLastAt: Double?
    let activityFirstAt: Double?
    let activityLastAt: Double?
    var recordShortfalls: [ReceiptActionsShortfall] = []

    enum CodingKeys: String, CodingKey {
        case capturedFirstAt = "captured_first_at"
        case capturedLastAt = "captured_last_at"
        case activityFirstAt = "activity_first_at"
        case activityLastAt = "activity_last_at"
        case recordShortfalls = "record_shortfalls"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        capturedFirstAt = try container.decodeIfPresent(Double.self, forKey: .capturedFirstAt)
        capturedLastAt = try container.decodeIfPresent(Double.self, forKey: .capturedLastAt)
        activityFirstAt = try container.decodeIfPresent(Double.self, forKey: .activityFirstAt)
        activityLastAt = try container.decodeIfPresent(Double.self, forKey: .activityLastAt)
        recordShortfalls = try container.decodeIfPresent([ReceiptActionsShortfall].self, forKey: .recordShortfalls) ?? []
    }
}

struct ReceiptActionsDim: Decodable {
    let toolCategoryCounts: [String: Int]?
    let toolCategoryTotal: Int?
    let touchedFileCount: Int?
    let provenance: [String]?
    let gaps: [String]?
    /// The reducer's tool-call tile: a count, or its named absence.
    let actionsTile: ReceiptTileText?
    /// The reducer's full synopsis (headline, integrity, labelled metrics).
    let actionsSynopsis: ReceiptActionSynopsis?
    /// `3 related paths` / `no related paths recorded`.
    let relatedPathsText: String?
    let relatedPathsDefinition: String?
    /// The capture sources behind the counts, in the reducer's words.
    let actionSourcesText: String?
    // The captured ledger the CLI already prints. Decoded here so the app can
    // show the same evidence instead of only its totals (additive; memberwise
    // defaults keep older call sites compiling).
    /// Captured tool calls by TOOL NAME (`Bash: 25`), distinct from the
    /// same-unit category partition `toolCategoryCounts` draws.
    var toolNameCounts: [String: Int]? = nil
    var toolNameTotal: Int? = nil
    var toolNamesElided: Int? = nil
    /// Captured command text, verbatim, and how many the reducer withheld.
    var commands: [String]? = nil
    var commandsPreview: [String]? = nil
    var commandCount: Int? = nil
    var commandsElided: Int? = nil
    /// Paths the capture associated with this task, and how many it withheld.
    var touchedFiles: [String]? = nil
    var touchedFilesPreview: [String]? = nil
    var touchedFilesElided: Int? = nil
    /// The reducer's capture-coverage window and record shortfalls.
    var captureCoverage: ReceiptActionsCoverage? = nil

    enum CodingKeys: String, CodingKey {
        case toolCategoryCounts = "tool_category_counts"
        case toolCategoryTotal = "tool_category_total"
        case touchedFileCount = "touched_file_count"
        case provenance, gaps, commands
        case actionsTile = "actions_tile"
        case actionsSynopsis = "actions_synopsis"
        case relatedPathsText = "related_paths_text"
        case relatedPathsDefinition = "related_paths_definition"
        case actionSourcesText = "action_sources_text"
        case toolNameCounts = "tool_name_counts"
        case toolNameTotal = "tool_name_total"
        case toolNamesElided = "tool_names_elided"
        case commandsPreview = "commands_preview"
        case commandCount = "command_count"
        case commandsElided = "commands_elided"
        case touchedFiles = "touched_files"
        case touchedFilesPreview = "touched_files_preview"
        case touchedFilesElided = "touched_files_elided"
        case captureCoverage = "capture_coverage"
    }

    /// Tool names as a stable list: by descending count, then by name so two
    /// equal counts never reorder between renders.
    var toolNameRows: [(name: String, count: Int)] {
        (toolNameCounts ?? [:]).map { (name: $0.key, count: $0.value) }
            .sorted { $0.count == $1.count ? $0.name < $1.name : $0.count > $1.count }
    }

    /// The command text the payload carried, full list preferred over preview.
    var commandLines: [String] {
        let lines = (commands?.isEmpty == false ? commands : commandsPreview) ?? []
        return lines.filter { PayloadAbsence.text($0) != nil }
    }

    /// The associated paths the payload carried, full list preferred.
    var touchedFileLines: [String] {
        let lines = (touchedFiles?.isEmpty == false ? touchedFiles : touchedFilesPreview) ?? []
        return lines.filter { PayloadAbsence.text($0) != nil }
    }

    /// The payload synopsis, or the named absence when an older daemon sent none.
    var synopsis: ReceiptActionSynopsis {
        actionsSynopsis ?? ReceiptActionSynopsis(
            state: nil, headline: nil,
            tile: actionsTile ?? ReceiptTileText(value: nil, absent: PayloadAbsence.toolCalls, qualifier: nil)
        )
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
    /// `no_usage` / `unpriced` / `partial` / `complete`.
    let state: String?
    let displayText: String?
    let basisLabel: String?
    let legend: String?
    /// The cost half of the verdict gap (nil when fully costed).
    let gapText: String?
    /// The reducer's weekly-plan headline, never a dash.
    let planShareHeadline: String?

    enum CodingKeys: String, CodingKey {
        case estimatedCostUsd = "estimated_cost_usd"
        case costBasis = "cost_basis"
        case costConfidence = "cost_confidence"
        case costComplete = "cost_complete"
        case planShare = "plan_share"
        case tokens
        case provenance, gaps, state, legend
        case displayText = "display_text"
        case basisLabel = "basis_label"
        case gapText = "gap_text"
        case planShareHeadline = "plan_share_headline"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        estimatedCostUsd = try container.decodeIfPresent(Double.self, forKey: .estimatedCostUsd)
        costBasis = try container.decodeIfPresent(String.self, forKey: .costBasis)
        costConfidence = try container.decodeIfPresent(String.self, forKey: .costConfidence)
        costComplete = try container.decodeIfPresent(Bool.self, forKey: .costComplete)
        tokens = try container.decodeIfPresent(ReceiptCostTokens.self, forKey: .tokens)
        provenance = try container.decodeIfPresent([String].self, forKey: .provenance)
        gaps = try container.decodeIfPresent([String].self, forKey: .gaps)
        state = try container.decodeIfPresent(String.self, forKey: .state)
        displayText = try container.decodeIfPresent(String.self, forKey: .displayText)
        basisLabel = try container.decodeIfPresent(String.self, forKey: .basisLabel)
        legend = try container.decodeIfPresent(String.self, forKey: .legend)
        gapText = try container.decodeIfPresent(String.self, forKey: .gapText)
        let headline = try container.decodeIfPresent(String.self, forKey: .planShareHeadline)
        planShareHeadline = headline
        // The dimension's headline rides on its plan share so the existing
        // `planShare.rowSummary` renders the reducer's words.
        var share = try container.decodeIfPresent(ReceiptPlanShare.self, forKey: .planShare)
        if share != nil, PayloadAbsence.text(share?.headline) == nil {
            share?.headline = headline
        }
        planShare = share
    }

    /// The Weekly plan row: the reducer's headline, even when no plan-share
    /// object was stamped (older payloads fall back to the share's own row).
    var planShareText: String {
        PayloadAbsence.text(planShareHeadline) ?? planShare?.rowSummary ?? PayloadAbsence.planShare
    }

    /// True when the reducer names an absence rather than a priced figure.
    var isAbsent: Bool {
        state == "no_usage" || state == "unpriced" || PayloadAbsence.text(displayText) == nil
    }

    /// The cost line: the reducer's figure with its basis label, or the named
    /// absence it sent (a payload without either names that absence).
    var text: String {
        guard let display = PayloadAbsence.text(displayText) else { return PayloadAbsence.cost }
        if state == "no_usage" || state == "unpriced" { return display }
        return "\(display) · \(PayloadAbsence.text(basisLabel) ?? PayloadAbsence.costBasis)"
    }
}

/// The git revision a check ran at, read mechanically (never self-reported).
struct ReceiptCheckRevision: Decodable, Equatable {
    let commit: String?
    let branch: String?
    let dirty: Bool?
    let basis: String?
}

struct ReceiptCheck: Decodable, Identifiable {
    let kind: String?
    /// The agent's own short check name (nil when none was recorded).
    let name: String?
    let result: String?
    let exitCode: Int?
    let scope: String?
    let source: String?
    // Detail-on-expand fields; every one optional so an older payload decodes.
    let superseded: Bool?
    let at: Double?
    /// The agent's summary, verbatim — nil when absent or only `<name>: <result>`.
    let summary: String?
    let files: [String]?
    // True says a command was recorded; receipts never show its text.
    let commandRedacted: Bool?
    let artifactRef: String?
    let artifactUrl: String?
    // Present only on a failing check that is a surfaced finding episode —
    // the handle the disposition controls post with.
    let finding: ReceiptCheckFinding?
    // Reducer fields (additive; memberwise defaults keep older call sites).
    /// What every surface prints as the check's title: name, else summary,
    /// else evidence type.
    var title: String? = nil
    var evidenceType: String? = nil
    /// `The agent's command argument was not stored; the title is the name the agent recorded.` (nil when none).
    var commandStateText: String? = nil
    var artifactUrlRedacted: Bool? = nil
    var artifactPath: String? = nil
    var artifactPathRedacted: Bool? = nil
    var revision: ReceiptCheckRevision? = nil
    /// `at 8a4e024 · main · uncommitted changes` or `revision not captured`.
    var revisionLabel: String? = nil
    /// Every recorded run of this check identity, and how many earlier ones failed.
    var runsTotal: Int? = nil
    var earlierFailed: Int? = nil
    var sectionId: String? = nil
    /// The source's display label when the payload carries one.
    var sourceLabel: String? = nil
    /// What "superseded" means for this run (the reducer's one sentence);
    /// nil when the run is current.
    var supersededDefinition: String? = nil
    /// The reducer's result words (`Passed`, `Failed`, `Could not run`).
    var resultLabel: String? = nil
    /// The reducer's tone key (`pass` / `failure` / `not_run`).
    var resultTone: String? = nil
    /// A named result/exit-code disagreement (nil when they agree).
    var noteText: String? = nil
    /// The reducer's redaction sentence for a withheld artifact path / URL.
    var artifactPathStateText: String? = nil
    var artifactUrlStateText: String? = nil
    /// This run's own event identity — the handle every supersession pointer
    /// below names, and what ties a receipt row to its timeline record.
    var eventId: String? = nil
    /// True for an EARLIER run of a check identity that a later run replaced.
    /// The header tally counts only the frontier, so a surface that renders
    /// history rows as peers would double-count them.
    var historyRun: Bool? = nil
    var supersededByEventId: String? = nil
    /// The failed run this passing run names as fixed, and on whose authority
    /// (`agent_declared` / `reciprocal_of_supersession`).
    var supersedesCheckEventId: String? = nil
    var supersedesBasis: String? = nil
    /// `agent_recorded` / `digest_only` — two different facts about a command
    /// that is not shown. `commandStateText` carries the reducer's sentence.
    var commandState: String? = nil
    /// Paths this check declared that the stamped revision cannot contain.
    var revisionAbsentFiles: [String]? = nil
    /// The reducer's sentence naming that contradiction (nil when none). The
    /// reducer CLEARS it on every row a group banner covers, so a surface that
    /// does not read `revision_groups[].contradiction_text` prints the sentence
    /// nowhere at all.
    var revisionContradictionText: String? = nil
    /// The row's one meta line — `Failed · Exit 1 · test · Agent-reported`,
    /// joined by the reducer on the single separator, with whichever fields are
    /// uniform across every row already hoisted OUT of it and onto the section
    /// heading. It replaces the four words a surface used to punctuate as four
    /// sentences.
    var metaLine: String? = nil
    /// A run of WHOLE sentences from the start of `summary`: the first always,
    /// then as many more as fit the reducer's budget. It never ends mid-clause
    /// and carries no ellipsis, so it must not be clamped to a line count.
    var summaryPreview: String? = nil
    /// True when `summary` holds more than `summaryPreview` shows.
    var summaryElided: Bool? = nil
    /// Which of `dimensions.evidence.revision_groups` this row belongs to.
    var revisionGroupIndex: Int? = nil
    /// True when the group header prints this row's revision label, so the row
    /// must not print it a second time.
    var revisionLabelHoisted: Bool? = nil

    /// A run whose own result failed but which a later run replaced: the
    /// recovery half of a fail → pass story, and neither a live failure nor a
    /// neutral record.
    var isResolvedFailure: Bool {
        (historyRun == true || superseded == true)
            && CheckResultTone(payload: resultTone) == .failure
    }

    /// Event identity first: two runs of one check identity differ only by
    /// event, and a name/result/exit triple collides across reruns.
    var id: String {
        PayloadAbsence.text(eventId)
            ?? "\(name ?? "check")-\(result ?? "")-\(exitCode ?? 0)-\(at ?? 0)"
    }

    /// The revision line, or its named absence.
    var revisionText: String {
        PayloadAbsence.text(revisionLabel) ?? PayloadAbsence.revision
    }

    /// The row's ONE meta line. The reducer composes it and hoists whatever is
    /// uniform across the rows; an older payload that carries no `meta_line`
    /// falls back to the same four facts joined on the SAME separator every
    /// surface uses, so there is never a second punctuation grammar — and never
    /// again four fragments each ended with a full stop.
    var metaLineText: String {
        if let line = PayloadAbsence.text(metaLine) { return line }
        return workMetaLine(
            client: PayloadAbsence.text(resultLabel) ?? PayloadAbsence.checkResult,
            project: exitCode.map { "Exit \($0)" },
            trailing: [PayloadAbsence.text(evidenceType), PayloadAbsence.text(sourceLabel)]
        )
    }

    enum CodingKeys: String, CodingKey {
        case kind, name, result, scope, source, superseded, at, summary, files, finding
        case title, revision
        case exitCode = "exit_code"
        case commandRedacted = "command_redacted"
        case artifactRef = "artifact_ref"
        case artifactUrl = "artifact_url"
        case evidenceType = "evidence_type"
        case commandStateText = "command_state_text"
        case artifactUrlRedacted = "artifact_url_redacted"
        case artifactPath = "artifact_path"
        case artifactPathRedacted = "artifact_path_redacted"
        case revisionLabel = "revision_label"
        case runsTotal = "runs_total"
        case earlierFailed = "earlier_failed"
        case sectionId = "section_id"
        case sourceLabel = "source_label"
        case supersededDefinition = "superseded_definition"
        case resultLabel = "result_label"
        case resultTone = "result_tone"
        case noteText = "note_text"
        case artifactPathStateText = "artifact_path_state_text"
        case artifactUrlStateText = "artifact_url_state_text"
        case eventId = "event_id"
        case historyRun = "history_run"
        case supersededByEventId = "superseded_by_event_id"
        case supersedesCheckEventId = "supersedes_check_event_id"
        case supersedesBasis = "supersedes_basis"
        case commandState = "command_state"
        case revisionAbsentFiles = "revision_absent_files"
        case revisionContradictionText = "revision_contradiction_text"
        case metaLine = "meta_line"
        case summaryPreview = "summary_preview"
        case summaryElided = "summary_elided"
        case revisionGroupIndex = "revision_group_index"
        case revisionLabelHoisted = "revision_label_hoisted"
    }
}

/// One run of adjacent check rows sharing a stamped revision
/// (`dimensions.evidence.revision_groups[]`).
///
/// The grouping is the REDUCER'S decision, not a Swift heuristic: it falls back
/// to strict time order whenever grouping by revision would split a
/// supersession pair across two groups, because a fail → pass recovery has to
/// stay legible as two adjacent rows. `revisionGroupingMode` says which choice
/// it made; walking each group's `eventIds` in order yields every row exactly
/// once, in time order, under either mode.
struct ReceiptCheckRevisionGroup: Decodable, Identifiable {
    let revision: ReceiptCheckRevision?
    /// The stamped revision line, printed ONCE over the group. On the flagship
    /// record this took `HEAD when recorded: c41d44f · main · uncommitted
    /// changes` from four prints to one.
    let label: String?
    let eventIds: [String]?
    let rowCount: Int?
    /// The reducer's one contradiction sentence for this group, emitted only
    /// when more than one row carried it byte-identically — and cleared from
    /// those rows. When the rows' sentences differ this is nil and each row
    /// keeps its own; no merged sentence is ever composed.
    let contradictionText: String?

    var id: String { (eventIds?.first).map { "group-\($0)" } ?? (label ?? "group") }

    enum CodingKeys: String, CodingKey {
        case revision, label
        case eventIds = "event_ids"
        case rowCount = "row_count"
        case contradictionText = "contradiction_text"
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
        title = Self.nonEmpty(check.title)
            ?? Self.nonEmpty(check.name)
            ?? Self.nonEmpty(check.evidenceType)
            ?? Self.nonEmpty(check.kind)
            ?? "Unnamed check"
        resultLabel = PayloadAbsence.text(check.resultLabel) ?? PayloadAbsence.checkResult
        sourceLabel = Self.sourceLabel(check)
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

    /// The payload's `source_label`, verbatim. No Swift label table: a check
    /// that names a source the payload did not label reads as the named
    /// absence, never a key rewritten in Swift.
    private static func sourceLabel(_ check: ReceiptCheck) -> String? {
        if let label = PayloadAbsence.text(check.sourceLabel) { return label }
        return nonEmpty(check.source) == nil ? nil : PayloadAbsence.source
    }

    private static func group(_ check: ReceiptCheck) -> ReceiptCheckGroup {
        if check.superseded == true { return .history }
        if check.finding?.attentionOpen == false { return .history }
        if let state = nonEmpty(check.finding?.state), state != "open" { return .history }
        // The reducer's tone key: only a recorded failure needs you; a check
        // that could not run is an "other" result, never grouped with failures.
        switch CheckResultTone(payload: check.resultTone) {
        case .failure: return .attention
        case .pass: return .passed
        case .notRun: return .other
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
            itemizedNotice = "\(rows.count) itemized \(noun) available for \(total) reported checks."
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
    // Reducer tally fields (additive; memberwise defaults keep call sites).
    var checksSuperseded: Int? = nil
    var checksEarlierFailed: Int? = nil
    var checksTile: ReceiptTileText? = nil
    /// `150/156 passed · 4 failed · 2 superseded · 1 earlier run failed`.
    var checkTallyText: String? = nil
    /// `failed` / `passed` / `not_reported` / `none`.
    var checkRunsState: String? = nil
    /// The section's ONE heading line: the tally, the evidence tier stated here
    /// and nowhere else on the page, then whichever meta fields were uniform
    /// across every row. Composed by the reducer.
    var headingLine: String? = nil
    /// The uniform fields the heading line hoisted off the rows — present so a
    /// surface can tell a hoisted fact from one that was never recorded.
    var hoistedSourceLabel: String? = nil
    var hoistedEvidenceType: String? = nil
    /// `by_revision` or `time_order` — the reducer's grouping decision.
    var revisionGroupingMode: String? = nil
    var revisionGroups: [ReceiptCheckRevisionGroup]? = nil

    enum CodingKeys: String, CodingKey {
        case checks
        case checksTotal = "checks_total"
        case checksPassed = "checks_passed"
        case checksFailed = "checks_failed"
        case provenance, gaps
        case checksSuperseded = "checks_superseded"
        case checksEarlierFailed = "checks_earlier_failed"
        case checksTile = "checks_tile"
        case checkTallyText = "check_tally_text"
        case checkRunsState = "check_runs_state"
        case headingLine = "heading_line"
        case hoistedSourceLabel = "hoisted_source_label"
        case hoistedEvidenceType = "hoisted_evidence_type"
        case revisionGroupingMode = "revision_grouping_mode"
        case revisionGroups = "revision_groups"
    }
}

/// Recorded checks are independent from step coverage. The reducer's
/// `checks_tile` / `check_tally_text` are the values; a malformed tally still
/// names its conflict, and a payload without the strings keeps any supplied
/// counts visible in the reducer's grammar instead of converting missing
/// values to zero.
struct ReceiptCheckRunsPresentation {
    let value: String
    let qualifier: String
    let rowText: String
    let headerText: String
    let isInconsistent: Bool

    /// The reducer's tile and tally for a receipt's evidence dimension.
    init(evidence: ReceiptEvidenceDim) {
        self.init(
            total: evidence.checksTotal,
            passed: evidence.checksPassed,
            failed: evidence.checksFailed,
            tile: evidence.checksTile,
            tallyText: evidence.checkTallyText
        )
    }

    /// The reducer's tile and tally for a task-list row's evidence strength.
    init(strength: ReceiptEvidence) {
        self.init(
            total: strength.checksTotal,
            passed: strength.checksPassed,
            failed: strength.checksFailed,
            tile: strength.checksTile,
            tallyText: strength.checkTallyText
        )
    }

    init(
        total: Int?,
        passed: Int?,
        failed: Int?,
        tile: ReceiptTileText? = nil,
        tallyText: String? = nil
    ) {
        let genericCountsConflict = Self.genericCountsConflict(
            total: total,
            passed: passed,
            failed: failed
        )
        let zeroTotalConflict = total == 0 && ((passed ?? 0) != 0 || (failed ?? 0) != 0)
        isInconsistent = genericCountsConflict || zeroTotalConflict
        let tally = PayloadAbsence.text(tallyText)
        if genericCountsConflict {
            value = "Inconsistent counts"
            let tallies = Self.tallies(passed: passed, failed: failed)
            let totalText = total.map { "\($0) total reported" } ?? "total not reported"
            qualifier = "\(tallies) · \(totalText)"
            rowText = "inconsistent checks · \(tallies) · "
                + (total.map { "\($0) total" } ?? "total not reported")
            headerText = "inconsistent · \(tallies) · "
                + (total.map { "\($0) total" } ?? "total not reported")
        } else if zeroTotalConflict {
            value = "0 total reported"
            let tallies = Self.tallies(passed: passed, failed: failed)
            qualifier = tallies + " · tallies conflict with total"
            rowText = "0 total reported · \(tallies)"
            headerText = rowText
        } else if let tileValue = PayloadAbsence.text(tile?.value) {
            // The reducer's own tile and tally — rendered verbatim.
            value = tileValue
            qualifier = PayloadAbsence.text(tile?.qualifier) ?? ""
            rowText = tally ?? [tileValue, qualifier].filter { !$0.isEmpty }.joined(separator: " ")
            headerText = rowText
        } else if let absent = PayloadAbsence.text(tile?.absent) {
            // The reducer's named absence (`no checks recorded`) — verbatim.
            value = absent
            qualifier = PayloadAbsence.text(tile?.qualifier) ?? ""
            rowText = tally ?? absent
            headerText = rowText
        } else if let tally {
            value = tally
            qualifier = ""
            rowText = tally
            headerText = tally
        } else if let total, total > 0 {
            if let passed {
                value = "\(passed)/\(total)"
                qualifier = "passed" + Self.failedSuffix(failed)
                rowText = "\(passed)/\(total) passed" + Self.failedSuffix(failed)
                headerText = rowText
            } else {
                value = "Not reported"
                qualifier = "passes unavailable · \(Fmt.count(total, "check"))" + Self.failedSuffix(failed)
                rowText = "passes not reported · \(Fmt.count(total, "check"))" + Self.failedSuffix(failed)
                headerText = "passes not reported · \(total) total" + Self.failedSuffix(failed)
            }
        } else if total == 0 {
            value = "none"
            qualifier = "no checks recorded"
            rowText = "no checks recorded"
            headerText = rowText
        } else {
            value = "Total not reported"
            let tallies = Self.tallies(passed: passed, failed: failed)
            qualifier = tallies == "no tallies reported"
                ? "check totals unavailable"
                : tallies
            rowText = tallies == "no tallies reported"
                ? PayloadAbsence.checks
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
    // Facts the daemon attaches only when the decision is `inactive`/`mostly_done`:
    // when this Task last recorded an event, and the start of the newer session
    // the daemon keyed its "went quiet elsewhere" inference off. Epoch seconds,
    // matching every other timestamp field in the app. Present-but-None on every
    // other decision key; older payloads omit them entirely (additive, tolerated).
    let quietSince: Double?
    let newerSessionStartedAt: Double?
    let assertedByLabel: String?
    /// The agent's own outcome words: the newest step summary, verbatim, with
    /// the label naming it agent-reported (never a verified statement).
    let summary: String?
    let summaryLabel: String?
    let summarySectionTitle: String?
    /// The agent's recorded continuation point (withheld once the work reads done).
    let nextStep: String?
    let nextStepSectionTitle: String?
    let nextAction: String?

    enum CodingKeys: String, CodingKey {
        case decisionStatus = "decision_status"
        case statement, summary
        case assertedBy = "asserted_by"
        case provenance, gaps
        case quietSince = "quiet_since"
        case newerSessionStartedAt = "newer_session_started_at"
        case assertedByLabel = "asserted_by_label"
        case summaryLabel = "summary_label"
        case summarySectionTitle = "summary_section_title"
        case nextStep = "next_step"
        case nextStepSectionTitle = "next_step_section_title"
        case nextAction = "next_action"
    }
}

struct ReceiptGapItem: Decodable, Identifiable {
    let dimension: String
    let reason: String
    /// The reducer's label for the dimension (`Agents`, `Tool calls`).
    var dimensionLabel: String? = nil
    /// `blocks_review` or `provenance` — whether this gap stops a reviewer or is
    /// bookkeeping.
    var kind: String? = nil
    var kindLabel: String? = nil
    /// The typed gap code, never a sentence to match on.
    var code: String? = nil
    /// The absence-budget noun that already carries this gap, when one does.
    /// A gap WITH a key is spoken by the collapsed absence line and belongs in
    /// its disclosure; a gap without one has no noun and stays a sentence.
    var absenceKey: String? = nil
    var rank: Int? = nil
    var id: String { "\(dimension)-\(reason)" }

    /// The printed group name: the payload label, else the raw key de-snaked
    /// (an older payload) — never the bare key.
    var label: String {
        PayloadAbsence.text(dimensionLabel) ?? dimension.replacingOccurrences(of: "_", with: " ").capitalized
    }

    enum CodingKeys: String, CodingKey {
        case dimension, reason, kind, code, rank
        case dimensionLabel = "dimension_label"
        case kindLabel = "kind_label"
        case absenceKey = "absence_key"
    }
}

/// One sentence behind the collapsed absence line: the noun the line printed,
/// and the full sentence it stands for. Absence stays NAMED — the line is a
/// summary of these, never a replacement for them.
struct ReceiptNotCapturedDetail: Decodable, Identifiable {
    let key: String
    let noun: String?
    let text: String?
    var id: String { key }
}

/// The record page's whole absence budget, in one statement
/// (`dimensions.gaps.not_captured`).
///
/// `line` is nil when nothing is missing, and an empty budget prints NOTHING —
/// never a positive "everything was captured" claim, which no receipt can make.
struct ReceiptNotCaptured: Decodable {
    let line: String?
    let keys: [String]?
    let detail: [ReceiptNotCapturedDetail]?
    let detailCount: Int?

    enum CodingKeys: String, CodingKey {
        case line, keys, detail
        case detailCount = "detail_count"
    }
}

struct ReceiptGapsDim: Decodable {
    let items: [ReceiptGapItem]?
    let count: Int?
    /// The collapsed absence statement. Every sentence it stands for is still
    /// in its `detail`.
    var notCaptured: ReceiptNotCaptured? = nil

    enum CodingKeys: String, CodingKey {
        case items, count
        case notCaptured = "not_captured"
    }
}

struct ReceiptProvenanceDim: Decodable {
    let byDimension: [String: [String]]?
    let sourcesPresent: [String]?
    let legend: [String: String]?
    /// Each present source with the reducer's label, legend sentence and tier.
    let sources: [ReceiptSourceEntry]?
    /// The named absence when no dimension recorded a source.
    var sourcesAbsentText: String? = nil

    enum CodingKeys: String, CodingKey {
        case byDimension = "by_dimension"
        case sourcesPresent = "sources_present"
        case legend, sources
        case sourcesAbsentText = "sources_absent_text"
    }

    /// The reducer's label for one source key; a key it did not label is
    /// shown de-snaked, never renamed.
    func sourceLabel(for key: String) -> String {
        if let label = PayloadAbsence.text(sources?.first(where: { $0.key == key })?.label) {
            return label
        }
        return key.replacingOccurrences(of: "_", with: " ")
    }
}

// MARK: - /v1 worksets (folder-anchored Work groupings)
//
// A workset is the user's own overlay: the sessions under one folder are one
// piece of work, gathered live across Claude Code and Codex. Every aggregate is
// a labeled SUM of independently-attributed sessions, never a re-graded verdict
// — the honesty rides the payload, as everywhere else on this lane.

struct WorksetSource: Decodable, Identifiable {
    let client: String
    let sessionCount: Int
    var id: String { client }
    enum CodingKeys: String, CodingKey {
        case client
        case sessionCount = "session_count"
    }
}

struct WorksetSummary: Decodable {
    let sessionCount: Int
    let sources: [WorksetSource]
    let firstActivityAt: Double?
    let lastActivityAt: Double?
    let totalTokens: Int?
    let estimatedCostUsd: Double?
    /// True only when every member session is priced; a partial sum otherwise.
    let costComplete: Bool?
    let pricedSessions: Int?
    let unpricedSessions: Int?
    let costConfidence: String?
    let costBasis: String?

    enum CodingKeys: String, CodingKey {
        case sources
        case sessionCount = "session_count"
        case firstActivityAt = "first_activity_at"
        case lastActivityAt = "last_activity_at"
        case totalTokens = "total_tokens"
        case estimatedCostUsd = "estimated_cost_usd"
        case costComplete = "cost_complete"
        case pricedSessions = "priced_sessions"
        case unpricedSessions = "unpriced_sessions"
        case costConfidence = "cost_confidence"
        case costBasis = "cost_basis"
    }
}

struct WorksetLane: Decodable, Identifiable {
    let sessionKey: String?
    let client: String?
    let clientSessionId: String?
    let title: String?
    let sessionKind: String?
    let status: String?
    let firstActivityAt: Double?
    let lastActivityAt: Double?
    let durationSeconds: Double?
    let totalTokens: Int?
    let estimatedCostUsd: Double?
    let costConfidence: String?
    let toolCalls: Int?
    let steps: Int?
    let checks: Int?
    let checksFailed: Int?

    var id: String { sessionKey ?? "\(client ?? "")::\(clientSessionId ?? "")" }

    var displayTitle: String {
        if let title, !title.isEmpty { return title }
        let short = clientSessionId.map { String($0.prefix(8)) } ?? "session"
        return "\(client ?? "session") · \(short)"
    }

    enum CodingKeys: String, CodingKey {
        case client, title, status, steps, checks
        case sessionKey = "session_key"
        case clientSessionId = "client_session_id"
        case sessionKind = "session_kind"
        case firstActivityAt = "first_activity_at"
        case lastActivityAt = "last_activity_at"
        case durationSeconds = "duration_seconds"
        case totalTokens = "total_tokens"
        case estimatedCostUsd = "estimated_cost_usd"
        case costConfidence = "cost_confidence"
        case toolCalls = "tool_calls"
        case checksFailed = "checks_failed"
    }
}

/// GET /v1/workset returns the card fields at the top level (with a `schema`
/// key alongside), so the detail response decodes as a `WorksetCard` directly.
struct WorksetCard: Decodable, Identifiable {
    let worksetId: String
    let name: String
    let projectIdentity: String
    let revision: Int
    let deleted: Bool?
    let createdAt: Double?
    let updatedAt: Double?
    let summary: WorksetSummary
    let sessions: [WorksetLane]
    let sessionsTotal: Int?
    let sessionsTruncated: Bool?

    var id: String { worksetId }

    enum CodingKeys: String, CodingKey {
        case name, revision, deleted, summary, sessions
        case worksetId = "workset_id"
        case projectIdentity = "project_identity"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case sessionsTotal = "sessions_total"
        case sessionsTruncated = "sessions_truncated"
    }
}

struct WorksetsPayload: Decodable {
    let schema: String
    let worksets: [WorksetCard]
    let total: Int?
}

struct WorksetCandidate: Decodable, Identifiable {
    let projectIdentity: String
    let label: String
    let sessionCount: Int
    let sources: [String]
    let firstActivityAt: Double?
    let lastActivityAt: Double?
    /// The id of a live group already anchored at this folder, if any — the
    /// picker uses it to avoid offering a duplicate.
    let existingWorksetId: String?

    var id: String { projectIdentity }
    var alreadyGrouped: Bool { existingWorksetId != nil }

    enum CodingKeys: String, CodingKey {
        case label, sources
        case projectIdentity = "project_identity"
        case sessionCount = "session_count"
        case firstActivityAt = "first_activity_at"
        case lastActivityAt = "last_activity_at"
        case existingWorksetId = "existing_workset_id"
    }
}

// MARK: - Receipt summary line items

/// One receipt line item: a value with its qualifier, or a named absence —
/// never a fabricated zero.
struct ReceiptSummaryItem: Identifiable, Equatable {
    let id: String
    let label: String
    let value: String?
    let qualifier: String?
    let absent: String?
    let isWarning: Bool
}

/// The receipt's line items, derived once from the receipt and its optional
/// summary row. Every value, qualifier and field label is a reducer string;
/// a missing string is a named absence. The derivation is split from the view
/// so each state can be tested without rendering.
struct RecordSummaryPresentation: Equatable {
    struct Inputs: Equatable {
        var fieldLabels: ReceiptFieldLabels?
        /// The verdict's proof clause in tile form (`1/1` · `self-checked
        /// completed steps`, or the named absence `not gradeable`).
        var coverageTile: ReceiptTileText?
        var checksTile: ReceiptTileText?
        var toolCalls: ReceiptTileText?
        var costDisplayText: String?
        var costBasisLabel: String?
        var costState: String?
        var sessionCount: Int?
        var sessionRoots: Int
    }

    let items: [ReceiptSummaryItem]

    init(receipt: Receipt, summary: ReceiptSummary?) {
        let dimensions = receipt.dimensions
        let strength = receipt.axes.evidenceStrength
        self.init(inputs: Inputs(
            fieldLabels: receipt.fieldLabels,
            coverageTile: strength.coverageTile,
            checksTile: dimensions.evidence.checksTile ?? strength.checksTile,
            toolCalls: dimensions.actions.actionsTile ?? dimensions.actions.synopsis.tile,
            costDisplayText: dimensions.cost.displayText,
            costBasisLabel: dimensions.cost.basisLabel,
            costState: dimensions.cost.state,
            sessionCount: summary?.sessionCount ?? dimensions.task.boundary?.sessionCount,
            sessionRoots: receipt.sessions?.count ?? 0
        ))
    }

    /// The tile strip states the figures, Coverage first. The proof clause
    /// used to be the record's LARGEST string — `Not gradeable (only step
    /// stopped: handed off)` set above the task title, so the page shouted the
    /// grading system rather than the work (C1). The clause is a figure like
    /// the others, so it is stated ONCE, here, at tile size; the hero keeps the
    /// state word, the consequence and the bar that draws the same proportion.
    /// An inconsistent count set is not lost: the payload's own tile text
    /// carries it (`ReceiptEvidence.headline`).
    init(inputs: Inputs) {
        let labels = inputs.fieldLabels ?? ReceiptFieldLabels()
        items = [
            Self.coverage(inputs, label: labels.coverageLabel),
            Self.checks(inputs, label: labels.checksLabel),
            Self.toolCalls(inputs, label: labels.actionsLabel),
            Self.cost(inputs, label: labels.costLabel),
            Self.sessions(inputs),
        ]
    }

    private static func coverage(_ inputs: Inputs, label: String) -> ReceiptSummaryItem {
        guard let value = PayloadAbsence.text(inputs.coverageTile?.value) else {
            return ReceiptSummaryItem(id: "coverage", label: label, value: nil,
                                      qualifier: PayloadAbsence.text(inputs.coverageTile?.qualifier),
                                      absent: PayloadAbsence.text(inputs.coverageTile?.absent) ?? PayloadAbsence.coverage,
                                      isWarning: false)
        }
        return ReceiptSummaryItem(id: "coverage", label: label, value: value,
                                  qualifier: PayloadAbsence.text(inputs.coverageTile?.qualifier),
                                  absent: nil, isWarning: false)
    }

    private static func checks(_ inputs: Inputs, label: String) -> ReceiptSummaryItem {
        guard let value = PayloadAbsence.text(inputs.checksTile?.value) else {
            return ReceiptSummaryItem(id: "checks", label: label, value: nil,
                                      qualifier: PayloadAbsence.text(inputs.checksTile?.qualifier),
                                      absent: PayloadAbsence.text(inputs.checksTile?.absent) ?? PayloadAbsence.checks,
                                      isWarning: false)
        }
        return ReceiptSummaryItem(id: "checks", label: label, value: value,
                                  qualifier: PayloadAbsence.text(inputs.checksTile?.qualifier),
                                  absent: nil, isWarning: false)
    }

    private static func toolCalls(_ inputs: Inputs, label: String) -> ReceiptSummaryItem {
        guard let value = PayloadAbsence.text(inputs.toolCalls?.value) else {
            return ReceiptSummaryItem(id: "actions", label: label, value: nil,
                                      qualifier: PayloadAbsence.text(inputs.toolCalls?.qualifier),
                                      absent: PayloadAbsence.text(inputs.toolCalls?.absent) ?? PayloadAbsence.toolCalls,
                                      isWarning: false)
        }
        return ReceiptSummaryItem(id: "actions", label: label, value: value,
                                  qualifier: PayloadAbsence.text(inputs.toolCalls?.qualifier),
                                  absent: nil, isWarning: false)
    }

    private static func cost(_ inputs: Inputs, label: String) -> ReceiptSummaryItem {
        guard let display = PayloadAbsence.text(inputs.costDisplayText) else {
            return ReceiptSummaryItem(id: "cost", label: label, value: nil, qualifier: nil,
                                      absent: PayloadAbsence.cost, isWarning: false)
        }
        // The reducer's display text IS the named absence for these states.
        if inputs.costState == "no_usage" || inputs.costState == "unpriced" {
            return ReceiptSummaryItem(id: "cost", label: label, value: nil, qualifier: nil,
                                      absent: display, isWarning: false)
        }
        return ReceiptSummaryItem(id: "cost", label: label, value: display,
                                  qualifier: PayloadAbsence.text(inputs.costBasisLabel) ?? PayloadAbsence.costBasis,
                                  absent: nil, isWarning: false)
    }

    private static func sessions(_ inputs: Inputs) -> ReceiptSummaryItem {
        guard let count = inputs.sessionCount else {
            return ReceiptSummaryItem(id: "sessions", label: "Sessions", value: nil, qualifier: nil,
                                      absent: "not recorded", isWarning: false)
        }
        return ReceiptSummaryItem(id: "sessions", label: "Sessions", value: "\(count)",
                                  qualifier: inputs.sessionRoots > 1 ? "\(inputs.sessionRoots) roots" : nil,
                                  absent: nil, isWarning: false)
    }

}

struct WorksetCandidatesPayload: Decodable {
    let schema: String
    let candidates: [WorksetCandidate]
}

struct WorksetWriteResponse: Decodable {
    let ok: Bool
    let worksetId: String?
    let action: String?
    let revision: Int?
    let name: String?
    let projectIdentity: String?
    let deleted: Bool?
    let eventId: String?

    enum CodingKeys: String, CodingKey {
        case ok, action, revision, name, deleted
        case worksetId = "workset_id"
        case projectIdentity = "project_identity"
        case eventId = "event_id"
    }
}

/// Response from POST /v1/self-update. `applied` is false when already on the
/// latest version; true (HTTP 202) means the daemon is installing + restarting.
struct SelfUpdateResponse: Decodable {
    let ok: Bool
    let applied: Bool?
    let to: String?
    let reason: String?
    let current: String?
}

/// Places member sessions as bars on ONE shared time axis (the tryairis-style
/// timeline): each bar's left offset and width are fractions of the group's
/// total span, so a Claude Code session and a Codex session read against the
/// same clock instead of two separate orderings. Pure and deterministic so the
/// geometry can be unit-tested without rendering.
struct WorksetTimelineLayout {
    struct Bar: Identifiable {
        let lane: WorksetLane
        /// 0…1 offset from the window start; 0 when the session has no time.
        let leftFraction: Double
        /// 0…1 width; at least `minWidth` so a zero-duration point still shows.
        let widthFraction: Double
        /// The session carries no usable start/end — placed at the start, flagged.
        let timeUnknown: Bool
        var id: String { lane.id }
    }

    /// Bars in start-time order; timeless sessions sort last.
    let bars: [Bar]
    let windowStart: Double?
    let windowEnd: Double?
    /// Members with no single clean time (rendered but flagged), for honesty.
    let timelessCount: Int

    static let minWidth = 0.015

    /// `windowStart`/`windowEnd` override the axis with the group's TRUE span
    /// (from the summary) so a bounded preview of bars still reads against the
    /// whole window — the bars fill the left, and the empty right honestly
    /// shows there is more time than the shown sessions cover.
    init(lanes: [WorksetLane], windowStart windowOverrideStart: Double? = nil, windowEnd windowOverrideEnd: Double? = nil) {
        let starts = lanes.compactMap { Self.time($0.firstActivityAt) }
        let ends = lanes.compactMap { Self.time($0.lastActivityAt) }
        let allTimes = starts + ends
        let overrideLo = Self.time(windowOverrideStart)
        let overrideHi = Self.time(windowOverrideEnd)
        let useOverride = overrideLo != nil && overrideHi != nil && overrideHi! > overrideLo!
        let lo = useOverride ? overrideLo : allTimes.min()
        let hi = useOverride ? overrideHi : allTimes.max()
        windowStart = lo
        windowEnd = hi
        let span = (lo != nil && hi != nil) ? max(0.0, hi! - lo!) : 0.0

        let ordered = lanes.sorted { a, b in
            let ta = Self.time(a.firstActivityAt)
            let tb = Self.time(b.firstActivityAt)
            switch (ta, tb) {
            case let (x?, y?): return x == y ? a.id < b.id : x < y
            case (nil, _?): return false
            case (_?, nil): return true
            case (nil, nil): return a.id < b.id
            }
        }

        var built: [Bar] = []
        var timeless = 0
        for lane in ordered {
            let first = Self.time(lane.firstActivityAt)
            let last = Self.time(lane.lastActivityAt)
            guard let lo, span > 0, let first else {
                // No usable clock, or every session shares one instant.
                if first == nil { timeless += 1 }
                built.append(Bar(lane: lane, leftFraction: 0, widthFraction: Self.minWidth,
                                 timeUnknown: first == nil))
                continue
            }
            let left = min(1.0, max(0.0, (first - lo) / span))
            let rawWidth = (last != nil && last! > first) ? (last! - first) / span : 0.0
            // Size the bar to at least the minimum, THEN pull its left in so it
            // stays fully on the axis — a session at the very end still shows.
            let width = min(1.0, max(Self.minWidth, rawWidth))
            let clampedLeft = min(max(0.0, left), 1.0 - width)
            built.append(Bar(lane: lane, leftFraction: clampedLeft, widthFraction: width, timeUnknown: false))
        }
        bars = built
        timelessCount = timeless
    }

    private static func time(_ value: Double?) -> Double? {
        guard let value, value.isFinite, value > 0 else { return nil }
        return value
    }
}

/// The visible time window of a zoomable timeline: a sub-range of the full
/// [lo, hi], `zoom`× narrower, centered at `panCenter` (0…1) and clamped so it
/// never leaves the data. Pure so the pan/zoom math is unit-tested without a UI.
struct WorksetZoomWindow: Equatable {
    let start: Double
    let end: Double

    init(lo: Double, hi: Double, zoom: Double, panCenter: Double) {
        let full = max(0.0, hi - lo)
        let z = min(WorksetZoomWindow.maxZoom, max(1.0, zoom.isFinite ? zoom : 1.0))
        let width = z > 0 ? full / z : full
        let centerFrac = min(1.0, max(0.0, panCenter.isFinite ? panCenter : 0.5))
        let center = lo + centerFrac * full
        var s = center - width / 2
        var e = center + width / 2
        if s < lo { e = min(hi, e + (lo - s)); s = lo }
        if e > hi { s = max(lo, s - (e - hi)); e = hi }
        start = s
        end = e
    }

    static let maxZoom = 64.0

    /// Whether a session's own [first, last] overlaps a visible window at all —
    /// used to cull sessions entirely outside a zoomed window instead of
    /// clamping them to the edge and drawing them as if they were active there.
    static func laneOverlaps(first: Double?, last: Double?, start: Double, end: Double) -> Bool {
        guard let first, first.isFinite, first > 0 else { return false }
        let l = (last ?? first)
        return !(l < start || first > end)
    }

    /// Whether a lane should STAY VISIBLE in a zoomed window. A timed lane must
    /// overlap the window; a TIMELESS lane (no usable first) has no position at
    /// all, so it is always kept — shown faded at the start and flagged by its
    /// own note — never culled and reclassified as "outside this range".
    static func laneVisible(first: Double?, last: Double?, start: Double, end: Double) -> Bool {
        guard let first, first.isFinite, first > 0 else { return true }
        return laneOverlaps(first: first, last: last, start: start, end: end)
    }

    /// The fraction of the full range this window covers (1 = everything).
    func coverage(lo: Double, hi: Double) -> Double {
        let full = max(0.0, hi - lo)
        guard full > 0 else { return 1.0 }
        return min(1.0, max(0.0, (end - start) / full))
    }

    /// Apply a scroll/pinch zoom `factor` (>1 zooms in, <1 out) anchored at
    /// `anchor` — a 0…1 fraction across the CURRENTLY VISIBLE window — and
    /// return the new `(zoom, panCenter)` so the time under the anchor stays
    /// put. Pure and clamped so the scroll-wheel math is unit-tested without a
    /// UI; the caller re-clamps panCenter to its own half-window bounds.
    static func applyZoom(currentZoom: Double, panCenter: Double, factor: Double,
                          anchor: Double, lo: Double, hi: Double) -> (zoom: Double, panCenter: Double) {
        let full = max(0.0, hi - lo)
        let z0 = min(maxZoom, max(1.0, currentZoom.isFinite ? currentZoom : 1.0))
        let center0 = min(1.0, max(0.0, panCenter.isFinite ? panCenter : 0.5))
        guard full > 0, factor.isFinite, factor > 0 else { return (z0, center0) }
        let win = WorksetZoomWindow(lo: lo, hi: hi, zoom: z0, panCenter: center0)
        let a = min(1.0, max(0.0, anchor.isFinite ? anchor : 0.5))
        // Absolute time sitting under the anchor right now.
        let anchorTime = win.start + a * (win.end - win.start)
        let z1 = min(maxZoom, max(1.0, z0 * factor))
        let newWidth = full / z1
        // Keep that time at the same fraction of the new (narrower/wider) window.
        let newStart = anchorTime - a * newWidth
        let newCenter = newStart + newWidth / 2
        let centerFrac = (newCenter - lo) / full
        return (z1, min(1.0, max(0.0, centerFrac)))
    }
}

/// The honest cost grammar for a workset summary: a bare/≈ prefix only when the
/// sum is complete, `~$` while any member is unpriced (a visibly partial sum),
/// and nothing when no member is priced — never a fabricated $0.
func worksetCostLabel(_ summary: WorksetSummary) -> String? {
    Fmt.costDisplay(
        usd: summary.estimatedCostUsd,
        knownAdditive: (summary.costComplete == true) ? nil : summary.estimatedCostUsd,
        complete: summary.costComplete,
        confidence: summary.costConfidence
    )
}
