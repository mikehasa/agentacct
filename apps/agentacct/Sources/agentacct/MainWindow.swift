import SwiftUI

// The full window: a precision instrument on warm paper (light) or the Tokyo
// Night storm (dark), following the system appearance — every color is a
// Theme token with both values. Custom chrome (no system sidebar), like the
// TUI. The menu bar is the glance; this is where details live.

struct MainWindow: View {
    @EnvironmentObject var dashboard: DashboardStore
    @EnvironmentObject var glance: GlanceState
    @EnvironmentObject var selection: AppSelection

    var body: some View {
        VStack(spacing: 0) {
            TopBar()
            Rectangle().fill(Theme.border).frame(height: 1)
            Group {
                switch selection.pane {
                case .sessions: SessionsPane()
                case .usage: UsagePane()
                case .limits: LimitsPane()
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(Theme.bg)
        .frame(minWidth: 960, minHeight: 560)
        .task { await dashboard.refresh() }
    }
}

// MARK: - Top bar

struct TopBar: View {
    @EnvironmentObject var dashboard: DashboardStore
    @EnvironmentObject var selection: AppSelection

    var body: some View {
        HStack(spacing: 14) {
            HStack(spacing: 7) {
                Circle().fill(Theme.accent.gradient).frame(width: 9, height: 9)
                Text("agentacct")
                    .font(.system(size: 13, weight: .semibold, design: .rounded))
                    .foregroundStyle(Theme.text)
            }
            .padding(.leading, 76)  // clear the traffic lights (hidden titlebar)

            HStack(spacing: 3) {
                ForEach(MainPane.allCases) { pane in
                    PaneTab(pane: pane, selected: selection.pane == pane) {
                        selection.pane = pane
                    }
                }
            }
            .padding(3)
            .background(Theme.surface, in: RoundedRectangle(cornerRadius: 8, style: .continuous))

            Spacer()

            if let updated = dashboard.lastUpdated {
                Text(updated, style: .relative)
                    .font(.system(size: 10.5))
                    .foregroundStyle(Theme.textFaint)
            }
            if dashboard.isRefreshing {
                ProgressView().controlSize(.small).tint(Theme.textMuted)
            } else {
                Button {
                    Task { await dashboard.refresh() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                        .font(.system(size: 11.5, weight: .medium))
                        .foregroundStyle(Theme.textMuted)
                }
                .buttonStyle(.plain)
                .help("Refresh")
            }
        }
        .padding(.horizontal, 14)
        .frame(height: 46)
        .background(Theme.bg)
    }
}

struct PaneTab: View {
    let pane: MainPane
    let selected: Bool
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 5) {
                Image(systemName: pane.icon).font(.system(size: 10.5, weight: .medium))
                Text(pane.rawValue).font(.system(size: 12, weight: .medium))
            }
            .padding(.horizontal, 11)
            .padding(.vertical, 5)
            .foregroundStyle(selected ? Theme.text : (hovering ? Theme.textMuted : Theme.textFaint))
            .background(
                selected ? AnyShapeStyle(Theme.cardAlt) : AnyShapeStyle(.clear),
                in: RoundedRectangle(cornerRadius: 6, style: .continuous)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
    }
}

extension MainPane {
    var icon: String {
        switch self {
        case .sessions: return "rectangle.stack.fill"
        case .usage: return "chart.bar.fill"
        case .limits: return "gauge.with.needle.fill"
        }
    }
}

// MARK: - Sessions

struct SessionsPane: View {
    @EnvironmentObject var dashboard: DashboardStore
    @EnvironmentObject var selection: AppSelection

    var body: some View {
        HStack(spacing: 0) {
            VStack(spacing: 0) {
                ScrollBox {
                    VStack(spacing: 2) {
                        ForEach(dashboard.sessions) { entry in
                            SessionRow(entry: entry, selected: selection.sessionId == entry.id)
                                .onTapGesture { selection.sessionId = entry.id }
                        }
                    }
                    .padding(10)
                }
                if let total = dashboard.totalSessions {
                    Rectangle().fill(Theme.border).frame(height: 1)
                    Text("\(dashboard.sessions.count) root sessions · \(total) total in the store")
                        .font(.system(size: 10))
                        .foregroundStyle(Theme.textFaint)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 7)
                }
            }
            .frame(width: 430)
            Rectangle().fill(Theme.border).frame(width: 1)
            detail
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Theme.surface.opacity(0.4))
        }
        .overlay(alignment: .bottom) {
            if let error = dashboard.errorText {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(Theme.red)
                    .padding(8)
                    .background(Theme.card, in: RoundedRectangle(cornerRadius: 8))
                    .padding(.bottom, 10)
            }
        }
    }

    @ViewBuilder
    private var detail: some View {
        if let id = selection.sessionId,
           let entry = dashboard.sessions.first(where: { $0.id == id }) {
            SessionDetail(entry: entry)
        } else {
            VStack(spacing: 10) {
                Image(systemName: "rectangle.stack")
                    .font(.system(size: 34, weight: .light))
                    .foregroundStyle(Theme.textFaint)
                Text("Select a session")
                    .font(.system(size: 13))
                    .foregroundStyle(Theme.textMuted)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

struct SessionRow: View {
    let entry: SessionEntry
    let selected: Bool
    @State private var hovering = false

    private var workStatus: String? {
        let statuses = Set((entry.work?.items ?? []).compactMap(\.latestStatus))
        if statuses.contains("blocked") { return "blocked" }
        if statuses.contains("handed_off") { return "handed_off" }
        if !statuses.isDisjoint(with: ["started", "checkpoint"]) { return "in_progress" }
        if statuses.contains("completed") { return "completed" }
        return nil
    }

    var body: some View {
        HStack(spacing: 10) {
            StatusDot(color: Theme.statusColor(workStatus), size: 7)
            VStack(alignment: .leading, spacing: 3) {
                Text(entry.displayTitle)
                    .font(.system(size: 12.5, weight: .medium))
                    .foregroundStyle(Theme.text)
                    .lineLimit(1)
                    .truncationMode(.tail)
                HStack(spacing: 6) {
                    Text(entry.client)
                        .font(.system(size: 9.5, weight: .semibold))
                        .foregroundStyle(Theme.clientColor(entry.client))
                    if let project = entry.project {
                        Text(project)
                            .font(.system(size: 10))
                            .foregroundStyle(Theme.textFaint)
                    }
                }
            }
            Spacer(minLength: 10)
            VStack(alignment: .trailing, spacing: 3) {
                Text(entry.usage?.costText ?? "—")
                    .font(.system(size: 12, weight: .semibold, design: .rounded))
                    .monospacedDigit()
                    .foregroundStyle(Theme.text)
                HStack(spacing: 5) {
                    if let tokens = entry.usage?.freshTokens {
                        Text(UsageTotals.compact(tokens))
                            .font(.system(size: 9.5))
                            .monospacedDigit()
                            .foregroundStyle(Theme.textFaint)
                    }
                    if let ts = entry.lastActivityAt {
                        Text(Date(timeIntervalSince1970: ts), style: .relative)
                            .font(.system(size: 9.5))
                            .foregroundStyle(Theme.textFaint)
                    }
                }
            }
        }
        .padding(.horizontal, 11)
        .padding(.vertical, 8)
        .background(
            selected ? AnyShapeStyle(Theme.cardAlt) : (hovering ? AnyShapeStyle(Theme.surface) : AnyShapeStyle(.clear)),
            in: RoundedRectangle(cornerRadius: 8, style: .continuous)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .strokeBorder(selected ? Theme.accent.opacity(0.45) : .clear, lineWidth: 1)
        )
        .contentShape(Rectangle())
        .onHover { hovering = $0 }
    }
}

struct SessionDetail: View {
    let entry: SessionEntry

    var body: some View {
        ScrollBox {
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(entry.displayTitle)
                        .font(.system(size: 16, weight: .semibold))
                        .foregroundStyle(Theme.text)
                        .lineLimit(3)
                    HStack(spacing: 6) {
                        Chip(text: entry.client, tint: Theme.clientColor(entry.client))
                        if let project = entry.project { Chip(text: project, tint: Theme.textMuted) }
                        if let duration = entry.durationSeconds {
                            Chip(text: SessionDetail.duration(duration), tint: Theme.textMuted)
                        }
                        if let models = entry.observedModels, !models.isEmpty {
                            Chip(text: models.joined(separator: " · "), tint: Theme.purple)
                        }
                    }
                }

                if let usage = entry.usage {
                    LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 8), count: 4), spacing: 8) {
                        PanelTile(label: "cost", value: usage.costText,
                                     detail: usage.costConfidence?.replacingOccurrences(of: "_", with: " "),
                                     accent: Theme.blue)
                        PanelTile(label: "fresh", value: usage.freshTokens.map(UsageTotals.compact) ?? "—")
                        PanelTile(label: "cache read", value: usage.cacheReadTokens.map(UsageTotals.compact) ?? "—")
                        PanelTile(label: "turns", value: usage.turnsTotal.map(String.init) ?? "—")
                    }
                }

                if let note = entry.usageNote {
                    Text(note)
                        .font(.system(size: 10.5))
                        .foregroundStyle(Theme.textFaint)
                }

                if let items = entry.work?.items, !items.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        SectionCaption(tone: Theme.textMuted, text: "Work")
                        Card(padding: 4) {
                            VStack(alignment: .leading, spacing: 0) {
                                ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                                    HStack(spacing: 9) {
                                        StatusDot(color: Theme.statusColor(item.latestStatus))
                                        Text(item.title ?? item.sectionId ?? "untitled")
                                            .font(.system(size: 12))
                                            .foregroundStyle(Theme.text)
                                            .lineLimit(2)
                                        Spacer(minLength: 8)
                                        if let evidence = item.evidenceStatus {
                                            Chip(text: evidence.replacingOccurrences(of: "_", with: " "),
                                                 tint: evidenceTint(evidence))
                                        }
                                    }
                                    .padding(.horizontal, 9)
                                    .padding(.vertical, 7)
                                    if index < items.count - 1 {
                                        Rectangle().fill(Theme.border.opacity(0.6)).frame(height: 1)
                                    }
                                }
                            }
                        }
                    }
                }

                if let join = entry.join {
                    VStack(alignment: .leading, spacing: 8) {
                        SectionCaption(tone: Theme.textMuted, text: "Attribution")
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Chip(text: join.state ?? "unknown", tint: joinTint(join.state))
                            if let reason = join.reason {
                                Text(reason)
                                    .font(.system(size: 10.5))
                                    .foregroundStyle(Theme.textMuted)
                            }
                        }
                    }
                }

                if let related = entry.related, (related.childSessionCount ?? 0) > 0 {
                    Card {
                        HStack(spacing: 8) {
                            Image(systemName: "point.3.connected.trianglepath.dotted")
                                .foregroundStyle(Theme.textMuted)
                            Text("\(related.childSessionCount ?? 0) subagent sessions")
                                .font(.system(size: 11.5))
                                .foregroundStyle(Theme.text)
                            Spacer()
                            if let fresh = related.childrenUsage?.freshTokens {
                                Text("\(UsageTotals.compact(fresh)) fresh tok")
                                    .font(.system(size: 11))
                                    .monospacedDigit()
                                    .foregroundStyle(Theme.textMuted)
                            }
                        }
                    }
                }
            }
            .padding(16)
        }
    }

    /// The evidence four-state enum, in the product's confidence colors —
    /// strong proof green, failure red, weak amber, nothing muted. This chip
    /// IS the product (what did the agent prove?), so it never renders as an
    /// undifferentiated gray.
    private func evidenceTint(_ status: String) -> Color {
        switch status {
        case "strong": return Theme.green
        case "failed": return Theme.red
        case "weak": return Theme.orange
        default: return Theme.textMuted
        }
    }

    private func joinTint(_ state: String?) -> Color {
        switch state {
        case "attributed": return Theme.green
        case "ambiguous": return Theme.orange
        case "sections_only": return Theme.blue
        default: return Theme.textMuted
        }
    }

    static func duration(_ seconds: Double) -> String {
        let total = Int(seconds)
        let hours = total / 3600
        let minutes = (total % 3600) / 60
        if hours > 0 { return "\(hours)h \(minutes)m" }
        return "\(minutes)m"
    }
}

// MARK: - Usage

struct UsagePane: View {
    @EnvironmentObject var dashboard: DashboardStore

    var body: some View {
        ScrollBox {
            VStack(alignment: .leading, spacing: 16) {
                if let usage = dashboard.usage {
                    if let totals = usage.totals {
                        HStack(spacing: 8) {
                            PanelTile(label: "30d cost", value: totals.costText, accent: Theme.blue)
                            PanelTile(label: "30d fresh tokens",
                                         value: totals.freshTokens.map(UsageTotals.compact) ?? "—")
                            PanelTile(label: "cache read",
                                         value: totals.cacheReadTokens.map(UsageTotals.compact) ?? "—")
                            PanelTile(label: "sessions",
                                         value: totals.sessions.map(String.init) ?? "—")
                        }
                    }
                    if let periods = usage.byPeriod, periods.count > 1 {
                        DailyChart(periods: periods)
                    }
                    bucketSection(title: "By agent", buckets: usage.byClient) { bucket in
                        (bucket.client ?? "?", Theme.clientColor(bucket.client))
                    }
                    bucketSection(title: "By model", buckets: usage.byModel) { bucket in
                        (bucket.model ?? "?", Theme.clientColor(bucket.client))
                    }
                } else {
                    VStack(spacing: 10) {
                        Image(systemName: "chart.bar.xaxis")
                            .font(.system(size: 32, weight: .light))
                            .foregroundStyle(Theme.textFaint)
                        Text("No usage loaded")
                            .foregroundStyle(Theme.textMuted)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.top, 90)
                }
            }
            .padding(16)
        }
    }

    private func bucketSection(
        title: String,
        buckets: [UsageBucket],
        nameOf: @escaping (UsageBucket) -> (String, Color)
    ) -> some View {
        let sorted = buckets.sorted { ($0.freshTokens ?? 0) > ($1.freshTokens ?? 0) }
        let maxFresh = Double(sorted.first?.freshTokens ?? 1)
        return VStack(alignment: .leading, spacing: 8) {
            SectionCaption(tone: Theme.textMuted, text: title + " · last 30 days")
            Card(padding: 6) {
                VStack(spacing: 0) {
                    ForEach(Array(sorted.enumerated()), id: \.element.id) { index, bucket in
                        let (name, tint) = nameOf(bucket)
                        HStack(spacing: 10) {
                            StatusDot(color: tint, size: 6)
                            Text(name)
                                .font(.system(size: 12, weight: .medium))
                                .foregroundStyle(Theme.text)
                                .lineLimit(1)
                                .frame(width: 168, alignment: .leading)
                            MeterBar(fraction: Double(bucket.freshTokens ?? 0) / max(maxFresh, 1),
                                     tint: tint, height: 7)
                            Text(bucket.freshTokens.map(UsageTotals.compact) ?? "—")
                                .font(.system(size: 11.5))
                                .monospacedDigit()
                                .foregroundStyle(Theme.textMuted)
                                .frame(width: 58, alignment: .trailing)
                            Text(bucket.costText)
                                .font(.system(size: 11.5, weight: .semibold, design: .rounded))
                                .monospacedDigit()
                                .foregroundStyle(Theme.text)
                                .frame(width: 82, alignment: .trailing)
                        }
                        .padding(.horizontal, 9)
                        .padding(.vertical, 7)
                        if index < sorted.count - 1 {
                            Rectangle().fill(Theme.border.opacity(0.6)).frame(height: 1)
                        }
                    }
                }
            }
        }
    }
}

/// The 30-day stacked daily bars, client-colored — the dashboard's centerpiece.
struct DailyChart: View {
    let periods: [PeriodBucket]

    private var clients: [String] {
        var seen: [String] = []
        for period in periods {
            for client in (period.byClient ?? [:]).keys where !seen.contains(client) {
                seen.append(client)
            }
        }
        return seen.sorted()
    }

    private var maxTokens: Double {
        max(Double(periods.compactMap(\.freshTokens).max() ?? 1), 1)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                SectionCaption(tone: Theme.textMuted, text: "Daily fresh tokens")
                Spacer()
                ForEach(clients, id: \.self) { client in
                    HStack(spacing: 4) {
                        StatusDot(color: Theme.clientColor(client), size: 5)
                        Text(client)
                            .font(.system(size: 9.5))
                            .foregroundStyle(Theme.textMuted)
                    }
                }
            }
            Card(padding: 12) {
                VStack(spacing: 6) {
                    HStack(alignment: .bottom, spacing: 3) {
                        ForEach(periods) { period in
                            VStack(spacing: 0) {
                                let total = Double(period.freshTokens ?? 0)
                                let height = 110 * total / maxTokens
                                VStack(spacing: 0.5) {
                                    ForEach(clients, id: \.self) { client in
                                        let slice = Double(period.byClient?[client]?.freshTokens ?? 0)
                                        if slice > 0, total > 0 {
                                            RoundedRectangle(cornerRadius: 1)
                                                .fill(Theme.clientColor(client).gradient)
                                                .frame(height: max(1.5, height * slice / total))
                                        }
                                    }
                                }
                                .frame(maxWidth: .infinity)
                            }
                            .frame(maxWidth: .infinity, alignment: .bottom)
                        }
                    }
                    .frame(height: 112, alignment: .bottom)
                    HStack {
                        Text(periods.first?.shortLabel ?? "")
                        Spacer()
                        Text(periods[periods.count / 2].shortLabel)
                        Spacer()
                        Text(periods.last?.shortLabel ?? "")
                    }
                    .font(.system(size: 9))
                    .foregroundStyle(Theme.textFaint)
                }
            }
        }
    }
}

// MARK: - Limits

struct LimitsPane: View {
    @EnvironmentObject var glance: GlanceState
    @State private var showStale = false

    var body: some View {
        ScrollBox {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    SectionCaption(tone: Theme.textMuted, text: "Provider limits")
                    Spacer()
                    Toggle("Show stale accounts", isOn: $showStale)
                        .toggleStyle(.checkbox)
                        .font(.system(size: 11))
                        .foregroundStyle(Theme.textMuted)
                }
                if case .connected(let snapshot) = glance.phase {
                    let limits = snapshot.glance.limits.filter { showStale || $0.stale != true }
                    if limits.isEmpty {
                        Text("No live limit readings.")
                            .font(.system(size: 12))
                            .foregroundStyle(Theme.textMuted)
                    }
                    ForEach(Array(limits.enumerated()), id: \.offset) { _, limit in
                        limitCard(limit)
                    }
                } else {
                    Text("Daemon not connected.")
                        .font(.system(size: 12))
                        .foregroundStyle(Theme.textMuted)
                }
            }
            .padding(16)
        }
    }

    private func limitCard(_ limit: LimitEntry) -> some View {
        Card {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 7) {
                    StatusDot(color: Theme.clientColor(limit.client))
                    Text(limit.client ?? "?")
                        .font(.system(size: 12.5, weight: .semibold))
                        .foregroundStyle(Theme.text)
                    if let plan = limit.planType {
                        Chip(text: plan, tint: Theme.clientColor(limit.client))
                    }
                    if limit.stale == true {
                        Chip(text: "stale", tint: Theme.textMuted)
                    }
                    Spacer()
                }
                ForEach(Array((limit.windows ?? []).enumerated()), id: \.offset) { _, window in
                    if let used = window.usedPercent {
                        HStack(spacing: 10) {
                            Text(window.kind ?? "")
                                .font(.system(size: 11))
                                .foregroundStyle(Theme.textMuted)
                                .frame(width: 26, alignment: .leading)
                            MeterBar(fraction: used / 100.0,
                                     tint: Theme.limitColor(usedPercent: used), height: 8)
                            Text(String(format: "%.0f%%", used))
                                .font(.system(size: 11.5, weight: .semibold))
                                .monospacedDigit()
                                .foregroundStyle(Theme.text)
                                .frame(width: 36, alignment: .trailing)
                            Text(Theme.resetsIn(window.resetsAt).map { "resets in \($0)" } ?? "")
                                .font(.system(size: 10))
                                .foregroundStyle(Theme.textFaint)
                                .frame(width: 100, alignment: .trailing)
                        }
                    }
                }
            }
        }
    }
}
