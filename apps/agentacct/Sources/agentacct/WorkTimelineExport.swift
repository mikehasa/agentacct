import Foundation

enum WorkTimelineExport {
    static func text(taskID: String, title: String?, records: [WorkTimelineRecord],
                     projection: WorkTimelineProjection, following: Bool,
                     query: String, file: String?, generatedAt: Date,
                     snapshotAt: Date? = nil, failuresOnly: Bool = false,
                     interval: WorkTimelineInterval? = nil, offlineReceiptAt: Date? = nil,
                     outcome: String? = nil, handoff: String? = nil) -> String {
        var lines = [
            title ?? "Work review", "Task: \(taskID)",
            "Exported: \(generatedAt.ISO8601Format())",
            "Last successful session snapshot: \(snapshotAt?.ISO8601Format() ?? "not observed in this view")",
            "Task outcome: \(outcome ?? "not supplied")", "Handoff: \(handoff ?? "not supplied")",
            "View: \(following ? "following latest observed snapshot" : "held for review")",
            "Scope: \(records.count) displayed \(records.count == 1 ? "record" : "records") of \(projection.records.count) loaded \(projection.records.count == 1 ? "record" : "records").",
            "These are displayed records, not deduplicated check runs. Unloaded evidence is excluded.",
            "Search: \(query.isEmpty ? "none" : query)", "Exact file filter: \(file ?? "none")",
            "Current failures filter: \(failuresOnly ? "on" : "off")",
            "Time range: \(interval.map { Date(timeIntervalSince1970: $0.lower).ISO8601Format() + " to " + Date(timeIntervalSince1970: $0.upper).ISO8601Format() } ?? "all available source times")",
            "Undated records remain included when they match the other filters.",
            "Offline receipt copy: \(offlineReceiptAt?.ISO8601Format() ?? "not an offline receipt")",
            "No token or cost total is calculated by this export.", ""
        ]
        for notice in projection.notices { lines.append("Coverage: \(notice)") }
        for lane in projection.lanes { lines.append("Session coverage — \(lane.title) [\(lane.id)]: \(lane.availability)") }
        for record in records {
            lines += ["", "---", record.title, "Displayed record: \(record.id)",
                "Event identity: \(record.eventID ?? "not supplied")",
                "Evidence lane identity: \(record.laneID)",
                // The reducer's lane and step kind: the same two facts the
                // on-screen list and the canvas axis now carry, so a reviewer
                // reading the export sees what a reviewer reading the app sees.
                "Lane: \(record.laneLabel ?? "not supplied")",
                "Kind: \(record.sectionKind ?? "not supplied")",
                "Session: \(record.laneTitle)", record.lineage,
                "Result: \(record.resultLabel)",
                "Source: \(record.source)", "Scope: \(record.scope ?? "not supplied")",
                "Recorded at: \(record.start.map { Date(timeIntervalSince1970: $0).ISO8601Format() } ?? "unknown")",
                record.timeNote]
            if let start = record.start { lines.append("Precise source time: \(WorkTimelineTimeAxis.exportLabel(start))") }
            if let end = record.end { lines.append("Last section update: \(WorkTimelineTimeAxis.exportLabel(end))") }
            if let summary = record.summary { lines.append("Summary: \(summary)") }
            if let code = record.exitCode { lines.append("Recorded exit code: \(code)") }
            // The reducer's named result/exit-code disagreement, and the
            // recorded continuation point — both reached the model and were
            // printed nowhere.
            if let note = record.noteText { lines.append(note) }
            if let next = record.nextStep { lines.append("Next step: \(next)") }
            if let revision = record.revisionLabel { lines.append(revision) }
            if let disposition = record.disposition { lines.append("Human disposition: \(disposition); recorded result unchanged.") }
            if let note = record.identityNote { lines.append("Identity limitation: \(note)") }
            if let resolution = record.resolutionDescription { lines.append(resolution) }
            lines += record.artifactDescriptions
            if let commandState = record.commandStateText { lines.append(commandState) }
            lines.append(record.files.isEmpty ? "Files: not supplied" : "Files:\n" + record.files.joined(separator: "\n"))
        }
        return lines.joined(separator: "\n") + "\n"
    }
}
