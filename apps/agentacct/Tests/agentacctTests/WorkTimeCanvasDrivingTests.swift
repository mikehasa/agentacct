import AppKit
import SwiftUI
import XCTest
@testable import agentacct

/// Does the INTERACTION work — not "does the canvas open"?
///
/// Every other canvas test calls a geometry function or an input view in
/// isolation. This one hosts the REAL `WorkTimeCanvas` in a real window, lets it
/// measure its own width, finds the input view the app actually installs, and
/// sends real `NSEvent`s into it: Option-scroll, pinch, `+`, `-`, Left arrow and
/// a blank-area drag. Each case records the window BEFORE and AFTER, so "the
/// visible interval changed" is measured rather than assumed.
///
/// The three record sets are the measured shapes of three real Tasks in the
/// installed store, read from `/v1/task-timeline`: 3 events over 0.3021s
/// (task_7bb028c1), 9 over 138.6125s (task_5f7dbea9) and 65 over 24,954.8252s
/// (task_ef6818aa). The first is shorter than the window floor, so nothing about
/// it can be narrowed — and this suite pins that the surface says so instead of
/// accepting a gesture and doing nothing.
///
/// Host-independent: it compares no image, only window numbers and which
/// accessibility elements exist.
final class WorkTimeCanvasDrivingTests: XCTestCase {

    // MARK: - the three measured tasks

    /// task_7bb028c1 — 3 events, span 0.3021s.
    private static let brief: [Double] = [1_789_441_994.940317, 1_789_441_995.020317, 1_789_441_995.242425]
    /// task_5f7dbea9 — 9 events, span 138.6125s. The extent and the count are
    /// the measured ones; the interior beats are spread evenly, which the window
    /// arithmetic does not read.
    private static let medium: [Double] = (0..<9).map { 1_789_558_174.695357 + Double($0) * 138.6125 / 8 }
    /// task_ef6818aa — 65 events, span 24,954.8252s.
    private static let long: [Double] = (0..<65).map { 1_789_347_057.589825 + Double($0) * 24_954.8252 / 64 }

    /// Whether each gesture MUST change the window, per task, and why.
    ///
    /// * task_7bb028c1: nothing may change. The whole recorded span is under the
    ///   window floor, so there is no narrower window and (the window already
    ///   covering its domain) nowhere to pan.
    /// * task_5f7dbea9: the canvas opens on the whole 138-second span, so
    ///   zooming IN works and so does panning — the pannable domain carries half
    ///   a card of margin beyond the recorded range (K18), about 16 seconds here,
    ///   so there is somewhere to go. Zooming OUT is the one saturated direction:
    ///   the span is already the whole recorded range, and that event belongs to
    ///   the page (C13).
    /// * task_ef6818aa: the canvas opens on the last 30 minutes of a 6.9-hour
    ///   span, so every direction has somewhere to go.
    private static let expectations: [String: [String: Bool]] = [
        "task_7bb028c1": ["Option-scroll": false, "pinch": false, "+": false, "-": false,
                          "Left arrow": false, "body drag": false],
        "task_5f7dbea9": ["Option-scroll": true, "pinch": true, "+": true, "-": false,
                          "Left arrow": true, "body drag": true],
        "task_ef6818aa": ["Option-scroll": true, "pinch": true, "+": true, "-": true,
                          "Left arrow": true, "body drag": true],
    ]

    private func records(_ times: [Double]) -> [WorkTimelineRecord] {
        times.enumerated().map { index, time in
            WorkTimelineRecord(id: "event-\(index)", laneID: "session", laneTitle: "Session",
                               lineage: "Recorded session", kind: index.isMultiple(of: 3) ? .check : .step,
                               title: "event \(index)", start: time, end: time,
                               lane: index.isMultiple(of: 3) ? "evidence" : "primary",
                               laneLabel: index.isMultiple(of: 3) ? "Check evidence" : "Primary session")
        }
    }

    private func interval(_ times: [Double]) -> WorkTimelineInterval {
        .init(lower: times.min()!, upper: times.max()!)
    }

    private var tasks: [(String, [Double])] {
        [("task_7bb028c1", Self.brief), ("task_5f7dbea9", Self.medium), ("task_ef6818aa", Self.long)]
    }

    // MARK: - driving

    @MainActor func testEveryCanvasGestureEitherMovesTheWindowOrIsNotAControl() throws {
        for (name, times) in tasks {
            let full = interval(times)
            // `WorkTimelineInterval.span` floors at one second, so the recorded
            // extent is read by subtraction here — a 0.30-second Task reports a
            // 1-second `span` and would otherwise look like a different shape.
            XCTAssertEqual(WorkTimeCanvasLayout.canNarrow(full), full.upper - full.lower > 5, name)
            let expected = try XCTUnwrap(Self.expectations[name])

            let outcomes: [(String, DriveResult)] = [
                ("Option-scroll", try drive(times, name: name) { view in
                    let event = DrivingEvent(x: 0, y: 12, modifiers: .option)
                    event.testLocation = CGPoint(x: view.bounds.midX, y: view.bounds.midY)
                    view.scrollWheel(with: event)
                }),
                ("pinch", try drive(times, name: name) { view in
                    let event = DrivingEvent(phase: .changed)
                    event.testMagnification = 0.3
                    event.testLocation = CGPoint(x: view.bounds.midX, y: view.bounds.midY)
                    view.magnify(with: event)
                }),
                ("+", try drive(times, name: name) { view in
                    view.window?.makeFirstResponder(view)
                    view.keyDown(with: Self.key("+", keyCode: 24, in: view.window))
                }),
                ("-", try drive(times, name: name) { view in
                    view.window?.makeFirstResponder(view)
                    view.keyDown(with: Self.key("-", keyCode: 27, in: view.window))
                }),
                ("Left arrow", try drive(times, name: name) { view in
                    view.window?.makeFirstResponder(view)
                    view.keyDown(with: Self.key("\u{F702}", keyCode: 123, in: view.window))
                }),
                ("body drag", try drive(times, name: name) { view in
                    let down = DrivingEvent(); down.testLocation = CGPoint(x: 400, y: 200)
                    view.mouseDown(with: down)
                    let moved = DrivingEvent(); moved.testLocation = CGPoint(x: 480, y: 200)
                    view.mouseDragged(with: moved)
                    view.mouseUp(with: moved)
                }),
            ]

            for (gesture, result) in outcomes {
                let moved = result.after.map { !WorkTimeCanvasLayout.sameWindow($0, result.before) } ?? false
                // Printed by subtraction, not `span`, which floors at one second.
                print(String(format: "DRIVE %@ %@: before [%.4f, %.4f] span %.4f -> %@",
                             name, gesture, result.before.lower, result.before.upper,
                             result.before.upper - result.before.lower,
                             result.after.map {
                                 String(format: "[%.4f, %.4f] span %.4f", $0.lower, $0.upper, $0.upper - $0.lower)
                             } ?? "no window requested"))
                XCTAssertEqual(moved, expected[gesture], "\(name): \(gesture)")
                // A refused KEY is not swallowed: it walks the responder chain
                // to the window, the way a saturated wheel reaches the page
                // (C13). Mouse and wheel gestures have their own pass-through,
                // pinned in WorkTimeCanvasInputTests.
                if ["+", "-", "Left arrow"].contains(gesture) {
                    XCTAssertEqual(result.reachedThePage.isEmpty, moved,
                        "\(name): \(gesture) reached the page: \(result.reachedThePage)")
                }
            }
        }
    }

    /// The overview strip is a control on the two narrowable tasks, and on the
    /// one it cannot narrow it is not installed at all: no navigation view, so no
    /// Tab stop, no cursor rects, no custom actions — the named state renders in
    /// its place (its words are pinned in `WorkTimeCanvasInputTests`).
    @MainActor func testOverviewIsInstalledOnlyWhileThereIsSomethingToNarrow() throws {
        for (name, times) in tasks {
            let narrowable = WorkTimeCanvasLayout.canNarrow(interval(times))
            let window = try hostedCanvas(times, requested: Box())
            defer { window.close() }
            let identifiers = Self.identifiers(in: window.contentView)
            XCTAssertEqual(identifiers.contains("work.timeline.overview.navigation"), narrowable,
                           "\(name): the overview's navigation view")
            let overview = Self.view(in: window.contentView, identifier: "work.timeline.overview.navigation")
            XCTAssertEqual(overview != nil, narrowable, "\(name): the overview's keyboard stop")
            // The canvas itself is always installed, and always names its window.
            XCTAssertTrue(identifiers.contains("work.timeline.navigation"), name)
            let canvas = try XCTUnwrap(Self.view(in: window.contentView, identifier: "work.timeline.navigation"))
            XCTAssertEqual(canvas.accessibilityHelp(),
                           narrowable ? WorkTimeCanvasHelp.navigable : WorkTimeWindowScroller.notNarrowableDetail,
                           "\(name): the canvas's spoken help")
            XCTAssertEqual((canvas.accessibilityCustomActions() ?? []).isEmpty, !narrowable,
                           "\(name): navigation actions offered")
        }
    }

    // MARK: - harness

    private struct DriveResult {
        let before: WorkTimelineInterval
        /// The window the canvas asked for, or nil when the gesture asked for
        /// nothing at all.
        let after: WorkTimelineInterval?
        /// Keys the canvas refused, which the responder chain carried to the
        /// window — the page's own scroll, in the app.
        let reachedThePage: [String]
    }

    private final class Box {
        var initial = WorkTimelineInterval(lower: 0, upper: 1)
        var window: WorkTimelineInterval?
    }

    /// One gesture against a freshly hosted canvas, so no result depends on a
    /// SwiftUI re-render landing between two events.
    @MainActor private func drive(_ times: [Double], name: String,
                                  _ gesture: (NSView) -> Void) throws -> DriveResult {
        let requested = Box()
        let window = try hostedCanvas(times, requested: requested)
        defer { window.close() }
        let input = try XCTUnwrap(Self.view(in: window.contentView, identifier: "work.timeline.navigation"),
                                  "\(name): the canvas installed no input view")
        XCTAssertGreaterThan(input.bounds.width, 100, "\(name): the canvas measured no width")
        gesture(input)
        return DriveResult(before: requested.initial, after: requested.window,
                           reachedThePage: (window as? KeySinkWindow)?.unhandled ?? [])
    }

    @MainActor private func hostedCanvas(_ times: [Double], requested: Box) throws -> NSWindow {
        let full = interval(times)
        // Every window the page holds passes through the same clamp, so the
        // canvas starts where the app starts it.
        requested.initial = WorkTimeCanvasLayout.clampedWindow(
            WorkTimeCanvasLayout.latestWindow(within: full, latest: times.max()), to: full)
        let start = requested.initial
        let view = NSHostingView(rootView: AnyView(
            WorkTimeCanvas(records: records(times), full: full, window: start,
                           selectedRecord: nil, onWindow: { requested.window = $0 },
                           onSelect: { _ in }, onCluster: { _, _ in }, onDismiss: {}, onHold: {})
                .frame(width: 900, height: 520)))
        let window = KeySinkWindow(contentRect: CGRect(x: 0, y: 0, width: 900, height: 520),
                                   styleMask: .borderless, backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = view
        view.layoutSubtreeIfNeeded()
        _ = view.fittingSize
        // One turn of the run loop lets the width preference and the geometry
        // reader settle, which is what makes the plot's seconds-per-pixel real.
        RunLoop.current.run(until: Date().addingTimeInterval(0.15))
        view.layoutSubtreeIfNeeded()
        return window
    }

    /// The hosted AppKit view carrying this accessibility identifier. The canvas
    /// input view's own `Content` is an opaque SwiftUI type, so it is found by
    /// identity rather than by cast.
    private static func view(in view: NSView?, identifier: String) -> NSView? {
        guard let view else { return nil }
        if view.accessibilityIdentifier() == identifier, view.acceptsFirstResponder { return view }
        for child in view.subviews {
            if let found = Self.view(in: child, identifier: identifier) { return found }
        }
        return nil
    }

    /// A real key event — synthesized, never posted. `NSEvent.keyEvent` is used
    /// rather than a stub subclass because an unhandled key walks the responder
    /// chain, and AppKit sends that path messages a stub cannot answer.
    private static func key(_ characters: String, keyCode: UInt16, in window: NSWindow?) -> NSEvent {
        NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0,
                         windowNumber: window?.windowNumber ?? 0, context: nil,
                         characters: characters, charactersIgnoringModifiers: characters,
                         isARepeat: false, keyCode: keyCode)!
    }

    /// Every accessibility identifier carried by an AppKit view in the hosted
    /// tree. SwiftUI's own elements are built lazily for a real accessibility
    /// client, so only the AppKit layer is inspected here — which is exactly the
    /// layer that owns the keyboard stop, the cursor rects and the custom
    /// actions this change removes.
    private static func identifiers(in view: NSView?) -> Set<String> {
        guard let view else { return [] }
        var found: Set<String> = []
        let identifier = view.accessibilityIdentifier()
        if !identifier.isEmpty { found.insert(identifier) }
        for child in view.subviews { found.formUnion(identifiers(in: child)) }
        return found
    }
}

/// Synthesized locally and handed straight to a view method. Never posted to
/// NSApplication, CGEvent, an event monitor or the user's event queue.
private final class DrivingEvent: NSEvent {
    var testLocation = NSPoint.zero
    var testMagnification: CGFloat = 0
    let testDeltaX: CGFloat
    let testDeltaY: CGFloat
    let testModifiers: NSEvent.ModifierFlags
    let testPhase: NSEvent.Phase

    init(x: CGFloat = 0, y: CGFloat = 0, modifiers: NSEvent.ModifierFlags = [], phase: NSEvent.Phase = []) {
        testDeltaX = x; testDeltaY = y; testModifiers = modifiers; testPhase = phase
        super.init()
    }
    required init?(coder: NSCoder) { return nil }
    override var type: NSEvent.EventType { .scrollWheel }
    override var locationInWindow: NSPoint { testLocation }
    override var magnification: CGFloat { testMagnification }
    override var scrollingDeltaX: CGFloat { testDeltaX }
    override var scrollingDeltaY: CGFloat { testDeltaY }
    override var hasPreciseScrollingDeltas: Bool { true }
    override var modifierFlags: NSEvent.ModifierFlags { testModifiers }
    override var phase: NSEvent.Phase { testPhase }
    override var momentumPhase: NSEvent.Phase { [] }
}

/// The end of the responder chain: a key nothing handled lands here instead of
/// reaching `noResponder(for:)` and its beep. It is also the evidence that a
/// refused key really does leave the canvas (C13).
private final class KeySinkWindow: NSWindow {
    var unhandled: [String] = []
    override func keyDown(with event: NSEvent) {
        unhandled.append(event.charactersIgnoringModifiers ?? "")
    }
}
