import SwiftUI

/// Geometry for view navigation only. It never changes evidence timestamps.
enum WorkTimelineRangeNavigation {
    static func focused(on record: WorkTimelineRecord) -> WorkTimelineInterval? {
        guard let start = record.start else { return nil }
        let end = record.end ?? start
        let context = max((end - start) * 0.15, 60)
        return .init(lower: start - context, upper: end + context)
    }

    /// Whether a window would draw at least one CARD. A card is drawn at its
    /// record's start, so this is a test of card positions — never of span
    /// overlap. A 70-minute section whose tail crosses the window puts no card
    /// on screen, and the canvas then prints its named-empty state.
    ///
    /// That IS `WorkTimelineInterval.contains` now (F5): the heading's count,
    /// the list and this test all ask the one predicate, so a window can never
    /// be reported populated by a rule the canvas does not draw by. Undated
    /// records are excluded here — they have no position to draw at, whereas
    /// the shared rule declines to exclude them from a filtered READING.
    static func holdsACard(_ window: WorkTimelineInterval, records: [WorkTimelineRecord]) -> Bool {
        records.contains { record in
            guard WorkTimelineProjection.validTime(record.start) != nil else { return false }
            return window.contains(record)
        }
    }

    /// A window the APP chose must draw at least one card. When the chosen span
    /// lands where no card is drawn, fall back to the newest dated record's own
    /// focused window rather than opening on a named-empty canvas. A reviewer's
    /// own pan is never passed through here: dragging into a quiet stretch is a
    /// request to see it.
    static func populated(_ window: WorkTimelineInterval,
                          records: [WorkTimelineRecord],
                          newest: WorkTimelineRecord?,
                          within full: WorkTimelineInterval) -> WorkTimelineInterval {
        let dated = records.filter { $0.start != nil }
        guard !dated.isEmpty, !holdsACard(window, records: dated) else { return window }
        guard let newest, let focused = focused(on: newest) else { return full }
        return WorkTimeCanvasLayout.clampedWindow(focused, to: full)
    }

    /// The window that must be shown when the request to select a record came
    /// from ELSEWHERE on the record page — the Checks table — rather than from
    /// a click inside the surface itself.
    ///
    /// `nil` means the visible window already draws that record's card, so the
    /// selection re-frames nothing: a reviewer who can already see the mark
    /// keeps their zoom. Re-framing is internal to the canvas/list either way;
    /// the document never moves for it (F9).
    static func reframed(_ window: WorkTimelineInterval,
                         toShow record: WorkTimelineRecord) -> WorkTimelineInterval? {
        guard !holdsACard(window, records: [record]) else { return nil }
        return focused(on: record)
    }

    /// Select only a visible recorded mark. An empty gap is not evidence.
    static func hitRecord(_ records: [WorkTimelineRecord], laneID: String, x: Double, width: Double,
                          within full: WorkTimelineInterval, tolerance: Double = 6) -> WorkTimelineRecord? {
        guard width > 0, x >= 0, x <= width else { return nil }
        let candidates = records.compactMap { record -> (WorkTimelineRecord, Double, Double)? in
            guard record.laneID == laneID, let start = record.start else { return nil }
            let left = full.fraction(start) * width
            if record.isDuration, let end = record.end {
                let right = full.fraction(end) * width
                let distance = max(left - x, x - right, 0)
                return distance <= tolerance ? (record, 1, max(right - left, 0)) : nil
            }
            let distance = abs(left - x)
            return distance <= tolerance ? (record, 0, distance) : nil
        }
        return candidates.sorted {
            if $0.1 != $1.1 { return $0.1 < $1.1 }
            if $0.2 != $1.2 { return $0.2 < $1.2 }
            return $0.0.id < $1.0.id
        }.first?.0
    }

    static func selected(from start: Double, to end: Double, within full: WorkTimelineInterval) -> WorkTimelineInterval {
        let lower = min(max(min(start, end), full.lower), full.upper)
        let upper = min(max(max(start, end), lower + min(1, full.span)), full.upper)
        return .init(lower: min(lower, upper - min(1, full.span)), upper: upper)
    }

    static func shifted(_ window: WorkTimelineInterval, by delta: Double, within full: WorkTimelineInterval) -> WorkTimelineInterval {
        let span = min(window.span, full.span)
        let lower = min(max(window.lower + delta, full.lower), full.upper - span)
        return .init(lower: lower, upper: lower + span)
    }
}

enum WorkTimelineTimeAxis {
    /// The exact source value, for the record inspector's labelled `Source
    /// time` line and the export. It is UTC and says so.
    static func preciseLabel(_ time: Double) -> String {
        let date = Date(timeIntervalSince1970: time)
        return "\(date.ISO8601Format(.init(includingFractionalSeconds: true))) UTC"
    }

    /// The exact source value written to an EXPORTED file, which a machine may
    /// read back: the UTC instant plus its raw epoch seconds. Screens use
    /// `preciseLabel`; nothing spoken uses either (K122).
    static func exportLabel(_ time: Double) -> String {
        "\(preciseLabel(time)) · Unix \(time)"
    }

    /// The time a person hears or reads: the shared local date and clock (C55)
    /// to the second, so two records a moment apart stay distinguishable and
    /// VoiceOver never reads an epoch (K122).
    static func spokenLabel(_ time: Double) -> String {
        let date = Date(timeIntervalSince1970: time)
        return "\(Fmt.displayDate(date)), \(Fmt.clockTimeWithSeconds(date))"
    }
    static func showsDates(in range: WorkTimelineInterval, calendar: Calendar = .current) -> Bool {
        !calendar.isDate(Date(timeIntervalSince1970: range.lower), inSameDayAs: Date(timeIntervalSince1970: range.upper))
    }
    static func label(_ time: Double, range: WorkTimelineInterval) -> String {
        formatter(template: showsDates(in: range) ? "MMMdjm" : "jms").string(from: Date(timeIntervalSince1970: time))
    }

    // MARK: Anchored ticks

    /// Candidate tick intervals in ascending order. Sub-day steps divide the
    /// hour or day evenly; larger steps count whole local days.
    private static let tickSteps: [Double] = [
        1, 2, 5, 10, 15, 30,
        60, 120, 300, 600, 900, 1_800,
        3_600, 7_200, 10_800, 21_600, 43_200,
        86_400, 172_800, 259_200, 604_800, 1_209_600, 2_592_000, 5_184_000,
        7_776_000, 15_724_800, 31_557_600,
    ]

    /// Date formatters are expensive to build and labels are regenerated on
    /// every canvas update, so templates are cached per calendar and locale.
    /// Main-thread-only use matches the views that call them.
    nonisolated(unsafe) private static var formatterCache: [String: DateFormatter] = [:]

    private static func formatter(template: String, calendar: Calendar = .current) -> DateFormatter {
        let key = "\(template)|\(calendar.identifier)|\(calendar.timeZone.identifier)|\(Locale.current.identifier)|\(Locale.current.hourCycle)"
        if let cached = formatterCache[key] { return cached }
        let formatter = DateFormatter()
        formatter.calendar = calendar
        formatter.timeZone = calendar.timeZone
        formatter.setLocalizedDateFormatFromTemplate(template)
        formatterCache[key] = formatter
        return formatter
    }

    /// The smallest step whose on-screen spacing stays readable at this scale.
    /// The result is finite and positive for any input.
    static func tickStep(span: Double, width: Double, minimumSpacing: Double) -> Double {
        let span = span.isFinite && span > 0 ? span : 1
        let width = width.isFinite && width > 0 ? width : 1
        let spacing = minimumSpacing.isFinite && minimumSpacing > 0 ? minimumSpacing : 100
        let needed = span * spacing / width
        let target = needed.isFinite && needed > 0 ? needed : Double.greatestFiniteMagnitude / 4
        for step in tickSteps where step >= target { return step }
        var step = tickSteps.last!
        while step < target, step < 1e15 { step *= 4 }
        return step
    }

    /// Absolute tick times inside `window`. Sub-day steps anchor to epoch
    /// multiples; day-and-larger steps align to local midnights. The step
    /// depends only on span and width — never on the window's position — so
    /// panning translates the same marks across the screen rather than
    /// redividing or relabelling them.
    static func ticks(in window: WorkTimelineInterval, width: Double, minimumSpacing: Double,
                      calendar: Calendar = .current) -> (step: Double, times: [Double]) {
        // Sanitize before any conversion so degenerate geometry can never
        // crash or produce an unbounded loop.
        let width = width.isFinite && width > 0 ? width : 1
        let minimumSpacing = minimumSpacing.isFinite && minimumSpacing > 0 ? minimumSpacing : 100
        let step = tickStep(span: window.span, width: width, minimumSpacing: minimumSpacing)
        let lower = min(window.lower, window.upper), upper = max(window.lower, window.upper)
        guard lower.isFinite, upper.isFinite else { return (step, []) }
        let limit = max(2, Int(min(width / minimumSpacing, 1e6)) + 4)
        if step < 86_400 {
            var tick = (lower / step).rounded(.up) * step
            var times: [Double] = []
            while tick <= upper, times.count < limit {
                times.append(tick)
                tick += step
            }
            return (step, times)
        }
        // Day-level steps follow local midnights on an absolute lattice: the
        // phase is counted from the local epoch day, so panning the window
        // never shifts where marks land. The phase floors for pre-1970 dates.
        let dayStep = max(1, Int(min(step / 86_400, 1e12).rounded()))
        let reference = calendar.startOfDay(for: Date(timeIntervalSince1970: 0))
        let lowerDay = calendar.startOfDay(for: Date(timeIntervalSince1970: lower))
        let elapsedDays = calendar.dateComponents([.day], from: reference, to: lowerDay).day ?? 0
        var offset = elapsedDays - (((elapsedDays % dayStep) + dayStep) % dayStep)
        var times: [Double] = []
        while times.count < limit {
            guard let day = calendar.date(byAdding: .day, value: offset, to: reference) else { break }
            let time = day.timeIntervalSince1970
            if time > upper { break }
            if time >= lower { times.append(time) }
            offset += dayStep
        }
        return (step, times)
    }

    /// A mark's label depends on its absolute time and the current step, never
    /// on the window position: panning keeps each mark's label unchanged, and
    /// reformatting happens only when zoom changes the step or day context.
    static func tickLabel(_ time: Double, step: Double, range: WorkTimelineInterval,
                          calendar: Calendar = .current) -> String {
        let template: String
        if step >= 86_400 {
            template = range.span > 400 * 86_400 ? "yMMMd" : "MMMdj"
        } else if showsDates(in: range, calendar: calendar)
                    || Date(timeIntervalSince1970: time) == calendar.startOfDay(for: Date(timeIntervalSince1970: time)) {
            template = "MMMdjm"
        } else {
            template = step < 60 ? "jms" : "jm"
        }
        return formatter(template: template, calendar: calendar).string(from: Date(timeIntervalSince1970: time))
    }
}

/// One coordinate system for the labels, Canvas marks, and pointer targets.
/// Font scaling must never move a label away from the lane that it selects.
struct WorkTimelineOverviewGeometry: Equatable {
    let laneHeight: CGFloat
    let verticalInset: CGFloat
    let labelWidth: CGFloat
    let markRadius: CGFloat
    let selectionRadius: CGFloat
    let hitTolerance: CGFloat
    let handleWidth: CGFloat
    let handleHeight: CGFloat

    init(dynamicTypeSize: DynamicTypeSize, systemScale: CGFloat = 1) {
        func scaled(_ base: CGFloat) -> CGFloat {
            WorkTypeScale.resolved(base: base, systemScaled: base * systemScale, dynamicTypeSize: dynamicTypeSize)
        }
        laneHeight = scaled(15)
        verticalInset = scaled(8)
        labelWidth = scaled(150)
        markRadius = scaled(2)
        selectionRadius = scaled(4)
        hitTolerance = scaled(6)
        handleWidth = scaled(3)
        handleHeight = scaled(14)
    }

    func height(laneCount: Int) -> CGFloat {
        CGFloat(max(laneCount, 1)) * laneHeight + verticalInset * 2
    }

    func laneCenter(at index: Int) -> CGFloat {
        verticalInset + (CGFloat(index) + 0.5) * laneHeight
    }

    func laneIndex(at y: CGFloat, laneCount: Int) -> Int? {
        guard laneCount > 0, y.isFinite, y >= verticalInset,
              y < verticalInset + CGFloat(laneCount) * laneHeight else { return nil }
        let index = Int(floor((y - verticalInset) / laneHeight))
        return abs(y - laneCenter(at: index)) <= hitTolerance ? index : nil
    }
}
