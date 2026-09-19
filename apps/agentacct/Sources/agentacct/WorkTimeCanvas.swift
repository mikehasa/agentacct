import AppKit
import SwiftUI

/// A viewport into recorded time. Card positions describe timestamps; neither
/// their placement nor their connecting stems claims an execution dependency.
/// Selection is presented by the parent below the canvas, keeping this
/// surface's geometry stable.
struct WorkTimeCanvas: View {
    let records: [WorkTimelineRecord]
    let full: WorkTimelineInterval
    let window: WorkTimelineInterval
    let selectedRecord: WorkTimelineRecord?
    private var selectedID: String? { selectedRecord?.id }
    let onWindow: (WorkTimelineInterval) -> Void
    let onSelect: (WorkTimelineRecord) -> Void
    let onCluster: ([WorkTimelineRecord], WorkTimelineInterval) -> Void
    let onDismiss: () -> Void
    let onHold: () -> Void
    var focusRecordID: String? = nil
    var focusRequest = 0
    /// Keyboard focus moved onto a card. The page scrolls the canvas into
    /// view, so a Tab stop can never sit behind the toolbar or below the fold
    /// (K115).
    var onFocusEnter: (() -> Void)? = nil
    var compact = false
    /// False when the whole task (unfiltered) has a single lane: every card's
    /// lane caption would then repeat the same text (C114).
    var showsLaneCaptions = true
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @ScaledMetric(relativeTo: .body) private var systemScale: CGFloat = 1
    @State private var lastTriggerID: String?
    @State private var hoveredID: String?
    @State private var dragBaseWindow: WorkTimelineInterval?
    @State private var measuredWidth: Double = 0
    @FocusState private var focusedItem: String?

    private var scale: Double {
        Double(WorkTypeScale.resolved(base: 1, systemScaled: systemScale, dynamicTypeSize: dynamicTypeSize))
    }
    /// A lane caption earns its line only when it tells cards apart: with one
    /// lane in the task (C114) — or many lanes that all carry the same caption,
    /// which is how every card ended up repeating the task's own title (K18) —
    /// the caption is noise. Computed from the loaded records, not the visible
    /// ones, so panning never adds or drops a line.
    private var showsCaptions: Bool {
        showsLaneCaptions && Set(records.map(\.laneTitle)).count > 1
    }
    private var canvasHeight: Double { max(compact ? 280 : 420, WorkTimeCanvasLayout.minimumHeight(textScale: scale)) }

    /// The reducer's words for a lane, taken from the records in it. Nothing
    /// is invented: a payload that carries no `lane_label` draws no caption
    /// (and the gutter then takes no width at all).
    private func laneCaption(_ band: WorkTimelineLaneBand) -> String? {
        // The work side holds more than one reducer lane (`primary` and
        // `supporting`). Naming it after whichever record happened to be first
        // would mislabel the other, so every label present is listed, in the
        // order the records arrive.
        var seen = Set<String>()
        let labels = records.compactMap { record -> String? in
            guard record.laneBand == band, let label = record.laneLabel,
                  seen.insert(label).inserted else { return nil }
            return label
        }
        return labels.isEmpty ? nil : labels.joined(separator: " · ")
    }
    private var laneCaptions: [(band: WorkTimelineLaneBand, text: String)] {
        WorkTimelineLaneBand.allCases.compactMap { band in
            laneCaption(band).map { (band: band, text: $0) }
        }
    }
    private var laneGutterWidth: Double { laneCaptions.isEmpty ? 0 : 72 * scale }

    /// PERMANENT lane labels beside the axis. The canvas's vertical axis
    /// carries the reducer's two-lane grammar (work above, check evidence
    /// below); unlabelled, that split was a shape a reader had to guess at.
    /// The captions sit outside the plot, so they can never cover a card.
    private var laneGutter: some View {
        let height = canvasHeight
        let axis = WorkTimeCanvasLayout.axisY(height: height, textScale: scale)
        return ZStack(alignment: .topLeading) {
            Color.clear
            ForEach(laneCaptions, id: \.band) { lane in
                Text(lane.text)
                    .workFont(.caption).foregroundStyle(Theme.muted)
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                    .frame(width: laneGutterWidth,
                           height: max(lane.band.isAbove ? axis - 6 : height - axis - 6, 0),
                           alignment: lane.band.isAbove ? .bottomLeading : .topLeading)
                    .offset(y: lane.band.isAbove ? 0 : axis + 6)
            }
        }
        .frame(width: laneGutterWidth, height: height)
        .accessibilityHidden(true)  // every card already speaks its own lane
    }

    var body: some View {
        VStack(spacing: 8) {
            if dynamicTypeSize.isAccessibilitySize {
                WorkTimeWindowScroller(records: records, full: full, window: window, domain: scrollerDomain, onWindow: onWindow)
            }
            HStack(alignment: .top, spacing: laneGutterWidth > 0 ? 6 : 0) {
            if laneGutterWidth > 0 { laneGutter }
            GeometryReader { geometry in
                let width = Double(geometry.size.width)
                let indexedRecords = Dictionary(records.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
                // The plot maps slightly more time than the requested window:
                // half a card at each edge, so a card centered on the first or
                // last record in view is drawn in full instead of being sliced
                // by the plot edge (K18). Every window the page computes —
                // initial, latest, focused, zoomed-out, restored — inherits it.
                let plotWindow = WorkTimeCanvasLayout.edgeRevealedWindow(window,
                    width: width, cardWidth: min(200 * scale, width))
                let layout = WorkTimeCanvasLayout(records: records, window: plotWindow,
                    width: width, height: canvasHeight, textScale: scale)
                let visibleCards = layout.visibleCards(in: width)
                let crossingSpans = layout.crossingSpans(in: width)
                // A small margin beyond the recorded range lets the extreme
                // positions show a card centered on the first/last record in
                // full; placement itself stays anchored to absolute time.
                let edgeDomain = WorkTimeCanvasLayout.expandedDomain(full,
                    by: WorkTimeCanvasLayout.edgeRevealTime(window: window, width: width, cardWidth: layout.cardWidth))
                let tickMarkings = WorkTimelineTimeAxis.ticks(in: plotWindow, width: width, minimumSpacing: 140 * scale)
                input(items: visibleCards, window: window, plotSpan: plotWindow.span, full: full, domain: edgeDomain, width: width) {
                    ZStack(alignment: .topLeading) {
                    Theme.well
                    drawing(layout: layout, items: visibleCards, crossing: crossingSpans, ticks: tickMarkings.times, width: width, indexedRecords: indexedRecords).allowsHitTesting(false)
                    axisLabels(ticks: tickMarkings, plotWindow: plotWindow, width: width, axisY: layout.axisY).allowsHitTesting(false)
                    ForEach(visibleCards) { item in
                        // `visibleCards` keeps a card's width of margin beyond
                        // each edge so a card centred on the first or last
                        // record in view is DRAWN in full (K18). A card that
                        // falls entirely in that margin is clipped away
                        // completely — yet it stayed in the key loop, so Tab
                        // landed on a card at x=254, behind the 20…380 sidebar,
                        // with nothing on screen to show for it (K130). Focus
                        // follows what the clip actually shows.
                        itemButton(item, indexedRecords: indexedRecords,
                                   onScreen: WorkTimeCanvasLayout.isOnScreen(item.frame, in: width))
                            .frame(width: item.frame.width, height: item.frame.height)
                            .position(x: item.frame.midX, y: item.frame.midY)
                    }
                    // Named absence whenever the window holds no record, even
                    // when a crossing span line runs through it (C12).
                    //
                    // The test is the SHARED window rule (F5), not the drawn
                    // frames: the Activity heading above counts by that rule
                    // and prints this window's own bounds beside the count, so
                    // "0 of 20 records" and this sentence must agree. The plot
                    // maps half a card of extra time at each edge so a record
                    // AT the boundary is drawn in full (K18) — a record just
                    // OUTSIDE can therefore still show a sliver of card, and
                    // that margin must not silence the named absence.
                    if !records.contains(where: {
                        WorkTimelineProjection.validTime($0.start) != nil && window.contains($0)
                    }) {
                        Text("No activity in this time window")
                            .workFont(.body).foregroundStyle(Theme.muted)
                            .frame(maxWidth: .infinity).offset(y: layout.axisY - 64 * scale)
                            .allowsHitTesting(false)
                    }
                    }
                }.clipped()
                .task(id: FocusTarget(request: focusRequest, recordID: focusRecordID)) {
                    guard focusRecordID != nil else { return }
                    // Selection dismissal restores its surface's responder
                    // first. Restore the event after that transition completes.
                    focusedItem = nil
                    try? await Task.sleep(for: .milliseconds(200))
                    guard !Task.isCancelled else { return }
                    focus(in: layout)
                }
            }
            .frame(height: canvasHeight)
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Activity timeline")
            .accessibilityIdentifier("work.timeline.canvas")
            }
            if !dynamicTypeSize.isAccessibilitySize {
                WorkTimeWindowScroller(records: records, full: full, window: window, domain: scrollerDomain, onWindow: onWindow)
            }
        }
        .background(Theme.well, in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Theme.hairline))
        .background(GeometryReader { proxy in
            Color.clear.preference(key: WorkTimeCanvasWidthKey.self, value: proxy.size.width)
        })
        .onPreferenceChange(WorkTimeCanvasWidthKey.self) { width in
            guard width.isFinite, width > 0, abs(width - measuredWidth) > 0.5 else { return }
            measuredWidth = width
        }
    }

    /// The overview maps the same pannable domain as the canvas — including
    /// the edge margin — so the pill reflects the true scrollable range and
    /// neither surface clamps the other's windows.
    private var scrollerDomain: WorkTimelineInterval {
        let width = measuredWidth > 0 ? measuredWidth : nil
        let card = width.map { min(200 * scale, $0) }
        return WorkTimeCanvasLayout.expandedDomain(full,
            by: WorkTimeCanvasLayout.edgeRevealTime(window: window, width: width, cardWidth: card))
    }

    /// `plotSpan` is the time the plot actually maps across `width` (the
    /// window plus its edge reveal), so a drag moves the scene exactly as far
    /// as the pointer travelled.
    private func input<Content: View>(items: [WorkTimeCanvasLayout.Item], window: WorkTimelineInterval, plotSpan: Double, full: WorkTimelineInterval, domain: WorkTimelineInterval, width: Double, @ViewBuilder content: () -> Content) -> some View {
        let secondsPerPixel = (plotSpan.isFinite && plotSpan > 0 ? plotSpan : window.span) / max(width, 1)
        return WorkTimeCanvasInput(interactiveRegions: items.map(\.frame),
            onPan: { pixels in
                // Keyboard and accessibility increments resolve against the
                // latest committed window.
                onWindow(WorkTimeCanvasLayout.pannedWindow(window, by: -pixels * secondsPerPixel, within: domain))
            },
            onDrag: { cumulativePixels in
                // Mouse drags carry the total translation from gesture start
                // and resolve against the window captured when the drag began,
                // so a slow frame can drop a request but never drop motion.
                if dragBaseWindow == nil { dragBaseWindow = window }
                let base = dragBaseWindow ?? window
                onWindow(WorkTimeCanvasLayout.pannedWindow(base,
                    by: -cumulativePixels * secondsPerPixel, within: domain))
            },
            onZoom: { factor, anchor in
                onWindow(WorkTimeCanvasLayout.zoomedWindow(window, factor: factor, anchorFraction: anchor,
                    within: full, positionDomain: domain))
            },
            canZoom: { factor, anchor in
                // A clamped zoom leaves the wheel event to the page.
                !WorkTimeCanvasLayout.sameWindow(WorkTimeCanvasLayout.zoomedWindow(window, factor: factor,
                    anchorFraction: anchor, within: full, positionDomain: domain),
                    WorkTimeCanvasLayout.clampedWindow(window, to: domain))
            },
            canPan: { pixels in
                !WorkTimeCanvasLayout.sameWindow(WorkTimeCanvasLayout.pannedWindow(window,
                    by: -pixels * secondsPerPixel, within: domain),
                    WorkTimeCanvasLayout.clampedWindow(window, to: domain))
            },
            canReachEdge: { latest in
                !WorkTimeCanvasLayout.sameWindow(WorkTimeCanvasLayout.pannedWindow(window,
                    by: latest ? domain.upper - window.upper : domain.lower - window.lower, within: domain),
                    WorkTimeCanvasLayout.clampedWindow(window, to: domain))
            },
            onEdge: { latest in
                onWindow(WorkTimeCanvasLayout.pannedWindow(window,
                    by: latest ? domain.upper - window.upper : domain.lower - window.lower, within: domain))
            },
            onDismiss: onDismiss,
            onBackgroundClick: onDismiss,
            onGestureBegan: { dragBaseWindow = nil },
            onGestureEnded: { dragBaseWindow = nil },
            onGestureCancelled: { dragBaseWindow = nil },
            accessibilityValue: "\(WorkTimelineTimeAxis.label(window.lower, range: window)) to \(WorkTimelineTimeAxis.label(window.upper, range: window))",
            // A canvas whose whole recorded span is shorter than the window
            // floor can neither be narrowed nor panned; reading out the pinch
            // and drag gestures there would announce a control that is not one.
            accessibilityHelp: WorkTimeCanvasLayout.canNarrow(full)
                ? WorkTimeCanvasHelp.navigable
                : WorkTimeWindowScroller.notNarrowableDetail,
            content: content).renderingSurface
    }

    /// Every mark's screen position derives from its absolute time, so panning
    /// translates the whole scene together. The clipped bounds trim partially
    /// visible cards and spans instead of re-placing them. Spans that cross
    /// the window keep their line running through it even when the record's
    /// card itself is far offscreen — the line is the honest affordance, and
    /// panning or zooming out reaches the card.
    private func drawing(layout: WorkTimeCanvasLayout, items: [WorkTimeCanvasLayout.Item], crossing: [WorkTimeCanvasLayout.Item], ticks: [Double], width: Double, indexedRecords: [String: WorkTimelineRecord]) -> some View {
        Canvas { context, size in
            // The plot's own window (the requested one plus its edge reveal).
            let plot = layout.window
            func xPosition(_ time: Double) -> Double { (time - plot.lower) / plot.span * width }
            var spine = Path()
            spine.move(to: CGPoint(x: 0, y: layout.axisY))
            spine.addLine(to: CGPoint(x: width, y: layout.axisY))
            context.stroke(spine, with: .color(Theme.chartNeutral), lineWidth: 1)
            for tick in ticks {
                let x = xPosition(tick)
                var line = Path()
                line.move(to: CGPoint(x: x, y: layout.axisY - 4))
                line.addLine(to: CGPoint(x: x, y: layout.axisY + 4))
                context.stroke(line, with: .color(Theme.muted), lineWidth: 1)
            }
            // A span crossing the window draws its line even when the card is
            // offscreen; every member of a crossing cluster contributes its
            // own recorded span. Non-crossing spans draw beside their card.
            var drawnSpans = Set<CrossingSpanKey>()
            for item in crossing {
                let y = layout.axisY + (item.isAbove ? -5.0 : 5.0)
                for id in item.recordIDs {
                    guard let record = indexedRecords[id], record.isDuration,
                          let start = record.start, let end = record.end else { continue }
                    let key = CrossingSpanKey(start: start, end: end, failure: record.isDanger)
                    guard drawnSpans.insert(key).inserted else { continue }
                    let tint: Color = record.isDanger ? Theme.coral : Theme.chartNeutral
                    var span = Path()
                    span.move(to: CGPoint(x: xPosition(start), y: y))
                    span.addLine(to: CGPoint(x: xPosition(end), y: y))
                    context.stroke(span, with: .color(tint), lineWidth: 3)
                }
            }
            // SUPERSESSION, drawn as the link it is: from a failed run's dot to
            // the dot of the run that replaced it. Without this the canvas's
            // only statement about a recovered check was silence — the tile
            // beside it said one run failed and the plot showed nothing.
            // Both endpoints and the direction come from the payload
            // (`superseded_by_event_id`); Swift infers no relationship.
            var anchorsByEvent: [String: Double] = [:]
            for item in layout.items {
                for id in item.recordIDs {
                    guard let event = indexedRecords[id]?.eventID else { continue }
                    anchorsByEvent[event] = item.anchorX
                }
            }
            for item in layout.items {
                for id in item.recordIDs {
                    guard let record = indexedRecords[id], let successor = record.resolvedByEventID,
                          let targetX = anchorsByEvent[successor] else { continue }
                    let fromX = item.anchorX, toX = targetX
                    guard max(fromX, toX) >= -width, min(fromX, toX) <= width * 2 else { continue }
                    // A shallow arc below the axis spine, so the link cannot be
                    // read as one more recorded span sitting on it.
                    let lift = min(max(abs(toX - fromX) * 0.35, 8), 22)
                    let y = layout.axisY
                    var link = Path()
                    link.move(to: CGPoint(x: fromX, y: y))
                    link.addQuadCurve(to: CGPoint(x: toX, y: y),
                                      control: CGPoint(x: (fromX + toX) / 2, y: y + lift * 2))
                    context.stroke(link, with: .color(Theme.coral),
                                   style: StrokeStyle(lineWidth: 1.5, dash: [3, 3]))
                    // The arrowhead names the direction: the failure is the
                    // tail, the run that replaced it is the head.
                    let direction: Double = toX >= fromX ? 1 : -1
                    var head = Path()
                    head.move(to: CGPoint(x: toX, y: y))
                    head.addLine(to: CGPoint(x: toX - direction * 5, y: y + 4))
                    head.addLine(to: CGPoint(x: toX - direction * 5, y: y - 1))
                    head.closeSubpath()
                    context.fill(head, with: .color(Theme.coral))
                }
            }
            for item in items {
                let selected = selectedID.map(item.recordIDs.contains) ?? false
                let emphasized = selected || hoveredID == item.id || focusedItem == item.id
                let danger = item.recordIDs.contains { id in indexedRecords[id]?.isDanger == true }
                let members = item.recordIDs.compactMap { indexedRecords[$0] }
                // A data mark never wears the interactive accent (K04). The
                // mark tone is the record's own: coral for a failure (current
                // or resolved), the neutral chart tone otherwise.
                let markTint = members.first(where: \.isCurrentFailure)?.markTint
                    ?? members.first(where: \.isResolvedFailure)?.markTint
                    ?? members.first(where: \.isDanger)?.markTint
                    ?? Theme.chartNeutral
                // Open only when EVERY member is a resolved failure, so a group
                // holding one live failure can never read as answered.
                let markHollow = !members.isEmpty && members.allSatisfy(\.markIsHollow)
                // The leader from the dot to the card: straight up or down at
                // the record's own time, or routed around an occluding nearer
                // card so it is never lost behind one (K18).
                let route = layout.stemPoints(for: item)
                var stem = Path()
                stem.move(to: route[0])
                for point in route.dropFirst() { stem.addLine(to: point) }
                // Resting geometry is neutral; the tint appears on emphasis.
                context.stroke(stem, with: .color(emphasized || danger ? markTint : Theme.rule),
                    lineWidth: emphasized ? 2 : 1)
                if !item.isCluster, let id = item.recordIDs.first, let record = indexedRecords[id], record.isDuration {
                    let x1 = xPosition(record.start!)
                    let x2 = xPosition(record.end!)
                    let y = layout.axisY + (item.isAbove ? -5.0 : 5.0)
                    var span = Path()
                    span.move(to: CGPoint(x: x1, y: y)); span.addLine(to: CGPoint(x: x2, y: y))
                    context.stroke(span, with: .color(emphasized || danger ? markTint : Theme.chartNeutral), lineWidth: 3)
                }
                let radius = item.isCluster ? 5.0 : 3.5
                let dot = Path(ellipseIn: CGRect(x: item.anchorX - radius, y: layout.axisY - radius,
                    width: radius * 2, height: radius * 2))
                if markHollow {
                    context.stroke(dot, with: .color(markTint), lineWidth: 1.5)
                } else {
                    context.fill(dot, with: .color(markTint))
                }
                // WHERE the reducer says a terminal status was reported
                // (completed, blocked, handed off): a short tick across the
                // span's end, so a handoff is not read at the section's start.
                // The time and the terminal decision both come from the
                // payload (`terminal_status_at`).
                if !item.isCluster, let id = item.recordIDs.first,
                   let time = indexedRecords[id]?.terminalMarkTime {
                    let x = xPosition(time)
                    let y = layout.axisY + (item.isAbove ? -5.0 : 5.0)
                    var mark = Path()
                    mark.move(to: CGPoint(x: x, y: y - 4))
                    mark.addLine(to: CGPoint(x: x, y: y + 4))
                    context.stroke(mark, with: .color(emphasized || danger ? markTint : Theme.chartNeutral), lineWidth: 2)
                }
            }
        }
        .accessibilityHidden(true)
    }

    /// Labels sit on their tick marks and slide with them. A label's text is
    /// fixed by its absolute time and the current step, so it never renumbers
    /// in place while the window moves.
    private func axisLabels(ticks: (step: Double, times: [Double]), plotWindow: WorkTimelineInterval, width: Double, axisY: Double) -> some View {
        ZStack(alignment: .topLeading) {
            ForEach(ticks.times, id: \.self) { tick in
                let text = WorkTimelineTimeAxis.tickLabel(tick, step: ticks.step, range: plotWindow)
                // A label centered on a tick near the plot edge would lose
                // half its characters to the clip ("5:45 PI"). Keeping its
                // centre half a label inside shows the whole time; the tick
                // itself still marks the exact position (K18).
                let labelSize = max(12, WorkFontRole.dataSmall.metrics.size * scale)
                let half = Double(workDataTextWidth(text, size: labelSize)) / 2 + 1
                Text(text)
                    .workFont(.dataSmall).foregroundStyle(Theme.muted)
                    .fixedSize().background(Theme.well)
                    .position(x: min(max((tick - plotWindow.lower) / plotWindow.span * width, half), max(width - half, half)),
                              y: axisY + 12 + 7 * scale)
            }
        }
        .frame(width: width)
        .accessibilityHidden(true)
    }

    private func itemButton(_ item: WorkTimeCanvasLayout.Item, indexedRecords: [String: WorkTimelineRecord], onScreen: Bool = true) -> some View {
        let members = item.recordIDs.compactMap { indexedRecords[$0] }
        let selected = selectedID.map(item.recordIDs.contains) ?? false
        return Button { activate(item, members: members) } label: {
            VStack(alignment: .leading, spacing: 2) {
                if item.isCluster {
                    clusterBody(members)
                } else if let record = members.first, record.isBeat {
                    // A progress note, drawn as narration under the SECTION
                    // that recorded it: its section's title as the eyebrow,
                    // its prose at caption weight in muted ink, and no result
                    // glyph at all — a beat reports no outcome, so it wears
                    // neither a step's square nor a check's circle (K05).
                    if let section = WorkTimelineProjection.nonempty(record.sectionTitle) {
                        Text(section).workFont(.caption).foregroundStyle(Theme.muted).lineLimit(1)
                    }
                    Text(record.displayTitle).workFont(.caption).foregroundStyle(Theme.muted).lineLimit(3)
                    Text(record.resultLabel).workFont(.caption).foregroundStyle(Theme.muted).lineLimit(1)
                } else if let record = members.first {
                    // The step's own declared kind, verbatim from the payload
                    // (`section_kind`): the eyebrow that tells a debugging step
                    // from a review one before the title is read.
                    if let kind = record.sectionKind {
                        Text(kind).workFont(.caption).foregroundStyle(Theme.muted).lineLimit(1)
                    } else if showsCaptions && record.laneTitle != record.displayTitle {
                        Text(record.laneTitle).workFont(.caption).foregroundStyle(Theme.muted).lineLimit(1)
                    }
                    Text(record.displayTitle).workFont(.rowLabel).foregroundStyle(Theme.ink).lineLimit(2)
                    Label {
                        Text(record.resultLabel)
                    } icon: {
                        if let symbol = symbol(record) {
                            Image(systemName: symbol)
                        } else {
                            // A lifecycle step is not an evidence tier: a small
                            // flat square, never a pip-family circle (K05).
                            Rectangle().frame(width: 6, height: 6)
                        }
                    }
                    .workFont(.caption).foregroundStyle(tint(record)).lineLimit(1)
                    // The reducer's named result/exit-code disagreement. It
                    // had no render site anywhere; the card now carries the
                    // caution glyph and the details region prints the sentence.
                    if record.noteText != nil {
                        Image(systemName: "exclamationmark.triangle")
                            .workFont(.caption).foregroundStyle(Theme.amber)
                            .accessibilityHidden(true)
                    }
                }
            }
            .padding(6)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
            .background(selected ? Theme.selected : Theme.card,
                in: RoundedRectangle(cornerRadius: Metrics.radius))
            .overlay(RoundedRectangle(cornerRadius: Metrics.radius)
                .strokeBorder(selected ? Theme.accent : Theme.cardLine, lineWidth: selected ? 2 : 1))
            // The reducer's salience, as a rule on the card's leading edge —
            // never the accent (the interactive voice) and never a result tone
            // (salience is not an outcome). A single-record card only: a group
            // card speaks for several records and none of them owns its edge.
            .modifier(SalienceRule(record: item.isCluster ? nil : members.first))
            .clipShape(RoundedRectangle(cornerRadius: Metrics.radius))
            .contentShape(Rectangle())
        }
        // The card states its own focus rather than leaving it to the ambient
        // `\.isFocused`, which a style body never sees under the card's OWN
        // `.focusable()` — so the inset ring below simply never drew.
        .buttonStyle(SurfaceButtonStyle(focusInset: 2, isFocused: onScreen && focusedItem == item.id))
        .focusable(onScreen)
        // The style draws its own inset focus ring; the system effect is drawn
        // by the AppKit host OUTSIDE this canvas's rounded container (K99).
        .focusEffectDisabled()
        .focused($focusedItem, equals: item.id)
        .onChange(of: focusedItem) { _, id in
            guard id == item.id else { return }
            onFocusEnter?()
        }
        .onKeyPress(.space) { activate(item, members: members); return .handled }
        .onKeyPress(.return) { activate(item, members: members); return .handled }
        .onHover { hoveredID = $0 ? item.id : nil }
        // A tooltip repeating the accessibility label teaches nothing (K122);
        // a single card's full title is already spoken and, when the card
        // truncates it, the inspector below prints it in full.
        .modifier(OptionalHelp(text: item.isCluster
            ? "Inspect these \(members.count) records or zoom into their time range" : nil))
        .accessibilityIdentifier(item.isCluster
            ? "work.timeline.cluster.\(item.id.replacingOccurrences(of: "cluster:", with: ""))"
            : "work.timeline.record.\(item.recordIDs[0])")
        // Spoken time is the same local clock the axis and the screen show
        // (C55/K122) — never a UTC ISO string or epoch digits.
        // A group speaks its members the way it prints them — a spoken bare
        // count would hide the same evidence the card used to hide.
        .accessibilityLabel(item.isCluster
            ? (["\(members.count) records in a time group"]
                + members.prefix(Self.clusterSpokenLimit).map { "\($0.displayTitle), \($0.resultLabel)" }
                + (members.count > Self.clusterSpokenLimit ? ["\(members.count - Self.clusterSpokenLimit) more"] : []))
                .joined(separator: ", ")
            : Self.spokenCard(members.first))
        .accessibilityAddTraits(selected ? .isSelected : [])
        .onKeyPress(.escape) {
            onDismiss()
            return .handled
        }
    }

    /// What ONE card says aloud. A salient card also speaks the reducer's
    /// reason: the leading rule is visual only, and a sentence is the only way
    /// a VoiceOver reader learns that this record was pulled forward (F3). A
    /// beat speaks its own prose and the section it belongs to, so narration is
    /// never heard as a step.
    static func spokenCard(_ record: WorkTimelineRecord?) -> String {
        guard let record else { return "Activity" }
        let time = record.start.map { WorkTimelineTimeAxis.spokenLabel($0) } ?? "Time unavailable"
        if record.isBeat {
            return [WorkTimelineProjection.nonempty(record.summary) ?? record.displayTitle,
                    record.resultLabel,
                    WorkTimelineProjection.nonempty(record.sectionTitle),
                    time].compactMap { $0 }.joined(separator: ", ")
        }
        return [record.displayTitle, record.resultLabel, record.laneTitle, record.source,
                record.isSalient ? record.salienceReason : nil,
                time].compactMap { $0 }.joined(separator: ", ")
    }

    /// How many lines a group card has under its count, at the card's height.
    private static let clusterContentLines = 3
    /// How many members a group card names aloud before it counts the rest.
    private static let clusterSpokenLimit = 6

    /// What a group card SAYS it contains.
    ///
    /// A bare "N records" hid the very thing a reviewer opened the timeline
    /// for — the check names and their results. A group now names its members
    /// (each with its own result glyph and tint) until the card is full, and
    /// whatever cannot fit is COUNTED, never silently dropped. A current
    /// failure and a multi-session mix outrank the names for the remaining
    /// lines: they change what the reviewer does next.
    @ViewBuilder
    private func clusterBody(_ members: [WorkTimelineRecord]) -> some View {
        let sessionCount = Set(members.map(\.laneID)).count
        let failures = members.filter(\.isCurrentFailure).count
        // A failure a LATER RUN replaced is still a failure this group
        // contains. Counting only live failures meant the one task with a
        // fail → pass recovery showed no failure line at all, while the tile
        // beside it reported an earlier failed run (B5).
        let resolved = failures > 0 ? 0 : members.filter(\.isResolvedFailure).count
        let failureLines = (failures > 0 || resolved > 0) ? 1 : 0
        let budget = Self.clusterContentLines - failureLines - (sessionCount > 1 ? 1 : 0)
        let named = members.count <= budget ? members.count : max(0, budget - 1)
        Text(Fmt.count(members.count, "record")).workFont(.rowLabel).foregroundStyle(Theme.ink)
        if failures > 0 {
            Label("\(failures) failed", systemImage: "exclamationmark.circle")
                .workFont(.caption).foregroundStyle(Theme.coral).lineLimit(1)
        } else if resolved > 0 {
            // Same words, same coral — a failure did happen. The GLYPH is what
            // separates it from a live failure: a failure and its re-run.
            Label("\(resolved) failed", systemImage: "exclamationmark.arrow.circlepath")
                .workFont(.caption).foregroundStyle(Theme.coral).lineLimit(1)
        }
        if sessionCount > 1 {
            Text("\(sessionCount) sessions").workFont(.caption).foregroundStyle(Theme.muted).lineLimit(1)
        }
        ForEach(members.prefix(named)) { member in
            Label {
                // One line per member, so the END of a command or path — the
                // part that tells two runs of the same tool apart — survives.
                Text(member.displayTitle).lineLimit(1).truncationMode(.middle)
            } icon: {
                if let symbol = symbol(member) {
                    Image(systemName: symbol)
                } else {
                    Rectangle().frame(width: 6, height: 6)
                }
            }
            .workFont(.caption).foregroundStyle(tint(member))
            .accessibilityLabel("\(member.displayTitle), \(member.resultLabel)")
        }
        if named > 0, named < members.count {
            Text("+\(members.count - named) more").workFont(.caption).foregroundStyle(Theme.muted).lineLimit(1)
        }
    }

    private func activate(_ item: WorkTimeCanvasLayout.Item, members: [WorkTimelineRecord]) {
        lastTriggerID = item.id
        if item.isCluster { onHold(); onCluster(members, item.timeBounds) }
        else if let record = members.first { onSelect(record) }
    }
    private struct FocusTarget: Equatable {
        let request: Int
        let recordID: String?
    }

    private func focus(in layout: WorkTimeCanvasLayout) {
        guard let focusRecordID else { return }
        focusedItem = layout.items.first { $0.recordIDs.contains(focusRecordID) }?.id
            ?? layout.items.first { $0.id == lastTriggerID }?.id
    }

    private func tint(_ record: WorkTimelineRecord) -> Color { record.presentationTint }
    /// The SF symbol of a record's result line, or nil for a step (drawn as
    /// the flat square marker). Circle-family shapes are the evidence pips'
    /// grammar, so a non-passing, non-current check is a flat minus (K05).
    private func symbol(_ record: WorkTimelineRecord) -> String? {
        // A failure a later run replaced gets its OWN glyph — a recorded
        // failure and its re-run — distinct from both a live failure (cross)
        // and plain superseded history (clock).
        if record.isResolvedFailure { return "exclamationmark.arrow.circlepath" }
        if record.superseded { return "clock.arrow.circlepath" }
        if record.kind == .step { return nil }
        switch record.checkTone {
        case .pass: return "checkmark.circle"
        case .failure: return record.isCurrentFailure ? "xmark.circle" : "minus"
        case .notRun: return CheckResultTone.notRun.symbol
        }
    }
}

private struct WorkTimeCanvasWidthKey: PreferenceKey {
    static var defaultValue: Double = 0
    static func reduce(value: inout Double, nextValue: () -> Double) { value = max(value, nextValue()) }
}

/// Distinct recorded spans in a crossing cluster; many members commonly share
/// identical extents, and each still draws once.
private struct CrossingSpanKey: Hashable {
    let start: Double
    let end: Double
    let failure: Bool
}

/// Scrollbar-like overview: dragging its body pans the window, its edges
/// resize one boundary, and wheel or pinch input resizes the visible span
/// around the pointer. The small histogram counts dated records, never elapsed
/// work or utilization.
/// The overview strip: a draggable, zoomable window over the whole recorded
/// range. It is the canvas's navigation control, and the record LIST uses the
/// same one — so choosing the list never costs the reviewer zoom or panning.
struct WorkTimeWindowScroller: View {
    let records: [WorkTimelineRecord]
    let full: WorkTimelineInterval
    let window: WorkTimelineInterval
    let domain: WorkTimelineInterval
    let onWindow: (WorkTimelineInterval) -> Void
    @State private var dragStart: WorkTimelineInterval?

    /// Is this strip a control at all?
    ///
    /// Narrowing is the only thing its handles, its adjustable steps and its
    /// wheel do, and `WorkTimeCanvasLayout.clampedWindow` widens every requested
    /// window back to the recorded span when that span is at or under the floor.
    /// The whole strip is then inert — panning included, because a window that
    /// already covers the domain has nowhere to move — so it is replaced by the
    /// named state rather than left looking live.
    private var canNarrow: Bool { WorkTimeCanvasLayout.canNarrow(full) }

    var body: some View {
        if canNarrow { control } else { notNarrowableState }
    }

    /// The named state that stands in for the control: the recorded span, and
    /// that there is nothing to narrow. It is static text — no Tab stop, no
    /// adjustable action, no cursor rect, no gesture — so nothing about it
    /// announces or looks like a control (C13, and "absence is a named state").
    private var notNarrowableState: some View {
        Text(Self.notNarrowableText(span: full.upper - full.lower))
            .workFont(.caption).foregroundStyle(Theme.muted)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 20)
            .frame(height: 40)
            .accessibilityIdentifier("work.timeline.window.not-narrowable")
            .accessibilityHint(Self.notNarrowableDetail)
            .help(Self.notNarrowableDetail)
    }

    private var control: some View {
        GeometryReader { geometry in
            let width = max(geometry.size.width - 40, 1)
            let left = domain.fraction(window.lower) * width
            let right = domain.fraction(window.upper) * width
            WorkTimeCanvasInput(interactiveRegions: [CGRect(x: 0, y: 0, width: geometry.size.width, height: 32)],
                onPan: { pixels in
                    onWindow(WorkTimeCanvasLayout.pannedWindow(window, by: -pixels / width * domain.span, within: domain))
                }, onZoom: { factor, anchor in
                    // The pointer position is a fraction of the domain, not of
                    // the visible window.
                    onWindow(WorkTimeCanvasLayout.zoomedWindow(window, factor: factor,
                        anchorTime: domain.lower + domain.span * anchor, within: full, positionDomain: domain))
                }, cursorRegions: cursorRegions(left: left, right: right, height: geometry.size.height),
                accessibilityIdentifier: "work.timeline.overview.navigation",
                accessibilityLabel: "Timeline overview") {
            ZStack(alignment: .leading) {
                // The track alone. main drew the density histogram here, under
                // the window thumb; this side keeps that histogram but moved it
                // to `marks` below, ON TOP of the thumb (F4) — so the track is
                // drawn at full hairline strength rather than halved, and the
                // histogram is never painted twice.
                RoundedRectangle(cornerRadius: 4).fill(Theme.hairline)
                Button { onWindow(window) } label: {
                    RoundedRectangle(cornerRadius: 4)
                        .fill(Theme.tintAccent)
                        .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(Theme.accent))
                        .contentShape(Rectangle())
                }
                    .buttonStyle(SurfaceButtonStyle())
                    // The style draws the app's own 2pt accent ring; the
                    // system's pale halo is the only thing that showed here
                    // before, and it measured below the 3:1 minimum (K117).
                    .focusEffectDisabled()
                    .frame(width: max(right - left, 2)).offset(x: left)
                    .simultaneousGesture(DragGesture(minimumDistance: 2, coordinateSpace: .named("work-time-overview")).onChanged { value in
                        let start = dragStart ?? window; dragStart = start
                        onWindow(WorkTimeCanvasLayout.pannedWindow(start, by: value.translation.width / width * domain.span, within: domain))
                    }.onEnded { _ in dragStart = nil })
                    .accessibilityRepresentation { accessibleRangeControl(isStart: nil) }
                    .help("Drag to move through time. Pinch, Option-scroll or drag either edge to change the visible time span.")
                // The marks are drawn ON TOP of the window thumb, not under it.
                // Underneath, a window that already covered everything loaded
                // hid every mark behind its own full-width fill, and the strip
                // read as a broken empty control (F4). The thumb says WHERE you
                // are looking; the marks say where the records are — and the
                // marks must survive the thumb covering them.
                marks.allowsHitTesting(false)
                handle(at: left, isStart: true, width: width)
                handle(at: right, isStart: false, width: width)
            }
            .frame(width: width, height: 32).coordinateSpace(name: "work-time-overview").padding(.horizontal, 20)
            }.renderingSurface
        }
        .frame(height: 40)
    }

    // MARK: the named state's words

    /// The reducer's sentence for a window with nothing to narrow.
    ///
    /// The WORDS are `display_vocabulary.TIMELINE_WINDOW_NOT_NARROWABLE`; only
    /// `{span}` is filled in here, because only a rendering surface can measure
    /// the span. `tests/test_surface_parity.py` pins both literals to Python
    /// character for character — the same arrangement `agoText` uses for the
    /// freshness phrase.
    static let notNarrowableTemplate = "Whole recorded span: {span} — nothing to narrow"
    /// `display_vocabulary.TIMELINE_WINDOW_NOT_NARROWABLE_DETAIL`: why the
    /// control is gone. Also the canvas's accessibility help in that state.
    /// How to drive the canvas. Mirrors `display_vocabulary.TIMELINE_GESTURE_HINT`
    /// character for character; the parity test pins both.
    ///
    /// Plain scroll is deliberately left to the page, so a reader who scrolls
    /// over the canvas sees nothing happen — indistinguishable from broken
    /// unless the canvas says what the gesture actually is. Google Maps answers
    /// the same problem the same way ("Use ctrl + scroll to zoom the map").
    static let gestureHint = "⌥ scroll to zoom · ⇧ scroll or drag to move"
    static let gestureHintDetail = "Hold Option and scroll to change how much time is in view. Hold Shift and scroll, swipe sideways, or drag the canvas to move through time. Plain scrolling is left to the page, so the timeline never takes over your scrolling."

    static let notNarrowableDetail = "The whole recorded span is already in view, and it is shorter than the smallest window the time axis can label, so there is no narrower view to move to."

    static func notNarrowableText(span: Double) -> String {
        notNarrowableTemplate.replacingOccurrences(of: "{span}", with: spanText(span))
    }

    /// A recorded span in words. Mirrors `display_vocabulary.timeline_span_text`
    /// branch for branch — hundredths below a second (the state exists for spans
    /// the axis cannot label, where whole seconds would print the forbidden
    /// `0s`), tenths below ten, whole seconds below a minute, and the app's
    /// shared duration words above that. An unmeasurable span is NAMED
    /// (`TIME_SPAN_NOT_RECORDED`), never zero.
    static func spanText(_ seconds: Double) -> String {
        guard seconds.isFinite, seconds > 0 else { return "window not recorded" }
        if seconds < 1 { return String(format: "%.2fs", seconds) }
        if seconds < 10 { return String(format: "%.1fs", seconds) }
        if seconds < 60 { return "\(Int(seconds.rounded()))s" }
        let total = Int(seconds)
        if total < 86_400 {
            let hours = total / 3_600, minutes = (total % 3_600) / 60
            return hours > 0 ? "\(hours)h \(minutes)m" : "\(minutes)m"
        }
        return "\(total / 86_400)d \((total % 86_400) / 3_600)h"
    }

    /// The strip's own content: one bar per bucket of dated records, counting
    /// RECORDS — never elapsed work or utilization. It is the reason the strip
    /// is worth looking at when the window already spans everything, so it is
    /// drawn over the window thumb rather than under it.
    private var marks: some View {
        Canvas { context, size in
            var bins = [Int](repeating: 0, count: max(1, Int(size.width / 5)))
            for record in records {
                guard let time = WorkTimelineProjection.validTime(record.start) else { continue }
                let bucket = domain.fraction(time) * Double(bins.count)
                guard bucket.isFinite else { continue }
                bins[min(max(Int(bucket), 0), bins.count - 1)] += 1
            }
            let peak = max(bins.max() ?? 0, 1)
            for (index, value) in bins.enumerated() where value > 0 {
                let h = max(3, Double(value) / Double(peak) * 20)
                // Widths and heights are taken as Double explicitly (main's
                // refinement): the mixed CGFloat/Double arithmetic that stood
                // here is an inference burden with no payoff.
                let w = Double(size.width)
                context.fill(Path(CGRect(x: Double(index) * w / Double(bins.count), y: Double(size.height) - h - 3,
                    width: max(w / Double(bins.count) - 1, 1), height: h)), with: .color(Theme.chartNeutral))
            }
        }
        .accessibilityHidden(true)  // the window control beside it speaks the range
    }

    /// Cursor rects owned by the input view (C107): the window body shows an
    /// open hand and each edge handle a resize cursor. Coordinates include the
    /// strip's 20pt horizontal inset and match the handle offsets below.
    private func cursorRegions(left: Double, right: Double, height: CGFloat) -> [WorkTimeCanvasCursorRegion] {
        let inset = 20.0
        return [
            .init(rect: CGRect(x: inset + left, y: 0, width: max(right - left, 2), height: height), cursor: .openHand),
            .init(rect: CGRect(x: inset + left - 19, y: 0, width: 18, height: height), cursor: .resizeLeftRight),
            .init(rect: CGRect(x: inset + right + 1, y: 0, width: 18, height: height), cursor: .resizeLeftRight),
        ]
    }

    private func handle(at x: Double, isStart: Bool, width: Double) -> some View {
        Button { onWindow(window) } label: {
            RoundedRectangle(cornerRadius: 2).fill(Theme.accent)
                .frame(width: 4, height: 18).frame(width: 18, height: 32)
                .contentShape(Rectangle())
        }
            .buttonStyle(SurfaceButtonStyle())
            .offset(x: x + (isStart ? -19 : 1))
            .simultaneousGesture(DragGesture(minimumDistance: 2, coordinateSpace: .named("work-time-overview")).onChanged { value in
                let start = dragStart ?? window; dragStart = start
                let delta = value.translation.width / width * full.span
                let minimum = min(WorkTimeCanvasLayout.minimumVisibleSpan, full.span)
                let lower = isStart ? min(max(start.lower + delta, full.lower), start.upper - minimum) : start.lower
                let upper = isStart ? start.upper : max(min(start.upper + delta, full.upper), start.lower + minimum)
                onWindow(.init(lower: lower, upper: upper))
            }.onEnded { _ in dragStart = nil })
            .accessibilityRepresentation { accessibleRangeControl(isStart: isStart) }
            .help(isStart ? "Drag to change the start of the time window" : "Drag to change the end of the time window")
    }

    /// The graphical range as an adjustable element: its VALUE is the clock a
    /// reader sees, and increment/decrement move it.
    ///
    /// A native `Slider` representation spoke its own numeric value instead —
    /// the raw epoch seconds ("1789441993.940317"), which is unusable aloud
    /// (K122). An adjustable element's value is the string set here, so the
    /// spoken window matches the axis.
    private func accessibleRangeControl(isStart: Bool?) -> some View {
        let minimum = min(WorkTimeCanvasLayout.minimumVisibleSpan, full.span)
        let step = max(minimum, window.span * 0.1)
        return Color.clear
            .accessibilityElement()
            .accessibilityLabel(isStart == nil ? "Visible time window" : (isStart == true ? "Start of visible time window" : "End of visible time window"))
            .accessibilityValue(isStart == nil
                ? "\(WorkTimelineTimeAxis.label(window.lower, range: window)) to \(WorkTimelineTimeAxis.label(window.upper, range: window))"
                : WorkTimelineTimeAxis.label(isStart == true ? window.lower : window.upper, range: window))
            .accessibilityIdentifier(isStart == nil ? "work.timeline.window" : (isStart == true ? "work.timeline.window.start" : "work.timeline.window.end"))
            .accessibilityAdjustableAction { direction in
                let delta = direction == .increment ? step : -step
                guard let isStart else {
                    onWindow(WorkTimeCanvasLayout.pannedWindow(window, by: delta, within: full))
                    return
                }
                let lower = isStart
                    ? min(max(window.lower + delta, full.lower), window.upper - minimum) : window.lower
                let upper = isStart
                    ? window.upper : max(min(window.upper + delta, full.upper), window.lower + minimum)
                onWindow(.init(lower: lower, upper: upper))
            }
    }

}
