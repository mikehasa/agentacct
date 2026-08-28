import SwiftUI

// Sources — what feeds the evidence store, exactly as the ingestion-health
// snapshot reports it: per-source import state and recency, the continuous-
// sync watcher, actionable issues, the verifier shelf (named not-connected
// states), and the scope-transparency card. Everything on this page is a
// live-connection fact from /v1/ingestion — nothing is a capability claim.

// MARK: - /v1/ingestion wire model (additive; every field optional)

struct V1IngestionPayload: Decodable {
    let schema: String
    let ingestion: V1IngestionSnapshot
}

struct V1IngestionSnapshot: Decodable {
    let state: String?
    let lastSuccessAt: Double?
    let sources: [V1IngestionSource]?
    let watcher: V1IngestionWatcher?
    let issues: [V1IngestionIssue]?

    enum CodingKeys: String, CodingKey {
        case state, sources, watcher, issues
        case lastSuccessAt = "last_success_at"
    }
}

struct V1IngestionSource: Decodable, Identifiable {
    let source: String
    let state: String?
    let scope: String?
    let lastSuccessAt: Double?
    let lastFailureAt: Double?
    let discovered: Int?
    let parsed: Int?
    let skipped: Int?
    let errorCount: Int?

    var id: String { source }

    enum CodingKeys: String, CodingKey {
        case source, state, scope, discovered, parsed, skipped
        case lastSuccessAt = "last_success_at"
        case lastFailureAt = "last_failure_at"
        case errorCount = "error_count"
    }
}

struct V1IngestionWatcher: Decodable {
    let state: String?
    let intervalSeconds: Double?
    let heartbeatAt: Double?

    enum CodingKeys: String, CodingKey {
        case state
        case intervalSeconds = "interval_seconds"
        case heartbeatAt = "heartbeat_at"
    }
}

struct V1IngestionIssue: Decodable, Identifiable {
    let code: String?
    let source: String?
    let action: String?

    var id: String { "\(code ?? "?")-\(source ?? "*")" }
}

// MARK: - Pane

struct SourcesPane: View {
    @Environment(DashboardStore.self) var dashboard

    var body: some View {
        ScrollBox {
            VStack(alignment: .leading, spacing: 0) {
                header
                content.padding(.top, Space.xl)
            }
            .padding(Space.gutter)
            .frame(maxWidth: 1172 + Space.gutter * 2, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Evidence sources")
                .font(Type.titlePage).tracking(Type.titlePageTracking)
                .foregroundStyle(Theme.ink)
            Text("what feeds the store · capture is local only")
                .font(Type.dataSmall).foregroundStyle(Theme.muted)
        }
    }

    @ViewBuilder
    private var content: some View {
        if let snapshot = dashboard.ingestion {
            connectedCard(snapshot)
            watcherCard(snapshot.watcher).padding(.top, Space.xl)
            issuesCard(snapshot.issues ?? []).padding(.top, Space.xl)
            verifierShelf.padding(.top, Space.xl)
            scopeCard.padding(.top, Space.xl)
        } else if let error = dashboard.ingestionError {
            VStack(alignment: .leading, spacing: 4) {
                Text("Source health unavailable").font(Type.rowLabel).foregroundStyle(Theme.ink)
                Text(error).font(Type.caption).foregroundStyle(Theme.muted)
                Text("An older daemon serves no /v1/ingestion — update and restart it.")
                    .font(Type.caption).foregroundStyle(Theme.muted)
            }
            verifierShelf.padding(.top, Space.xl)
            scopeCard.padding(.top, Space.xl)
        } else {
            Text("Loading source health…").font(Type.body).foregroundStyle(Theme.muted)
        }
    }

    // MARK: connected sources

    private func connectedCard(_ snapshot: V1IngestionSnapshot) -> some View {
        let sources = (snapshot.sources ?? []).sorted { $0.source < $1.source }
        let watcherRunning = snapshot.watcher?.state == "running"
        return Card(padding: 0) {
            VStack(spacing: 0) {
                HStack(spacing: Space.s) {
                    Text("Connected sources").font(Type.titleCard).foregroundStyle(Theme.ink)
                    Text("\(sources.count)").font(Type.dataSmall).foregroundStyle(Theme.muted)
                    Spacer()
                    if let overall = snapshot.state {
                        overallLozenge(overall, watcherRunning: watcherRunning)
                    }
                }
                .padding(.horizontal, Space.xl)
                .frame(height: 52)
                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.horizontal, Space.xl)
                if sources.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("No import sources configured")
                            .font(Type.rowLabel).foregroundStyle(Theme.ink)
                        Text("Run `agentacct onboard` to wire your coding agents into the store.")
                            .font(Type.caption).foregroundStyle(Theme.muted)
                    }
                    .padding(Space.xl)
                    .frame(maxWidth: .infinity, alignment: .leading)
                } else {
                    ForEach(Array(sources.enumerated()), id: \.element.id) { index, source in
                        if index > 0 {
                            Rectangle().fill(Theme.hairline).frame(height: 1)
                                .padding(.horizontal, Space.xl)
                        }
                        sourceRow(source, watcherRunning: watcherRunning)
                    }
                }
            }
        }
    }

    private func sourceRow(_ source: V1IngestionSource, watcherRunning: Bool) -> some View {
        HStack(alignment: .center, spacing: Space.l) {
            RoundedRectangle(cornerRadius: Metrics.radius)
                .fill(Theme.tintNeutral)
                .frame(width: 36, height: 36)
                .overlay(
                    Text(Self.monogram(source.source))
                        .font(Type.dataSmallSemibold).foregroundStyle(Theme.muted)
                )
            VStack(alignment: .leading, spacing: 4) {
                Text(source.source).font(Type.rowLabel).foregroundStyle(Theme.ink)
                Text(sourceDetail(source, watcherRunning: watcherRunning))
                    .font(Type.dataSmall).foregroundStyle(Theme.muted)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 4) {
                if let ago = agoText(source.lastSuccessAt) {
                    Text("last import \(ago)").font(Type.dataSmall).foregroundStyle(Theme.muted)
                } else {
                    Text("no successful import yet").font(Type.dataSmall).foregroundStyle(Theme.muted)
                }
                if let errors = source.errorCount, errors > 0 {
                    Text("\(errors) error\(errors == 1 ? "" : "s")")
                        .font(Type.dataSmall).foregroundStyle(Theme.coral)
                }
            }
            sourceLozenge(source, watcherRunning: watcherRunning)
        }
        .padding(.horizontal, Space.xl)
        .frame(minHeight: Metrics.rowSource)
    }

    /// Two-letter monogram that actually distinguishes sources: hyphenated
    /// names take their parts' initials (claude-code → CC); plain names take
    /// first + last letter (opencode → OE, openclaw → OW, codex → CX).
    static func monogram(_ name: String) -> String {
        let parts = name.split(whereSeparator: { $0 == "-" || $0 == "_" })
        if parts.count >= 2 {
            return parts.prefix(2).compactMap { $0.first.map(String.init) }.joined().uppercased()
        }
        guard let first = name.first, let last = name.last, name.count > 1 else {
            return name.uppercased()
        }
        return String([first, last]).uppercased()
    }

    /// The row's fact line, built only from reported numbers. "watched" is a
    /// live claim, so it degrades to "configured" while the importer is down.
    private func sourceDetail(_ source: V1IngestionSource, watcherRunning: Bool) -> String {
        var parts: [String] = []
        if let scope = source.scope {
            parts.append(scope == "watched" && !watcherRunning ? "configured" : scope)
        }
        if let discovered = source.discovered { parts.append("\(discovered) files discovered") }
        if let parsed = source.parsed { parts.append("\(parsed) parsed") }
        if let skipped = source.skipped, skipped > 0 { parts.append("\(skipped) skipped") }
        return parts.isEmpty ? "no scan recorded" : parts.joined(separator: " · ")
    }

    /// Per-source lozenge — green "Reporting" is a LIVE-connection fact: it
    /// requires a healthy source, a running watcher, and rows actually
    /// parsed. A healthy source under a stopped watcher is "Idle"; a watch
    /// that has never yielded a row is "Watching", not "Reporting".
    @ViewBuilder
    private func sourceLozenge(_ source: V1IngestionSource, watcherRunning: Bool) -> some View {
        switch source.state ?? "unknown" {
        case "healthy" where watcherRunning && (source.parsed ?? 0) > 0:
            StateLozenge(text: "Reporting", tint: Theme.green, wash: Theme.tintGreen, pip: .filled)
        case "healthy" where watcherRunning:
            StateLozenge(text: "Watching · no data yet", tint: Theme.muted, wash: Theme.tintNeutral, pip: .hollow)
        case "healthy":
            StateLozenge(text: "Idle", tint: Theme.muted, wash: Theme.tintNeutral, pip: .hollow)
        case "degraded":
            StateLozenge(text: "Degraded", tint: Theme.amber, wash: Theme.tintAmber, pip: .hollow)
        case "pending":
            StateLozenge(text: "Pending", tint: Theme.muted, wash: Theme.tintNeutral, pip: .hollow)
        case let state:
            StateLozenge(text: state.capitalized, tint: Theme.muted, wash: Theme.tintNeutral, pip: .hollow)
        }
    }

    /// The card-level roll-up follows the same live-fact rule.
    @ViewBuilder
    private func overallLozenge(_ state: String, watcherRunning: Bool) -> some View {
        switch state {
        case "healthy" where watcherRunning:
            StateLozenge(text: "Reporting", tint: Theme.green, wash: Theme.tintGreen, pip: .filled)
        case "healthy":
            StateLozenge(text: "Idle", tint: Theme.muted, wash: Theme.tintNeutral, pip: .hollow)
        case "degraded":
            StateLozenge(text: "Degraded", tint: Theme.amber, wash: Theme.tintAmber, pip: .hollow)
        case let state:
            StateLozenge(text: state.capitalized, tint: Theme.muted, wash: Theme.tintNeutral, pip: .hollow)
        }
    }

    // MARK: watcher

    @ViewBuilder
    private func watcherCard(_ watcher: V1IngestionWatcher?) -> some View {
        Card(padding: Space.xl) {
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: Space.s) {
                    Text("Continuous sync").font(Type.titleCard).foregroundStyle(Theme.ink)
                    Spacer()
                    switch watcher?.state {
                    case "running":
                        StateLozenge(text: "Running", tint: Theme.green, wash: Theme.tintGreen, pip: .filled)
                    case "stale":
                        StateLozenge(text: "Stale", tint: Theme.amber, wash: Theme.tintAmber, pip: .hollow)
                    case "stopped":
                        StateLozenge(text: "Stopped", tint: Theme.coral, wash: Theme.tintCoral, pip: .hollow)
                    case "not_configured":
                        StateLozenge(text: "Not configured", tint: Theme.muted, wash: Theme.tintNeutral, pip: .hollow)
                    default:
                        StateLozenge(text: "Unknown", tint: Theme.muted, wash: Theme.tintNeutral, pip: .hollow)
                    }
                }
                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.vertical, Space.m)
                Text(watcherDetail(watcher))
                    .font(Type.caption).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// State-dependent copy: present-tense "keeps the store current" is only
    /// true while the watcher is actually running.
    private func watcherDetail(_ watcher: V1IngestionWatcher?) -> String {
        guard let watcher else { return "The daemon reported no watcher block." }
        let heartbeat = agoText(watcher.heartbeatAt).map { "last heartbeat \($0)" } ?? "no heartbeat recorded"
        let cadenceSeconds = watcher.intervalSeconds.map { Int($0.rounded()) }
        switch watcher.state {
        case "running":
            let cadence = cadenceSeconds.map { " · scans every \($0)s" } ?? ""
            return "The importer keeps the store current in the background — \(heartbeat)\(cadence)"
        case "stale":
            let cadence = cadenceSeconds.map { " (expected every \($0)s)" } ?? ""
            return "The importer's heartbeat is overdue — \(heartbeat)\(cadence)"
        case "stopped":
            let cadence = cadenceSeconds.map { " (expected every \($0)s)" } ?? ""
            return "Importer stopped — \(heartbeat)\(cadence). Start it with `agentacct start`."
        case "not_configured":
            return "No continuous sync is configured — imports happen only on manual scans."
        default:
            return heartbeat
        }
    }

    // MARK: issues

    @ViewBuilder
    private func issuesCard(_ issues: [V1IngestionIssue]) -> some View {
        if !issues.isEmpty {
            Card(padding: Space.xl) {
                VStack(alignment: .leading, spacing: 0) {
                    Text("Needs attention (\(issues.count))")
                        .font(Type.titleCard).foregroundStyle(Theme.ink)
                    Rectangle().fill(Theme.hairline).frame(height: 1).padding(.vertical, Space.m)
                    VStack(alignment: .leading, spacing: Space.m) {
                        ForEach(issues) { issue in
                            HStack(alignment: .firstTextBaseline, spacing: Space.m) {
                                VStack(alignment: .leading, spacing: 2) {
                                    HStack(spacing: Space.s) {
                                        Text(issueTitle(issue))
                                            .font(Type.rowLabel).foregroundStyle(Theme.amber)
                                        Text(issue.code ?? "")
                                            .font(Type.dataSmall).foregroundStyle(Theme.muted)
                                    }
                                    Text(issue.action ?? "see `agentacct doctor`")
                                        .font(Type.caption).foregroundStyle(Theme.muted)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    /// Human phrasing first; the raw code stays beside it for diagnostics.
    private func issueTitle(_ issue: V1IngestionIssue) -> String {
        let phrase = (issue.code ?? "issue")
            .replacingOccurrences(of: "_", with: " ")
        let sentence = phrase.prefix(1).uppercased() + phrase.dropFirst()
        if let source = issue.source {
            return "\(sentence) — \(source)"
        }
        return sentence
    }

    // MARK: verifier shelf

    private var verifierShelf: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            CapsLabel(text: "Verifiers · not connected · upgrade self-checked claims to verified")
            HStack(alignment: .top, spacing: Space.xl) {
                verifierCard(
                    name: "CI check runs",
                    provides: "independent check results recorded against receipts"
                )
                verifierCard(
                    name: "Human reviewer",
                    provides: "finding review and approval dispositions"
                )
            }
        }
    }

    private func verifierCard(name: String, provides: String) -> some View {
        Card(padding: Space.xl) {
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: Space.m) {
                    RoundedRectangle(cornerRadius: Metrics.radius)
                        .fill(Theme.tintNeutral)
                        .frame(width: 36, height: 36)
                        .overlay(EvidencePip(shape: .hollow, tint: Theme.muted, radius: 6))
                    VStack(alignment: .leading, spacing: 3) {
                        Text(name).font(Type.rowLabel).foregroundStyle(Theme.ink)
                        Text(provides).font(Type.dataSmall).foregroundStyle(Theme.muted)
                    }
                    Spacer()
                    HStack(spacing: 6) {
                        EvidencePip(shape: .verified, tint: Theme.muted)
                        Text("→ verified").font(Type.captionSemibold).foregroundStyle(Theme.muted)
                    }
                }

            }
        }
    }

    // MARK: scope transparency

    private var scopeCard: some View {
        Card(padding: Space.xl) {
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: Space.s) {
                    StatusDot(color: Theme.green, size: 8)
                    Text("Local only — nothing leaves this machine")
                        .font(Type.rowLabel).foregroundStyle(Theme.ink)
                    Spacer()
                    Text("store: \(GlanceClient.storeDir().path)")
                        .font(Type.dataSmall).foregroundStyle(Theme.muted)
                        .lineLimit(1).truncationMode(.middle)
                        .frame(maxWidth: 420, alignment: .trailing)
                }
                Text("Reads tool names, commands, file paths, exit codes, timestamps, and token counts from your agents' own local logs — never file contents or prompts.")
                    .font(Type.dataSmall).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, Space.s)
            }
        }
    }
}

/// A v7 status lozenge: h22 rx4 tint wash, pip + 12/600 text.
struct StateLozenge: View {
    let text: String
    let tint: Color
    let wash: Color
    let pip: PipShape

    var body: some View {
        HStack(spacing: 6) {
            EvidencePip(shape: pip, tint: tint)
            Text(text).font(Type.captionSemibold).foregroundStyle(tint)
        }
        .padding(.horizontal, 11)
        .frame(height: Metrics.tierBadgeH)
        .background(wash, in: RoundedRectangle(cornerRadius: Metrics.radius))
    }
}
