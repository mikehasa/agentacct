import SwiftUI

struct SourcesUnavailablePresentation: Equatable {
    let title: String
    let detail: String
    let recovery: String
    let diagnostic: String?

    init(title: String, detail: String, recovery: String, diagnostic: String? = nil) {
        self.title = title
        self.detail = detail
        self.recovery = recovery
        self.diagnostic = diagnostic
    }

    init(failure: IngestionFailure) {
        switch failure {
        case .unsupported:
            self.init(
                title: "Source health unavailable",
                detail: "This version of agentacct cannot report source health.",
                recovery: "Update agentacct, restart it, then refresh."
            )
        case .serviceUnavailable:
            self.init(
                title: "agentacct is not running",
                detail: "Source health and background updates are unavailable.",
                recovery: "In Terminal, run agentacct start, then refresh."
            )
        case .requestFailed(let detail):
            self.init(
                title: "Source health unavailable",
                detail: "The latest source health request did not complete.",
                recovery: "Refresh after agentacct is reachable.",
                diagnostic: detail
            )
        }
    }
}

struct EvidenceRolePresentation: Equatable {
    let name: String
    let detail: String
    let outcome: String

    static let ci = Self(
        name: "CI checks",
        detail: "Not connected",
        outcome: "Independent check evidence"
    )
    static let human = Self(
        name: "Human review",
        detail: "Records review and resolution decisions",
        outcome: "Review assertion"
    )
}

struct LocalStorePrivacyPresentation: Equatable {
    let stored: String
    let defaultExclusions: String
    let restrictedOptIn: String
    let derivedLabels: String

    static let current = Self(
        stored: "Stored locally: tool categories, reported file paths, exit codes, timestamps, and imported token counts.",
        defaultExclusions: "Default capture excludes raw prompts, file contents, non-command tool arguments, and tool output. Execute command lines may be stored locally after length limits and best-effort secret masking.",
        restrictedOptIn: "Restricted evidence can include raw content only with explicit opt-in; recognized secret fields are rejected.",
        derivedLabels: "Some sources may derive short labels from local client data."
    )
}

enum SourcesContentMode: Equatable {
    case loading
    case current
    case unavailable
    case retainedFailure
}

func sourcesContentMode(
    hasSnapshot: Bool,
    failure: IngestionFailure?
) -> SourcesContentMode {
    if failure != nil { return hasSnapshot ? .retainedFailure : .unavailable }
    return hasSnapshot ? .current : .loading
}

// Sources combines live /v1/ingestion state with clearly separated product
// policy: evidence roles and the local-store privacy contract. A failed
// refresh always outranks retained ingestion data so older status cannot read
// as current.

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
            Text("Data sources")
                .font(Type.titlePage).tracking(Type.titlePageTracking)
                .foregroundStyle(Theme.ink)
            Text("Local activity data used by this dashboard")
                .font(Type.dataSmall).foregroundStyle(Theme.muted)
        }
    }

    @ViewBuilder
    private var content: some View {
        switch sourcesContentMode(
            hasSnapshot: dashboard.ingestion != nil,
            failure: dashboard.ingestionFailure
        ) {
        case .retainedFailure:
            if let failure = dashboard.ingestionFailure,
               let snapshot = dashboard.ingestion {
                failureCard(failure)
                CapsLabel(text: "Last loaded source status")
                    .padding(.top, Space.xl)
                connectedCard(snapshot, isRetained: true).padding(.top, Space.m)
            }
            verifierShelf.padding(.top, Space.xl)
            scopeCard.padding(.top, Space.xl)
        case .unavailable:
            if let failure = dashboard.ingestionFailure {
                failureCard(failure)
            }
            verifierShelf.padding(.top, Space.xl)
            scopeCard.padding(.top, Space.xl)
        case .current:
            if let snapshot = dashboard.ingestion {
                connectedCard(snapshot)
                watcherCard(snapshot.watcher).padding(.top, Space.xl)
                issuesCard(snapshot.issues ?? []).padding(.top, Space.xl)
            }
            verifierShelf.padding(.top, Space.xl)
            scopeCard.padding(.top, Space.xl)
        case .loading:
            Text("Loading source health…").font(Type.body).foregroundStyle(Theme.muted)
        }
    }

    private func failureCard(_ failure: IngestionFailure) -> some View {
        let presentation = SourcesUnavailablePresentation(failure: failure)
        return Card(padding: Space.xl) {
            VStack(alignment: .leading, spacing: 4) {
                Label(presentation.title, systemImage: "exclamationmark.triangle.fill")
                    .font(Type.rowLabel).foregroundStyle(Theme.amber)
                Text(presentation.detail).font(Type.caption).foregroundStyle(Theme.muted)
                    .help(presentation.diagnostic ?? presentation.detail)
                Text(presentation.recovery).font(Type.caption).foregroundStyle(Theme.muted)
            }
        }
    }

    // MARK: connected sources

    private func connectedCard(
        _ snapshot: V1IngestionSnapshot,
        isRetained: Bool = false
    ) -> some View {
        let sources = (snapshot.sources ?? []).sorted { $0.source < $1.source }
        let watcherRunning = !isRetained && snapshot.watcher?.state == "running"
        return Card(padding: 0) {
            VStack(spacing: 0) {
                HStack(spacing: Space.s) {
                    Text("Imported sources").font(Type.titleCard).foregroundStyle(Theme.ink)
                    Text("\(sources.count)").font(Type.dataSmall).foregroundStyle(Theme.muted)
                    Spacer()
                    if isRetained {
                        StateLozenge(text: "Older data", tint: Theme.muted, wash: Theme.tintNeutral, pip: .hollow)
                    } else if let overall = snapshot.state {
                        overallLozenge(overall, watcherRunning: watcherRunning)
                    }
                }
                .padding(.horizontal, Space.xl)
                .frame(height: 52)
                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.horizontal, Space.xl)
                if sources.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("No coding apps connected")
                            .font(Type.rowLabel).foregroundStyle(Theme.ink)
                        Text("In Terminal, run agentacct onboard to connect supported coding apps.")
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
                        sourceRow(
                            source,
                            watcherRunning: watcherRunning,
                            isRetained: isRetained
                        )
                    }
                }
            }
        }
    }

    private func sourceRow(
        _ source: V1IngestionSource,
        watcherRunning: Bool,
        isRetained: Bool
    ) -> some View {
        HStack(alignment: .center, spacing: Space.l) {
            RoundedRectangle(cornerRadius: Metrics.radius)
                .fill(Theme.tintNeutral)
                .frame(width: 36, height: 36)
                .overlay(
                    Text(Self.monogram(source.source))
                        .font(Type.dataSmallSemibold).foregroundStyle(Theme.muted)
                )
            VStack(alignment: .leading, spacing: 4) {
                Text(clientDisplayName(source.source)).font(Type.rowLabel).foregroundStyle(Theme.ink)
                Text(sourceDetail(source, watcherRunning: watcherRunning))
                    .font(Type.dataSmall).foregroundStyle(Theme.muted)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 4) {
                if let ago = agoText(source.lastSuccessAt) {
                    Text("Imported \(ago)").font(Type.dataSmall).foregroundStyle(Theme.muted)
                } else {
                    Text("No successful import yet").font(Type.dataSmall).foregroundStyle(Theme.muted)
                }
                if let errors = source.errorCount, errors > 0 {
                    Text("\(errors) error\(errors == 1 ? "" : "s")")
                        .font(Type.dataSmall).foregroundStyle(Theme.coral)
                }
            }
            if !isRetained {
                sourceLozenge(source, watcherRunning: watcherRunning)
            }
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
            switch scope {
            case "watched" where watcherRunning:
                parts.append("background monitoring")
            case "watched":
                parts.append("configured for background monitoring")
            default:
                parts.append(scope.replacingOccurrences(of: "_", with: " "))
            }
        }
        if let discovered = source.discovered { parts.append("\(discovered) files found") }
        if let parsed = source.parsed { parts.append("\(parsed) imported") }
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
                    Text("Background updates").font(Type.titleCard).foregroundStyle(Theme.ink)
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
        guard let watcher else { return "Background update status was not reported." }
        let heartbeat = agoText(watcher.heartbeatAt).map { "last check \($0)" } ?? "no check time recorded"
        let cadence = TemporalText.interval(seconds: watcher.intervalSeconds)
        switch watcher.state {
        case "running":
            return cadence.map { "Checks for new activity every \($0)." }
                ?? "Checks for new activity in the background."
        case "stale":
            let expected = cadence.map { "; expected every \($0)" } ?? ""
            return "Updates are delayed — \(heartbeat)\(expected)."
        case "stopped":
            let expected = cadence.map { "; expected every \($0)" } ?? ""
            return "Background updates stopped — \(heartbeat)\(expected). In Terminal, run agentacct start."
        case "not_configured":
            return "Background updates are not configured. Data changes only after a manual import."
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
                                    Text(issue.action ?? "In Terminal, run agentacct doctor.")
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
            return "\(sentence) — \(clientDisplayName(source))"
        }
        return sentence
    }

    // MARK: verifier shelf

    private var verifierShelf: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            CapsLabel(text: "Evidence and review roles")
            HStack(alignment: .top, spacing: Space.xl) {
                verifierCard(.ci)
                verifierCard(.human)
            }
        }
    }

    private func verifierCard(_ role: EvidenceRolePresentation) -> some View {
        Card(padding: Space.xl) {
            VStack(alignment: .leading, spacing: Space.m) {
                HStack(alignment: .top, spacing: Space.m) {
                    RoundedRectangle(cornerRadius: Metrics.radius)
                        .fill(Theme.tintNeutral)
                        .frame(width: 36, height: 36)
                        .overlay(EvidencePip(shape: .hollow, tint: Theme.muted, radius: 6))
                    VStack(alignment: .leading, spacing: 3) {
                        Text(role.name).font(Type.rowLabel).foregroundStyle(Theme.ink)
                        Text(role.detail).font(Type.dataSmall).foregroundStyle(Theme.muted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: 0)
                }
                HStack(spacing: 6) {
                    EvidencePip(shape: .hollow, tint: Theme.muted)
                    Text(role.outcome).font(Type.captionSemibold).foregroundStyle(Theme.muted)
                }
            }
        }
    }

    // MARK: scope transparency

    private var scopeCard: some View {
        let privacy = LocalStorePrivacyPresentation.current
        return Card(padding: Space.xl) {
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: Space.s) {
                    StatusDot(color: Theme.green, size: 8)
                    Text("Local agentacct store")
                        .font(Type.rowLabel).foregroundStyle(Theme.ink)
                    Spacer()
                    Text(GlanceClient.storeDir().path)
                        .font(Type.dataSmall).foregroundStyle(Theme.muted)
                        .lineLimit(1).truncationMode(.middle)
                        .frame(maxWidth: 420, alignment: .trailing)
                }
                VStack(alignment: .leading, spacing: 4) {
                    Text(privacy.stored)
                    Text(privacy.defaultExclusions)
                    Text(privacy.restrictedOptIn)
                    Text(privacy.derivedLabels)
                }
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
