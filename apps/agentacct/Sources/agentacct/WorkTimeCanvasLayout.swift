import CoreGraphics
import Foundation

/// Pure geometry for a time canvas with cards above and below one shared axis.
/// Card width is reading space, never elapsed execution time. `timeBounds`
/// retains the recorded extent independently of the card's collision placement.
/// Packing covers every loaded dated record and positions are anchored to
/// absolute time, so panning only translates items; grouping changes require a
/// scale, width or record-set change.
struct WorkTimeCanvasLayout {
    struct Item: Identifiable, Equatable {
        let id: String
        let recordIDs: [String]
        let frame: CGRect
        let anchorX: Double
        let isAbove: Bool
        let timeBounds: WorkTimelineInterval
        /// The x of the clear column this item's stem is routed through when a
        /// nearer card sits over its anchor. `nil` is the common case: a plain
        /// vertical stem from the dot to the card edge.
        var stemDetourX: Double? = nil

        var count: Int { recordIDs.count }
        var isCluster: Bool { count > 1 }
    }

    let items: [Item]
    let window: WorkTimelineInterval
    let axisY: Double
    let cardWidth: Double
    let cardHeight: Double
    /// The reserved strip under the axis (tick labels live here), so the stem
    /// router can drop into it without crossing a time label.
    let lowerAxisClearance: Double
    let bandCountPerSide: Int
    let visibleRecordCount: Int
    let undatedRecordIDs: [String]

    private static let gap = 12.0
    private static let upperAxisClearance = 12.0
    private static let outerMargin = 8.0
    /// How far outside an occluding card a routed stem runs. Cards in a band
    /// are packed at least `gap` apart, so this column is always clear of the
    /// occluder's neighbours.
    private static let stemDetourInset = 8.0

    /// The smallest time window the canvas will show — and why it is an
    /// absolute number of seconds rather than a fraction of the task.
    ///
    /// The floor is THE AXIS'S OWN RESOLUTION. `WorkTimelineTimeAxis.tickSteps`
    /// begins at one second: no window can be labelled more finely than that,
    /// and an axis needs several labelled ticks to be a scale rather than a
    /// single stamp. At the canvas's 140pt minimum label spacing, a 5-second
    /// window in an 880pt plot draws 5 one-second ticks 176pt apart — the
    /// smallest window that still reads as a scale. A narrower window is not
    /// more zoom: it is the same one-second lattice with fewer labels in it,
    /// ending at an axis with none.
    ///
    /// A RELATIVE floor — a fixed fraction of `full.span`, so every task can be
    /// zoomed to the same proportion — was considered and rejected for exactly
    /// that reason. On the measured 0.30-second task a 1/20 floor would map a
    /// 0.015-second window onto an axis that can draw no tick inside it, while
    /// spreading three cards so far apart that at most one is on screen with
    /// nothing left to place it in time. Proportional zoom is not legible zoom,
    /// because the axis does not scale with the task.
    ///
    /// The consequence is that a task shorter than this floor cannot be
    /// narrowed AT ALL. That is a state every surface must NAME rather than
    /// offer a dead control for — see `canNarrow`.
    static let minimumVisibleSpan = 5.0

    /// Is there any window narrower than the whole recorded span?
    ///
    /// False for a task at or under the floor: `clampedWindow` then resolves
    /// every requested window — zoomed, dragged, incremented — back to the
    /// domain it was given, so no zoom, handle drag or adjustable step can
    /// change what is on screen. A surface that keeps offering those controls
    /// advertises something it cannot do; it must name the state instead
    /// (`display_vocabulary.TIMELINE_WINDOW_NOT_NARROWABLE`).
    static func canNarrow(_ full: WorkTimelineInterval, minimumSpan: Double = minimumVisibleSpan) -> Bool {
        let full = normalizedDomain(full)
        let minimum = minimumSpan.isFinite && minimumSpan > 0 ? minimumSpan : 1
        return full.upper - full.lower > minimum
    }

    /// Upper bound on the extra time the viewport may show beyond the recorded
    /// domain. Cards are centered on their timestamps, so the earliest and
    /// latest records need half a card of margin to be shown in full at the
    /// extreme positions — without it, their edge half is unreachable. The
    /// bound keeps a restored or foreign window finite; zoom-out itself is
    /// clamped to the recorded range, not to this margin.
    static let maximumEdgeRevealFraction = 0.5

    init(
        records: [WorkTimelineRecord],
        window: WorkTimelineInterval,
        width: Double,
        height: Double,
        textScale: Double = 1
    ) {
        let window = Self.normalizedDomain(window)
        let width = width.isFinite ? max(0, width) : 0
        let height = height.isFinite ? max(0, height) : 0
        let scale = Self.normalizedScale(textScale)
        let cardHeight = 80 * scale
        let cardWidth = min(200 * scale, width)
        let lowerAxisClearance = 44 * scale
        let axisY = min(height, max(0, (height - lowerAxisClearance + Self.upperAxisClearance) / 2))
        let availableHeight = max(0, min(
            axisY - Self.upperAxisClearance - Self.outerMargin,
            height - axisY - lowerAxisClearance - Self.outerMargin
        ))
        let bands = Int(min(2, floor((availableHeight + Self.gap + 0.000001) / (cardHeight + Self.gap))))

        self.window = window
        self.axisY = axisY
        self.cardWidth = cardWidth
        self.cardHeight = cardHeight
        self.lowerAxisClearance = lowerAxisClearance
        self.bandCountPerSide = bands

        // Placement packs every loaded dated record, not just the records
        // inside the window: grouping and band assignment then depend only on
        // the records and the current scale, so panning translates cards
        // without rearranging them. Rendering culls far-offscreen items.
        var seen = Set<String>()
        var undated: [String] = []
        var visibleCount = 0
        var candidates: [Group] = []
        for record in records where seen.insert(record.id).inserted {
            guard let start = WorkTimelineProjection.validTime(record.start) else {
                undated.append(record.id)
                continue
            }
            let end = max(start, WorkTimelineProjection.validTime(record.end) ?? start)
            // ONE membership rule for the canvas and the Activity heading that
            // counts above it (F5): a record is in the window when its CARD's
            // moment is, which is the moment this layout anchors it at. A span
            // that merely crosses the window keeps its line (`crossingSpans`)
            // without being counted as a record in view.
            if window.contains(record) { visibleCount += 1 }
            candidates.append(Group(recordIDs: [record.id], lower: start, upper: end, band: record.laneBand))
        }
        // The projection already orders records chronologically, so the sort
        // is a safety net for arbitrary callers. Verifying order costs O(n)
        // versus O(n log n) for a sort that reruns on every drag frame.
        var ordered = true
        for (previous, next) in zip(candidates, candidates.dropFirst()) where Self.precedes(next, previous) {
            ordered = false
            break
        }
        if !ordered {
            candidates.sort(by: Self.precedes)
        }
        self.visibleRecordCount = visibleCount
        self.undatedRecordIDs = undated.sorted()

        guard width > 0, bands > 0, !candidates.isEmpty else {
            self.items = []
            return
        }

        self.items = Self.pack(
            candidates, window: window, width: width, axisY: axisY,
            cardWidth: cardWidth, cardHeight: cardHeight,
            lowerAxisClearance: lowerAxisClearance, slots: bands * 2
        )
    }

    /// Cards intersecting the viewport plus one card of margin: the only items
    /// that render as interactive buttons, so the accessibility tree never
    /// contains an invisible offscreen card. Culling is presentation-only:
    /// placement and grouping never depend on it, so panning cannot rearrange
    /// or regroup cards at the viewport edges.
    ///
    /// This is a RENDERING margin, not a membership rule. Whether a record is
    /// IN a window is decided in one place for every surface —
    /// `WorkTimelineInterval.contains` (F5) — and that is what
    /// `visibleRecordCount` above, the Activity heading's count, the ordered
    /// list and the canvas's named-empty state all ask. The margin here only
    /// lets a card slide in during a pan instead of popping in, and the plot's
    /// half-card edge reveal (K18) only keeps a boundary card whole; neither
    /// adds a record to the window.
    func visibleCards(in width: Double) -> [Item] {
        guard width.isFinite else { return items }
        let margin = cardWidth + Self.gap
        return items.filter { $0.frame.maxX >= -margin && $0.frame.minX <= width + margin }
    }

    /// Does the clip actually show any of this card?
    ///
    /// `visibleCards` deliberately returns cards a full card-width OUTSIDE the
    /// plot so an edge card is drawn whole; one that lies entirely in that
    /// margin shows nothing at all, and must not be a keyboard stop (K130). A
    /// non-finite width means "not measured yet": assume on screen rather than
    /// silently dropping every card out of the key loop.
    static func isOnScreen(_ frame: CGRect, in width: Double) -> Bool {
        guard width.isFinite, width > 0, frame.minX.isFinite, frame.maxX.isFinite else { return true }
        return frame.maxX > 0 && frame.minX < width
    }

    /// Items whose recorded extent crosses the window while their card sits
    /// far offscreen. Their span lines still run through the viewport, marking
    /// activity that continues beyond an edge — without pinning a fake card.
    func crossingSpans(in width: Double) -> [Item] {
        guard width.isFinite else { return [] }
        let margin = cardWidth + Self.gap
        return items.filter {
            !($0.frame.maxX >= -margin && $0.frame.minX <= width + margin)
                && $0.timeBounds.upper >= window.lower && $0.timeBounds.lower <= window.upper
        }
    }

    /// The polyline a card's stem follows, from its dot on the axis to the
    /// card's axis-facing edge.
    ///
    /// Normally two points: straight up (or down) at the record's own time.
    /// When a nearer card sits over that column the stem is ROUTED around it
    /// — out through the clear column beside the occluder, along the gap
    /// between the two bands, and back to the anchor — so the whole leader
    /// stays visible and no reader has to guess which dot belongs to which
    /// card (K18). The dot never moves: only the leader bends, so every
    /// position stays time-true.
    func stemPoints(for item: Item) -> [CGPoint] {
        let edge = item.isAbove ? item.frame.maxY : item.frame.minY
        let straight = [CGPoint(x: item.anchorX, y: axisY), CGPoint(x: item.anchorX, y: edge)]
        guard let lane = item.stemDetourX, lane.isFinite else { return straight }
        // Into the axis clearance on this side — under the tick labels below,
        // inside the 12pt strip above — then along the inter-band gap.
        let clearance = item.isAbove ? Self.upperAxisClearance - 2 : lowerAxisClearance - 4
        let nearLaneY = item.isAbove ? axisY - clearance : axisY + clearance
        let farLaneY = item.isAbove ? edge + Self.gap / 2 : edge - Self.gap / 2
        // A band close enough that the two lanes would cross has no room to
        // route through; the straight stem is then the honest drawing.
        guard clearance > 0, abs(farLaneY - axisY) > abs(nearLaneY - axisY) else { return straight }
        return [
            CGPoint(x: item.anchorX, y: axisY),
            CGPoint(x: item.anchorX, y: nearLaneY),
            CGPoint(x: lane, y: nearLaneY),
            CGPoint(x: lane, y: farLaneY),
            CGPoint(x: item.anchorX, y: farLaneY),
            CGPoint(x: item.anchorX, y: edge),
        ]
    }

    /// Where the axis spine sits for a canvas of this height — the same value
    /// the instance computes, exposed so the lane gutter beside the plot can
    /// align its two captions to the split without re-deriving it.
    static func axisY(height: Double, textScale: Double = 1) -> Double {
        let height = height.isFinite ? max(0, height) : 0
        let lowerAxisClearance = 44 * normalizedScale(textScale)
        return min(height, max(0, (height - lowerAxisClearance + upperAxisClearance) / 2))
    }

    /// Enough height for one readable band per side plus the time-label strip.
    /// The view can grow its canvas at large text sizes instead of clipping.
    static func minimumHeight(textScale: Double = 1) -> Double {
        let scale = normalizedScale(textScale)
        return 160 * scale + 44 * scale + upperAxisClearance + 2 * outerMargin
    }

    /// Follow the newest actual timestamp, not the padded domain's empty tail.
    /// A small trailing margin keeps the latest mark clear of the right edge.
    static func latestWindow(
        within full: WorkTimelineInterval,
        latest: Double?,
        span: Double = 1800
    ) -> WorkTimelineInterval {
        let full = normalizedDomain(full)
        let span = span.isFinite && span > 0 ? span : 1800
        let duration = min(span, full.upper - full.lower)
        let end: Double
        if let latest = WorkTimelineProjection.validTime(latest),
           latest >= full.lower, latest <= full.upper {
            end = min(full.upper, latest + min(duration * 0.08, 60))
        } else {
            end = full.upper
        }
        return clampedWindow(.init(lower: end - duration, upper: end), to: full)
    }

    /// The pannable domain: the recorded range plus a small edge margin so a
    /// card centered on the first or last record is fully reachable. Extremes
    /// that would overflow fall back to the unexpanded domain.
    static func expandedDomain(_ full: WorkTimelineInterval, by reveal: Double) -> WorkTimelineInterval {
        let full = normalizedDomain(full)
        let reveal = reveal.isFinite && reveal > 0 ? reveal : 0
        let lower = full.lower - reveal, upper = full.upper + reveal
        guard lower.isFinite, upper.isFinite, lower < upper, (upper - lower).isFinite else { return full }
        return .init(lower: lower, upper: upper)
    }

    /// The window a plot of `width` must MAP so a card centered on the
    /// requested window's first and last record is drawn in full (K18).
    ///
    /// Cards are centered on their timestamps, so a window ending exactly on
    /// the last record cuts that card in half — and the last record is often
    /// the failed check or the handoff a reviewer came for. Buying half a card
    /// of time at each edge solves `span' = span / (1 - cardWidth / width)`;
    /// the reveal per side is capped at `maximumEdgeRevealFraction` of the
    /// original span so an unknown or pathological geometry stays bounded.
    /// Cards themselves are never clamped: only the mapped window grows, so
    /// every position stays time-true and panning still only translates.
    static func edgeRevealedWindow(
        _ window: WorkTimelineInterval,
        width: Double? = nil,
        cardWidth: Double? = nil
    ) -> WorkTimelineInterval {
        let window = normalizedDomain(window)
        let span = window.span
        guard span.isFinite, span > 0 else { return window }
        let cap = maximumEdgeRevealFraction * span
        var reveal = cap
        if let width, let cardWidth, width.isFinite, width > 0, cardWidth.isFinite, cardWidth > 0 {
            let ratio = cardWidth / width
            reveal = ratio < 1 ? min(ratio / 2 * span / (1 - ratio), cap) : cap
        }
        guard reveal.isFinite, reveal > 0 else { return window }
        let lower = window.lower - reveal, upper = window.upper + reveal
        guard lower.isFinite, upper.isFinite, lower < upper, (upper - lower).isFinite else { return window }
        return .init(lower: lower, upper: upper)
    }

    /// How far the viewport may pan beyond the recorded domain: half a card
    /// at the current scale, so a card centered on the first or last record
    /// fits in full at the extreme. Falls back to the capped fraction when
    /// the geometry is unknown. Half a card is at most half the viewport, so
    /// the result is always inside `maximumEdgeRevealFraction`.
    static func edgeRevealTime(window: WorkTimelineInterval, width: Double? = nil, cardWidth: Double? = nil) -> Double {
        let rawSpan = window.span
        let span = rawSpan.isFinite && rawSpan > 0 ? rawSpan : 1
        let fallback = maximumEdgeRevealFraction * span
        guard let width, let cardWidth,
              width.isFinite, width > 0, cardWidth.isFinite, cardWidth > 0 else { return fallback }
        return min(cardWidth / 2 / width, maximumEdgeRevealFraction) * span
    }

    /// Whether two windows are the same up to floating-point noise. Used to
    /// tell a clamped (no-op) zoom or pan from a real move, so saturated
    /// input is left to the page instead of being swallowed.
    ///
    /// TWO tolerances, because the span and the timestamps have different
    /// scales. A proportion of the span covers coarse rounding in a long window;
    /// a few representable steps of the BOUNDS covers the fact that these are
    /// absolute epoch seconds, spaced about 0.24 microseconds apart near 2026.
    /// A fully zoomed-out canvas re-derives its span from those numbers, so the
    /// clamp lands a step or two from where it started — with only the
    /// span-relative tolerance (1.4e-7 on a 138-second window, below one step)
    /// every further zoom-out was reported as a real move, consumed the key and
    /// shifted the window by 240 nanoseconds. That is a gesture that looks
    /// handled and is not (C13).
    static func sameWindow(_ lhs: WorkTimelineInterval, _ rhs: WorkTimelineInterval) -> Bool {
        let spans = max(abs(lhs.upper - lhs.lower), abs(rhs.upper - rhs.lower), 1) * 1e-9
        let steps = [lhs.lower, lhs.upper, rhs.lower, rhs.upper]
            .filter { $0.isFinite }.map { $0.ulp }.max() ?? 0
        let tolerance = max(spans, 4 * steps)
        return abs(lhs.lower - rhs.lower) <= tolerance && abs(lhs.upper - rhs.upper) <= tolerance
    }

    /// Constrain a requested window to the domain, preserving its span where
    /// possible. Spans clamp to at least `minimumVisibleSpan` (unless the
    /// domain itself is smaller). Invalid domains become [0, 1]; a finite
    /// point domain expands by one second when representable. Invalid windows
    /// show the full domain.
    static func clampedWindow(
        _ window: WorkTimelineInterval,
        to full: WorkTimelineInterval,
        minimumSpan: Double = minimumVisibleSpan
    ) -> WorkTimelineInterval {
        let full = normalizedDomain(full)
        guard window.lower.isFinite, window.upper.isFinite else { return full }
        let lower = min(window.lower, window.upper)
        let upper = max(window.lower, window.upper)
        let fullSpan = full.upper - full.lower
        let minimum = minimumSpan.isFinite && minimumSpan > 0 ? minimumSpan : 1
        let span = min(fullSpan, max(minimum, upper - lower))
        let start = min(max(lower, full.lower), full.upper - span)
        return WorkTimelineInterval(lower: start, upper: min(full.upper, start + span))
    }

    /// Positive seconds move toward later records. Saturation at the domain
    /// boundaries avoids overflow even for an extreme finite input delta.
    static func pannedWindow(
        _ window: WorkTimelineInterval,
        by delta: Double,
        within full: WorkTimelineInterval
    ) -> WorkTimelineInterval {
        let full = normalizedDomain(full)
        let current = clampedWindow(window, to: full)
        guard delta.isFinite else { return current }
        let span = current.upper - current.lower
        if delta >= full.upper - current.upper {
            return .init(lower: full.upper - span, upper: full.upper)
        }
        if delta <= full.lower - current.lower {
            return .init(lower: full.lower, upper: full.lower + span)
        }
        return .init(lower: current.lower + delta, upper: current.upper + delta)
    }

    /// A factor greater than one zooms in. Keep the time at `anchorFraction`
    /// under the same viewport position unless a domain edge requires clamping.
    /// The span clamps to the recorded range (`full`); the position clamps to
    /// `positionDomain`, which may include the edge margin. That keeps the
    /// maximum zoom-out at the recorded history while an overscrolled window
    /// zooms without snapping back.
    static func zoomedWindow(
        _ window: WorkTimelineInterval,
        factor: Double,
        anchorFraction: Double,
        within full: WorkTimelineInterval,
        positionDomain: WorkTimelineInterval? = nil,
        minimumSpan: Double = minimumVisibleSpan
    ) -> WorkTimelineInterval {
        let full = normalizedDomain(full)
        let positions = normalizedDomain(positionDomain ?? full)
        let current = clampedWindow(window, to: positions, minimumSpan: minimumSpan)
        guard factor.isFinite, factor > 0 else { return current }
        let fraction = anchorFraction.isFinite ? min(max(anchorFraction, 0), 1) : 0.5
        let minimum = minimumSpan.isFinite && minimumSpan > 0 ? minimumSpan : 1
        let oldSpan = current.upper - current.lower
        let newSpan = min(full.upper - full.lower, max(minimum, oldSpan / factor))
        let anchor = current.lower + oldSpan * fraction
        let lower = anchor - newSpan * fraction
        return clampedWindow(.init(lower: lower, upper: lower + newSpan), to: positions, minimumSpan: minimumSpan)
    }

    /// Zoom keeping an absolute anchor time fixed. Surfaces whose pointer
    /// position is expressed against the pannable domain (the overview)
    /// convert to the window-relative fraction here. An anchor outside the
    /// window clamps to the nearest edge.
    static func zoomedWindow(
        _ window: WorkTimelineInterval,
        factor: Double,
        anchorTime: Double,
        within full: WorkTimelineInterval,
        positionDomain: WorkTimelineInterval? = nil,
        minimumSpan: Double = minimumVisibleSpan
    ) -> WorkTimelineInterval {
        let positions = normalizedDomain(positionDomain ?? full)
        let current = clampedWindow(window, to: positions, minimumSpan: minimumSpan)
        let span = current.upper - current.lower
        let fraction = anchorTime.isFinite && span > 0 ? (anchorTime - current.lower) / span : 0.5
        return zoomedWindow(current, factor: factor, anchorFraction: fraction,
                            within: full, positionDomain: positions, minimumSpan: minimumSpan)
    }

    private struct Group {
        var recordIDs: [String]
        var lower: Double
        var upper: Double
        /// The reducer's lane for every member. Packing may merge two groups
        /// only within one lane, so a card can never change what its side of
        /// the axis says about its members.
        var band: WorkTimelineLaneBand
    }

    /// Chronological packing order: by start time, then by first member ID so
    /// ties stay deterministic. Shared by the ordering fast-path and the sort
    /// fallback so the two can never drift apart.
    private static func precedes(_ lhs: Group, _ rhs: Group) -> Bool {
        lhs.lower == rhs.lower ? lhs.recordIDs[0] < rhs.recordIDs[0] : lhs.lower < rhs.lower
    }

    private struct PlacedGroup {
        var group: Group
        let frame: CGRect
        let anchorX: Double
        let isAbove: Bool
        var stemDetourX: Double?
    }

    private static func pack(
        _ groups: [Group], window: WorkTimelineInterval, width: Double,
        axisY: Double, cardWidth: Double, cardHeight: Double,
        lowerAxisClearance: Double, slots: Int
    ) -> [Item] {
        var ends = Array(repeating: -Double.infinity, count: slots)
        var lastPlaced = Array(repeating: -1, count: slots)
        var placed: [PlacedGroup] = []
        /// The horizontal extent of the nearer cards that sit over `anchorX`,
        /// or nil when the column down to the axis is clear.
        ///
        /// A farther band's stem crosses every nearer band on its side, so a
        /// nearer card drawn over that stem makes the two cards' dots
        /// ambiguous (K18). Cards that share an anchor are not occluders:
        /// their stems coincide and the pair reads as one moment. Cards within
        /// a band are placed left to right and anchors only grow, so the one
        /// card that can contain this anchor is each nearer band's last: the
        /// test is O(bands) and, like the rest of packing, depends on no
        /// window position.
        func occluders(slot: Int, anchorX: Double) -> (minX: Double, maxX: Double)? {
            var minX = Double.infinity
            var maxX = -Double.infinity
            var nearer = slot - 2
            while nearer >= 0 {
                let index = lastPlaced[nearer]
                if index >= 0 {
                    let card = placed[index]
                    if card.anchorX != anchorX, card.frame.minX <= anchorX, anchorX <= card.frame.maxX {
                        minX = min(minX, card.frame.minX)
                        maxX = max(maxX, card.frame.maxX)
                    }
                }
                nearer -= 2
            }
            return minX <= maxX ? (minX, maxX) : nil
        }
        for group in groups {
            let anchorX = anchor(group.lower, window: window, width: width)
            // Cards are not clamped to the viewport: a partially offscreen card
            // keeps its time-true position and slides under the edge while
            // panning, instead of jumping to or away from the boundary.
            let x = anchorX - cardWidth / 2
            // THE LANE DECIDES THE SIDE. Even slots are the work lane above
            // the axis, odd slots the check-evidence lane below it, so
            // vertical position states the reducer's `lane` and nothing else.
            // Collision packing then chooses only a BAND within that lane.
            let above = group.band.isAbove
            let laneSlots = ends.indices.filter { $0.isMultiple(of: 2) == above }
            // A slot whose stem column is clear is always preferred. When none
            // is, the record still gets its OWN named card in the first free
            // band and its stem is routed around the occluder (see
            // `stemPoints`) — refusing the band instead merged two named
            // checks into an unlabelled group at the default viewport, which
            // is exactly the evidence a reviewer opened the timeline for.
            var slot = -1
            var firstFree = -1
            for candidate in laneSlots where x >= ends[candidate] + gap {
                if firstFree < 0 { firstFree = candidate }
                if occluders(slot: candidate, anchorX: anchorX) == nil {
                    slot = candidate
                    break
                }
            }
            if slot < 0 { slot = firstFree }
            guard slot >= 0 else {
                // Every band IN THIS LANE is occupied at this x: genuinely
                // dense. Extend the nearest last card's membership without
                // moving earlier cards or repacking them. Only same-lane cards
                // are candidates, so a dense group never mixes a work step
                // with a check.
                var target = -1
                for candidate in laneSlots.map({ lastPlaced[$0] }) where candidate >= 0 {
                    if target < 0 || anchorX - placed[candidate].anchorX < anchorX - placed[target].anchorX {
                        target = candidate
                    }
                }
                guard target >= 0 else {
                    // Unreachable (an empty first band in the lane always
                    // accepts). Placing is still the right answer: no record
                    // may silently vanish, and it stays on its own side.
                    let offset = above ? upperAxisClearance : lowerAxisClearance
                    let frame = CGRect(x: x, y: above ? axisY - offset - cardHeight : axisY + offset,
                                       width: cardWidth, height: cardHeight)
                    placed.append(PlacedGroup(group: group, frame: frame, anchorX: anchorX,
                                              isAbove: above, stemDetourX: nil))
                    let home = above ? 0 : 1
                    lastPlaced[home] = placed.count - 1
                    ends[home] = frame.maxX
                    continue
                }
                placed[target].group.recordIDs.append(contentsOf: group.recordIDs)
                placed[target].group.upper = max(placed[target].group.upper, group.upper)
                continue
            }
            let band = Double(slot / 2)
            let offset = (above ? upperAxisClearance : lowerAxisClearance) + band * (cardHeight + gap)
            let y = above ? axisY - offset - cardHeight : axisY + offset
            let frame = CGRect(x: x, y: y, width: cardWidth, height: cardHeight)
            // Route through whichever side of the occluding run is nearer, so
            // the leader stays as short as the geometry allows.
            let detour = occluders(slot: slot, anchorX: anchorX).map { covered in
                anchorX - covered.minX <= covered.maxX - anchorX
                    ? covered.minX - stemDetourInset : covered.maxX + stemDetourInset
            }
            placed.append(PlacedGroup(group: group, frame: frame, anchorX: anchorX,
                                      isAbove: above, stemDetourX: detour))
            lastPlaced[slot] = placed.count - 1
            ends[slot] = frame.maxX
        }
        return placed.map {
            Item(
                id: stableID($0.group.recordIDs), recordIDs: $0.group.recordIDs,
                frame: $0.frame, anchorX: $0.anchorX, isAbove: $0.isAbove,
                timeBounds: .init(lower: $0.group.lower, upper: $0.group.upper),
                stemDetourX: $0.stemDetourX
            )
        }
    }

    /// The unclipped linear time-to-position map. Offscreen times produce
    /// positions outside `0...width`; that is what keeps relative placement
    /// independent of the window's position within the full domain.
    private static func anchor(_ time: Double, window: WorkTimelineInterval, width: Double) -> Double {
        (time - window.lower) / (window.upper - window.lower) * width
    }

    private static func stableID(_ members: [String]) -> String {
        guard members.count > 1 else { return "record:\(members[0])" }
        // Stable presentation identity, not a claim that separate source events
        // are identical. Length-prefix each member to avoid delimiter ambiguity.
        var hash: UInt64 = 14_695_981_039_346_656_037
        for id in members.sorted() {
            for byte in "\(id.utf8.count):\(id)".utf8 {
                hash = (hash ^ UInt64(byte)) &* 1_099_511_628_211
            }
        }
        return "cluster:\(members.count):\(String(hash, radix: 16))"
    }

    private static func normalizedDomain(_ interval: WorkTimelineInterval) -> WorkTimelineInterval {
        guard interval.lower.isFinite, interval.upper.isFinite else { return .init(lower: 0, upper: 1) }
        let lower = min(interval.lower, interval.upper), upper = max(interval.lower, interval.upper)
        guard (upper - lower).isFinite else { return .init(lower: 0, upper: 1) }
        if upper > lower { return .init(lower: lower, upper: upper) }
        if lower + 1 > lower, (lower + 1).isFinite { return .init(lower: lower, upper: lower + 1) }
        return .init(lower: 0, upper: 1)
    }

    private static func normalizedScale(_ scale: Double) -> Double {
        scale.isFinite && scale > 0 ? min(scale, 8) : 1
    }
}
