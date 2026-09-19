import AppKit
import Foundation
import SwiftUI

// The dashboard is a shift brief: what deserves attention now, what recorded
// evidence supports that claim, and where the operator can inspect it. Recent
// work, active sessions, plan headroom, and source health remain supporting
// context rather than four equally weighted destinations.

struct DashboardWorkItem: Identifiable {
    let id: String
    let title: String
    let client: String
    let lastActivityAt: Double?
    let outcome: String
    let outcomeKey: String
    let evidence: String
    let evidenceQualifier: String
    /// Whether `evidence` is a measured figure or a named state — the role
    /// that decides its FACE, from the one coverage presentation (K10).
    let evidenceIsMetric: Bool
    let evidenceIsInconsistent: Bool
    /// The reducer's cost figure (`≈$4.82`) or its named absence
    /// (`no usage recorded`, `unpriced`) — rendered as it arrives.
    let cost: String
    /// The reducer's cost is a named absence, not a figure (K10: prose face).
    let costIsAbsent: Bool
    /// The cost line with its basis (`≈$4.82 · pricing estimate`) for
    /// accessibility, where a cost must carry its basis.
    let costWithBasis: String
    /// The basis phrase on its own (`pricing estimate`), so the row can PRINT
    /// it under the figure. It used to live only in a hover tooltip, and the
    /// rule is that every cost carries its basis on screen (K75).
    let costBasis: String?
    /// The reducer's decision statement, shown as the badge's help (C71).
    let decisionStatement: String?
    let gradeable: Bool
    let strongestTierKey: String?

    init(task: ReceiptSummary) {
        id = task.taskId
        if let taskTitle = task.title, !taskTitle.isEmpty {
            title = taskTitle
        } else {
            title = task.taskId
        }
        client = task.primaryRoot?.client ?? "Unknown agent"
        lastActivityAt = task.lastActivityAt
        outcomeKey = task.decisionStatus.key
        // The reducer owns the decision word; Swift never re-cases a key.
        outcome = PayloadAbsence.text(task.decisionStatus.label) ?? DashboardVocabulary.decisionNotReported
        decisionStatement = PayloadAbsence.text(task.decisionStatus.statement)
        let evidencePresentation = ReceiptCoveragePresentation(evidence: task.evidenceStrength)
        evidence = task.evidenceStrength.compactHeadline
        evidenceQualifier = evidencePresentation.qualifier
        evidenceIsMetric = evidencePresentation.valueIsMetric
        evidenceIsInconsistent = evidencePresentation.isInconsistent
        cost = PayloadAbsence.text(task.cost.displayText) ?? PayloadAbsence.cost
        costIsAbsent = task.cost.isAbsent
        costWithBasis = task.cost.text
        // A named absence carries no basis; only a figure does.
        costBasis = task.cost.isAbsent ? nil : PayloadAbsence.text(task.cost.basisLabel)
        gradeable = task.evidenceStrength.gradeable == true
        strongestTierKey = task.evidenceStrength.strongestTier
    }

    var recency: String? {
        agoText(lastActivityAt)
    }

    /// Strongest evidence tier present (drives the row's tier pip).
    var strongestTier: String? {
        gradeable ? (strongestTierKey ?? "unchecked") : nil
    }

}

/// Named absences the Dashboard shows when an older payload omits a reducer
/// string. They name what is missing; they never re-derive the missing word.
enum DashboardVocabulary {
    static let decisionNotReported = "decision not reported"
    static let reasonNotReported = "attention reason not reported"
    static let sourceNotReported = "source not reported"
    static let timeNotReported = "time not reported"
}

/// UI projection of one server-ranked attention row. It deliberately exposes
/// the recorded reason and recorded next step separately: nil stays nil, so a
/// generic UI hint can never masquerade as agent-authored recovery guidance.
struct DashboardAttentionItem: Identifiable, Equatable {
    let id: String
    let title: String
    let project: String?
    let client: String?
    /// Internal sort key only — never a display word.
    let reasonKind: String
    /// The reducer's reason noun (`Failed check`, `Blocker`).
    let reasonLabel: String
    /// The reducer's full recorded reason naming the evidence it rests on
    /// (`Failed test check · pytest · exit 2`, `Blocker · Deploy`).
    let label: String
    let summary: String
    let nextStep: String?
    let observedAt: Double?
    let sourceLabel: String?
    let handedOff: Bool?
    /// Decision axis for the attention card (C39): key drives the tint class,
    /// label and statement are reducer text.
    let decisionKey: String
    let decisionLabel: String
    let decisionStatement: String?
    let verdictHeadline: String?

    init?(task: ReceiptSummary) {
        guard let reason = task.attention else { return nil }
        id = task.taskId
        if let taskTitle = task.title, !taskTitle.isEmpty {
            title = taskTitle
        } else {
            title = task.taskId
        }
        project = task.project
        client = task.primaryRoot?.client
        reasonKind = reason.kind
        let reasonWord = PayloadAbsence.text(reason.reasonLabel) ?? DashboardVocabulary.reasonNotReported
        reasonLabel = reasonWord
        label = PayloadAbsence.text(reason.label) ?? reasonWord
        summary = reason.summary
        nextStep = PayloadAbsence.text(reason.nextStep)
        observedAt = reason.observedAt
        handedOff = task.handedOff
        sourceLabel = PayloadAbsence.text(reason.sourceLabel)
        decisionKey = task.decisionStatus.key
        decisionLabel = PayloadAbsence.text(task.decisionStatus.label) ?? DashboardVocabulary.decisionNotReported
        decisionStatement = PayloadAbsence.text(task.decisionStatus.statement)
        // Beside the decision badge: the proof clause alone, so the decision
        // word is never printed twice side by side.
        verdictHeadline = task.verdict?.badgeClause
    }

    var recency: String? { agoText(observedAt) }
}

/// A paste-ready brief assembled only from fields the daemon recorded. It
/// never guesses a recovery step and never implies that copying changes agent
/// state. A handoff marker changes the framing, not the underlying facts.
struct DashboardActionBrief: Equatable {
    enum Kind: Equatable {
        case review
        case continuation
    }

    let kind: Kind
    let text: String

    init(focus: DashboardAttentionItem) {
        kind = focus.handedOff == true ? .continuation : .review

        var lines = [
            focus.handedOff == true ? "Continuation brief" : "Review brief",
            "Task: \(focus.title)",
            "Task ID: \(focus.id)",
        ]
        if let project = focus.project, !project.isEmpty {
            lines.append("Project: \(project)")
        }
        if let client = focus.client, !client.isEmpty {
            lines.append("Agent: \(client)")
        }
        lines.append("Decision: \(focus.decisionLabel)")
        lines.append("Recorded reason: \(focus.label)")
        lines.append("Recorded summary: \(focus.summary)")
        lines.append("Recorded next step: \(focus.nextStep ?? NextStepRow.absence)")
        lines.append("Observed: \(Self.timestamp(focus.observedAt) ?? DashboardVocabulary.timeNotReported)")
        lines.append("Provenance: \(focus.sourceLabel ?? DashboardVocabulary.sourceNotReported)")
        text = lines.joined(separator: "\n")
    }

    var buttonTitle: String {
        switch kind {
        case .review: return "Copy review brief"
        case .continuation: return "Copy continuation brief"
        }
    }

    var copiedAccessibilityLabel: String {
        switch kind {
        case .review: return "Review brief copied"
        case .continuation: return "Continuation brief copied"
        }
    }

    var failedAccessibilityLabel: String {
        switch kind {
        case .review: return "Review brief copy failed"
        case .continuation: return "Continuation brief copy failed"
        }
    }

    private static func timestamp(_ epoch: Double?) -> String? {
        guard let epoch else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.string(from: Date(timeIntervalSince1970: epoch))
    }
}

@MainActor
enum DashboardClipboard {
    static func copy(
        _ text: String,
        to pasteboard: NSPasteboard = .general
    ) -> Bool {
        pasteboard.clearContents()
        return pasteboard.setString(text, forType: .string)
    }
}

enum DashboardCopyFeedback: Equatable {
    case idle
    case copied(String)
    case failed(String)

    mutating func record(succeeded: Bool, text: String) {
        self = succeeded ? .copied(text) : .failed(text)
    }

    mutating func clear() {
        self = .idle
    }
}

enum DashboardAttentionPresentation: Equatable {
    case loading
    case unavailable(String)
    case clear
    case focus(item: DashboardAttentionItem, total: Int)
    case inconsistent(total: Int)

    init(payload: V1AttentionPayload?, error: String?) {
        if let error {
            self = .unavailable(error)
            return
        }
        guard let payload else {
            self = .loading
            return
        }
        guard Self.hasConsistentEnvelope(payload) else {
            self = .inconsistent(total: max(0, payload.total))
            return
        }
        guard payload.total > 0 else {
            self = .clear
            return
        }
        if let item = payload.items.lazy.compactMap(DashboardAttentionItem.init).first {
            self = .focus(item: item, total: payload.total)
        } else {
            self = .inconsistent(total: payload.total)
        }
    }

    private static func hasConsistentEnvelope(_ payload: V1AttentionPayload) -> Bool {
        let taskIDs = payload.items.map {
            $0.taskId.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        guard payload.schema == "agentacct.v1-attention.v1",
              payload.total >= 0,
              payload.offset == 0,
              (1 ... 50).contains(payload.limit),
              payload.counts.failedCheck >= 0,
              payload.counts.failedStep >= 0,
              payload.counts.blocker >= 0,
              payload.counts.checkNotRun >= 0,
              payload.items.count <= payload.limit,
              payload.items.count <= payload.total,
              taskIDs.allSatisfy({ !$0.isEmpty }),
              Set(taskIDs).count == payload.items.count,
              payload.items.allSatisfy({ $0.attention != nil }),
              payload.total == 0 || !payload.items.isEmpty,
              payload.truncated == (payload.total > payload.items.count)
        else {
            return false
        }

        let first = payload.counts.failedCheck.addingReportingOverflow(
            payload.counts.failedStep
        )
        guard !first.overflow else { return false }
        let second = first.partialValue.addingReportingOverflow(payload.counts.blocker)
        guard !second.overflow else { return false }
        let total = second.partialValue.addingReportingOverflow(payload.counts.checkNotRun)
        return !total.overflow && total.partialValue == payload.total
    }

    var dashboardHeadline: String {
        switch self {
        case .loading: return "Checking recorded work"
        case .unavailable: return "Review status unavailable"
        case .clear: return "No recorded work needs review"
        case .focus(let item, _): return item.title
        case .inconsistent: return "Review details unavailable"
        }
    }

    /// The status caption: the queue's own count words (`4 in Attention`)
    /// from the payload, or a neutral count when an older daemon sent none.
    func dashboardStatus(queue: AttentionQueueCopy?) -> String {
        switch self {
        case .loading: return "Loading review projection"
        case .unavailable: return "Refresh to retry"
        case .clear:
            return PayloadAbsence.text(queue?.countText) ?? "0 in queue"
        case .focus(_, let total), .inconsistent(let total):
            return PayloadAbsence.text(queue?.countText) ?? "\(total) in queue"
        }
    }

    var dashboardStatusIsWarning: Bool {
        switch self {
        case .unavailable, .inconsistent: return true
        case .loading, .clear, .focus: return false
        }
    }
}

/// How one period bucket draws in the Dashboard chart (C48). Absence is a
/// named state with its own mark, never a zero-height bar or a dash.
enum DashboardPeriodBarState: Equatable {
    /// A charted value. `partial` marks a known-partial cost subtotal.
    case value(Double, partial: Bool)
    /// No usage recorded for the period: a 1pt hairline stub.
    case noUsage
    /// Usage recorded, but this series has no figure (unpriced cost, or
    /// fresh tokens not reported): a 3pt neutral stub.
    case unpriced

    var isPartial: Bool {
        if case .value(_, let partial) = self { return partial }
        return false
    }
}

/// The Dashboard chart's measures — the SAME keys, order, labels and resting
/// rule as the Usage chart (`UsageChartVocabulary`, from the payload).
enum DashboardUsageSeries: String, CaseIterable, Identifiable {
    case tokens
    case cost

    var id: Self { self }

    var chartSeries: UsageChartSeries { self == .tokens ? .tokens : .cost }

    /// True when the cube recorded no usage for the bucket.
    static func hasNoUsage(_ period: PeriodBucket) -> Bool {
        period.costState == "none_recorded"
            || period.usageAvailability == "unknown"
            || period.rows == 0
    }

    /// The charted value, or nil when the series has no complete-or-partial
    /// figure for this period. A missing figure is never charted as zero.
    func value(for period: PeriodBucket) -> Double? {
        switch self {
        case .tokens:
            guard !Self.hasNoUsage(period),
                  let tokens = period.freshTokens, tokens >= 0 else { return nil }
            return Double(tokens)
        case .cost:
            guard !Self.hasNoUsage(period) else { return nil }
            let figure = period.costComplete == true
                ? period.estimatedCostUsd
                : (period.estimatedCostUsd ?? period.knownAdditiveCostUsd)
            guard let cost = figure, cost.isFinite, cost >= 0 else { return nil }
            return cost
        }
    }

    func barState(for period: PeriodBucket) -> DashboardPeriodBarState {
        if let value = value(for: period) {
            // The cube's own cost state names a partial bucket (no Swift rule).
            return .value(value, partial: self == .cost && period.costState == "partial")
        }
        if Self.hasNoUsage(period) { return .noUsage }
        return .unpriced
    }

    func valueText(for period: PeriodBucket) -> String {
        switch barState(for: period) {
        case .noUsage:
            return PayloadAbsence.noUsage
        case .unpriced:
            switch self {
            case .tokens: return "fresh tokens \(PayloadAbsence.tokens)"
            case .cost:
                return usageCostAbsenceText(costState: period.costState, rows: period.rows, hasFigure: false)
                    ?? PayloadAbsence.unpriced
            }
        case .value(let value, let partial):
            switch self {
            case .tokens: return "\(UsageTotals.compact(value)) fresh tokens"
            case .cost:
                return partial
                    ? "\(period.costText) · \(PayloadAbsence.text(period.costTotalLabel) ?? PayloadAbsence.costLabel)"
                    : period.costText
            }
        }
    }

    /// The range readout. With the cube's own totals it renders the reducer
    /// grammar; otherwise it sums the charted buckets and names absence.
    func totalText(for periods: [PeriodBucket], totals: UsageBucket? = nil) -> String {
        let withUsage = periods.filter { !Self.hasNoUsage($0) }
        guard !withUsage.isEmpty else { return PayloadAbsence.noUsage }
        switch self {
        case .tokens:
            if let tokens = totals?.freshTokens, tokens >= 0 {
                return "\(UsageTotals.compact(tokens)) fresh tokens total"
            }
            let available = withUsage.compactMap { value(for: $0) }
            guard !available.isEmpty else { return "fresh tokens \(PayloadAbsence.tokens)" }
            let prefix = available.count == withUsage.count ? "" : "~"
            let total = available.reduce(0, +)
            guard total.isFinite else { return "fresh tokens total not charted" }
            return "\(prefix)\(UsageTotals.compact(total)) fresh tokens total"
        case .cost:
            // The cube's range figure with the reducer's own label (`total`
            // only when complete; `Partial subtotal · N of M usage records
            // unpriced` otherwise). Swift never sums buckets into a "total"
            // or appends the word itself (K105).
            guard let totals else { return Self.costTotalNotReported }
            let cost = UsageCostPresentation(bucket: totals)
            guard let figure = cost.figure else { return cost.absence }
            return "\(figure) \(cost.totalLabel)"
        }
    }

    static let costTotalNotReported = "cost total not reported"

    /// Subtitle naming the measure, the range and the basis — every word from
    /// the payload: the measure label (`usage_series`), the token basis
    /// (`token_basis_label`) and the cube's `cost_confidence_display` (C21).
    /// A cost subtitle opens with the unit (`USD`) since ticks carry no glyph.
    func subtitle(
        rangeDays: Int,
        periodPresentation: UsagePeriodPresentation,
        costBasis: String? = nil,
        vocabulary: UsageChartVocabulary = UsageChartVocabulary()
    ) -> String {
        let range = periodPresentation.historyRangeDescription(days: rangeDays)
        let label = vocabulary.label(for: chartSeries)
        switch self {
        case .tokens:
            return "\(label) · \(range) · \(PayloadAbsence.text(vocabulary.tokenBasis) ?? PayloadAbsence.costBasis)"
        case .cost:
            let unit = PayloadAbsence.text(vocabulary.costUnit) ?? PayloadAbsence.measure
            return "\(unit) · \(range) · \(PayloadAbsence.text(costBasis) ?? PayloadAbsence.costBasis)"
        }
    }

    /// The shared axis grammar (C44): `Fmt.axisTokens` / `Fmt.axisAmount`.
    /// Cost ticks are plain rounded scale values sharing the axis maximum's
    /// precision; they carry no cost glyph (K39).
    func axisText(for value: Double, scale: Double? = nil) -> String {
        switch self {
        case .tokens: return Fmt.axisTokens(value)
        case .cost: return Fmt.axisAmount(value, scale: scale)
        }
    }
}

func isActiveWorkStatus(_ status: String?) -> Bool {
    switch status {
    case "started", "checkpoint", "in_progress": return true
    default: return false
    }
}

/// A factual active-work digest. After 15 minutes it promotes one old recorded
/// activity timestamp for triage, but deliberately never calls it the oldest
/// overall—or the session stalled, abandoned, or blocked—because the bounded
/// glance projection cannot prove any of those states.
struct DashboardActiveWorkSignal: Equatable {
    let title: String
    let detail: String
    let promotesInactivity: Bool
    let hasConfirmedActiveWork: Bool

    init(
        sessions: [RecentSession],
        availability: DashboardSignalAvailability,
        now: Date = SnapshotMode.currentDate
    ) {
        switch availability {
        case .loading:
            title = "Checking active work"
            detail = "Waiting for the local glance projection."
            promotesInactivity = false
            hasConfirmedActiveWork = false
            return
        case .unavailable(let message):
            title = "Active work unavailable"
            detail = message
            promotesInactivity = false
            hasConfirmedActiveWork = false
            return
        case .connected:
            break
        }

        guard !sessions.isEmpty else {
            title = "No recent agent activity"
            detail = "Activity appears here once a session is observed."
            promotesInactivity = false
            hasConfirmedActiveWork = false
            return
        }

        let activeSessions = sessions.filter { isActiveWorkStatus($0.status) }
        let statuslessSessions = sessions.filter { $0.status == nil }

        if activeSessions.isEmpty, !statuslessSessions.isEmpty {
            title = "Work status unavailable"
            let validActivity = Self.validActivity(statuslessSessions, now: now)
            if let mostRecent = validActivity.min(by: { $0.1 < $1.1 }) {
                detail = "\(Self.sessionLabel(mostRecent.0)) · activity \(Self.elapsedText(mostRecent.1)) ago · \(statuslessSessions.count)/\(sessions.count) shown with no work status"
            } else {
                detail = "\(statuslessSessions.count)/\(sessions.count) shown with no work status · activity time unavailable"
            }
            promotesInactivity = false
            hasConfirmedActiveWork = false
            return
        }

        guard !activeSessions.isEmpty else {
            title = "No status-confirmed active work"
            detail = "\(sessions.count) recent session\(sessions.count == 1 ? "" : "s") shown."
            promotesInactivity = false
            hasConfirmedActiveWork = false
            return
        }

        let activeCount = activeSessions.count
        let validActivity = Self.validActivity(activeSessions, now: now)
        let unknownStatusSuffix = statuslessSessions.isEmpty
            ? ""
            : " · \(statuslessSessions.count) more shown without work status"

        if let oldestVisible = validActivity.max(by: { $0.1 < $1.1 }), oldestVisible.1 >= 15 * 60 {
            title = "One session last active \(Self.elapsedText(oldestVisible.1)) ago"
            detail = "\(Self.sessionLabel(oldestVisible.0)) · \(activeCount) recent active session\(activeCount == 1 ? "" : "s") shown\(unknownStatusSuffix)"
            promotesInactivity = true
            hasConfirmedActiveWork = true
            return
        }

        title = "\(activeCount) active session\(activeCount == 1 ? "" : "s") shown"
        if let mostRecent = validActivity.min(by: { $0.1 < $1.1 }) {
            detail = "\(Self.sessionLabel(mostRecent.0)) · activity \(Self.elapsedText(mostRecent.1)) ago\(unknownStatusSuffix)"
        } else {
            detail = "Activity time unavailable for the recorded session\(activeCount == 1 ? "" : "s")\(unknownStatusSuffix)."
        }
        promotesInactivity = false
        hasConfirmedActiveWork = true
    }

    private static func validActivity(
        _ sessions: [RecentSession],
        now: Date
    ) -> [(RecentSession, TimeInterval)] {
        sessions.compactMap { session -> (RecentSession, TimeInterval)? in
            guard let lastActivityAt = session.lastActivityAt, lastActivityAt > 0 else { return nil }
            let elapsed = now.timeIntervalSince1970 - lastActivityAt
            guard elapsed >= 0, elapsed.isFinite else { return nil }
            return (session, elapsed)
        }
    }

    private static func sessionLabel(_ session: RecentSession) -> String {
        if let title = session.title?.trimmingCharacters(in: .whitespacesAndNewlines), !title.isEmpty {
            return title
        }
        return "\(session.client) · \(session.shortSessionId)"
    }

    private static func elapsedText(_ seconds: TimeInterval) -> String {
        let total = Int(seconds)
        if total < 60 { return "\(total)s" }
        if total < 3_600 { return "\(total / 60)m" }
        if total < 86_400 { return "\(total / 3_600)h" }
        return "\(total / 86_400)d"
    }
}

/// Summarizes the latest two dated fresh-token buckets without forecasting or
/// assigning causes. A current day/week is labeled as partial and shown as an
/// absolute value; completed periods may use bounded comparison copy. Compact
/// values disclose scale while full period labels keep the interval explicit.
struct DashboardUsagePulse: Equatable {
    enum State: Equatable {
        case loading
        case unavailable
        case insufficient
        case ready
    }

    let state: State
    let title: String
    let detail: String

    init(
        periods: [PeriodBucket]?,
        isLoaded: Bool,
        rangeDays: Int,
        error: String?,
        tokenBasis: String? = nil,
        now: Date = SnapshotMode.currentDate,
        // No default. `TimeZone.current` used to be the default here, which made
        // the rendered period label depend on the zone of whichever process did
        // the rendering — the single largest source of false visual-snapshot
        // failures on a non-canonical host. Callers must say which zone they
        // mean; views read `\.timeZone` from the environment.
        timeZone: TimeZone
    ) {
        let basis = PayloadAbsence.text(tokenBasis) ?? PayloadAbsence.costBasis
        if let error {
            state = .unavailable
            title = "Usage comparison unavailable"
            detail = error
            return
        }
        guard isLoaded else {
            state = .loading
            title = "Checking usage change"
            detail = "Waiting for recorded usage buckets."
            return
        }
        guard let periods else {
            state = .insufficient
            title = "Usage history not reported"
            detail = "The loaded usage summary has no period history."
            return
        }

        guard !periods.contains(where: { period in
            guard let label = period.period else { return true }
            return label != "unknown" && !Self.isDateLabel(label)
        }) else {
            state = .insufficient
            title = "Usage comparison ambiguous"
            detail = "A usage bucket has an unsupported period label."
            return
        }

        guard !periods.contains(where: { ($0.freshTokens ?? 0) < 0 }) else {
            state = .insufficient
            title = "Usage comparison incomplete"
            detail = "A fresh-token bucket reported an invalid negative value."
            return
        }

        let dated = periods.compactMap { period -> DatedBucket? in
            guard let label = period.period, Self.isDateLabel(label) else { return nil }
            return DatedBucket(
                label: label,
                tokens: period.freshTokens,
                noUsage: DashboardUsageSeries.hasNoUsage(period)
            )
        }.sorted { $0.label < $1.label }

        guard Set(dated.map(\.label)).count == dated.count else {
            state = .insufficient
            title = "Usage comparison ambiguous"
            detail = "Multiple fresh-token buckets share a date."
            return
        }

        guard dated.count >= 2 else {
            state = .insufficient
            title = "Usage comparison needs history"
            detail = "Two dated fresh-token buckets are required."
            return
        }

        let previous = dated[dated.count - 2]
        let latest = dated[dated.count - 1]
        guard previous.hasComparableValue, latest.hasComparableValue else {
            state = .insufficient
            title = "Usage comparison incomplete"
            detail = "The latest two dated buckets need fresh-token values."
            return
        }

        let weekly = rangeDays >= 90
        let currentPeriod = Self.currentPeriodLabel(now: now, weekly: weekly, timeZone: timeZone)
        let priorPeriod = Self.currentPeriodLabel(
            now: now.addingTimeInterval(weekly ? -7 * 86_400 : -86_400),
            weekly: weekly,
            timeZone: timeZone
        )
        guard latest.label <= currentPeriod else {
            state = .insufficient
            title = "Usage history is ahead of local time"
            detail = "Latest recorded period: \(latest.label)."
            return
        }
        state = .ready
        if latest.label == currentPeriod {
            // The reducer's availability is the primary state (C47): a period
            // with no recorded usage is named, never shown as a numeric 0.
            if latest.noUsage {
                title = weekly ? "No usage recorded this week" : "No usage recorded today"
            } else {
                title = "\(weekly ? "This week" : "Today") so far · \(Self.tokenPhrase(latest.tokens ?? 0))"
            }
            detail = Self.detailWithBasis([previous], weekly: weekly, priorPeriod: priorPeriod, basis: basis)
            return
        }

        let previousTokens = previous.tokens ?? 0
        let latestTokens = latest.tokens ?? 0
        if previous.noUsage && latest.noUsage {
            title = "No usage recorded in either period"
        } else if latest.noUsage {
            title = "No usage recorded in the latest period"
        } else if previous.noUsage {
            title = "Fresh tokens recorded after a period with no usage"
        } else if previousTokens == 0 {
            title = latestTokens == 0
                ? "Fresh tokens unchanged at 0"
                : "Fresh tokens rose from 0"
        } else {
            let change = ((Double(latestTokens) - Double(previousTokens)) / Double(previousTokens)) * 100
            let rounded = abs(change).rounded()
            if latestTokens == previousTokens {
                title = "Fresh tokens unchanged"
            } else if rounded < 1 {
                title = "Fresh tokens roughly unchanged"
            } else if change > 999 {
                title = "Fresh tokens >999% higher"
            } else {
                title = "Fresh tokens \(String(format: "%.0f", rounded))% \(change > 0 ? "higher" : "lower")"
            }
        }
        detail = Self.detailWithBasis([latest, previous], weekly: weekly, priorPeriod: priorPeriod, basis: basis)
    }

    /// Joins bucket descriptions and attaches the token basis ONLY to recorded
    /// figures — an absence ("no usage recorded") never wears a basis. With
    /// every bucket recorded the basis closes the line once; with one recorded
    /// figure beside an absence, the basis rides directly on that figure.
    private static func detailWithBasis(
        _ buckets: [DatedBucket],
        weekly: Bool,
        priorPeriod: String,
        basis: String
    ) -> String {
        let recorded = buckets.filter { !$0.noUsage }
        let parts = buckets.map { bucketDescription($0, weekly: weekly, priorPeriod: priorPeriod) }
        if recorded.isEmpty { return parts.joined(separator: " · ") }
        if recorded.count == buckets.count { return (parts + [basis]).joined(separator: " · ") }
        return zip(buckets, parts).map { bucket, part in
            bucket.noUsage ? part : "\(part) · \(basis)"
        }.joined(separator: " · ")
    }

    private struct DatedBucket {
        let label: String
        let tokens: Int?
        let noUsage: Bool

        /// A no-usage bucket is a named state and still comparable; a bucket
        /// with usage needs a valid fresh-token count.
        var hasComparableValue: Bool {
            if noUsage { return true }
            guard let tokens else { return false }
            return tokens >= 0
        }
    }

    private static func tokenPhrase(_ tokens: Int) -> String {
        "\(UsageTotals.compact(tokens)) fresh tokens"
    }

    /// Names the period and the unit: `yesterday 121.1M fresh tokens`,
    /// `last week no usage recorded`, `121.1M fresh tokens on 2026-09-13`.
    private static func bucketDescription(_ bucket: DatedBucket, weekly: Bool, priorPeriod: String) -> String {
        let value = bucket.noUsage ? PayloadAbsence.noUsage : tokenPhrase(bucket.tokens ?? 0)
        if bucket.label == priorPeriod {
            return "\(weekly ? "last week" : "yesterday") \(value)"
        }
        return "\(value) \(weekly ? "in week of \(bucket.label)" : "on \(bucket.label)")"
    }

    private static func isDateLabel(_ label: String) -> Bool {
        let pieces = label.split(separator: "-", omittingEmptySubsequences: false)
        guard pieces.count == 3,
              pieces[0].count == 4, pieces[1].count == 2, pieces[2].count == 2,
              let year = Int(pieces[0]), let month = Int(pieces[1]), let day = Int(pieces[2])
        else { return false }

        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        guard let date = calendar.date(from: DateComponents(year: year, month: month, day: day)) else {
            return false
        }
        let resolved = calendar.dateComponents([.year, .month, .day], from: date)
        return resolved.year == year && resolved.month == month && resolved.day == day
    }

    private static func currentPeriodLabel(now: Date, weekly: Bool, timeZone: TimeZone) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        let date: Date
        if weekly {
            let weekday = calendar.component(.weekday, from: now)
            let daysSinceMonday = (weekday + 5) % 7
            date = calendar.date(byAdding: .day, value: -daysSinceMonday, to: now) ?? now
        } else {
            date = now
        }
        let components = calendar.dateComponents([.year, .month, .day], from: date)
        return String(
            format: "%04d-%02d-%02d",
            components.year ?? 0,
            components.month ?? 0,
            components.day ?? 0
        )
    }
}

struct DashboardPane: View {
    @Environment(DashboardStore.self) var dashboard
    @Environment(GlanceState.self) var glance
    @Environment(AppSelection.self) var selection
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    /// The zone the usage-period comparison is made in. The live app inherits
    /// the system zone through this environment value, exactly as before; the
    /// snapshot renderers set it to GMT, so the rendered period label no longer
    /// depends on which zone the rendering process happens to be in.
    @Environment(\.timeZone) private var timeZone

    private var presentedError: String? {
        dashboard.errorText ?? dashboard.receiptListError
    }

    private var recentWork: [DashboardWorkItem] {
        dashboard.receiptTasks.prefix(3).map(DashboardWorkItem.init)
    }

    private var liveLimits: [LimitEntry] {
        guard case .connected(let snapshot) = glance.phase else { return [] }
        return snapshot.glance.limits.filter { $0.stale != true }
    }

    /// The glance 7-day per-client usage slice keeps recording clients in the
    /// capacity input even when they do not report provider limits.
    private var glanceUsageByClient: [GlanceClientUsage] {
        guard case .connected(let snapshot) = glance.phase else { return [] }
        return snapshot.glance.usage.byClient ?? []
    }

    /// Clients whose 7-day reading is available only as stale data. A live
    /// short-window stream must not erase that useful absence distinction.
    private var staleLimitClients: Set<String> {
        guard case .connected(let snapshot) = glance.phase else { return [] }
        return staleSevenDayLimitClients(in: snapshot.glance.limits)
    }

    private var recentSessions: [RecentSession] {
        guard case .connected(let snapshot) = glance.phase else { return [] }
        return snapshot.glance.recentSessions
    }

    private var glanceAvailability: DashboardSignalAvailability {
        switch glance.phase {
        case .connected: return .connected
        case .connecting: return .loading
        case .disconnected(let message), .incompatible(let message): return .unavailable(message)
        }
    }

    /// The reducer's headline window (`headline_limit_key`) — the same limit
    /// the menu hero and the TUI lead with.
    private var capacityHeadline: DashboardCapacityHeadline? {
        guard case .connected(let snapshot) = glance.phase,
              let headline = snapshot.glance.headlineLimit else { return nil }
        return DashboardCapacityHeadline(entry: headline.entry, window: headline.window)
    }

    private var planRows: [DashboardAgentPlanRow] {
        DashboardAgentPlanRow.rows(
            limits: liveLimits,
            staleClients: staleLimitClients,
            planClients: dashboard.planClients,
            usage: glanceUsageByClient
        )
    }

    var body: some View {
        ScrollBox {
            VStack(alignment: .leading, spacing: Space.l) {
                // The Dashboard reads the short attention PREVIEW, never the
                // Work pane's loaded queue: the two now have their own state,
                // so a refresh cannot trim one to fit the other (K87).
                DashboardShiftBriefHeader(
                    payload: dashboard.dashboardAttention,
                    error: dashboard.dashboardAttentionError
                )

                splitRow {
                    DashboardAttentionBriefCard(
                        payload: dashboard.dashboardAttention,
                        error: dashboard.dashboardAttentionError
                    ) { destination in
                        selection.open(destination)
                    }
                } right: {
                    DashboardSignalRail(
                        sessions: recentSessions,
                        planRows: planRows,
                        headline: capacityHeadline,
                        availability: glanceAvailability,
                        usagePulse: DashboardUsagePulse(
                            periods: dashboard.usage?.byPeriod,
                            isLoaded: dashboard.usage != nil,
                            rangeDays: dashboard.usageDays,
                            error: dashboard.errorText,
                            tokenBasis: dashboard.usage?.tokenBasisLabel,
                            timeZone: timeZone
                        ),
                        ingestion: dashboard.ingestion,
                        ingestionError: dashboard.ingestionError
                    ) { destination in
                        selection.open(destination)
                    }
                }

                RecentWorkCard(
                    items: recentWork,
                    totalCount: dashboard.totalReceiptTasks ?? dashboard.receiptTasks.count,
                    fieldLabels: dashboard.receiptFieldLabels ?? ReceiptFieldLabels()
                ) { destination in
                    selection.open(destination)
                }

                if let usage = dashboard.usage,
                   let periods = usage.byPeriod,
                   periods.count > 1
                {
                    DashboardUsageChart(
                        periods: periods,
                        totals: usage.totals,
                        rangeDays: dashboard.usageDays,
                        periodPresentation: UsagePeriodPresentation(usage: usage),
                        vocabulary: UsageChartVocabulary(usage: usage)
                    )
                }
            }
            .padding(Space.gutter)
            .pageFrame()
        }
        .overlay(alignment: .bottom) {
            if let error = presentedError {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .workFont(.caption)
                    .foregroundStyle(Theme.coral)
                    .padding(Space.s)
                    .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous))
                    .padding(.bottom, 10)
                    .id(error)
                    .transition(.opacity)
            }
        }
        .animation(
            reduceMotion ? Motion.reducedCrossfade : Motion.phaseCrossfade,
            value: presentedError
        )
    }

    private func splitRow<Left: View, Right: View>(
        @ViewBuilder left: () -> Left,
        @ViewBuilder right: () -> Right
    ) -> some View {
        // The split is chosen by the width the pane actually offers, not by
        // the cards' single-line ideal widths: a long reducer sentence in the
        // signal rail wraps inside its column instead of silently collapsing
        // the row into a stack that pushes Recent work out of the first
        // viewport.
        DashboardSplitLayout(leftFraction: 7 / 12, spacing: Space.l, minimumSplitWidth: 820) {
            left()
            right()
        }
    }

}

/// Two columns at `leftFraction` when the proposed width reaches
/// `minimumSplitWidth`; otherwise the two cards stack at full width.
private struct DashboardSplitLayout: Layout {
    let leftFraction: CGFloat
    let spacing: CGFloat
    let minimumSplitWidth: CGFloat

    private func splits(_ width: CGFloat?) -> Bool {
        guard let width else { return false }
        return width >= minimumSplitWidth
    }

    func sizeThatFits(
        proposal: ProposedViewSize,
        subviews: Subviews,
        cache: inout ()
    ) -> CGSize {
        guard subviews.count == 2 else { return .zero }
        guard splits(proposal.width), let width = proposal.width else {
            // Stacked: each card measures at the full offered width. Heights
            // are intrinsic so flexible cards never consume the viewport.
            let top = subviews[0].sizeThatFits(.init(width: proposal.width, height: nil))
            let bottom = subviews[1].sizeThatFits(.init(width: proposal.width, height: nil))
            return CGSize(
                width: proposal.width ?? max(top.width, bottom.width),
                height: top.height + spacing + bottom.height
            )
        }
        let usableWidth = max(0, width - spacing)
        let leftWidth = usableWidth * leftFraction
        let rightWidth = usableWidth - leftWidth
        // Measure intrinsic heights at the final column widths. Forwarding the
        // viewport height here lets flexible cards greedily consume it and
        // creates arbitrary empty space between rows.
        let leftSize = subviews[0].sizeThatFits(.init(width: leftWidth, height: nil))
        let rightSize = subviews[1].sizeThatFits(.init(width: rightWidth, height: nil))
        return CGSize(width: width, height: max(leftSize.height, rightSize.height))
    }

    func placeSubviews(
        in bounds: CGRect,
        proposal: ProposedViewSize,
        subviews: Subviews,
        cache: inout ()
    ) {
        guard subviews.count == 2 else { return }
        guard splits(bounds.width) else {
            let topHeight = subviews[0].sizeThatFits(.init(width: bounds.width, height: nil)).height
            let bottomHeight = subviews[1].sizeThatFits(.init(width: bounds.width, height: nil)).height
            subviews[0].place(
                at: bounds.origin,
                proposal: .init(width: bounds.width, height: topHeight)
            )
            subviews[1].place(
                at: CGPoint(x: bounds.minX, y: bounds.minY + topHeight + spacing),
                proposal: .init(width: bounds.width, height: bottomHeight)
            )
            return
        }
        let usableWidth = max(0, bounds.width - spacing)
        let leftWidth = usableWidth * leftFraction
        let rightWidth = usableWidth - leftWidth
        subviews[0].place(
            at: bounds.origin,
            proposal: .init(width: leftWidth, height: bounds.height)
        )
        subviews[1].place(
            at: CGPoint(x: bounds.minX + leftWidth + spacing, y: bounds.minY),
            proposal: .init(width: rightWidth, height: bounds.height)
        )
    }
}

private struct DashboardShiftBriefHeader: View {
    let payload: V1AttentionPayload?
    let error: String?

    private var presentation: DashboardAttentionPresentation {
        DashboardAttentionPresentation(payload: payload, error: error)
    }

    var body: some View {
        // The status caption stacks under the title before the title would
        // truncate (C83): the row form is chosen only when both fit at their
        // ideal widths.
        ViewThatFits(in: .horizontal) {
            HStack(alignment: .lastTextBaseline, spacing: Space.xl) {
                titleBlock
                Spacer(minLength: Space.m)
                statusText
                    .multilineTextAlignment(.trailing)
            }
            VStack(alignment: .leading, spacing: Space.s) {
                titleBlock
                statusText
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("dashboard.shift-brief.header")
    }

    private var titleBlock: some View {
        VStack(alignment: .leading, spacing: 5) {
            CapsLabel(text: "Shift brief")
            Text(presentation.dashboardHeadline)
                .workFont(.titlePage)
                .tracking(Type.titlePageTracking)
                .foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var statusText: some View {
        Text(presentation.dashboardStatus(queue: payload?.queue))
            .workFont(.dataSmall)
            .foregroundStyle(presentation.dashboardStatusIsWarning ? Theme.amber : Theme.muted)
    }
}

private struct DashboardAttentionBriefCard: View {
    let payload: V1AttentionPayload?
    let error: String?
    let open: (DashboardDestination) -> Void
    @State private var copyFeedback = DashboardCopyFeedback.idle
    @State private var copyFeedbackToken: UUID?

    private var presentation: DashboardAttentionPresentation {
        DashboardAttentionPresentation(payload: payload, error: error)
    }

    /// The stem is a FILLED mark, so it takes fill weights (K47).
    private var tint: Color {
        switch presentation {
        // One decision-to-tint lookup (C26): the stem wears the same class as
        // the decision badge, so a blocked task is never amber here and coral
        // on its badge.
        case .focus(let focus, _): return DecisionTintClass.forKey(focus.decisionKey).fill
        // A clear review projection is not externally verified evidence, so it
        // is never green (C27).
        case .clear: return Theme.ink
        case .loading: return Theme.muted
        case .unavailable, .inconsistent: return Theme.amberFill
        }
    }

    var body: some View {
        Card(padding: 0, fillsHeight: true) {
            HStack(spacing: 0) {
                Rectangle()
                    .fill(tint)
                    .frame(width: 5)
                    .accessibilityHidden(true)
                // Vertical padding matches the signal rail's 16pt rhythm so
                // the brief row (whose height the taller card sets) leaves
                // Recent work visible in the minimum first viewport.
                content
                    .padding(.horizontal, Space.xl)
                    .padding(.vertical, Space.l)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            }
        }
        // A named group, not an anonymous one: VoiceOver announces what the
        // card IS on entry, using its own visible caption (K121).
        .accessibilityElement(children: .contain)
        .accessibilityLabel(Self.caption)
        .accessibilityIdentifier("dashboard.shift-brief.attention")
    }

    /// The card's visible caption, and the name of its accessibility group.
    static let caption = "Primary attention"

    @ViewBuilder
    private var content: some View {
        switch presentation {
        case .clear:
            DashboardBriefEmptyState()
        case .focus(let focus, let total):
            focusContent(total: total, focus: focus)
        case .inconsistent:
            DashboardBriefUnavailableState(
                title: "Attention details unavailable",
                message: "The review count loaded, but its leading recorded reason did not. Refresh before acting."
            )
        case .unavailable(let error):
            DashboardBriefUnavailableState(
                title: "Review status unavailable",
                message: "The recorder didn't return the review projection. Refresh before acting on this brief.",
                rawError: error
            )
        case .loading:
            HStack(spacing: Space.m) {
                ProgressView().controlSize(.small).tint(Theme.muted)
                VStack(alignment: .leading, spacing: 3) {
                    Text("Checking recorded work…")
                        .workFont(.titleSection)
                        .foregroundStyle(Theme.ink)
                    Text("Loading the complete review projection; no clear-state claim is shown yet.")
                        .workFont(.body)
                        .foregroundStyle(Theme.muted)
                }
            }
            .frame(maxHeight: .infinity, alignment: .center)
        }
    }

    private func focusContent(total: Int, focus: DashboardAttentionItem) -> some View {
        let brief = DashboardActionBrief(focus: focus)
        let copySucceeded = copyFeedback == .copied(brief.text)
        let copyFailed = copyFeedback == .failed(brief.text)
        return VStack(alignment: .leading, spacing: Space.m) {
            HStack(spacing: Space.s) {
                CapsLabel(text: Self.caption)
                Text("1 OF \(total)")
                    .workFont(.dataSmallSemibold)
                    .foregroundStyle(Theme.muted)
                    .padding(.horizontal, 7)
                    .frame(minHeight: 21)
                    .background(Theme.tintNeutral, in: Capsule())
                Spacer(minLength: Space.s)
                if total > 1 {
                    Button(PayloadAbsence.text(payload?.queue?.openAction) ?? "Open queue") { open(.reviewQueue) }
                        .workFont(.captionSemibold)
                        .foregroundStyle(Theme.accent)
                        .buttonStyle(QuietButtonStyle())
                        .accessibilityIdentifier("dashboard.shift-brief.view-queue")
                }
            }

            // Decision first (C39): the reducer's decision word and verdict
            // headline, before any narrative.
            VStack(alignment: .leading, spacing: 7) {
                HStack(alignment: .center, spacing: Space.s) {
                    DecisionBadge(
                        key: focus.decisionKey,
                        label: focus.decisionLabel,
                        help: focus.decisionStatement
                    )
                    if let headline = focus.verdictHeadline {
                        Text(headline)
                            .workFont(.rowLabel)
                            .foregroundStyle(Theme.ink)
                            .fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                // One meta order everywhere: client, then project (K21).
                let context = workMetaLine(client: focus.client, project: focus.project)
                if !context.isEmpty {
                    Text(context)
                        .workFont(.dataSmall)
                        .foregroundStyle(Theme.muted)
                }
            }

            // The shared attention block (K21): the reducer's reason noun as
            // the eyebrow (C61), then the agent's sentence in body ink, then
            // the identity of the evidence it rests on (C40). The compact next
            // step keeps this card inside the minimum window's first viewport.
            AttentionBlockBody(
                reasonLabel: focus.reasonLabel,
                summary: focus.summary,
                label: focus.label,
                nextStep: focus.nextStep,
                variant: .dashboard
            )

            DashboardProofline(focus: focus)

            if SnapshotMode.rendersStaticControls {
                DashboardStaticBriefActions(copyTitle: brief.buttonTitle)
            } else {
            HStack(spacing: Space.s) {
                Button {
                    open(.attentionTask(focus.id))
                } label: {
                    Label("Review evidence", systemImage: "doc.text.magnifyingglass")
                }
                .buttonStyle(PrimaryButtonStyle(height: Metrics.buttonHCompact))
                .accessibilityHint("Opens this task in Work")
                .accessibilityIdentifier("dashboard.shift-brief.review-evidence")

                Button {
                    let feedbackToken = UUID()
                    copyFeedbackToken = feedbackToken
                    copyFeedback.record(
                        succeeded: DashboardClipboard.copy(brief.text),
                        text: brief.text
                    )
                    Task { @MainActor in
                        try? await Task.sleep(for: .seconds(2))
                        guard copyFeedbackToken == feedbackToken else { return }
                        copyFeedback.clear()
                        copyFeedbackToken = nil
                    }
                } label: {
                    ZStack {
                        // Reserve the idle label's full width so copy feedback
                        // cannot shove the primary action sideways.
                        Label(brief.buttonTitle, systemImage: "doc.on.doc")
                            .hidden()
                            .accessibilityHidden(true)
                        Label(
                            copySucceeded ? "Copied" : (copyFailed ? "Copy failed" : brief.buttonTitle),
                            systemImage: copySucceeded ? "checkmark" : "doc.on.doc"
                        )
                    }
                    .workFont(.captionSemibold)
                }
                .buttonStyle(.bordered)
                .tint(copyFailed ? Theme.coral : Theme.accent)
                .accessibilityLabel(
                    copySucceeded
                        ? brief.copiedAccessibilityLabel
                        : (copyFailed ? brief.failedAccessibilityLabel : brief.buttonTitle)
                )
                .accessibilityHint("Copies recorded facts only; it does not resume or rerun an agent")
                .accessibilityIdentifier("dashboard.shift-brief.copy-action-brief")
            }
            }
        }
        .accessibilityElement(children: .contain)
    }
}

/// Snapshot-only stand-ins for the two Shift-Brief action buttons.
///
/// SwiftUI's offscreen `ImageRenderer` only paints native `.borderedProminent`
/// / `.bordered` `Button` chrome (and the SF Symbols inside them) on the pinned
/// golden toolchain; on other macOS versions those controls collapse to an
/// unrendered fill with dropped labels. The README `--snapshot` path sets
/// `SnapshotMode.rendersStaticControls`, so its docs renders draw these faithful
/// primitives — matching the live app's real buttons — while the live app and
/// the pinned fixture renderers keep the interactive controls untouched.
private struct DashboardStaticBriefActions: View {
    let copyTitle: String

    var body: some View {
        HStack(spacing: Space.s) {
            // Same chrome as the live PrimaryButtonStyle: onAccent label on
            // the accent fill, radius 4 (C36).
            Label("Review evidence", systemImage: "doc.text.magnifyingglass")
                .modifier(PrimaryButtonChrome(height: Metrics.buttonHCompact))
            Label(copyTitle, systemImage: "doc.on.doc")
                .workFont(.captionSemibold)
                .foregroundStyle(Theme.accent)
                .padding(.horizontal, 13)
                .workScaledMinFrame(height: Metrics.buttonHCompact)
                .background(Theme.tintAccent, in: RoundedRectangle(cornerRadius: Metrics.radius))
        }
        .accessibilityHidden(true)
    }
}

private struct DashboardProofline: View {
    let focus: DashboardAttentionItem

    private var observed: String { focus.recency ?? DashboardVocabulary.timeNotReported }
    private var provenance: String { focus.sourceLabel ?? DashboardVocabulary.sourceNotReported }

    var body: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 0) {
                fact(label: "Observed", value: observed)
                proofRule
                fact(label: "Provenance", value: provenance)
            }
            VStack(alignment: .leading, spacing: Space.s) {
                fact(label: "Observed", value: observed)
                fact(label: "Provenance", value: provenance)
            }
        }
        .padding(.vertical, Space.s)
        .overlay(alignment: .top) { Divider().overlay(Theme.hairline) }
        .overlay(alignment: .bottom) { Divider().overlay(Theme.hairline) }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Observed: \(observed). Provenance: \(provenance).")
    }

    private func fact(label: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            CapsLabel(text: label)
            Text(value)
                .workFont(.dataSmallSemibold)
                .foregroundStyle(Theme.ink)
                .lineLimit(1)
                .fixedSize(horizontal: true, vertical: false)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var proofRule: some View {
        Rectangle().fill(Theme.hairline).frame(width: 1, height: 35).padding(.horizontal, Space.m)
    }
}

private struct DashboardBriefEmptyState: View {
    var body: some View {
        VStack(alignment: .leading, spacing: Space.l) {
            HStack(spacing: Space.s) {
                // Clear is ink/muted, never green (C27): an empty review queue
                // is not externally verified evidence.
                Image(systemName: "checkmark.seal")
                    .workFont(.iconLarge)
                    .foregroundStyle(Theme.muted)
                    .accessibilityHidden(true)
                CapsLabel(text: "Complete review projection")
            }
            Text("No recorded work needs review.")
                .workFont(.titleSection)
                .tracking(Type.titleSectionTracking)
                .foregroundStyle(Theme.ink)
            Text("No current failed check, failed step, or unresolved blocker was found across the complete attention projection.")
                .workFont(.body)
                .foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
    }
}

private struct DashboardBriefUnavailableState: View {
    let title: String
    let message: String
    /// The raw developer error, if there is one. It rides behind the same
    /// disclosure every other state uses instead of being the message (K53).
    var rawError: String? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: Space.l) {
            Image(systemName: "questionmark.diamond.fill")
                .workFont(.iconLarge)
                .foregroundStyle(Theme.amber)
            Text(title)
                .workFont(.titleSection)
                .tracking(Type.titleSectionTracking)
                .foregroundStyle(Theme.ink)
            Text(message)
                .workFont(.body)
                .foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
            if let rawError {
                DisclosureGroup(EmptyStateView.detailsLabel) {
                    Text(rawError)
                        .workFont(.caption)
                        .foregroundStyle(Theme.muted)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, Space.xs)
                }
                .workFont(.caption)
                .foregroundStyle(Theme.muted)
                .frame(maxWidth: Metrics.readingMeasure, alignment: .leading)
                .accessibilityIdentifier("dashboard.brief.error-details")
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
    }
}

enum DashboardIngestionTone: Equatable {
    case muted
    case healthy
    case warning
}

struct DashboardIngestionPresentation: Equatable {
    let title: String
    let detail: String
    /// The reducer's rail-length twin, when the payload carries one (K69).
    var detailCompact: String? = nil
    let tone: DashboardIngestionTone

    init(title: String, detail: String, detailCompact: String? = nil, tone: DashboardIngestionTone) {
        self.title = title
        self.detail = detail
        self.detailCompact = detailCompact
        self.tone = tone
    }

    init(snapshot: V1IngestionSnapshot?, error: String?) {
        if error != nil {
            // The rail is one line: it states the CAUSE. The raw error stays
            // available where this row leads — the Sources pane keeps it
            // behind its "Error details" disclosure (K53).
            self.init(
                title: "Source status unavailable",
                detail: "The recorder didn't return source health.",
                detailCompact: "The recorder didn't return source health.",
                tone: .warning
            )
            return
        }
        guard let snapshot else {
            self.init(
                title: "Checking source status",
                detail: "Waiting for the current ingestion record.",
                tone: .muted
            )
            return
        }

        let issueCount = snapshot.issues?.count ?? 0
        let detail: String
        if issueCount > 0 {
            detail = "\(issueCount) recorded issue\(issueCount == 1 ? "" : "s") · inspect Sources"
        } else if let last = agoText(snapshot.lastSuccessAt) {
            detail = "Last successful ingest \(last)"
        } else {
            // The reducer's state sentence is a fact; never an instruction.
            detail = PayloadAbsence.text(snapshot.stateDetail) ?? "ingestion state detail not reported"
        }

        // Title and detail are the reducer's state copy (C79); a state key is
        // never capitalized into a title, and a daemon that predates the copy
        // gets a named absence. Amber is rationed to a degraded source; an
        // unknown or unrecorded import history is a neutral fact, not a
        // threshold.
        let title = PayloadAbsence.text(snapshot.stateTitle) ?? "Source status not reported"
        // Only the state sentence has a reducer-written short form; a detail
        // this initializer composed (issue count, last ingest) is already short.
        let compact = issueCount > 0 || agoText(snapshot.lastSuccessAt) != nil
            ? nil
            : PayloadAbsence.text(snapshot.stateDetailCompact)
        switch snapshot.state {
        case "healthy":
            self.init(title: title, detail: detail, detailCompact: compact, tone: .healthy)
        case "degraded":
            self.init(title: title, detail: detail, detailCompact: compact, tone: .warning)
        default:
            self.init(title: title, detail: detail, detailCompact: compact, tone: .muted)
        }
    }
}

/// One signal row's detail, as the ordered payload phrases that compose it
/// (K69).
///
/// The rail gives every row one line, so a long detail was simply cut with an
/// ellipsis — and what fell off the end was the part that mattered most: the
/// CAPACITY row lost "plan share unavailable · won't calibrate at current
/// ratio", two NAMED ABSENCES a sighted reviewer could then read nowhere on
/// the page. The rule here is the same one the receipt follows: an absence is
/// a named state, so it leads the short form and is never dropped to make
/// room for measured context.
struct DashboardSignalDetail: Equatable {
    /// Phrases naming something the payload could not report. Always shown.
    var absences: [String] = []
    /// Measured context, most important first. Trimmed from the tail.
    var context: [String] = []
    /// The reducer's OWN short form, when the payload carries one. A prose
    /// sentence has no phrase to drop, so the short form has to be written
    /// where the long one is — in Python — not abbreviated here.
    var payloadCompact: String? = nil

    /// Roughly one rail line at the default reading size.
    static let compactBudget = 60

    static let separator = " · "

    /// A detail that is one sentence, with nothing to re-order.
    static func sentence(_ text: String, compact: String? = nil) -> DashboardSignalDetail {
        DashboardSignalDetail(context: [text], payloadCompact: compact)
    }

    var full: String {
        (absences + context).joined(separator: Self.separator)
    }

    /// The reducer's short form when there is one; otherwise absences first,
    /// then as much context as fits the budget. Never empty while the full
    /// detail has words.
    var compact: String {
        if let payloadCompact, !payloadCompact.isEmpty { return payloadCompact }
        var parts = absences
        var length = parts.joined(separator: Self.separator).count
        for phrase in context {
            let added = (parts.isEmpty ? 0 : Self.separator.count) + phrase.count
            if !parts.isEmpty, length + added > Self.compactBudget { break }
            parts.append(phrase)
            length += added
        }
        return parts.isEmpty ? full : parts.joined(separator: Self.separator)
    }
}

private struct DashboardSignalRail: View {
    let sessions: [RecentSession]
    let planRows: [DashboardAgentPlanRow]
    var headline: DashboardCapacityHeadline? = nil
    let availability: DashboardSignalAvailability
    let usagePulse: DashboardUsagePulse
    let ingestion: V1IngestionSnapshot?
    let ingestionError: String?
    let open: (DashboardDestination) -> Void


    private var rowsWithoutValidCapacity: [DashboardAgentPlanRow] {
        planRows.filter { $0.usedPercent == nil }
    }

    private var active: DashboardActiveWorkSignal {
        DashboardActiveWorkSignal(sessions: sessions, availability: availability)
    }

    private var ingestionPresentation: DashboardIngestionPresentation {
        DashboardIngestionPresentation(snapshot: ingestion, error: ingestionError)
    }

    var body: some View {
        Card(padding: 0, fillsHeight: true) {
            VStack(spacing: 0) {
                DashboardCardHeader("Signals")
                Divider().overlay(Theme.hairline)
                DashboardSignalRow(
                    eyebrow: "WORKING NOW",
                    title: active.title,
                    detail: .sentence(active.detail),
                    stem: active.promotesInactivity
                        ? Theme.amberFill
                        : (active.hasConfirmedActiveWork ? Theme.accent : Theme.muted),
                    destination: .work,
                    action: { open(.work) }
                )
                Divider().overlay(Theme.hairline).padding(.leading, Space.l)
                DashboardSignalRow(
                    eyebrow: "CAPACITY",
                    title: capacityTitle,
                    detail: capacityDetail,
                    stem: capacityTint,
                    destination: .limits,
                    action: { open(.limits) }
                )
                Divider().overlay(Theme.hairline).padding(.leading, Space.l)
                DashboardSignalRow(
                    eyebrow: "USAGE CHANGE",
                    title: usagePulse.title,
                    detail: .sentence(usagePulse.detail),
                    // A usage change is data, not a control: a neutral rule
                    // stem, never cobalt (K04).
                    stem: usagePulse.state == .ready ? Theme.rule : Theme.muted,
                    destination: .limits,
                    action: { open(.limits) }
                )
                Divider().overlay(Theme.hairline).padding(.leading, Space.l)
                DashboardSignalRow(
                    eyebrow: "EVIDENCE TRUST",
                    title: ingestionTitle,
                    detail: .sentence(ingestionDetail, compact: ingestionPresentation.detailCompact),
                    stem: ingestionTint,
                    destination: .sources,
                    action: { open(.sources) }
                )
            }
        }
        // `.contain` keeps the rail one group and lets each row keep its own
        // identifier, instead of the card's id replacing all four (K125).
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("dashboard.shift-brief.signal-rail")
    }

    private var capacityTitle: String {
        switch availability {
        case .loading: return "Checking provider limits"
        case .unavailable: return "Live allowance unavailable"
        case .connected: break
        }
        if let headline { return headline.title }
        return rowsWithoutValidCapacity.isEmpty ? "No live allowance" : "No valid live allowance"
    }

    private var capacityDetail: DashboardSignalDetail {
        switch availability {
        case .loading: return .sentence("Waiting for the local glance projection.")
        case .unavailable(let message): return .sentence(message)
        case .connected: break
        }
        if let headline { return headline.signalDetail }
        if rowsWithoutValidCapacity.count == 1, let row = rowsWithoutValidCapacity.first {
            let detail = row.signalDetail
            return DashboardSignalDetail(
                absences: detail.absences,
                context: [row.client] + detail.context
            )
        }
        if !rowsWithoutValidCapacity.isEmpty {
            return .sentence("\(rowsWithoutValidCapacity.count) recording clients lack a valid 7-day reading.")
        }
        return .sentence("Open Usage for recorded volume and provider limits.")
    }

    private var capacityTint: Color {
        guard case .connected = availability else { return Theme.muted }
        guard let used = headline?.usedPercent else { return Theme.muted }
        // A stem is a filled mark: the limit FILL weight (K47), matching the
        // Usage and menu meters.
        return Theme.limitFillColor(usedPercent: used)
    }

    private var ingestionTitle: String {
        ingestionPresentation.title
    }

    private var ingestionDetail: String {
        ingestionPresentation.detail
    }

    private var ingestionTint: Color {
        switch ingestionPresentation.tone {
        case .muted: return Theme.muted
        case .healthy: return Theme.green
        case .warning: return Theme.amberFill
        }
    }
}

enum DashboardSignalAvailability: Equatable {
    case connected
    case loading
    case unavailable(String)
}

private struct DashboardSignalRow: View {
    let eyebrow: String
    let title: String
    /// The ordered payload phrases for this signal (K69).
    let detail: DashboardSignalDetail
    /// FILL-weight color of the 3pt stem (K47): `amberFill`, never `amber`;
    /// `limitFillColor`, never `limitTextColor`.
    let stem: Color
    /// Where the row leads. The hint names it, instead of saying every row
    /// opens "the related detail" (K125).
    let destination: DashboardDestination
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(alignment: .top, spacing: Space.m) {
                RoundedRectangle(cornerRadius: 1, style: .continuous)
                    .fill(stem)
                    .frame(width: 3, height: 30)
                    .padding(.top, 2)
                // Eyebrow, title and ONE detail line: the rail is a scan of
                // four signals, so each row keeps a fixed three-line rhythm
                // and never pushes Recent work out of the first viewport.
                // The full detail stays in the hover help and the
                // accessibility label.
                VStack(alignment: .leading, spacing: 2) {
                    Text(eyebrow)
                        .workFont(.labelCaps)
                        .tracking(Type.labelCapsTracking)
                        .foregroundStyle(Theme.muted)
                    Text(title)
                        .workFont(.rowLabel)
                        .foregroundStyle(Theme.ink)
                        .lineLimit(1)
                    // One line, but never a cut-off fact: the full detail if
                    // it fits, otherwise the reducer's short form, which
                    // leads with the named absences. Both are complete
                    // sentences — neither ends in an ellipsis (K69).
                    ViewThatFits(in: .horizontal) {
                        detailLine(detail.full)
                        detailLine(detail.compact)
                    }
                }
                Spacer(minLength: Space.s)
                DashboardDisclosureIndicator().padding(.top, 18)
            }
            .padding(.horizontal, Space.l)
            .padding(.vertical, Space.s)
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
        }
        // Full-bleed card rows take the ONE shared interaction contract
        // (idle/hover/pressed/disabled from ButtonFeedback), so a hovered
        // Dashboard row no longer wears the evidence-tier tint that Work uses
        // to mean "selected" (K76).
        .buttonStyle(SurfaceButtonStyle(cornerRadius: 0, focusInset: 2))
        .accessibilityElement(children: .combine)
        // The FULL detail is the spoken value, whichever form the row drew.
        .accessibilityLabel("\(eyebrow), \(title)")
        .accessibilityValue(detail.full)
        .accessibilityHint("Opens \(destination.paneName)")
        .accessibilityIdentifier("dashboard.signal.\(eyebrow.lowercased().replacingOccurrences(of: " ", with: "-"))")
    }

    private func detailLine(_ text: String) -> some View {
        Text(text)
            .workFont(.caption)
            .foregroundStyle(Theme.muted)
            .lineLimit(1)
    }
}

private struct DashboardCardHeader<Action: View>: View {
    let title: String
    var count: Int?
    @ViewBuilder let action: () -> Action

    init(
        _ title: String,
        count: Int? = nil,
        @ViewBuilder action: @escaping () -> Action
    ) {
        self.title = title
        self.count = count
        self.action = action
    }

    var body: some View {
        HStack(spacing: Space.s) {
            Text(title)
                .workFont(.titleCard)
                .foregroundStyle(Theme.muted)
                // A card title is the section heading VoiceOver's heading
                // rotor jumps to; without the trait the landing screen had no
                // structure at all (K121).
                .accessibilityAddTraits(.isHeader)
            if let count {
                Text(String(count))
                    .workFont(.dataSmallSemibold)
                    .foregroundStyle(Theme.muted)
                    .padding(.horizontal, 6)
                    .frame(minWidth: 20, minHeight: 20)
                    .background(Theme.tintNeutral, in: Capsule())
            }
            Spacer(minLength: Space.s)
            action()
        }
        .padding(.leading, Space.l)
        .padding(.trailing, 8)
        .frame(height: 44)
    }
}

private extension DashboardCardHeader where Action == EmptyView {
    init(_ title: String, count: Int? = nil) {
        self.init(title, count: count) { EmptyView() }
    }
}

/// Fixed columns of the Recent work table. Each is sized to the widest
/// string it actually holds (header or cell), measured at the rendered font,
/// so a fixed column never wraps its own text and never starves Task (C29).
enum RecentWorkColumn: Hashable {
    case outcome
    case evidence
    case cost
}

private struct RecentWorkColumnWidthsKey: PreferenceKey {
    static let defaultValue: [RecentWorkColumn: CGFloat] = [:]

    static func reduce(
        value: inout [RecentWorkColumn: CGFloat],
        nextValue: () -> [RecentWorkColumn: CGFloat]
    ) {
        value.merge(nextValue()) { max($0, $1) }
    }
}

private extension View {
    /// Reports this cell's ideal width for `column`, then takes the column's
    /// shared width once measured.
    func recentWorkColumn(
        _ column: RecentWorkColumn,
        widths: [RecentWorkColumn: CGFloat],
        alignment: Alignment
    ) -> some View {
        fixedSize(horizontal: true, vertical: false)
            .background(
                GeometryReader { proxy in
                    Color.clear.preference(
                        key: RecentWorkColumnWidthsKey.self,
                        value: [column: proxy.size.width]
                    )
                }
            )
            .frame(width: widths[column], alignment: alignment)
    }
}

private struct RecentWorkCard: View {
    let items: [DashboardWorkItem]
    let totalCount: Int
    /// The vocabulary's field labels (`/v1/tasks` `field_labels`).
    var fieldLabels = ReceiptFieldLabels()
    let open: (DashboardDestination) -> Void
    @State private var columnWidths: [RecentWorkColumn: CGFloat] = [:]

    var body: some View {
        Card(padding: 0, fillsHeight: true) {
            VStack(spacing: 0) {
                DashboardCardHeader("Recent work", count: totalCount) {
                    Button { open(.work) } label: {
                        Text("View all").workFont(.captionSemibold)
                    }
                    .foregroundStyle(Theme.accent)
                    .buttonStyle(QuietButtonStyle())
                    .accessibilityIdentifier("dashboard.recent-work.view-all")
                }
                Divider().overlay(Theme.hairline)

                if items.isEmpty {
                    DashboardEmptyState(
                        icon: "checklist",
                        title: "No recorded work yet",
                        message: "Set up recording to see task outcomes and evidence here."
                    )
                    .frame(minHeight: 222)
                } else {
                    workColumnLabels
                    Divider().overlay(Theme.hairline)
                    ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                        RecentWorkRow(
                            item: item,
                            columnWidths: columnWidths,
                            fieldLabels: fieldLabels
                        ) { open(.task(item.id)) }
                        if index < items.count - 1 {
                            Divider().overlay(Theme.hairline)
                        }
                    }
                }
            }
            .onPreferenceChange(RecentWorkColumnWidthsKey.self) { widths in
                if columnWidths != widths { columnWidths = widths }
            }
        }
    }

    /// The one column-header style (K126): the caps-mono label species, the
    /// same as the Work table header.
    private var workColumnLabels: some View {
        HStack(spacing: 12) {
            CapsLabel(text: fieldLabels.taskLabel).frame(maxWidth: .infinity, alignment: .leading)
            CapsLabel(text: fieldLabels.decisionLabel).recentWorkColumn(.outcome, widths: columnWidths, alignment: .leading)
            CapsLabel(text: fieldLabels.coverageLabel).recentWorkColumn(.evidence, widths: columnWidths, alignment: .leading)
            CapsLabel(text: fieldLabels.costLabel).recentWorkColumn(.cost, widths: columnWidths, alignment: .trailing)
            DashboardDisclosureIndicator().hidden()
        }
        .lineLimit(1)
        .padding(.horizontal, Space.l)
        .frame(minHeight: 30)
        // One named group of column names, not four loose texts above the
        // rows (K123).
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Column headers")
        .accessibilityIdentifier("dashboard.recent-work.columns")
    }
}

private struct RecentWorkRow: View {
    let item: DashboardWorkItem
    let columnWidths: [RecentWorkColumn: CGFloat]
    /// The vocabulary's field labels, so a spoken fact is named by the same
    /// word its column header prints.
    var fieldLabels = ReceiptFieldLabels()
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(item.title)
                        .workFont(.rowLabel)
                        .foregroundStyle(Theme.ink)
                        .lineLimit(2)
                    Text([item.client, item.recency].compactMap { $0 }.joined(separator: " · "))
                        .workFont(.caption)
                        .foregroundStyle(Theme.muted)
                        .lineLimit(1)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .layoutPriority(1)

                // Decision axis: a pip-less tinted badge (a filled dot here
                // read as the independently-checked evidence pip). The
                // reducer's decision statement is its help (C71).
                DecisionBadge(
                    key: item.outcomeKey,
                    label: item.outcome,
                    compact: true,
                    help: item.decisionStatement
                )
                .recentWorkColumn(.outcome, widths: columnWidths, alignment: .leading)

                // Evidence axis: the strongest tier's pip shape + the ratio.
                HStack(spacing: 6) {
                    if item.evidenceIsInconsistent {
                        Image(systemName: "exclamationmark.triangle")
                            .workFont(.icon)
                            .foregroundStyle(Theme.amber)
                            .accessibilityHidden(true)
                    } else if let tier = item.strongestTier {
                        EvidencePip(grade: tier)
                    } else {
                        EvidencePip(grade: nil)
                    }
                    Text(item.evidence)
                        // One face rule, one role source — never a condition
                        // re-derived per surface (K10).
                        .workFont(FieldFont.value(.dataSmall, isMetric: item.evidenceIsMetric))
                        .foregroundStyle(item.evidenceIsInconsistent ? Theme.amber : Theme.muted)
                        .lineLimit(1)
                }
                .recentWorkColumn(.evidence, widths: columnWidths, alignment: .leading)
                .help(item.evidenceQualifier)

                // The reducer's cost figure or named absence, as it arrives,
                // with its basis PRINTED beneath it (C17/C20). The basis used
                // to live only in `.help`, and hover help is not a place a
                // required fact may hide (K75).
                VStack(alignment: .trailing, spacing: 1) {
                    Text(item.cost)
                        .workFont(item.costIsAbsent ? FieldFont.absence : .dataSmall)
                        .foregroundStyle(Theme.muted)
                        .lineLimit(1)
                    if let basis = item.costBasis {
                        Text(basis)
                            .workFont(.caption)
                            .foregroundStyle(Theme.muted)
                            .lineLimit(1)
                            .minimumScaleFactor(0.85)
                    }
                }
                .recentWorkColumn(.cost, widths: columnWidths, alignment: .trailing)

                DashboardDisclosureIndicator()
            }
            .padding(.horizontal, Space.l)
            .frame(minHeight: 64)
            .contentShape(Rectangle())
        }
        // Full-bleed card rows take the ONE shared interaction contract
        // (idle/hover/pressed/disabled from ButtonFeedback), so a hovered
        // Dashboard row no longer wears the evidence-tier tint that Work uses
        // to mean "selected" (K76).
        .buttonStyle(SurfaceButtonStyle(cornerRadius: 0, focusInset: 2))
        // Every visible fact has a spoken twin: the subline's client and
        // recency were on screen but missing from the label (K123). The
        // measured fields ride as named custom content, as the Work rows do.
        .accessibilityLabel("\(item.title), \(item.outcome)")
        .workRowAccessibilityFields([
            .init(label: fieldLabels.coverageLabel, value: item.evidence),
            .init(label: fieldLabels.costLabel, value: item.costWithBasis),
            .init(label: fieldLabels.clientLabel, value: item.client),
            .init(label: fieldLabels.updatedLabel, value: item.recency ?? PayloadAbsence.activityTime),
        ])
        .accessibilityHint("Opens this task in Work")
        .accessibilityIdentifier("dashboard.recent-work.task.\(item.id)")
    }
}

/// The CAPACITY signal: the reducer's headline window, worded like every
/// other surface (`codex · 99% used`), with its window, provenance, reset and
/// data age — all payload words.
struct DashboardCapacityHeadline: Equatable {
    let title: String
    /// The ordered payload phrases; the rail renders whichever form fits (K69).
    let signalDetail: DashboardSignalDetail
    let usedPercent: Double?

    var detail: String { signalDetail.full }

    init(entry: LimitEntry, window: LimitWindow) {
        let client = entry.client ?? "Client name not reported"
        title = "\(client) · \(window.valueLabelText)"
        signalDetail = DashboardSignalDetail(
            absences: [PayloadAbsence.text(entry.dataAgeText)].compactMap { $0 },
            context: [window.windowLabelText, "provider reported", window.resetLabelText]
        )
        usedPercent = window.usedPercent.flatMap { $0.isFinite && $0 >= 0 ? $0 : nil }
    }
}

/// Per-agent capacity context for the signal rail. The union retains every
/// recording client so missing or stale limits remain named; the rail may then
/// select the least-headroom live provider reading without inventing one.
struct DashboardAgentPlanRow: Equatable, Identifiable {
    let client: String
    let planType: String?
    let usedPercent: Double?
    let meterCaption: String
    let resetText: String?
    let calibrating: Bool
    let calibratingDetail: String?
    let usageText: String?

    var id: String { client }

    /// The one capacity wording (`codex · 99% used`): the reducer's value
    /// phrase, never a Swift-derived headroom figure (K11).
    let valueText: String?

    var decisionTitle: String {
        guard usedPercent != nil, let valueText else { return client }
        return "\(client) · \(valueText)"
    }

    /// Full capacity sentence: window, provenance, reset, and the plan-share
    /// state as its muted named state while it cannot be shown (C49).
    var detailText: String { signalDetail.full }

    /// The same phrases, ordered so the plan-share NAMED ABSENCE leads the
    /// rail's short form instead of falling off the end of it (K69).
    var signalDetail: DashboardSignalDetail {
        var context = [meterCaption]
        if usedPercent != nil { context.append("provider reported") }
        if let resetText { context.append(resetText) }
        let absences = (calibrating ? calibratingDetail : nil).map { [$0] } ?? []
        return DashboardSignalDetail(absences: absences, context: context)
    }

    init(
        client: String,
        limit: LimitEntry?,
        staleLimit: Bool = false,
        plan: V1PlanClient?,
        usage: GlanceClientUsage?
    ) {
        self.client = client
        self.planType = limit?.planType
        let window = (limit?.windows ?? []).first { $0.kind == "7d" }
        let reportedUsed = window?.usedPercent
        let validUsed = reportedUsed.flatMap { value in
            value.isFinite && value >= 0 ? value : nil
        }
        self.usedPercent = validUsed
        valueText = window.map(\.valueLabelText)
        if validUsed != nil, let window {
            // The window name and reset phrase are the reducer's
            // (C18/C55/C95); the value phrase rides decisionTitle.
            meterCaption = window.windowLabelText
            resetText = window.resetLabelText
        } else if reportedUsed != nil, let window {
            meterCaption = "invalid \(window.windowLabelText) value reported"
            resetText = nil
        } else if let window {
            meterCaption = "\(window.windowLabelText) usage not reported"
            resetText = nil
        } else if staleLimit {
            // A stale reading is hidden, not never-reported — say so.
            meterCaption = "limit reading stale — see Usage"
            resetText = nil
        } else if limit != nil {
            meterCaption = "no 7-day window reported"
            resetText = nil
        } else {
            meterCaption = "no limits reported"
            resetText = nil
        }
        // A terminally out-of-band fit is not "warming up": it is included
        // here and shown as the reducer's muted named state (C49).
        let calibrationState = plan?.calibrationState ?? limit?.planShare?.calibrationState
        self.calibrating = calibrationState == "calibrating" || calibrationState == "out_of_band"
        self.calibratingDetail = PayloadAbsence.text(limit?.planShare?.headline)
            ?? PayloadAbsence.text(plan?.stateDetail)
        if let usage {
            // "7d ·" anchors figures to the fixed signal-rail window; Usage
            // remains the full range-aware destination.
            let cost = usage.costText ?? usage.costAbsenceText ?? PayloadAbsence.cost
            let tokens = usage.freshTokens.map { UsageTotals.compact($0) }
            usageText = "7d · " + ([cost] + (tokens.map { [$0] } ?? [])).joined(separator: " · ")
        } else {
            usageText = nil
        }
    }

    /// Signal input set: every non-stale limit client ∪ every client with
    /// 7-day usage. Limit clients first (most-used first, so the least
    /// headroom still leads), then usage-only clients in the cube's own
    /// volume order. One row per client — a second org's entry for the same
    /// client is merged Usage-pane detail.
    static func rows(
        limits: [LimitEntry],
        staleClients: Set<String> = [],
        planClients: [V1PlanClient],
        usage: [GlanceClientUsage]
    ) -> [DashboardAgentPlanRow] {
        func sevenDayUsed(_ entry: LimitEntry) -> Double? {
            guard let used = (entry.windows ?? []).first(where: { $0.kind == "7d" })?.usedPercent,
                  used.isFinite, used >= 0 else { return nil }
            return used
        }
        var limitByClient: [String: LimitEntry] = [:]
        for entry in limits {
            guard let client = entry.client, !client.isEmpty else { continue }
            if let existing = limitByClient[client] {
                // Prefer the entry that actually reports a 7d percent; ties
                // keep the higher-used one (least headroom is the honest pick).
                let existingUsed = sevenDayUsed(existing)
                let candidateUsed = sevenDayUsed(entry)
                if (existingUsed ?? -1) < (candidateUsed ?? -1) {
                    limitByClient[client] = entry
                }
            } else {
                limitByClient[client] = entry
            }
        }
        var order = limitByClient.keys.sorted {
            let left = sevenDayUsed(limitByClient[$0]!) ?? -1
            let right = sevenDayUsed(limitByClient[$1]!) ?? -1
            if left != right { return left > right }
            return $0 < $1
        }
        for client in staleClients.sorted() where !order.contains(client) && !client.isEmpty {
            order.append(client)
        }
        for entry in usage where !order.contains(entry.client) && !entry.client.isEmpty {
            order.append(entry.client)
        }
        let usageMap = Dictionary(usage.map { ($0.client, $0) }) { first, _ in first }
        let planMap = Dictionary(planClients.map { ($0.client, $0) }) { first, _ in first }
        return order.map { client in
            DashboardAgentPlanRow(
                client: client,
                limit: limitByClient[client],
                staleLimit: staleClients.contains(client),
                plan: planMap[client],
                usage: usageMap[client]
            )
        }
    }
}

func staleSevenDayLimitClients(in limits: [LimitEntry]) -> Set<String> {
    var live = Set<String>()
    var stale = Set<String>()
    for limit in limits {
        guard let client = limit.client?.trimmingCharacters(in: .whitespacesAndNewlines),
              !client.isEmpty,
              limit.windows?.contains(where: { $0.kind == "7d" }) == true
        else {
            continue
        }
        if limit.stale == true {
            stale.insert(client)
        } else {
            live.insert(client)
        }
    }
    return stale.subtracting(live)
}

private struct DashboardUsageChart: View {
    let periods: [PeriodBucket]
    let totals: UsageBucket?
    let rangeDays: Int
    let periodPresentation: UsagePeriodPresentation
    var vocabulary: UsageChartVocabulary = UsageChartVocabulary()

    /// The persisted measure key ("" = never chosen: the payload default,
    /// the same resting rule as the Usage chart).
    @AppStorage("usage.series.dashboard") private var storedSeries: String = ""

    private var series: DashboardUsageSeries {
        vocabulary.resolvedSeries(stored: storedSeries, periods: periods) == .cost ? .cost : .tokens
    }
    @State private var hoveredIndex: Int?
    /// No bar is selected by default: the chart rests in full color and the
    /// readout shows the range total (C25).
    @State private var pinnedIndex: Int?
    @FocusState private var focusedIndex: Int?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private let plotHeight: CGFloat = 110
    private let dateBandSpacing: CGFloat = 5
    private let dateBandHeight: CGFloat = 20

    private var activeIndex: Int? { hoveredIndex ?? focusedIndex ?? pinnedIndex }
    private var hasUserSelection: Bool {
        pinnedIndex != nil || hoveredIndex != nil || focusedIndex != nil
    }
    private var maximum: Double {
        max(periods.compactMap { series.value(for: $0) }.max() ?? 0, 1)
    }

    var body: some View {
        Card(padding: 0) {
            VStack(spacing: 0) {
                HStack(spacing: Space.m) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Usage history")
                            .workFont(.titleCard)
                            .foregroundStyle(Theme.muted)
                            .accessibilityAddTraits(.isHeader)
                        Text(
                            series.subtitle(
                                rangeDays: rangeDays,
                                periodPresentation: periodPresentation,
                                costBasis: totals?.costConfidenceDisplay,
                                vocabulary: vocabulary
                            )
                        )
                            .workFont(.caption)
                            .foregroundStyle(Theme.muted)
                    }
                    Spacer(minLength: Space.s)
                    Text(series.totalText(for: periods, totals: totals))
                        .workFont(.dataSmall)
                        .foregroundStyle(Theme.muted)
                    // The ONE app segmented control, shared with the Usage
                    // pane's range and measure choices (K108).
                    SegmentedChoice(
                        options: vocabulary.orderedSeries.map { chartChoice in
                            (chartChoice == .cost ? DashboardUsageSeries.cost : .tokens,
                             vocabulary.label(for: chartChoice))
                        },
                        selection: Binding(
                            get: { series },
                            set: { choice in
                                storedSeries = choice.rawValue
                                hoveredIndex = nil
                                pinnedIndex = nil
                            }
                        ),
                        accessibilityLabel: "Chart measure",
                        accessibilityIdentifier: "dashboard.usage.measure"
                    )
                }
                .padding(.horizontal, Space.l)
                .frame(minHeight: 47)

                Divider().overlay(Theme.hairline)

                chart
                    .padding(.horizontal, Space.l)
                    .padding(.vertical, 11)

                if series == .cost, periods.contains(where: { series.barState(for: $0).isPartial }) {
                    CostChartLegendRow(caption: nil, legend: vocabulary.costLegend)
                        .padding(.horizontal, Space.l)
                        .padding(.bottom, Space.m)
                }
            }
        }
        .onChange(of: periods.map(\.period)) {
            hoveredIndex = nil
            pinnedIndex = nil
            focusedIndex = nil
        }
    }

    private var axisLabels: [String] {
        [maximum, maximum / 2, 0].map { series.axisText(for: $0, scale: maximum) }
    }

    private var chart: some View {
        // The axis column is exactly the plot height and each label centers
        // on its gridline; the date band sits below, outside it (C46). The
        // readout band sits above the plot so the words never cover a bar.
        HStack(alignment: .top, spacing: Space.s) {
            PeriodChartAxis(labels: axisLabels, plotHeight: plotHeight)
                .padding(.top, PeriodChartReadout.bandHeight)

            GeometryReader { proxy in
                let count = max(periods.count, 1)
                let gap: CGFloat = 6
                let columnWidth = max(1, (proxy.size.width - gap * CGFloat(count - 1)) / CGFloat(count))
                let stride = PeriodChartDateBand.stride(
                    labelWidth: Self.dateLabelWidth(periods),
                    count: periods.count,
                    plotWidth: proxy.size.width
                )
                let labelled = PeriodChartDateBand.labelledIndices(count: periods.count, stride: stride)

                VStack(alignment: .leading, spacing: dateBandSpacing) {
                    readoutBand(columnWidth: columnWidth, gap: gap, plotWidth: proxy.size.width)

                    ZStack(alignment: .topLeading) {
                        ForEach(0..<axisLabels.count, id: \.self) { line in
                            Rectangle()
                                .fill(Theme.hairline)
                                .frame(height: 1)
                                .offset(
                                    y: PeriodChartAxis.gridlineY(
                                        index: line,
                                        count: axisLabels.count,
                                        plotHeight: plotHeight
                                    ) - 0.5
                                )
                        }

                        HStack(alignment: .bottom, spacing: gap) {
                            ForEach(Array(periods.enumerated()), id: \.offset) { index, period in
                                barButton(index: index, period: period)
                                    .frame(width: columnWidth)
                            }
                        }
                        .frame(height: plotHeight, alignment: .bottom)
                        .animation(
                            Motion.animatesChartGeometry(
                                bucketCount: periods.count,
                                reduceMotion: reduceMotion
                            ) ? Motion.contentUpdate : nil,
                            value: series
                        )
                    }
                    .frame(width: proxy.size.width, height: plotHeight, alignment: .topLeading)

                    HStack(spacing: gap) {
                        ForEach(Array(periods.enumerated()), id: \.offset) { index, period in
                            Text(labelled.contains(index) ? period.displayLabel : "")
                                .workFont(.dataSmall)
                                .foregroundStyle(Theme.muted)
                                .lineLimit(1)
                                .fixedSize(horizontal: true, vertical: false)
                                .frame(width: columnWidth)
                        }
                    }
                    .frame(height: dateBandHeight)
                    .accessibilityHidden(true)
                }
            }
            .frame(height: chartHeight)
        }
        .frame(minHeight: chartHeight)
    }

    private var chartHeight: CGFloat {
        PeriodChartReadout.bandHeight + dateBandSpacing + plotHeight + dateBandSpacing + dateBandHeight
    }

    /// Widest date label plus breathing room, so the stride rule can be read
    /// off the labels the band actually draws.
    static func dateLabelWidth(_ periods: [PeriodBucket]) -> CGFloat {
        let longest = periods.map(\.displayLabel.count).max() ?? 5
        // `.dataSmall` is a 12pt mono face: ~7.2pt per advance. Round UP, and
        // add a gutter, so the stride can never be one step too small and let
        // two labels touch.
        return CGFloat(longest) * 8 + 12
    }

    /// The ONE readout, anchored over the focused bar (K79).
    @ViewBuilder
    private func readoutBand(columnWidth: CGFloat, gap: CGFloat, plotWidth: CGFloat) -> some View {
        ZStack(alignment: .topLeading) {
            Color.clear
            if let index = activeIndex, periods.indices.contains(index) {
                let center = columnWidth / 2 + CGFloat(index) * (columnWidth + gap)
                let text = PeriodChartReadout.text(
                    period: periods[index].displayLabel,
                    value: series.valueText(for: periods[index])
                )
                PeriodChartReadout(text: text)
                    .modifier(PeriodChartReadoutAnchor(
                        columnCenter: center,
                        plotWidth: plotWidth
                    ))
                    .transition(.opacity)
            }
        }
        .frame(width: plotWidth, height: PeriodChartReadout.bandHeight, alignment: .topLeading)
    }

    private func barButton(index: Int, period: PeriodBucket) -> some View {
        Button {
            pinnedIndex = pinnedIndex == index ? nil : index
        } label: {
            VStack(spacing: 0) {
                Spacer(minLength: 0)
                bar(index: index, period: period)
            }
            // Fixed plot height so the max bar tops at the gridline its axis
            // label names.
            .frame(maxWidth: .infinity)
            .frame(height: plotHeight)
            .contentShape(Rectangle())
        }
        // The standard 2pt focus ring and press feedback (C106).
        .buttonStyle(TransparentButtonStyle())
        .focused($focusedIndex, equals: index)
        .onHover { inside in
            withAnimation(Motion.hover) {
                if inside {
                    hoveredIndex = index
                } else if hoveredIndex == index {
                    hoveredIndex = nil
                }
            }
        }
        .accessibilityLabel(
            "\(period.displayLabel), \(series.valueText(for: period))"
        )
        .accessibilityHint(periodPresentation.pinAccessibilityHint)
        .accessibilityAddTraits(pinnedIndex == index ? .isSelected : [])
        .accessibilityIdentifier("dashboard.usage.day.\(index)")
    }

    /// One mark per cube state (C48): a charted value in the shared bar
    /// color, else the SHARED absence mark both period charts draw (K46).
    @ViewBuilder
    private func bar(index: Int, period: PeriodBucket) -> some View {
        switch series.barState(for: period) {
        case .value(let value, let partial):
            // A partial-cost bucket wears the open cap at rest (K106).
            PeriodValueBar(
                color: Theme.periodBarColor(
                    isActive: activeIndex == index,
                    hasUserSelection: hasUserSelection
                ),
                height: value > 0 ? max(3, plotHeight * value / maximum) : 1,
                partial: partial
            )
        case .noUsage:
            PeriodAbsenceMark(kind: .noUsage)
        case .unpriced:
            PeriodAbsenceMark(kind: .unpriced)
        }
    }
}

private struct DashboardDisclosureIndicator: View {
    var body: some View {
        Image(systemName: "chevron.forward")
            .workFont(.icon)
            .foregroundStyle(Theme.muted)
            .fixedSize()
            .accessibilityHidden(true)
    }
}

private struct DashboardEmptyState: View {
    let icon: String
    let title: String
    let message: String

    var body: some View {
        HStack(spacing: Space.m) {
            Image(systemName: icon)
                .workFont(.iconLarge)
                .foregroundStyle(Theme.muted)
                .frame(width: 24)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).workFont(.rowLabel).foregroundStyle(Theme.ink)
                Text(message).workFont(.caption).foregroundStyle(Theme.muted)
            }
        }
        .padding(Space.l)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
