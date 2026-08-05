import Foundation
import SwiftUI

// The app's one state machine. Poll cadence is a fixed 30s in the skeleton
// (the daemon's glance cache makes polls cheap); CodexBar-style adaptive
// cadence is a follow-up. Phases mirror the precedents this design copied:
// a daemon that is down or incompatible is an explicit, labeled UI state.

@MainActor
final class GlanceState: ObservableObject {
    enum Phase {
        case connecting
        case disconnected(String)
        case incompatible(String)
        case connected(GlanceSnapshot)
    }

    @Published private(set) var phase: Phase = .connecting
    @Published private(set) var lastUpdated: Date?
    @Published private(set) var isRefreshing = false

    private let client = GlanceClient()
    private var pollTask: Task<Void, Never>?
    private let pollIntervalSeconds: UInt64 = 30

    init() {
        start()
    }

    func start() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while let self, !Task.isCancelled {
                await self.poll()
                try? await Task.sleep(nanoseconds: self.pollIntervalSeconds * 1_000_000_000)
            }
        }
    }

    func refreshNow() {
        Task { await poll() }
    }

    private func poll() async {
        isRefreshing = true
        defer { isRefreshing = false }
        do {
            let snapshot = try await client.fetch()
            phase = .connected(snapshot)
            lastUpdated = Date()
        } catch let error as GlanceClientError {
            switch error {
            case .incompatible(let version, let schema):
                phase = .incompatible("daemon \(version) serves \(schema); this app expects \(GlanceClient.supportedGlanceSchema). Update one of them.")
            default:
                phase = .disconnected(error.description)
            }
        } catch {
            phase = .disconnected(error.localizedDescription)
        }
    }

    /// Menu bar label: today's cost when connected (the number a glance is
    /// for), a quiet marker otherwise — never a fabricated figure.
    var menuBarTitle: String {
        switch phase {
        case .connected(let snapshot):
            let today = snapshot.glance.usage.windows.first { $0.label == "today" }
            let cost = today?.totals.costText ?? "—"
            return "⏺ \(cost)"
        case .connecting:
            return "⏺ …"
        case .disconnected, .incompatible:
            return "⏺ ∅"
        }
    }
}
