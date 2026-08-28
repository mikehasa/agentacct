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
    let cost: String
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
        if let label = task.decisionStatus.label, !label.isEmpty {
            outcome = label
        } else {
            outcome = Self.outcomeLabel(for: task.decisionStatus.key)
        }
        evidence = task.evidenceStrength.compactHeadline
        cost = Self.compactCost(task.cost)
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

    private static func compactCost(_ cost: ReceiptCost) -> String {
        guard let value = cost.estimatedCostUsd else { return "—" }
        let prefix: String
        if cost.costComplete == false {
            prefix = "~$"
        } else if cost.costComplete == true && cost.costConfidence == "client_reported" {
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
        if let taskTitle = task.title, !taskTitle.isEmpty {
            title = taskTitle
        } else {
            title = task.taskId
        }
        project = task.project
        client = task.primaryRoot?.client
        reasonKind = reason.kind
        summary = reason.summary
        nextStep = reason.nextStep
        observedAt = reason.observedAt
        handedOff = task.handedOff
        switch reason.source {
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
private enum DashboardClipboard {
    static func copy(_ text: String) -> Bool {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        return pasteboard.setString(text, forType: .string)
    }
}

enum DashboardAttentionPresentation: Equatable {
    case loading
    case unavailable(String)
    case clear
    case focus(item: DashboardAttentionItem, total: Int)
    case inconsistent(total: Int)

    init(payload: V1AttentionPayload?, error: String?) {
        guard let payload else {
            self = error.map(Self.unavailable) ?? .loading
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
        case .unavailable: return "Refresh to retry"
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
        case .tokens: return Double(period.freshTokens ?? 0)
        case .cost: return period.estimatedCostUsd ?? 0
        }
    }

    func valueText(for period: PeriodBucket) -> String {
        switch self {
        case .tokens:
            guard let value = period.freshTokens else { return "—" }
            return UsageTotals.compact(value)
        case .cost:
            return period.costText
        }
    }

    func totalText(for periods: [PeriodBucket]) -> String {
        switch self {
        case .tokens:
            let available = periods.compactMap(\.freshTokens)
            guard !available.isEmpty else { return "—" }
            let prefix = available.count == periods.count ? "" : "~"
            return "\(prefix)\(UsageTotals.compact(available.reduce(0, +))) total"
        case .cost:
            let available = periods.compactMap(\.estimatedCostUsd)
            guard !available.isEmpty else { return "—" }
            let complete = available.count == periods.count
                && periods.allSatisfy { $0.costComplete == true }
            let reported = complete && periods.allSatisfy {
                ["client_reported", "provider_billed"].contains($0.costConfidence ?? "")
            }
            let prefix = reported ? "$" : (complete ? "≈$" : "~$")
            return "\(Fmt.dollars(available.reduce(0, +), prefix: prefix)) total"
        }
    }

    func subtitle(dayCount: Int) -> String {
        switch self {
        case .tokens: return "Fresh tokens · last \(dayCount) days · client reported"
        case .cost: return "Estimated cost · last \(dayCount) days · pricing-table basis"
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
            return
        case .unavailable(let message):
            title = "Active work unavailable"
            detail = message
            promotesInactivity = false
            return
        case .connected:
            break
        }

        guard !sessions.isEmpty else {
            title = "No active work"
            detail = "Recorded agent activity will appear here."
            promotesInactivity = false
            return
        }

        let activeCount = sessions.count
        let validActivity = sessions.compactMap { session -> (RecentSession, TimeInterval)? in
            guard let lastActivityAt = session.lastActivityAt, lastActivityAt > 0 else { return nil }
            let elapsed = now.timeIntervalSince1970 - lastActivityAt
            guard elapsed >= 0, elapsed.isFinite else { return nil }
            return (session, elapsed)
        }

        if let oldestVisible = validActivity.max(by: { $0.1 < $1.1 }), oldestVisible.1 >= 15 * 60 {
            title = "Session activity last seen \(Self.elapsedText(oldestVisible.1)) ago"
            detail = "\(Self.sessionLabel(oldestVisible.0)) · \(activeCount) active session\(activeCount == 1 ? "" : "s") recorded"
            promotesInactivity = true
            return
        }

        title = "\(activeCount) active session\(activeCount == 1 ? "" : "s")"
        if let mostRecent = validActivity.min(by: { $0.1 < $1.1 }) {
            detail = "\(Self.sessionLabel(mostRecent.0)) · activity \(Self.elapsedText(mostRecent.1)) ago"
        } else {
            detail = "Activity time unavailable for the recorded session\(activeCount == 1 ? "." : "s.")"
        }
        promotesInactivity = false
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
    @EnvironmentObject var dashboard: DashboardStore
    @EnvironmentObject var glance: GlanceState
    @EnvironmentObject var selection: AppSelection

    private var recentWork: [DashboardWorkItem] {
        dashboard.receiptTasks.prefix(3).map(DashboardWorkItem.init)
    }

    private var liveLimits: [LimitEntry] {
        guard case .connected(let snapshot) = glance.phase else { return [] }
        return snapshot.glance.limits.filter { $0.stale != true }
    }

    /// The glance 7-day per-client usage slice — what lets EVERY recording
    /// agent appear on the plan card, not only the ones reporting limits.
    private var glanceUsageByClient: [GlanceClientUsage] {
        guard case .connected(let snapshot) = glance.phase else { return [] }
        return snapshot.glance.usage.byClient ?? []
    }

    /// Clients whose only limit readings are STALE — hidden from the live
    /// meters, but "no limits reported" would be affirmatively false for them.
    private var staleLimitClients: Set<String> {
        guard case .connected(let snapshot) = glance.phase else { return [] }
        let live = Set(liveLimits.compactMap(\.client))
        return Set(
            snapshot.glance.limits
                .filter { $0.stale == true }
                .compactMap(\.client)
        ).subtracting(live)
    }

    private var recentSessions: [RecentSession] {
        guard case .connected(let snapshot) = glance.phase else { return [] }
        return snapshot.glance.recentSessions.filter {
            isActiveWorkStatus($0.status)
        }
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
        ScrollBox {
            VStack(alignment: .leading, spacing: Space.l) {
                DashboardShiftBriefHeader(
                    payload: dashboard.attention,
                    error: dashboard.attentionError
                )

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
                    totalCount: dashboard.totalReceiptTasks ?? dashboard.receiptTasks.count
                ) { destination in
                    selection.open(destination)
                }

                if let periods = dashboard.usage?.byPeriod, periods.count > 1 {
                    DashboardUsageChart(periods: periods)
                }
            }
            .padding(Space.gutter)
        }
        .overlay(alignment: .bottom) {
            if let error = dashboard.errorText ?? dashboard.receiptListError {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(Type.caption)
                    .foregroundStyle(Theme.coral)
                    .padding(Space.s)
                    .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous))
                    .padding(.bottom, 10)
            }
        }
    }

    private func splitRow<Left: View, Right: View>(
        @ViewBuilder left: () -> Left,
        @ViewBuilder right: () -> Right
    ) -> some View {
        ViewThatFits(in: .horizontal) {
            DashboardSplitLayout(leftFraction: 8 / 12, spacing: Space.l) {
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

    private var presentation: DashboardAttentionPresentation {
        DashboardAttentionPresentation(payload: payload, error: error)
    }

    var body: some View {
        HStack(alignment: .lastTextBaseline, spacing: Space.xl) {
            VStack(alignment: .leading, spacing: 5) {
                Text("SHIFT BRIEF")
                    .font(Type.labelCaps)
                    .tracking(Type.labelCapsTracking)
                    .foregroundStyle(Theme.accent)
                Text(presentation.dashboardHeadline)
                    .font(Type.titlePage)
                    .tracking(Type.titlePageTracking)
                    .foregroundStyle(Theme.ink)
                    .lineLimit(2)
            }
            Spacer(minLength: Space.m)
            Text(presentation.dashboardStatus)
                .font(Type.dataSmall)
                .foregroundStyle(presentation.dashboardStatusIsWarning ? Theme.amber : Theme.muted)
                .multilineTextAlignment(.trailing)
        }
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("dashboard.shift-brief.header")
    }
}

private struct DashboardAttentionBriefCard: View {
    let payload: V1AttentionPayload?
    let error: String?
    let open: (DashboardDestination) -> Void
    @State private var copiedBriefText: String?
    @State private var failedBriefText: String?
    @State private var copyFeedbackToken: UUID?

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
        Card(padding: 0, fillsHeight: true) {
            HStack(spacing: 0) {
                Rectangle()
                    .fill(tint)
                    .frame(width: 5)
                    .accessibilityHidden(true)
                content
                    .padding(Space.xl)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
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
                        .font(Type.titleSection)
                        .foregroundStyle(Theme.ink)
                    Text("Loading the complete review projection; no clear-state claim is shown yet.")
                        .font(Type.body)
                        .foregroundStyle(Theme.muted)
                }
            }
            .frame(maxHeight: .infinity, alignment: .center)
        }
    }

    private func focusContent(total: Int, focus: DashboardAttentionItem) -> some View {
        let brief = DashboardActionBrief(focus: focus)
        let copySucceeded = copiedBriefText == brief.text
        let copyFailed = failedBriefText == brief.text
        return VStack(alignment: .leading, spacing: Space.l) {
            HStack(spacing: Space.s) {
                Text("PRIMARY ATTENTION")
                    .font(Type.labelCaps)
                    .tracking(Type.labelCapsTracking)
                    .foregroundStyle(tint)
                Text("1 OF \(total)")
                    .font(Type.dataSmallSemibold)
                    .foregroundStyle(Theme.muted)
                    .padding(.horizontal, 7)
                    .frame(minHeight: 21)
                    .background(Theme.tintNeutral, in: Capsule())
                Spacer(minLength: Space.s)
                if total > 1 {
                    Button("View queue") { open(.reviewQueue) }
                        .font(Type.captionSemibold)
                        .foregroundStyle(Theme.accent)
                        .buttonStyle(QuietButtonStyle())
                        .accessibilityIdentifier("dashboard.shift-brief.view-queue")
                }
            }

            VStack(alignment: .leading, spacing: 7) {
                let context = [focus.project, focus.client].compactMap { $0 }
                if !context.isEmpty {
                    Text(context.joined(separator: " · "))
                        .font(Type.dataSmall)
                        .foregroundStyle(Theme.muted)
                }
                Text(focus.summary)
                    .font(Type.body)
                    .foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }

            DashboardProofline(focus: focus)

            VStack(alignment: .leading, spacing: 5) {
                Text("RECORDED NEXT STEP")
                    .font(Type.labelCaps)
                    .tracking(Type.labelCapsTracking)
                    .foregroundStyle(Theme.muted)
                Text(focus.nextStep ?? "No next step recorded.")
                    .font(Type.body)
                    .foregroundStyle(focus.nextStep == nil ? Theme.muted : Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(Space.m)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.tintNeutral, in: RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous))

            HStack(spacing: Space.s) {
                Button {
                    open(.task(focus.id))
                } label: {
                    Label("Review evidence", systemImage: "doc.text.magnifyingglass")
                        .font(Type.captionSemibold)
                }
                .buttonStyle(.borderedProminent)
                .tint(Theme.accent)
                .accessibilityHint("Opens this task in Work")
                .accessibilityIdentifier("dashboard.shift-brief.review-evidence")

                Button {
                    let feedbackToken = UUID()
                    copyFeedbackToken = feedbackToken
                    if DashboardClipboard.copy(brief.text) {
                        copiedBriefText = brief.text
                        failedBriefText = nil
                    } else {
                        copiedBriefText = nil
                        failedBriefText = brief.text
                    }
                    Task { @MainActor in
                        try? await Task.sleep(for: .seconds(2))
                        guard copyFeedbackToken == feedbackToken else { return }
                        copiedBriefText = nil
                        failedBriefText = nil
                        copyFeedbackToken = nil
                    }
                } label: {
                    Label(
                        copySucceeded ? "Copied" : (copyFailed ? "Copy failed" : brief.buttonTitle),
                        systemImage: copySucceeded ? "checkmark" : "doc.on.doc"
                    )
                    .font(Type.captionSemibold)
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

    var body: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 0) {
                fact(label: "RECORDED REASON", value: focus.reasonLabel)
                proofRule
                fact(label: "OBSERVED", value: focus.recency ?? "Time unavailable")
                proofRule
                fact(label: "PROVENANCE", value: focus.sourceLabel ?? "Source unavailable")
            }
            VStack(alignment: .leading, spacing: Space.s) {
                fact(label: "RECORDED REASON", value: focus.reasonLabel)
                fact(label: "OBSERVED", value: focus.recency ?? "Time unavailable")
                fact(label: "PROVENANCE", value: focus.sourceLabel ?? "Source unavailable")
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

    private func fact(label: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label)
                .font(Type.labelCaps)
                .tracking(Type.labelCapsTracking)
                .foregroundStyle(Theme.muted)
            Text(value)
                .font(Type.dataSmallSemibold)
                .foregroundStyle(Theme.ink)
                .lineLimit(1)
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
                    .font(Type.labelCaps)
                    .tracking(Type.labelCapsTracking)
                    .foregroundStyle(Theme.green)
            }
            Text("No recorded work needs review.")
                .font(Type.titleSection)
                .tracking(Type.titleSectionTracking)
                .foregroundStyle(Theme.ink)
            Text("No current failed check, failed step, or unresolved blocker was found across the complete attention projection.")
                .font(Type.body)
                .foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
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
                .font(Type.titleSection)
                .tracking(Type.titleSectionTracking)
                .foregroundStyle(Theme.ink)
            Text(message)
                .font(Type.body)
                .foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
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

    var body: some View {
        Card(padding: 0, fillsHeight: true) {
            VStack(spacing: 0) {
                DashboardCardHeader("Signal rail")
                Divider().overlay(Theme.hairline)
                DashboardSignalRow(
                    eyebrow: "WORKING NOW",
                    title: active.title,
                    detail: active.detail,
                    tint: active.promotesInactivity ? Theme.amber : (sessions.isEmpty ? Theme.muted : Theme.accent),
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
        if ingestion?.state == "healthy" { return "Sources healthy" }
        if let state = ingestion?.state, !state.isEmpty {
            return state.replacingOccurrences(of: "_", with: " ").capitalized
        }
        return "Source status unavailable"
    }

    private var ingestionDetail: String {
        if let issueCount = ingestion?.issues?.count, issueCount > 0 {
            return "\(issueCount) recorded issue\(issueCount == 1 ? "" : "s") · inspect Sources"
        }
        if let last = agoText(ingestion?.lastSuccessAt) { return "Last successful ingest \(last)" }
        return ingestionError ?? "Open Sources for the current ingestion record."
    }

    private var ingestionTint: Color {
        if ingestion?.state == "healthy" { return Theme.green }
        return ingestion == nil ? Theme.muted : Theme.amber
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

    var body: some View {
        Button(action: action) {
            HStack(alignment: .top, spacing: Space.m) {
                RoundedRectangle(cornerRadius: 1, style: .continuous)
                    .fill(tint)
                    .frame(width: 3, height: 30)
                    .padding(.top, 2)
                VStack(alignment: .leading, spacing: 4) {
                    Text(eyebrow)
                        .font(Type.labelCaps)
                        .tracking(Type.labelCapsTracking)
                        .foregroundStyle(Theme.muted)
                    Text(title)
                        .font(Type.rowLabel)
                        .foregroundStyle(Theme.ink)
                        .lineLimit(1)
                    Text(detail)
                        .font(Type.caption)
                        .foregroundStyle(Theme.muted)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: Space.s)
                DashboardDisclosureIndicator().padding(.top, 18)
            }
            .padding(.horizontal, Space.l)
            .padding(.vertical, Space.m)
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
                .font(Type.titleCard)
                .foregroundStyle(Theme.muted)
            if let count {
                Text(String(count))
                    .font(Type.dataSmallSemibold)
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

private struct RecentWorkCard: View {
    let items: [DashboardWorkItem]
    let totalCount: Int
    let open: (DashboardDestination) -> Void

    var body: some View {
        Card(padding: 0, fillsHeight: true) {
            VStack(spacing: 0) {
                DashboardCardHeader("Recent work", count: totalCount) {
                    Button { open(.work) } label: {
                        Text("View all").font(Type.captionSemibold)
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
        .font(Face.sansFont(12, .medium))
        .foregroundStyle(Theme.muted)
        .padding(.horizontal, Space.l)
        .frame(height: 30)
    }
}

private struct RecentWorkRow: View {
    let item: DashboardWorkItem
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(item.title)
                        .font(Type.rowLabel)
                        .foregroundStyle(Theme.ink)
                        .lineLimit(2)
                    Text([item.client, item.recency].compactMap { $0 }.joined(separator: " · "))
                        .font(Type.caption)
                        .foregroundStyle(Theme.muted)
                        .lineLimit(1)
                }
                .frame(maxWidth: .infinity, alignment: .leading)

                // Decision axis: a pip-less tinted badge (a filled dot here
                // read as the independently-checked evidence pip).
                DecisionBadge(key: item.outcomeKey, label: item.outcome, compact: true)
                    .frame(width: 116, alignment: .leading)

                // Evidence axis: the strongest tier's pip shape + the ratio.
                HStack(spacing: 6) {
                    if let tier = item.strongestTier {
                        let style = EvidenceTierStyle.forGrade(tier)
                        EvidencePip(shape: style.pip, tint: style.tint)
                    } else {
                        EvidencePip(shape: .hollow, tint: Theme.muted)
                    }
                    Text(item.evidence)
                        .font(Type.dataSmall)
                        .foregroundStyle(Theme.muted)
                        .lineLimit(1)
                }
                .frame(width: 118, alignment: .leading)

                Text(item.cost == "—" ? "unpriced" : item.cost)
                    .font(Type.dataSmall)
                    .foregroundStyle(Theme.muted)
                    .frame(width: 68, alignment: .trailing)

                DashboardDisclosureIndicator()
            }
            .padding(.horizontal, Space.l)
            .frame(minHeight: 64)
            .contentShape(Rectangle())
        }
        .buttonStyle(DashboardRowButtonStyle())
        .accessibilityLabel(
            "\(item.title), \(item.outcome), \(item.evidence), \(item.cost)"
        )
        .accessibilityHint("Opens this task in Work")
        .accessibilityIdentifier("dashboard.recent-work.task.\(item.id)")
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
        } else if limit != nil {
            meterCaption = "no 7-day window reported"
            resetText = nil
        } else if staleLimit {
            // A stale reading is hidden, not never-reported — say so.
            meterCaption = "limit reading stale — see Usage"
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

private struct DashboardUsageChart: View {
    let periods: [PeriodBucket]

    @State private var series: DashboardUsageSeries = .tokens
    @State private var hoveredIndex: Int?
    @State private var pinnedIndex: Int?
    @FocusState private var focusedIndex: Int?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var activeIndex: Int? { hoveredIndex ?? focusedIndex ?? pinnedIndex }
    private var maximum: Double { max(periods.map(series.value).max() ?? 0, 1) }

    var body: some View {
        Card(padding: 0) {
            VStack(spacing: 0) {
                HStack(spacing: Space.m) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Usage history")
                            .font(Type.titleCard)
                            .foregroundStyle(Theme.muted)
                        Text(series.subtitle(dayCount: periods.count))
                            .font(Type.caption)
                            .foregroundStyle(Theme.muted)
                    }
                    Spacer(minLength: Space.s)
                    Text(series.totalText(for: periods))
                        .font(Type.dataSmall)
                        .foregroundStyle(Theme.muted)
                    HStack(spacing: 2) {
                        ForEach(DashboardUsageSeries.allCases) { choice in
                            Button {
                                withAnimation(reduceMotion ? nil : Motion.contentUpdate) {
                                    series = choice
                                    hoveredIndex = nil
                                    pinnedIndex = nil
                                }
                            } label: {
                                Text(choice.rawValue).font(Type.captionSemibold)
                                    .padding(.horizontal, 9)
                                    .frame(height: 24)
                            }
                            .buttonStyle(DashboardSeriesButtonStyle(selected: series == choice))
                            .accessibilityAddTraits(series == choice ? .isSelected : [])
                            .accessibilityIdentifier("dashboard.usage.\(choice.rawValue.lowercased())")
                        }
                    }
                    .padding(2)
                    .background(Theme.tintNeutral, in: RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous))
                }
                .padding(.horizontal, Space.l)
                .frame(minHeight: 47)

                Divider().overlay(Theme.hairline)

                chart
                    .padding(.horizontal, Space.l)
                    .padding(.vertical, 11)
            }
        }
        .onChange(of: periods.map(\.period)) {
            hoveredIndex = nil
            pinnedIndex = nil
            focusedIndex = nil
        }
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
            .font(Type.dataSmall)
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
                                    withAnimation(reduceMotion ? nil : Motion.hover) {
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
                                .accessibilityHint("Pins or clears this day's value")
                                .accessibilityAddTraits(pinnedIndex == index ? .isSelected : [])
                                .accessibilityIdentifier("dashboard.usage.day.\(index)")

                                Text(period.shortLabel)
                                    .font(Type.dataSmall)
                                    .foregroundStyle(Theme.muted)
                                    .lineLimit(1)
                            }
                            .frame(width: columnWidth)
                        }
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)

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
        switch series {
        case .tokens: return UsageTotals.compact(Int(value))
        case .cost: return Fmt.dollars(value)
        }
    }
}

private struct DashboardChartTooltip: View {
    let date: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(date).font(Type.dataSmall).foregroundStyle(Theme.muted)
            Text(value).font(Type.dataSmallSemibold).foregroundStyle(Theme.ink)
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
    @Environment(\.isFocused) private var isFocused

    var body: some View {
        configuration.label
            .foregroundStyle(selected ? Theme.ink : Theme.muted)
            .background(
                selected ? Theme.card : (configuration.isPressed ? Theme.hairline.opacity(0.6) : Color.clear),
                in: RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
            )
            .overlay {
                if isFocused {
                    RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
                        .strokeBorder(Theme.accent, lineWidth: Metrics.focusW)
                }
            }
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
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

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
                withAnimation(reduceMotion ? nil : Motion.hover) {
                    hovering = inside
                }
            }
            .animation(reduceMotion ? nil : Motion.feedback, value: configuration.isPressed)
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
                Text(title).font(Type.rowLabel).foregroundStyle(Theme.ink)
                Text(message).font(Type.caption).foregroundStyle(Theme.muted)
            }
        }
        .padding(Space.l)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
