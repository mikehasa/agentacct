import AppKit
import SwiftUI
import XCTest
@testable import agentacct

/// The focus indicator has to be SEEN. An audit photographed focused and
/// unfocused states across the record page and could not tell them apart:
/// the canvas stroked its ring under an opaque child, the task table ringed
/// every visible row at once, and the record document had no keyboard stops at
/// all (K130). These tests pin the three mechanisms that fix that.
final class FocusIndicatorVisibilityTests: XCTestCase {

    // MARK: C1 — the canvas ring is drawn above the hosted content

    @MainActor
    func testCanvasFocusRingIsDrawnOverTheOpaqueHostAndOnlyWhileFocused() throws {
        let view = Self.makeCanvasInput()
        view.frame = NSRect(x: 0, y: 0, width: 240, height: 120)
        view.layout()

        // The ring's sibling sits ABOVE the host, which fills these bounds with
        // an opaque surface: order is the whole fix.
        let overlayIndex = try XCTUnwrap(
            view.subviews.firstIndex { $0 is WorkTimeCanvasFocusOverlay },
            "the canvas has no focus overlay"
        )
        let hostIndex = try XCTUnwrap(
            view.subviews.firstIndex { $0 === view.hosting },
            "the canvas has no hosted content"
        )
        XCTAssertGreaterThan(overlayIndex, hostIndex,
                             "the ring would be painted first and then covered")

        let overlay = try XCTUnwrap(view.subviews[overlayIndex] as? WorkTimeCanvasFocusOverlay)
        overlay.frame = view.bounds

        // Unfocused: nothing at all is drawn, so a diff against the focused
        // state cannot come back empty.
        overlay.showsRing = false
        XCTAssertEqual(Self.strokedPixels(in: overlay), 0)

        overlay.showsRing = true
        XCTAssertGreaterThan(Self.strokedPixels(in: overlay), 0,
                             "a focused canvas draws no visible ring")

        // It never intercepts input: the canvas's drag, wheel and cursor rects
        // all still belong to the view underneath.
        XCTAssertNil(overlay.hitTest(NSPoint(x: 10, y: 10)))
        XCTAssertFalse(overlay.acceptsFirstResponder)
    }

    @MainActor
    func testCanvasRingUsesTheAppsOneFocusWeightNotTheSystemIndicator() {
        let bounds = NSRect(x: 0, y: 0, width: 200, height: 100)
        let path = WorkTimeCanvasFocusOverlay.ringPath(in: bounds)
        XCTAssertEqual(path.lineWidth, Metrics.focusW)
        // Stroke centred on the path: the same arithmetic `FocusRing` does, so
        // the visible gap is `Metrics.focusGap`.
        let inset = Metrics.focusGap + Metrics.focusW / 2
        XCTAssertEqual(path.bounds.minX, inset, accuracy: 0.51)
        XCTAssertEqual(path.bounds.maxX, bounds.maxX - inset, accuracy: 0.51)
        XCTAssertLessThanOrEqual(Metrics.radius, 4)
    }

    @MainActor
    func testCanvasFocusRingFollowsFirstResponderAndSurvivesRelayout() throws {
        let view = Self.makeCanvasInput()
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 300, height: 200),
                             styleMask: [.titled], backing: .buffered, defer: true)
        window.contentView?.addSubview(view)
        view.frame = NSRect(x: 0, y: 0, width: 240, height: 120)
        view.layout()
        let overlay = try XCTUnwrap(
            view.subviews.compactMap { $0 as? WorkTimeCanvasFocusOverlay }.first
        )

        XCTAssertFalse(overlay.showsRing)
        XCTAssertTrue(window.makeFirstResponder(view))
        XCTAssertTrue(overlay.showsRing, "becoming first responder drew no ring")
        // A relayout (the canvas is resized by every window change) must not
        // strand a ring on, or off.
        view.layout()
        XCTAssertTrue(overlay.showsRing)
        XCTAssertTrue(window.makeFirstResponder(window.contentView))
        XCTAssertFalse(overlay.showsRing)
        view.layout()
        XCTAssertFalse(overlay.showsRing)
    }

    // MARK: C2 — one ring per keyboard stop, not one per descendant

    func testAnExplicitFocusAnswerBeatsTheAmbientEnvironment() {
        // `\.isFocused` is an ENVIRONMENT value: a focusable container sets it
        // for every descendant. A row inside the table's single stop must not
        // ring just because the table does.
        XCTAssertFalse(SurfaceButtonStyle.resolvedFocus(declared: false, ambient: true))
        XCTAssertTrue(SurfaceButtonStyle.resolvedFocus(declared: true, ambient: false))
        // Default (nil): every control that IS its own focusable element keeps
        // reading the environment, so no existing ring is lost.
        XCTAssertTrue(SurfaceButtonStyle.resolvedFocus(declared: nil, ambient: true))
        XCTAssertFalse(SurfaceButtonStyle.resolvedFocus(declared: nil, ambient: false))
        XCTAssertNil(SurfaceButtonStyle().isFocused, "the default must stay ambient")
        // A control's own KeyboardStop is the other honest source, and an
        // explicit `false` still silences both.
        XCTAssertTrue(SurfaceButtonStyle.resolvedFocus(declared: nil, ambient: false, stop: true))
        XCTAssertFalse(SurfaceButtonStyle.resolvedFocus(declared: false, ambient: false, stop: true))
    }

    // MARK: C3 — focus never lands on something the clip hides

    func testOnlyACardTheClipActuallyShowsCanTakeFocus() {
        let width = 380.0
        // Fully inside, straddling either edge: all shown, all focusable.
        XCTAssertTrue(WorkTimeCanvasLayout.isOnScreen(CGRect(x: 40, y: 0, width: 120, height: 60), in: width))
        XCTAssertTrue(WorkTimeCanvasLayout.isOnScreen(CGRect(x: -60, y: 0, width: 120, height: 60), in: width))
        XCTAssertTrue(WorkTimeCanvasLayout.isOnScreen(CGRect(x: 340, y: 0, width: 120, height: 60), in: width))
        // Entirely in the draw margin `visibleCards` keeps beyond each edge:
        // clipped away, so not a keyboard stop. This is the card Tab landed on
        // at x=254, behind the sidebar.
        XCTAssertFalse(WorkTimeCanvasLayout.isOnScreen(CGRect(x: -146, y: 0, width: 120, height: 60), in: width))
        XCTAssertFalse(WorkTimeCanvasLayout.isOnScreen(CGRect(x: 380, y: 0, width: 120, height: 60), in: width))
        XCTAssertFalse(WorkTimeCanvasLayout.isOnScreen(CGRect(x: 0, y: 0, width: 0, height: 60), in: width))
        // Not measured yet is not a reason to empty the key loop.
        XCTAssertTrue(WorkTimeCanvasLayout.isOnScreen(CGRect(x: -146, y: 0, width: 120, height: 60), in: 0))
        XCTAssertTrue(WorkTimeCanvasLayout.isOnScreen(CGRect(x: -146, y: 0, width: 120, height: 60), in: .nan))
        XCTAssertTrue(WorkTimeCanvasLayout.isOnScreen(
            CGRect(x: CGFloat.nan, y: 0, width: 120, height: 60), in: width))
    }

    /// The record page's keyboard stops are real `focusable` stops, with the
    /// system's own effect off so the app's ring is the only indicator, and with
    /// Return bound — a focusable wrapper answers the key, not the button inside
    /// it. The offscreen renderer has no key loop and is left alone.
    func testEveryRecordPageControlDeclaresAKeyboardStop() throws {
        let sources = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/agentacct", isDirectory: true)
        func read(_ name: String) throws -> String {
            try String(contentsOf: sources.appendingPathComponent(name), encoding: .utf8)
        }

        let theme = try read("Theme.swift")
        for required in [".focusable(enabled)", ".focusEffectDisabled()", ".onKeyPress(.return)",
                         "SnapshotMode.enabled && !SnapshotMode.interactiveFixture"] {
            XCTAssertTrue(theme.contains(required), "KeyboardStop dropped \(required)")
        }
        // The one glyph-only family: status legend, section help, Copy task ID.
        XCTAssertTrue(theme.contains(".keyboardStop(activate: action)"),
                      "IconButton is not in the key loop")

        let expected: [String: [String]] = [
            "WorkPane.swift": [
                ".keyboardStop(activate: leaveRecord)",              // work.breadcrumb.back
                ".keyboardStop(activate: onToggleTimelineFocus)",    // work.focus-timeline
                ".keyboardStop { expanded.toggle() }",               // outcome-summary toggle
                ".keyboardStop { toggle() }",                        // overflow disclosures
            ],
            "WorkRecordChecks.swift": [".keyboardStop { activate(check) }"],
            "StepComponents.swift": [
                ".keyboardStop { expanded.toggle() }",               // step-spine row
                ".keyboardStop { showHistory.toggle() }",            // historical checks
                ".keyboardStop { showAllAttention.toggle() }",
                ".keyboardStop { showAllCurrentChecks.toggle() }",
            ],
            "ReceiptsPane.swift": [".keyboardStop(activate: action)"],  // disposition buttons
        ]
        for (file, fragments) in expected.sorted(by: { $0.key < $1.key }) {
            let source = try read(file)
            for fragment in fragments {
                XCTAssertTrue(source.contains(fragment), "\(file) is missing \(fragment)")
            }
        }
    }

    // MARK: - helpers

    @MainActor
    private static func makeCanvasInput() -> WorkTimeCanvasInputView<EmptyView> {
        WorkTimeCanvasInputView(
            configuration: WorkTimeCanvasInput(
                interactiveRegions: [],
                onPan: { _ in },
                onZoom: { _, _ in },
                content: { EmptyView() }
            )
        )
    }

    /// Pixels the overlay actually paints. The overlay is transparent, so any
    /// non-zero alpha is the ring.
    @MainActor
    private static func strokedPixels(in view: NSView) -> Int {
        guard let representation = view.bitmapImageRepForCachingDisplay(in: view.bounds) else {
            return 0
        }
        view.cacheDisplay(in: view.bounds, to: representation)
        var painted = 0
        for y in 0..<representation.pixelsHigh {
            for x in 0..<representation.pixelsWide {
                guard let color = representation.colorAt(x: x, y: y) else { continue }
                if color.alphaComponent > 0.05 { painted += 1 }
            }
        }
        return painted
    }
}
