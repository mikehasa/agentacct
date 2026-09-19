import AppKit
import Foundation
import Observation
import SwiftUI

struct LatestRequestGeneration {
    private(set) var current = 0

    mutating func begin() -> Int {
        current += 1
        return current
    }

    func accepts(_ generation: Int) -> Bool {
        generation == current
    }
}

func mergedAttentionPages(
    _ current: V1AttentionPayload,
    _ next: V1AttentionPayload
) -> V1AttentionPayload {
    var seen = Set<String>()
    let items = (current.items + next.items).filter { seen.insert($0.taskId).inserted }
    return V1AttentionPayload(
        schema: next.schema,
        items: items,
        total: next.total,
        counts: next.counts,
        snapshot: next.snapshot,
        offset: current.offset,
        limit: items.count,
        truncated: next.truncated
    )
}

func attentionPageCanAppend(
    _ current: V1AttentionPayload,
    _ next: V1AttentionPayload
) -> Bool {
    current.snapshot != nil
        && next.snapshot == current.snapshot
        && next.schema == current.schema
        && next.offset == current.offset + current.items.count
        && next.total == current.total
        && next.counts == current.counts
}

/// What a window refresh does with the review queue (K87).
///
/// The Dashboard's short preview and the Work pane's queue are separate state,
/// so a 60-second tick refreshes the preview and leaves a queue the reviewer
/// has not opened untouched. Once the Work pane HAS loaded one, the refresh
/// re-requests exactly that extent: the queue still shrinks when the store has
/// fewer items, never because the preview's page size replaced it.
enum AttentionRefreshPlan: Equatable {
    case previewOnly
    case reloadQueue(extent: Int)

    init(loadedExtent: Int?) {
        guard let loadedExtent, loadedExtent > 0 else {
            self = .previewOnly
            return
        }
        self = .reloadQueue(extent: loadedExtent)
    }
}

/// How many items the next page of a queue reload asks for: whole pages until
/// the loaded extent is back, then only the remainder (the daemon caps one
/// request at its page size).
func attentionReloadPageLimit(loaded: Int, extent: Int, pageLimit: Int) -> Int {
    min(pageLimit, max(extent - loaded, 1))
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
    private(set) var receiptTasksTruncated: Bool?
    private(set) var receiptAttention: ReceiptAttentionPayload?
    /// The vocabulary's status legend, from `/v1/tasks`.
    private(set) var decisionLegend: DecisionLegendPayload?
    /// The vocabulary's receipt field labels, from `/v1/tasks`.
    private(set) var receiptFieldLabels: ReceiptFieldLabels?
    /// The review queue's words, from `/v1/tasks` (or `/v1/attention`).
    private(set) var receiptQueue: AttentionQueueCopy?
    /// The queue words for the current attention count: the attention
    /// projection's own copy, else the task list's.
    var attentionQueue: AttentionQueueCopy? { attention?.queue ?? receiptQueue }
    /// Complete review classification plus a bounded operational queue.
    private(set) var attention: V1AttentionPayload?
    private(set) var attentionError: String?
    private(set) var isLoadingMoreAttention = false
    /// The Dashboard's short attention preview, kept apart from the queue
    /// above. A window refresh used to publish its 5-item page into
    /// `attention`, so a Work queue the reviewer had paged through shrank back
    /// to five on a 60-second timer — dropping the Blocked item off the end of
    /// their own review list (K87).
    private(set) var attentionPreview: V1AttentionPayload?
    private(set) var attentionPreviewError: String?
    /// The queue page size the reviewer has loaded, nil until the Work pane
    /// asks for one. A refresh re-requests exactly this much: the queue still
    /// shrinks when the store has fewer items, never because of the timer.
    @ObservationIgnored private var attentionLoadedExtent: Int?
    /// The complete review total. BOTH attention pages carry the store-wide
    /// total — only the number of items they return differs — so a tab count
    /// stays true whether the Work pane has loaded the queue or only the
    /// Dashboard's preview has arrived (K87).
    var attentionTotal: Int? { (attention ?? attentionPreview)?.total }
    /// What the Dashboard's attention card reads: the preview when a refresh
    /// has published one, else whatever page the Work pane loaded.
    var dashboardAttention: V1AttentionPayload? {
        attentionPreviewError == nil ? (attentionPreview ?? attention) : nil
    }
    var dashboardAttentionError: String? {
        attentionPreviewError ?? (attentionPreview == nil ? attentionError : nil)
    }
    /// The Dashboard's preview page size, and the queue page the Work pane
    /// requests. The daemon caps `/v1/attention?limit=` at the page size.
    /// How far behind a failed-refresh fixture's freshness stamp sits, so a
    /// review render shows the aging time the live app would show (K02).
    static let fixtureStaleRefreshSeconds: TimeInterval = 240
    static let attentionPreviewLimit = 5
    static let attentionPageLimit = 50
    private(set) var receipt: Receipt?
    /// WHEN each Task's receipt was last fetched successfully, by task id.
    /// A copy still on screen after a failed refresh needs its own absolute
    /// age: the payload's own relative "updated 2m ago" froze at fetch time
    /// and would read as current (K55).
    private(set) var receiptFetchedAt: [String: Date] = [:]
    private(set) var receiptListError: String?
    private(set) var receiptError: String?
    private(set) var receiptErrorTaskId: String?
    private(set) var receiptLoadingTaskId: String?
    /// Session deep views preloaded by key ("client::session"). Only the offscreen
    /// snapshot path fills this (the live app loads each drill row lazily via a
    /// SwiftUI `.task`, while deterministic rendering cannot wait on network
    /// work); a drill row reads it as a fallback so its steps render in a snapshot.
    private(set) var preloadedSessions: [String: V1SessionDetail] = [:]
    private(set) var nativeReviewRevision = 0

    /// Explicit native review input only. This cannot replace live app data.
    func applyNativeReviewSessions(_ details: [V1SessionDetail], receipt: Receipt? = nil) {
        guard SnapshotMode.enabled, SnapshotMode.interactiveFixture else { return }
        preloadedSessions = Dictionary(details.map { ("\($0.session.client)::\($0.session.clientSessionId)", $0) }, uniquingKeysWith: { _, latest in latest })
        if let receipt { self.receipt = receipt }
        nativeReviewRevision += 1
    }
    private(set) var errorText: String?
    /// Source/watcher health from /v1/ingestion (the Sources pane).
    private(set) var ingestion: V1IngestionSnapshot?
    private(set) var ingestionError: String?
    private(set) var ingestionLastUpdated: Date?
    private(set) var isRefreshingIngestion = false

    /// Per-agent connection rows from /v1/connections (the Diagnostics pane).
    private(set) var connections: [V1Connection]?
    private(set) var connectionsError: String?
    private(set) var isRefreshingConnections = false

    private(set) var versionInfo: VersionInfo?
    private(set) var versionError: String?
    private(set) var isApplyingUpdate = false
    private(set) var updateRestarting = false
    private(set) var isRefreshing = false
    private(set) var isLoadingReceipts = false
    private(set) var lastUpdated: Date?

    /// Folder-anchored Work groupings (the Work tab). Membership is re-queried
    /// live on every fetch, so a new session in a folder joins on its own.
    private(set) var worksets: [WorksetCard] = []
    private(set) var worksetsError: String?
    private(set) var isLoadingWorksets = false
    private(set) var worksetsLastUpdated: Date?
    /// Folders the recorder has seen, for the "point at a folder" picker.
    private(set) var worksetCandidates: [WorksetCandidate] = []
    private(set) var worksetCandidatesError: String?
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
    @ObservationIgnored private var attentionGeneration = LatestRequestGeneration()
    @ObservationIgnored private var attentionPreviewGeneration = LatestRequestGeneration()
    @ObservationIgnored private var setupCaptureCursors = SetupCaptureCursorState()

    @ObservationIgnored private let client: GlanceClient
    let savedWork: SavedWorkSnapshot?
    var isOfflineSnapshot: Bool { savedWork != nil }
    var receiptSavedAt: Date? {
        guard let taskID = receipt?.taskId else { return nil }
        return savedWork?.entries["/v1/receipt?task=\(Self.queryValue(taskID))"]?.receivedAt
    }
    /// When the receipt currently on screen was taken: saved (offline) or
    /// fetched (live). Nil when no receipt is loaded or its time is unknown.
    var currentReceiptCopiedAt: Date? {
        guard let taskID = receipt?.taskId else { return nil }
        return receiptSavedAt ?? receiptFetchedAt[taskID]
    }
    func sessionSavedAt(client: String, sessionID: String) -> Date? {
        savedWork?.entries["/v1/session?client=\(Self.queryValue(client))&session_id=\(Self.queryValue(sessionID))"]?.receivedAt
    }
    /// Whether a polled receipt is already the one on screen, byte for byte,
    /// so republishing it would rebuild the record page to show the same facts.
    ///
    /// Every condition here fails OPEN: an absent fingerprint, a task the page
    /// is not showing, or a fingerprint we have not stored all republish. The
    /// costly direction (a needless rebuild) is recoverable; the cheap-looking
    /// one (a real change withheld from the page) is a silent stale receipt,
    /// which in a product about evidence is the worse failure by far.
    nonisolated static func receiptIsAlreadyOnScreen(
        showing: String?, taskId: String,
        incoming: PayloadFingerprint?, stored: PayloadFingerprint?
    ) -> Bool {
        guard showing == taskId else { return false }
        guard let incoming, let stored else { return false }
        return incoming == stored
    }

    static func queryValue(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? value
    }

    init() { client = GlanceClient(); savedWork = nil }

    init(savedWork: SavedWorkSnapshot, taskID: String? = nil) {
        self.savedWork = savedWork
        client = GlanceClient(savedWork: savedWork)
        if let tasks: ReceiptTasksPayload = try? savedWork.value("/v1/tasks?limit=200") {
            publishReceiptTasks(tasks)
        }
        if let taskID { receipt = try? savedWork.value("/v1/receipt?task=\(Self.queryValue(taskID))") }
        receiptListLastUpdated = savedWork.collectionDate
        lastUpdated = savedWork.collectionDate
        for entry in savedWork.entries.values where entry.path.hasPrefix("/v1/session?") {
            if let session = try? JSONDecoder().decode(V1SessionDetail.self, from: entry.data) {
                preloadedSessions["\(session.session.client)::\(session.session.clientSessionId)"] = session
            }
        }
    }

    /// Design-review tooling: populate the same state the daemon endpoints
    /// would, without network access or a developer's local account data.
    init(
        preloaded fixture: DashboardSnapshotFixture,
        workState: SnapshotWorkStoreState = .populated,
        usageState: SnapshotUsageStoreState? = nil,
        ingestionOverride: V1IngestionSnapshot? = nil
    ) {
        client = GlanceClient()
        savedWork = nil
        planClients = fixture.plan.clients
        usage = usageState?.summary ?? fixture.usage
        usageDays = usageState?.days ?? 7
        attention = fixture.attention
        ingestion = ingestionOverride ?? fixture.ingestion?.ingestion
        switch workState {
        case .populated:
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptTasksTruncated = fixture.tasks.truncated
            receiptAttention = fixture.tasks.attention
            decisionLegend = fixture.tasks.decisionLegend
            receiptFieldLabels = fixture.tasks.fieldLabels
            receiptQueue = fixture.tasks.queue
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
            attention = V1AttentionPayload(
                schema: fixture.attention.schema,
                items: [],
                total: 0,
                counts: V1AttentionCounts(failedCheck: 0, failedStep: 0, blocker: 0),
                snapshot: nil,
                offset: 0,
                limit: fixture.attention.limit,
                truncated: false
            )
        case .listError:
            receiptTasks = []
            receiptListError = "receipts fetch failed: synthetic review error"
        case .listErrorWithRetainedData:
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptTasksTruncated = fixture.tasks.truncated
            receiptAttention = fixture.tasks.attention
            decisionLegend = fixture.tasks.decisionLegend
            receiptFieldLabels = fixture.tasks.fieldLabels
            receiptQueue = fixture.tasks.queue
            receiptListError = "receipts fetch failed: synthetic review error"
        case .shiftBriefUnavailable:
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptTasksTruncated = fixture.tasks.truncated
            receiptAttention = fixture.tasks.attention
            decisionLegend = fixture.tasks.decisionLegend
            receiptFieldLabels = fixture.tasks.fieldLabels
            receiptQueue = fixture.tasks.queue
            attentionError = "attention fetch failed: synthetic review error"
            ingestionError = "source health fetch failed: synthetic review error"
        case .receiptLoading:
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptTasksTruncated = fixture.tasks.truncated
            receiptAttention = fixture.tasks.attention
            decisionLegend = fixture.tasks.decisionLegend
            receiptFieldLabels = fixture.tasks.fieldLabels
            receiptQueue = fixture.tasks.queue
        case .receiptError:
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptTasksTruncated = fixture.tasks.truncated
            receiptAttention = fixture.tasks.attention
            decisionLegend = fixture.tasks.decisionLegend
            receiptFieldLabels = fixture.tasks.fieldLabels
            receiptQueue = fixture.tasks.queue
            receiptError = "receipt fetch failed: synthetic review error"
            receiptErrorTaskId = fixture.work?.receipt.taskId
        case .receiptStale:
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptTasksTruncated = fixture.tasks.truncated
            receiptAttention = fixture.tasks.attention
            decisionLegend = fixture.tasks.decisionLegend
            receiptFieldLabels = fixture.tasks.fieldLabels
            receiptQueue = fixture.tasks.queue
            receipt = fixture.work?.receipt
            receiptError = "receipt refresh failed: synthetic review error"
            receiptErrorTaskId = fixture.work?.receipt.taskId
        case .attentionReceipt:
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptTasksTruncated = fixture.tasks.truncated
            receiptAttention = fixture.tasks.attention
            decisionLegend = fixture.tasks.decisionLegend
            receiptFieldLabels = fixture.tasks.fieldLabels
            receiptQueue = fixture.tasks.queue
            receipt = fixture.work?.attentionReceipt
        }
        let updated = fixture.glance.generatedAt.map(Date.init(timeIntervalSince1970:))
        // Only a SUCCESSFUL refresh advances the window's freshness stamp —
        // the same rule the live path follows (`if tasksSucceeded`). Stamping
        // the generation time on every state made an error render read "just
        // now" beside its own failure banner, which is not what the live app
        // does: there the stamp stays at the last success and ages (K02).
        switch workState {
        case .listError, .listErrorWithRetainedData:
            lastUpdated = updated.map { $0.addingTimeInterval(-Self.fixtureStaleRefreshSeconds) }
        default:
            lastUpdated = updated
        }
        // A preloaded receipt was "fetched" when the fixture was generated, so
        // a stale-state render shows a deterministic saved-copy time.
        if let taskID = receipt?.taskId, let updated { receiptFetchedAt[taskID] = updated }
        switch workState {
        case .listLoading, .listError:
            receiptListLastUpdated = nil
        default:
            receiptListLastUpdated = updated
        }
        usageLastUpdated = updated
    }

    func refresh() async {
        guard !isOfflineSnapshot else { return }
        guard !isRefreshing else { return }
        isRefreshing = true
        let receiptListGeneration = beginReceiptListLoad()
        defer {
            isRefreshing = false
        }
        let days = usageDays
        let rangeGeneration = usageDaysGeneration
        let attentionRequestGeneration = attentionPreviewGeneration.begin()
        // Launch independent lanes together, but publish each error through
        // its own state so a successful range request cannot hide a stale Task
        // list (or vice versa).
        async let tasksRequest: ReceiptTasksPayload = client.getAuthed("/v1/tasks?limit=200")
        async let attentionRequest: V1AttentionPayload = client.getAuthed(
            "/v1/attention?limit=\(Self.attentionPreviewLimit)"
        )
        async let planRequest: V1PlanPayload = client.getAuthed("/v1/plan?days=\(days)")
        async let usageRequest: UsageSummary = client.getLocal("/usage/summary?days=\(days)")
        async let ingestionRefresh: Void = refreshIngestion()
        async let connectionsRefresh: Void = refreshConnections()
        async let versionRefresh: Void = refreshVersion()

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
            let payload = try await attentionRequest
            if !Task.isCancelled,
               attentionPreviewGeneration.accepts(attentionRequestGeneration) {
                attentionPreview = payload
                attentionPreviewError = nil
            }
        } catch GlanceClientError.http(404) {
            if attentionPreviewGeneration.accepts(attentionRequestGeneration) {
                // A pre-attention daemon cannot support a complete review claim.
                attentionPreview = nil
                attentionPreviewError = "this daemon predates /v1/attention"
            }
        } catch GlanceClientError.noDiscovery(_) {
            if attentionPreviewGeneration.accepts(attentionRequestGeneration) {
                attentionPreview = nil
                attentionPreviewError = "daemon not running (no discovery file) — start it with `agentacct start`"
            }
        } catch {
            if attentionPreviewGeneration.accepts(attentionRequestGeneration),
               !requestWasCancelled(error, taskIsCancelled: Task.isCancelled) {
                attentionPreview = nil
                attentionPreviewError = "attention fetch failed: \(error.localizedDescription)"
            }
        }

        switch AttentionRefreshPlan(loadedExtent: attentionLoadedExtent) {
        case .previewOnly:
            break
        case .reloadQueue(let extent):
            await reloadAttentionQueue(extent: extent)
        }

        _ = await ingestionRefresh
        _ = await connectionsRefresh
        _ = await versionRefresh

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

    /// Sources retries only its own endpoint (upstream PR #158). A cancelled
    /// window refresh leaves retained source health and its timestamp intact.
    func refreshIngestion() async {
        // Snapshot mode is allowed through (unlike the live-only retry loops the
        // other panes gate off): the docs `--snapshot` render calls refresh()
        // once against the demo daemon, so source health — the dashboard's
        // Evidence-trust signal and the Sources pane — renders populated instead
        // of a perpetual "checking" state. Golden fixture renders never call
        // refresh(), so their pixels are unaffected.
        guard !isOfflineSnapshot, !isRefreshingIngestion else { return }
        isRefreshingIngestion = true
        defer { isRefreshingIngestion = false }
        do {
            let payload: V1IngestionPayload = try await client.getAuthed("/v1/ingestion")
            try Task.checkCancellation()
            ingestion = payload.ingestion
            ingestionError = nil
            ingestionLastUpdated = Date()
        } catch GlanceClientError.http(404) {
            // An older daemon without the route: a named state, not an error toast.
            if !Task.isCancelled {
                ingestionError = "this daemon predates /v1/ingestion"
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
    }

    /// Per-agent connection rows for the Diagnostics pane. Retains the last rows
    /// on a cancelled/failed refresh, like source health.
    func refreshConnections() async {
        // Snapshot mode is allowed through for the same reason as
        // refreshIngestion(): the docs `--snapshot` render calls refresh() once
        // against the demo daemon, so the Diagnostics pane renders its per-agent
        // Connections card instead of the older per-source fallback. Golden
        // fixture renders never call refresh(), so their pixels are unaffected.
        guard !isOfflineSnapshot, !isRefreshingConnections else { return }
        isRefreshingConnections = true
        defer { isRefreshingConnections = false }
        do {
            let payload: V1ConnectionsPayload = try await client.getAuthed("/v1/connections")
            try Task.checkCancellation()
            connections = payload.connections
            connectionsError = nil
        } catch GlanceClientError.http(404) {
            if !Task.isCancelled {
                connectionsError = "this daemon predates /v1/connections"
            }
        } catch GlanceClientError.noDiscovery(_) {
            if !Task.isCancelled {
                connectionsError = "daemon not running (no discovery file) — start it with `agentacct start`"
            }
        } catch {
            if !requestWasCancelled(error, taskIsCancelled: Task.isCancelled) {
                connectionsError = "connections fetch failed: \(error.localizedDescription)"
            }
        }
    }

    /// Recorder version + whether a newer release is published. Retains the last
    /// value on a cancelled/failed refresh, like source health.
    func refreshVersion() async {
        guard !isOfflineSnapshot, !isApplyingUpdate else { return }
        do {
            let payload: VersionInfo = try await client.getAuthed("/v1/version")
            try Task.checkCancellation()
            versionInfo = payload
            versionError = nil
        } catch GlanceClientError.http(404) {
            if !Task.isCancelled {
                versionError = "this daemon predates /v1/version"
            }
        } catch GlanceClientError.noDiscovery(_) {
            if !Task.isCancelled {
                // Expected while a self-update restart is in flight: the daemon
                // drops its discovery file as it respawns on the new binary.
                if !updateRestarting {
                    versionError = "daemon not running (no discovery file) — start it with `agentacct start`"
                }
            }
        } catch {
            if !requestWasCancelled(error, taskIsCancelled: Task.isCancelled) {
                versionError = "version fetch failed: \(error.localizedDescription)"
            }
        }
    }

    /// One-click apply of a published update. The daemon installs the new
    /// version and restarts itself, so a subsequent noDiscovery is expected, not
    /// an error. Never offered for a dev/editable install (the button is hidden).
    func applyUpdate() async throws {
        guard !isOfflineSnapshot else { throw SavedWorkError.readOnly }
        guard !isApplyingUpdate else { return }
        isApplyingUpdate = true
        defer { isApplyingUpdate = false }
        let response: SelfUpdateResponse = try await client.postAuthed("/v1/self-update", body: [:])
        if response.applied == true {
            updateRestarting = true
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

    /// Refresh the complete attention classification independently of the
    /// paginated Receipt list. Used after a human disposition changes whether
    /// a finding or blocker still demands review.
    func fetchAttention() async {
        guard !isOfflineSnapshot else { return }
        let generation = attentionGeneration.begin()
        isLoadingMoreAttention = false
        do {
            let payload: V1AttentionPayload = try await client.getAuthed(
                "/v1/attention?limit=\(Self.attentionPageLimit)&offset=0"
            )
            guard !Task.isCancelled, attentionGeneration.accepts(generation) else { return }
            attention = payload
            attentionLoadedExtent = Self.attentionPageLimit
            attentionError = nil
        } catch GlanceClientError.http(404) {
            guard !Task.isCancelled, attentionGeneration.accepts(generation) else { return }
            attention = nil
            attentionLoadedExtent = nil
            attentionError = "this daemon predates /v1/attention"
        } catch GlanceClientError.noDiscovery(_) {
            guard !Task.isCancelled, attentionGeneration.accepts(generation) else { return }
            attention = nil
            attentionLoadedExtent = nil
            attentionError = "daemon not running (no discovery file) — start it with `agentacct start`"
        } catch {
            guard attentionGeneration.accepts(generation),
                  !requestWasCancelled(error, taskIsCancelled: Task.isCancelled) else { return }
            attention = nil
            attentionLoadedExtent = nil
            attentionError = "attention fetch failed: \(error.localizedDescription)"
        }
    }

    /// Re-request the review queue at the extent the Work pane has loaded,
    /// paging back up to it when the reviewer had asked for more. A background
    /// refresh publishes the fresh queue only when it arrives whole: a
    /// transient failure leaves the loaded queue and names the error, rather
    /// than emptying the list someone is working through (K87).
    private func reloadAttentionQueue(extent: Int) async {
        guard !isOfflineSnapshot, !isLoadingMoreAttention else { return }
        let generation = attentionGeneration.begin()
        var merged: V1AttentionPayload?
        do {
            while true {
                let loaded = merged?.items.count ?? 0
                let pageLimit = attentionReloadPageLimit(
                    loaded: loaded,
                    extent: extent,
                    pageLimit: Self.attentionPageLimit
                )
                let page: V1AttentionPayload = try await client.getAuthed(
                    "/v1/attention?limit=\(pageLimit)&offset=\(loaded)"
                )
                guard !Task.isCancelled, attentionGeneration.accepts(generation) else { return }
                guard page.offset == loaded else { break }
                if let current = merged {
                    // The queue changed between pages: keep the whole pages we
                    // have rather than stitch two classifications together.
                    guard attentionPageCanAppend(current, page) else { break }
                    merged = mergedAttentionPages(current, page)
                } else {
                    merged = page
                }
                guard let current = merged, current.truncated, current.items.count < extent else { break }
            }
        } catch GlanceClientError.http(404) {
            guard !Task.isCancelled, attentionGeneration.accepts(generation) else { return }
            // A daemon without the route cannot support a complete review claim.
            attention = nil
            attentionLoadedExtent = nil
            attentionError = "this daemon predates /v1/attention"
            return
        } catch GlanceClientError.noDiscovery(_) {
            guard !Task.isCancelled, attentionGeneration.accepts(generation) else { return }
            attentionError = "daemon not running (no discovery file) — start it with `agentacct start`"
            return
        } catch {
            guard attentionGeneration.accepts(generation),
                  !requestWasCancelled(error, taskIsCancelled: Task.isCancelled) else { return }
            attentionError = "attention fetch failed: \(error.localizedDescription)"
            return
        }
        guard attentionGeneration.accepts(generation), let merged else { return }
        attention = merged
        attentionLoadedExtent = max(extent, merged.items.count)
        attentionError = nil
    }

    func fetchMoreAttention() async {
        guard let current = attention, current.truncated, !isLoadingMoreAttention else { return }
        let generation = attentionGeneration.begin()
        isLoadingMoreAttention = true
        defer {
            if attentionGeneration.accepts(generation) { isLoadingMoreAttention = false }
        }
        let offset = current.offset + current.items.count
        do {
            let page: V1AttentionPayload = try await client.getAuthed(
                "/v1/attention?limit=50&offset=\(offset)"
            )
            guard !Task.isCancelled, attentionGeneration.accepts(generation) else { return }
            guard page.offset == offset else {
                attentionError = "this daemon predates paged /v1/attention"
                return
            }
            guard attentionPageCanAppend(current, page) else {
                // The queue changed between page requests. Restart instead of
                // stitching two incompatible classifications together.
                await fetchAttention()
                return
            }
            let merged = mergedAttentionPages(current, page)
            attention = merged
            attentionLoadedExtent = max(attentionLoadedExtent ?? 0, merged.items.count)
            attentionError = nil
        } catch GlanceClientError.http(404) {
            guard !Task.isCancelled, attentionGeneration.accepts(generation) else { return }
            attentionError = "this daemon predates paged /v1/attention"
        } catch GlanceClientError.noDiscovery(_) {
            guard !Task.isCancelled, attentionGeneration.accepts(generation) else { return }
            attentionError = "daemon not running (no discovery file) — start it with `agentacct start`"
        } catch {
            guard attentionGeneration.accepts(generation),
                  !requestWasCancelled(error, taskIsCancelled: Task.isCancelled) else { return }
            attentionError = "attention page fetch failed: \(error.localizedDescription)"
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
    /// The bytes behind the receipt currently published for each task. A
    /// repeating poll compares against this so an unchanged answer never
    /// re-publishes — see `fetchReceipt`.
    @ObservationIgnored private var receiptPayloadFingerprints: [String: PayloadFingerprint] = [:]

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
        decisionLegend = payload.decisionLegend
        receiptFieldLabels = payload.fieldLabels
        receiptQueue = payload.queue
        receiptListError = nil
        receiptListLastUpdated = savedWork?.collectionDate ?? Date()
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
            if generation == receiptGeneration, receiptLoadingTaskId != nil {
                receiptLoadingTaskId = nil
            }
        }
        do {
            let encoded = Self.queryValue(taskId)
            let (payload, fingerprint): (Receipt, PayloadFingerprint?) =
                try await client.getAuthedFingerprinted("/v1/receipt?task=\(encoded)")
            guard !Task.isCancelled, generation == receiptGeneration else { return }
            // A refresh that finds nothing new must cost nothing to render.
            // The record page is the most expensive surface in the app, and
            // this route is polled every three seconds while one is open, so
            // re-publishing a byte-identical receipt would rebuild the whole
            // page twenty times a minute to show the same facts. Publishing is
            // skipped only when the raw bytes match what is already on screen —
            // the page keeps showing exactly what the daemon just returned.
            let unchanged = Self.receiptIsAlreadyOnScreen(
                showing: receipt?.taskId, taskId: taskId,
                incoming: fingerprint, stored: receiptPayloadFingerprints[taskId]
            )
            if !unchanged {
                receipt = payload
                receiptPayloadFingerprints[taskId] = fingerprint
            }
            receiptFetchedAt[taskId] = SnapshotMode.currentDate
            // Clearing already-clear error state would invalidate every reader
            // of it for no change, which on this route means the record page.
            if receiptError != nil { receiptError = nil }
            if receiptErrorTaskId != nil { receiptErrorTaskId = nil }
            if receiptLoadingTaskId != nil { receiptLoadingTaskId = nil }
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

    /// Fetch the canonical task timeline; only a completely assembled snapshot
    /// is persisted for offline use. Live cursors are never saved as history.
    func loadTimeline(taskID: String, previous: TaskTimelinePage?) async throws -> TaskTimelinePage {
        let path = "/v1/task-timeline?task=\(Self.queryValue(taskID))"
        if isOfflineSnapshot { return try await client.getAuthed(path) }
        let started = Date()
        let page = try await TaskTimelineLoader.load(taskID: taskID, previous: previous) { cursor in
            try await self.client.getAuthed(path + "&limit=500" + (cursor.map { "&cursor=\(Self.queryValue($0))" } ?? ""))
        }
        try Task.checkCancellation()
        // Re-encoding an unchanged page would spend main-thread JSON work, and
        // a whole-snapshot rewrite on disk, to save a copy the cache already
        // holds. The poll asks every three seconds; only a page that actually
        // moved is worth writing down.
        if page != previous, let store = try? GlanceClient.storeDir(),
           let data = try? JSONEncoder().encode(page) {
            await SavedWorkCache.shared.record(path: path, data: data, store: store, requestStartedAt: started)
        }
        return page
    }

    /// Load one session for a Receipt drill row. Each row owns its result, so
    /// several expanded sessions can remain visible at the same time.
    func loadSession(client clientName: String, sessionId: String) async throws -> V1SessionDetail {
        let encodedClient = Self.queryValue(clientName)
        let encodedSession = Self.queryValue(sessionId)
        return try await client.getAuthed(
            "/v1/session?client=\(encodedClient)&session_id=\(encodedSession)"
        )
    }

    /// Equatable exact relations let the window enrich a capture link when a
    /// later tasks response or same-task receipt adds the matching session.
    var setupCaptureTaskAssociations: [SetupCaptureTaskAssociation] {
        SetupCaptureTaskResolver.associations(tasks: receiptTasks, receipts: receipt.map { [$0] } ?? [])
    }

    func taskID(for capture: SetupCaptureConfirmation) -> String? {
        SetupCaptureTaskResolver.taskID(for: capture, associations: setupCaptureTaskAssociations)
    }

    /// Bounded fresh-capture lookup during first setup. Never treats a recent
    /// session, successful request, or imported token count as capture proof.
    func findSetupCapture(client target: SetupClient, after boundary: Date) async throws -> SetupCaptureConfirmation? {
        guard !isOfflineSnapshot, !SnapshotMode.enabled else { return nil }
        let store = try GlanceClient.storeDir().standardizedFileURL.resolvingSymlinksInPath()
        let ticket = setupCaptureCursors.begin(store: store, client: target, boundary: boundary)
        func verifyStore() throws {
            guard try GlanceClient.storeDir().standardizedFileURL.resolvingSymlinksInPath() == store else {
                throw SetupCaptureLookupError.storeChanged
            }
        }
        let result = try await SetupCaptureLookup.scan(client: target, after: boundary, cursor: ticket.cursor,
            loadPage: { limit, offset in
                try verifyStore()
                let page: V1SessionsPayload = try await self.client.getAuthed(
                    "/v1/sessions?client=\(Self.queryValue(target.rawValue))&roots_only=false&limit=\(limit)&offset=\(offset)"
                )
                try verifyStore()
                return page
            },
            loadDetail: { sessionID in
                try verifyStore()
                let detail = try await self.loadSession(client: target.rawValue, sessionId: sessionID)
                try verifyStore()
                return detail
            })
        try Task.checkCancellation()
        try verifyStore()
        setupCaptureCursors.finish(ticket, nextCursor: result.nextCursor)
        guard let confirmation = result.capture else { return nil }
        return taskID(for: confirmation).map { confirmation.associatingTask($0) } ?? confirmation
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
        guard !isOfflineSnapshot else { throw SavedWorkError.readOnly }
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

    // MARK: - Worksets (folder-anchored Work groupings)

    func fetchWorksets() async {
        guard !isOfflineSnapshot else { return }
        isLoadingWorksets = true
        defer { isLoadingWorksets = false }
        do {
            let payload: WorksetsPayload = try await client.getAuthed("/v1/worksets")
            try Task.checkCancellation()
            worksets = payload.worksets
            worksetsError = nil
            worksetsLastUpdated = SnapshotMode.enabled ? nil : Date()
        } catch GlanceClientError.noDiscovery(_) {
            if !Task.isCancelled {
                worksetsError = "daemon not running (no discovery file) — start it with `agentacct start`"
            }
        } catch GlanceClientError.http(404) {
            worksets = []
            worksetsError = nil
        } catch {
            // A cancelled fetch (pane switch / view teardown) is benign and must
            // never surface as a failure — every sibling fetch in this file guards
            // this the same way. Retain the last rows instead of showing "cancelled".
            if !requestWasCancelled(error, taskIsCancelled: Task.isCancelled) {
                worksetsError = "work groups fetch failed: \(error.localizedDescription)"
            }
        }
    }

    func fetchWorksetCandidates() async {
        guard !isOfflineSnapshot else { return }
        do {
            let payload: WorksetCandidatesPayload = try await client.getAuthed("/v1/workset-candidates")
            try Task.checkCancellation()
            worksetCandidates = payload.candidates
            worksetCandidatesError = nil
        } catch GlanceClientError.noDiscovery(_) {
            if !Task.isCancelled { worksetCandidatesError = "daemon not running" }
        } catch GlanceClientError.http(404) {
            // A daemon predating /v1/workset-candidates: no candidates is a named
            // empty state, not an error toast (mirrors fetchWorksets' 404 branch).
            worksetCandidates = []
            worksetCandidatesError = nil
        } catch {
            if !requestWasCancelled(error, taskIsCancelled: Task.isCancelled) {
                worksetCandidatesError = "folders fetch failed: \(error.localizedDescription)"
            }
        }
    }

    /// The caller supplies a STABLE `worksetId` (minted once per create intent)
    /// so a retry after a lost response replays idempotently on the server —
    /// the operation is keyed by this id, never a fresh one per call — instead
    /// of forking a second grouping for the same folder.
    @discardableResult
    func createWorkset(name: String, directory: String, worksetId: String) async throws -> WorksetWriteResponse {
        guard !isOfflineSnapshot else { throw SavedWorkError.readOnly }
        let body: [String: Any] = [
            "action": "create",
            "workset_id": worksetId,
            "name": name,
            "directory": directory,
            "expected_revision": 0,
        ]
        let response: WorksetWriteResponse = try await client.postAuthed("/v1/worksets", body: body)
        await fetchWorksets()
        return response
    }

    /// A fresh workset id for one create intent, reused across retries by the UI.
    static func newWorksetId() -> String { "ws_" + UUID().uuidString }

    func renameWorkset(id: String, name: String, expectedRevision: Int) async throws {
        guard !isOfflineSnapshot else { throw SavedWorkError.readOnly }
        do {
            let _: WorksetWriteResponse = try await client.postAuthed("/v1/worksets", body: [
                "action": "rename", "workset_id": id, "name": name, "expected_revision": expectedRevision,
            ])
        } catch {
            // A 409 means it moved under us — re-read so the next attempt uses
            // the current revision instead of re-offering the stale one.
            await fetchWorksets()
            throw error
        }
        await fetchWorksets()
    }

    func deleteWorkset(id: String, expectedRevision: Int) async throws {
        guard !isOfflineSnapshot else { throw SavedWorkError.readOnly }
        do {
            let _: WorksetWriteResponse = try await client.postAuthed("/v1/worksets", body: [
                "action": "delete", "workset_id": id, "expected_revision": expectedRevision,
            ])
        } catch {
            await fetchWorksets()
            throw error
        }
        await fetchWorksets()
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
        guard !isOfflineSnapshot else { return }
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

/// Why the record page is on screen. Navigating to a record opens it at its
/// verdict with a clean inspector; coming BACK from somewhere else (Setup)
/// restores the reading position the user left (K104).
enum WorkEntryReason {
    case navigate
    case restore
}

/// The menu bar → main window selection channel.
@MainActor
@Observable
final class AppSelection {
    var sessionId: String?
    var taskId: String? {
        didSet {
            // Landing on a different record is navigation. Only an explicit
            // return (`prepareWorkReturnFocus`, which keeps the same task)
            // asks for the previous reading position back.
            if taskId != oldValue { workEntry = .navigate }
        }
    }
    var pane: MainPane = .dashboard
    let workBrowse = WorkBrowseState()
    var workReturnFocus = WorkTimelineFocusRestoration()
    /// How the record now on screen was reached. Selecting a row or following
    /// a dashboard link is navigation; only an explicit return restores.
    private(set) var workEntry: WorkEntryReason = .navigate

    func prepareWorkReturnFocus() {
        guard pane == .work, let taskId else { return }
        workEntry = .restore
        workReturnFocus.prepare(taskID: taskId)
    }

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
        let previousGroup = workGroup
        defer {
            // A deep link that also changes the lifecycle filter shows its new
            // state ("Status: Attention"), but a screen-reader user never sees
            // that control move, so the change is announced (K104).
            if let group = workGroup, group != previousGroup, pane == .work {
                Self.announce("Showing \(group.rawValue)")
            }
        }
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

extension AppSelection {
    /// The app's one VoiceOver announcement call: a state change a sighted
    /// reader can see but a screen-reader user would otherwise miss.
    static func announce(_ message: String) {
        // `NSApp` is an implicitly-unwrapped global that stays nil until an
        // NSApplication exists, so it must be BOUND, not dereferenced: a unit
        // test that exercises a navigation action without having created an
        // application otherwise crashed here rather than simply not speaking.
        guard !SnapshotMode.enabled,
              let app = NSApp,
              let window = app.keyWindow ?? app.mainWindow
        else { return }
        NSAccessibility.post(
            element: window,
            notification: .announcementRequested,
            userInfo: [
                .announcement: message,
                .priority: NSAccessibilityPriorityLevel.high.rawValue,
            ]
        )
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

    /// The section this destination lands in, named exactly as its tab and
    /// its menu command name it. A hint that says where a control leads uses
    /// this, so no surface invents a second name for the same place.
    var paneName: String {
        switch self {
        case .work, .reviewQueue, .task, .attentionTask, .session: return MainPane.work.rawValue
        case .limits: return MainPane.usage.rawValue
        case .sources: return MainPane.sources.rawValue
        }
    }
}

enum MainPane: String, CaseIterable, Identifiable {
    case dashboard = "Dashboard"
    // Folder-anchored Work groupings across Claude Code and Codex. The internal
    // case is `worksets`; its user-facing tab is "Work". The `work` case below
    // (the receipts collection) keeps its name for its many call sites but now
    // shows as "Sessions" — the granular runs a Work groups the higher level.
    case worksets = "Work"
    case work = "Sessions"
    case usage = "Usage"
    // Internal case stays `.sources`; it now shows as "Diagnostics" because the
    // panel is where you check the whole service's health and what went wrong.
    case sources = "Diagnostics"
    var id: String { rawValue }
}
