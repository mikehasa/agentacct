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
    /// error | attention | advisory | transient. Absent → treated as error, so a
    /// new issue is never silently demoted to a quiet note.
    let severity: String?

    // Explicit init so `severity` defaults to nil at call sites (test fixtures)
    // without dropping the synthesized Decodable conformance.
    init(code: String?, source: String?, action: String?, severity: String? = nil) {
        self.code = code
        self.source = source
        self.action = action
        self.severity = severity
    }

    var id: String { "\(code ?? "?")-\(source ?? "*")" }

    /// Only errors and attention items belong in the loud card; advisories and
    /// self-healing transients are quiet notes that never paint the panel red.
    var isAlert: Bool { (severity ?? "error") == "error" || severity == "attention" }

    var tint: Color {
        switch severity {
        case "attention": return Theme.amber
        case "advisory", "transient": return Theme.muted
        default: return Theme.coral
        }
    }
}

/// Only this backend code is a store-wide cause projected onto each source.
/// All other diagnostics, including watcher staleness, remain independent.
struct SourceIssueGroup: Identifiable {
    static let globalReconciliationCode = "evidence_refreshable_usage_failed"
    let id: String
    private(set) var issues: [V1IngestionIssue]

    var isGlobalReconciliation: Bool { issues.first?.code == Self.globalReconciliationCode }
    var affectedSources: [String] { Array(Set(issues.compactMap(\.source))).sorted() }

    static func group(_ issues: [V1IngestionIssue]) -> [Self] {
        var result: [Self] = []
        var globalIndex: Int?
        for (index, issue) in issues.enumerated() {
            if issue.code == globalReconciliationCode {
                if let globalIndex {
                    result[globalIndex].issues.append(issue)
                } else {
                    globalIndex = result.count
                    result.append(Self(id: "global:\(globalReconciliationCode)", issues: [issue]))
                }
            } else {
                // Repeated source/code pairs can carry different diagnostics.
                // Retain every original row instead of inferring a common cause.
                result.append(Self(id: "issue:\(index):\(issue.id)", issues: [issue]))
            }
        }
        return result
    }
}

struct SourceHealthPresentation {
    let refreshError: String?
    var isRetained: Bool { refreshError != nil }

    func watcherIsCurrentlyRunning(_ watcher: V1IngestionWatcher?) -> Bool {
        !isRetained && watcher?.state == "running"
    }

    func retainedStatus(_ state: String?) -> String {
        "Last reported: \((state ?? "unknown").replacingOccurrences(of: "_", with: " ").capitalized)"
    }
}

// MARK: - Pane

struct SourcesPane: View {
    var onSetup: (() -> Void)? = nil
    @Environment(DashboardStore.self) var dashboard
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @ScaledMetric(relativeTo: .caption) private var scaledMonogramSize: CGFloat = 36
    private var stacksRows: Bool { dynamicTypeSize.isAccessibilitySize }
    private var monogramSize: CGFloat {
        WorkTypeScale.resolved(base: 36, systemScaled: scaledMonogramSize, dynamicTypeSize: dynamicTypeSize)
    }
    private var presentation: SourceHealthPresentation {
        SourceHealthPresentation(refreshError: dashboard.ingestionError)
    }

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
        .workFont(.body)
    }

    private func adaptiveRow<Content: View>(spacing: CGFloat, alignment: VerticalAlignment = .center, @ViewBuilder content: () -> Content) -> some View {
        let layout = stacksRows
            ? AnyLayout(VStackLayout(alignment: .leading, spacing: spacing))
            : AnyLayout(HStackLayout(alignment: alignment, spacing: spacing))
        return layout { content() }
    }

    private var header: some View {
        adaptiveRow(spacing: Space.l) {
        VStack(alignment: .leading, spacing: 6) {
            Text("Diagnostics")
                .workFont(.titlePage).tracking(Type.titlePageTracking)
                .foregroundStyle(Theme.ink)
            Text("Your data sources and the recorder's health — and anything that needs a look")
                .workFont(.dataSmall).foregroundStyle(Theme.muted)
        }
        if !stacksRows { Spacer() }
        Button { Task { await dashboard.refreshIngestion() } } label: {
            Image(systemName: "arrow.clockwise")
        }
        .buttonStyle(QuietButtonStyle(horizontalPadding: 8))
        .disabled(dashboard.isRefreshingIngestion || dashboard.isOfflineSnapshot || SnapshotMode.enabled)
        .help("Refresh source health")
        .accessibilityLabel("Refresh source health")
        .accessibilityIdentifier("sources.refresh")
        if let onSetup {
            Button("Connections", action: onSetup).buttonStyle(NativeSetupActionStyle())
                .accessibilityIdentifier("sources.connections")
        }
        }
    }

    @ViewBuilder
    private var content: some View {
        if let snapshot = dashboard.ingestion {
            if let error = dashboard.ingestionError {
                retainedHealthBanner(error).padding(.bottom, Space.l)
            }
            issuesCard(snapshot.issues ?? []).padding(.bottom, (snapshot.issues ?? []).isEmpty ? 0 : Space.l)
            connectedCard(snapshot)
            watcherCard(snapshot.watcher).padding(.top, Space.xl)
            verificationDisclosure.padding(.top, Space.xl)
            scopeCard.padding(.top, Space.xl)
        } else if let error = dashboard.ingestionError {
            VStack(alignment: .leading, spacing: 4) {
                Text("Source health unavailable").workFont(.rowLabel).foregroundStyle(Theme.ink)
                Text(error).workFont(.caption).foregroundStyle(Theme.muted)
                Text("Reconnect the recorder and refresh source health to load current diagnostics.")
                    .workFont(.caption).foregroundStyle(Theme.muted)
            }
            verificationDisclosure.padding(.top, Space.xl)
            scopeCard.padding(.top, Space.xl)
        } else {
            Text("Loading source health…").workFont(.body).foregroundStyle(Theme.muted)
        }
    }

    // MARK: connected sources

    private func retainedHealthBanner(_ error: String) -> some View {
        Card(padding: Space.l) {
            HStack(alignment: .top, spacing: Space.m) {
                Image(systemName: "exclamationmark.triangle").foregroundStyle(Theme.amber)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: Space.s) {
                    Text("Current source health unavailable").workFont(.rowLabel).foregroundStyle(Theme.ink)
                    Text("Showing the previous source snapshot. Statuses below are last reported; current recording and watcher health are unconfirmed.")
                        .workFont(.body).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                    Text(error).workFont(.caption).foregroundStyle(Theme.muted).textSelection(.enabled)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .accessibilityIdentifier("sources-retained-health")
    }

    private func connectedCard(_ snapshot: V1IngestionSnapshot) -> some View {
        let sources = (snapshot.sources ?? []).sorted { $0.source < $1.source }
        let watcherRunning = presentation.watcherIsCurrentlyRunning(snapshot.watcher)
        return Card(padding: 0) {
            VStack(spacing: 0) {
                adaptiveRow(spacing: Space.s) {
                    HStack(spacing: Space.s) {
                        Text(presentation.isRetained ? "Last reported sources" : "Connected sources").workFont(.titleCard).foregroundStyle(Theme.ink)
                        Text("\(sources.count)").workFont(.dataSmall).foregroundStyle(Theme.muted)
                    }
                    if !stacksRows { Spacer() }
                    if let overall = snapshot.state {
                        overallLozenge(overall, watcherRunning: watcherRunning)
                    }
                }
                .padding(.horizontal, Space.xl)
                .padding(.vertical, Space.m)
                .frame(minHeight: 52)
                .frame(maxWidth: .infinity, alignment: .leading)
                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.horizontal, Space.xl)
                if sources.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("No import sources configured")
                            .workFont(.rowLabel).foregroundStyle(Theme.ink)
                        Text("Use Connections to add a coding client.")
                            .workFont(.caption).foregroundStyle(Theme.muted)
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
        adaptiveRow(spacing: Space.l) {
            HStack(alignment: .top, spacing: Space.l) {
                RoundedRectangle(cornerRadius: Metrics.radius)
                    .fill(Theme.tintNeutral)
                    .frame(width: monogramSize, height: monogramSize)
                    .overlay(
                        Text(Self.monogram(source.source))
                            .workFont(.dataSmallSemibold).foregroundStyle(Theme.muted)
                    )
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 4) {
                    Text(source.source).workFont(.rowLabel).foregroundStyle(Theme.ink)
                    Text(sourceDetail(source, watcherRunning: watcherRunning))
                        .workFont(.dataSmall).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if !stacksRows { Spacer() }
            VStack(alignment: stacksRows ? .leading : .trailing, spacing: 4) {
                if let ago = agoText(source.lastSuccessAt) {
                    Text("last import \(ago)").workFont(.dataSmall).foregroundStyle(Theme.muted)
                } else {
                    Text("no successful import yet").workFont(.dataSmall).foregroundStyle(Theme.muted)
                }
                if let errors = source.errorCount, errors > 0 {
                    Text("\(errors) error\(errors == 1 ? "" : "s")")
                        .workFont(.dataSmall).foregroundStyle(presentation.isRetained ? Theme.muted : Theme.coral)
                }
            }
            sourceLozenge(source, watcherRunning: watcherRunning)
        }
        .padding(.horizontal, Space.xl)
        .padding(.vertical, Space.s)
        .frame(minHeight: Metrics.rowSource)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
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
        if presentation.isRetained {
            StateLozenge(text: presentation.retainedStatus(source.state), tint: Theme.muted, wash: Theme.tintNeutral, pip: .hollow)
        } else {
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
    }

    /// The card-level roll-up follows the same live-fact rule.
    @ViewBuilder
    private func overallLozenge(_ state: String, watcherRunning: Bool) -> some View {
        if presentation.isRetained {
            StateLozenge(text: presentation.retainedStatus(state), tint: Theme.muted, wash: Theme.tintNeutral, pip: .hollow)
        } else {
            switch state {
            case "healthy" where watcherRunning:
                StateLozenge(text: "Reporting", tint: Theme.green, wash: Theme.tintGreen, pip: .filled)
            case "healthy":
                StateLozenge(text: "Idle", tint: Theme.muted, wash: Theme.tintNeutral, pip: .hollow)
            case "attention":
                StateLozenge(text: "Attention", tint: Theme.amber, wash: Theme.tintAmber, pip: .hollow)
            case "degraded":
                StateLozenge(text: "Needs a fix", tint: Theme.coral, wash: Theme.tintCoral, pip: .hollow)
            case let state:
                StateLozenge(text: state.capitalized, tint: Theme.muted, wash: Theme.tintNeutral, pip: .hollow)
            }
        }
    }

    // MARK: watcher

    @ViewBuilder
    private func watcherCard(_ watcher: V1IngestionWatcher?) -> some View {
        Card(padding: Space.xl) {
            VStack(alignment: .leading, spacing: 0) {
                adaptiveRow(spacing: Space.s) {
                    Text("Continuous sync").workFont(.titleCard).foregroundStyle(Theme.ink)
                    if !stacksRows { Spacer() }
                    if presentation.isRetained {
                        StateLozenge(text: presentation.retainedStatus(watcher?.state), tint: Theme.muted, wash: Theme.tintNeutral, pip: .hollow)
                    } else {
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
                }
                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.vertical, Space.m)
                Text(watcherDetail(watcher))
                    .workFont(.caption).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// State-dependent copy: present-tense "keeps the store current" is only
    /// true while the watcher is actually running.
    private func watcherDetail(_ watcher: V1IngestionWatcher?) -> String {
        guard let watcher else { return "The daemon reported no watcher block." }
        let heartbeat = agoText(watcher.heartbeatAt).map { "last heartbeat \($0)" } ?? "no heartbeat recorded"
        if presentation.isRetained {
            return "The previous snapshot reported \(watcher.state ?? "an unknown state") · \(heartbeat). Current watcher activity is unconfirmed."
        }
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
            return "Importer stopped — \(heartbeat)\(cadence). Open recording health to reconnect."
        case "not_configured":
            return "No continuous sync is configured — imports happen only on manual scans."
        default:
            return heartbeat
        }
    }

    // MARK: issues

    @ViewBuilder
    private func issuesCard(_ issues: [V1IngestionIssue]) -> some View {
        // Loud (error/attention) vs quiet (advisory/transient): the alarming card
        // is only for things that actually need action; everything else is a
        // calm note so a self-healing system never reads as broken.
        let alerts = issues.filter { $0.isAlert }
        let notes = issues.filter { !$0.isAlert }
        VStack(alignment: .leading, spacing: Space.l) {
            if !alerts.isEmpty { alertsCard(alerts) }
            // Only reassure that imports are fine when there's no real alert
            // sitting right above saying otherwise.
            if !notes.isEmpty { notesCard(notes, reassure: alerts.isEmpty) }
        }
    }

    private func alertsCard(_ issues: [V1IngestionIssue]) -> some View {
        let groups = SourceIssueGroup.group(issues)
        return Card(padding: Space.xl) {
            VStack(alignment: .leading, spacing: 0) {
                adaptiveRow(spacing: Space.s, alignment: .firstTextBaseline) {
                    Text("\(presentation.isRetained ? "Previously reported" : "Needs attention") (\(groups.count))")
                        .workFont(.titleCard).foregroundStyle(Theme.ink)
                    Text("\(issues.count) diagnostic \(issues.count == 1 ? "report" : "reports")")
                        .workFont(.caption).foregroundStyle(Theme.muted)
                }
                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.vertical, Space.m)
                VStack(alignment: .leading, spacing: Space.xl) {
                    ForEach(groups) { group in
                        if group.isGlobalReconciliation {
                            sharedReconciliationIssue(group)
                        } else if let issue = group.issues.first {
                            originalDiagnostic(issue)
                        }
                    }
                }
            }
        }
    }

    /// Quiet, non-alarming notes: cosmetic advisories (e.g. a dev version
    /// mismatch) and self-healing transients. Never red; reassures that data
    /// is still importing.
    private func notesCard(_ notes: [V1IngestionIssue], reassure: Bool) -> some View {
        Card(padding: Space.xl) {
            VStack(alignment: .leading, spacing: Space.m) {
                Text("Notes").workFont(.titleCard).foregroundStyle(Theme.ink)
                ForEach(notes) { note in
                    HStack(alignment: .top, spacing: Space.s) {
                        Image(systemName: "info.circle").workFont(.caption).foregroundStyle(Theme.muted)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(issueTitle(note)).workFont(.rowLabel).foregroundStyle(Theme.ink)
                            Text(note.action ?? "Nothing to do — this clears on its own.")
                                .workFont(.caption).foregroundStyle(Theme.muted)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
                if reassure {
                    Text("Your data is still importing normally.")
                        .workFont(.caption).foregroundStyle(Theme.muted)
                }
            }
        }
    }

    private func sharedReconciliationIssue(_ group: SourceIssueGroup) -> some View {
        VStack(alignment: .leading, spacing: Space.m) {
            Text("Evidence reconciliation needs review")
                .workFont(.rowLabel).foregroundStyle(presentation.isRetained ? Theme.muted : Theme.amber)
            Text(group.affectedSources.isEmpty
                ? "One global reconciliation fault is reported. Affected sources were not identified."
                : "One global reconciliation fault is reported across \(group.affectedSources.count) source \(group.affectedSources.count == 1 ? "summary" : "summaries").")
                .workFont(.body).foregroundStyle(Theme.ink)
            if !group.affectedSources.isEmpty {
                Text("Affected sources: \(group.affectedSources.joined(separator: ", "))")
                    .workFont(.caption).foregroundStyle(Theme.muted)
            }
            Text("Usage history may be incomplete or conflicting. This shared fault does not establish that every affected client stopped recording.")
                .workFont(.caption).foregroundStyle(Theme.muted).fixedSize(horizontal: false, vertical: true)
            DisclosureGroup {
                VStack(alignment: .leading, spacing: Space.l) {
                    ForEach(Array(group.issues.enumerated()), id: \.offset) { _, issue in
                        originalDiagnostic(issue)
                    }
                }
                .padding(.top, Space.m)
            } label: {
                Text("Original diagnostics (\(group.issues.count))")
                    .workFont(.captionSemibold).foregroundStyle(Theme.ink)
            }
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("sources-reconciliation-diagnostics")
        }
    }

    private func originalDiagnostic(_ issue: V1IngestionIssue) -> some View {
        VStack(alignment: .leading, spacing: Space.s) {
            Text(issueTitle(issue))
                .workFont(.rowLabel).foregroundStyle(presentation.isRetained ? Theme.muted : issue.tint)
            Text(issue.code ?? "code not supplied").workFont(.dataSmall).foregroundStyle(Theme.muted)
            Text(issue.action ?? "See agentacct doctor for source diagnostics.")
                .workFont(.caption).foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
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

    private var verificationDisclosure: some View {
        DisclosureGroup("Verification connections · not connected") {
            verifierShelf.padding(.top, Space.m)
        }
        .workFont(.caption)
        .accessibilityIdentifier("sources.verification-connections")
    }

    private var verifierShelf: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            Text("Independent evidence can support verification. These connections are not configured.")
                .workFont(.caption).foregroundStyle(Theme.muted)
            adaptiveRow(spacing: Space.xl, alignment: .top) {
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
                adaptiveRow(spacing: Space.m) {
                    RoundedRectangle(cornerRadius: Metrics.radius)
                        .fill(Theme.tintNeutral)
                        .frame(width: monogramSize, height: monogramSize)
                        .overlay(EvidencePip(shape: .hollow, tint: Theme.muted, radius: 6))
                    VStack(alignment: .leading, spacing: 3) {
                        Text(name).workFont(.rowLabel).foregroundStyle(Theme.ink)
                        Text(provides).workFont(.dataSmall).foregroundStyle(Theme.muted)
                    }
                    if !stacksRows { Spacer() }
                    HStack(spacing: 6) {
                        EvidencePip(shape: .verified, tint: Theme.muted)
                        Text("→ verified").workFont(.captionSemibold).foregroundStyle(Theme.muted)
                    }
                }

            }
        }
    }

    // MARK: scope transparency

    private var scopeCard: some View {
        Card(padding: Space.xl) {
            VStack(alignment: .leading, spacing: 0) {
                adaptiveRow(spacing: Space.s) {
                    HStack(spacing: Space.s) {
                        StatusDot(color: Theme.green, size: 8)
                        Text("Local evidence store")
                            .workFont(.rowLabel).foregroundStyle(Theme.ink)
                            .fixedSize(horizontal: false, vertical: true)
                        ContextHelp(title: "What is stored locally",
                            message: "Imports activity and usage from local client logs, plus work sections and checks reported by agents. Records can include summaries, commands, file paths, exit codes and artifact references.",
                            identifier: "sources.capture-scope")
                    }
                    if !stacksRows { Spacer() }
                    Text("store: \(SnapshotMode.enabled ? "/synthetic-review/state" : ((try? GlanceClient.storeDir())?.path ?? "invalid configuration"))")
                        .workFont(.dataSmall).foregroundStyle(Theme.muted)
                        .lineLimit(stacksRows ? nil : 1).truncationMode(.middle)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: stacksRows ? .infinity : 420, alignment: stacksRows ? .leading : .trailing)
                        .textSelection(.enabled)
                }
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
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @ScaledMetric(relativeTo: .caption) private var scaledMinimumHeight = Metrics.tierBadgeH

    var body: some View {
        HStack(spacing: 6) {
            EvidencePip(shape: pip, tint: tint)
            Text(text).workFont(.captionSemibold).foregroundStyle(tint)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 11)
        .padding(.vertical, 3)
        .frame(minHeight: WorkTypeScale.resolved(base: Metrics.tierBadgeH, systemScaled: scaledMinimumHeight, dynamicTypeSize: dynamicTypeSize))
        .background(wash, in: RoundedRectangle(cornerRadius: Metrics.radius))
    }
}
