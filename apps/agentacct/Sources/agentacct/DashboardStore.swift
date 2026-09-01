import Foundation
import Observation
import SwiftUI

enum DashboardDaemonFeature {
    case attention
    case ingestion

    var upgradeMessage: String {
        switch self {
        case .attention:
            return "Update agentacct, then restart its local service to enable review status."
        case .ingestion:
            return "Update agentacct, then restart its local service to enable source status."
        }
    }
}
/// Named state variants used only by deterministic offscreen review tooling.
/// Keeping the mutation inside DashboardStore preserves its private setters;
/// the live initializer and network lifecycle remain unchanged.
enum SnapshotWorkStoreState {
    case populated
    case listLoading
    case empty
    case listError
    case listErrorWithRetainedData
    case shiftBriefUnavailable
    case oldDaemonUnavailable
    case attentionOverflow
    case receiptLoading
    case receiptError
    case receiptStale
    case attentionReceipt
}

struct SnapshotUsageStoreState {
    /// Keep the selected range and its matching response inseparable in
    /// deterministic renders; a stale summary must never wear a new range.
    let days: Int
    let summary: UsageSummary
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
    private(set) var hasLoadedReceiptTasks = false
    private(set) var receiptTasksTruncated: Bool?
    private(set) var receiptAttention: ReceiptAttentionPayload?
    /// Complete review classification plus a bounded operational queue.
    private(set) var attention: V1AttentionPayload?
    private(set) var attentionError: String?
    private(set) var attentionQueueItems: [ReceiptSummary] = []
    private(set) var attentionPageError: String?
    private(set) var isLoadingMoreAttention = false
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
    private(set) var ingestionError: String?
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
    @ObservationIgnored private var attentionGeneration = 0
    @ObservationIgnored private var receiptListGeneration = 0

    @ObservationIgnored private let client = GlanceClient()

    init() {}

    /// Design-review tooling: populate the same state the daemon endpoints
    /// would, without network access or a developer's local account data.
    init(
        preloaded fixture: DashboardSnapshotFixture,
        workState: SnapshotWorkStoreState = .populated,
        usageState: SnapshotUsageStoreState? = nil
    ) {
        planClients = fixture.plan.clients
        usage = usageState?.summary ?? fixture.usage
        usageDays = usageState?.days ?? 7
        attention = fixture.attention
        attentionQueueItems = fixture.attention.items
        ingestion = fixture.ingestion?.ingestion
        switch workState {
        case .populated:
            hasLoadedReceiptTasks = true
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
            hasLoadedReceiptTasks = true
            receiptTasks = []
            totalReceiptTasks = 0
            receiptTasksTruncated = false
            attention = V1AttentionPayload(
                schema: fixture.attention.schema,
                items: [],
                total: 0,
                counts: V1AttentionCounts(failedCheck: 0, failedStep: 0, blocker: 0),
                revision: fixture.attention.revision,
                offset: fixture.attention.revision == nil ? nil : 0,
                limit: fixture.attention.limit,
                truncated: false
            )
            attentionQueueItems = []
        case .listError:
            receiptTasks = []
            receiptListError = "receipts fetch failed: synthetic review error"
        case .listErrorWithRetainedData:
            hasLoadedReceiptTasks = true
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptTasksTruncated = fixture.tasks.truncated
            receiptAttention = fixture.tasks.attention
            receiptListError = "receipts fetch failed: synthetic review error"
        case .shiftBriefUnavailable:
            hasLoadedReceiptTasks = true
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptTasksTruncated = fixture.tasks.truncated
            receiptAttention = fixture.tasks.attention
            attentionError = "attention fetch failed: synthetic review error"
            ingestionError = "source health fetch failed: synthetic review error"
        case .oldDaemonUnavailable:
            hasLoadedReceiptTasks = true
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            attention = nil
            attentionQueueItems = []
            ingestion = nil
            attentionError = DashboardDaemonFeature.attention.upgradeMessage
            ingestionError = DashboardDaemonFeature.ingestion.upgradeMessage
        case .attentionOverflow:
            hasLoadedReceiptTasks = true
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            let overflow = V1AttentionPayload(
                schema: fixture.attention.schema,
                items: fixture.attention.items,
                total: 7,
                counts: V1AttentionCounts(failedCheck: 4, failedStep: 1, blocker: 2),
                revision: fixture.attention.revision ?? "snapshot-overflow-revision",
                offset: 0,
                limit: 5,
                truncated: true
            )
            attention = overflow
            attentionQueueItems = overflow.items
        case .receiptLoading:
            hasLoadedReceiptTasks = true
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptTasksTruncated = fixture.tasks.truncated
            receiptAttention = fixture.tasks.attention
        case .receiptError:
            hasLoadedReceiptTasks = true
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptTasksTruncated = fixture.tasks.truncated
            receiptAttention = fixture.tasks.attention
            receiptError = "receipt fetch failed: synthetic review error"
            receiptErrorTaskId = fixture.work?.receipt.taskId
        case .receiptStale:
            hasLoadedReceiptTasks = true
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptTasksTruncated = fixture.tasks.truncated
            receiptAttention = fixture.tasks.attention
            receipt = fixture.work?.receipt
            receiptError = "receipt refresh failed: synthetic review error"
            receiptErrorTaskId = fixture.work?.receipt.taskId
        case .attentionReceipt:
            hasLoadedReceiptTasks = true
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
    }

    func refresh() async {
        guard !isRefreshing else { return }
        isRefreshing = true
        defer {
            isRefreshing = false
        }
        let days = usageDays
        let rangeGeneration = usageDaysGeneration
        let receiptRequestGeneration = beginReceiptListRequest()
        let attentionRequestGeneration = beginAttentionRequest()
        isLoadingMoreAttention = false
        // Launch independent lanes together, but publish each error through
        // its own state so a successful range request cannot hide a stale Task
        // list (or vice versa).
        async let tasksRequest: ReceiptTasksPayload = client.getAuthed("/v1/tasks?limit=200")
        async let attentionRequest: V1AttentionPayload = client.getAuthed("/v1/attention?limit=5")
        async let planRequest: V1PlanPayload = client.getAuthed("/v1/plan?days=\(days)")
        async let usageRequest: UsageSummary = client.getLocal("/usage/summary?days=\(days)")
        async let ingestionRequest: V1IngestionPayload = client.getAuthed("/v1/ingestion")

        var tasksSucceeded = false
        do {
            let tasks = try await tasksRequest
            if receiptRequestGeneration == receiptListGeneration {
                publishReceiptList(tasks, requestGeneration: receiptRequestGeneration)
                tasksSucceeded = true
            }
        } catch GlanceClientError.noDiscovery(_) {
            publishReceiptListFailure(
                "daemon not running (no discovery file) — start it with `agentacct start`",
                requestGeneration: receiptRequestGeneration
            )
        } catch {
            if !requestWasCancelled(error, taskIsCancelled: Task.isCancelled) {
                publishReceiptListFailure(
                    "receipts fetch failed: \(error.localizedDescription)",
                    requestGeneration: receiptRequestGeneration
                )
            }
        }
        finishReceiptListRequest(receiptRequestGeneration)

        do {
            let payload = try await attentionRequest
            if !Task.isCancelled {
                publishAttentionHead(payload, requestGeneration: attentionRequestGeneration)
            }
        } catch GlanceClientError.http(404) {
            // A pre-attention daemon cannot support a complete review claim.
            publishAttentionFailure(
                DashboardDaemonFeature.attention.upgradeMessage,
                requestGeneration: attentionRequestGeneration
            )
        } catch GlanceClientError.noDiscovery(_) {
            publishAttentionFailure(
                "daemon not running (no discovery file) — start it with `agentacct start`",
                requestGeneration: attentionRequestGeneration
            )
        } catch {
            if !requestWasCancelled(error, taskIsCancelled: Task.isCancelled) {
                publishAttentionFailure(
                    "attention fetch failed: \(error.localizedDescription)",
                    requestGeneration: attentionRequestGeneration
                )
            }
        }

        do {
            let payload = try await ingestionRequest
            ingestion = payload.ingestion
            ingestionError = nil
        } catch GlanceClientError.http(404) {
            // An older daemon without the route: a named state, not an error toast.
            if !Task.isCancelled {
                ingestionError = DashboardDaemonFeature.ingestion.upgradeMessage
            }
        } catch GlanceClientError.noDiscovery(_) {
            if !Task.isCancelled {
                ingestionError = "daemon not running (no discovery file) — start it with `agentacct start`"
            }
        } catch {
            if !requestWasCancelled(error, taskIsCancelled: Task.isCancelled) {
                ingestionError = "source health fetch failed: \(error.localizedDescription)"
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
        // The window refresh already owns this lane. Starting a second request
        // here would advance the generation; if Work then disappears and its
        // task is cancelled, the still-valid window response could be rejected.
        guard !isRefreshing else { return }
        let requestGeneration = beginReceiptListRequest()
        defer { finishReceiptListRequest(requestGeneration) }
        do {
            let payload: ReceiptTasksPayload = try await client.getAuthed("/v1/tasks?limit=200")
            guard !Task.isCancelled else { return }
            publishReceiptList(payload, requestGeneration: requestGeneration)
        } catch is CancellationError {
            return
        } catch let error as URLError where error.code == .cancelled {
            return
        } catch GlanceClientError.noDiscovery(_) {
            publishReceiptListFailure(
                "daemon not running (no discovery file) — start it with `agentacct start`",
                requestGeneration: requestGeneration
            )
        } catch {
            guard !requestWasCancelled(error, taskIsCancelled: Task.isCancelled) else { return }
            publishReceiptListFailure(
                "receipts fetch failed: \(error.localizedDescription)",
                requestGeneration: requestGeneration
            )
        }
    }

    @discardableResult
    func beginReceiptListRequest() -> Int {
        receiptListGeneration += 1
        isLoadingReceipts = true
        return receiptListGeneration
    }

    private func finishReceiptListRequest(_ requestGeneration: Int) {
        guard requestGeneration == receiptListGeneration else { return }
        isLoadingReceipts = false
    }

    func publishReceiptList(_ payload: ReceiptTasksPayload, requestGeneration: Int) {
        guard requestGeneration == receiptListGeneration else { return }
        receiptTasks = payload.tasks
        totalReceiptTasks = payload.total
        receiptTasksTruncated = payload.truncated
        receiptAttention = payload.attention
        hasLoadedReceiptTasks = true
        receiptListError = nil
        receiptListLastUpdated = Date()
    }

    func publishReceiptListFailure(_ message: String, requestGeneration: Int) {
        guard requestGeneration == receiptListGeneration else { return }
        receiptListError = message
    }

    /// Refresh the complete attention classification independently of the
    /// paginated Receipt list. Used after a human disposition changes whether
    /// a finding or blocker still demands review.
    func fetchAttention() async {
        let requestGeneration = beginAttentionRequest()
        isLoadingMoreAttention = false
        do {
            let payload: V1AttentionPayload = try await client.getAuthed("/v1/attention?limit=5")
            guard !Task.isCancelled else { return }
            publishAttentionHead(payload, requestGeneration: requestGeneration)
        } catch is CancellationError {
            return
        } catch let error as URLError where error.code == .cancelled {
            return
        } catch GlanceClientError.http(404) {
            publishAttentionFailure(
                DashboardDaemonFeature.attention.upgradeMessage,
                requestGeneration: requestGeneration
            )
        } catch GlanceClientError.noDiscovery(_) {
            publishAttentionFailure(
                "daemon not running (no discovery file) — start it with `agentacct start`",
                requestGeneration: requestGeneration
            )
        } catch {
            guard !requestWasCancelled(error, taskIsCancelled: Task.isCancelled) else { return }
            publishAttentionFailure(
                "attention fetch failed: \(error.localizedDescription)",
                requestGeneration: requestGeneration
            )
        }
    }

    @discardableResult
    func beginAttentionRequest() -> Int {
        attentionGeneration += 1
        return attentionGeneration
    }

    func publishAttentionHead(_ payload: V1AttentionPayload, requestGeneration: Int) {
        guard requestGeneration == attentionGeneration else { return }
        guard hasConsistentAttentionHeadEnvelope(payload) else {
            attention = nil
            attentionQueueItems = []
            attentionError = "Review status response was inconsistent. Refresh before acting on it."
            attentionPageError = nil
            return
        }
        attentionQueueItems = attentionItemsAfterHeadRefresh(
            existing: attentionQueueItems,
            previous: attention,
            refreshed: payload
        )
        attention = payload
        attentionError = nil
        attentionPageError = nil
    }

    func publishAttentionFailure(_ message: String, requestGeneration: Int) {
        guard requestGeneration == attentionGeneration else { return }
        attention = nil
        attentionQueueItems = []
        attentionError = message
        attentionPageError = nil
    }

    var hasMoreAttention: Bool {
        guard let attention else { return false }
        return attentionQueueItems.count < attention.total
    }

    var supportsAttentionPaging: Bool {
        guard let attention else { return false }
        return attention.offset != nil && attention.revision != nil
    }

    var canLoadMoreAttention: Bool {
        hasMoreAttention && supportsAttentionPaging
    }

    /// Fetch the next server-ranked page without replacing the Shift Brief's
    /// complete count or leading row. Page compatibility is validated before
    /// publishing so older daemons that ignore `offset` cannot duplicate page
    /// one and make a bounded queue look complete.
    func fetchMoreAttention() async {
        guard attention != nil,
              canLoadMoreAttention,
              !isLoadingMoreAttention
        else {
            return
        }

        isLoadingMoreAttention = true
        let generation = attentionGeneration
        defer {
            if generation == attentionGeneration {
                isLoadingMoreAttention = false
            }
        }
        let offset = attentionQueueItems.count
        do {
            let page: V1AttentionPayload = try await client.getAuthed(
                "/v1/attention?limit=50&offset=\(offset)"
            )
            guard !Task.isCancelled, generation == attentionGeneration else { return }
            publishAttentionPage(page, requestGeneration: generation)
        } catch is CancellationError {
            return
        } catch let error as URLError where error.code == .cancelled {
            return
        } catch GlanceClientError.http(404) {
            guard generation == attentionGeneration else { return }
            attentionPageError = DashboardDaemonFeature.attention.upgradeMessage
        } catch GlanceClientError.noDiscovery(_) {
            guard generation == attentionGeneration else { return }
            attentionPageError = "daemon not running (no discovery file) — start it with `agentacct start`"
        } catch {
            guard generation == attentionGeneration,
                  !requestWasCancelled(error, taskIsCancelled: Task.isCancelled) else { return }
            attentionPageError = "review queue page failed: \(error.localizedDescription)"
        }
    }

    func publishAttentionPage(_ page: V1AttentionPayload, requestGeneration: Int) {
        guard requestGeneration == attentionGeneration, let summary = attention else { return }
        if let summaryRevision = summary.revision,
           let pageRevision = page.revision,
           pageRevision != summaryRevision
        {
            attention = nil
            attentionQueueItems = []
            attentionError = "Review queue changed while loading. Refresh before acting on it."
            attentionPageError = nil
            return
        }
        guard let merged = mergedAttentionItems(
            existing: attentionQueueItems,
            summary: summary,
            page: page
        ) else {
            attentionPageError = page.offset == nil
                ? "Update agentacct, then restart its local service to load the complete review queue."
                : "The review queue changed while loading. Refresh before acting on it."
            return
        }
        attentionQueueItems = merged
        attentionPageError = nil
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
            await fetchAttention()
            throw error
        }
        if let refreshTaskId {
            await fetchReceipt(taskId: refreshTaskId)
        }
        await fetchReceipts()
        await fetchAttention()
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
    /// Shared lifecycle filter so a dashboard review-queue deep link survives
    /// the Work table being mounted and later round-tripped through a Receipt.
    var workGroup: WorkGroup? {
        get { workBrowse.group }
        set { workBrowse.group = newValue }
    }

    /// Dashboard actions replace stale deep links before changing panes. This
    /// keeps a previous Task or session from overriding the control the user
    /// just activated when WorkPane resolves its selection.
    func open(_ destination: DashboardDestination) {
        switch destination {
        case .work:
            taskId = nil
            sessionId = nil
            workGroup = nil
            pane = .work
        case .reviewQueue:
            taskId = nil
            sessionId = nil
            workGroup = .attention
            workSort = .attention
            pane = .work
        case .task(let id):
            taskId = id
            sessionId = nil
            workGroup = nil
            pane = .work
        case .attentionTask(let id):
            taskId = id
            sessionId = nil
            workGroup = .attention
            workSort = .attention
            pane = .work
        case .session(let id):
            taskId = nil
            sessionId = id
            workGroup = nil
            pane = .work
        case .limits:
            taskId = nil
            sessionId = nil
            pane = .usage
        case .sources:
            taskId = nil
            sessionId = nil
            pane = .sources
        }
    }
}

enum DashboardDestination: Equatable {
    case work
    case reviewQueue
    case task(String)
    case attentionTask(String)
    case session(String)
    case limits
    case sources
}

enum MainPane: String, CaseIterable, Identifiable {
    case dashboard = "Dashboard"
    case work = "Work"
    case usage = "Usage"
    case sources = "Sources"
    var id: String { rawValue }
}
