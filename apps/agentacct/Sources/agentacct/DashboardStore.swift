import Foundation
import SwiftUI

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
    @Published private(set) var receipt: Receipt?
    @Published private(set) var receiptListError: String?
    @Published private(set) var receiptError: String?
    /// Session deep views preloaded by key ("client::session"). Only the offscreen
    /// snapshot path fills this (the live app loads each drill row lazily via a
    /// SwiftUI `.task`, which ImageRenderer does not run); a drill row reads it as
    /// a fallback so its steps render in a snapshot.
    @Published private(set) var preloadedSessions: [String: V1SessionDetail] = [:]
    @Published private(set) var errorText: String?
    /// Source/watcher health from /v1/ingestion (the Sources pane).
    @Published private(set) var ingestion: V1IngestionSnapshot?
    @Published private(set) var ingestionError: String?
    @Published private(set) var isRefreshing = false
    @Published private(set) var lastUpdated: Date?

    /// The usage-pane range (7/30/90 trailing days). Defaults to 7 so the
    /// per-model plan breakdown lines up with the 7d headline out of the box
    /// (a 30-day accumulation reads as >100% of a weekly plan and confuses);
    /// the today/7d headline windows are fixed regardless of this.
    @Published private(set) var usageDays = 7

    /// Monotonic token so rapid range switches can't land out of order and a
    /// failed fetch can't leave the old data labeled with the new range.
    private var usageDaysGeneration = 0

    private let client = GlanceClient()

    init() {}

    /// Design-review tooling: populate the same state the daemon endpoints
    /// would, without network access or a developer's local account data.
    init(preloaded fixture: DashboardSnapshotFixture) {
        planClients = fixture.plan.clients
        usage = fixture.usage
        receiptTasks = fixture.tasks.tasks
        totalReceiptTasks = fixture.tasks.total
        lastUpdated = fixture.glance.generatedAt.map(Date.init(timeIntervalSince1970:))
    }

    func refresh() async {
        guard !isRefreshing else { return }
        isRefreshing = true
        defer { isRefreshing = false }
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
            receiptTasks = tasks.tasks
            totalReceiptTasks = tasks.total
            receiptListError = nil
            tasksSucceeded = true
        } catch GlanceClientError.noDiscovery(_) {
            receiptListError = "daemon not running (no discovery file) — start it with `agentacct start`"
        } catch {
            receiptListError = "receipts fetch failed: \(error.localizedDescription)"
        }

        do {
            let payload = try await ingestionRequest
            ingestion = payload.ingestion
            ingestionError = nil
        } catch GlanceClientError.http(404) {
            // An older daemon without the route: a named state, not an error toast.
            ingestionError = "this daemon predates /v1/ingestion"
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
            if tasksSucceeded { lastUpdated = Date() }
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
        do {
            let payload: ReceiptTasksPayload = try await client.getAuthed("/v1/tasks?limit=200")
            receiptTasks = payload.tasks
            totalReceiptTasks = payload.total
            receiptListError = nil
        } catch GlanceClientError.noDiscovery(_) {
            receiptListError = "daemon not running (no discovery file) — start it with `agentacct start`"
        } catch {
            receiptListError = "receipts fetch failed: \(error.localizedDescription)"
        }
    }

    /// One Task's full Receipt. 404 (task unknown / recorded elsewhere) is a
    /// first-class message; a CANCELLED fetch (the user picked another Task)
    /// writes nothing so a late error can't mask the fresh receipt.
    func fetchReceipt(taskId: String) async {
        receipt = nil
        receiptError = nil
        do {
            let encoded = taskId.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? taskId
            let payload: Receipt = try await client.getAuthed("/v1/receipt?task=\(encoded)")
            guard !Task.isCancelled else { return }
            receipt = payload
            receiptError = nil
        } catch is CancellationError {
            return
        } catch let error as URLError where error.code == .cancelled {
            return
        } catch GlanceClientError.http(404) {
            guard !Task.isCancelled else { return }
            receiptError = "this Task is not in the store (it may have been recorded elsewhere)"
        } catch {
            guard !Task.isCancelled else { return }
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
            if receiptListError == nil { lastUpdated = Date() }
        } catch {
            guard generation == usageDaysGeneration else { return }
            errorText = "usage range fetch failed: \(error.localizedDescription)"
        }
    }
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
            pane = .limits
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
    case limits = "Limits"
    case sources = "Sources"
    var id: String { rawValue }
}
