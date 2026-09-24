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
    /// A store-wide issue is reported once and names the sources it touched
    /// here instead of carrying one copy per source.
    let affectedSources: [String]?

    enum CodingKeys: String, CodingKey {
        case code, source, action, severity
        case affectedSources = "affected_sources"
    }

    // Explicit init so `severity` defaults to nil at call sites (test fixtures)
    // without dropping the synthesized Decodable conformance.
    init(code: String?, source: String?, action: String?, severity: String? = nil, affectedSources: [String]? = nil) {
        self.code = code
        self.source = source
        self.action = action
        self.severity = severity
        self.affectedSources = affectedSources
    }

    /// Every source this issue names: its own source plus any affected list.
    var namedSources: [String] {
        (source.map { [$0] } ?? []) + (affectedSources ?? [])
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

/// One honest per-agent connection row from /v1/connections: whether agentacct
/// set it up (activation) joined with whether it is recording (ingestion), plus
/// its kind and the point-to-point action. The backend never claims connected/
/// recording without evidence; the view renders exactly what it vouches for.
struct V1ConnectionsPayload: Decodable {
    let schema: String
    let connections: [V1Connection]
}

struct V1Connection: Decodable, Identifiable {
    let id: String
    let displayName: String
    let kind: String            // active | semi | passive
    let configured: Bool
    let recordingState: String?
    let scope: String?
    let lastSuccessAt: Double?
    let issues: [V1IngestionIssue]
    let status: String          // recording | connected_idle | not_connected | needs_attention | reading | read_only
    let primaryAction: String?  // connect | connect_manual | resync | resolve | nil

    enum CodingKeys: String, CodingKey {
        case id, kind, configured, scope, issues, status
        case displayName = "display_name"
        case recordingState = "recording_state"
        case lastSuccessAt = "last_success_at"
        case primaryAction = "primary_action"
    }

    /// The wizard client for a client agentacct can configure. nil for a client
    /// with no onboarding writer at all (passive cursor), which therefore has no
    /// setup path. A client that is still reported as `semi` — an older recorder,
    /// or an agent whose MCP registration stays manual (openclaw) — is absent from
    /// this roster too, but the row's action is the authority in every case.
    var setupClient: SetupClient? { SetupClient(rawValue: id) }

    /// One-click Connect/Re-sync. Only an ACTIVE agent receives the backend's
    /// connect/re-sync action; a semi agent's manual registration arrives as
    /// `connect_manual` guidance, which must never render as a button.
    var offersOneClickSetup: Bool {
        setupClient != nil && (primaryAction == "connect" || primaryAction == "resync")
    }
}

/// Only this backend code is a store-wide cause projected onto each source.
/// All other diagnostics, including watcher staleness, remain independent.
struct SourceIssueGroup: Identifiable {
    static let globalReconciliationCode = "evidence_refreshable_usage_failed"
    let id: String
    private(set) var issues: [V1IngestionIssue]

    var isGlobalReconciliation: Bool { issues.first?.code == Self.globalReconciliationCode }
    var affectedSources: [String] { Array(Set(issues.flatMap(\.namedSources))).sorted() }

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
    /// Opens the setup wizard; a non-nil client pre-selects that agent (a
    /// per-row Connect/Re-sync), nil opens the general chooser.
    var onSetup: ((SetupClient?) -> Void)? = nil
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
    /// The connections card is "retained" (last-reported, unconfirmed) when
    /// EITHER store is stale: the ingestion snapshot it reads health from, or
    /// the connections array itself. Gating only on ingestion would let a stale
    /// connections array (a failed /v1/connections while /v1/ingestion still
    /// succeeds) render as live green.
    private var connectionsRetained: Bool {
        dashboard.ingestionError != nil || dashboard.connectionsError != nil
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
        Button { Task { await dashboard.refreshIngestion(); await dashboard.refreshConnections() } } label: {
            Image(systemName: "arrow.clockwise")
        }
        .buttonStyle(QuietButtonStyle(horizontalPadding: 8))
        .disabled(dashboard.isRefreshingIngestion || dashboard.isOfflineSnapshot || SnapshotMode.enabled)
        .help("Refresh source health")
        .accessibilityLabel("Refresh source health")
        .accessibilityIdentifier("sources.refresh")
        if let onSetup {
            Button("Connections") { onSetup(nil) }.buttonStyle(NativeSetupActionStyle())
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
            if let conns = dashboard.connections {
                // Surface a connections-only staleness (its endpoint failed while
                // ingestion stayed healthy) so the rows below read as unconfirmed
                // rather than silently live. When ingestion is also stale, the
                // banner above already covers it.
                if let connectionsError = dashboard.connectionsError, dashboard.ingestionError == nil {
                    retainedConnectionsBanner(connectionsError).padding(.bottom, Space.l)
                }
                connectionsCard(conns, snapshot: snapshot)
            } else {
                // An older daemon without /v1/connections: the per-source card.
                connectedCard(snapshot)
            }
            watcherCard(snapshot.watcher).padding(.top, Space.xl)
            // Gated off snapshot renders so the golden fixtures are pixel-unaffected.
            if !SnapshotMode.enabled {
                updateCard.padding(.top, Space.xl)
            }
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

    /// When every row wears the same state the rows already say it; a
    /// seventh copy in the header adds nothing.
    static func rowsShareState(_ sources: [V1IngestionSource], overall: String?, watcherRunning: Bool) -> Bool {
        guard sources.count > 1, let overall else { return false }
        // Compare what the reader sees, not the raw state: three "healthy"
        // rows can display Reporting, Reporting and Watching, and then the
        // header's Reporting is not a repeat.
        let header = overallStatusLabel(overall, watcherRunning: watcherRunning)
        return sources.allSatisfy { sourceStatusLabel($0, watcherRunning: watcherRunning) == header }
    }

    /// The live per-source status word, shared by the row lozenge and the
    /// header-repeat rule so the two can never disagree.
    static func sourceStatusLabel(_ source: V1IngestionSource, watcherRunning: Bool) -> String {
        switch source.state ?? "unknown" {
        case "healthy" where watcherRunning && (source.parsed ?? 0) > 0: return "Reporting"
        case "healthy" where watcherRunning: return "Watching · no data yet"
        case "healthy": return "Idle"
        case "degraded": return "Degraded"
        case "pending": return "Pending"
        case let state: return state.capitalized
        }
    }

    /// The live card-level status word, shared with the header lozenge.
    static func overallStatusLabel(_ state: String, watcherRunning: Bool) -> String {
        switch state {
        case "healthy" where watcherRunning: return "Reporting"
        case "healthy": return "Idle"
        case "attention": return "Attention"
        case "degraded": return "Needs a fix"
        case let state: return state.capitalized
        }
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
                    if let overall = snapshot.state,
                       presentation.isRetained || !Self.rowsShareState(sources, overall: overall, watcherRunning: watcherRunning) {
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

    // MARK: connections (per-agent)

    private func retainedConnectionsBanner(_ error: String) -> some View {
        Card(padding: Space.l) {
            HStack(alignment: .top, spacing: Space.m) {
                Image(systemName: "exclamationmark.triangle").foregroundStyle(Theme.amber)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: Space.s) {
                    Text("Current connection status unavailable").workFont(.rowLabel).foregroundStyle(Theme.ink)
                    Text("Showing the last reported agents. Statuses below are unconfirmed until the connections refresh succeeds.")
                        .workFont(.body).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                    Text(error).workFont(.caption).foregroundStyle(Theme.muted).textSelection(.enabled)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .accessibilityIdentifier("connections-retained-health")
    }

    private func connectionsCard(_ connections: [V1Connection], snapshot: V1IngestionSnapshot) -> some View {
        let watcherRunning = presentation.watcherIsCurrentlyRunning(snapshot.watcher)
        return Card(padding: 0) {
            VStack(spacing: 0) {
                adaptiveRow(spacing: Space.s) {
                    HStack(spacing: Space.s) {
                        Text(connectionsRetained ? "Last reported agents" : "Agents").workFont(.titleCard).foregroundStyle(Theme.ink)
                        Text("\(connections.count)").workFont(.dataSmall).foregroundStyle(Theme.muted)
                    }
                    if !stacksRows { Spacer() }
                    if let overall = snapshot.state {
                        overallLozenge(overall, watcherRunning: watcherRunning)
                    }
                }
                .padding(.horizontal, Space.xl).padding(.vertical, Space.m)
                .frame(minHeight: 52).frame(maxWidth: .infinity, alignment: .leading)
                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.horizontal, Space.xl)
                ForEach(Array(connections.enumerated()), id: \.element.id) { index, conn in
                    if index > 0 {
                        Rectangle().fill(Theme.hairline).frame(height: 1).padding(.horizontal, Space.xl)
                    }
                    connectionRow(conn)
                }
            }
        }
    }

    private func connectionRow(_ conn: V1Connection) -> some View {
        adaptiveRow(spacing: Space.l) {
            HStack(alignment: .top, spacing: Space.l) {
                RoundedRectangle(cornerRadius: Metrics.radius)
                    .fill(Theme.tintNeutral)
                    .frame(width: monogramSize, height: monogramSize)
                    .overlay(
                        Text(Self.monogram(conn.id))
                            .workFont(.dataSmallSemibold).foregroundStyle(Theme.muted)
                    )
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 4) {
                    Text(conn.displayName).workFont(.rowLabel).foregroundStyle(Theme.ink)
                    Text(connectionDetail(conn))
                        .workFont(.dataSmall).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                    if let note = connectionActionNote(conn) {
                        Text(note).workFont(.caption).foregroundStyle(Theme.muted)
                            .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                    }
                }
            }
            if !stacksRows { Spacer() }
            HStack(spacing: Space.s) {
                connectionStatusLozenge(conn)
                connectionActionButton(conn)
            }
        }
        .padding(.horizontal, Space.xl)
        .padding(.vertical, Space.s)
        .frame(minHeight: Metrics.rowSource)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(conn.displayName), \(connectionDetail(conn))")
    }

    private func connectionDetail(_ conn: V1Connection) -> String {
        switch conn.kind {
        case "passive":
            return conn.status == "needs_attention"
                ? "Read-only source — needs attention"
                : "Read-only — agentacct just reads its logs"
        case "semi":
            // Describe only what we observe (the automatic log import). The
            // manual MCP-registration step is surfaced as guidance in the note
            // below, never asserted here as already done.
            return conn.status == "needs_attention"
                ? "Imported from its logs — needs attention"
                : "Imported from its logs automatically"
        default:  // active
            switch conn.status {
            case "recording": return "Connected and recording"
            case "connected_idle": return "Connected — no data captured yet"
            case "needs_attention": return "Connected, but its recording needs a fix"
            case "not_connected": return "Not connected — agentacct isn't recording this agent yet"
            default: return conn.status.replacingOccurrences(of: "_", with: " ")
            }
        }
    }

    /// A per-agent guidance/fix note under the row (never fabricated — the
    /// resolve text comes from the source's own ingestion issue).
    private func connectionActionNote(_ conn: V1Connection) -> String? {
        switch conn.primaryAction {
        case "resolve":
            // Remediation comes from the source's own ingestion issue; if that
            // issue was suppressed (a source outside the live watcher's scope),
            // fall back to a generic next step so a coral row is never a
            // dead-end with no instruction.
            return conn.issues.first?.action
                ?? "This source reported a problem. Refresh Diagnostics, or check the agent's logs and read permissions."
        case "connect_manual":
            // A semi agent's MCP self-reporting is a manual terminal step; state
            // it as an available action, never as already done.
            return "MCP self-reporting is a manual step — add its MCP server in your terminal for richer receipts."
        default:
            return nil
        }
    }

    @ViewBuilder
    private func connectionStatusLozenge(_ conn: V1Connection) -> some View {
        if connectionsRetained {
            StateLozenge(text: "Last reported", tint: Theme.muted, wash: Theme.tintNeutral, pip: .hollow)
        } else {
            switch conn.status {
            case "recording", "reading":
                StateLozenge(text: conn.status == "reading" ? "Reading" : "Recording", tint: Theme.green, wash: Theme.tintGreen, pip: .filled)
            case "connected_idle":
                StateLozenge(text: "Connected", tint: Theme.muted, wash: Theme.tintNeutral, pip: .hollow)
            case "needs_attention":
                StateLozenge(text: "Needs a fix", tint: Theme.coral, wash: Theme.tintCoral, pip: .hollow)
            case "read_only":
                StateLozenge(text: "Read-only", tint: Theme.muted, wash: Theme.tintNeutral, pip: .hollow)
            case "not_connected":
                StateLozenge(text: "Not connected", tint: Theme.muted, wash: Theme.tintNeutral, pip: .hollow)
            default:
                StateLozenge(text: conn.status.replacingOccurrences(of: "_", with: " ").capitalized, tint: Theme.muted, wash: Theme.tintNeutral, pip: .hollow)
            }
        }
    }

    /// Only ACTIVE agents get a one-click Connect/Re-sync (its own idempotent
    /// `onboard --agent X`). Semi/passive agents have no wizard path, so their
    /// row shows guidance/health instead of a button.
    @ViewBuilder
    private func connectionActionButton(_ conn: V1Connection) -> some View {
        if let onSetup, !connectionsRetained, conn.offersOneClickSetup,
           let client = conn.setupClient {
            Button(conn.primaryAction == "resync" ? "Re-sync" : "Connect") { onSetup(client) }
                .buttonStyle(NativeSetupActionStyle())
                .disabled(dashboard.isOfflineSnapshot || SnapshotMode.enabled)
                .accessibilityIdentifier("connections.\(conn.id).action")
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

    // MARK: version + update

    /// Recorder version + a one-click Update when a newer release is published.
    /// Notify + one-click, never silent: the button appears only for a packaged
    /// install with an update available, and never for a dev/editable build.
    @ViewBuilder
    private var updateCard: some View {
        let info = dashboard.versionInfo
        let shownVersion = info?.displayVersion
        let updateAvailable = info?.offersInAppUpdate == true
        Card(padding: Space.xl) {
            VStack(alignment: .leading, spacing: 0) {
                adaptiveRow(spacing: Space.s) {
                    Text("Recorder version").workFont(.titleCard).foregroundStyle(Theme.ink)
                    if !stacksRows { Spacer() }
                    if let shownVersion {
                        Text(shownVersion).workFont(.dataSmall).foregroundStyle(Theme.muted).textSelection(.enabled)
                    } else {
                        Text("unknown").workFont(.dataSmall).foregroundStyle(Theme.muted)
                    }
                }
                Rectangle().fill(Theme.hairline).frame(height: 1).padding(.vertical, Space.m)
                if updateAvailable {
                    adaptiveRow(spacing: Space.s) {
                        HStack(spacing: Space.s) {
                            Image(systemName: "arrow.up.circle.fill").foregroundStyle(Theme.amber)
                                .accessibilityHidden(true)
                            Text("Update available → \(info?.latest ?? "newer")")
                                .workFont(.body).foregroundStyle(Theme.ink)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if !stacksRows { Spacer() }
                        Button(dashboard.updateRestarting ? "Updating…" : "Update") {
                            Task { try? await dashboard.applyUpdate() }
                        }
                        .buttonStyle(NativeSetupActionStyle(prominent: true))
                        .disabled(dashboard.isApplyingUpdate || dashboard.updateRestarting || dashboard.isOfflineSnapshot)
                        .accessibilityIdentifier("diagnostics.update")
                    }
                    if dashboard.updateRestarting {
                        Text("Installing the update and restarting the recorder — this pane will reconnect on its own.")
                            .workFont(.caption).foregroundStyle(Theme.muted)
                            .fixedSize(horizontal: false, vertical: true)
                            .padding(.top, Space.s)
                    }
                } else if info?.isDevInstall == true {
                    Text("Development build — update via git, not in-app.")
                        .workFont(.caption).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                } else if let error = dashboard.versionError {
                    Text(error).workFont(.caption).foregroundStyle(Theme.muted).textSelection(.enabled)
                } else {
                    Text("Up to date.").workFont(.caption).foregroundStyle(Theme.muted)
                }
            }
        }
        .accessibilityIdentifier("diagnostics-update-card")
    }

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
            Text("Usage totals may be incomplete")
                .workFont(.rowLabel).foregroundStyle(presentation.isRetained ? Theme.muted : Theme.amber)
            Text(group.affectedSources.isEmpty
                ? "Recorded usage did not reconcile cleanly. The affected sources were not identified."
                : "Recorded usage for \(group.affectedSources.joined(separator: ", ")) did not reconcile cleanly.")
                .workFont(.body).foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
            Text("Refresh usage; if it persists, run agentacct doctor before rebuilding or cleaning any store. This does not mean any client stopped recording.")
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
            // The raw code is for diagnostics, so it lives on hover; the
            // title already says the same thing in words.
            Text(issueTitle(issue))
                .workFont(.rowLabel).foregroundStyle(presentation.isRetained ? Theme.muted : issue.tint)
                .help("Diagnostic code: \(issue.code ?? "not supplied")")
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
