import Foundation
import SwiftUI

// Data for the full window: the /sessions rollup and the /usage/summary cube.
// These are the daemon's machine-local JSON surfaces (localhost-guarded, no
// bearer); the port comes from the same discovery file the glance uses.

@MainActor
final class DashboardStore: ObservableObject {
    @Published private(set) var sessions: [SessionEntry] = []
    @Published private(set) var totalSessions: Int?
    @Published private(set) var usage: UsageSummary?
    @Published private(set) var errorText: String?
    @Published private(set) var isRefreshing = false
    @Published private(set) var lastUpdated: Date?

    private let session: URLSession

    init() {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 30
        session = URLSession(configuration: config)
    }

    func refresh() async {
        guard !isRefreshing else { return }
        isRefreshing = true
        defer { isRefreshing = false }
        do {
            guard let data = try? Data(contentsOf: GlanceClient.discoveryPath()),
                  let discovery = try? JSONDecoder().decode(Discovery.self, from: data)
            else {
                errorText = "daemon not running (no discovery file) — start it with `agentacct start`"
                return
            }
            let host = discovery.host ?? "127.0.0.1"
            let base = "http://\(host):\(discovery.port)"
            let payload: SessionsPayload = try await get("\(base)/sessions?limit=300")
            let summary: UsageSummary = try await get("\(base)/usage/summary?days=30")
            // Root sessions only, newest first: the rollup lists child lanes as
            // their own entries (related.parent set); the window folds them —
            // a child's usage is already summarized under its root.
            sessions = payload.sessions
                .filter(\.isRoot)
                .sorted { ($0.lastActivityAt ?? 0) > ($1.lastActivityAt ?? 0) }
            totalSessions = payload.totalSessions
            usage = summary
            errorText = nil
            lastUpdated = Date()
        } catch {
            errorText = "daemon fetch failed: \(error.localizedDescription)"
        }
    }

    private func get<T: Decodable>(_ urlString: String) async throws -> T {
        guard let url = URL(string: urlString) else {
            throw GlanceClientError.transport("bad URL")
        }
        let (data, response) = try await session.data(from: url)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw GlanceClientError.http((response as? HTTPURLResponse)?.statusCode ?? -1)
        }
        return try JSONDecoder().decode(T.self, from: data)
    }
}

/// The menu bar → main window selection channel.
@MainActor
final class AppSelection: ObservableObject {
    @Published var sessionId: String?
    @Published var pane: MainPane = .sessions
}

enum MainPane: String, CaseIterable, Identifiable {
    case sessions = "Sessions"
    case usage = "Usage"
    case limits = "Limits"
    var id: String { rawValue }
}
