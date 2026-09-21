import Foundation
import Observation

/// A capture observation must identify one client and one immutable record.
/// Callers supply actual receipt observation time, never an HTTP response time.
struct RecordingCaptureObservation: Equatable {
    let clientID: String
    let eventID: String
    let observedAt: Date
    var taskID: String? = nil
}

enum RecordingHealthTone: String, Equatable, Codable {
    case neutral, positive, caution, failure
}

enum RecordingHealthAction: String, Equatable, Codable {
    case setup, sources, refresh

    var title: String {
        switch self {
        case .setup: return "Open Connections"
        case .sources: return "View Diagnostics"
        case .refresh: return "Check again"
        }
    }
}

enum RecordingHealthScope: String, Equatable, Hashable, Codable {
    case setup, endpoint, ingestion
}

struct RecordingHealthCause: Equatable, Identifiable, Codable {
    let id: String
    let scope: RecordingHealthScope
    let title: String
    let detail: String
    let tone: RecordingHealthTone
    let action: RecordingHealthAction
    let affectedSources: [String]
    let recoveryDetail: String

    var recoveryTitle: String {
        switch id {
        case "endpoint:unreachable": return "Recorder connection restored"
        case "endpoint:incompatible": return "Recorder compatibility restored"
        case "setup:failed": return "Setup command finished"
        case "ingestion:unavailable": return "Source health is available again"
        case "ingestion:watcher": return "Import watcher reports running"
        case "ingestion:evidence_refreshable_usage_failed": return "Reconciliation issue no longer reported"
        default: return "Source issue no longer reported"
        }
    }

    /// The recorder-unreachable fault is the one a one-click "Start recorder"
    /// (an in-app `agentacct start`) can address. Every other cause routes to
    /// setup or diagnostics instead, so only this one carries the restart button.
    var isRecorderUnreachable: Bool { id == "endpoint:unreachable" }
}

struct RecordingHealthDimension: Equatable, Identifiable {
    let id: String
    let title: String
    let value: String
    let detail: String
    let tone: RecordingHealthTone
}

struct RecordingClientCapture: Equatable, Identifiable {
    let id: String
    let lastObservation: RecordingCaptureObservation?
    let requiredAfter: Date?
    let confirmed: Bool

    var title: String { recordingHealthClientName(id) }
    var status: String { confirmed ? "Capture observed" : "Awaiting capture confirmation" }
}

/// A presentation projection only. Installation, endpoint liveness, import
/// health and client capture remain separate facts throughout this model.
struct RecordingHealthSnapshot: Equatable {
    let title: String
    let tone: RecordingHealthTone
    let dimensions: [RecordingHealthDimension]
    let clients: [RecordingClientCapture]
    let causes: [RecordingHealthCause]
    /// Missing or failed fetches cannot resolve a previous ingestion fault.
    let resolutionScopes: Set<RecordingHealthScope>
    var resolutionExclusions: Set<String> = []

    @MainActor
    static func project(
        glancePhase: GlanceState.Phase,
        setupPhase: SetupModel.Phase,
        ingestion: V1IngestionSnapshot?,
        ingestionError: String?,
        canSetUp: Bool = true,
        needsSetup: Bool = false,
        configuredClientIDs: [String] = [],
        captures: [RecordingCaptureObservation] = [],
        requiredCaptureAfter: [String: Date] = [:]
    ) -> RecordingHealthSnapshot {
        var dimensions: [RecordingHealthDimension] = []
        var causes: [RecordingHealthCause] = []
        var resolutionScopes: Set<RecordingHealthScope> = []
        var resolutionExclusions: Set<String> = []
        var title = "Checking recording"
        var tone = RecordingHealthTone.neutral
        var endpointReachable = false

        switch glancePhase {
        case .connecting:
            dimensions.append(.init(id: "endpoint", title: "Recorder", value: "Checking connection", detail: "Waiting for the local recorder to respond.", tone: .neutral))
        case .disconnected(let message):
            title = "Recorder unreachable"
            tone = .failure
            dimensions.append(.init(id: "endpoint", title: "Recorder", value: "Unreachable", detail: message, tone: .failure))
            causes.append(.init(id: "endpoint:unreachable", scope: .endpoint, title: "Recorder is unreachable", detail: "New recording cannot be confirmed. Saved work remains available where already loaded.", tone: .failure, action: .setup, affectedSources: [], recoveryDetail: "The recorder responds again. This confirms the connection; client capture still needs its own evidence."))
        case .incompatible(let message):
            title = "Recorder needs attention"
            tone = .failure
            dimensions.append(.init(id: "endpoint", title: "Recorder", value: "Version mismatch", detail: message, tone: .failure))
            causes.append(.init(id: "endpoint:incompatible", scope: .endpoint, title: "Recorder version needs attention", detail: message, tone: .failure, action: .setup, affectedSources: [], recoveryDetail: "The recorder now serves a compatible response. Capture and coverage are checked separately."))
        case .connected:
            endpointReachable = true
            resolutionScopes.insert(.endpoint)
            title = "Recorder reachable"
            dimensions.append(.init(id: "endpoint", title: "Recorder", value: "Reachable", detail: "The local recorder responded. This alone does not confirm capture from each client.", tone: .positive))
        }

        switch setupPhase {
        case .idle:
            dimensions.append(.init(id: "setup", title: "Configuration", value: needsSetup ? "Setup is pending" : "Manage in Connections", detail: canSetUp ? "Client configuration and capture confirmation are separate checks." : "Review Connections for the available recorder setup and recovery options.", tone: .neutral))
            if needsSetup { title = "Set up recording"; tone = .neutral }
        case .working(let stage):
            title = "Setting up recording"
            tone = .neutral
            dimensions.append(.init(id: "setup", title: "Configuration", value: "In progress", detail: stage, tone: .neutral))
        case .failed(let message):
            title = "Setup needs attention"
            tone = .failure
            dimensions.append(.init(id: "setup", title: "Configuration", value: "Needs attention", detail: message, tone: .failure))
            causes.append(.init(id: "setup:failed", scope: .setup, title: "Recording setup needs attention", detail: message, tone: .failure, action: .setup, affectedSources: [], recoveryDetail: "The setup command finished. Review its log for skipped client settings, then open a new client session to confirm capture."))
        case .done:
            resolutionScopes.insert(.setup)
            dimensions.append(.init(id: "setup", title: "Configuration", value: "Setup command finished", detail: "Review the setup log for skipped settings or warnings. A successful command does not prove client capture.", tone: .neutral))
        }

        // An error supersedes retained data: never paint an old snapshot green.
        if let ingestionError {
            dimensions.append(.init(id: "imports", title: "Imports and coverage", value: "Current health unavailable", detail: ingestionError, tone: .caution))
            if endpointReachable {
                causes.append(.init(id: "ingestion:unavailable", scope: .ingestion, title: "Source health is unavailable", detail: "The recorder responds, but current import health and coverage cannot be checked.", tone: .caution, action: .refresh, affectedSources: [], recoveryDetail: "Source health is available again. Review the reported coverage separately."))
            }
        } else if let ingestion, endpointReachable {
            if ingestion.issues != nil, let state = ingestion.state, state != "unknown" {
                resolutionScopes.insert(.ingestion)
            }
            let watcher = ingestion.watcher?.state
            if watcher != "running" { resolutionExclusions.insert("ingestion:watcher") }
            dimensions.append(.init(id: "imports", title: "Continuous import", value: watcherValue(watcher), detail: "Importer state reported by the recorder. A successful scan can contain only older records.", tone: watcher == "running" ? .positive : .caution))
            if watcher == "stopped" || watcher == "stale" || (ingestion.issues ?? []).contains(where: { $0.code == "watcher_stale" }) {
                causes.append(.init(id: "ingestion:watcher", scope: .ingestion, title: "Continuous import needs attention", detail: watcher == "stopped" ? "The import watcher is stopped. New usage may be delayed." : "The import watcher heartbeat is stale. New usage may be delayed.", tone: .caution, action: .setup, affectedSources: [], recoveryDetail: "The recorder reports a running import watcher. Check client capture separately; the exact bounds of any missed recording remain unknown."))
            }
            causes += groupedIssues(ingestion.issues ?? [])
            let hasIssues = !(ingestion.issues ?? []).isEmpty || ingestion.state == "degraded"
            dimensions.append(.init(id: "coverage", title: "Evidence coverage", value: hasIssues ? "Needs review" : ingestion.issues == nil ? "Not assessed" : "No issues reported", detail: hasIssues ? "Reported import or attribution issues may affect coverage. New capture does not repair missing or conflicting history." : "A health snapshot cannot establish complete historical coverage.", tone: hasIssues ? .caution : .neutral))
            if ingestion.state == "degraded", (ingestion.issues ?? []).isEmpty {
                causes.append(.init(id: "ingestion:degraded", scope: .ingestion, title: "Import coverage needs review", detail: "The recorder reports degraded ingestion without a specific cause. Open Diagnostics for the reported source states.", tone: .caution, action: .sources, affectedSources: [], recoveryDetail: "The recorder no longer reports degraded ingestion. Historical completeness is still not established."))
            }
        } else {
            dimensions.append(.init(id: "imports", title: "Imports and coverage", value: "Not confirmed", detail: ingestion == nil ? "Waiting for source health." : "The last source snapshot is retained, but current health cannot be confirmed while the recorder is unreachable.", tone: .neutral))
        }

        let clients = Array(Set(configuredClientIDs)).sorted().map { id in
            let observation = captures.filter { $0.clientID == id && !$0.eventID.isEmpty }
                .max { $0.observedAt < $1.observedAt }
            let after = requiredCaptureAfter[id]
            // No activation/recovery boundary means we cannot use a historical
            // receipt to certify the current configuration.
            let confirmed = after.map { boundary in observation.map { $0.observedAt > boundary } ?? false } ?? false
            return RecordingClientCapture(id: id, lastObservation: observation, requiredAfter: after, confirmed: confirmed)
        }
        let confirmedCount = clients.filter(\.confirmed).count
        dimensions.append(.init(id: "capture", title: "Client capture", value: clients.isEmpty ? "Not individually confirmed" : "\(confirmedCount) of \(clients.count) confirmed", detail: "Confirmation requires a fresh record attributed to that client after its setup or recovery check. Silence does not establish that an agent stopped.", tone: .neutral))

        if endpointReachable, !causes.isEmpty, tone != .failure {
            title = "Recording needs review"
            tone = .caution
        } else if endpointReachable, clients.contains(where: { !$0.confirmed }), setupPhase == .done {
            title = "Waiting for client capture"
            tone = .neutral
        }
        return .init(title: title, tone: tone, dimensions: dimensions, clients: clients, causes: causes, resolutionScopes: resolutionScopes, resolutionExclusions: resolutionExclusions)
    }

    private static func watcherValue(_ state: String?) -> String {
        switch state {
        case "running": return "Watcher running"
        case "stale": return "Watcher heartbeat stale"
        case "stopped": return "Watcher stopped"
        case "not_configured": return "Watcher not configured"
        default: return "Watcher state unknown"
        }
    }

    static func groupedIssues(_ issues: [V1IngestionIssue]) -> [RecordingHealthCause] {
        // This backend code is explicitly a global reconciliation failure
        // replicated onto each source. Other source codes stay independently
        // scoped; identical words alone are not evidence of a shared cause.
        let globalCode = "evidence_refreshable_usage_failed"
        let grouped = Dictionary(grouping: issues.filter { $0.code != "watcher_stale" }) { issue in
            issue.code == globalCode ? "ingestion:\(globalCode)" : "ingestion:\(issue.code ?? "unknown"):\(issue.source ?? "global")"
        }
        return grouped.keys.sorted().compactMap { key in
            guard let entries = grouped[key], let first = entries.first else { return nil }
            let isGlobal = first.code == globalCode
            let sources = Array(Set(entries.flatMap(\.namedSources))).sorted()
            let title = isGlobal ? "Usage totals may be incomplete" : readableIssue(first.code)
            let detail = isGlobal
                ? "One reconciliation fault is reported for \(Fmt.count(sources.count, "source")). Recorded usage may be incomplete or conflicting; this does not establish that any client stopped recording."
                : first.action ?? "Review the source diagnostics for the reported issue."
            return .init(id: key, scope: .ingestion, title: title, detail: detail, tone: .caution, action: .sources, affectedSources: sources, recoveryDetail: "The latest source health no longer reports this issue. Any historical gap still requires separate evidence to establish its extent.")
        }
    }

    private static func readableIssue(_ code: String?) -> String {
        switch code {
        case "source_identity_unresolved": return "Source identity needs review"
        case "watcher_version_mismatch": return "Importer version needs attention"
        case "source_read_permission_required": return "Source access needs attention"
        case "health_state_corrupt": return "Source health data needs attention"
        default: return code?.replacingOccurrences(of: "_", with: " ").capitalized ?? "Source issue needs review"
        }
    }
}

struct RecordingHealthNotice: Equatable, Identifiable {
    let id: String
    let cause: RecordingHealthCause
    let observedAt: Date
    var recoveredAt: Date?
    var dismissed = false

    var isRecovered: Bool { recoveredAt != nil }
    /// A recovered notice can open Connections, but must not revive its old
    /// error as a new current cause in the recovery page.
    var actionableCause: RecordingHealthCause? { isRecovered ? nil : cause }
    var title: String { isRecovered ? cause.recoveryTitle : cause.title }
    var detail: String { isRecovered ? cause.recoveryDetail : cause.detail }
}

/// In-window notices only: no system notification permission, focus changes,
/// automatic navigation or modal presentation. Dismissal affects the notice,
/// never the persistent health projection or its underlying evidence.
@MainActor
@Observable
final class RecordingHealthCoordinator {
    private(set) var notices: [RecordingHealthNotice] = []
    private(set) var activeCauseIDs: Set<String> = []
    @ObservationIgnored private var episode = 0

    var visibleNotices: [RecordingHealthNotice] {
        let visible = notices.filter { !$0.dismissed }
        // Keep stable episode order within each group while placing present
        // problems before retained success messages from earlier recoveries.
        return visible.filter { !$0.isRecovered } + visible.filter(\.isRecovered)
    }
    var recentRecoveries: [RecordingHealthNotice] { Array(notices.filter(\.isRecovered).suffix(5).reversed()) }

    func update(_ snapshot: RecordingHealthSnapshot, now: Date = Date()) {
        let current = Set(snapshot.causes.map(\.id))
        for index in notices.indices where !notices[index].isRecovered {
            let cause = notices[index].cause
            if !current.contains(cause.id), snapshot.resolutionScopes.contains(cause.scope), !snapshot.resolutionExclusions.contains(cause.id) {
                notices[index].recoveredAt = now
                // Recovery is observable even when the original notice was
                // dismissed. It makes no stronger claim than the recovered lane.
                notices[index].dismissed = false
                activeCauseIDs.remove(cause.id)
            }
        }
        for cause in snapshot.causes {
            if activeCauseIDs.insert(cause.id).inserted {
                episode += 1
                notices.append(.init(id: "\(cause.id):\(episode)", cause: cause, observedAt: now))
            } else if let index = notices.lastIndex(where: { $0.cause.id == cause.id && !$0.isRecovered }) {
                let old = notices[index]
                notices[index] = .init(id: old.id, cause: cause, observedAt: old.observedAt, dismissed: old.dismissed)
            }
        }
        // Keep active episodes and a bounded recovery history.
        let retainedRecoveredIDs = Set(notices.filter(\.isRecovered).suffix(20).map(\.id))
        notices.removeAll { $0.isRecovered && !retainedRecoveredIDs.contains($0.id) }
    }

    func dismiss(_ id: String) {
        guard let index = notices.firstIndex(where: { $0.id == id }) else { return }
        notices[index].dismissed = true
    }
}

func recordingHealthClientName(_ id: String) -> String {
    switch id {
    case "claude-code", "claude_code", "claude": return "Claude Code"
    case "codex": return "Codex"
    case "opencode": return "OpenCode"
    case "openclaw": return "OpenClaw"
    case "hermes": return "Hermes"
    case "cursor": return "Cursor"
    default: return id
    }
}
