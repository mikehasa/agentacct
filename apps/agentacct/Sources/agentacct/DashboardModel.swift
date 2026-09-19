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
    /// unpriced session names its absence — never $0.00 and never a dash. A
    /// reported OR billed figure is exact; matches Fmt.costDisplay so the same
    /// cost never reads exact at the task level and estimated at the step level.
    var costText: String {
        guard let cost = estimatedCostUsd else {
            return usageCostAbsenceText(costState: nil, rows: rows, hasFigure: false) ?? PayloadAbsence.cost
        }
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
    /// The cost-prefix legend, verbatim from the payload.
    var costLegend: String? = nil
    /// The cost chart's legend row (`~$ partial subtotal · open cap = partial`).
    var costChartLegend: String? = nil
    /// The cost chart's unit, named once in its caption (`USD`).
    var costChartUnit: String? = nil
    /// The fresh-token basis words (`client-reported`).
    var tokenBasisLabel: String? = nil
    /// The by-model overlap note (sessions count once per model).
    var byModelSessionsFootnote: String? = nil
    /// The one chart-measure vocabulary, in display order.
    var usageSeries: [UsageSeriesOption]? = nil
    /// The resting measure key for this payload (`cost` when anything is priced).
    var usageSeriesDefault: String? = nil

    enum CodingKeys: String, CodingKey {
        case byClient = "by_client"
        case byModel = "by_model"
        case byPeriod = "by_period"
        case totals
        case filtersEcho = "filters_echo"
        case costLegend = "cost_legend"
        case costChartLegend = "cost_chart_legend"
        case costChartUnit = "cost_chart_unit"
        case tokenBasisLabel = "token_basis_label"
        case byModelSessionsFootnote = "by_model_sessions_footnote"
        case usageSeries = "usage_series"
        case usageSeriesDefault = "usage_series_default"
    }
}

/// One chart measure from the payload vocabulary (`tokens` → `Fresh tokens`).
struct UsageSeriesOption: Decodable, Equatable {
    let key: String
    let label: String
}

struct UsageFiltersEcho: Decodable {
    let granularity: String?
}

struct PeriodBucket: Decodable {
    let period: String?
    let freshTokens: Int?
    let estimatedCostUsd: Double?
    let costComplete: Bool?
    let costConfidence: String?
    let byClient: [String: PeriodClientSlice]?
    // Cube state and labels (additive).
    let rows: Int?
    let pricedRows: Int?
    let unpricedRows: Int?
    let knownAdditiveCostUsd: Double?
    let cacheReadTokens: Int?
    /// `available` / `partial` / `held` / `unknown`.
    let usageAvailability: String?
    /// `none_recorded` / `held` / `unpriced` / `partial` / `complete`.
    let costState: String?
    let costConfidenceDisplay: String?
    /// What this bucket's cost may call itself (`total` / `Partial subtotal ·
    /// N of M usage records unpriced` / a named absence), from the reducer.
    var costTotalLabel: String? = nil
    /// The reducer's DISPLAY name for the bucket (`Sep 12`, `week of Sep 12`).
    /// Every chart, axis band and readout prints this; nothing slices the key.
    var periodLabel: String? = nil

    /// "08-05" from "2026-08-05". The pre-`period_label` fallback only.
    var shortLabel: String {
        guard let period, period.count >= 10 else { return period ?? "" }
        return String(period.dropFirst(5))
    }

    /// The ONE name a chart may show for this bucket: the reducer's label,
    /// which alone says whether the date means a day or a week (K79). Older
    /// payloads without one fall back to the sliced key.
    var displayLabel: String {
        PayloadAbsence.text(periodLabel) ?? shortLabel
    }

    /// The shared cost grammar ($ reported / ≈$ estimate / ~$ partial); with
    /// nothing priced, the cube state's named absence — never a dash.
    var costText: String {
        if costComplete != true, let cost = estimatedCostUsd ?? knownAdditiveCostUsd {
            return Fmt.dollars(cost, prefix: "~$")
        }
        return Fmt.costDisplay(
            usd: estimatedCostUsd,
            knownAdditive: knownAdditiveCostUsd,
            complete: costComplete,
            confidence: costConfidence
        )
            ?? usageCostAbsenceText(costState: costState, rows: rows, hasFigure: false)
            ?? PayloadAbsence.cost
    }

    enum CodingKeys: String, CodingKey {
        case period, rows
        case freshTokens = "fresh_tokens"
        case estimatedCostUsd = "estimated_cost_usd"
        case costComplete = "cost_complete"
        case costConfidence = "cost_confidence"
        case byClient = "by_client"
        case pricedRows = "priced_rows"
        case unpricedRows = "unpriced_rows"
        case knownAdditiveCostUsd = "known_additive_cost_usd"
        case cacheReadTokens = "cache_read_tokens"
        case usageAvailability = "usage_availability"
        case costState = "cost_state"
        case costConfidenceDisplay = "cost_confidence_display"
        case costTotalLabel = "cost_total_label"
        case periodLabel = "period_label"
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
    let costConfidence: String?
    let costConfidenceLabel: String?
    // Cube state and labels (additive).
    let rows: Int?
    let pricedRows: Int?
    let unpricedRows: Int?
    /// `available` / `partial` / `held` / `unknown`.
    let usageAvailability: String?
    /// `none_recorded` / `held` / `unpriced` / `partial` / `complete`.
    let costState: String?
    /// The one human basis label, e.g. `mixed · mostly pricing estimate`.
    let costConfidenceDisplay: String?
    /// What this bucket's cost may call itself (reducer text; see PeriodBucket).
    var costTotalLabel: String? = nil

    var id: String { "\(client ?? "?")::\(model ?? "*")" }

    enum CodingKeys: String, CodingKey {
        case client, model, sessions, rows
        case freshTokens = "fresh_tokens"
        case cacheReadTokens = "cache_read_tokens"
        case estimatedCostUsd = "estimated_cost_usd"
        case costComplete = "cost_complete"
        case knownAdditiveCostUsd = "known_additive_cost_usd"
        case costConfidence = "cost_confidence"
        case costConfidenceLabel = "cost_confidence_label"
        case pricedRows = "priced_rows"
        case unpricedRows = "unpriced_rows"
        case usageAvailability = "usage_availability"
        case costState = "cost_state"
        case costConfidenceDisplay = "cost_confidence_display"
        case costTotalLabel = "cost_total_label"
    }

    /// The shared cost grammar ($ reported / ≈$ estimate / ~$ partial); with
    /// nothing priced, the cube state's named absence — never a dash.
    var costText: String {
        Fmt.costDisplay(
            usd: costComplete == true ? estimatedCostUsd : nil,
            knownAdditive: knownAdditiveCostUsd,
            complete: costComplete,
            confidence: costConfidence
        )
            ?? usageCostAbsenceText(costState: costState, rows: rows, hasFigure: false)
            ?? PayloadAbsence.cost
    }
}
