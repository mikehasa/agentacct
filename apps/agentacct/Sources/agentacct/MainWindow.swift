import SwiftUI

// The full window: Sessions (list → detail), Usage (by agent / by model),
// Limits. The menu bar is the glance; this is where details live.

struct MainWindow: View {
    @EnvironmentObject var dashboard: DashboardStore
    @EnvironmentObject var glance: GlanceState
    @EnvironmentObject var selection: AppSelection

    var body: some View {
        NavigationSplitView {
            List(selection: paneBinding) {
                Section {
                    ForEach(MainPane.allCases) { pane in
                        Label(pane.rawValue, systemImage: pane.icon)
                            .tag(pane)
                    }
                }
            }
            .listStyle(.sidebar)
            .navigationSplitViewColumnWidth(min: 150, ideal: 160)
        } detail: {
            Group {
                switch selection.pane {
                case .sessions: SessionsPane()
                case .usage: UsagePane()
                case .limits: LimitsPane()
                }
            }
        }
        .frame(minWidth: 900, minHeight: 540)
        .task { await dashboard.refresh() }
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                if dashboard.isRefreshing {
                    ProgressView().controlSize(.small)
                } else {
                    Button {
                        Task { await dashboard.refresh() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .help("Refresh")
                }
            }
        }
    }

    private var paneBinding: Binding<MainPane?> {
        Binding(
            get: { selection.pane },
            set: { selection.pane = $0 ?? .sessions }
        )
    }
}

extension MainPane {
    var icon: String {
        switch self {
        case .sessions: return "rectangle.stack"
        case .usage: return "chart.bar.xaxis"
        case .limits: return "gauge.with.needle"
        }
    }
}

// MARK: - Sessions

struct SessionsPane: View {
    @EnvironmentObject var dashboard: DashboardStore
    @EnvironmentObject var selection: AppSelection

    var body: some View {
        HSplitView {
            VStack(spacing: 0) {
                List(dashboard.sessions, selection: $selection.sessionId) { entry in
                    SessionRow(entry: entry)
                        .tag(entry.id)
                        .listRowSeparator(.hidden)
                }
                .listStyle(.inset)
                if let total = dashboard.totalSessions {
                    Divider()
                    Text("\(dashboard.sessions.count) root sessions · \(total) total in the store")
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 6)
                }
            }
            .frame(minWidth: 400)
            detail
                .frame(minWidth: 360, maxWidth: .infinity, maxHeight: .infinity)
        }
        .overlay(alignment: .bottom) {
            if let error = dashboard.errorText {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(Theme.red)
                    .padding(8)
                    .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 8))
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
            VStack(spacing: 8) {
                Image(systemName: "rectangle.stack")
                    .font(.system(size: 30))
                    .foregroundStyle(.quaternary)
                Text("Select a session")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

struct SessionRow: View {
    let entry: SessionEntry

    private var workStatus: String? {
        // Show the most severe work state on the row, mirroring the shared
        // reduction: blocked > handed_off > in progress > completed.
        let statuses = Set((entry.work?.items ?? []).compactMap(\.latestStatus))
        if statuses.contains("blocked") { return "blocked" }
        if statuses.contains("handed_off") { return "handed_off" }
        if !statuses.isDisjoint(with: ["started", "checkpoint"]) { return "in_progress" }
        if statuses.contains("completed") { return "completed" }
        return nil
    }

    var body: some View {
        HStack(spacing: 10) {
            RoundedRectangle(cornerRadius: 2)
                .fill(Theme.statusColor(workStatus))
                .frame(width: 3, height: 34)
            VStack(alignment: .leading, spacing: 2.5) {
                Text(entry.displayTitle)
                    .font(.system(size: 12.5, weight: .medium))
                    .lineLimit(1)
                    .truncationMode(.tail)
                HStack(spacing: 6) {
                    Chip(text: entry.client, tint: Theme.clientColor(entry.client))
                    if let project = entry.project {
                        Text(project)
                            .font(.system(size: 10.5))
                            .foregroundStyle(.tertiary)
                    }
                }
            }
            Spacer(minLength: 10)
            VStack(alignment: .trailing, spacing: 2.5) {
                Text(entry.usage?.costText ?? "—")
                    .font(.system(size: 12, weight: .semibold, design: .rounded))
                    .monospacedDigit()
                HStack(spacing: 5) {
                    if let tokens = entry.usage?.freshTokens {
                        Text(UsageTotals.compact(tokens))
                            .font(.system(size: 10))
                            .monospacedDigit()
                            .foregroundStyle(.tertiary)
                    }
                    if let ts = entry.lastActivityAt {
                        Text(Date(timeIntervalSince1970: ts), style: .relative)
                            .font(.system(size: 10))
                            .foregroundStyle(.tertiary)
                    }
                }
            }
        }
        .padding(.vertical, 3)
    }
}

struct SessionDetail: View {
    let entry: SessionEntry

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                // Hero
                VStack(alignment: .leading, spacing: 7) {
                    Text(entry.displayTitle)
                        .font(.system(size: 17, weight: .semibold))
                        .lineLimit(3)
                    HStack(spacing: 6) {
                        Chip(text: entry.client, tint: Theme.clientColor(entry.client))
                        if let project = entry.project { Chip(text: project) }
                        if let duration = entry.durationSeconds {
                            Chip(text: Self.duration(duration))
                        }
                        if let models = entry.observedModels, !models.isEmpty {
                            Chip(text: models.joined(separator: " · "), tint: Theme.purple)
                        }
                    }
                }

                if let usage = entry.usage {
                    LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 8), count: 4), spacing: 8) {
                        StatTile(label: "cost", value: usage.costText,
                                 detail: usage.costConfidence?.replacingOccurrences(of: "_", with: " "),
                                 accent: Theme.blue)
                        StatTile(label: "fresh", value: usage.freshTokens.map(UsageTotals.compact) ?? "—")
                        StatTile(label: "cache read", value: usage.cacheReadTokens.map(UsageTotals.compact) ?? "—")
                        StatTile(label: "turns", value: usage.turnsTotal.map(String.init) ?? "—")
                    }
                }

                if let note = entry.usageNote {
                    Text(note)
                        .font(.system(size: 10.5))
                        .foregroundStyle(.tertiary)
                }

                if let items = entry.work?.items, !items.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        SectionCaption(text: "Work")
                        VStack(alignment: .leading, spacing: 0) {
                            ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                                HStack(spacing: 9) {
                                    StatusDot(color: Theme.statusColor(item.latestStatus))
                                    Text(item.title ?? item.sectionId ?? "untitled")
                                        .font(.system(size: 12))
                                        .lineLimit(2)
                                    Spacer(minLength: 8)
                                    if let evidence = item.evidenceStatus {
                                        Chip(text: evidence.replacingOccurrences(of: "_", with: " "),
                                             tint: evidence.contains("verified") ? Theme.green : .secondary)
                                    }
                                }
                                .padding(.vertical, 6)
                                if index < items.count - 1 { Divider().opacity(0.5) }
                            }
                        }
                        .padding(.horizontal, 11)
                        .padding(.vertical, 4)
                        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 9, style: .continuous))
                    }
                }

                if let join = entry.join {
                    VStack(alignment: .leading, spacing: 8) {
                        SectionCaption(text: "Attribution")
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Chip(text: join.state ?? "unknown", tint: joinTint(join.state))
                            if let reason = join.reason {
                                Text(reason)
                                    .font(.system(size: 10.5))
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                }

                if let related = entry.related, (related.childSessionCount ?? 0) > 0 {
                    HStack(spacing: 8) {
                        Image(systemName: "point.3.connected.trianglepath.dotted")
                            .foregroundStyle(.secondary)
                        Text("\(related.childSessionCount ?? 0) subagent sessions")
                            .font(.system(size: 11.5))
                        Spacer()
                        if let fresh = related.childrenUsage?.freshTokens {
                            Text("\(UsageTotals.compact(fresh)) fresh tok")
                                .font(.system(size: 11))
                                .monospacedDigit()
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding(10)
                    .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 9, style: .continuous))
                }
            }
            .padding(16)
        }
    }

    private func joinTint(_ state: String?) -> Color {
        switch state {
        case "attributed": return Theme.green
        case "ambiguous": return Theme.orange
        case "sections_only": return Theme.blue
        default: return .secondary
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
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                if let usage = dashboard.usage {
                    if let totals = usage.totals {
                        HStack(spacing: 8) {
                            StatTile(label: "30d cost", value: totals.costText, accent: Theme.blue)
                            StatTile(label: "30d fresh tokens",
                                     value: totals.freshTokens.map(UsageTotals.compact) ?? "—")
                            StatTile(label: "cache read",
                                     value: totals.cacheReadTokens.map(UsageTotals.compact) ?? "—")
                            StatTile(label: "sessions",
                                     value: totals.sessions.map(String.init) ?? "—")
                        }
                    }
                    bucketSection(title: "By agent", buckets: usage.byClient) { bucket in
                        (bucket.client ?? "?", Theme.clientColor(bucket.client))
                    }
                    bucketSection(title: "By model", buckets: usage.byModel) { bucket in
                        ("\(bucket.model ?? "?")", Theme.clientColor(bucket.client))
                    }
                } else {
                    VStack(spacing: 8) {
                        Image(systemName: "chart.bar.xaxis")
                            .font(.system(size: 30))
                            .foregroundStyle(.quaternary)
                        Text("No usage loaded")
                            .foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.top, 80)
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
            SectionCaption(text: title + " · last 30 days")
            VStack(spacing: 0) {
                ForEach(Array(sorted.enumerated()), id: \.element.id) { index, bucket in
                    let (name, tint) = nameOf(bucket)
                    HStack(spacing: 10) {
                        StatusDot(color: tint, size: 6)
                        Text(name)
                            .font(.system(size: 12, weight: .medium))
                            .lineLimit(1)
                            .frame(width: 170, alignment: .leading)
                        MeterBar(fraction: Double(bucket.freshTokens ?? 0) / max(maxFresh, 1),
                                 tint: tint, height: 6)
                        Text(bucket.freshTokens.map(UsageTotals.compact) ?? "—")
                            .font(.system(size: 11.5))
                            .monospacedDigit()
                            .foregroundStyle(.secondary)
                            .frame(width: 58, alignment: .trailing)
                        Text(bucket.costText)
                            .font(.system(size: 11.5, weight: .medium))
                            .monospacedDigit()
                            .frame(width: 78, alignment: .trailing)
                    }
                    .padding(.vertical, 6.5)
                    if index < sorted.count - 1 { Divider().opacity(0.5) }
                }
            }
            .padding(.horizontal, 11)
            .padding(.vertical, 4)
            .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 9, style: .continuous))
        }
    }
}

// MARK: - Limits

struct LimitsPane: View {
    @EnvironmentObject var glance: GlanceState
    @State private var showStale = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack {
                    SectionCaption(text: "Provider limits")
                    Spacer()
                    Toggle("Show stale accounts", isOn: $showStale)
                        .toggleStyle(.checkbox)
                        .font(.system(size: 11))
                }
                if case .connected(let snapshot) = glance.phase {
                    let limits = snapshot.glance.limits.filter { showStale || $0.stale != true }
                    if limits.isEmpty {
                        Text("No live limit readings.")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                    ForEach(Array(limits.enumerated()), id: \.offset) { _, limit in
                        limitCard(limit)
                    }
                } else {
                    Text("Daemon not connected.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(16)
        }
    }

    private func limitCard(_ limit: LimitEntry) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 7) {
                StatusDot(color: Theme.clientColor(limit.client))
                Text(limit.client ?? "?")
                    .font(.system(size: 12.5, weight: .semibold))
                if let plan = limit.planType {
                    Chip(text: plan, tint: Theme.clientColor(limit.client))
                }
                if limit.stale == true {
                    Chip(text: "stale", tint: .secondary)
                }
                Spacer()
            }
            ForEach(Array((limit.windows ?? []).enumerated()), id: \.offset) { _, window in
                if let used = window.usedPercent {
                    HStack(spacing: 10) {
                        Text(window.kind ?? "")
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                            .frame(width: 26, alignment: .leading)
                        MeterBar(fraction: used / 100.0,
                                 tint: Theme.limitColor(usedPercent: used), height: 7)
                        Text(String(format: "%.0f%%", used))
                            .font(.system(size: 11.5, weight: .medium))
                            .monospacedDigit()
                            .frame(width: 36, alignment: .trailing)
                        Text(Theme.resetsIn(window.resetsAt).map { "resets in \($0)" } ?? "")
                            .font(.system(size: 10))
                            .foregroundStyle(.tertiary)
                            .frame(width: 96, alignment: .trailing)
                    }
                }
            }
        }
        .padding(12)
        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
}
