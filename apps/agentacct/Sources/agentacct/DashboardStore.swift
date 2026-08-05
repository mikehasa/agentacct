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
    @Published private(set) var errorText: String?
    @Published private(set) var isRefreshing = false
    @Published private(set) var isLoadingMore = false
    @Published private(set) var lastUpdated: Date?

    /// The cost-chart range (7/30/90 trailing days); the plan lane stays on
    /// its own fixed windows.
    @Published private(set) var usageDays = 30

    private let client = GlanceClient()
    private let pageSize = 60

    func refresh() async {
        guard !isRefreshing else { return }
        isRefreshing = true
        defer { isRefreshing = false }
        do {
            // Preserve the paginated depth across auto-refreshes: replacing a
            // Load-more'd list with page 1 collapsed the walk and blanked an
            // open detail mid-read (review finding).
            let limit = max(pageSize, min(sessions.count, 500))
            let payload: V1SessionsPayload = try await client.getAuthed(
                "/v1/sessions?limit=\(limit)&offset=0"
            )
            let plan: V1PlanPayload = try await client.getAuthed("/v1/plan?days=30")
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

    /// The plan status for one client (three-state honesty), if known.
    func planStatus(for clientName: String) -> V1PlanStatus? {
        planStatuses.first { $0.client == clientName }
    }

    /// Switch the cost-chart range and refetch just the usage cube.
    func setUsageDays(_ days: Int) async {
        guard days != usageDays else { return }
        usageDays = days
        do {
            let summary: UsageSummary = try await client.getLocal("/usage/summary?days=\(days)")
            usage = summary
        } catch {
            errorText = "usage range fetch failed: \(error.localizedDescription)"
        }
    }
}

/// The menu bar → main window selection channel.
@MainActor
final class AppSelection: ObservableObject {
    @Published var sessionId: String?
    @Published var pane: MainPane = .dashboard
}

enum MainPane: String, CaseIterable, Identifiable {
    case dashboard = "Dashboard"
    case sessions = "Sessions"
    case usage = "Usage"
    case limits = "Limits"
    var id: String { rawValue }
}
