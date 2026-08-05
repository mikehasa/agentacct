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

    /// Snapshot/tooling: a fixed state, no polling.
    init(preloaded: GlanceSnapshot) {
        phase = .connected(preloaded)
        lastUpdated = Date()
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

    /// Menu bar label: the weekly plan meter (the subscription user's real
    /// question), account truth from the provider's own 7d reading —
    /// claude-code's when live, else the hottest live 7d window. Falls back
    /// to today's cost when no limit reading exists; a quiet marker when the
    /// daemon is down — never a fabricated figure.
    var menuBarTitle: String {
        switch phase {
        case .connected(let snapshot):
            if let used = Self.sevenDayUsedPercent(snapshot.glance) {
                return "⏺ \(Int(used.rounded()))%"
            }
            let today = snapshot.glance.usage.windows.first { $0.label == "today" }
            return "⏺ \(today?.totals.costText ?? "—")"
        case .connecting:
            return "⏺ …"
        case .disconnected, .incompatible:
            return "⏺ ∅"
        }
    }

    /// The provider-reported 7d used %% for the label + dropdown hero:
    /// claude-code's live reading first (the primary plan), else the hottest
    /// live 7d window across accounts. nil when nothing live reports one.
    static func sevenDayUsedPercent(_ glance: Glance, client: String = "claude-code") -> Double? {
        let live = glance.limits.filter { $0.stale != true }
        let sevenDay: (LimitEntry) -> Double? = { entry in
            (entry.windows ?? []).first { $0.kind == "7d" }?.usedPercent
        }
        if let primary = live.first(where: { $0.client == client }), let used = sevenDay(primary) {
            return used
        }
        return live.compactMap(sevenDay).max()
    }
}
