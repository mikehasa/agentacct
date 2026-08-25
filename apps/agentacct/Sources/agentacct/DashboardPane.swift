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
    let failedChecks: Int
    let cost: String

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
        evidence = task.evidenceStrength.compactHeadline
        failedChecks = task.evidenceStrength.checksFailed ?? 0
        cost = Self.compactCost(task.cost)
    }

    var recency: String? {
        agoText(lastActivityAt)
    }

    var needsReview: Bool {
        failedChecks > 0 || ["finding", "failed", "blocked"].contains(outcomeKey)
    }

    var hasFinding: Bool {
        failedChecks > 0 || ["finding", "failed"].contains(outcomeKey)
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
            return "\(Fmt.dollars(available.reduce(0, +), prefix: complete ? "$" : "~$")) total"
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

    private var recentWork: [DashboardWorkItem] {
        dashboard.receiptTasks.prefix(3).map(DashboardWorkItem.init)
    }

    private var liveLimits: [LimitEntry] {
        guard case .connected(let snapshot) = glance.phase else { return [] }
        return snapshot.glance.limits.filter { $0.stale != true }
    }

    /// The dashboard shows the account with the least headroom; Limits remains
    /// the complete multi-account surface. This bounds the overview as providers
    /// are added without hiding the most actionable plan state.
    private var primaryLimit: LimitEntry? {
        liveLimits.max { sevenDayUsed($0) < sevenDayUsed($1) }
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
                        limit: primaryLimit,
                        accountCount: liveLimits.count,
                        today: todayUsage,
                        onViewLimits: { selection.open(.limits) }
                    )
                }

                if let periods = dashboard.usage?.byPeriod, periods.count > 1 {
                    DashboardUsageChart(periods: periods)
                }
            }
            .padding(Space.dashboard)
        }
        .overlay(alignment: .bottom) {
            if let error = dashboard.errorText ?? dashboard.receiptListError {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(Type.small)
                    .foregroundStyle(Theme.red)
                    .padding(Space.s)
                    .background(Theme.card, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                    .padding(.bottom, 10)
            }
        }
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

    private func sevenDayUsed(_ limit: LimitEntry) -> Double {
        (limit.windows ?? []).first { $0.kind == "7d" }?.usedPercent ?? 0
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
                .font(Type.callout.weight(.semibold))
                .foregroundStyle(Theme.textMuted)
            if let count {
                Text(String(count))
                    .font(Type.tiny.weight(.semibold).monospacedDigit())
                    .foregroundStyle(Theme.textMuted)
                    .padding(.horizontal, 6)
                    .frame(minWidth: 20, minHeight: 20)
                    .background(Theme.cardAlt, in: Capsule())
            }
            Spacer(minLength: Space.s)
            action()
        }
        .padding(.leading, 15)
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
                        Text("View all").font(Type.action)
                    }
                    .foregroundStyle(Theme.accent)
                    .buttonStyle(QuietButtonStyle())
                    .accessibilityIdentifier("dashboard.recent-work.view-all")
                }
                Divider().overlay(Theme.border)

                if items.isEmpty {
                    DashboardEmptyState(
                        icon: "checklist",
                        title: "No recorded work yet",
                        message: "Set up recording to see task outcomes and evidence here."
                    )
                    .frame(minHeight: 222)
                } else {
                    workColumnLabels
                    Divider().overlay(Theme.border)
                    ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                        RecentWorkRow(item: item) { open(.task(item.id)) }
                        if index < items.count - 1 {
                            Divider().overlay(Theme.border.opacity(0.72))
                        }
                    }
                }
            }
        }
    }

    private var workColumnLabels: some View {
        HStack(spacing: 12) {
            Text("Task").frame(maxWidth: .infinity, alignment: .leading)
            Text("Outcome").frame(width: 100, alignment: .leading)
            Text("Evidence").frame(width: 78, alignment: .leading)
            Text("Cost").frame(width: 64, alignment: .trailing)
            Color.clear.frame(width: 10, height: 1)
        }
        .font(Type.tiny.weight(.medium))
        .foregroundStyle(Theme.textFaint)
        .padding(.horizontal, 15)
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
                        .font(Type.rowTitle.weight(.semibold))
                        .foregroundStyle(Theme.text)
                        .lineLimit(2)
                    Text([item.client, item.recency].compactMap { $0 }.joined(separator: " · "))
                        .font(Type.tiny)
                        .foregroundStyle(Theme.textFaint)
                        .lineLimit(1)
                }
                .frame(maxWidth: .infinity, alignment: .leading)

                HStack(spacing: 7) {
                    StatusDot(color: receiptDecisionTint(item.outcomeKey), size: 7)
                    Text(item.outcome).lineLimit(2)
                }
                .font(Type.tiny.weight(.semibold))
                .foregroundStyle(Theme.textMuted)
                .frame(width: 100, alignment: .leading)

                Text(item.evidence)
                    .font(Type.tiny.monospacedDigit())
                    .foregroundStyle(Theme.textMuted)
                    .frame(width: 78, alignment: .leading)

                Text(item.cost)
                    .font(Type.tiny.monospacedDigit())
                    .foregroundStyle(Theme.textMuted)
                    .frame(width: 64, alignment: .trailing)

                DashboardDisclosureIndicator()
            }
            .padding(.horizontal, 15)
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
                            Text("View all").font(Type.action)
                        }
                        .foregroundStyle(Theme.accent)
                        .buttonStyle(QuietButtonStyle())
                        .accessibilityIdentifier("dashboard.review.view-all")
                    }
                }
                Divider().overlay(Theme.border)

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
                            Divider().overlay(Theme.border.opacity(0.72))
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

    private var tint: Color { item.hasFinding ? Theme.red : Theme.orange }
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
                        .font(Type.rowTitle.weight(.semibold))
                        .foregroundStyle(Theme.text)
                        .lineLimit(2)
                    Text(status)
                        .font(Type.small)
                        .foregroundStyle(tint)
                        .lineLimit(1)
                }
                Spacer(minLength: Space.s)
                DashboardDisclosureIndicator()
            }
            .padding(.horizontal, 15)
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
                        Text("View all").font(Type.action)
                    }
                    .foregroundStyle(Theme.accent)
                    .buttonStyle(QuietButtonStyle())
                    .accessibilityIdentifier("dashboard.active-work.view-all")
                }
                Divider().overlay(Theme.border)
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
                            Divider().overlay(Theme.border.opacity(0.72)).padding(.leading, 36)
                        }
                    }
                }
            }
        }
    }
}

struct DashboardPlanWindowPresentation: Equatable {
    let titleSuffix: String
    let remainingText: String
    let remainingCaption: String
    let remainingFraction: Double
    let usedPercent: Double?
    let resetText: String
    let provenanceText: String

    init(limit: LimitEntry?) {
        guard let limit else {
            titleSuffix = ""
            remainingText = "—"
            remainingCaption = "7-day"
            remainingFraction = 0
            usedPercent = nil
            resetText = "Provider limit unavailable"
            provenanceText = "No live provider data"
            return
        }
        guard let window = (limit.windows ?? []).first(where: { $0.kind == "7d" }) else {
            titleSuffix = ""
            remainingText = "—"
            remainingCaption = "7-day"
            remainingFraction = 0
            usedPercent = nil
            resetText = "7-day limit unavailable"
            provenanceText = "No provider-reported 7-day window"
            return
        }

        let remaining = window.usedPercent.map { max(0, 100 - $0) }
        titleSuffix = " · 7-day window"
        remainingText = remaining.map { String(format: "%.0f%%", $0) } ?? "—"
        remainingCaption = "remaining"
        remainingFraction = (remaining ?? 0) / 100
        usedPercent = window.usedPercent
        resetText = Theme.resetsIn(window.resetsAt).map { "Resets in \($0)" }
            ?? "Reset time unavailable"
        provenanceText = "Provider reported"
    }
}

private struct PlanAndUsageCard: View {
    let limit: LimitEntry?
    let accountCount: Int
    let today: UsageTotals?
    let onViewLimits: () -> Void

    private var providerTitle: String {
        guard let limit else { return "No live provider limit" }
        let client = limit.client?.replacingOccurrences(of: "-", with: " ").capitalized
        let plan = limit.planType?.capitalized
        let title = [client, plan].compactMap { $0 }.joined(separator: " ")
        return title.isEmpty ? "Provider limit" : title
    }

    var body: some View {
        let window = DashboardPlanWindowPresentation(limit: limit)

        Card(padding: 0, fillsHeight: true) {
            VStack(spacing: 0) {
                DashboardCardHeader("Plan and usage", count: accountCount > 1 ? accountCount : nil) {
                    Button(action: onViewLimits) {
                        Text("View limits").font(Type.action)
                    }
                    .foregroundStyle(Theme.accent)
                    .buttonStyle(QuietButtonStyle())
                    .accessibilityIdentifier("dashboard.plan.view-limits")
                }
                Divider().overlay(Theme.border)
                HStack(spacing: Space.l) {
                    PlanRing(
                        fraction: window.remainingFraction,
                        tint: window.usedPercent.map(Theme.limitColor) ?? Theme.textFaint
                    )
                    .frame(width: 82, height: 82)
                    .overlay {
                        VStack(spacing: 1) {
                            Text(window.remainingText)
                                .font(Type.hero)
                                .foregroundStyle(Theme.text)
                            Text(window.remainingCaption)
                                .font(Type.tiny)
                                .foregroundStyle(Theme.textMuted)
                        }
                    }

                    VStack(alignment: .leading, spacing: Space.s) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(providerTitle + window.titleSuffix)
                                .font(Type.tiny)
                                .foregroundStyle(Theme.textMuted)
                                .lineLimit(1)
                            Text(window.resetText)
                                .font(Type.rowTitle.weight(.semibold))
                                .foregroundStyle(Theme.text)
                            Text(window.provenanceText)
                                .font(Type.tiny)
                                .foregroundStyle(Theme.textFaint)
                        }

                        Divider().overlay(Theme.border)

                        VStack(alignment: .leading, spacing: 2) {
                            Text("Today")
                                .font(Type.tiny)
                                .foregroundStyle(Theme.textMuted)
                            Text("\(today?.costText ?? "—") · \(today?.tokensText ?? "—")")
                                .font(Type.rowTitle.weight(.semibold).monospacedDigit())
                                .foregroundStyle(Theme.text)
                            Text("Pricing estimate · client-reported fresh tokens")
                                .font(Type.tiny)
                                .foregroundStyle(Theme.textFaint)
                                .lineLimit(1)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(15)
                .frame(minHeight: 134)
            }
        }
    }
}

struct PlanRing: View {
    let fraction: Double
    var tint: Color

    var body: some View {
        ZStack {
            Circle().stroke(Theme.cardAlt, lineWidth: 7)
            Circle()
                .trim(from: 0, to: min(max(fraction, 0), 1))
                .stroke(tint, style: StrokeStyle(lineWidth: 7, lineCap: .round))
                .rotationEffect(.degrees(-90))
        }
        .accessibilityHidden(true)
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
                            .font(Type.callout.weight(.semibold))
                            .foregroundStyle(Theme.textMuted)
                        Text(series.subtitle(dayCount: periods.count))
                            .font(Type.tiny)
                            .foregroundStyle(Theme.textFaint)
                    }
                    Spacer(minLength: Space.s)
                    Text(series.totalText(for: periods))
                        .font(Type.tiny.monospacedDigit())
                        .foregroundStyle(Theme.textMuted)
                    HStack(spacing: 2) {
                        ForEach(DashboardUsageSeries.allCases) { choice in
                            Button {
                                withAnimation(reduceMotion ? nil : Motion.contentUpdate) {
                                    series = choice
                                    hoveredIndex = nil
                                    pinnedIndex = nil
                                }
                            } label: {
                                Text(choice.rawValue).font(Type.tiny.weight(.semibold))
                                    .padding(.horizontal, 9)
                                    .frame(height: 24)
                            }
                            .buttonStyle(DashboardSeriesButtonStyle(selected: series == choice))
                            .accessibilityAddTraits(series == choice ? .isSelected : [])
                            .accessibilityIdentifier("dashboard.usage.\(choice.rawValue.lowercased())")
                        }
                    }
                    .padding(2)
                    .background(Theme.cardAlt, in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                }
                .padding(.horizontal, 15)
                .frame(minHeight: 47)

                Divider().overlay(Theme.border)

                chart
                    .padding(.horizontal, 15)
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
            .font(Type.tiny.monospacedDigit())
            .foregroundStyle(Theme.textFaint)
            .frame(width: 38, height: 130)

            GeometryReader { proxy in
                let count = max(periods.count, 1)
                let gap: CGFloat = 6
                let columnWidth = max(1, (proxy.size.width - gap * CGFloat(count - 1)) / CGFloat(count))

                ZStack(alignment: .bottomLeading) {
                    VStack(spacing: 0) {
                        Divider().overlay(Theme.border)
                        Spacer()
                        Divider().overlay(Theme.border.opacity(0.72))
                        Spacer()
                        Divider().overlay(Theme.border)
                    }
                    .padding(.bottom, 18)

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
                                    .frame(maxWidth: .infinity, maxHeight: .infinity)
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
                                    .font(Type.tiny.monospacedDigit())
                                    .foregroundStyle(Theme.textFaint)
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
        if activeIndex == index || index == periods.count - 1 { return Theme.accent }
        return Theme.textFaint.opacity(0.72)
    }

    private func barHeight(for period: PeriodBucket) -> CGFloat {
        let value = series.value(for: period)
        guard value > 0 else { return 0 }
        return max(3, 106 * value / maximum)
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
            Text(date).font(Type.tiny).foregroundStyle(Theme.textFaint)
            Text(value).font(Type.small.weight(.semibold).monospacedDigit()).foregroundStyle(Theme.text)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 5)
        .background(Theme.cardAlt, in: RoundedRectangle(cornerRadius: 6, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .strokeBorder(Theme.border, lineWidth: 1)
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
            .foregroundStyle(selected ? Theme.text : Theme.textMuted)
            .background(
                selected ? Theme.card : (configuration.isPressed ? Theme.border.opacity(0.6) : Color.clear),
                in: RoundedRectangle(cornerRadius: 5, style: .continuous)
            )
            .overlay {
                if isFocused {
                    RoundedRectangle(cornerRadius: 5, style: .continuous)
                        .strokeBorder(Theme.accent, lineWidth: 2)
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
                    RoundedRectangle(cornerRadius: 4, style: .continuous)
                        .strokeBorder(Theme.accent.opacity(0.55), lineWidth: 1)
                }
            }
    }
}

private struct DashboardSessionRow: View {
    let session: RecentSession
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 10) {
                StatusDot(color: Theme.statusColor(session.status), size: 6)
                VStack(alignment: .leading, spacing: 3) {
                    Text(session.title ?? "\(session.client) · \(session.shortSessionId)")
                        .font(Type.rowTitle.weight(.semibold))
                        .foregroundStyle(Theme.text)
                        .lineLimit(1)
                    Text([Optional(session.client), session.status.map(statusLabel), agoText(session.lastActivityAt)]
                        .compactMap { $0 }
                        .joined(separator: " · "))
                        .font(Type.tiny)
                        .foregroundStyle(Theme.textFaint)
                        .lineLimit(1)
                }
                Spacer(minLength: 8)
                DashboardDisclosureIndicator()
            }
            .padding(.horizontal, 15)
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
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        configuration.label
            .background(
                Theme.accent.opacity(configuration.isPressed ? 0.10 : (hovering ? 0.055 : 0))
            )
            .overlay {
                if isFocused {
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .strokeBorder(Theme.accent, lineWidth: 2)
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
            .foregroundStyle(Theme.textFaint)
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
                .foregroundStyle(Theme.textFaint)
                .frame(width: 24)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(Type.rowTitle.weight(.semibold)).foregroundStyle(Theme.text)
                Text(message).font(Type.small).foregroundStyle(Theme.textMuted)
            }
        }
        .padding(15)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
