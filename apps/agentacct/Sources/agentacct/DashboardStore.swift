import Foundation
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
    case loading
    case empty
    case listError
    case retainedListError
    case retainedLongListError
    case shiftBriefUnavailable
    case oldDaemonUnavailable
    case attentionOverflow
    case receiptLoading
    case receiptError
}

// Data for the full window: /v1/tasks and /v1/receipt supply task-level work
// evidence, /v1/session supplies each Receipt's expandable session detail, and
// /v1/plan supplies attributed aggregates. The legacy /usage/summary cube
// still feeds cost charts (no /v1 twin yet). Honesty rides the payloads; the
// store never re-derives a number.

@MainActor
final class DashboardStore: ObservableObject {
    @Published private(set) var planClients: [V1PlanClient] = []
    @Published private(set) var usage: UsageSummary?
    @Published private(set) var receiptTasks: [ReceiptSummary] = []
    @Published private(set) var totalReceiptTasks: Int?
    @Published private(set) var hasLoadedReceiptTasks = false
    /// Complete review classification plus a bounded operational queue.
    @Published private(set) var attention: V1AttentionPayload?
    @Published private(set) var attentionError: String?
    @Published private(set) var attentionQueueItems: [ReceiptSummary] = []
    @Published private(set) var attentionPageError: String?
    @Published private(set) var isLoadingMoreAttention = false
    @Published private(set) var receipt: Receipt?
    @Published private(set) var receiptListError: String?
    @Published private(set) var receiptError: String?
    /// Session deep views preloaded by key ("client::session"). Only the offscreen
    /// snapshot path fills this (the live app loads each drill row lazily via a
    /// SwiftUI `.task`, while deterministic rendering cannot wait on network
    /// work); a drill row reads it as a fallback so its steps render in a snapshot.
    @Published private(set) var preloadedSessions: [String: V1SessionDetail] = [:]
    @Published private(set) var errorText: String?
    /// Source/watcher health from /v1/ingestion (the Sources pane).
    @Published private(set) var ingestion: V1IngestionSnapshot?
    @Published private(set) var ingestionError: String?
    @Published private(set) var isRefreshing = false
    @Published private(set) var lastUpdated: Date?
    /// Freshness of the independently published plan + recorded-usage pair.
    /// Receipt-list failures must not make a successful usage refresh look old.
    @Published private(set) var usageLastUpdated: Date?

    /// The usage-pane range (7/30/90 trailing days). Defaults to 7 so the
    /// per-model plan breakdown lines up with the 7d headline out of the box
    /// (a 30-day accumulation reads as >100% of a weekly plan and confuses);
    /// the today/7d headline windows are fixed regardless of this.
    @Published private(set) var usageDays = 7

    /// Monotonic token so rapid range switches can't land out of order and a
    /// failed fetch can't leave the old data labeled with the new range.
    private var usageDaysGeneration = 0
    private var attentionGeneration = 0
    private var receiptListGeneration = 0

    private let client = GlanceClient()

    init() {}

    /// Design-review tooling: populate the same state the daemon endpoints
    /// would, without network access or a developer's local account data.
    init(
        preloaded fixture: DashboardSnapshotFixture,
        workState: SnapshotWorkStoreState = .populated
    ) {
        planClients = fixture.plan.clients
        usage = fixture.usage
        attention = fixture.attention
        attentionQueueItems = fixture.attention.items
        ingestion = fixture.ingestion?.ingestion
        switch workState {
        case .populated:
            hasLoadedReceiptTasks = true
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receipt = fixture.work?.receipt
            for session in fixture.work?.sessions ?? [] {
                let key = "\(session.session.client)::\(session.session.clientSessionId)"
                preloadedSessions[key] = session
            }
        case .loading:
            break
        case .empty:
            hasLoadedReceiptTasks = true
            receiptTasks = []
            totalReceiptTasks = 0
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
        case .listError:
            receiptTasks = []
            receiptListError = "receipts fetch failed: synthetic review error"
        case .retainedListError:
            hasLoadedReceiptTasks = true
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptListError = "receipts fetch failed: synthetic review error"
        case .retainedLongListError:
            hasLoadedReceiptTasks = true
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptListError = "receipts fetch failed: The local service returned an incomplete response while refreshing cached work. Existing items remain visible and may be stale."
        case .shiftBriefUnavailable:
            hasLoadedReceiptTasks = true
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
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
        case .receiptError:
            hasLoadedReceiptTasks = true
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptError = "receipt fetch failed: synthetic review error"
        }
        let updated = fixture.glance.generatedAt.map(Date.init(timeIntervalSince1970:))
        lastUpdated = updated
        usageLastUpdated = updated
    }

    func refresh() async {
        guard !isRefreshing else { return }
        isRefreshing = true
        defer { isRefreshing = false }
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
            publishReceiptListFailure(
                "receipts fetch failed: \(error.localizedDescription)",
                requestGeneration: receiptRequestGeneration
            )
        }

        do {
            let payload = try await attentionRequest
            publishAttentionHead(payload, requestGeneration: attentionRequestGeneration)
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
            publishAttentionFailure(
                "attention fetch failed: \(error.localizedDescription)",
                requestGeneration: attentionRequestGeneration
            )
        }

        do {
            let payload = try await ingestionRequest
            ingestion = payload.ingestion
            ingestionError = nil
        } catch GlanceClientError.http(404) {
            // An older daemon without the route: a named state, not an error toast.
            ingestionError = DashboardDaemonFeature.ingestion.upgradeMessage
        } catch GlanceClientError.noDiscovery(_) {
            ingestionError = "daemon not running (no discovery file) — start it with `agentacct start`"
        } catch {
            ingestionError = "source health fetch failed: \(error.localizedDescription)"
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
            guard rangeGeneration == usageDaysGeneration else { return }
            errorText = "daemon not running (no discovery file) — start it with `agentacct start`"
        } catch {
            guard rangeGeneration == usageDaysGeneration else { return }
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
            publishReceiptListFailure(
                "receipts fetch failed: \(error.localizedDescription)",
                requestGeneration: requestGeneration
            )
        }
    }

    @discardableResult
    func beginReceiptListRequest() -> Int {
        receiptListGeneration += 1
        return receiptListGeneration
    }

    func publishReceiptList(_ payload: ReceiptTasksPayload, requestGeneration: Int) {
        guard requestGeneration == receiptListGeneration else { return }
        receiptTasks = payload.tasks
        totalReceiptTasks = payload.total
        hasLoadedReceiptTasks = true
        receiptListError = nil
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
            guard generation == attentionGeneration else { return }
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
    private var receiptGeneration = 0

    func fetchReceipt(taskId: String) async {
        receiptGeneration += 1
        let generation = receiptGeneration
        if receipt?.taskId != taskId { receipt = nil }
        receiptError = nil
        do {
            let encoded = taskId.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? taskId
            let payload: Receipt = try await client.getAuthed("/v1/receipt?task=\(encoded)")
            guard !Task.isCancelled, generation == receiptGeneration else { return }
            receipt = payload
            receiptError = nil
        } catch is CancellationError {
            return
        } catch let error as URLError where error.code == .cancelled {
            return
        } catch GlanceClientError.http(404) {
            guard !Task.isCancelled, generation == receiptGeneration else { return }
            receiptError = "this Task is not in the store (it may have been recorded elsewhere)"
        } catch {
            guard !Task.isCancelled, generation == receiptGeneration else { return }
            receiptError = "receipt fetch failed: \(error.localizedDescription)"
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
    /// so the plan breakdown, the daily bars, and the $ view stay on one window.
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
final class AppSelection: ObservableObject {
    @Published var sessionId: String?
    @Published var taskId: String?
    @Published var pane: MainPane = .dashboard
    /// The Work surface's shared sort. Lives here (not in the table's @State)
    /// so opening a record — which unmounts the table — never resets it, and
    /// the record-mode rail stays on the same order as the table.
    @Published var workSort: WorkSort = .latest
    /// Shared lifecycle filter so a dashboard review-queue deep link survives
    /// the Work table being mounted and later round-tripped through a Receipt.
    @Published var workGroup: WorkGroup?

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
