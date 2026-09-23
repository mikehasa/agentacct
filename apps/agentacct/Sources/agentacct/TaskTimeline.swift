import Foundation

/// The existing receipt timeline, extended by the daemon for native history.
/// Optional identity fields let receipts from an older recorder still open.
struct TaskTimelineEvent: Codable, Equatable {
    var id: String?
    var eventID: String?
    var kind: String
    var title: String
    var status: String
    var startedAt: Double?
    var updatedAt: Double?
    var occurredAt: Double?
    var timeNote: String?
    var timeWarning: String?
    var sourceLabel: String?
    var sessionKey: String?
    var sessionTitle: String?
    var lineage: String?
    var scope: String?
    var summary: String?
    var files: [String]?
    var exitCode: Int?
    var superseded: Bool?
    var supersededByEventID: String?
    var sectionRecordID: String?
    var sectionRecordIDs: [String]?
    var sectionTitle: String?
    var resolution: String?
    var resolutionScope: String?
    var artifactRef: String?
    var artifactPath: String?
    var artifactURL: String?
    var artifactPathRedacted: Bool?
    var artifactURLRedacted: Bool?
    var commandRedacted: Bool?
    var identityNote: String?
    var disposition: String?

    enum CodingKeys: String, CodingKey {
        case id, kind, title, status, lineage, scope, summary, files, superseded, resolution, disposition
        case eventID = "event_id", startedAt = "started_at", updatedAt = "updated_at", occurredAt = "occurred_at"
        case timeNote = "time_note", timeWarning = "time_warning", sourceLabel = "source_label"
        case sessionKey = "session_key", sessionTitle = "session_title", exitCode = "exit_code"
        case supersededByEventID = "superseded_by_event_id", sectionRecordID = "section_record_id"
        case sectionRecordIDs = "section_record_ids", sectionTitle = "section_title", resolutionScope = "resolution_scope"
        case artifactRef = "artifact_ref", artifactPath = "artifact_path", artifactURL = "artifact_url"
        case artifactPathRedacted = "artifact_path_redacted", artifactURLRedacted = "artifact_url_redacted"
        case commandRedacted = "command_redacted", identityNote = "identity_note"
    }

    func record(taskID: String) -> WorkTimelineRecord? {
        guard let id, !id.isEmpty else { return nil }
        return WorkTimelineRecord(id: id, eventID: eventID,
            laneID: sessionKey ?? "task:\(taskID)", laneTitle: sessionTitle ?? "Task evidence",
            lineage: lineage ?? "Session attribution unavailable", kind: kind == "work" ? .step : kind == "check" ? .check : .activity,
            title: title, start: WorkTimelineProjection.validTime(startedAt ?? occurredAt), end: WorkTimelineProjection.validTime(updatedAt),
            timeNote: timeNote ?? "Source time unavailable", timeWarning: timeWarning,
            result: status, source: sourceLabel ?? "Source unavailable", scope: scope, summary: summary, files: files ?? [],
            exitCode: exitCode, superseded: superseded == true, supersededBy: supersededByEventID,
            sectionRecordID: sectionRecordID, sectionRecordIDs: sectionRecordIDs ?? [], resolution: resolution, resolutionScope: resolutionScope,
            artifact: artifactRef, artifactPath: artifactPathRedacted == true ? nil : artifactPath,
            artifactURL: artifactURLRedacted == true ? nil : artifactURL,
            artifactPathRedacted: artifactPathRedacted, artifactURLRedacted: artifactURLRedacted,
            commandRedacted: commandRedacted == true, identityNote: identityNote, disposition: disposition, sectionTitle: sectionTitle)
    }
}

struct TaskTimelinePage: Codable, Equatable {
    var workProjection: WorkProjectionMetadata? = nil
    static let schema = "agentacct.task-timeline.v1"
    var schemaVersion: String?
    var taskID: String?
    var snapshotID: String?
    var events: [TaskTimelineEvent]
    var offset: Int?
    var shown: Int
    var total: Int
    var truncated: Bool
    var nextCursor: String?

    enum CodingKeys: String, CodingKey {
        case workProjection = "projection"
        case events, offset, shown, total, truncated
        case schemaVersion = "schema_version", taskID = "task_id", snapshotID = "snapshot_id", nextCursor = "next_cursor"
    }

    func projection(taskID: String) -> WorkTimelineProjection {
        guard self.taskID == nil || self.taskID == taskID else {
            return WorkTimelineProjection(records: [], notices: ["Activity belongs to another task and was not loaded."])
        }
        let records = events.compactMap { $0.record(taskID: taskID) }
        let notices = records.count < events.count ? ["Update the recorder to load activity identities and details."] : []
        return WorkTimelineProjection(records: records, notices: notices)
    }
}

enum TaskTimelineError: LocalizedError {
    case inconsistentHistory
    var errorDescription: String? { "Activity history changed or was incomplete. Retaining the last complete snapshot." }
}

/// Assemble one snapshot before publishing. Cancellation and failed/expired
/// pages never replace the user's retained history with a partial mixture.
enum TaskTimelineLoader {
    static func load(taskID: String, previous: TaskTimelinePage? = nil,
                     fetch: (String?) async throws -> TaskTimelinePage) async throws -> TaskTimelinePage {
        for attempt in 0..<3 {
            do { return try await assemble(taskID: taskID, previous: previous, fetch: fetch) }
            catch GlanceClientError.http(409) where attempt < 2 { try Task.checkCancellation() }
        }
        throw TaskTimelineError.inconsistentHistory
    }

    private static func assemble(taskID: String, previous: TaskTimelinePage?,
                                 fetch: (String?) async throws -> TaskTimelinePage) async throws -> TaskTimelinePage {
        var page = try await fetch(nil)
        let snapshotID = page.snapshotID
        let generation = page.workProjection?.generation
        let total = page.total
        var events: [TaskTimelineEvent] = []
        var identities = Set<String>()
        var cursors = Set<String>()
        while true {
            try Task.checkCancellation()
            guard page.schemaVersion == TaskTimelinePage.schema, page.taskID == taskID,
                  let snapshotID, !snapshotID.isEmpty, page.snapshotID == snapshotID,
                  page.workProjection?.generation == generation,
                  total >= 0, page.total == total, page.offset == events.count,
                  page.shown == page.events.count, page.shown <= 500,
                  events.count + page.shown <= total, page.truncated == (page.nextCursor != nil),
                  page.truncated == (events.count + page.shown < total),
                  !page.truncated || page.shown > 0 else { throw TaskTimelineError.inconsistentHistory }
            for event in page.events {
                guard let id = event.id, !id.isEmpty, identities.insert(id).inserted else {
                    throw TaskTimelineError.inconsistentHistory
                }
            }
            if events.isEmpty, let previous, previous.taskID == taskID,
               previous.snapshotID == snapshotID, previous.total == total,
               previous.events.count == total, !previous.truncated {
                var retained = previous
                retained.workProjection = page.workProjection
                return retained
            }
            events += page.events
            guard let cursor = page.nextCursor else { break }
            guard cursors.insert(cursor).inserted else { throw TaskTimelineError.inconsistentHistory }
            page = try await fetch(cursor)
        }
        // The daemon pages newest first; the presentation owns chronological
        // ordering. All identities and relationships remain daemon-authored.
        page.events = events
        page.offset = 0
        page.shown = events.count
        return page
    }
}
