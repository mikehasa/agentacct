import Foundation
import Observation
import SwiftUI

/// Named state variants used only by deterministic offscreen review tooling.
/// Keeping the mutation inside DashboardStore preserves its private setters;
/// the live initializer and network lifecycle remain unchanged.
enum SnapshotWorkStoreState {
    case populated
    case listLoading
    case empty
    case listError
    case listErrorWithRetainedData
    case receiptLoading
    case receiptError
    case receiptStale
    case attentionReceipt
}

enum SnapshotSourcesStoreState {
    case loading
    case healthy
    case degraded
    case unsupported
    case serviceUnavailable
    case requestFailed
    case retainedFailure
}

struct SnapshotUsageStoreState {
    /// Keep the selected range and its matching response inseparable in
    /// deterministic renders; a stale summary must never wear a new range.
    let days: Int
    let summary: UsageSummary
}

enum IngestionFailure: Equatable {
    case unsupported
    case serviceUnavailable
    case requestFailed(String)
}

// Data for the full window: /v1/tasks and /v1/receipt supply task-level work
// evidence, /v1/session supplies each Receipt's expandable session detail, and
// /v1/plan supplies attributed aggregates. The legacy /usage/summary cube
// still feeds cost charts (no /v1 twin yet). Honesty rides the payloads; the
// store never re-derives a number.

@MainActor
@Observable
final class DashboardStore {
    private(set) var planClients: [V1PlanClient] = []
    private(set) var usage: UsageSummary?
    private(set) var receiptTasks: [ReceiptSummary] = []
    private(set) var totalReceiptTasks: Int?
    private(set) var receiptTasksTruncated: Bool?
    private(set) var receiptAttention: ReceiptAttentionPayload?
    private(set) var receipt: Receipt?
    private(set) var receiptListError: String?
    private(set) var receiptError: String?
    private(set) var receiptErrorTaskId: String?
    private(set) var receiptLoadingTaskId: String?
    /// Session deep views preloaded by key ("client::session"). Only the offscreen
    /// snapshot path fills this (the live app loads each drill row lazily via a
    /// SwiftUI `.task`, while deterministic rendering cannot wait on network
    /// work); a drill row reads it as a fallback so its steps render in a snapshot.
    private(set) var preloadedSessions: [String: V1SessionDetail] = [:]
    private(set) var errorText: String?
    /// Source/watcher health from /v1/ingestion (the Sources pane).
    private(set) var ingestion: V1IngestionSnapshot?
    private(set) var ingestionFailure: IngestionFailure?
    private(set) var isRefreshing = false
    private(set) var isLoadingReceipts = false
    private(set) var lastUpdated: Date?
    /// Freshness of the independently published receipt collection.
    /// A Work-only retry must not relabel the other dashboard panes as fresh.
    private(set) var receiptListLastUpdated: Date?
    /// Freshness of the independently published plan + recorded-usage pair.
    /// Receipt-list failures must not make a successful usage refresh look old.
    private(set) var usageLastUpdated: Date?

    /// The usage-pane range (7/30/90 trailing days). Defaults to 7 so the
    /// per-model plan breakdown lines up with the 7d headline out of the box
    /// (a 30-day accumulation reads as >100% of a weekly plan and confuses);
    /// the today/7d headline windows are fixed regardless of this.
    private(set) var usageDays = 7

    /// Monotonic token so rapid range switches can't land out of order and a
    /// failed fetch can't leave the old data labeled with the new range.
    @ObservationIgnored private var usageDaysGeneration = 0

    @ObservationIgnored private let client = GlanceClient()

    init() {}

    /// Design-review tooling: populate the same state the daemon endpoints
    /// would, without network access or a developer's local account data.
    init(
        preloaded fixture: DashboardSnapshotFixture,
        workState: SnapshotWorkStoreState = .populated,
        usageState: SnapshotUsageStoreState? = nil,
        sourcesState: SnapshotSourcesStoreState = .loading
    ) {
        planClients = fixture.plan.clients
        usage = usageState?.summary ?? fixture.usage
        usageDays = usageState?.days ?? 7
        switch workState {
        case .populated:
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptTasksTruncated = fixture.tasks.truncated
            receiptAttention = fixture.tasks.attention
            receipt = fixture.work?.receipt
            for session in fixture.work?.sessions ?? [] {
                let key = "\(session.session.client)::\(session.session.clientSessionId)"
                preloadedSessions[key] = session
            }
        case .listLoading:
            receiptTasks = []
            isLoadingReceipts = true
        case .empty:
            receiptTasks = []
            totalReceiptTasks = 0
            receiptTasksTruncated = false
        case .listError:
            receiptTasks = []
            receiptListError = "receipts fetch failed: synthetic review error"
        case .listErrorWithRetainedData:
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptTasksTruncated = fixture.tasks.truncated
            receiptAttention = fixture.tasks.attention
            receiptListError = "receipts fetch failed: synthetic review error"
        case .receiptLoading:
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptTasksTruncated = fixture.tasks.truncated
            receiptAttention = fixture.tasks.attention
        case .receiptError:
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptTasksTruncated = fixture.tasks.truncated
            receiptAttention = fixture.tasks.attention
            receiptError = "receipt fetch failed: synthetic review error"
            receiptErrorTaskId = fixture.work?.receipt.taskId
        case .receiptStale:
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptTasksTruncated = fixture.tasks.truncated
            receiptAttention = fixture.tasks.attention
            receipt = fixture.work?.receipt
            receiptError = "receipt refresh failed: synthetic review error"
            receiptErrorTaskId = fixture.work?.receipt.taskId
        case .attentionReceipt:
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptTasksTruncated = fixture.tasks.truncated
            receiptAttention = fixture.tasks.attention
            receipt = fixture.work?.attentionReceipt
        }
        let updated = fixture.glance.generatedAt.map(Date.init(timeIntervalSince1970:))
        lastUpdated = updated
        switch workState {
        case .listLoading, .listError:
            receiptListLastUpdated = nil
        default:
            receiptListLastUpdated = updated
        }
        usageLastUpdated = updated
        let generatedAt = fixture.glance.generatedAt ?? 0
        switch sourcesState {
        case .loading:
            break
        case .healthy:
            ingestion = Self.snapshotIngestion(generatedAt: generatedAt, degraded: false)
        case .degraded:
            ingestion = Self.snapshotIngestion(generatedAt: generatedAt, degraded: true)
        case .unsupported:
            ingestionFailure = .unsupported
        case .serviceUnavailable:
            ingestionFailure = .serviceUnavailable
        case .requestFailed:
            ingestionFailure = .requestFailed("The connection ended before a response arrived.")
        case .retainedFailure:
            ingestion = Self.snapshotIngestion(generatedAt: generatedAt, degraded: false)
            ingestionFailure = .requestFailed("The connection ended before a response arrived.")
        }
    }

    private static func snapshotIngestion(
        generatedAt: Double,
        degraded: Bool
    ) -> V1IngestionSnapshot {
        V1IngestionSnapshot(
            state: degraded ? "degraded" : "healthy",
            lastSuccessAt: generatedAt - 35,
            sources: [
                V1IngestionSource(
                    source: "claude-code",
                    state: "healthy",
                    scope: "watched",
                    lastSuccessAt: generatedAt - 35,
                    lastFailureAt: nil,
                    discovered: 42,
                    parsed: 42,
                    skipped: 0,
                    errorCount: 0
                ),
                V1IngestionSource(
                    source: "codex",
                    state: degraded ? "degraded" : "healthy",
                    scope: "watched",
                    lastSuccessAt: generatedAt - 95,
                    lastFailureAt: degraded ? generatedAt - 20 : nil,
                    discovered: 31,
                    parsed: degraded ? 29 : 31,
                    skipped: degraded ? 2 : 0,
                    errorCount: degraded ? 1 : 0
                ),
            ],
            watcher: V1IngestionWatcher(
                state: degraded ? "stale" : "running",
                intervalSeconds: 30,
                heartbeatAt: generatedAt - (degraded ? 330 : 5)
            ),
            issues: degraded
                ? [V1IngestionIssue(
                    code: "codex_import_delayed",
                    source: "codex",
                    action: "Check Codex access, then refresh the source."
                )]
                : []
        )
    }

    func refresh() async {
        guard !isRefreshing else { return }
        isRefreshing = true
        let receiptListGeneration = beginReceiptListLoad()
        defer {
            isRefreshing = false
        }
        let days = usageDays
        let rangeGeneration = usageDaysGeneration
        // Launch independent lanes together, but publish each error through
        // its own state so a successful range request cannot hide a stale Task
        // list (or vice versa).
        async let tasksRequest: ReceiptTasksPayload = client.getAuthed("/v1/tasks?limit=200")
        async let planRequest: V1PlanPayload = client.getAuthed("/v1/plan?days=\(days)")
        async let usageRequest: UsageSummary = client.getLocal("/usage/summary?days=\(days)")
        async let ingestionRequest: V1IngestionPayload = client.getAuthed("/v1/ingestion")

        var tasksSucceeded = false
        do {
            let tasks = try await tasksRequest
            if receiptListGeneration == self.receiptListGeneration {
                publishReceiptTasks(tasks)
                tasksSucceeded = true
            }
        } catch GlanceClientError.noDiscovery(_) {
            if !Task.isCancelled,
               receiptListGeneration == self.receiptListGeneration {
                receiptListError = "daemon not running (no discovery file) — start it with `agentacct start`"
            }
        } catch {
            if receiptListGeneration == self.receiptListGeneration,
               !requestWasCancelled(error, taskIsCancelled: Task.isCancelled) {
                receiptListError = "receipts fetch failed: \(error.localizedDescription)"
            }
        }
        endReceiptListLoad(generation: receiptListGeneration)

        do {
            let payload = try await ingestionRequest
            ingestion = payload.ingestion
            ingestionFailure = nil
        } catch GlanceClientError.http(404) {
            // An older daemon without the route: a named state, not an error toast.
            if !Task.isCancelled {
                ingestionFailure = .unsupported
            }
        } catch GlanceClientError.noDiscovery(_) {
            if !Task.isCancelled {
                ingestionFailure = .serviceUnavailable
            }
        } catch {
            if !requestWasCancelled(error, taskIsCancelled: Task.isCancelled) {
                ingestionFailure = .requestFailed(error.localizedDescription)
            }
        }

        do {
            let (plan, summary) = try await (planRequest, usageRequest)
            guard rangeGeneration == usageDaysGeneration, days == usageDays else { return }
            planClients = plan.clients
            usage = summary
            errorText = nil
            let updated = Date()
            usageLastUpdated = updated
            if tasksSucceeded { lastUpdated = updated }
        } catch GlanceClientError.noDiscovery(_) {
            guard !Task.isCancelled,
                  rangeGeneration == usageDaysGeneration else { return }
            errorText = "daemon not running (no discovery file) — start it with `agentacct start`"
        } catch {
            guard rangeGeneration == usageDaysGeneration,
                  !requestWasCancelled(error, taskIsCancelled: Task.isCancelled) else { return }
            errorText = "daemon fetch failed: \(error.localizedDescription)"
        }
    }

    /// The Task list for the Receipts pane (one compact Receipt summary each).
    func fetchReceipts() async {
        let generation = beginReceiptListLoad()
        defer { endReceiptListLoad(generation: generation) }
        do {
            let payload: ReceiptTasksPayload = try await client.getAuthed("/v1/tasks?limit=200")
            guard generation == receiptListGeneration else { return }
            publishReceiptTasks(payload)
        } catch GlanceClientError.noDiscovery(_) {
            guard !Task.isCancelled,
                  generation == receiptListGeneration else { return }
            receiptListError = "daemon not running (no discovery file) — start it with `agentacct start`"
        } catch {
            guard generation == receiptListGeneration,
                  !requestWasCancelled(error, taskIsCancelled: Task.isCancelled) else { return }
            receiptListError = "receipts fetch failed: \(error.localizedDescription)"
        }
    }

    /// One Task's full Receipt. 404 (task unknown / recorded elsewhere) is a
    /// first-class message; a CANCELLED fetch (the user picked another Task)
    /// writes nothing so a late error can't mask the fresh receipt. A
    /// generation token guards against UNCANCELLED racers too (a disposition
    /// post's refresh runs in its own unstructured Task): only the newest
    /// fetch may write, so a late straggler can never wedge another task's
    /// page. A same-task refresh keeps the current receipt on screen instead
    /// of unmounting the record page for the rebuild.
    @ObservationIgnored private var receiptGeneration = 0
    @ObservationIgnored private var receiptListGeneration = 0

    @discardableResult
    private func beginReceiptListLoad() -> Int {
        receiptListGeneration += 1
        isLoadingReceipts = true
        return receiptListGeneration
    }

    private func endReceiptListLoad(generation: Int) {
        guard generation == receiptListGeneration else { return }
        isLoadingReceipts = false
    }

    private func publishReceiptTasks(_ payload: ReceiptTasksPayload) {
        receiptTasks = payload.tasks
        totalReceiptTasks = payload.total
        receiptTasksTruncated = payload.truncated
        receiptAttention = payload.attention
        receiptListError = nil
        receiptListLastUpdated = Date()
    }

    func fetchReceipt(taskId: String) async {
        receiptGeneration += 1
        let generation = receiptGeneration
        if receipt?.taskId != taskId {
            receipt = nil
            receiptError = nil
            receiptErrorTaskId = nil
        }
        receiptLoadingTaskId = taskId
        defer {
            if generation == receiptGeneration { receiptLoadingTaskId = nil }
        }
        do {
            let encoded = taskId.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? taskId
            let payload: Receipt = try await client.getAuthed("/v1/receipt?task=\(encoded)")
            guard !Task.isCancelled, generation == receiptGeneration else { return }
            receipt = payload
            receiptError = nil
            receiptErrorTaskId = nil
            receiptLoadingTaskId = nil
        } catch is CancellationError {
            if generation == receiptGeneration { receiptLoadingTaskId = nil }
            return
        } catch let error as URLError where error.code == .cancelled {
            if generation == receiptGeneration { receiptLoadingTaskId = nil }
            return
        } catch GlanceClientError.http(404) {
            guard !Task.isCancelled, generation == receiptGeneration else { return }
            receiptError = "this Task is not in the store (it may have been recorded elsewhere)"
            receiptErrorTaskId = taskId
            receiptLoadingTaskId = nil
        } catch {
            guard !Task.isCancelled, generation == receiptGeneration else { return }
            receiptError = "receipt fetch failed: \(error.localizedDescription)"
            receiptErrorTaskId = taskId
            receiptLoadingTaskId = nil
        }
    }

    /// Load one session for a Receipt drill row. Each row owns its result, so
    /// several expanded sessions can remain visible at the same time.
    func loadSession(client clientName: String, sessionId: String) async throws -> V1SessionDetail {
        let encodedClient = clientName.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? clientName
        let encodedSession = sessionId.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? sessionId
        return try await client.getAuthed(
            "/v1/session?client=\(encodedClient)&session_id=\(encodedSession)"
        )
    }

    /// Record one human attention disposition (finding or blocker) and refresh
    /// the open receipt so the new state is what the user sees next. Throws
    /// with the daemon's own conflict/not-found detail on failure.
    func postDisposition(
        kind: String,
        action: String,
        expectedRevision: Int,
        note: String?,
        targetDigest: String? = nil,
        blockedEventId: String? = nil,
        refreshTaskId: String? = nil
    ) async throws {
        var body: [String: Any] = [
            "kind": kind,
            "action": action,
            "expected_revision": expectedRevision,
        ]
        // The server rejects control characters and >1200 chars in a note;
        // normalize what a paste can legally contain instead of bouncing the
        // user off a 409 for invisible newlines.
        let normalizedNote = note?
            .components(separatedBy: .newlines)
            .joined(separator: " ")
            .trimmingCharacters(in: .whitespaces)
            .prefix(1200)
        if let normalizedNote, !normalizedNote.isEmpty { body["note"] = String(normalizedNote) }
        if let targetDigest { body["target_digest"] = targetDigest }
        if let blockedEventId { body["blocked_event_id"] = blockedEventId }
        do {
            let _: DispositionResponse = try await client.postAuthed("/v1/disposition", body: body)
        } catch {
            // A conflict means the state moved under us — refresh so the
            // controls re-render with the CURRENT revision instead of
            // re-offering the stale one forever, then surface the error.
            if let refreshTaskId { await fetchReceipt(taskId: refreshTaskId) }
            await fetchReceipts()
            throw error
        }
        if let refreshTaskId {
            await fetchReceipt(taskId: refreshTaskId)
        }
        await fetchReceipts()
    }

    /// Preload one session's deep view into `preloadedSessions` (snapshot support).
    func preloadSession(client clientName: String, sessionId: String) async {
        if let detail = try? await loadSession(client: clientName, sessionId: sessionId) {
            preloadedSessions["\(clientName)::\(sessionId)"] = detail
        }
    }

    /// Switch the pane range and refetch BOTH the plan lane and the cost cube
    /// so the plan breakdown, the period bars, and the $ view stay on one window.
    /// The range label only flips once both payloads have landed, and only the
    /// newest in-flight switch is allowed to write.
    func setUsageDays(_ days: Int) async {
        guard days != usageDays else { return }
        usageDaysGeneration += 1
        let generation = usageDaysGeneration
        do {
            async let planRequest: V1PlanPayload = client.getAuthed("/v1/plan?days=\(days)")
            async let usageRequest: UsageSummary = client.getLocal("/usage/summary?days=\(days)")
            let (plan, summary) = try await (planRequest, usageRequest)
            guard generation == usageDaysGeneration else { return }
            usageDays = days
            planClients = plan.clients
            usage = summary
            errorText = nil
            let updated = Date()
            usageLastUpdated = updated
            if receiptListError == nil { lastUpdated = updated }
        } catch {
            guard generation == usageDaysGeneration else { return }
            errorText = "usage range fetch failed: \(error.localizedDescription)"
        }
    }
}

/// The POST /v1/disposition acknowledgement: the chain's new state.
struct DispositionResponse: Decodable {
    let ok: Bool?
    let state: String?
    let revision: Int?
}

/// The menu bar → main window selection channel.
@MainActor
@Observable
final class AppSelection {
    var sessionId: String?
    var taskId: String?
    var pane: MainPane = .dashboard
    let workBrowse = WorkBrowseState()
    var workSort: WorkSort {
        get { workBrowse.sort }
        set { workBrowse.sort = newValue }
    }

    /// Dashboard actions replace stale deep links before changing panes. This
    /// keeps a previous Task or session from overriding the control the user
    /// just activated when WorkPane resolves its selection.
    func open(_ destination: DashboardDestination) {
        switch destination {
        case .work:
            taskId = nil
            sessionId = nil
            pane = .work
        case .task(let id):
            taskId = id
            sessionId = nil
            pane = .work
        case .session(let id):
            taskId = nil
            sessionId = id
            pane = .work
        case .limits:
            taskId = nil
            sessionId = nil
            pane = .usage
        }
    }
}

enum DashboardDestination: Equatable {
    case work
    case task(String)
    case session(String)
    case limits
}

enum MainPane: String, CaseIterable, Identifiable {
    case dashboard = "Dashboard"
    case work = "Work"
    case usage = "Usage"
    case sources = "Sources"
    var id: String { rawValue }
}
