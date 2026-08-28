import Foundation
import Observation
import SwiftUI

// The app's one state machine. Poll cadence is a fixed 30s in the skeleton
// (the daemon's glance cache makes polls cheap); CodexBar-style adaptive
// cadence is a follow-up. Phases mirror the precedents this design copied:
// a daemon that is down or incompatible is an explicit, labeled UI state.

@MainActor
@Observable
final class GlanceState {
    enum Phase {
        case connecting
        case disconnected(String)
        case incompatible(String)
        case connected(GlanceSnapshot)
    }

    private(set) var phase: Phase = .connecting
    private(set) var lastUpdated: Date?
    private(set) var isRefreshing = false

    @ObservationIgnored private let client = GlanceClient()
    @ObservationIgnored private var pollTask: Task<Void, Never>?
    private let pollIntervalSeconds: UInt64 = 30

    init() {
        start()
    }

    /// Snapshot/tooling: a fixed state, no polling.
    init(preloaded: GlanceSnapshot) {
        phase = .connected(preloaded)
        lastUpdated = SnapshotMode.currentDate
    }

    /// Snapshot/tooling: an explicit non-connected state with no polling.
    init(preloadedPhase: Phase, lastUpdated: Date? = nil) {
        phase = preloadedPhase
        self.lastUpdated = lastUpdated
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
        guard !isRefreshing else { return }
        isRefreshing = true
        defer { isRefreshing = false }
        do {
            let snapshot = try await client.fetch()
            phase = .connected(snapshot)
            lastUpdated = Date()
        } catch let error as GlanceClientError {
            switch error {
            case .incompatible(let version, let schema):
                phase = .incompatible(
                    "daemon \(version) serves \(schema); this app expects "
                        + "\(GlanceClient.supportedGlanceSchema). Update one of them."
                )
            default:
                phase = .disconnected(error.description)
            }
        } catch {
            phase = .disconnected(error.localizedDescription)
        }
    }
}
