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
    /// When the reducer says a terminal status (completed, blocked, handed off)
    /// was reported. Nil for a section still running and for older payloads.
    var terminalStatusAt: Double?
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
    // Reducer fields (additive; older recorders omit them).
    /// A check's own short name (nil when none was recorded).
    var name: String? = nil
    /// A work step's declared kind (`kind` stays the event kind).
    var sectionKind: String? = nil
    /// A work step's recorded continuation point, verbatim.
    var nextStep: String? = nil
    var evidenceGrade: String? = nil
    /// The reducer's words for the step's evidence grade.
    var evidenceGradeLabel: String? = nil
    var evidenceGradeReason: String? = nil
    /// `at 8a4e024 · main · uncommitted changes` or `revision not captured`.
    var revisionLabel: String? = nil
    /// The failed run this passing run names as fixed.
    var supersedesCheckEventID: String? = nil
    /// `The agent's command argument was not stored; the title is the name the agent recorded.` (nil when none).
    var commandStateText: String? = nil
    /// The reducer's attention-open predicate for a failing check.
    var isCurrentFailure: Bool? = nil
    /// The reducer's result words, tone key and result/exit-code note.
    var statusLabel: String? = nil
    var resultTone: String? = nil
    var noteText: String? = nil
    var artifactPathStateText: String? = nil
    var artifactURLStateText: String? = nil
    /// The reducer's two-lane grammar: `primary` / `supporting` work versus
    /// `evidence` (recorded checks). This is the canvas's vertical meaning —
    /// without it the axis split carried no fact at all.
    var lane: String? = nil
    /// The reducer's words for that lane (`Primary session`, `Check evidence`).
    var laneLabel: String? = nil
    /// Why this record is worth a reviewer's eye, decided by the reducer and
    /// never re-derived here: the strongest reason key, its sentence, every
    /// reason that applied, and the derived restatement. A record the reducer
    /// left unranked carries nothing and is drawn quiet.
    var salience: String? = nil
    var salienceReason: String? = nil
    var salienceKeys: [String]? = nil
    var important: Bool? = nil

    enum CodingKeys: String, CodingKey {
        case id, kind, title, status, lineage, scope, summary, files, superseded, resolution, disposition, name, lane
        case salience, important
        case salienceReason = "salience_reason", salienceKeys = "salience_keys"
        case eventID = "event_id", startedAt = "started_at", updatedAt = "updated_at", occurredAt = "occurred_at"
        case timeNote = "time_note", timeWarning = "time_warning", sourceLabel = "source_label"
        case sessionKey = "session_key", sessionTitle = "session_title", exitCode = "exit_code"
        case supersededByEventID = "superseded_by_event_id", sectionRecordID = "section_record_id"
        case sectionRecordIDs = "section_record_ids", sectionTitle = "section_title", resolutionScope = "resolution_scope"
        case artifactRef = "artifact_ref", artifactPath = "artifact_path", artifactURL = "artifact_url"
        case artifactPathRedacted = "artifact_path_redacted", artifactURLRedacted = "artifact_url_redacted"
        case commandRedacted = "command_redacted", identityNote = "identity_note"
        case sectionKind = "section_kind", nextStep = "next_step", evidenceGrade = "evidence_grade"
        case evidenceGradeLabel = "evidence_grade_label", evidenceGradeReason = "evidence_grade_reason"
        case revisionLabel = "revision_label", supersedesCheckEventID = "supersedes_check_event_id"
        case commandStateText = "command_state_text", isCurrentFailure = "is_current_failure"
        case statusLabel = "status_label", resultTone = "result_tone", noteText = "note_text"
        case artifactPathStateText = "artifact_path_state_text", artifactURLStateText = "artifact_url_state_text"
        case terminalStatusAt = "terminal_status_at", laneLabel = "lane_label"
    }

    func record(taskID: String) -> WorkTimelineRecord? {
        guard let id, !id.isEmpty else { return nil }
        return WorkTimelineRecord(id: id, eventID: eventID,
            laneID: sessionKey ?? "task:\(taskID)", laneTitle: sessionTitle ?? "Task evidence",
            lineage: lineage ?? "Session attribution unavailable",
            // A `beat` is the reducer's progress-note kind: narration the
            // section recorded while it was still open. It is NOT a step, so
            // it never falls through to `.activity` beside one.
            kind: kind == "work" ? .step : kind == "check" ? .check : kind == "beat" ? .beat : .activity,
            title: title, start: WorkTimelineProjection.validTime(startedAt ?? occurredAt), end: WorkTimelineProjection.validTime(updatedAt),
            timeNote: timeNote ?? "Source time unavailable", timeWarning: timeWarning,
            result: status, source: sourceLabel ?? "Source unavailable", scope: scope, summary: summary, files: files ?? [],
            exitCode: exitCode, superseded: superseded == true, supersededBy: supersededByEventID,
            sectionRecordID: sectionRecordID, sectionRecordIDs: sectionRecordIDs ?? [], resolution: resolution, resolutionScope: resolutionScope,
            artifact: artifactRef, artifactPath: artifactPathRedacted == true ? nil : artifactPath,
            artifactURL: artifactURLRedacted == true ? nil : artifactURL,
            artifactPathRedacted: artifactPathRedacted, artifactURLRedacted: artifactURLRedacted,
            commandRedacted: commandRedacted == true, identityNote: identityNote, disposition: disposition, sectionTitle: sectionTitle,
            name: PayloadAbsence.text(name), evidenceGrade: evidenceGrade,
            evidenceGradeLabel: PayloadAbsence.text(evidenceGradeLabel),
            evidenceGradeReason: PayloadAbsence.text(evidenceGradeReason),
            revisionLabel: PayloadAbsence.text(revisionLabel),
            supersedesCheckEventID: PayloadAbsence.text(supersedesCheckEventID),
            commandStateText: PayloadAbsence.text(commandStateText),
            attentionOpenFailure: isCurrentFailure,
            resultText: PayloadAbsence.text(statusLabel), resultTone: PayloadAbsence.text(resultTone),
            noteText: PayloadAbsence.text(noteText),
            artifactPathStateText: PayloadAbsence.text(artifactPathStateText),
            artifactURLStateText: PayloadAbsence.text(artifactURLStateText),
            terminalStatusAt: WorkTimelineProjection.validTime(terminalStatusAt),
            lane: PayloadAbsence.text(lane), laneLabel: PayloadAbsence.text(laneLabel),
            sectionKind: PayloadAbsence.text(sectionKind), nextStep: PayloadAbsence.text(nextStep),
            salience: PayloadAbsence.text(salience),
            salienceReason: PayloadAbsence.text(salienceReason),
            salienceKeys: salienceKeys ?? [],
            // `important` is the reducer's own restatement of "salience is not
            // None". Read it when the payload sent it; otherwise fall back to
            // the key rather than inventing a second rule here.
            important: important ?? (PayloadAbsence.text(salience) != nil))
    }
}

struct TaskTimelinePage: Codable, Equatable {
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
    /// How many of these records are progress notes, and the reducer's own
    /// sentence for what a progress note IS. The receipt carries both; the
    /// paged task-timeline route does not, so both stay optional and a surface
    /// that has only the page simply has no definition to show.
    var beatCount: Int?
    var beatDefinition: String?

    enum CodingKeys: String, CodingKey {
        case events, offset, shown, total, truncated
        case schemaVersion = "schema_version", taskID = "task_id", snapshotID = "snapshot_id", nextCursor = "next_cursor"
        case beatCount = "beat_count", beatDefinition = "beat_definition"
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
        let total = page.total
        var events: [TaskTimelineEvent] = []
        var identities = Set<String>()
        var cursors = Set<String>()
        while true {
            try Task.checkCancellation()
            guard page.schemaVersion == TaskTimelinePage.schema, page.taskID == taskID,
                  let snapshotID, !snapshotID.isEmpty, page.snapshotID == snapshotID,
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
                return previous
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
