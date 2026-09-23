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
        truncated: next.truncated,
        projection: next.projection
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

/// Named state variants used only by deterministic offscreen review tooling.
/// Keeping the mutation inside DashboardStore preserves its private setters;
/// the live initializer and network lifecycle remain unchanged.
enum SnapshotWorkStoreState {
    case populated
    case projectionPending
    case projectionUpdating
    case listLoading
    case empty
    case listError
    case listErrorWithRetainedData
    case shiftBriefUnavailable
    case receiptLoading
    case receiptError
    case receiptStale
    case attentionReceipt
    case attentionNextStepFirst
    case attentionClear
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
    /// Complete review classification plus a bounded operational queue.
    private(set) var attention: V1AttentionPayload?
    private(set) var attentionError: String?
    private(set) var isLoadingMoreAttention = false
    private(set) var receipt: Receipt?
    private(set) var projectionSafetyRevision = 0
    private(set) var receiptListProjection: WorkProjectionMetadata?
    private(set) var attentionProjection: WorkProjectionMetadata?
    private(set) var receiptProjection: WorkProjectionMetadata?
    var projectedCollectionsNeedRefresh: Bool {
        receiptListProjection?.needsRefresh == true || attentionProjection?.needsRefresh == true
    }
    var canMutateReceipts: Bool {
        !isOfflineSnapshot && [receiptListProjection, attentionProjection, receiptProjection]
            .allSatisfy { $0?.isCurrent != false }
    }
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
    @ObservationIgnored private var setupCaptureCursors = SetupCaptureCursorState()

    @ObservationIgnored private let client: GlanceClient
    let savedWork: SavedWorkSnapshot?
    var isOfflineSnapshot: Bool { savedWork != nil }
    var receiptSavedAt: Date? {
        guard let taskID = receipt?.taskId else { return nil }
        return savedWork?.entries["/v1/receipt?task=\(Self.queryValue(taskID))"]?.evidenceDate
    }
    func sessionSavedAt(client: String, sessionID: String) -> Date? {
        savedWork?.entries["/v1/session?client=\(Self.queryValue(client))&session_id=\(Self.queryValue(sessionID))"]?.evidenceDate
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
        attentionProjection = fixture.attention.projection
        receiptListProjection = fixture.tasks.projection
        receiptProjection = fixture.work?.receipt.projection
        ingestion = ingestionOverride ?? fixture.ingestion?.ingestion
        switch workState {
        case .populated, .projectionUpdating:
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptTasksTruncated = fixture.tasks.truncated
            receiptAttention = fixture.tasks.attention
            receipt = fixture.work?.receipt
            for session in fixture.work?.sessions ?? [] {
                let key = "\(session.session.client)::\(session.session.clientSessionId)"
                preloadedSessions[key] = session
            }
        case .projectionPending:
            receiptListProjection = .pending
            receiptProjection = .pending
            attentionProjection = .pending
            receiptTasks = []
            attention = nil
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
            receiptListError = "receipts fetch failed: synthetic review error"
        case .shiftBriefUnavailable:
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptTasksTruncated = fixture.tasks.truncated
            receiptAttention = fixture.tasks.attention
            attentionError = "attention fetch failed: synthetic review error"
            ingestionError = "source health fetch failed: synthetic review error"
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
        case .attentionClear:
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptTasksTruncated = fixture.tasks.truncated
            receiptAttention = fixture.tasks.attention
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
        case .attentionNextStepFirst:
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptTasksTruncated = fixture.tasks.truncated
            receiptAttention = fixture.tasks.attention
            attention = V1AttentionPayload(
                schema: fixture.attention.schema,
                items: Array(fixture.attention.items.reversed()),
                total: fixture.attention.total,
                counts: fixture.attention.counts,
                snapshot: fixture.attention.snapshot,
                offset: fixture.attention.offset,
                limit: fixture.attention.limit,
                truncated: fixture.attention.truncated
            )
        case .attentionReceipt:
            receiptTasks = fixture.tasks.tasks
            totalReceiptTasks = fixture.tasks.total
            receiptTasksTruncated = fixture.tasks.truncated
            receiptAttention = fixture.tasks.attention
            receipt = fixture.work?.attentionReceipt
        }
        if workState == .projectionUpdating {
            let metadata = WorkProjectionMetadata(state: "updating", builtAt: fixture.glance.generatedAt,
                                                  generation: "review-generation", error: nil)
            receiptListProjection = metadata
            receiptProjection = metadata
            attentionProjection = metadata
        }
        let updated = fixture.glance.generatedAt.map(Date.init(timeIntervalSince1970:))
        lastUpdated = receiptListProjection == nil ? updated : receiptListProjection?.builtDate
        switch workState {
        case .listLoading, .listError, .projectionPending:
            receiptListLastUpdated = nil
        default:
            receiptListLastUpdated = lastUpdated
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
        let attentionRequestGeneration = attentionGeneration.begin()
        isLoadingMoreAttention = false
        // Launch independent lanes together, but publish each error through
        // its own state so a successful range request cannot hide a stale Task
        // list (or vice versa).
        async let tasksRequest: ReceiptTasksPayload = readAuthed("/v1/tasks?limit=200")
        async let attentionRequest: V1AttentionPayload = readAuthed("/v1/attention?limit=5")
        async let planRequest: V1PlanPayload = readAuthed("/v1/plan?days=\(days)")
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
        } catch let pending as WorkProjectionPending {
            if !Task.isCancelled, receiptListGeneration == self.receiptListGeneration {
                receiptListProjection = pending.projection.retainingBuild(from: receiptListProjection)
                receiptListError = nil
            }
        } catch GlanceClientError.noDiscovery(_) {
            if !Task.isCancelled,
               receiptListGeneration == self.receiptListGeneration {
                recordReceiptListFailure("daemon not running (no discovery file) — start it with `agentacct start`")
            }
        } catch {
            if receiptListGeneration == self.receiptListGeneration,
               !requestWasCancelled(error, taskIsCancelled: Task.isCancelled) {
                recordReceiptListFailure("receipts fetch failed: \(error.localizedDescription)")
            }
        }
        endReceiptListLoad(generation: receiptListGeneration)

        do {
            let payload = try await attentionRequest
            if !Task.isCancelled,
               attentionGeneration.accepts(attentionRequestGeneration) {
                attention = payload
                attentionProjection = payload.projection
                attentionError = nil
            }
        } catch let pending as WorkProjectionPending {
            if !Task.isCancelled, attentionGeneration.accepts(attentionRequestGeneration) {
                attentionProjection = pending.projection.retainingBuild(from: attentionProjection)
                attentionError = nil
            }
        } catch GlanceClientError.http(404) {
            if attentionGeneration.accepts(attentionRequestGeneration) {
                // A pre-attention daemon cannot support a complete review claim.
                if attentionProjection == nil { attention = nil }
                recordAttentionFailure("this daemon predates /v1/attention")
            }
        } catch GlanceClientError.noDiscovery(_) {
            if attentionGeneration.accepts(attentionRequestGeneration) {
                if attentionProjection == nil { attention = nil }
                recordAttentionFailure("daemon not running (no discovery file) — start it with `agentacct start`")
            }
        } catch {
            if attentionGeneration.accepts(attentionRequestGeneration),
               !requestWasCancelled(error, taskIsCancelled: Task.isCancelled) {
                if attentionProjection == nil { attention = nil }
                recordAttentionFailure("attention fetch failed: \(error.localizedDescription)")
            }
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
            if tasksSucceeded { lastUpdated = receiptListLastUpdated }
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
            let payload: V1IngestionPayload = try await readAuthed("/v1/ingestion")
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
            let payload: V1ConnectionsPayload = try await readAuthed("/v1/connections")
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
            let payload: VersionInfo = try await readAuthed("/v1/version")
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

    /// A privacy-invalidated generation must disappear from every visible lane,
    /// including detail views and late responses started before invalidation.
    private func readAuthed<T: Decodable>(_ path: String) async throws -> T {
        let revision = projectionSafetyRevision
        do {
            let result: T = try await client.getAuthed(path)
            if GlanceClient.usesSnapshotReadMode(path), revision != projectionSafetyRevision {
                throw WorkProjectionPending(projection: .pending)
            }
            return result
        } catch let pending as WorkProjectionPending {
            if !isOfflineSnapshot, pending.projection.available == false {
                invalidateProjectedWork(pending.projection)
            }
            throw pending
        }
    }

    func invalidateProjectedWork(_ projection: WorkProjectionMetadata) {
        guard !isOfflineSnapshot else { return }
        projectionSafetyRevision += 1
        receiptTasks = []
        totalReceiptTasks = nil
        receiptTasksTruncated = nil
        receiptAttention = nil
        attention = nil
        receipt = nil
        preloadedSessions = [:]
        receiptListLastUpdated = nil
        lastUpdated = nil
        receiptListError = nil
        receiptError = nil
        receiptErrorTaskId = nil
        attentionError = nil
        receiptListProjection = projection
        receiptProjection = projection
        attentionProjection = projection
        WorkTimelineMemory.cache = WorkTimelineMemoryCache()
    }

    /// The Task list for the Receipts pane (one compact Receipt summary each).
    func fetchReceipts() async {
        let generation = beginReceiptListLoad()
        defer { endReceiptListLoad(generation: generation) }
        do {
            let payload: ReceiptTasksPayload = try await readAuthed("/v1/tasks?limit=200")
            guard generation == receiptListGeneration else { return }
            publishReceiptTasks(payload)
        } catch let pending as WorkProjectionPending {
            if !Task.isCancelled, generation == receiptListGeneration {
                receiptListProjection = pending.projection.retainingBuild(from: receiptListProjection)
                receiptListError = nil
            }
        } catch GlanceClientError.noDiscovery(_) {
            guard !Task.isCancelled,
                  generation == receiptListGeneration else { return }
            recordReceiptListFailure("daemon not running (no discovery file) — start it with `agentacct start`")
        } catch {
            guard generation == receiptListGeneration,
                  !requestWasCancelled(error, taskIsCancelled: Task.isCancelled) else { return }
            recordReceiptListFailure("receipts fetch failed: \(error.localizedDescription)")
        }
    }

    /// A failed read cannot certify retained evidence as current. Legacy
    /// daemons have no projection metadata and keep their existing behavior.
    func recordReceiptListFailure(_ message: String) {
        receiptListError = message
        receiptListProjection = receiptListProjection.map { .failed(GlanceClientError.transport(message), retaining: $0) }
    }

    func recordAttentionFailure(_ message: String) {
        attentionError = message
        attentionProjection = attentionProjection.map { .failed(GlanceClientError.transport(message), retaining: $0) }
    }

    func recordReceiptFailure(_ message: String) {
        receiptError = message
        receiptProjection = receiptProjection.map { .failed(GlanceClientError.transport(message), retaining: $0) }
    }

    func refreshProjectedCollections() async {
        guard !isOfflineSnapshot else { return }
        if receiptListProjection?.needsRefresh == true, !isLoadingReceipts { await fetchReceipts() }
        if attentionProjection?.needsRefresh == true { await fetchAttention() }
    }

    /// Refresh the complete attention classification independently of the
    /// paginated Receipt list. Used after a human disposition changes whether
    /// a finding or blocker still demands review.
    func fetchAttention() async {
        guard !isOfflineSnapshot else { return }
        let generation = attentionGeneration.begin()
        isLoadingMoreAttention = false
        do {
            let payload: V1AttentionPayload = try await readAuthed("/v1/attention?limit=50&offset=0")
            guard !Task.isCancelled, attentionGeneration.accepts(generation) else { return }
            attention = payload
            attentionProjection = payload.projection
            attentionError = nil
        } catch let pending as WorkProjectionPending {
            if !Task.isCancelled, attentionGeneration.accepts(generation) {
                attentionProjection = pending.projection.retainingBuild(from: attentionProjection)
                attentionError = nil
            }
        } catch GlanceClientError.http(404) {
            guard !Task.isCancelled, attentionGeneration.accepts(generation) else { return }
            if attentionProjection == nil { attention = nil }
            recordAttentionFailure("this daemon predates /v1/attention")
        } catch GlanceClientError.noDiscovery(_) {
            guard !Task.isCancelled, attentionGeneration.accepts(generation) else { return }
            if attentionProjection == nil { attention = nil }
            recordAttentionFailure("daemon not running (no discovery file) — start it with `agentacct start`")
        } catch {
            guard attentionGeneration.accepts(generation),
                  !requestWasCancelled(error, taskIsCancelled: Task.isCancelled) else { return }
            if attentionProjection == nil { attention = nil }
            recordAttentionFailure("attention fetch failed: \(error.localizedDescription)")
        }
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
            let page: V1AttentionPayload = try await readAuthed(
                "/v1/attention?limit=50&offset=\(offset)"
            )
            guard !Task.isCancelled, attentionGeneration.accepts(generation) else { return }
            guard page.offset == offset else {
                recordAttentionFailure("this daemon predates paged /v1/attention")
                return
            }
            guard attentionPageCanAppend(current, page) else {
                // The queue changed between page requests. Restart instead of
                // stitching two incompatible classifications together.
                await fetchAttention()
                return
            }
            attentionProjection = page.projection
            attention = mergedAttentionPages(current, page)
            attentionError = nil
        } catch let pending as WorkProjectionPending {
            if !Task.isCancelled, attentionGeneration.accepts(generation) {
                attentionProjection = pending.projection.retainingBuild(from: attentionProjection)
                attentionError = nil
            }
        } catch GlanceClientError.http(404) {
            guard !Task.isCancelled, attentionGeneration.accepts(generation) else { return }
            recordAttentionFailure("this daemon predates paged /v1/attention")
        } catch GlanceClientError.noDiscovery(_) {
            guard !Task.isCancelled, attentionGeneration.accepts(generation) else { return }
            recordAttentionFailure("daemon not running (no discovery file) — start it with `agentacct start`")
        } catch {
            guard attentionGeneration.accepts(generation),
                  !requestWasCancelled(error, taskIsCancelled: Task.isCancelled) else { return }
            recordAttentionFailure("attention page fetch failed: \(error.localizedDescription)")
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
        receiptListProjection = payload.projection
        receiptTasks = payload.tasks
        totalReceiptTasks = payload.total
        receiptTasksTruncated = payload.truncated
        receiptAttention = payload.attention
        receiptListError = nil
        receiptListLastUpdated = payload.projection == nil
            ? (savedWork?.collectionDate ?? Date()) : payload.projection?.builtDate
    }

    func fetchReceipt(taskId: String) async {
        receiptGeneration += 1
        let generation = receiptGeneration
        if receipt?.taskId != taskId {
            receipt = nil
            receiptProjection = nil
            receiptError = nil
            receiptErrorTaskId = nil
        }
        receiptLoadingTaskId = taskId
        defer {
            if generation == receiptGeneration { receiptLoadingTaskId = nil }
        }
        do {
            let encoded = Self.queryValue(taskId)
            let payload: Receipt = try await readAuthed("/v1/receipt?task=\(encoded)")
            guard !Task.isCancelled, generation == receiptGeneration else { return }
            receipt = payload
            receiptProjection = payload.projection
            receiptError = nil
            receiptErrorTaskId = nil
            receiptLoadingTaskId = nil
        } catch let pending as WorkProjectionPending {
            if !Task.isCancelled, generation == receiptGeneration {
                receiptProjection = pending.projection.retainingBuild(from: receiptProjection)
                receiptError = nil
            }
        } catch is CancellationError {
            if generation == receiptGeneration { receiptLoadingTaskId = nil }
            return
        } catch let error as URLError where error.code == .cancelled {
            if generation == receiptGeneration { receiptLoadingTaskId = nil }
            return
        } catch GlanceClientError.http(404) {
            guard !Task.isCancelled, generation == receiptGeneration else { return }
            recordReceiptFailure("this Task is not in the store (it may have been recorded elsewhere)")
            receiptErrorTaskId = taskId
            receiptLoadingTaskId = nil
        } catch {
            guard !Task.isCancelled, generation == receiptGeneration else { return }
            recordReceiptFailure("receipt fetch failed: \(error.localizedDescription)")
            receiptErrorTaskId = taskId
            receiptLoadingTaskId = nil
        }
    }

    /// Fetch the canonical task timeline; only a completely assembled snapshot
    /// is persisted for offline use. Live cursors are never saved as history.
    func loadTimeline(taskID: String, previous: TaskTimelinePage?) async throws -> TaskTimelinePage {
        let path = "/v1/task-timeline?task=\(Self.queryValue(taskID))"
        if isOfflineSnapshot { return try await readAuthed(path) }
        let started = Date()
        let page = try await TaskTimelineLoader.load(taskID: taskID, previous: previous) { cursor in
            try await self.readAuthed(path + "&limit=500" + (cursor.map { "&cursor=\(Self.queryValue($0))" } ?? ""))
        }
        try Task.checkCancellation()
        if let store = try? GlanceClient.storeDir(), let data = try? JSONEncoder().encode(page) {
            await SavedWorkCache.shared.record(path: path, data: data, store: store, requestStartedAt: started)
        }
        return page
    }

    /// Load one session for a Receipt drill row. Each row owns its result, so
    /// several expanded sessions can remain visible at the same time.
    func loadSession(client clientName: String, sessionId: String) async throws -> V1SessionDetail {
        let encodedClient = Self.queryValue(clientName)
        let encodedSession = Self.queryValue(sessionId)
        return try await readAuthed(
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
                let page: V1SessionsPayload = try await self.readAuthed(
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
        guard canMutateReceipts else { throw WorkProjectionReadOnly() }
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
            let payload: WorksetsPayload = try await readAuthed("/v1/worksets")
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
            let payload: WorksetCandidatesPayload = try await readAuthed("/v1/workset-candidates")
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
            async let planRequest: V1PlanPayload = readAuthed("/v1/plan?days=\(days)")
            async let usageRequest: UsageSummary = client.getLocal("/usage/summary?days=\(days)")
            let (plan, summary) = try await (planRequest, usageRequest)
            guard generation == usageDaysGeneration else { return }
            usageDays = days
            planClients = plan.clients
            usage = summary
            errorText = nil
            let updated = Date()
            usageLastUpdated = updated
            if receiptListError == nil { lastUpdated = receiptListLastUpdated }
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
    var workReturnFocus = WorkTimelineFocusRestoration()

    func prepareWorkReturnFocus() {
        guard pane == .work, let taskId else { return }
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
