import Foundation

/// A bounded preview of the server-ranked queue. Counts always describe the
/// server's whole queue, while rows retain its order and recorded wording.
struct DashboardAttentionQueue {
    enum State: Equatable {
        case loading, preparing, empty, items, unavailable, inconsistent
    }

    let state: State
    let items: [DashboardAttentionItem]
    let total: Int?
    let detail: String
    let error: String?

    init(payload: V1AttentionPayload?, error: String?, projection: WorkProjectionMetadata?) {
        self.error = error
        // A privacy-invalidated generation must never donate visible rows,
        // even if its old payload has not yet been cleared by the store.
        if projection?.available == false {
            state = .preparing
            items = []
            total = nil
            detail = "Preparing a current view of recorded work."
            return
        }
        let presentation = DashboardAttentionPresentation(payload: payload, error: nil)
        switch presentation {
        case .loading:
            state = error != nil ? .unavailable : projection?.needsRefresh == true ? .preparing : .loading
            items = []
            total = nil
            detail = error ?? (state == .preparing ? "Preparing a current view of recorded work." : "Checking recorded tasks for failed checks, failed steps, and blockers.")
        case .clear:
            state = .empty
            items = []
            total = 0
            detail = error != nil || projection?.needsRefresh == true
                ? "No recorded issues in this saved snapshot."
                : "No failed checks, failed steps, or unresolved blockers are recorded."
        case .focus(_, let count):
            state = .items
            items = Array((payload?.items ?? []).prefix(3).compactMap(DashboardAttentionItem.init))
            total = count
            detail = "Showing \(items.count) of \(count) recorded issues"
        case .inconsistent:
            state = .inconsistent
            items = []
            total = nil
            detail = "The issue count and its recorded details do not agree. Refresh to reload them."
        case .unavailable(let message):
            state = .unavailable
            items = []
            total = nil
            detail = message
        }
    }
}

enum DashboardRecentActivity {
    /// Latest saved task activity comes first. Preserve server order for ties
    /// and unknown dates; missing timestamps must not look like activity now.
    static func items(_ tasks: [ReceiptSummary], limit: Int = 8) -> [DashboardWorkItem] {
        tasks.enumerated().sorted { left, right in
            let a = timestamp(left.element.lastActivityAt), b = timestamp(right.element.lastActivityAt)
            if a != b { return (a ?? -.infinity) > (b ?? -.infinity) }
            return left.offset < right.offset
        }.prefix(max(0, limit)).map { DashboardWorkItem(task: $0.element) }
    }

    private static func timestamp(_ value: Double?) -> Double? {
        value.flatMap { $0.isFinite && $0 > 0 ? $0 : nil }
    }
}

struct DashboardSessionActivity {
    let value: String
    let scope: String

    init(sessions: [RecentSession], availability: DashboardSignalAvailability) {
        switch availability {
        case .loading:
            value = "Loading"
            scope = "Reading recent session status"
        case .unavailable(let error):
            value = "Unavailable"
            scope = error
        case .connected:
            guard !sessions.isEmpty else {
                value = "None recorded"
                scope = "No recent sessions in this snapshot"
                return
            }
            let knownStatuses: Set<String> = ["started", "checkpoint", "in_progress", "completed", "blocked", "handed_off", "inactive", "failed", "cancelled", "interrupted"]
            let known = sessions.filter { knownStatuses.contains($0.status ?? "") }
            let active = known.filter { isActiveWorkStatus($0.status) }
            value = known.isEmpty ? "Status unknown" : "\(active.count) in progress"
            let unknown = sessions.count - known.count
            let lastActive = active.compactMap(\.lastActivityAt).filter {
                $0.isFinite && $0 > 0 && $0 <= SnapshotMode.currentDate.timeIntervalSince1970
            }.max()
            let recency = active.isEmpty ? "" : lastActive.flatMap { agoText($0) }.map { " · last activity \($0)" } ?? " · activity time unavailable"
            scope = "Recorded status · \(Fmt.count(sessions.count, "recent session"))"
                + (unknown > 0 ? " · \(unknown) with unknown status" : "") + recency
        }
    }
}

enum DashboardRecentWorkScope {
    static func text(visible: Int, total: Int?) -> String {
        guard let total, total >= visible else { return "\(Fmt.count(visible, "recent task")) shown" }
        return "Showing \(visible) of \(Fmt.count(total, "recorded task"))"
    }
}
