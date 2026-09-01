import AppKit
import Foundation
import SwiftUI

enum DashboardFontRole {
    case titlePage
    case titleSection
    case titleCard
    case kpi
    case rowLabel
    case body
    case data
    case caption
    case captionSemibold
    case dataSmall
    case dataSmallSemibold
    case labelCaps
    case columnHeader

    var baseSize: CGFloat {
        switch self {
        case .titlePage: return 26
        case .titleSection: return 20
        case .kpi: return 18
        case .titleCard: return 15
        case .rowLabel, .body: return 14
        case .data: return 13
        case .caption, .captionSemibold, .dataSmall, .dataSmallSemibold,
             .labelCaps, .columnHeader: return 12
        }
    }

    func scaledSize(for dynamicTypeSize: DynamicTypeSize) -> CGFloat {
        let requestedScale: CGFloat
        if dynamicTypeSize >= .accessibility5 {
            requestedScale = 2.0
        } else if dynamicTypeSize >= .accessibility4 {
            requestedScale = 1.8
        } else if dynamicTypeSize >= .accessibility3 {
            requestedScale = 1.65
        } else if dynamicTypeSize >= .accessibility2 {
            requestedScale = 1.5
        } else if dynamicTypeSize >= .accessibility1 {
            requestedScale = 1.35
        } else if dynamicTypeSize >= .xxxLarge {
            requestedScale = 1.24
        } else if dynamicTypeSize >= .xxLarge {
            requestedScale = 1.16
        } else if dynamicTypeSize >= .xLarge {
            requestedScale = 1.08
        } else if dynamicTypeSize <= .xSmall {
            requestedScale = 0.9
        } else if dynamicTypeSize <= .small {
            requestedScale = 0.95
        } else {
            requestedScale = 1.0
        }

        // Keep the page title from overwhelming a desktop window while body,
        // row, and data text can use the full requested accessibility scale.
        let roleMaximum: CGFloat
        switch self {
        case .titlePage:
            roleMaximum = 1.7
        case .titleSection, .titleCard, .kpi:
            roleMaximum = 1.85
        default:
            roleMaximum = 2.0
        }
        return baseSize * min(requestedScale, roleMaximum)
    }

    fileprivate func font(size: CGFloat) -> Font {
        let weight: Font.Weight
        switch self {
        case .titlePage, .titleSection, .titleCard, .rowLabel, .captionSemibold,
             .dataSmallSemibold:
            weight = .semibold
        case .kpi, .labelCaps:
            weight = .bold
        case .columnHeader:
            weight = .medium
        case .body, .data, .caption, .dataSmall:
            weight = .regular
        }

        switch self {
        case .kpi, .data, .dataSmall, .dataSmallSemibold, .labelCaps:
            if let mono = Face.mono {
                return Font.custom(mono, size: size)
                .weight(weight)
                .monospacedDigit()
            }
            return Font.system(size: size, weight: weight, design: .monospaced)
                .monospacedDigit()
        default:
            if let sans = Face.sans {
                return Font.custom(sans, size: size)
                .weight(weight)
            }
            return Font.system(size: size, weight: weight)
        }
    }
}

private struct DashboardFontModifier: ViewModifier {
    let role: DashboardFontRole
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    func body(content: Content) -> some View {
        content.font(role.font(size: role.scaledSize(for: dynamicTypeSize)))
    }
}

extension View {
    func dashboardFont(_ role: DashboardFontRole) -> some View {
        modifier(DashboardFontModifier(role: role))
    }
}

func dashboardUsesAccessibilityLayout(_ size: DynamicTypeSize) -> Bool {
    size >= .xxLarge
}

// The dashboard is a shift brief: what deserves attention now, what recorded
// evidence supports that claim, and where the operator can inspect it. Recent
// work, active sessions, plan headroom, and source health remain supporting
// context rather than four equally weighted destinations.

struct DashboardWorkItem: Identifiable {
    let id: String
    let title: String
    let project: String?
    let client: String
    let lastActivityAt: Double?
    let outcome: String
    let outcomeKey: String
    let evidence: String
    let evidenceQualifier: String
    let evidenceIsInconsistent: Bool
    let failedChecks: Int
    let cost: String
    let gradeable: Bool
    let strongestTierKey: String?

    init(task: ReceiptSummary) {
        id = task.taskId
        title = recordedTaskDisplayTitle(task.title, taskId: task.taskId)
        project = Self.nonempty(task.project)
        client = Self.nonempty(task.primaryRoot?.client) ?? "Unknown agent"
        lastActivityAt = task.lastActivityAt
        outcomeKey = task.decisionStatus.key
        if let label = task.decisionStatus.label, !label.isEmpty {
            outcome = label
        } else {
            outcome = Self.outcomeLabel(for: task.decisionStatus.key)
        }
        let evidencePresentation = ReceiptCoveragePresentation(evidence: task.evidenceStrength)
        evidence = task.evidenceStrength.compactHeadline
        evidenceQualifier = evidencePresentation.qualifier
        evidenceIsInconsistent = evidencePresentation.isInconsistent
        failedChecks = task.evidenceStrength.checksFailed ?? 0
        cost = Self.compactCost(task.cost)
        gradeable = task.evidenceStrength.gradeable == true
        strongestTierKey = task.evidenceStrength.strongestTier
    }

    var recency: String? {
        agoText(lastActivityAt)
    }

    var contextComponents: [String] {
        [project, client, recency].compactMap { $0 }
    }

    var visibleCost: String { cost == "—" ? "unpriced" : cost }

    /// Strongest evidence tier present (drives the row's tier pip).
    var strongestTier: String? {
        gradeable ? (strongestTierKey ?? "unchecked") : nil
    }

    private static func outcomeLabel(for key: String) -> String {
        switch key {
        case "verified": return "Verified"
        case "reported": return "Agent reported"
        case "finding", "failed": return "Open finding"
        case "blocked": return "Blocked"
        case "in_progress": return "In progress"
        case "handed_off": return "Handed off"
        default: return key.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    private static func nonempty(_ value: String?) -> String? {
        guard let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines),
              !trimmed.isEmpty
        else {
            return nil
        }
        return trimmed
    }

    private static func compactCost(_ cost: ReceiptCost) -> String {
        guard let value = cost.estimatedCostUsd else { return "—" }
        let prefix: String
        // A complete reported OR billed figure is exact ("$"); everything else
        // is an estimate. Matches Fmt.costDisplay and receiptCostDisplay so the
        // same cost never reads exact on Usage and estimated on the Dashboard.
        let reported = cost.costConfidence == "client_reported" || cost.costConfidence == "provider_billed"
        if cost.costComplete == false {
            prefix = "~$"
        } else if cost.costComplete == true && reported {
            prefix = "$"
        } else {
            prefix = "≈$"
        }
        return Fmt.dollars(value, prefix: prefix)
    }
}

/// UI projection of one server-ranked attention row. It deliberately exposes
/// the recorded reason and recorded next step separately: nil stays nil, so a
/// generic UI hint can never masquerade as agent-authored recovery guidance.
struct DashboardAttentionItem: Identifiable, Equatable {
    let id: String
    let title: String
    let project: String?
    let client: String?
    let reasonKind: String
    let summary: String
    let nextStep: String?
    let observedAt: Double?
    let sourceLabel: String?
    let handedOff: Bool?

    init?(task: ReceiptSummary) {
        guard let reason = task.attention else { return nil }
        id = task.taskId
        title = recordedTaskDisplayTitle(task.title, taskId: task.taskId)
        project = Self.nonempty(task.project)
        client = Self.nonempty(task.primaryRoot?.client)
        reasonKind = reason.kind
        summary = reason.summary.trimmingCharacters(in: .whitespacesAndNewlines)
        nextStep = Self.nonempty(reason.nextStep)
        observedAt = reason.observedAt
        handedOff = task.handedOff
        switch Self.nonempty(reason.source) {
        case "mcp": sourceLabel = "MCP record"
        case "client_log": sourceLabel = "Local client log"
        case "machine": sourceLabel = "Machine check"
        case "hook": sourceLabel = "Client hook"
        case "transcript_scan": sourceLabel = "Transcript import"
        case "ci": sourceLabel = "External CI or provider"
        case "git": sourceLabel = "Git repository"
        case "human": sourceLabel = "Human record"
        case "inferred": sourceLabel = "agentacct inference"
        case "none": sourceLabel = "No source recorded"
        case .some(let source): sourceLabel = source.replacingOccurrences(of: "_", with: " ").capitalized
        case nil: sourceLabel = nil
        }
    }

    var reasonLabel: String {
        switch reasonKind {
        case "failed_check": return "Failed check"
        case "failed_step": return "Failed step"
        case "blocker": return "Recorded blocker"
        default: return reasonKind.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    private static func nonempty(_ value: String?) -> String? {
        guard let value = value?.trimmingCharacters(in: .whitespacesAndNewlines),
              !value.isEmpty
        else {
            return nil
        }
        return value
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
        lines.append("Recorded attention: \(focus.reasonLabel) — \(focus.summary)")
        lines.append("Recorded next step: \(focus.nextStep ?? "None recorded")")
        lines.append("Observed: \(Self.timestamp(focus.observedAt) ?? "Not recorded")")
        lines.append("Provenance: \(focus.sourceLabel ?? "Not recorded")")
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
        guard hasConsistentAttentionHeadEnvelope(payload) else {
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

    var dashboardHeadline: String {
        switch self {
        case .loading: return "Checking recorded work"
        case .unavailable: return "Review status unavailable"
        case .clear: return "No recorded work needs review"
        case .focus(let item, _): return item.title
        case .inconsistent: return "Review details unavailable"
        }
    }

    var dashboardStatus: String {
        switch self {
        case .loading: return "Loading review projection"
        case .unavailable: return "Unavailable"
        case .clear: return "0 review items"
        case .focus(_, let total), .inconsistent(let total):
            return "\(total) review item\(total == 1 ? "" : "s")"
        }
    }

    var dashboardStatusIsWarning: Bool {
        switch self {
        case .unavailable, .inconsistent: return true
        case .loading, .clear, .focus: return false
        }
    }
}

enum DashboardUsageSeries: String, CaseIterable, Identifiable {
    case tokens = "Tokens"
    case cost = "Cost"

    var id: Self { self }

    func value(for period: PeriodBucket) -> Double {
        switch self {
        case .tokens:
            guard let tokens = period.freshTokens, tokens >= 0 else { return 0 }
            return Double(tokens)
        case .cost:
            guard let cost = period.estimatedCostUsd, cost.isFinite, cost >= 0 else { return 0 }
            return cost
        }
    }

    func valueText(for period: PeriodBucket) -> String {
        switch self {
        case .tokens:
            guard let value = period.freshTokens, value >= 0 else { return "—" }
            return UsageTotals.compact(value)
        case .cost:
            guard let cost = period.estimatedCostUsd, cost.isFinite, cost >= 0 else { return "—" }
            return period.costText
        }
    }

    func totalText(for periods: [PeriodBucket]) -> String {
        switch self {
        case .tokens:
            let available: [Int] = periods.compactMap { period -> Int? in
                guard let tokens = period.freshTokens, tokens >= 0 else { return nil }
                return tokens
            }
            guard !available.isEmpty else { return "—" }
            let prefix = available.count == periods.count ? "" : "~"
            let total = available.reduce(0.0) { $0 + Double($1) }
            return "\(prefix)\(UsageTotals.compact(total)) total"
        case .cost:
            let available = periods.compactMap { period -> Double? in
                guard let cost = period.estimatedCostUsd, cost.isFinite, cost >= 0 else { return nil }
                return cost
            }
            guard !available.isEmpty else { return "—" }
            let complete = available.count == periods.count
                && periods.allSatisfy { $0.costComplete == true }
            let reported = complete && periods.allSatisfy {
                ["client_reported", "provider_billed"].contains($0.costConfidence ?? "")
            }
            let prefix = reported ? "$" : (complete ? "≈$" : "~$")
            let total = available.reduce(0, +)
            guard total.isFinite else { return "—" }
            return "\(Fmt.dollars(total, prefix: prefix)) total"
        }
    }

    func subtitle(rangeDays: Int, periodPresentation: UsagePeriodPresentation) -> String {
        let range = periodPresentation.historyRangeDescription(days: rangeDays)
        switch self {
        case .tokens: return "Fresh tokens · \(range) · client reported"
        case .cost: return "Estimated cost · \(range) · pricing-table basis"
        }
    }

    func axisText(for value: Double) -> String {
        switch self {
        case .tokens:
            let compact = UsageTotals.compact(value)
            guard compact.count > 5 else { return compact }
            let whole = value.rounded(.towardZero)
            let magnitude = abs(whole)
            switch magnitude {
            case 1_000_000_000_000_000...:
                return String(format: "%.0e", whole)
                    .replacingOccurrences(of: "e+", with: "e")
            case 1_000_000_000_000...: return String(format: "%.0fT", whole / 1_000_000_000_000)
            case 1_000_000_000...: return String(format: "%.0fB", whole / 1_000_000_000)
            case 1_000_000...: return String(format: "%.0fM", whole / 1_000_000)
            case 1_000...: return String(format: "%.0fk", whole / 1_000)
            default: return compact
            }
        case .cost: return Self.costAxisText(value)
        }
    }

    private static func costAxisText(_ value: Double) -> String {
        guard value.isFinite, value >= 0 else { return "—" }
        let whole = value.rounded(.towardZero)
        let magnitude = abs(whole)
        switch magnitude {
        case 1_000_000_000_000_000...:
            return "$" + String(format: "%.0e", whole)
                .replacingOccurrences(of: "e+", with: "e")
        case 1_000_000_000_000...:
            return String(format: "$%.0fT", (whole / 1_000_000_000_000).rounded(.towardZero))
        case 1_000_000_000...:
            return String(format: "$%.0fB", (whole / 1_000_000_000).rounded(.towardZero))
        case 1_000_000...:
            return String(format: "$%.0fM", (whole / 1_000_000).rounded(.towardZero))
        case 1_000...:
            return String(format: "$%.0fk", (whole / 1_000).rounded(.towardZero))
        case 10...: return String(format: "$%.0f", whole)
        default:
            let cents = (value * 100).rounded(.towardZero) / 100
            return String(format: "$%.2f", cents)
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
            detail = "Recorded activity will appear here when an agent session is observed."
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
        now: Date = SnapshotMode.currentDate,
        timeZone: TimeZone = .current
    ) {
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

        let dated = periods.compactMap { period -> (String, Int?)? in
            guard let label = period.period, Self.isDateLabel(label) else { return nil }
            return (label, period.freshTokens)
        }.sorted { $0.0 < $1.0 }

        guard Set(dated.map(\.0)).count == dated.count else {
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

        let previousBucket = dated[dated.count - 2]
        let latestBucket = dated[dated.count - 1]
        guard let previousTokens = previousBucket.1, previousTokens >= 0,
              let latestTokens = latestBucket.1, latestTokens >= 0 else {
            state = .insufficient
            title = "Usage comparison incomplete"
            detail = "The latest two dated buckets need fresh-token values."
            return
        }

        let previous = (previousBucket.0, previousTokens)
        let latest = (latestBucket.0, latestTokens)
        let weekly = rangeDays >= 90
        let currentPeriod = Self.currentPeriodLabel(now: now, weekly: weekly, timeZone: timeZone)
        guard latest.0 <= currentPeriod else {
            state = .insufficient
            title = "Usage history is ahead of local time"
            detail = "Latest recorded period: \(latest.0)."
            return
        }
        state = .ready
        if latest.0 == currentPeriod {
            title = "\(weekly ? "This week" : "Today") so far · \(UsageTotals.compact(latest.1))"
            detail = "\(Self.bucketDescription(previous, weekly: weekly)) · client reported"
            return
        }

        if previous.1 == 0 {
            title = latest.1 == 0
                ? "Fresh tokens unchanged at 0"
                : "Fresh tokens rose from 0"
        } else {
            let change = ((Double(latest.1) - Double(previous.1)) / Double(previous.1)) * 100
            let rounded = abs(change).rounded()
            if latest.1 == previous.1 {
                title = "Fresh tokens unchanged"
            } else if rounded < 1 {
                title = "Fresh tokens roughly unchanged"
            } else if change > 999 {
                title = "Fresh tokens >999% higher"
            } else {
                title = "Fresh tokens \(String(format: "%.0f", rounded))% \(change > 0 ? "higher" : "lower")"
            }
        }
        detail = "\(Self.bucketDescription(latest, weekly: weekly)) · "
            + "\(Self.bucketDescription(previous, weekly: weekly)) · client reported"
    }

    private static func bucketDescription(_ bucket: (String, Int), weekly: Bool) -> String {
        let interval = weekly ? "in week of \(bucket.0)" : "on \(bucket.0)"
        return "\(UsageTotals.compact(bucket.1)) \(interval)"
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
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var usesScrollViewport = true

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

    private var planRows: [DashboardAgentPlanRow] {
        DashboardAgentPlanRow.rows(
            limits: liveLimits,
            staleClients: staleLimitClients,
            planClients: dashboard.planClients,
            usage: glanceUsageByClient
        )
    }

    var body: some View {
        Group {
            if usesScrollViewport {
                ScrollBox { renderedContent }
            } else {
                renderedContent
            }
        }
        .animation(
            reduceMotion ? Motion.reducedCrossfade : Motion.phaseCrossfade,
            value: presentedError
        )
    }

    /// The intrinsic dashboard surface. The production pane scrolls this view;
    /// the visual harness measures the same surface before applying a canvas.
    private var renderedContent: some View {
        VStack(alignment: .leading, spacing: Space.l) {
            DashboardShiftBriefHeader(
                payload: dashboard.attention,
                error: dashboard.attentionError
            )

            if let error = presentedError {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .dashboardFont(.caption)
                    .foregroundStyle(Theme.coral)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(Space.s)
                    .background(
                        Theme.card,
                        in: RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
                    )
                    .id(error)
                    .transition(.opacity)
            }

            splitRow {
                DashboardAttentionBriefCard(
                    payload: dashboard.attention,
                    error: dashboard.attentionError
                ) { destination in
                    selection.open(destination)
                }
            } right: {
                DashboardSignalRail(
                    sessions: recentSessions,
                    planRows: planRows,
                    availability: glanceAvailability,
                    usagePulse: DashboardUsagePulse(
                        periods: dashboard.usage?.byPeriod,
                        isLoaded: dashboard.usage != nil,
                        rangeDays: dashboard.usageDays,
                        error: dashboard.errorText
                    ),
                    ingestion: dashboard.ingestion,
                    ingestionError: dashboard.ingestionError
                ) { destination in
                    selection.open(destination)
                }
            }

            RecentWorkCard(
                items: recentWork,
                totalCount: dashboard.totalReceiptTasks,
                hasLoaded: dashboard.hasLoadedReceiptTasks,
                error: dashboard.receiptListError
            ) { destination in
                selection.open(destination)
            }

            if let usage = dashboard.usage,
               let periods = usage.byPeriod,
               periods.count > 1
            {
                DashboardUsageChart(
                    periods: periods,
                    rangeDays: dashboard.usageDays,
                    periodPresentation: UsagePeriodPresentation(usage: usage)
                )
            }
        }
        .padding(Space.gutter)
        .fixedSize(
            horizontal: false,
            vertical: dashboardUsesAccessibilityLayout(dynamicTypeSize)
        )
    }

    private func splitRow<Left: View, Right: View>(
        @ViewBuilder left: () -> Left,
        @ViewBuilder right: () -> Right
    ) -> some View {
        Group {
            if dashboardUsesAccessibilityLayout(dynamicTypeSize) {
                VStack(spacing: Space.l) {
                    left()
                    right()
                }
            } else {
                ViewThatFits(in: .horizontal) {
                    DashboardSplitLayout(leftFraction: 7 / 12, spacing: Space.l) {
                        left()
                        right()
                    }
                    .frame(minWidth: 820)

                    VStack(spacing: Space.l) {
                        left()
                        right()
                    }
                }
            }
        }
    }

}

private struct DashboardSplitLayout: Layout {
    let leftFraction: CGFloat
    let spacing: CGFloat

    func sizeThatFits(
        proposal: ProposedViewSize,
        subviews: Subviews,
        cache: inout ()
    ) -> CGSize {
        guard subviews.count == 2 else { return .zero }
        let width = proposal.width ?? subviews.reduce(0) {
            $0 + $1.sizeThatFits(.unspecified).width
        } + spacing
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
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    private var presentation: DashboardAttentionPresentation {
        DashboardAttentionPresentation(payload: payload, error: error)
    }

    var body: some View {
        Group {
            if dashboardUsesAccessibilityLayout(dynamicTypeSize) {
                VStack(alignment: .leading, spacing: Space.s) {
                    headline
                    status
                }
            } else {
                HStack(alignment: .lastTextBaseline, spacing: Space.xl) {
                    headline
                    Spacer(minLength: Space.m)
                    status
                }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("dashboard.shift-brief.header")
    }

    private var headline: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text("SHIFT BRIEF")
                .dashboardFont(.labelCaps)
                .tracking(Type.labelCapsTracking)
                .foregroundStyle(Theme.accent)
            Text(presentation.dashboardHeadline)
                .dashboardFont(.titlePage)
                .tracking(Type.titlePageTracking)
                .foregroundStyle(Theme.ink)
                .lineLimit(dashboardUsesAccessibilityLayout(dynamicTypeSize) ? nil : 2)
        }
    }

    private var status: some View {
        Text(presentation.dashboardStatus)
            .dashboardFont(.dataSmall)
            .foregroundStyle(presentation.dashboardStatusIsWarning ? Theme.amber : Theme.muted)
            .multilineTextAlignment(
                dashboardUsesAccessibilityLayout(dynamicTypeSize) ? .leading : .trailing
            )
    }
}

private struct DashboardAttentionBriefCard: View {
    let payload: V1AttentionPayload?
    let error: String?
    let open: (DashboardDestination) -> Void
    @State private var copyFeedback = DashboardCopyFeedback.idle
    @State private var copyFeedbackToken: UUID?
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    private var presentation: DashboardAttentionPresentation {
        DashboardAttentionPresentation(payload: payload, error: error)
    }

    private var tint: Color {
        switch presentation {
        case .focus(let focus, _): return focus.reasonKind == "blocker" ? Theme.amber : Theme.coral
        case .clear: return Theme.green
        case .loading: return Theme.muted
        case .unavailable, .inconsistent: return Theme.amber
        }
    }

    var body: some View {
        Card(
            padding: 0,
            fillsHeight: !dashboardUsesAccessibilityLayout(dynamicTypeSize)
        ) {
            HStack(spacing: 0) {
                Rectangle()
                    .fill(tint)
                    .frame(width: 5)
                    .accessibilityHidden(true)
                content
                    .padding(Space.xl)
                    .frame(
                        maxWidth: .infinity,
                        maxHeight: dashboardUsesAccessibilityLayout(dynamicTypeSize)
                            ? nil
                            : .infinity,
                        alignment: .topLeading
                    )
            }
        }
        .accessibilityIdentifier("dashboard.shift-brief.attention")
    }

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
                message: error
            )
        case .loading:
            HStack(spacing: Space.m) {
                ProgressView().controlSize(.small).tint(Theme.muted)
                VStack(alignment: .leading, spacing: 3) {
                    Text("Checking recorded work…")
                        .dashboardFont(.titleSection)
                        .foregroundStyle(Theme.ink)
                    Text("Loading the complete review projection; no clear-state claim is shown yet.")
                        .dashboardFont(.body)
                        .foregroundStyle(Theme.muted)
                }
            }
            .frame(
                maxHeight: dashboardUsesAccessibilityLayout(dynamicTypeSize)
                    ? nil
                    : .infinity,
                alignment: .center
            )
        }
    }

    private func focusContent(total: Int, focus: DashboardAttentionItem) -> some View {
        let brief = DashboardActionBrief(focus: focus)
        let copySucceeded = copyFeedback == .copied(brief.text)
        let copyFailed = copyFeedback == .failed(brief.text)
        return VStack(alignment: .leading, spacing: Space.l) {
            HStack(spacing: Space.s) {
                Text("PRIMARY ATTENTION")
                    .dashboardFont(.labelCaps)
                    .tracking(Type.labelCapsTracking)
                    .foregroundStyle(tint)
                Text("1 OF \(total)")
                    .dashboardFont(.dataSmallSemibold)
                    .foregroundStyle(Theme.muted)
                    .padding(.horizontal, 7)
                    .frame(minHeight: 21)
                    .background(Theme.tintNeutral, in: Capsule())
                Spacer(minLength: Space.s)
                if total > 1 {
                    Button("View queue") { open(.reviewQueue) }
                        .dashboardFont(.captionSemibold)
                        .foregroundStyle(Theme.accent)
                        .buttonStyle(QuietButtonStyle())
                        .accessibilityIdentifier("dashboard.shift-brief.view-queue")
                }
            }

            VStack(alignment: .leading, spacing: 7) {
                let context = [focus.project, focus.client].compactMap { $0 }
                if !context.isEmpty {
                    Text(context.joined(separator: " · "))
                        .dashboardFont(.dataSmall)
                        .foregroundStyle(Theme.muted)
                }
                Text(focus.summary)
                    .dashboardFont(.body)
                    .foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }

            DashboardProofline(focus: focus)

            VStack(alignment: .leading, spacing: 5) {
                Text("RECORDED NEXT STEP")
                    .dashboardFont(.labelCaps)
                    .tracking(Type.labelCapsTracking)
                    .foregroundStyle(Theme.muted)
                Text(focus.nextStep ?? "No next step recorded.")
                    .dashboardFont(.body)
                    .foregroundStyle(focus.nextStep == nil ? Theme.muted : Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(Space.m)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.tintNeutral, in: RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous))

            HStack(spacing: Space.s) {
                Button {
                    open(.attentionTask(focus.id))
                } label: {
                    Label("Review evidence", systemImage: "doc.text.magnifyingglass")
                        .dashboardFont(.captionSemibold)
                }
                .buttonStyle(.borderedProminent)
                .tint(Theme.accent)
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
                    .dashboardFont(.captionSemibold)
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
        .accessibilityElement(children: .contain)
    }
}

private struct DashboardProofline: View {
    let focus: DashboardAttentionItem
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        Group {
            if dashboardUsesAccessibilityLayout(dynamicTypeSize) {
                stackedFacts
            } else {
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 0) {
                        fact(label: "RECORDED REASON", value: focus.reasonLabel)
                        proofRule
                        fact(label: "OBSERVED", value: focus.recency ?? "Time unavailable")
                        proofRule
                        fact(label: "PROVENANCE", value: focus.sourceLabel ?? "Source unavailable")
                    }
                    stackedFacts
                }
            }
        }
        .padding(.vertical, Space.s)
        .overlay(alignment: .top) { Divider().overlay(Theme.hairline) }
        .overlay(alignment: .bottom) { Divider().overlay(Theme.hairline) }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(
            "Recorded reason: \(focus.reasonLabel). Observed: \(focus.recency ?? "time unavailable"). Provenance: \(focus.sourceLabel ?? "unavailable")."
        )
    }

    private var stackedFacts: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            fact(label: "RECORDED REASON", value: focus.reasonLabel)
            fact(label: "OBSERVED", value: focus.recency ?? "Time unavailable")
            fact(label: "PROVENANCE", value: focus.sourceLabel ?? "Source unavailable")
        }
    }

    private func fact(label: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label)
                .dashboardFont(.labelCaps)
                .tracking(Type.labelCapsTracking)
                .foregroundStyle(Theme.muted)
            Text(value)
                .dashboardFont(.dataSmallSemibold)
                .foregroundStyle(Theme.ink)
                .lineLimit(dashboardUsesAccessibilityLayout(dynamicTypeSize) ? nil : 1)
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
                Image(systemName: "checkmark.seal.fill")
                    .font(.system(size: 20, weight: .semibold))
                    .foregroundStyle(Theme.green)
                Text("COMPLETE REVIEW PROJECTION")
                    .dashboardFont(.labelCaps)
                    .tracking(Type.labelCapsTracking)
                    .foregroundStyle(Theme.green)
            }
            Text("No recorded work needs review.")
                .dashboardFont(.titleSection)
                .tracking(Type.titleSectionTracking)
                .foregroundStyle(Theme.ink)
            Text("No current failed check, failed step, or unresolved blocker was found across the complete attention projection.")
                .dashboardFont(.body)
                .foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
    }
}

private struct DashboardBriefUnavailableState: View {
    let title: String
    let message: String

    var body: some View {
        VStack(alignment: .leading, spacing: Space.l) {
            Image(systemName: "questionmark.diamond.fill")
                .font(.system(size: 20, weight: .semibold))
                .foregroundStyle(Theme.amber)
            Text(title)
                .dashboardFont(.titleSection)
                .tracking(Type.titleSectionTracking)
                .foregroundStyle(Theme.ink)
            Text(message)
                .dashboardFont(.body)
                .foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
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
    let tone: DashboardIngestionTone

    init(title: String, detail: String, tone: DashboardIngestionTone) {
        self.title = title
        self.detail = detail
        self.tone = tone
    }

    init(snapshot: V1IngestionSnapshot?, error: String?) {
        if let error {
            self.init(
                title: "Source status unavailable",
                detail: error,
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
            detail = "Open Sources for the current ingestion record."
        }

        if snapshot.state == "healthy" {
            self.init(title: "Sources healthy", detail: detail, tone: .healthy)
        } else if let state = snapshot.state, !state.isEmpty {
            self.init(
                title: state.replacingOccurrences(of: "_", with: " ").capitalized,
                detail: detail,
                tone: .warning
            )
        } else {
            self.init(title: "Source status unavailable", detail: detail, tone: .warning)
        }
    }
}

private struct DashboardSignalRail: View {
    let sessions: [RecentSession]
    let planRows: [DashboardAgentPlanRow]
    let availability: DashboardSignalAvailability
    let usagePulse: DashboardUsagePulse
    let ingestion: V1IngestionSnapshot?
    let ingestionError: String?
    let open: (DashboardDestination) -> Void
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    private var capacity: DashboardAgentPlanRow? {
        planRows.filter { $0.usedPercent != nil }
            .max { ($0.usedPercent ?? 0) < ($1.usedPercent ?? 0) }
    }

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
        Card(
            padding: 0,
            fillsHeight: !dashboardUsesAccessibilityLayout(dynamicTypeSize)
        ) {
            VStack(spacing: 0) {
                DashboardCardHeader("Signal rail")
                Divider().overlay(Theme.hairline)
                DashboardSignalRow(
                    eyebrow: "WORKING NOW",
                    title: active.title,
                    detail: active.detail,
                    tint: active.promotesInactivity
                        ? Theme.amber
                        : (active.hasConfirmedActiveWork ? Theme.accent : Theme.muted),
                    action: { open(.work) }
                )
                Divider().overlay(Theme.hairline).padding(.leading, Space.l)
                DashboardSignalRow(
                    eyebrow: "CAPACITY",
                    title: capacityTitle,
                    detail: capacityDetail,
                    tint: capacityTint,
                    action: { open(.limits) }
                )
                Divider().overlay(Theme.hairline).padding(.leading, Space.l)
                DashboardSignalRow(
                    eyebrow: "USAGE CHANGE",
                    title: usagePulse.title,
                    detail: usagePulse.detail,
                    tint: usagePulse.state == .ready ? Theme.accent : Theme.muted,
                    action: { open(.limits) }
                )
                Divider().overlay(Theme.hairline).padding(.leading, Space.l)
                DashboardSignalRow(
                    eyebrow: "EVIDENCE TRUST",
                    title: ingestionTitle,
                    detail: ingestionDetail,
                    tint: ingestionTint,
                    action: { open(.sources) }
                )
            }
        }
        .accessibilityIdentifier("dashboard.shift-brief.signal-rail")
    }

    private var capacityTitle: String {
        switch availability {
        case .loading: return "Checking provider limits"
        case .unavailable: return "Live allowance unavailable"
        case .connected: break
        }
        if let capacity { return capacity.decisionTitle }
        return rowsWithoutValidCapacity.isEmpty ? "No live allowance" : "No valid 7-day allowance"
    }

    private var capacityDetail: String {
        switch availability {
        case .loading: return "Waiting for the local glance projection."
        case .unavailable(let message): return message
        case .connected: break
        }
        if let capacity { return capacity.detailText }
        if rowsWithoutValidCapacity.count == 1, let row = rowsWithoutValidCapacity.first {
            return "\(row.client) · \(row.detailText)"
        }
        if !rowsWithoutValidCapacity.isEmpty {
            return "\(rowsWithoutValidCapacity.count) recording clients lack a valid 7-day reading."
        }
        return "Open Usage for recorded volume and provider limits."
    }

    private var capacityTint: Color {
        guard case .connected = availability else { return Theme.muted }
        guard let used = capacity?.usedPercent else { return Theme.muted }
        return Theme.limitColor(usedPercent: used)
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
        case .warning: return Theme.amber
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
    let detail: String
    let tint: Color
    let action: () -> Void
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        Button(action: action) {
            HStack(alignment: .top, spacing: Space.m) {
                RoundedRectangle(cornerRadius: 1, style: .continuous)
                    .fill(tint)
                    .frame(width: 3, height: 30)
                    .padding(.top, 2)
                VStack(alignment: .leading, spacing: 4) {
                    Text(eyebrow)
                        .dashboardFont(.labelCaps)
                        .tracking(Type.labelCapsTracking)
                        .foregroundStyle(Theme.muted)
                    Text(title)
                        .dashboardFont(.rowLabel)
                        .foregroundStyle(Theme.ink)
                        .lineLimit(dashboardUsesAccessibilityLayout(dynamicTypeSize) ? nil : 1)
                    Text(detail)
                        .dashboardFont(.caption)
                        .foregroundStyle(Theme.muted)
                        .lineLimit(dashboardUsesAccessibilityLayout(dynamicTypeSize) ? nil : 2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: Space.s)
                DashboardDisclosureIndicator().padding(.top, 18)
            }
            .padding(.horizontal, Space.l)
            .padding(.vertical, Space.s)
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(DashboardRowButtonStyle())
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(eyebrow), \(title), \(detail)")
        .accessibilityHint("Opens the related detail")
        .accessibilityIdentifier("dashboard.signal.\(eyebrow.lowercased().replacingOccurrences(of: " ", with: "-"))")
    }
}

private struct DashboardCardHeader<Action: View>: View {
    let title: String
    var count: Int?
    @ViewBuilder let action: () -> Action
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

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
        Group {
            if dashboardUsesAccessibilityLayout(dynamicTypeSize) {
                VStack(alignment: .leading, spacing: Space.s) {
                    titleAndCount
                    action()
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(.vertical, Space.m)
                .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                HStack(spacing: Space.s) {
                    titleAndCount
                    Spacer(minLength: Space.s)
                    action()
                }
                .frame(height: 44)
            }
        }
        .padding(.leading, Space.l)
        .padding(.trailing, 8)
    }

    private var titleAndCount: some View {
        HStack(spacing: Space.s) {
            Text(title)
                .dashboardFont(.titleCard)
                .foregroundStyle(Theme.muted)
            if let count {
                Text(String(count))
                    .dashboardFont(.dataSmallSemibold)
                    .foregroundStyle(Theme.muted)
                    .padding(.horizontal, 6)
                    .frame(minWidth: 20, minHeight: 20)
                    .background(Theme.tintNeutral, in: Capsule())
            }
        }
    }
}

private extension DashboardCardHeader where Action == EmptyView {
    init(_ title: String, count: Int? = nil) {
        self.init(title, count: count) { EmptyView() }
    }
}

enum DashboardRecentWorkPresentation: Equatable {
    case loading
    case unavailable(String)
    case empty
    case populated

    init(items: [DashboardWorkItem], total: Int?, hasLoaded: Bool, error: String?) {
        if !items.isEmpty {
            self = .populated
        } else if let error {
            self = .unavailable(error)
        } else if !hasLoaded {
            self = .loading
        } else if let total, total > 0 {
            self = .unavailable("The receipt count loaded, but no recent rows were returned.")
        } else {
            self = .empty
        }
    }
}

private struct RecentWorkCard: View {
    let items: [DashboardWorkItem]
    let totalCount: Int?
    let hasLoaded: Bool
    let error: String?
    let open: (DashboardDestination) -> Void
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    private var presentation: DashboardRecentWorkPresentation {
        DashboardRecentWorkPresentation(
            items: items,
            total: totalCount,
            hasLoaded: hasLoaded,
            error: error
        )
    }

    var body: some View {
        Card(
            padding: 0,
            fillsHeight: !dashboardUsesAccessibilityLayout(dynamicTypeSize)
        ) {
            VStack(spacing: 0) {
                DashboardCardHeader("Recent work", count: totalCount) {
                    Button { open(.work) } label: {
                        Text("View all").dashboardFont(.captionSemibold)
                    }
                    .foregroundStyle(Theme.accent)
                    .buttonStyle(QuietButtonStyle())
                    .accessibilityIdentifier("dashboard.recent-work.view-all")
                }
                Divider().overlay(Theme.hairline)

                switch presentation {
                case .loading:
                    HStack(spacing: Space.m) {
                        ProgressView().controlSize(.small).tint(Theme.muted)
                        Text("Loading recent work…")
                            .dashboardFont(.body)
                            .foregroundStyle(Theme.muted)
                    }
                    .padding(Space.xl)
                    .frame(maxWidth: .infinity, minHeight: 222, alignment: .leading)
                case .unavailable(let message):
                    DashboardEmptyState(
                        icon: "exclamationmark.triangle",
                        title: "Recent work unavailable",
                        message: message
                    )
                    .frame(minHeight: 222)
                case .empty:
                    DashboardEmptyState(
                        icon: "checklist",
                        title: "No recorded work yet",
                        message: "Set up recording to see task outcomes and evidence here."
                    )
                    .frame(minHeight: 222)
                case .populated:
                    if let error {
                        Label(
                            "Showing last loaded work · \(error)",
                            systemImage: "exclamationmark.triangle.fill"
                        )
                        .dashboardFont(.dataSmall)
                        .foregroundStyle(Theme.amber)
                        .padding(.horizontal, Space.l)
                        .padding(.vertical, Space.s)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        Divider().overlay(Theme.hairline)
                    }
                    if !dashboardUsesAccessibilityLayout(dynamicTypeSize) {
                        workColumnLabels
                        Divider().overlay(Theme.hairline)
                    }
                    ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                        RecentWorkRow(item: item) { open(.task(item.id)) }
                        if index < items.count - 1 {
                            Divider().overlay(Theme.hairline.opacity(0.72))
                        }
                    }
                }
            }
        }
    }

    private var workColumnLabels: some View {
        HStack(spacing: 12) {
            Text("Task").frame(maxWidth: .infinity, alignment: .leading)
            Text("Outcome").frame(width: 116, alignment: .leading)
            Text("Evidence").frame(width: 118, alignment: .leading)
            Text("Cost").frame(width: 68, alignment: .trailing)
            Color.clear.frame(width: 10, height: 1)
        }
        .dashboardFont(.columnHeader)
        .foregroundStyle(Theme.muted)
        .padding(.horizontal, Space.l)
        .frame(height: 30)
    }
}

private struct RecentWorkRow: View {
    let item: DashboardWorkItem
    let action: () -> Void
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        Button(action: action) {
            if dashboardUsesAccessibilityLayout(dynamicTypeSize) {
                accessibleContent
            } else {
                tableContent
            }
        }
        .buttonStyle(DashboardRowButtonStyle())
        .accessibilityLabel(
            [
                item.title,
                item.contextComponents.joined(separator: ", "),
                item.outcome,
                item.evidence,
                item.visibleCost,
            ].joined(separator: ", ")
        )
        .accessibilityHint("Opens this task in Work")
        .accessibilityIdentifier("dashboard.recent-work.task.\(item.id)")
    }

    private var tableContent: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(item.title)
                    .dashboardFont(.rowLabel)
                    .foregroundStyle(Theme.ink)
                    .lineLimit(2)
                Text(item.contextComponents.joined(separator: " · "))
                    .dashboardFont(.caption)
                    .foregroundStyle(Theme.muted)
                    .lineLimit(1)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            DecisionBadge(key: item.outcomeKey, label: item.outcome, compact: true)
                .frame(width: 116, alignment: .leading)

            evidence
                .frame(width: 118, alignment: .leading)

            Text(item.visibleCost)
                .dashboardFont(.dataSmall)
                .foregroundStyle(Theme.muted)
                .frame(width: 68, alignment: .trailing)

            DashboardDisclosureIndicator()
        }
        .padding(.horizontal, Space.l)
        .frame(minHeight: 64)
        .contentShape(Rectangle())
    }

    private var accessibleContent: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            HStack(alignment: .top, spacing: Space.m) {
                VStack(alignment: .leading, spacing: Space.xs) {
                    Text(item.title)
                        .dashboardFont(.rowLabel)
                        .foregroundStyle(Theme.ink)
                    Text(item.contextComponents.joined(separator: " · "))
                        .dashboardFont(.caption)
                        .foregroundStyle(Theme.muted)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                DashboardDisclosureIndicator()
            }

            DashboardAccessibleDecisionBadge(
                key: item.outcomeKey,
                label: item.outcome
            )

            HStack(alignment: .firstTextBaseline, spacing: Space.m) {
                evidence
                Spacer(minLength: Space.m)
                Text("Cost \(item.visibleCost)")
                    .dashboardFont(.dataSmall)
                    .foregroundStyle(Theme.muted)
            }
        }
        .padding(Space.l)
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
    }

    private var evidence: some View {
        HStack(spacing: 6) {
            if item.evidenceIsInconsistent {
                Image(systemName: "exclamationmark.triangle")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(Theme.amber)
                    .accessibilityHidden(true)
            } else if let tier = item.strongestTier {
                let style = EvidenceTierStyle.forGrade(tier)
                EvidencePip(shape: style.pip, tint: style.tint)
            } else {
                EvidencePip(shape: .hollow, tint: Theme.muted)
            }
            Text(item.evidence)
                .dashboardFont(.dataSmall)
                .foregroundStyle(item.evidenceIsInconsistent ? Theme.amber : Theme.muted)
        }
        .help(item.evidenceQualifier)
    }
}

private struct DashboardAccessibleDecisionBadge: View {
    let key: String?
    let label: String

    var body: some View {
        let tint = DecisionTintClass.forKey(key)
        Text(label)
            .dashboardFont(.captionSemibold)
            .foregroundStyle(tint.text)
            .padding(.horizontal, Space.m)
            .padding(.vertical, Space.s)
            .background(tint.wash, in: RoundedRectangle(cornerRadius: Metrics.radius))
            .overlay {
                if tint.outlined {
                    RoundedRectangle(cornerRadius: Metrics.radius)
                        .strokeBorder(tint.text.opacity(0.55), lineWidth: Metrics.borderW)
                }
            }
            .fixedSize(horizontal: false, vertical: true)
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

    /// Conservative provider headroom derived from a valid live 7-day used
    /// percentage. Rounds down so the compact title never overstates room.
    var decisionTitle: String {
        guard let usedPercent else { return client }
        if usedPercent > 100 { return "\(client) · limit exceeded" }
        if usedPercent == 100 { return "\(client) · no headroom" }
        let headroom = 100 - usedPercent
        if headroom < 1 { return "\(client) · <1% headroom" }
        return "\(client) · \(Int(headroom.rounded(.down)))% headroom"
    }

    /// Full capacity sentence: window, provenance, and reset when reported.
    var detailText: String {
        var parts = [meterCaption]
        if usedPercent != nil { parts.append("provider reported") }
        if let resetText { parts.append(resetText) }
        return parts.joined(separator: " · ")
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
        if let used = validUsed {
            // Short and window-anchored; provenance + reset time remain in the
            // signal detail sentence.
            meterCaption = String(format: "%.0f%% of 7-day limit", used)
            resetText = Theme.resetsIn(window?.resetsAt).map { "resets in \($0)" }
        } else if reportedUsed != nil {
            meterCaption = "invalid 7-day value reported"
            resetText = nil
        } else if window != nil {
            meterCaption = "7-day usage not reported"
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
        self.calibrating = plan?.calibrationState == "calibrating"
        self.calibratingDetail = plan?.stateDetail
        if let usage {
            // "7d ·" anchors figures to the fixed signal-rail window; Usage
            // remains the full range-aware destination.
            let cost = usage.costText ?? "unpriced"
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
    let rangeDays: Int
    let periodPresentation: UsagePeriodPresentation

    @State private var series: DashboardUsageSeries = .tokens
    @State private var hoveredIndex: Int?
    @State private var pinnedIndex: Int?
    @FocusState private var focusedIndex: Int?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    private var activeIndex: Int? { hoveredIndex ?? focusedIndex ?? pinnedIndex }
    private var maximum: Double { max(periods.map(series.value).max() ?? 0, 1) }

    var body: some View {
        Card(padding: 0) {
            VStack(spacing: 0) {
                header

                Divider().overlay(Theme.hairline)

                if dashboardUsesAccessibilityLayout(dynamicTypeSize) {
                    accessiblePeriodList
                } else {
                    chart
                        .padding(.horizontal, Space.l)
                        .padding(.vertical, 11)
                }
            }
        }
        .onChange(of: periods.map(\.period)) {
            hoveredIndex = nil
            pinnedIndex = nil
            focusedIndex = nil
        }
    }

    @ViewBuilder
    private var header: some View {
        if dashboardUsesAccessibilityLayout(dynamicTypeSize) {
            VStack(alignment: .leading, spacing: Space.m) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Usage history")
                        .dashboardFont(.titleCard)
                        .foregroundStyle(Theme.muted)
                    Text(
                        series.subtitle(
                            rangeDays: rangeDays,
                            periodPresentation: periodPresentation
                        )
                    )
                        .dashboardFont(.caption)
                        .foregroundStyle(Theme.muted)
                }
                Text(series.totalText(for: periods))
                    .dashboardFont(.dataSmall)
                    .foregroundStyle(Theme.muted)
                seriesControls
            }
            .padding(Space.l)
            .frame(maxWidth: .infinity, alignment: .leading)
        } else {
            HStack(spacing: Space.m) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Usage history")
                        .dashboardFont(.titleCard)
                        .foregroundStyle(Theme.muted)
                    Text(
                        series.subtitle(
                            rangeDays: rangeDays,
                            periodPresentation: periodPresentation
                        )
                    )
                        .dashboardFont(.caption)
                        .foregroundStyle(Theme.muted)
                }
                Spacer(minLength: Space.s)
                Text(series.totalText(for: periods))
                    .dashboardFont(.dataSmall)
                    .foregroundStyle(Theme.muted)
                seriesControls
            }
            .padding(.horizontal, Space.l)
            .frame(minHeight: 47)
        }
    }

    private var seriesControls: some View {
        HStack(spacing: 2) {
            ForEach(DashboardUsageSeries.allCases) { choice in
                Button {
                    withAnimation(reduceMotion ? nil : Motion.contentUpdate) {
                        series = choice
                        hoveredIndex = nil
                        pinnedIndex = nil
                    }
                } label: {
                    Text(choice.rawValue)
                        .dashboardFont(.captionSemibold)
                        .padding(.horizontal, 9)
                        .frame(minHeight: 24)
                }
                .buttonStyle(DashboardSeriesButtonStyle(selected: series == choice))
                .accessibilityAddTraits(series == choice ? .isSelected : [])
                .accessibilityIdentifier("dashboard.usage.\(choice.rawValue.lowercased())")
            }
        }
        .padding(2)
        .background(
            Theme.tintNeutral,
            in: RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
        )
    }

    private var accessiblePeriodList: some View {
        VStack(spacing: 0) {
            ForEach(Array(periods.enumerated()), id: \.offset) { index, period in
                HStack(alignment: .firstTextBaseline, spacing: Space.m) {
                    Text(period.period ?? period.shortLabel)
                        .dashboardFont(.body)
                        .foregroundStyle(Theme.ink)
                    Spacer(minLength: Space.m)
                    Text(series.valueText(for: period))
                        .dashboardFont(.dataSmallSemibold)
                        .foregroundStyle(Theme.ink)
                        .multilineTextAlignment(.trailing)
                }
                .padding(.horizontal, Space.l)
                .padding(.vertical, Space.m)
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(
                    "\(period.period ?? period.shortLabel), \(series.valueText(for: period))"
                )
                .accessibilityIdentifier("dashboard.usage.day.\(index)")

                if index < periods.count - 1 {
                    Divider().overlay(Theme.hairline).padding(.leading, Space.l)
                }
            }
        }
        .accessibilityIdentifier("dashboard.usage.accessible-list")
    }

    private var chart: some View {
        HStack(alignment: .top, spacing: Space.s) {
            VStack(alignment: .trailing) {
                Text(axisText(maximum))
                Spacer()
                Text(axisText(maximum / 2))
                Spacer()
                Text("0")
            }
            .dashboardFont(.dataSmall)
            .foregroundStyle(Theme.muted)
            .frame(width: 38, height: 130)

            GeometryReader { proxy in
                let count = max(periods.count, 1)
                let gap: CGFloat = 6
                let columnWidth = max(1, (proxy.size.width - gap * CGFloat(count - 1)) / CGFloat(count))

                ZStack(alignment: .bottomLeading) {
                    VStack(spacing: 0) {
                        Divider().overlay(Theme.hairline)
                        Spacer()
                        Divider().overlay(Theme.hairline.opacity(0.72))
                        Spacer()
                        Divider().overlay(Theme.hairline)
                    }
                    .padding(.bottom, 20)

                    HStack(alignment: .bottom, spacing: gap) {
                        ForEach(Array(periods.enumerated()), id: \.offset) { index, period in
                            VStack(spacing: 5) {
                                Button {
                                    pinnedIndex = pinnedIndex == index ? nil : index
                                } label: {
                                    VStack {
                                        Spacer(minLength: 0)
                                        RoundedRectangle(cornerRadius: 2, style: .continuous)
                                            .fill(barColor(index))
                                            .frame(height: barHeight(for: period))
                                    }
                                    // Fixed plot height so the max bar tops at
                                    // the gridline its axis label names.
                                    .frame(maxWidth: .infinity)
                                    .frame(height: 110)
                                    .contentShape(Rectangle())
                                }
                                .buttonStyle(DashboardChartBarStyle(active: activeIndex == index))
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
                                    "\(period.period ?? period.shortLabel), \(series.valueText(for: period))"
                                )
                                .accessibilityHint(periodPresentation.pinAccessibilityHint)
                                .accessibilityAddTraits(pinnedIndex == index ? .isSelected : [])
                                .accessibilityIdentifier("dashboard.usage.day.\(index)")

                                Text(period.shortLabel)
                                    .dashboardFont(.dataSmall)
                                    .foregroundStyle(Theme.muted)
                                    .lineLimit(1)
                            }
                            .frame(width: columnWidth)
                        }
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
                    .animation(
                        Motion.animatesChartGeometry(
                            bucketCount: periods.count,
                            reduceMotion: reduceMotion
                        ) ? Motion.contentUpdate : nil,
                        value: series
                    )

                    if let index = activeIndex, periods.indices.contains(index) {
                        DashboardChartTooltip(
                            date: periods[index].period ?? periods[index].shortLabel,
                            value: series.valueText(for: periods[index])
                        )
                        .position(
                            x: min(
                                max(58, columnWidth / 2 + CGFloat(index) * (columnWidth + gap)),
                                proxy.size.width - 58
                            ),
                            y: 17
                        )
                        .transition(.opacity)
                        .allowsHitTesting(false)
                    }
                }
            }
            .frame(height: 130)
        }
        .frame(minHeight: 130)
    }

    private func barColor(_ index: Int) -> Color {
        if activeIndex == index || index == periods.count - 1 { return Theme.chartBar }
        return Theme.chartBarDim
    }

    private func barHeight(for period: PeriodBucket) -> CGFloat {
        let value = series.value(for: period)
        guard value > 0 else { return 0 }
        return max(3, 110 * value / maximum)
    }

    private func axisText(_ value: Double) -> String {
        series.axisText(for: value)
    }
}

private struct DashboardChartTooltip: View {
    let date: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(date).dashboardFont(.dataSmall).foregroundStyle(Theme.muted)
            Text(value).dashboardFont(.dataSmallSemibold).foregroundStyle(Theme.ink)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 5)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
                .strokeBorder(Theme.cardLine, lineWidth: Metrics.borderW)
        )
        .fixedSize()
    }
}

private struct DashboardSeriesButtonStyle: ButtonStyle {
    let selected: Bool

    func makeBody(configuration: Configuration) -> some View {
        DashboardSeriesButtonBody(configuration: configuration, selected: selected)
    }
}

private struct DashboardSeriesButtonBody: View {
    let configuration: ButtonStyleConfiguration
    let selected: Bool
    @State private var hovering = false
    @Environment(\.isFocused) private var isFocused
    @Environment(\.isEnabled) private var isEnabled

    private var phase: ButtonInteractionPhase {
        buttonInteractionPhase(
            isEnabled: isEnabled,
            isPressed: configuration.isPressed,
            isHovering: hovering
        )
    }

    var body: some View {
        configuration.label
            .foregroundStyle(selected ? Theme.ink : Theme.muted)
            .background {
                RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
                    .fill(selected ? Theme.card : Color.clear)
                RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
                    .fill(Theme.accent.opacity(ButtonFeedback.surfaceFillOpacity(for: phase)))
            }
            .opacity(ButtonFeedback.labelOpacity(for: phase, pressed: 0.82))
            .overlay {
                if isFocused && isEnabled {
                    RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
                        .strokeBorder(Theme.accent, lineWidth: Metrics.focusW)
                }
            }
            .onHover { inside in
                withAnimation(Motion.hover) {
                    hovering = inside
                }
            }
            .animation(Motion.feedback, value: phase)
    }
}

private struct DashboardChartBarStyle: ButtonStyle {
    let active: Bool

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .opacity(configuration.isPressed ? 0.72 : 1)
            .overlay {
                if active {
                    RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
                        .strokeBorder(Theme.accent.opacity(0.55), lineWidth: Metrics.borderW)
                }
            }
            .animation(Motion.feedback, value: configuration.isPressed)
    }
}

private struct DashboardRowButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        DashboardRowButtonBody(configuration: configuration)
    }
}

private struct DashboardRowButtonBody: View {
    let configuration: ButtonStyleConfiguration
    @State private var hovering = false
    @Environment(\.isFocused) private var isFocused

    var body: some View {
        configuration.label
            .background(
                Theme.accent.opacity(configuration.isPressed ? 0.10 : (hovering ? 0.055 : 0))
            )
            .overlay {
                if isFocused {
                    RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
                        .strokeBorder(Theme.accent, lineWidth: Metrics.focusW)
                        .padding(2)
                }
            }
            .onHover { inside in
                withAnimation(Motion.hover) {
                    hovering = inside
                }
            }
            .animation(Motion.feedback, value: configuration.isPressed)
    }
}

private struct DashboardDisclosureIndicator: View {
    var body: some View {
        Image(systemName: "chevron.forward")
            .font(.system(size: 8.5, weight: .semibold))
            .foregroundStyle(Theme.muted)
            .frame(width: 10)
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
                .font(.system(size: 17, weight: .medium))
                .foregroundStyle(Theme.muted)
                .frame(width: 24)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).dashboardFont(.rowLabel).foregroundStyle(Theme.ink)
                Text(message).dashboardFont(.caption).foregroundStyle(Theme.muted)
            }
        }
        .padding(Space.l)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
