import SwiftUI

// The full window: Sessions (list → detail), Usage (by agent / by model),
// Limits. The menu bar is the glance; this is where details live.

struct MainWindow: View {
    @EnvironmentObject var dashboard: DashboardStore
    @EnvironmentObject var glance: GlanceState
    @EnvironmentObject var selection: AppSelection

    var body: some View {
        NavigationSplitView {
            List(MainPane.allCases, selection: paneBinding) { pane in
                Text(pane.rawValue).tag(pane)
            }
            .navigationSplitViewColumnWidth(min: 140, ideal: 150)
        } detail: {
            Group {
                switch selection.pane {
                case .sessions: SessionsPane()
                case .usage: UsagePane()
                case .limits: LimitsPane()
                }
            }
        }
        .frame(minWidth: 860, minHeight: 520)
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

// MARK: - Sessions

struct SessionsPane: View {
    @EnvironmentObject var dashboard: DashboardStore
    @EnvironmentObject var selection: AppSelection

    var body: some View {
        HSplitView {
            List(dashboard.sessions, selection: $selection.sessionId) { entry in
                SessionRow(entry: entry).tag(entry.id)
            }
            .frame(minWidth: 380)
            detail
                .frame(minWidth: 340, maxWidth: .infinity, maxHeight: .infinity)
        }
        .overlay(alignment: .bottom) {
            if let error = dashboard.errorText {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .padding(6)
                    .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 6))
                    .padding(.bottom, 8)
            }
        }
    }

    @ViewBuilder
    private var detail: some View {
        if let id = selection.sessionId,
           let entry = dashboard.sessions.first(where: { $0.id == id }) {
            SessionDetail(entry: entry)
        } else {
            ContentUnavailableView(
                "Select a session",
                systemImage: "list.bullet.rectangle",
                description: Text(footerText)
            )
        }
    }

    private var footerText: String {
        if let total = dashboard.totalSessions {
            return "\(dashboard.sessions.count) root sessions shown · \(total) sessions in the store"
        }
        return ""
    }
}

struct SessionRow: View {
    let entry: SessionEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack {
                Text(entry.displayTitle)
                    .lineLimit(1)
                    .truncationMode(.tail)
                Spacer()
                if let ts = entry.lastActivityAt {
                    Text(Date(timeIntervalSince1970: ts), style: .relative)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            HStack(spacing: 8) {
                Text(entry.client)
                    .font(.caption)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 1)
                    .background(.quaternary, in: Capsule())
                if let project = entry.project {
                    Text(project).font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if let tokens = entry.usage?.freshTokens {
                    Text(UsageTotals.compact(tokens) + " tok")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                Text(entry.usage?.costText ?? "—")
                    .font(.caption.monospacedDigit())
            }
        }
        .padding(.vertical, 2)
    }
}

struct SessionDetail: View {
    let entry: SessionEntry

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(entry.displayTitle).font(.title3.bold())
                    HStack(spacing: 10) {
                        Text(entry.client)
                        if let project = entry.project { Text(project) }
                        if let duration = entry.durationSeconds {
                            Text(Self.duration(duration))
                        }
                        if let models = entry.observedModels, !models.isEmpty {
                            Text(models.joined(separator: ", "))
                        }
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }

                if let usage = entry.usage {
                    GroupBox("Usage") {
                        Grid(alignment: .leading, horizontalSpacing: 18, verticalSpacing: 4) {
                            GridRow {
                                stat("fresh", usage.freshTokens)
                                stat("cache read", usage.cacheReadTokens)
                                stat("cache write", usage.cacheCreationTokens)
                            }
                            GridRow {
                                stat("total", usage.totalTokens)
                                statText("turns", usage.turnsTotal.map(String.init) ?? "—")
                                statText("cost", usage.costText)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }

                if let note = entry.usageNote {
                    Text(note).font(.caption).foregroundStyle(.secondary)
                }

                if let items = entry.work?.items, !items.isEmpty {
                    GroupBox("Work") {
                        VStack(alignment: .leading, spacing: 5) {
                            ForEach(items) { item in
                                HStack(spacing: 6) {
                                    Text(item.statusGlyph)
                                        .foregroundStyle(item.latestStatus == "blocked" ? .orange : .secondary)
                                    Text(item.title ?? item.sectionId ?? "untitled")
                                        .lineLimit(2)
                                    Spacer()
                                    if let evidence = item.evidenceStatus {
                                        Text(evidence)
                                            .font(.caption2)
                                            .foregroundStyle(.secondary)
                                    }
                                }
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }

                if let join = entry.join {
                    GroupBox("Attribution") {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(join.state ?? "unknown").font(.callout)
                            if let reason = join.reason {
                                Text(reason).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }

                if let related = entry.related, (related.childSessionCount ?? 0) > 0 {
                    GroupBox("Subagents") {
                        HStack {
                            Text("\(related.childSessionCount ?? 0) child sessions")
                            Spacer()
                            if let fresh = related.childrenUsage?.freshTokens {
                                Text(UsageTotals.compact(fresh) + " fresh tok")
                                    .font(.callout.monospacedDigit())
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
            .padding(14)
        }
    }

    private func stat(_ label: String, _ value: Int?) -> some View {
        statText(label, value.map(UsageTotals.compact) ?? "—")
    }

    private func statText(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(label).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.body.monospacedDigit())
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
            VStack(alignment: .leading, spacing: 16) {
                if let usage = dashboard.usage {
                    GroupBox("By agent · last 30 days") {
                        bucketTable(usage.byClient, nameOf: { $0.client ?? "?" })
                    }
                    GroupBox("By model · last 30 days") {
                        bucketTable(usage.byModel, nameOf: { "\($0.model ?? "?")  (\($0.client ?? "?"))" })
                    }
                } else {
                    ContentUnavailableView("No usage loaded", systemImage: "chart.bar")
                }
            }
            .padding(14)
        }
    }

    private func bucketTable(_ buckets: [UsageBucket], nameOf: @escaping (UsageBucket) -> String) -> some View {
        Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 4) {
            GridRow {
                Text("").gridColumnAlignment(.leading)
                Text("sessions").font(.caption).foregroundStyle(.secondary)
                Text("fresh tok").font(.caption).foregroundStyle(.secondary)
                Text("cache read").font(.caption).foregroundStyle(.secondary)
                Text("cost").font(.caption).foregroundStyle(.secondary)
            }
            ForEach(buckets.sorted { ($0.freshTokens ?? 0) > ($1.freshTokens ?? 0) }) { bucket in
                GridRow {
                    Text(nameOf(bucket)).lineLimit(1)
                    Text(bucket.sessions.map(String.init) ?? "—")
                        .font(.body.monospacedDigit())
                    Text(bucket.freshTokens.map(UsageTotals.compact) ?? "—")
                        .font(.body.monospacedDigit())
                    Text(bucket.cacheReadTokens.map(UsageTotals.compact) ?? "—")
                        .font(.body.monospacedDigit())
                    Text(bucket.costText)
                        .font(.body.monospacedDigit())
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: - Limits

struct LimitsPane: View {
    @EnvironmentObject var glance: GlanceState
    @State private var showStale = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Toggle("Show stale accounts", isOn: $showStale)
                .toggleStyle(.checkbox)
            if case .connected(let snapshot) = glance.phase {
                let limits = snapshot.glance.limits.filter { showStale || $0.stale != true }
                if limits.isEmpty {
                    Text("No live limit readings.").foregroundStyle(.secondary)
                }
                ForEach(Array(limits.enumerated()), id: \.offset) { _, limit in
                    GroupBox(limit.client ?? "?") {
                        VStack(alignment: .leading, spacing: 6) {
                            ForEach(Array((limit.windows ?? []).enumerated()), id: \.offset) { _, window in
                                if let used = window.usedPercent {
                                    HStack(spacing: 8) {
                                        Text(window.kind ?? "")
                                            .frame(width: 30, alignment: .leading)
                                            .foregroundStyle(.secondary)
                                        ProgressView(value: min(max(used / 100.0, 0), 1))
                                            .tint(used >= 90 ? .red : used >= 70 ? .orange : .green)
                                        Text(String(format: "%.0f%%", used))
                                            .font(.callout.monospacedDigit())
                                            .frame(width: 44, alignment: .trailing)
                                        if limit.stale == true {
                                            Text("stale").font(.caption2).foregroundStyle(.secondary)
                                        }
                                    }
                                }
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            } else {
                Text("Daemon not connected.").foregroundStyle(.secondary)
            }
            Spacer()
        }
        .padding(14)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}
