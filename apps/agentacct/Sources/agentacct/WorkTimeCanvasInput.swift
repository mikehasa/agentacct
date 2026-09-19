import AppKit
import SwiftUI

/// The canvas's navigation help. It describes the gestures THIS surface owns,
/// so it is the app's own sentence rather than a payload fact — but the sentence
/// that REPLACES it when none of those gestures can act is a named state, and
/// that one comes from the reducer's vocabulary through `accessibilityHelp`.
enum WorkTimeCanvasHelp {
    static let navigable = "Pinch or Option-scroll to change the visible time span; scroll sideways or drag to move through time. The viewing window below also supports dragging and resizing."
}

/// A cursor owned by the input view through cursor rects, in the same top-left
/// coordinates as `interactiveRegions` (C107).
struct WorkTimeCanvasCursorRegion {
    var rect: CGRect
    var cursor: NSCursor
}

/// Native input for a fixed-size time canvas embedded in a scrolling page.
/// Dragging and sideways scrolling pan through time in content-motion pixels
/// (positive to the right); pinch and Option-scroll resize the visible span
/// with zoom factors above one zooming in. A plain vertical wheel always
/// reaches the page, and saturated zoom or pan input does too (C13). Interactive
/// rectangles use the same top-left coordinate system as the hosted SwiftUI
/// content.
struct WorkTimeCanvasInput<Content: View>: NSViewRepresentable {
    @Environment(\.self) private var environment
    var interactiveRegions: [CGRect]
    var onPan: (Double) -> Void
    var onDrag: ((Double) -> Void)?
    var onZoom: (Double, Double) -> Void
    /// Whether a zoom would change the window; nil means always.
    var canZoom: ((Double, Double) -> Bool)?
    /// Whether a pan would change the window; nil means always.
    var canPan: ((Double) -> Bool)?
    /// Whether jumping to the earliest/latest edge would change the window;
    /// nil means always.
    var canReachEdge: ((Bool) -> Bool)?
    var onInteraction: (() -> Void)?
    var onEdge: ((Bool) -> Void)?
    var onDismiss: (() -> Void)?
    var onBackgroundClick: (() -> Void)?
    var onGestureBegan: (() -> Void)?
    var onGestureEnded: (() -> Void)?
    var onGestureCancelled: (() -> Void)?
    var cursorRegions: [WorkTimeCanvasCursorRegion]
    var accessibilityValue: String
    var accessibilityIdentifier: String
    /// Names THIS canvas. Two groups that both said "Time canvas" left a
    /// screen-reader user unable to tell the plot from its overview (K122).
    var accessibilityLabel: String
    /// What this canvas can be DONE to, spoken on focus. It is a parameter and
    /// not a constant because a canvas whose window can be neither narrowed nor
    /// panned must not read out gestures it will ignore; the replacement
    /// sentence is the reducer's
    /// (`display_vocabulary.TIMELINE_WINDOW_NOT_NARROWABLE_DETAIL`).
    var accessibilityHelp: String
    var content: Content

    var hostedContent: WorkTimeCanvasHostedContent<Content> {
        .init(content: content, environment: environment)
    }

    init(interactiveRegions: [CGRect], onPan: @escaping (Double) -> Void,
         onDrag: ((Double) -> Void)? = nil,
         onZoom: @escaping (Double, Double) -> Void,
         canZoom: ((Double, Double) -> Bool)? = nil, canPan: ((Double) -> Bool)? = nil,
         canReachEdge: ((Bool) -> Bool)? = nil,
         onInteraction: (() -> Void)? = nil,
         onEdge: ((Bool) -> Void)? = nil, onDismiss: (() -> Void)? = nil,
         onBackgroundClick: (() -> Void)? = nil, onGestureBegan: (() -> Void)? = nil,
         onGestureEnded: (() -> Void)? = nil,
         onGestureCancelled: (() -> Void)? = nil,
         cursorRegions: [WorkTimeCanvasCursorRegion] = [], accessibilityValue: String = "",
         accessibilityIdentifier: String = "work.timeline.navigation",
         accessibilityLabel: String = "Activity timeline",
         accessibilityHelp: String = WorkTimeCanvasHelp.navigable,
         @ViewBuilder content: () -> Content) {
        self.interactiveRegions = interactiveRegions
        self.onPan = onPan; self.onDrag = onDrag; self.onZoom = onZoom; self.onInteraction = onInteraction
        self.canZoom = canZoom; self.canPan = canPan; self.canReachEdge = canReachEdge
        self.cursorRegions = cursorRegions
        self.accessibilityHelp = accessibilityHelp
        self.onEdge = onEdge; self.onDismiss = onDismiss
        self.onBackgroundClick = onBackgroundClick; self.onGestureBegan = onGestureBegan
        self.onGestureEnded = onGestureEnded
        self.onGestureCancelled = onGestureCancelled
        self.accessibilityValue = accessibilityValue; self.accessibilityIdentifier = accessibilityIdentifier
        self.accessibilityLabel = accessibilityLabel
        self.content = content()
    }

    func makeNSView(context: Context) -> WorkTimeCanvasInputView<Content> {
        WorkTimeCanvasInputView(configuration: self)
    }

    func updateNSView(_ view: WorkTimeCanvasInputView<Content>, context: Context) {
        view.configuration = self
        view.hosting.rootView = hostedContent
        view.setAccessibilityValue(accessibilityValue)
        view.setAccessibilityLabel(accessibilityLabel)
        view.setAccessibilityHelp(accessibilityHelp)
    }
}

extension WorkTimeCanvasInput {
    /// ImageRenderer cannot draw AppKit hosts. Static fixtures draw the same
    /// SwiftUI content directly; native review and the app retain real input.
    @ViewBuilder var renderingSurface: some View {
        if SnapshotMode.enabled && !SnapshotMode.interactiveFixture {
            content
        } else {
            self
        }
    }
}

/// NSHostingView begins a new SwiftUI tree, so explicitly carry the containing
/// window's environment, including Reading size, appearance and Reduce Motion.
struct WorkTimeCanvasHostedContent<Content: View>: View {
    var content: Content
    var environment: EnvironmentValues
    var body: some View { content.environment(\.self, environment) }
}

/// Pure interpretation helpers keep device deltas, zoom direction and reserved
/// modifiers testable without posting input into the user's desktop session.
enum WorkTimeCanvasInputIntent {
    enum KeyAction: Equatable {
        case pan(Double), zoom(Double), edge(latest: Bool), dismiss
    }

    /// ONE step per navigation action, shared by `keyAction` and the
    /// accessibility custom actions. They were separate literals, so an action
    /// filtered out for being a no-op could have been filtered by a different
    /// step than the key would have used.
    static let panStep = 48.0
    static let zoomInFactor = 1.25
    static let zoomOutFactor = 0.8

    /// Only Option-scroll resizes the visible time span; an unmodified wheel
    /// returns nil so the page keeps its scroll axis. A positive dominant delta
    /// (scrolling up or right) zooms in; negative zooms out. The per-event
    /// factor is bounded so one notch or a fast trackpad flick stays
    /// predictable. Command and Control keep their system behavior (Control
    /// scroll is accessibility zoom).
    static func zoomScroll(deltaX: Double, deltaY: Double, precise: Bool,
                           modifiers: NSEvent.ModifierFlags,
                           horizontalGesture: Bool? = nil) -> Double? {
        guard modifiers.contains(.option), modifiers.intersection([.command, .control]).isEmpty,
              deltaX.isFinite, deltaY.isFinite else { return nil }
        let delta: Double
        if let horizontalGesture {
            delta = horizontalGesture ? deltaX : deltaY
        } else {
            delta = deltaY != 0 ? deltaY : deltaX
        }
        guard delta != 0 else { return nil }
        // Clamp before exponentiating so an extreme device delta saturates at
        // the factor bounds instead of overflowing to a pass-through event.
        let bounded = min(max(delta, -100), 100)
        let factor = exp(bounded * (precise ? 0.004 : 0.12))
        guard factor.isFinite, factor > 0 else { return nil }
        return min(max(factor, 0.75), 1.33)
    }

    /// Sideways scrolling pans through time in content-motion pixels. Only a
    /// horizontal-dominant, unmodified delta pans; vertical-dominant input
    /// returns nil and reaches the page. A phased gesture passes its latched
    /// axis so diagonal drift cannot flip meaning mid-gesture.
    static func panScroll(deltaX: Double, deltaY: Double, precise: Bool,
                          modifiers: NSEvent.ModifierFlags,
                          horizontalGesture: Bool? = nil) -> Double? {
        guard modifiers.intersection([.command, .control, .option]).isEmpty,
              deltaX.isFinite, deltaY.isFinite else { return nil }
        let horizontal = horizontalGesture ?? (abs(deltaX) > abs(deltaY))
        guard horizontal, deltaX != 0 else { return nil }
        // A line-based wheel reports whole lines; scale them to points.
        let pixels = precise ? deltaX : deltaX * 10
        return min(max(pixels, -2_000), 2_000)
    }

    static func zoomFactor(magnification: Double) -> Double? {
        guard magnification.isFinite, magnification != 0 else { return nil }
        let factor = exp(magnification)
        return factor.isFinite && factor > 0 ? factor : nil
    }

    static func anchorFraction(x: Double, width: Double) -> Double? {
        guard x.isFinite, width.isFinite, width > 0 else { return nil }
        return min(max(x / width, 0), 1)
    }

    static func keyAction(keyCode: UInt16, characters: String,
                          modifiers: NSEvent.ModifierFlags) -> KeyAction? {
        guard modifiers.intersection([.command, .control, .option]).isEmpty else { return nil }
        switch keyCode {
        case 123: return .pan(panStep) // Left: reveal earlier time.
        case 124: return .pan(-panStep)
        case 115: return .edge(latest: false)
        case 119: return .edge(latest: true)
        case 53: return .dismiss
        default:
            switch characters {
            case "+", "=": return .zoom(zoomInFactor)
            case "-": return .zoom(zoomOutFactor)
            default: return nil
            }
        }
    }
}

/// The hosting child routes wheel and pinch events here even when a card is
/// under the pointer. Blank-area mouse input is handled by this parent; card
/// mouse input remains inside SwiftUI. No application-wide event monitor exists.
final class WorkTimeCanvasInputView<Content: View>: NSView {
    var configuration: WorkTimeCanvasInput<Content> {
        didSet {
            let old = oldValue.cursorRegions.map(\.rect)
            if old != configuration.cursorRegions.map(\.rect) { window?.invalidateCursorRects(for: self) }
            syncCustomActions()
        }
    }
    let hosting: WorkTimeCanvasHostingView<WorkTimeCanvasHostedContent<Content>>
    /// The focus indicator, drawn by a sibling ABOVE `hosting`. See
    /// `WorkTimeCanvasFocusOverlay`.
    private let focusOverlay = WorkTimeCanvasFocusOverlay()
    private var dragStart: NSPoint?
    private var lastDragX: CGFloat = 0
    private var dragging = false
    private var magnifying = false
    private var scrollHorizontalAtStart: Bool?
    private var cursorTracking: NSTrackingArea?

    init(configuration: WorkTimeCanvasInput<Content>) {
        self.configuration = configuration
        hosting = WorkTimeCanvasHostingView(rootView: configuration.hostedContent)
        super.init(frame: .zero)
        // This host begins its own AppKit layer tree, so the enclosing
        // SwiftUI `.clipped()` does not bound what it draws: a selected card's
        // accent outline escaped into the page gutter (K99). Clip here, where
        // the container's bounds are.
        wantsLayer = true
        layer?.masksToBounds = true
        hosting.wantsLayer = true
        hosting.layer?.masksToBounds = true
        hosting.autoresizingMask = [.width, .height]
        addSubview(hosting)
        // ABOVE the host, because the host is OPAQUE: its SwiftUI root fills
        // these bounds with `Theme.well`, so a ring stroked in this view's own
        // `draw(_:)` was painted first and then covered — AX reported the canvas
        // focused and an audit photographing both states could not tell them
        // apart (K130). A sibling added after `hosting` draws the same stroke on
        // top of it; it is transparent and never hit-tested, so nothing else
        // about the canvas changes.
        focusOverlay.autoresizingMask = [.width, .height]
        addSubview(focusOverlay, positioned: .above, relativeTo: hosting)
        hosting.routeScroll = { [weak self] in self?.scrollWheel(with: $0) }
        hosting.routeMagnify = { [weak self] in self?.magnify(with: $0) }
        setAccessibilityElement(true)
        setAccessibilityRole(.group)
        setAccessibilityLabel(configuration.accessibilityLabel)
        setAccessibilityIdentifier(configuration.accessibilityIdentifier)
        setAccessibilityValue(configuration.accessibilityValue)
        setAccessibilityHelp(configuration.accessibilityHelp)
        syncCustomActions()
    }

    /// Only the navigation actions that CAN act are offered.
    ///
    /// An adjustable action that reports success and moves nothing is the same
    /// defect as a dead Tab stop: a screen-reader user is told the window
    /// changed when it did not. On a task shorter than the window floor every
    /// one of these is a no-op, and the list is then empty — the canvas's help
    /// says so in the reducer's words instead (C13).
    private func syncCustomActions() {
        let intent = WorkTimeCanvasInputIntent.self
        let candidates: [(name: String, action: WorkTimeCanvasInputIntent.KeyAction)] = [
            ("Move earlier", .pan(intent.panStep)),
            ("Move later", .pan(-intent.panStep)),
            ("Narrow viewing window", .zoom(intent.zoomInFactor)),
            ("Widen viewing window", .zoom(intent.zoomOutFactor)),
        ]
        let live = candidates.filter { canPerform($0.action) }
        guard live.map(\.name) != (accessibilityCustomActions() ?? []).map(\.name) else { return }
        setAccessibilityCustomActions(live.map { candidate in
            NSAccessibilityCustomAction(name: candidate.name) { [weak self] in
                self?.perform(candidate.action) ?? false
            }
        })
    }

    /// Would this action change the visible window? The same predicates the
    /// wheel and pinch paths consult, so a key, an accessibility action and a
    /// gesture can never disagree about whether the canvas is saturated.
    private func canPerform(_ action: WorkTimeCanvasInputIntent.KeyAction) -> Bool {
        switch action {
        case .pan(let pixels): return configuration.canPan?(pixels) ?? true
        case .zoom(let factor): return configuration.canZoom?(factor, 0.5) ?? true
        case .edge(let latest):
            guard configuration.onEdge != nil else { return false }
            return configuration.canReachEdge?(latest) ?? true
        case .dismiss: return dragging || magnifying || configuration.onDismiss != nil
        }
    }

    required init?(coder: NSCoder) { return nil }
    override var isFlipped: Bool { true }
    override var acceptsFirstResponder: Bool { true }

    override func accessibilityPerformPress() -> Bool { window?.makeFirstResponder(self) ?? false }

    override func becomeFirstResponder() -> Bool {
        syncFocusRing(true)
        // A ring below the fold is the same defect as no ring: the key loop
        // reaches this canvas from the controls above it, so it brings itself
        // into view the way every other stop on the page does (K130).
        scrollToVisible(bounds)
        return true
    }
    override func resignFirstResponder() -> Bool { syncFocusRing(false); return true }

    /// Hand the overlay the one fact it draws from. Called on both responder
    /// transitions and on layout, so a ring never survives a rebuild.
    private func syncFocusRing(_ focused: Bool) {
        focusOverlay.showsRing = focused
    }

    override func layout() {
        super.layout()
        hosting.frame = bounds
        focusOverlay.frame = bounds
        syncFocusRing(window?.firstResponder === self)
        window?.invalidateCursorRects(for: self)
    }

    override func resetCursorRects() {
        super.resetCursorRects()
        for region in configuration.cursorRegions {
            let rect = region.rect.intersection(bounds)
            guard !rect.isNull, !rect.isEmpty else { continue }
            addCursorRect(rect, cursor: region.cursor)
        }
    }

    override func hitTest(_ point: NSPoint) -> NSView? {
        guard let hit = super.hitTest(point) else { return nil }
        let local = convert(point, from: superview)
        return configuration.interactiveRegions.contains { $0.contains(local) } ? hit : self
    }

    /// Option-scroll resizes the visible span around the pointer; sideways
    /// scrolling pans. The dominant axis latches for the gesture's phases so a
    /// diagonal drift cannot flip meaning mid-gesture. A plain vertical wheel,
    /// reserved modifiers, zero deltas and saturated zoom or pan fall through
    /// to the page.
    override func scrollWheel(with event: NSEvent) {
        if event.phase.contains(.began) || event.phase.contains(.mayBegin) {
            scrollHorizontalAtStart = nil
        }
        let phased = !event.phase.isEmpty || !event.momentumPhase.isEmpty
        if phased, scrollHorizontalAtStart == nil,
           event.scrollingDeltaX != 0 || event.scrollingDeltaY != 0 {
            scrollHorizontalAtStart = abs(event.scrollingDeltaX) > abs(event.scrollingDeltaY)
        }
        defer {
            if event.phase.contains(.cancelled) || event.momentumPhase.contains(.ended)
                || event.momentumPhase.contains(.cancelled) {
                scrollHorizontalAtStart = nil
            }
        }
        let latched = phased ? scrollHorizontalAtStart : nil
        if let factor = WorkTimeCanvasInputIntent.zoomScroll(
            deltaX: event.scrollingDeltaX, deltaY: event.scrollingDeltaY,
            precise: event.hasPreciseScrollingDeltas, modifiers: event.modifierFlags,
            horizontalGesture: latched) {
            let anchor = WorkTimeCanvasInputIntent.anchorFraction(
                x: convert(event.locationInWindow, from: nil).x, width: bounds.width) ?? 0.5
            if configuration.canZoom?(factor, anchor) ?? true {
                configuration.onInteraction?()
                configuration.onZoom(factor, anchor)
                return
            }
        } else if let pixels = WorkTimeCanvasInputIntent.panScroll(
            deltaX: event.scrollingDeltaX, deltaY: event.scrollingDeltaY,
            precise: event.hasPreciseScrollingDeltas, modifiers: event.modifierFlags,
            horizontalGesture: latched) {
            if configuration.canPan?(pixels) ?? true {
                configuration.onInteraction?()
                configuration.onPan(pixels)
                return
            }
        }
        super.scrollWheel(with: event)
    }

    override func magnify(with event: NSEvent) {
        if event.phase == .cancelled {
            if magnifying { configuration.onGestureCancelled?() }
            magnifying = false
            return
        }
        if let factor = WorkTimeCanvasInputIntent.zoomFactor(magnification: event.magnification),
           let anchor = WorkTimeCanvasInputIntent.anchorFraction(
            x: convert(event.locationInWindow, from: nil).x, width: bounds.width) {
            if configuration.canZoom?(factor, anchor) ?? true {
                if !magnifying { configuration.onInteraction?(); magnifying = true }
                configuration.onZoom(factor, anchor)
            } else if !magnifying {
                // Saturated pinch input is not consumed.
                super.magnify(with: event)
            }
        }
        if event.phase == .ended {
            if magnifying { configuration.onGestureEnded?() }
            magnifying = false
        }
    }

    override func mouseDown(with event: NSEvent) {
        guard event.modifierFlags.intersection([.command, .control, .option]).isEmpty else {
            super.mouseDown(with: event)
            return
        }
        window?.makeFirstResponder(self)
        dragStart = convert(event.locationInWindow, from: nil)
        lastDragX = dragStart!.x
        dragging = false
        configuration.onGestureBegan?()
    }

    override func mouseDragged(with event: NSEvent) {
        guard let dragStart else { return }
        let point = convert(event.locationInWindow, from: nil)
        guard point.x.isFinite else { return }
        if !dragging {
            // A drag that cannot move the window never becomes a drag: no
            // closed-hand cursor, no interaction hold, no pan request that
            // resolves back to the window it started from.
            guard abs(point.x - dragStart.x) > 3, canDragToPan else { return }
            dragging = true
            configuration.onInteraction?()
            NSCursor.closedHand.set()
        }
        if let onDrag = configuration.onDrag {
            // Cumulative translation from gesture start; the receiver resolves
            // it against its gesture-start state.
            onDrag(point.x - dragStart.x)
        } else {
            let delta = point.x - lastDragX
            lastDragX = point.x
            if delta != 0 { configuration.onPan(delta) }
        }
    }

    override func mouseUp(with event: NSEvent) {
        guard dragStart != nil else { return }
        let wasDragging = dragging
        dragStart = nil; dragging = false
        if wasDragging { configuration.onGestureEnded?() }
        else if bounds.contains(convert(event.locationInWindow, from: nil)) { configuration.onBackgroundClick?() }
        updateCursor(at: convert(event.locationInWindow, from: nil))
    }

    override func keyDown(with event: NSEvent) {
        guard window?.firstResponder === self,
              let action = WorkTimeCanvasInputIntent.keyAction(keyCode: event.keyCode,
                characters: event.charactersIgnoringModifiers ?? "", modifiers: event.modifierFlags),
              perform(action) else {
            super.keyDown(with: event)
            return
        }
    }

    /// Returns false for an action this canvas cannot carry out, so the key
    /// event reaches the page instead of being swallowed — the same policy the
    /// wheel and pinch paths already follow for saturated input (C13). Before
    /// this, +/- and the arrows were consumed and did nothing on a task shorter
    /// than the window floor, which is what "the scroll interaction is still not
    /// working" looked like from the outside.
    @discardableResult
    private func perform(_ action: WorkTimeCanvasInputIntent.KeyAction) -> Bool {
        guard canPerform(action) else { return false }
        switch action {
        case .pan(let pixels): configuration.onInteraction?(); configuration.onPan(pixels)
        case .zoom(let factor): configuration.onInteraction?(); configuration.onZoom(factor, 0.5)
        case .edge(let latest):
            guard let onEdge = configuration.onEdge else { return false }
            configuration.onInteraction?(); onEdge(latest)
        case .dismiss:
            if dragging || magnifying {
                configuration.onGestureCancelled?()
                dragStart = nil; dragging = false; magnifying = false
                NSCursor.openHand.set()
            } else if let onDismiss = configuration.onDismiss { onDismiss() }
            else { return false }
        }
        return true
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let cursorTracking { removeTrackingArea(cursorTracking) }
        let area = NSTrackingArea(rect: .zero,
            options: [.activeInKeyWindow, .inVisibleRect, .mouseMoved, .cursorUpdate, .mouseEnteredAndExited],
            owner: self, userInfo: nil)
        addTrackingArea(area); cursorTracking = area
    }

    override func mouseMoved(with event: NSEvent) { updateCursor(at: convert(event.locationInWindow, from: nil)) }
    override func cursorUpdate(with event: NSEvent) { updateCursor(at: convert(event.locationInWindow, from: nil)) }
    override func mouseExited(with event: NSEvent) { if !dragging { NSCursor.arrow.set() } }

    /// An open hand over the blank canvas PROMISES a drag. When neither
    /// direction of pan can change the window that promise is false, so the
    /// blank area keeps the plain arrow — the cursor is the first thing a reader
    /// reads about whether a surface is a control.
    private func updateCursor(at point: NSPoint) {
        if dragging { NSCursor.closedHand.set() }
        else if let region = configuration.cursorRegions.first(where: { $0.rect.contains(point) }) { region.cursor.set() }
        else if configuration.interactiveRegions.contains(where: { $0.contains(point) }) { NSCursor.arrow.set() }
        else if canDragToPan { NSCursor.openHand.set() }
        else { NSCursor.arrow.set() }
    }

    /// Can a drag move the window in EITHER direction? One pixel is the
    /// smallest real drag, and a window parked against one domain edge can
    /// still be dragged away from it.
    private var canDragToPan: Bool {
        guard let canPan = configuration.canPan else { return true }
        return canPan(1) || canPan(-1)
    }
}

/// The canvas's focus indicator, and nothing else.
///
/// It exists because the canvas's content is an opaque AppKit child: the
/// enclosing view cannot draw over its own subview, so the ring had to become a
/// sibling drawn after it. It paints the app's ONE focus treatment — a
/// `Metrics.focusW` accent stroke set `Metrics.focusGap` inside the edge, at
/// `Metrics.radius` — never the system's `keyboardFocusIndicatorColor`, which
/// is a second, off-palette focus colour. Accent is right here for the same
/// reason it is right in `FocusRing`: it is the interactive voice, and a ring
/// says "you can act here".
///
/// It is transparent, hit-tests to nothing, and accepts no responder status, so
/// it changes no gesture, no cursor rect, and no accessibility tree.
final class WorkTimeCanvasFocusOverlay: NSView {
    var showsRing = false {
        didSet { if showsRing != oldValue { needsDisplay = true } }
    }

    override var isFlipped: Bool { true }
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
    override var acceptsFirstResponder: Bool { false }
    override var isOpaque: Bool { false }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { false }

    /// The ring's stroke is centred on the path, so the path sits half a line
    /// width further in than the gap — the same arithmetic `FocusRing` does.
    static func ringPath(in bounds: NSRect) -> NSBezierPath {
        let inset = Metrics.focusGap + Metrics.focusW / 2
        let rect = bounds.insetBy(dx: inset, dy: inset)
        let path = NSBezierPath(roundedRect: rect, xRadius: Metrics.radius, yRadius: Metrics.radius)
        path.lineWidth = Metrics.focusW
        return path
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard showsRing, bounds.width > 0, bounds.height > 0 else { return }
        NSColor(Theme.accent).setStroke()
        Self.ringPath(in: bounds).stroke()
    }
}

final class WorkTimeCanvasHostingView<Content: View>: NSHostingView<Content> {
    var routeScroll: ((NSEvent) -> Void)?
    var routeMagnify: ((NSEvent) -> Void)?
    override func scrollWheel(with event: NSEvent) {
        if let routeScroll { routeScroll(event) } else { super.scrollWheel(with: event) }
    }
    override func magnify(with event: NSEvent) {
        if let routeMagnify { routeMagnify(event) } else { super.magnify(with: event) }
    }
}
