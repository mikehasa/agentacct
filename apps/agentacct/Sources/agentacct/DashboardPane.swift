import SwiftUI

// The dashboard answers three questions in order: what happened, what needs
// review, and what resources it used. Work Receipt summaries lead because they
// keep claimed outcome and supporting evidence visibly separate. Plan and
// usage remain available as decision context, not as the product's headline.

struct DashboardWorkItem: Identifiable {
    let id: String
    let title: String
    let client: String
    let lastActivityAt: Double?
    let outcome: String
    let outcomeKey: String
    let outcomeSource: String?
    let evidence: String
    let evidenceQualifier: String
    let evidenceIsInconsistent: Bool
    let failedChecks: Int
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
        outcomeSource = Self.outcomeSourceLabel(task.decisionStatus.assertedBy)
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

    /// Strongest evidence tier present (drives the row's tier pip).
    var strongestTier: String? {
        gradeable ? (strongestTierKey ?? "unchecked") : nil
    }

    /// A superseded finding's failed check belongs to the finding that
    /// superseded it — it never resurfaces as needing review (keeps this
    /// predicate agreeing with the Work tab's Attention bucket).
    /// Keys whose failed checks no longer demand review: a later same-scope
    /// pass superseded them, or a human explicitly resolved the finding.
    private static let settledFindingKeys: Set<String> = [
        "finding_superseded", "finding_resolved_by_user",
    ]

    var needsReview: Bool {
        (failedChecks > 0 && !Self.settledFindingKeys.contains(outcomeKey))
            || ["finding", "failed", "blocked"].contains(outcomeKey)
    }

    var hasFinding: Bool {
        (failedChecks > 0 && !Self.settledFindingKeys.contains(outcomeKey))
            || ["finding", "failed"].contains(outcomeKey)
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

    private static func outcomeSourceLabel(_ source: String?) -> String? {
        switch source {
        case "agent_report": return "agent reported"
        case "machine": return "machine checked"
        case "human": return "human reviewed"
        case "inferred": return "state inferred"
        default: return nil
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

struct DashboardPane: View {
    @EnvironmentObject var dashboard: DashboardStore
    @EnvironmentObject var glance: GlanceState
    @EnvironmentObject var selection: AppSelection
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

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

    private var todayUsage: UsageTotals? {
        guard case .connected(let snapshot) = glance.phase else { return nil }
        return snapshot.glance.usage.windows.first { $0.label == "today" }?.totals
    }

    private var recentSessions: [RecentSession] {
        guard case .connected(let snapshot) = glance.phase else { return [] }
        return snapshot.glance.recentSessions.filter {
            isActiveWorkStatus($0.status)
        }
    }

    private var attentionItems: [DashboardWorkItem] {
        let items = dashboard.receiptTasks.map(DashboardWorkItem.init)
        // Failed evidence is the more urgent review target. Preserve API order
        // inside each group so equal-priority tasks remain stable.
        return items.filter { $0.needsReview && $0.hasFinding }
            + items.filter { $0.needsReview && !$0.hasFinding }
    }

    var body: some View {
        ScrollBox {
            VStack(alignment: .leading, spacing: Space.m) {
                splitRow {
                    RecentWorkCard(
                        items: recentWork,
                        totalCount: dashboard.totalReceiptTasks ?? dashboard.receiptTasks.count
                    ) { destination in
                        selection.open(destination)
                    }
                } right: {
                    NeedsReviewCard(items: attentionItems) { destination in
                        selection.open(destination)
                    }
                }

                splitRow {
                    ActiveWorkCard(sessions: recentSessions) { destination in
                        selection.open(destination)
                    }
                } right: {
                    PlanAndUsageCard(
                        rows: DashboardAgentPlanRow.rows(
                            limits: liveLimits,
                            staleClients: staleLimitClients,
                            planClients: dashboard.planClients,
                            usage: glanceUsageByClient
                        ),
                        today: todayUsage,
                        onViewLimits: { selection.open(.limits) }
                    )
                }

                if let periods = dashboard.usage?.byPeriod, periods.count > 1 {
                    DashboardUsageChart(periods: periods)
                }
            }
            .padding(Space.gutter)
        }
        .overlay(alignment: .bottom) {
            if let error = presentedError {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(Type.caption)
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
        ViewThatFits(in: .horizontal) {
            DashboardSplitLayout(leftFraction: 7 / 12, spacing: Space.m) {
                left()
                right()
            }
            // The 960 pt minimum window leaves roughly 920 pt after canvas
            // padding; stack there rather than squeezing the evidence columns.
            // The standard 1120 pt review window still uses the split grid.
            .frame(minWidth: 1_000)

            VStack(spacing: Space.m) {
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
                        .font(Type.dataSmall)
                        .foregroundStyle(item.evidenceIsInconsistent ? Theme.amber : Theme.muted)
                        .lineLimit(1)
                }
                .frame(width: 118, alignment: .leading)
                .help(item.evidenceQualifier)

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

private struct NeedsReviewCard: View {
    let items: [DashboardWorkItem]
    let open: (DashboardDestination) -> Void

    private var visibleItems: [DashboardWorkItem] { Array(items.prefix(2)) }

    var body: some View {
        Card(padding: 0, fillsHeight: true) {
            VStack(spacing: 0) {
                DashboardCardHeader("Needs review", count: items.count) {
                    if items.count > visibleItems.count {
                        Button { open(.work) } label: {
                            Text("View all").font(Type.captionSemibold)
                        }
                        .foregroundStyle(Theme.accent)
                        .buttonStyle(QuietButtonStyle())
                        .accessibilityIdentifier("dashboard.review.view-all")
                    }
                }
                Divider().overlay(Theme.hairline)

                if visibleItems.isEmpty {
                    DashboardEmptyState(
                        icon: "checkmark.circle.fill",
                        title: "All clear",
                        message: "No blocked work or failed checks."
                    )
                    .frame(minHeight: 222)
                } else {
                    ForEach(Array(visibleItems.enumerated()), id: \.element.id) { index, item in
                        DashboardAttentionRow(item: item) { open(.task(item.id)) }
                        if index < visibleItems.count - 1 {
                            Divider().overlay(Theme.hairline.opacity(0.72))
                        }
                    }
                    Spacer(minLength: 0)
                }
            }
        }
    }
}

private struct DashboardAttentionRow: View {
    let item: DashboardWorkItem
    let action: () -> Void

    private var tint: Color { item.hasFinding ? Theme.coral : Theme.amber }
    private var icon: String { item.hasFinding ? "xmark.circle.fill" : "hand.raised.circle.fill" }
    private var status: String {
        if item.failedChecks > 0 {
            return "Open finding · \(item.failedChecks) failed check\(item.failedChecks == 1 ? "" : "s")"
        }
        return [item.outcome, item.outcomeSource].compactMap { $0 }.joined(separator: " · ")
    }

    var body: some View {
        Button(action: action) {
            HStack(spacing: Space.m) {
                Image(systemName: icon)
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(tint)
                    .frame(width: 24, height: 24)
                VStack(alignment: .leading, spacing: 4) {
                    Text(item.title)
                        .font(Type.rowLabel)
                        .foregroundStyle(Theme.ink)
                        .lineLimit(2)
                    Text(status)
                        .font(Type.caption)
                        .foregroundStyle(tint)
                        .lineLimit(1)
                }
                Spacer(minLength: Space.s)
                DashboardDisclosureIndicator()
            }
            .padding(.horizontal, Space.l)
            .frame(minHeight: 95)
            .contentShape(Rectangle())
        }
        .buttonStyle(DashboardRowButtonStyle())
        .accessibilityLabel("\(item.title), \(status)")
        .accessibilityHint("Opens this task in Work")
        .accessibilityIdentifier("dashboard.review.task.\(item.id)")
    }
}

private struct ActiveWorkCard: View {
    let sessions: [RecentSession]
    let open: (DashboardDestination) -> Void

    var body: some View {
        Card(padding: 0, fillsHeight: true) {
            VStack(spacing: 0) {
                DashboardCardHeader("Active work", count: sessions.count) {
                    Button { open(.work) } label: {
                        Text("View all").font(Type.captionSemibold)
                    }
                    .foregroundStyle(Theme.accent)
                    .buttonStyle(QuietButtonStyle())
                    .accessibilityIdentifier("dashboard.active-work.view-all")
                }
                Divider().overlay(Theme.hairline)
                if sessions.isEmpty {
                    DashboardEmptyState(
                        icon: "pause.circle",
                        title: "No active work",
                        message: "Current agent activity will appear here."
                    )
                    .frame(minHeight: 104)
                } else {
                    ForEach(Array(sessions.prefix(2).enumerated()), id: \.offset) { index, session in
                        DashboardSessionRow(session: session) {
                            open(.session("\(session.client)::\(session.sessionId)"))
                        }
                        if index < min(sessions.count, 2) - 1 {
                            Divider().overlay(Theme.hairline.opacity(0.72)).padding(.leading, 36)
                        }
                    }
                }
            }
        }
    }
}

/// Per-agent presentation for the Plan and usage card. Every recording agent
/// gets a row stating only what IT can prove: a meter only from a
/// provider-reported 7-day percent, the hatched track (and words) when a
/// client reports no limit, a calibrating chip only while a plan fit is
/// genuinely pending, and the 7-day volume/cost from the glance cube. The old
/// card elected a single "least headroom" account, which read as favoritism —
/// The merged Usage surface remains the full per-window destination.
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

    /// The row's full sentence for hover/accessibility: the caption, the
    /// provenance (the caption itself stays short — the card footer states
    /// provenance once for every meter), and the reset time when reported.
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
        self.usedPercent = window?.usedPercent
        if let used = window?.usedPercent {
            // Short and window-anchored; provenance + reset time ride
            // hover/accessibility and the card footer (a truncated caption
            // would silently drop words).
            meterCaption = String(format: "%.0f%% of 7-day limit", used)
            resetText = Theme.resetsIn(window?.resetsAt).map { "resets in \($0)" }
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
            // "7d ·" anchors the figures to the card's window (the Usage
            // pane's picker never moves these rows); the footer names the
            // fresh-token basis.
            let cost = usage.costText ?? "unpriced"
            let tokens = usage.freshTokens.map { UsageTotals.compact($0) }
            usageText = "7d · " + ([cost] + (tokens.map { [$0] } ?? [])).joined(separator: " · ")
        } else {
            usageText = nil
        }
    }

    /// Row set for the card: every non-stale limit client ∪ every client with
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
            (entry.windows ?? []).first { $0.kind == "7d" }?.usedPercent
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

private struct PlanAndUsageCard: View {
    let rows: [DashboardAgentPlanRow]
    let today: UsageTotals?
    let onViewLimits: () -> Void

    var body: some View {
        Card(padding: 0, fillsHeight: true) {
            VStack(spacing: 0) {
                DashboardCardHeader("Plan and usage", count: rows.count > 1 ? rows.count : nil) {
                    Button(action: onViewLimits) {
                        Text("View usage & limits").font(Type.captionSemibold)
                    }
                    .foregroundStyle(Theme.accent)
                    .buttonStyle(QuietButtonStyle())
                    .accessibilityIdentifier("dashboard.plan.view-limits")
                }
                Divider().overlay(Theme.hairline)
                if rows.isEmpty {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("No live provider data")
                            .font(Type.rowLabel).foregroundStyle(Theme.muted)
                        Text("Agent rows appear once usage or a provider limit is recorded.")
                            .font(Type.caption).foregroundStyle(Theme.muted)
                    }
                    .padding(Space.l)
                    .frame(maxWidth: .infinity, alignment: .leading)
                } else {
                    ForEach(Array(rows.enumerated()), id: \.element.id) { index, row in
                        if index > 0 {
                            Rectangle().fill(Theme.hairline).frame(height: 1)
                                .padding(.horizontal, Space.l)
                        }
                        AgentPlanRowView(row: row)
                    }
                }
                Spacer(minLength: 0)
                Divider().overlay(Theme.hairline)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Today · all agents")
                        .font(Type.caption)
                        .foregroundStyle(Theme.muted)
                    Text("\(today?.costText ?? "—") · \(today?.tokensText ?? "—")")
                        .font(Face.monoFont(14, .semibold))
                        .foregroundStyle(Theme.ink)
                    Text("Pricing estimate · fresh tokens client-reported · meters provider-reported")
                        .font(Type.caption)
                        .foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.horizontal, Space.l)
                .padding(.vertical, Space.m)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }
}

private struct AgentPlanRowView: View {
    let row: DashboardAgentPlanRow

    var body: some View {
        HStack(alignment: .center, spacing: Space.l) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(row.client)
                        .font(Type.rowLabel).foregroundStyle(Theme.ink)
                        .lineLimit(1)
                    if let plan = row.planType {
                        Chip(text: plan, tint: Theme.muted)
                    }
                    if row.calibrating {
                        Chip(text: "calibrating", tint: Theme.amber)
                            .fixedSize()
                            .help(row.calibratingDetail
                                  ?? "Weekly plan % is still calibrating for this client — see Usage")
                    }
                }
                Text(row.meterCaption)
                    .font(Type.caption).foregroundStyle(Theme.muted)
                    .lineLimit(1).truncationMode(.tail)
                    .help(row.detailText)
            }
            Spacer(minLength: Space.m)
            VStack(alignment: .trailing, spacing: 4) {
                Group {
                    if let used = row.usedPercent {
                        LimitMeter(usedPercent: used)
                    } else {
                        HatchedTrack()
                    }
                }
                .frame(width: 108)
                // 7-day volume with the cost grammar; absence stays named.
                Text(row.usageText ?? "no usage this week")
                    .font(Type.dataSmall)
                    .foregroundStyle(row.usageText == nil ? Theme.muted : Theme.ink)
                    .lineLimit(1)
            }
        }
        .padding(.horizontal, Space.l)
        .padding(.vertical, 10)
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("dashboard.plan.agent.\(row.client)")
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
                                series = choice
                                hoveredIndex = nil
                                pinnedIndex = nil
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

private struct DashboardSessionRow: View {
    let session: RecentSession
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 10) {
                RoundedRectangle(cornerRadius: 1)
                .fill(Theme.statusColor(session.status))
                .frame(width: 3, height: 14)
                VStack(alignment: .leading, spacing: 3) {
                    Text(session.title ?? "\(session.client) · \(session.shortSessionId)")
                        .font(Type.rowLabel)
                        .foregroundStyle(Theme.ink)
                        .lineLimit(1)
                    Text([Optional(session.client), session.status.map(statusLabel), agoText(session.lastActivityAt)]
                        .compactMap { $0 }
                        .joined(separator: " · "))
                        .font(Type.caption)
                        .foregroundStyle(Theme.muted)
                        .lineLimit(1)
                }
                Spacer(minLength: 8)
                DashboardDisclosureIndicator()
            }
            .padding(.horizontal, Space.l)
            .frame(minHeight: 66)
            .contentShape(Rectangle())
        }
        .buttonStyle(DashboardRowButtonStyle())
        .accessibilityLabel(
            "\(session.title ?? session.shortSessionId), \(session.client), \(session.status ?? "status unavailable")"
        )
        .accessibilityHint("Opens this work session")
        .accessibilityIdentifier("dashboard.active-work.session.\(session.sessionId)")
    }

    private func statusLabel(_ status: String) -> String {
        status.replacingOccurrences(of: "_", with: " ")
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
                Text(title).font(Type.rowLabel).foregroundStyle(Theme.ink)
                Text(message).font(Type.caption).foregroundStyle(Theme.muted)
            }
        }
        .padding(Space.l)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
