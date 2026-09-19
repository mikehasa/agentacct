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
    var compact = false
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
    private var canvasHeight: Double { max(compact ? 280 : 420, WorkTimeCanvasLayout.minimumHeight(textScale: scale)) }

    var body: some View {
        VStack(spacing: 8) {
            if dynamicTypeSize.isAccessibilitySize {
                WorkTimeWindowScroller(records: records, full: full, window: window, domain: scrollerDomain, onWindow: onWindow)
            }
            GeometryReader { geometry in
                let width = Double(geometry.size.width)
                let indexedRecords = Dictionary(records.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
                let layout = WorkTimeCanvasLayout(records: records, window: window,
                    width: width, height: canvasHeight, textScale: scale)
                let visibleCards = layout.visibleCards(in: width)
                let crossingSpans = layout.crossingSpans(in: width)
                // A small margin beyond the recorded range lets the extreme
                // positions show a card centered on the first/last record in
                // full; placement itself stays anchored to absolute time.
                let edgeDomain = WorkTimeCanvasLayout.expandedDomain(full,
                    by: WorkTimeCanvasLayout.edgeRevealTime(window: window, width: width, cardWidth: layout.cardWidth))
                let tickMarkings = WorkTimelineTimeAxis.ticks(in: window, width: width, minimumSpacing: 140 * scale)
                input(items: visibleCards, window: window, full: full, domain: edgeDomain, width: width) {
                    ZStack(alignment: .topLeading) {
                    Theme.canvas
                    drawing(layout: layout, items: visibleCards, crossing: crossingSpans, ticks: tickMarkings.times, width: width, indexedRecords: indexedRecords).allowsHitTesting(false)
                    axisLabels(ticks: tickMarkings, width: width, axisY: layout.axisY).allowsHitTesting(false)
                    ForEach(visibleCards) { item in
                        itemButton(item, indexedRecords: indexedRecords)
                            .frame(width: item.frame.width, height: item.frame.height)
                            .position(x: item.frame.midX, y: item.frame.midY)
                    }
                    if layout.visibleRecordCount == 0 {
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
            if !dynamicTypeSize.isAccessibilitySize {
                WorkTimeWindowScroller(records: records, full: full, window: window, domain: scrollerDomain, onWindow: onWindow)
            }
        }
        .background(Theme.canvas, in: RoundedRectangle(cornerRadius: Metrics.radius))
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

    private func input<Content: View>(items: [WorkTimeCanvasLayout.Item], window: WorkTimelineInterval, full: WorkTimelineInterval, domain: WorkTimelineInterval, width: Double, @ViewBuilder content: () -> Content) -> some View {
        WorkTimeCanvasInput(interactiveRegions: items.map(\.frame),
            onPan: { pixels in
                // Keyboard and accessibility increments resolve against the
                // latest committed window.
                onWindow(WorkTimeCanvasLayout.pannedWindow(window, by: -pixels / max(width, 1) * window.span, within: domain))
            },
            onDrag: { cumulativePixels in
                // Mouse drags carry the total translation from gesture start
                // and resolve against the window captured when the drag began,
                // so a slow frame can drop a request but never drop motion.
                if dragBaseWindow == nil { dragBaseWindow = window }
                let base = dragBaseWindow ?? window
                onWindow(WorkTimeCanvasLayout.pannedWindow(base,
                    by: -cumulativePixels / max(width, 1) * base.span, within: domain))
            },
            onZoom: { factor, anchor in
                onWindow(WorkTimeCanvasLayout.zoomedWindow(window, factor: factor, anchorFraction: anchor,
                    within: full, positionDomain: domain))
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
            func xPosition(_ time: Double) -> Double { (time - window.lower) / window.span * width }
            var spine = Path()
            spine.move(to: CGPoint(x: 0, y: layout.axisY))
            spine.addLine(to: CGPoint(x: width, y: layout.axisY))
            context.stroke(spine, with: .color(Theme.muted.opacity(0.65)), lineWidth: 1)
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
                    let key = CrossingSpanKey(start: start, end: end, failure: record.isCurrentFailure)
                    guard drawnSpans.insert(key).inserted else { continue }
                    let tint: Color = record.isCurrentFailure ? Theme.coral : Theme.accent
                    var span = Path()
                    span.move(to: CGPoint(x: xPosition(start), y: y))
                    span.addLine(to: CGPoint(x: xPosition(end), y: y))
                    context.stroke(span, with: .color(tint.opacity(0.35)), lineWidth: 3)
                }
            }
            for item in items {
                let selected = selectedID.map(item.recordIDs.contains) ?? false
                let emphasized = selected || hoveredID == item.id || focusedItem == item.id
                let tint = item.recordIDs.contains { id in indexedRecords[id]?.isCurrentFailure == true }
                    ? Theme.coral : Theme.accent
                let edge = item.isAbove ? item.frame.maxY : item.frame.minY
                var stem = Path()
                stem.move(to: CGPoint(x: item.anchorX, y: layout.axisY))
                stem.addLine(to: CGPoint(x: item.anchorX, y: edge))
                context.stroke(stem, with: .color(tint.opacity(emphasized ? 0.85 : 0.32)), lineWidth: emphasized ? 2 : 1)
                if !item.isCluster, let id = item.recordIDs.first, let record = indexedRecords[id], record.isDuration {
                    let x1 = xPosition(record.start!)
                    let x2 = xPosition(record.end!)
                    let y = layout.axisY + (item.isAbove ? -5.0 : 5.0)
                    var span = Path()
                    span.move(to: CGPoint(x: x1, y: y)); span.addLine(to: CGPoint(x: x2, y: y))
                    context.stroke(span, with: .color(tint.opacity(emphasized ? 0.9 : 0.35)), lineWidth: 3)
                }
                let radius = item.isCluster ? 5.0 : 3.5
                context.fill(Path(ellipseIn: CGRect(x: item.anchorX - radius, y: layout.axisY - radius,
                    width: radius * 2, height: radius * 2)), with: .color(tint))
            }
        }
        .accessibilityHidden(true)
    }

    /// Labels sit on their tick marks and slide with them. A label's text is
    /// fixed by its absolute time and the current step, so it never renumbers
    /// in place while the window moves.
    private func axisLabels(ticks: (step: Double, times: [Double]), width: Double, axisY: Double) -> some View {
        ZStack(alignment: .topLeading) {
            ForEach(ticks.times, id: \.self) { tick in
                Text(WorkTimelineTimeAxis.tickLabel(tick, step: ticks.step, range: window))
                    .workFont(.dataSmall).foregroundStyle(Theme.muted)
                    .fixedSize().background(Theme.canvas)
                    .position(x: (tick - window.lower) / window.span * width, y: axisY + 12 + 7 * scale)
            }
        }
        .frame(width: width)
        .accessibilityHidden(true)
    }

    private func itemButton(_ item: WorkTimeCanvasLayout.Item, indexedRecords: [String: WorkTimelineRecord]) -> some View {
        let members = item.recordIDs.compactMap { indexedRecords[$0] }
        let selected = selectedID.map(item.recordIDs.contains) ?? false
        return Button { activate(item, members: members) } label: {
            VStack(alignment: .leading, spacing: 2) {
                if item.isCluster {
                    Text(Fmt.count(members.count, "record")).workFont(.rowLabel).foregroundStyle(Theme.ink)
                    let sessionCount = Set(members.map(\.laneID)).count
                    Text(sessionCount == 1 ? (members.first?.laneTitle ?? "Activity") : "\(sessionCount) sessions")
                        .workFont(.caption).foregroundStyle(Theme.muted).lineLimit(1)
                    let failures = members.filter(\.isCurrentFailure).count
                    if failures > 0 {
                        Label("\(failures) failed", systemImage: "exclamationmark.circle")
                            .workFont(.caption).foregroundStyle(Theme.coral)
                    }
                } else if let record = members.first {
                    Text(record.laneTitle).workFont(.caption).foregroundStyle(Theme.muted).lineLimit(1)
                    Text(record.title).workFont(.rowLabel).foregroundStyle(Theme.ink).lineLimit(2)
                    Label(record.resultLabel, systemImage: symbol(record))
                        .workFont(.caption).foregroundStyle(tint(record)).lineLimit(1)
                }
            }
            .padding(6)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
            .background(selected ? Theme.selected : Theme.card,
                in: RoundedRectangle(cornerRadius: Metrics.radius))
            .overlay(RoundedRectangle(cornerRadius: Metrics.radius)
                .strokeBorder(selected ? Theme.accent : Theme.cardLine, lineWidth: selected ? 2 : 1))
            .contentShape(Rectangle())
        }
        .buttonStyle(SurfaceButtonStyle(focusInset: 2))
        .focusable()
        .focused($focusedItem, equals: item.id)
        .onKeyPress(.space) { activate(item, members: members); return .handled }
        .onKeyPress(.return) { activate(item, members: members); return .handled }
        .onHover { hoveredID = $0 ? item.id : nil }
        .help(members.count == 1 ? "\(members[0].title)\n\(members[0].laneTitle)\n\(members[0].source)" : "Inspect these \(members.count) records or zoom into their time range")
        .accessibilityIdentifier(item.isCluster
            ? "work.timeline.cluster.\(item.id.replacingOccurrences(of: "cluster:", with: ""))"
            : "work.timeline.record.\(item.recordIDs[0])")
        .accessibilityLabel(item.isCluster ? "\(members.count) records in a time group" : "\(members.first?.title ?? "Activity"), \(members.first?.resultLabel ?? ""), \(members.first?.laneTitle ?? ""), \(members.first?.source ?? ""), \(members.first?.start.map { WorkTimelineTimeAxis.preciseLabel($0) } ?? "Time unavailable")")
        .accessibilityAddTraits(selected ? .isSelected : [])
        .onKeyPress(.escape) {
            onDismiss()
            return .handled
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
    private func symbol(_ record: WorkTimelineRecord) -> String {
        if record.superseded { return "clock.arrow.circlepath" }
        if record.kind == .step { return "circle.inset.filled" }
        return record.result == "passed" ? "checkmark.circle" : (record.isCurrentFailure ? "xmark.circle" : "circle")
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
private struct WorkTimeWindowScroller: View {
    let records: [WorkTimelineRecord]
    let full: WorkTimelineInterval
    let window: WorkTimelineInterval
    let domain: WorkTimelineInterval
    let onWindow: (WorkTimelineInterval) -> Void
    @State private var dragStart: WorkTimelineInterval?

    var body: some View {
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
                }, accessibilityIdentifier: "work.timeline.overview.navigation") {
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 4).fill(Theme.hairline.opacity(0.5))
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
                        let w = Double(size.width)
                        context.fill(Path(CGRect(x: Double(index) * w / Double(bins.count), y: Double(size.height) - h - 3,
                            width: max(w / Double(bins.count) - 1, 1), height: h)), with: .color(Theme.muted.opacity(0.4)))
                    }
                }.allowsHitTesting(false)
                Button { onWindow(window) } label: {
                    RoundedRectangle(cornerRadius: 4)
                        .fill(Theme.accent.opacity(0.12))
                        .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(Theme.accent.opacity(0.7)))
                        .contentShape(Rectangle())
                }
                    .buttonStyle(SurfaceButtonStyle())
                    .frame(width: max(right - left, 2)).offset(x: left)
                    .simultaneousGesture(DragGesture(minimumDistance: 2, coordinateSpace: .named("work-time-overview")).onChanged { value in
                        let start = dragStart ?? window; dragStart = start
                        onWindow(WorkTimeCanvasLayout.pannedWindow(start, by: value.translation.width / width * domain.span, within: domain))
                    }.onEnded { _ in dragStart = nil })
                    .onHover { ($0 ? NSCursor.openHand : NSCursor.arrow).set() }
                    .accessibilityRepresentation { accessibleRangeControl(isStart: nil) }
                    .help("Drag to move through time. Scroll or drag either edge to change the visible time span.")
                handle(at: left, isStart: true, width: width)
                handle(at: right, isStart: false, width: width)
            }
            .frame(width: width, height: 32).coordinateSpace(name: "work-time-overview").padding(.horizontal, 20)
            }.renderingSurface
        }
        .frame(height: 40)
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
            .onHover { ($0 ? NSCursor.resizeLeftRight : NSCursor.arrow).set() }
            .accessibilityRepresentation { accessibleRangeControl(isStart: isStart) }
            .help(isStart ? "Drag to change the start of the time window" : "Drag to change the end of the time window")
    }

    /// The graphical range exposes native slider values and standard
    /// increment/decrement actions to keyboard and accessibility navigation.
    private func accessibleRangeControl(isStart: Bool?) -> some View {
        let minimum = min(WorkTimeCanvasLayout.minimumVisibleSpan, full.span)
        let lower = isStart == false ? window.lower + minimum : full.lower
        let upper = isStart == true ? window.upper - minimum
            : (isStart == false ? full.upper : full.upper - window.span)
        let value = Binding<Double>(
            get: { isStart == false ? window.upper : window.lower },
            set: { value in
                if let isStart {
                    onWindow(.init(lower: isStart ? value : window.lower, upper: isStart ? window.upper : value))
                } else {
                    onWindow(WorkTimeCanvasLayout.pannedWindow(window, by: value - window.lower, within: full))
                }
            })
        // A window that already spans the whole domain would otherwise give
        // the slider a zero-length range.
        let upperBound = max(upper, lower + min(WorkTimeCanvasLayout.minimumVisibleSpan, full.span))
        return Slider(value: value, in: lower...upperBound)
            .accessibilityLabel(isStart == nil ? "Visible time window" : (isStart == true ? "Start of visible time window" : "End of visible time window"))
            .accessibilityValue(isStart == nil
                ? "\(WorkTimelineTimeAxis.label(window.lower, range: window)) to \(WorkTimelineTimeAxis.label(window.upper, range: window))"
                : WorkTimelineTimeAxis.label(isStart == true ? window.lower : window.upper, range: window))
            .accessibilityIdentifier(isStart == nil ? "work.timeline.window" : (isStart == true ? "work.timeline.window.start" : "work.timeline.window.end"))
    }

}
