import Foundation

/// The canvas's two permanent lanes. The reducer decides which one a record
/// is in (`lane`); this type only says which side of the axis draws it, so
/// collision packing can never move a record across the grammar.
enum WorkTimelineLaneBand: String, Codable, CaseIterable {
    /// Recorded work steps — above the axis.
    case work
    /// Recorded check evidence — below the axis.
    case evidence

    var isAbove: Bool { self == .work }
}

/// A read-only projection. Ordering, shared files and session lineage never
/// create a causal edge or upgrade the result/source supplied by the receipt.
struct WorkTimelineRecord: Identifiable, Equatable, Codable {
    /// `beat` is the reducer's progress-note kind (`task_timeline.BEAT_KIND`):
    /// narration a section recorded while it was still open. It is deliberately
    /// its own kind so no surface can count it as a step or as evidence.
    enum Kind: String, Codable { case step, check, activity, beat }
    var id: String
    var eventID: String? = nil
    var laneID: String
    var laneTitle: String
    var lineage: String
    var kind: Kind
    var title: String
    var start: Double? = nil
    var end: Double? = nil
    var timeNote: String = "Source time unavailable"
    var timeWarning: String? = nil
    var result: String = "unknown"
    var source: String = "Source unavailable"
    var scope: String? = nil
    var summary: String? = nil
    var files: [String] = []
    var exitCode: Int? = nil
    var superseded: Bool = false
    var supersededBy: String? = nil
    var sectionRecordID: String? = nil
    var sectionRecordIDs: [String] = []
    var resolution: String? = nil
    var resolutionScope: String? = nil
    var artifact: String? = nil
    var artifactPath: String? = nil
    var artifactURL: String? = nil
    var artifactPathRedacted: Bool? = nil
    var artifactURLRedacted: Bool? = nil
    var commandRedacted: Bool = false
    var identityNote: String? = nil
    var disposition: String? = nil
    var sectionTitle: String? = nil
    // Reducer fields carried from `TaskTimelineEvent` (additive; older
    // recorders and cached projections omit them).
    /// A check's own short name, when one was recorded.
    var name: String? = nil
    var evidenceGrade: String? = nil
    /// The reducer's tier word for a work step (`self-checked`, `unchecked`, ...).
    var evidenceGradeLabel: String? = nil
    /// The reducer's sentence explaining that grade.
    var evidenceGradeReason: String? = nil
    /// `at 8a4e024 · main · uncommitted changes` or `revision not captured`.
    var revisionLabel: String? = nil
    /// The earlier failed run this run names as superseded.
    var supersedesCheckEventID: String? = nil
    /// The reducer's sentence about the recorded command (nil when none).
    var commandStateText: String? = nil
    /// The reducer's predicate: a recorded failure, not superseded, and its
    /// attention still open. Nil only for payloads that predate it.
    var attentionOpenFailure: Bool? = nil
    /// The reducer's result words for a check (`Failed`, `Could not run`).
    var resultText: String? = nil
    /// The reducer's result tone key (`pass` / `failure` / `not_run`).
    var resultTone: String? = nil
    /// A named result/exit-code disagreement (nil when they agree).
    var noteText: String? = nil
    /// The reducer's redaction sentences for withheld artifact fields.
    var artifactPathStateText: String? = nil
    var artifactURLStateText: String? = nil
    /// When the reducer says a terminal status (completed, blocked, handed off)
    /// was reported. The canvas marks that moment; Swift never decides which
    /// statuses are terminal.
    var terminalStatusAt: Double? = nil
    /// The reducer's lane key (`primary` / `supporting` / `evidence`) and its
    /// words. The canvas's vertical axis renders THIS, so a card's side of the
    /// axis states a recorded fact rather than an arbitrary packing slot.
    var lane: String? = nil
    var laneLabel: String? = nil
    /// A work step's declared kind, verbatim (`debugging`, `review`, …).
    var sectionKind: String? = nil
    /// A work step's recorded continuation point, verbatim.
    var nextStep: String? = nil
    /// The reducer's salience decision for this record: the strongest reason
    /// key, its sentence, and every reason that applied. Salience is a PAYLOAD
    /// FACT (it depends on the whole Task — the largest recorded file set, a
    /// superseded run, a completed-but-unchecked step) and cannot be
    /// re-derived from one record, so no surface tries.
    var salience: String? = nil
    var salienceReason: String? = nil
    var salienceKeys: [String] = []
    /// The reducer's own restatement of "salience is not nil". Never set it
    /// independently of `salience`.
    var important: Bool = false

    /// Narration recorded inside a section, not a step of its own.
    var isBeat: Bool { kind == .beat }

    /// Which side of the axis this record belongs to, from the payload's lane.
    /// A payload without a lane falls back to the record's own kind, so an
    /// older recorder still lands checks and steps on opposite sides.
    var laneBand: WorkTimelineLaneBand {
        if let lane { return lane == "evidence" ? .evidence : .work }
        return kind == .check ? .evidence : .work
    }

    /// The terminal moment worth its own mark: a reported stop that happened
    /// later than the section's start. A terminal time equal to the start adds
    /// nothing — the record's own dot already sits there.
    var terminalMarkTime: Double? {
        guard let terminalStatusAt, let start, terminalStatusAt > start else { return nil }
        return terminalStatusAt
    }

    /// The check's tone, from the payload key (a missing key is `not_run`).
    var checkTone: CheckResultTone { CheckResultTone(payload: resultTone) }

    /// The single "still needs you" signal for a check. The payload predicate
    /// wins (C87); without it only a current recorded failure (the payload's
    /// `failure` tone) counts — a check that could not run never does.
    var isCurrentFailure: Bool {
        guard kind == .check else { return false }
        if let attentionOpenFailure { return attentionOpenFailure }
        return !superseded && disposition == nil && checkTone == .failure
    }
    /// A recorded FAILURE that a later run replaced — the first half of a
    /// fail → pass recovery. It is neither "still failing" (nothing is owed)
    /// nor neutral history (a failure did happen and the recovery is the whole
    /// story the canvas exists to tell), so it takes its own treatment.
    var isResolvedFailure: Bool {
        kind == .check && superseded && disposition == nil && checkTone == .failure
    }
    /// The successor run named by the payload, if any: the other end of the
    /// supersession link the canvas draws.
    var resolvedByEventID: String? { isResolvedFailure ? supersededBy : nil }
    /// The danger tint class shared by the card label, stems and spans: a
    /// current failing check, or a step whose decision key is danger (C26).
    var isDanger: Bool {
        if superseded || disposition != nil { return false }
        if isCurrentFailure { return true }
        return kind == .step && DecisionTintClass.forKey(result) == .danger
    }
    /// Card and inspector title: the check's recorded name, else the title.
    var displayTitle: String { PayloadAbsence.text(name) ?? title }
    var isDuration: Bool { start != nil && end != nil && end! > start! }
    var latestTime: Double? { end ?? start }
    var resolutionDescription: String? {
        if kind == .step { return resolution }
        guard resolution != nil || resolutionScope != nil else { return nil }
        return "Reported resolution (\(resolutionScope ?? "scope not supplied")): \(resolution ?? "summary not supplied")"
    }
    var artifactDescriptions: [String] {
        [artifact.map { "Artifact reference: \($0)" },
         artifactPathRedacted == true
            ? PayloadAbsence.text(artifactPathStateText) ?? PayloadAbsence.artifact
            : artifactPath.map { "Artifact path: \($0)" },
         artifactURLRedacted == true
            ? PayloadAbsence.text(artifactURLStateText) ?? PayloadAbsence.artifact
            : artifactURL.map { "Artifact URL: \($0)" }]
            .compactMap { $0 }
    }
    /// The reducer's status words for this event (`status_label`: `Passed`,
    /// `Failed · superseded`, `Reported completed`), or their named absence.
    /// The superseded state is already part of the payload's words.
    var resultLabel: String {
        PayloadAbsence.text(resultText) ?? PayloadAbsence.checkResult
    }
    var searchableText: String {
        ([title, sectionTitle ?? "", laneID, laneTitle, lineage, source, result, scope ?? "", summary ?? "", eventID ?? ""] + files)
            .joined(separator: " ")
    }
}

/// View-navigation rules for discrete filter changes (C12). A filter toggle
/// that leaves the visible window empty moves to the newest match; matches
/// outside the window are named, never silently hidden.
enum WorkTimelineFilterReveal {
    struct Outside: Equatable {
        var before: Int
        var after: Int
        var newest: WorkTimelineRecord?
        var count: Int { before + after }
    }

    /// The window to move to after a discrete filter change, or nil when the
    /// window already shows a match or nothing matches.
    static func interval(for matches: [WorkTimelineRecord], window: WorkTimelineInterval?) -> WorkTimelineInterval? {
        let dated = matches.filter { $0.start != nil }
        guard let window, !dated.isEmpty, !dated.contains(where: window.contains) else { return nil }
        return newest(dated).flatMap(WorkTimelineRangeNavigation.focused(on:))
    }

    static func outside(_ matches: [WorkTimelineRecord], window: WorkTimelineInterval?) -> Outside {
        guard let window else { return Outside(before: 0, after: 0, newest: nil) }
        let dated = matches.filter { $0.start != nil && !window.contains($0) }
        let before = dated.filter { ($0.latestTime ?? 0) < window.lower }.count
        return Outside(before: before, after: dated.count - before, newest: newest(dated))
    }

    /// `1 failed check before this window` · `3 matching records after this window`.
    static func cueText(_ outside: Outside, failuresOnly: Bool) -> String? {
        guard outside.count > 0 else { return nil }
        let noun = failuresOnly
            ? (outside.count == 1 ? "failed check" : "failed checks")
            : (outside.count == 1 ? "matching record" : "matching records")
        let place = outside.after == 0 ? "before" : (outside.before == 0 ? "after" : "outside")
        return "\(outside.count) \(noun) \(place) this window"
    }

    /// The failures filter button counts distinct check identities from the
    /// receipt tally when the payload carries one, else the loaded records.
    static func failuresButtonTitle(tallyFailed: Int?, currentFailureRecords: Int) -> String {
        let count = tallyFailed.flatMap { $0 > 0 ? $0 : nil } ?? currentFailureRecords
        return "\(count) failed \(count == 1 ? "check" : "checks")"
    }

    private static func newest(_ records: [WorkTimelineRecord]) -> WorkTimelineRecord? {
        records.max {
            let lhs = $0.latestTime ?? 0, rhs = $1.latestTime ?? 0
            return lhs == rhs ? $0.id < $1.id : lhs < rhs
        }
    }
}

struct WorkTimelineLane: Identifiable, Equatable {
    var id: String
    var title: String
    var lineage: String
    var availability: String
}

struct WorkTimelineProjection: Equatable {
    let records: [WorkTimelineRecord]
    let lanes: [WorkTimelineLane]
    let notices: [String]
    let newestRecord: WorkTimelineRecord?
    let interval: WorkTimelineInterval?

    static let empty = WorkTimelineProjection(records: [], lanes: [], notices: [])

    init(records: [WorkTimelineRecord], lanes: [WorkTimelineLane] = [], notices: [String] = []) {
        // Only immutable event IDs authorize deduplication. Anonymous rows with
        // identical text remain separate: sameness of text is not event identity.
        var events = Set<String>()
        self.records = records.filter { record in
            guard let eventID = record.eventID else { return true }
            return events.insert(eventID).inserted
        }.sorted(by: Self.chronological)
        self.lanes = lanes
        self.notices = notices
        newestRecord = self.records.filter { $0.latestTime != nil }.max {
            if $0.latestTime == $1.latestTime { return $0.id < $1.id }
            return $0.latestTime! < $1.latestTime!
        }
        let times = self.records.flatMap { [$0.start, $0.end].compactMap { $0 } }
        guard let lower = times.min(), let upper = times.max() else { interval = nil; return }
        let padding = max((upper - lower) * 0.03, 1)
        interval = WorkTimelineInterval(lower: lower - padding, upper: upper + padding)
    }

    static func chronological(_ lhs: WorkTimelineRecord, _ rhs: WorkTimelineRecord) -> Bool {
        if lhs.start != rhs.start { return (lhs.start ?? .infinity) < (rhs.start ?? .infinity) }
        return lhs.id < rhs.id
    }

    static func nonempty(_ value: String?) -> String? {
        guard let value = value?.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty else { return nil }
        return value
    }

    static func validTime(_ value: Double?) -> Double? {
        guard let value, value.isFinite, value > 0, value < 253_402_300_800 else { return nil }
        return value
    }


}

struct WorkTimelineInterval: Equatable, Codable {
    var lower: Double
    var upper: Double
    var span: Double { max(upper - lower, 1) }

    /// THE rule for "this record is in this window" — the ONE predicate behind
    /// the Activity heading's count, the ordered list, the canvas's own visible
    /// tally and every reveal cue (F5).
    ///
    /// A record is in the window when the moment it is DRAWN AT — its recorded
    /// start, which is where the canvas anchors its card and where the list
    /// orders its row — falls inside the window. It used to be span overlap
    /// here and card position in `WorkTimelineRangeNavigation.holdsACard`, so
    /// the heading counted a 70-minute section whose tail merely crossed the
    /// window while the canvas drew no card for it: the count and the cards
    /// disagreed, and neither was wrong on its own terms. A record whose span
    /// crosses the window still draws its span LINE on the canvas and is still
    /// counted by the outside-the-window cue — it is simply not claimed to be
    /// one of the records in view.
    ///
    /// A record with no usable time is never excluded by a window: it has no
    /// position to judge, and the undated list names it separately.
    func contains(_ record: WorkTimelineRecord) -> Bool {
        guard let start = WorkTimelineProjection.validTime(record.start) else { return true }
        return start >= lower && start <= upper
    }
    func fraction(_ time: Double) -> Double { min(max((time - lower) / span, 0), 1) }
}

/// Synthesized decoding ignores retired comparison keys in saved bookmarks,
/// retaining the user's filters, selection and history without reviving that UI.
struct WorkTimelineBookmark: Equatable, Codable {
    var selectedID: String? = nil
    var query = ""
    var file: String? = nil
    var failuresOnly = false
    var mode = "timeline"
    var interval: WorkTimelineInterval? = nil
    var anchorID: String? = nil
    var scrollOffsets: [WorkTimelineScrollOffset]? = nil
    var overviewExpanded: Bool? = nil
    var previousFileFilters: WorkTimelineFilterContext? = nil
    /// The reviewer's own choice of presentation: the ordered record list, or
    /// the time canvas. Optional (and so absent from older saved bookmarks) so
    /// that "never chosen" stays distinguishable from "chose the canvas" — the
    /// data picks the opening view only while nobody has chosen.
    var recordListChosen: Bool? = nil
}

struct WorkTimelineFilterContext: Equatable, Codable {
    var query: String
    var file: String?
    var failuresOnly: Bool
    var interval: WorkTimelineInterval?
}

struct WorkTimelineNavigation: Equatable, Codable {
    var view = WorkTimelineBookmark()
    var following = true
    var history: WorkTimelineBookmark? = nil

    mutating func showFile(_ file: String) {
        if view.previousFileFilters == nil {
            view.previousFileFilters = .init(query: view.query, file: view.file, failuresOnly: view.failuresOnly, interval: view.interval)
        }
        following = false
        view.file = file
        view.query = ""
        view.failuresOnly = false
        view.interval = nil
    }

    mutating func leaveFile() {
        if let previous = view.previousFileFilters {
            view.query = previous.query; view.file = previous.file
            view.failuresOnly = previous.failuresOnly; view.interval = previous.interval
        } else { view.file = nil }
        view.previousFileFilters = nil
        following = false
    }

    mutating func beginArrivals() {
        if history == nil { history = view }
        following = false
        view.query = ""
        view.file = nil
        view.failuresOnly = false
    }
    /// A bookmark can survive an app restart even when its held snapshot cannot.
    /// Restore the original investigation, never pretend current data is that
    /// earlier snapshot or keep an empty arrivals-only view.
    @discardableResult
    mutating func restorePositionWithoutSnapshot() -> Bool {
        let wasInvestigating = !following || history != nil
        if let history { view = history; self.history = nil }
        if wasInvestigating { following = false }
        return wasInvestigating
    }

    mutating func returnToHistory() {
        guard let history else { return }
        view = history
        self.history = nil
        following = false
    }
}

/// A held snapshot does not mutate records under a selected/dragged view.
/// Pending revisions are counted once by identity, including late source-time
/// arrivals. The current polling API has no ingestion cursor, so this claims
/// snapshot observation only, never lossless event streaming.
struct WorkTimelineFeed {
    private(set) var visible = WorkTimelineProjection.empty
    private(set) var latest = WorkTimelineProjection.empty
    private(set) var initialized = false
    private(set) var historySnapshot: WorkTimelineProjection?
    private(set) var arrivalIDs: Set<String> = []
    private(set) var removedArrivalCount = 0

    private(set) var pendingIDs: Set<String> = []

    private mutating func updatePendingIDs() {
        let existing = Dictionary(visible.records.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        let incoming = Dictionary(latest.records.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        pendingIDs = Set(incoming.keys.filter { existing[$0] != incoming[$0] })
            .union(Set(existing.keys).subtracting(incoming.keys))
    }

    mutating func ingest(_ projection: WorkTimelineProjection, following: Bool) {
        latest = projection
        if following || !initialized { visible = projection }
        initialized = true
        if following { pendingIDs = [] } else { updatePendingIDs() }
    }

    mutating func reveal() { visible = latest; pendingIDs = [] }

    mutating func reviewArrivals() {
        if historySnapshot == nil { historySnapshot = visible }
        arrivalIDs = pendingIDs
        let available = Set(latest.records.map(\.id))
        removedArrivalCount = arrivalIDs.subtracting(available).count
        visible = latest
        pendingIDs = []
    }

    mutating func restoreHistory() {
        if let historySnapshot { visible = historySnapshot }
        historySnapshot = nil
        arrivalIDs = []
        removedArrivalCount = 0
        updatePendingIDs()
    }
}
