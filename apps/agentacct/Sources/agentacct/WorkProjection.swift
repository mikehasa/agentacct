import Foundation
import SwiftUI

/// Freshness belongs to the daemon's completed generation, never the HTTP read.
struct WorkProjectionMetadata: Codable, Equatable {
    var state: String
    var available: Bool? = nil
    var builtAt: Double?
    var generation: String?
    var error: String?

    enum CodingKeys: String, CodingKey {
        case state, available, generation, error
        case builtAt = "built_at"
    }

    var builtDate: Date? {
        guard let builtAt, builtAt.isFinite, builtAt > 0 else { return nil }
        return Date(timeIntervalSince1970: builtAt)
    }
    var isCurrent: Bool { state == "current" && available != false }
    var needsRefresh: Bool { !isCurrent }
    var statusText: String {
        switch state {
        case "current": return "Updated"
        case "pending": return "Preparing work receipts"
        case "updating": return "Updating work receipts"
        case "error": return "Work receipt update delayed"
        default: return "Work receipt freshness unavailable"
        }
    }
    var asOfText: String {
        builtDate.map { "As of \($0.formatted(date: .abbreviated, time: .standard))" }
            ?? "Snapshot time unavailable"
    }

    func retainingBuild(from previous: Self?) -> Self {
        guard available != false else { return self }
        var result = self
        if result.builtDate == nil {
            result.builtAt = previous?.builtAt
            result.generation = previous?.generation
        }
        return result
    }

    static func failed(_ error: Error, retaining previous: Self?) -> Self {
        Self(state: "error", available: previous?.available, builtAt: previous?.builtAt, generation: previous?.generation,
             error: error.localizedDescription)
    }

    static let pending = Self(state: "pending", builtAt: nil, generation: nil, error: nil)

    static func from(_ data: Data) -> Self? {
        struct Envelope: Decodable { let projection: WorkProjectionMetadata? }
        return (try? JSONDecoder().decode(Envelope.self, from: data))?.projection
    }
}

struct WorkProjectionPending: LocalizedError {
    let projection: WorkProjectionMetadata
    var errorDescription: String? { projection.statusText }
}

struct WorkProjectionReadOnly: LocalizedError {
    var errorDescription: String? { "Wait for the current work receipt before changing a finding." }
}

/// Absent metadata preserves the old daemon's UI. Healthy generations use a
/// quiet timestamp; rebuilding generations explicitly retain their as-of time.
struct WorkProjectionNotice: View {
    let projection: WorkProjectionMetadata?
    var isOffline = false
    var body: some View {
        if let projection {
            HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                if projection.needsRefresh {
                    Image(systemName: projection.state == "error" ? "clock.badge.exclamationmark" : "arrow.triangle.2.circlepath")
                        .accessibilityHidden(true)
                }
                Text(isOffline ? "Saved snapshot · \(projection.asOfText)" : projection.isCurrent ? projection.asOfText
                     : "\(projection.statusText) · \(projection.builtDate == nil ? "Checking again automatically" : projection.asOfText)")
                    .fixedSize(horizontal: false, vertical: true)
            }
            .workFont(.caption)
            .foregroundStyle(projection.needsRefresh ? Theme.amber : Theme.muted)
            .help(isOffline ? "Offline copy; reconnect the recorder to refresh." : projection.error ?? (projection.needsRefresh ? "Showing the last completed snapshot while the recorder prepares an update." : "Time this snapshot was built by the recorder."))
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("work.projection.status")
        }
    }
}
