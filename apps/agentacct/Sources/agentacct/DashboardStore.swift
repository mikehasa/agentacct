import Foundation
import SwiftUI

// Data for the full window, all on the authenticated /v1 lane:
// /v1/sessions (server-side roots + pagination + plan shares),
// /v1/session (the one-session deep view), /v1/plan (attributed aggregates).
// The legacy /usage/summary cube still feeds the cost charts (no /v1 twin
// yet). Honesty rides the payloads; the store never re-derives a number.

@MainActor
final class DashboardStore: ObservableObject {
    @Published private(set) var sessions: [V1SessionRow] = []
    @Published private(set) var totalSessions: Int?
    @Published private(set) var totalRootSessions: Int?
    @Published private(set) var truncated = false
    @Published private(set) var planStatuses: [V1PlanStatus] = []
    @Published private(set) var planClients: [V1PlanClient] = []
    @Published private(set) var usage: UsageSummary?
    @Published private(set) var detail: V1SessionDetail?
    @Published private(set) var detailError: String?
    @Published private(set) var receiptTasks: [ReceiptSummary] = []
    @Published private(set) var receipt: Receipt?
    @Published private(set) var receiptError: String?
    /// Session deep views preloaded by key ("client::session"). Only the offscreen
    /// snapshot path fills this (the live app loads each drill row lazily via a
    /// SwiftUI `.task`, which ImageRenderer does not run); a drill row reads it as
    /// a fallback so its steps render in a snapshot.
    @Published private(set) var preloadedSessions: [String: V1SessionDetail] = [:]
    @Published private(set) var errorText: String?
    @Published private(set) var isRefreshing = false
    @Published private(set) var isLoadingMore = false
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
    private let pageSize = 60

    init() {}

    /// Design-review tooling: populate the same state the daemon endpoints
    /// would, without network access or a developer's local account data.
    init(preloaded fixture: DashboardSnapshotFixture) {
        sessions = fixture.sessions.sessions
        totalSessions = fixture.sessions.totalSessions
        totalRootSessions = fixture.sessions.totalRootSessions
        truncated = fixture.sessions.truncated ?? false
        planStatuses = fixture.sessions.plan ?? []
        planClients = fixture.plan.clients
        usage = fixture.usage
        receiptTasks = fixture.tasks.tasks
        lastUpdated = fixture.glance.generatedAt.map(Date.init(timeIntervalSince1970:))
    }

    func refresh() async {
        guard !isRefreshing else { return }
        isRefreshing = true
        defer { isRefreshing = false }
        async let receipts: Void = fetchReceipts()
        do {
            // Preserve the paginated depth across auto-refreshes: replacing a
            // Load-more'd list with page 1 collapsed the walk and blanked an
            // open detail mid-read (review finding).
            let limit = max(pageSize, min(sessions.count, 500))
            let payload: V1SessionsPayload = try await client.getAuthed(
                "/v1/sessions?limit=\(limit)&offset=0"
            )
            // Both the plan lane and the cost cube follow the pane range so the
            // per-model breakdown, the daily bars, and the $ view all describe
            // the same window. The today/7d headline windows are fixed inside
            // the plan payload regardless of this days param.
            let plan: V1PlanPayload = try await client.getAuthed("/v1/plan?days=\(usageDays)")
            let summary: UsageSummary = try await client.getLocal("/usage/summary?days=\(usageDays)")
            sessions = payload.sessions
            totalSessions = payload.totalSessions
            totalRootSessions = payload.totalRootSessions
            truncated = payload.truncated ?? false
            planStatuses = payload.plan ?? []
            planClients = plan.clients
            usage = summary
            errorText = nil
            lastUpdated = Date()
        } catch GlanceClientError.noDiscovery(_) {
            errorText = "daemon not running (no discovery file) — start it with `agentacct start`"
        } catch {
            errorText = "daemon fetch failed: \(error.localizedDescription)"
        }
        await receipts
    }

    /// The next page of the recency-ordered roots walk (server-side slice;
    /// `truncated` from the envelope says whether more rows exist).
    func loadMore() async {
        guard truncated, !isLoadingMore else { return }
        isLoadingMore = true
        defer { isLoadingMore = false }
        do {
            let payload: V1SessionsPayload = try await client.getAuthed(
                "/v1/sessions?limit=\(pageSize)&offset=\(sessions.count)"
            )
            // A session landing between pages shifts the window by one; the
            // id-keyed de-dup keeps the walk honest instead of double-listing.
            let known = Set(sessions.map(\.id))
            sessions += payload.sessions.filter { !known.contains($0.id) }
            truncated = payload.truncated ?? false
            totalSessions = payload.totalSessions
            totalRootSessions = payload.totalRootSessions
        } catch {
            errorText = "load more failed: \(error.localizedDescription)"
        }
    }

    /// The deep view for one session. 404 (session aged out / unknown) is a
    /// first-class message, never a silent empty screen. A CANCELLED fetch
    /// (the user clicked another row) writes nothing: its error used to land
    /// after the new row's fetch had already started and masked the freshly
    /// loaded steps (review finding).
    func fetchDetail(client clientName: String, sessionId: String) async {
        detail = nil
        detailError = nil
        do {
            let encodedClient = clientName.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? clientName
            let encodedSession = sessionId.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? sessionId
            let payload: V1SessionDetail = try await client.getAuthed(
                "/v1/session?client=\(encodedClient)&session_id=\(encodedSession)"
            )
            guard !Task.isCancelled else { return }
            detail = payload
            detailError = nil
        } catch is CancellationError {
            return
        } catch let error as URLError where error.code == .cancelled {
            return
        } catch GlanceClientError.http(404) {
            guard !Task.isCancelled else { return }
            detailError = "this session is not in the store (it may have been recorded elsewhere)"
        } catch {
            guard !Task.isCancelled else { return }
            detailError = "detail fetch failed: \(error.localizedDescription)"
        }
    }

    /// The Task list for the Receipts pane (one compact Receipt summary each).
    func fetchReceipts() async {
        do {
            let payload: ReceiptTasksPayload = try await client.getAuthed("/v1/tasks?limit=200")
            receiptTasks = payload.tasks
            receiptError = nil
        } catch GlanceClientError.noDiscovery(_) {
            receiptError = "daemon not running (no discovery file) — start it with `agentacct start`"
        } catch {
            receiptError = "receipts fetch failed: \(error.localizedDescription)"
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

    /// Load one session's deep view WITHOUT touching the shared `detail` slot —
    /// the Work drill-down expands several of a Task's sessions independently,
    /// so each drill row owns its own result. Reuses the same authed endpoint
    /// as `fetchDetail`; the caller holds the returned value in local state.
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

    /// The plan status for one client (three-state honesty), if known.
    func planStatus(for clientName: String) -> V1PlanStatus? {
        planStatuses.first { $0.client == clientName }
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
            let plan: V1PlanPayload = try await client.getAuthed("/v1/plan?days=\(days)")
            let summary: UsageSummary = try await client.getLocal("/usage/summary?days=\(days)")
            guard generation == usageDaysGeneration else { return }
            usageDays = days
            planClients = plan.clients
            usage = summary
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
    var id: String { rawValue }
}
