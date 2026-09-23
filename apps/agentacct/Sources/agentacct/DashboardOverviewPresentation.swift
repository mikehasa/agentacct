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
                ? "No review items in this saved snapshot."
                : "No failed checks, failed steps, or unresolved blockers are recorded."
        case .focus(_, let count):
            state = .items
            items = Array((payload?.items ?? []).prefix(3).compactMap(DashboardAttentionItem.init))
            total = count
            detail = "Showing \(items.count) of \(count) recorded review items"
        case .inconsistent:
            state = .inconsistent
            items = []
            total = nil
            detail = "The review count and its recorded details do not agree. Refresh before acting."
        case .unavailable(let message):
            state = .unavailable
            items = []
            total = nil
            detail = message
        }
    }
}

enum DashboardRecentWorkScope {
    static func text(visible: Int, total: Int?) -> String {
        guard let total, total >= visible else { return "\(Fmt.count(visible, "recent task")) shown" }
        return "Showing \(visible) of \(Fmt.count(total, "recorded task"))"
    }
}
