import Foundation
import SwiftUI
import XCTest
@testable import agentacct

/// Backgrounds, strokes and chart geometry come only from named tokens (C42).
/// An `.opacity()` derived from a semantic or ink token produces an
/// off-palette color whose contrast differs by mode (an amber wash invisible
/// on cream but loud on dark). Use a surface-relative token instead:
/// `tintAmber` / `tintAmberOnCanvas`, `tintNeutral` / `tintNeutralOnCanvas`,
/// `tintAccent`, `rule` / `hairline`, `chartBarDim`, `chartNeutral`, `well`.
///
/// A second scan bans raw system hues (`Color.green`, `.foregroundStyle(.white)`,
/// `NSColor.systemRed`, …): green/amber/coral are rationed semantic tokens and a
/// label on an accent fill is `Theme.onAccent`, so no view may paint a hue the
/// palette does not name.
final class ColorTokenLintTests: XCTestCase {
    private static let bannedTokens = ["amber", "coral", "green", "accent", "muted", "ink"]
    private static let paintCalls = ["fill", "stroke", "strokeBorder", "background"]

    private var sourcesDirectory: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()  // agentacctTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // apps/agentacct
            .appendingPathComponent("Sources/agentacct", isDirectory: true)
    }

    func testNoOpacityDerivedSemanticColorInsideFillStrokeOrBackground() throws {
        let files = try FileManager.default.contentsOfDirectory(
            at: sourcesDirectory,
            includingPropertiesForKeys: nil
        ).filter { $0.pathExtension == "swift" }
        XCTAssertFalse(files.isEmpty, "no Swift sources found at \(sourcesDirectory.path)")

        var violations: [String] = []
        for file in files.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
            let source = try String(contentsOf: file, encoding: .utf8)
            for finding in Self.violations(in: source) {
                violations.append("\(file.lastPathComponent):\(finding.line): \(finding.snippet)")
            }
        }

        XCTAssertTrue(
            violations.isEmpty,
            "Alpha-derived semantic colors inside fill/stroke/background (use a named token):\n"
                + violations.joined(separator: "\n")
        )
    }

    /// The scanner itself must catch single-line, multi-line and Canvas
    /// `context.fill` forms, and must ignore token fills and text styling.
    func testScannerCatchesKnownFormsAndIgnoresTokens() {
        let bad = """
        Rectangle().fill(Theme.muted.opacity(0.3))
        .background(
            Theme.amber.opacity(0.08),
            in: RoundedRectangle(cornerRadius: 4)
        )
        context.stroke(path, with: .color(Theme.accent.opacity(0.4)), lineWidth: 1)
        .strokeBorder(Theme.coral .opacity(0.5))
        """
        XCTAssertEqual(Self.violations(in: bad).count, 4)

        let good = """
        Rectangle().fill(Theme.tintAmberOnCanvas)
        .background(Theme.well, in: RoundedRectangle(cornerRadius: 4))
        Text("x").foregroundStyle(Theme.muted.opacity(0.8))
        .fill(Theme.chartNeutral)
        // .fill(Theme.ink.opacity(0.2)) in a comment
        """
        XCTAssertEqual(Self.violations(in: good).count, 0)
    }

    func testNoRawSystemHueAnywhereInViews() throws {
        let files = try FileManager.default.contentsOfDirectory(
            at: sourcesDirectory,
            includingPropertiesForKeys: nil
        ).filter { $0.pathExtension == "swift" }
        XCTAssertFalse(files.isEmpty, "no Swift sources found at \(sourcesDirectory.path)")

        var violations: [String] = []
        for file in files.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
            let source = try String(contentsOf: file, encoding: .utf8)
            for finding in Self.rawHueViolations(in: source) {
                violations.append("\(file.lastPathComponent):\(finding.line): \(finding.snippet)")
            }
        }
        XCTAssertTrue(
            violations.isEmpty,
            "Raw system hues in views (use a Theme token such as Theme.green / Theme.onAccent):\n"
                + violations.joined(separator: "\n")
        )
    }

    /// The raw-hue scanner must catch explicit, shorthand and AppKit forms —
    /// including white text on an accent fill — and ignore tokens, `.clear`
    /// and comments.
    func testRawHueScannerCatchesKnownFormsAndIgnoresTokens() {
        let bad = """
        Text("Verified").foregroundStyle(Color.green)
        Circle().fill(.orange)
        Text("Open").foregroundStyle(.white).background(Theme.accent)
        let warning = NSColor.systemYellow
        .background(
            .red,
            in: RoundedRectangle(cornerRadius: 4)
        )
        """
        XCTAssertEqual(Self.rawHueViolations(in: bad).count, 5)

        let good = """
        Text("Verified").foregroundStyle(Theme.green)
        Text("Open").foregroundStyle(Theme.onAccent).background(Theme.accent)
        Rectangle().fill(Color.clear)
        .background(Theme.tintAmberOnCanvas)
        // Text("x").foregroundStyle(.white) in a comment
        """
        XCTAssertEqual(Self.rawHueViolations(in: good).count, 0)
    }

    private static let rawHues = [
        "green", "red", "orange", "yellow", "white", "black", "blue", "mint", "teal", "cyan",
        "pink", "purple", "brown", "indigo", "gray",
        "systemGreen", "systemRed", "systemOrange", "systemYellow", "systemBlue",
    ]

    static func rawHueViolations(in source: String) -> [Violation] {
        let stripped = stripLineComments(source)
        let hues = rawHues.joined(separator: "|")
        let patterns = [
            // Color.green / NSColor.systemRed
            "(?<![A-Za-z_.])(?:Color|NSColor)\\s*\\.\\s*(?:" + hues + ")\\b",
            // .foregroundStyle(.white) / .fill(.orange) / .background(\n .red …)
            "\\.(?:foregroundStyle|foregroundColor|fill|stroke|strokeBorder|background|tint|border)\\s*\\(\\s*\\.(?:" + hues + ")\\b",
        ].map { try! NSRegularExpression(pattern: $0) }
        let full = stripped as NSString
        var results: [Violation] = []
        for pattern in patterns {
            for hit in pattern.matches(in: stripped, range: NSRange(location: 0, length: full.length)) {
                let prefix = full.substring(to: hit.range.location)
                let line = prefix.reduce(1) { $1 == "\n" ? $0 + 1 : $0 }
                results.append(Violation(line: line, snippet: full.substring(with: hit.range)))
            }
        }
        return results.sorted { $0.line < $1.line }
    }

    struct Violation {
        let line: Int
        let snippet: String
    }

    static func violations(in source: String) -> [Violation] {
        let stripped = stripLineComments(source)
        let tokenPattern = try! NSRegularExpression(
            pattern: "Theme\\s*\\.\\s*(" + bannedTokens.joined(separator: "|") + ")\\s*\\.\\s*opacity\\s*\\("
        )
        let callPattern = try! NSRegularExpression(
            pattern: "\\.(" + paintCalls.joined(separator: "|") + ")\\s*\\("
        )

        var results: [Violation] = []
        var reported = Set<Int>()
        let full = stripped as NSString
        let utf16 = Array(stripped.utf16)
        for match in callPattern.matches(in: stripped, range: NSRange(location: 0, length: full.length)) {
            // Find the argument list of this call by balancing parentheses.
            let open = match.range.location + match.range.length - 1
            var depth = 0
            var close = open
            var index = open
            while index < utf16.count {
                if utf16[index] == 40 { depth += 1 }  // (
                if utf16[index] == 41 {  // )
                    depth -= 1
                    if depth == 0 { close = index; break }
                }
                index += 1
            }
            guard close > open else { continue }
            let argumentRange = NSRange(location: open, length: close - open + 1)
            for hit in tokenPattern.matches(in: stripped, range: argumentRange)
            where reported.insert(hit.range.location).inserted {
                let prefix = full.substring(to: hit.range.location)
                let line = prefix.reduce(1) { $1 == "\n" ? $0 + 1 : $0 }
                let snippet = full.substring(with: hit.range)
                results.append(Violation(line: line, snippet: snippet))
            }
        }
        return results.sorted { $0.line < $1.line }
    }

    /// Removes `//` comments (outside string literals) while keeping line
    /// breaks, so reported line numbers still match the file.
    private static func stripLineComments(_ source: String) -> String {
        source.split(separator: "\n", omittingEmptySubsequences: false).map { line -> String in
            var inString = false
            var previous: Character = " "
            var output = ""
            let iterator = Array(line)
            var index = 0
            while index < iterator.count {
                let character = iterator[index]
                if character == "\"" && previous != "\\" { inString.toggle() }
                if !inString && character == "/" && index + 1 < iterator.count && iterator[index + 1] == "/" {
                    break
                }
                output.append(character)
                previous = character
                index += 1
            }
            return output
        }.joined(separator: "\n")
    }
}

/// Contract for the B2 shared tokens and formatters that the view batches
/// build on. Contrast targets: 4.5:1 for text roles, 3:1 for fills and marks.
final class ThemeTokenContractTests: XCTestCase {
    func testFillAndSurfaceTokensMeetTheirContrastTargets() {
        typealias P = Theme.Palette
        let checks: [(String, Theme.AdaptiveColor, Theme.AdaptiveColor, Double)] = [
            ("amberFill on card", P.amberFill, P.card, 3),
            ("amberFill on meterTrack", P.amberFill, P.meterTrack, 3),
            ("amberFill on well", P.amberFill, P.well, 3),
            ("amber text on tintAmberOnCanvas", P.amber, P.tintAmberOnCanvas, 4.5),
            ("ink on tintAmberOnCanvas", P.ink, P.tintAmberOnCanvas, 4.5),
            ("muted on tintNeutralOnCanvas", P.muted, P.tintNeutralOnCanvas, 4.5),
            ("ink on tintNeutralOnCanvas", P.ink, P.tintNeutralOnCanvas, 4.5),
            ("ink on well", P.ink, P.well, 4.5),
            ("muted on well", P.muted, P.well, 4.5),
            ("chartNeutral on card", P.chartNeutral, P.card, 3),
            ("chartNeutral on well", P.chartNeutral, P.well, 3),
            ("onAccent on accentPressed", P.onAccent, P.accentPressed, 4.5),
            // A selected bar against the dimmed context bars (K48).
            ("chartBar vs chartBarDim", P.chartBar, P.chartBarDim, 3),
            ("chartBar on card", P.chartBar, P.card, 3),
            // The raised selection thumb carries ink labels (K43).
            ("ink on thumb", P.ink, P.thumb, 7),
            // A disabled primary button: muted on the neutral wash (K06).
            ("muted on tintNeutral (disabled primary)", P.muted, P.tintNeutral, 5),
            // The app text field's boundary (K15), on card and canvas.
            ("rule field boundary on card", P.rule, P.card, 3),
            ("rule field boundary on canvas", P.rule, P.canvas, 3),
        ]
        for scheme in [ColorScheme.light, .dark] {
            for (context, foreground, background, minimum) in checks {
                let ratio = Self.contrast(foreground.hex(for: scheme), background.hex(for: scheme))
                XCTAssertGreaterThanOrEqual(
                    ratio, minimum,
                    "\(context) in \(scheme) mode is \(String(format: "%.2f", ratio)):1"
                )
            }
        }
        // Dark mode keeps the amber text value as its fill (DESIGN.md).
        XCTAssertEqual(P.amberFill.darkHex, P.amber.darkHex)
    }

    func testWellSitsBetweenCardAndCanvas() {
        for scheme in [ColorScheme.light, .dark] {
            let well = Self.luminance(Theme.Palette.well.hex(for: scheme))
            let card = Self.luminance(Theme.Palette.card.hex(for: scheme))
            let canvas = Self.luminance(Theme.Palette.canvas.hex(for: scheme))
            XCTAssertTrue((min(card, canvas)...max(card, canvas)).contains(well), "well outside card..canvas in \(scheme)")
        }
    }

    /// A raised selection is LIGHTER than every tray it can sit on, by a
    /// visible margin, in both schemes (K43) — so a selected pill never
    /// vanishes (ΔE ≈ 1) or reads recessed in dark.
    func testThumbIsRaisedAboveEveryTray() {
        typealias P = Theme.Palette
        for scheme in [ColorScheme.light, .dark] {
            let thumb = P.thumb.hex(for: scheme)
            for (name, tray) in [("tintNeutral", P.tintNeutral), ("tintNeutralOnCanvas", P.tintNeutralOnCanvas)] {
                let trayHex = tray.hex(for: scheme)
                XCTAssertGreaterThanOrEqual(
                    Self.deltaE(thumb, trayHex), 7,
                    "thumb vs \(name) in \(scheme): ΔE \(String(format: "%.1f", Self.deltaE(thumb, trayHex)))"
                )
                XCTAssertGreaterThan(Self.luminance(thumb), Self.luminance(trayHex), "thumb must be raised over \(name) in \(scheme)")
            }
            // The hover preview sits strictly between the canvas tray and the thumb.
            let hover = Self.luminance(P.thumbHoverOnCanvas.hex(for: scheme))
            XCTAssertLessThan(hover, Self.luminance(thumb), "hover below thumb in \(scheme)")
            XCTAssertGreaterThan(hover, Self.luminance(P.tintNeutralOnCanvas.hex(for: scheme)), "hover above tray in \(scheme)")
        }
    }

    func testPercentShareMirrorsThePythonRule() {
        XCTAssertEqual(Fmt.percentShare(nil), "0%")
        XCTAssertEqual(Fmt.percentShare(0), "0%")
        XCTAssertEqual(Fmt.percentShare(-0.2), "0%")
        XCTAssertEqual(Fmt.percentShare(0.0001), "<1%")
        XCTAssertEqual(Fmt.percentShare(0.00499), "<1%")
        XCTAssertEqual(Fmt.percentShare(0.005), "1%")
        XCTAssertEqual(Fmt.percentShare(0.255), "26%")
        XCTAssertEqual(Fmt.percentShare(1), "100%")
    }

    func testAxisFormattersShareTheNumberGrammarAndStayShort() {
        // K39 (deliberate pin change): ticks are plain rounded scale values
        // with no cost glyph; the chart caption names the unit and basis.
        XCTAssertEqual(Fmt.axisAmount(0), "0.00")
        XCTAssertEqual(Fmt.axisAmount(9.994), "9.99")
        XCTAssertEqual(Fmt.axisAmount(1163.49), "1,163")
        XCTAssertEqual(Fmt.axisAmount(581.745, scale: 1163.49), "582")
        XCTAssertEqual(Fmt.axisAmount(0, scale: 1163.49), "0")
        XCTAssertEqual(Fmt.axisAmount(12_345), "12k")
        XCTAssertEqual(Fmt.axisAmount(1_250_000), "1.2M")
        XCTAssertEqual(Fmt.axisAmount(9e18), "9e18")
        for value in [0.0, 581.745, 1_163.49, 12_345] {
            XCTAssertFalse(Fmt.axisAmount(value).contains("$"), "a tick never carries a cost glyph")
        }
        for value in [0, 999, 1_000, 1_234, 99_999, 121_100_000, 999_999_999, 999_900_000_000_000, Double(Int.max)] {
            let text = Fmt.axisTokens(value)
            XCTAssertLessThanOrEqual(text.count, 5, "\(value) -> \(text)")
        }
        XCTAssertEqual(Fmt.axisTokens(121_100_000), "121M")
        XCTAssertEqual(Fmt.axisTokens(1_250), "1.2k")
        XCTAssertEqual(Fmt.axisTokens(2_000), "2k")
    }

    func testDisplayDateUsesTheSharedWords() {
        var components = DateComponents()
        components.year = 2026; components.month = 9; components.day = 14; components.hour = 12
        let date = Calendar.current.date(from: components)!
        XCTAssertEqual(Fmt.displayDate(date), "Sep 14")
        XCTAssertFalse(Fmt.clockTime(date).isEmpty)
    }

    func testDecisionTintIsOneLookup() {
        XCTAssertEqual(DecisionTintClass.forKey("blocked"), .danger)
        let blockedStep = WorkTimelineRecord(
            id: "s1", laneID: "l", laneTitle: "Lane", lineage: "root", kind: .step,
            title: "Blocked step", result: "blocked"
        )
        XCTAssertEqual(blockedStep.presentationTint, Theme.coral)
        var completedStep = blockedStep
        completedStep.result = "completed"
        XCTAssertEqual(completedStep.presentationTint, Theme.ink)
        XCTAssertEqual(Theme.statusColor("blocked"), Theme.coral)
        XCTAssertEqual(Theme.statusColor("finding"), Theme.coral)
    }

    /// The decision axis never wears evidence green — not even "Verified",
    /// whose evidence may be only self-checked.
    func testNoDecisionKeyWearsEvidenceGreen() throws {
        // Every decision word the vocabulary legend defines (fixture payload).
        let url = try XCTUnwrap(Bundle.module.url(forResource: "dashboard", withExtension: "json"))
        let legend = try XCTUnwrap(DashboardSnapshotFixture.load(from: url).tasks.decisionLegend)
        XCTAssertEqual(legend.decisions.count, 15)
        let keys: [String?] = legend.decisions.map(\.key) + ["started", "checkpoint", "completed", "unknown", nil]
        for key in keys {
            let tint = DecisionTintClass.forKey(key)
            XCTAssertNotEqual(tint.text, Theme.green, "\(key ?? "nil") text")
            XCTAssertNotEqual(tint.wash, Theme.tintGreen, "\(key ?? "nil") wash")
            XCTAssertNotEqual(Theme.statusColor(key), Theme.green, "\(key ?? "nil") status")
        }
        let verified = DecisionTintClass.forKey("verified")
        XCTAssertEqual(verified.text, Theme.ink)
        XCTAssertTrue(verified.outlined, "verified is set apart from the neutral family by its rule")
    }

    /// The decision axis never merges with the evidence tiers or the
    /// interactive voice (K03): no decision class shares a (text, wash) pair
    /// with any tier badge or with the accent control pair, it speaks only in
    /// ink, muted and coral, and every class is a distinct treatment.
    func testDecisionBadgesNeverShareATierPairOrTheControlPair() {
        let tierPairs: [(String, Color, Color)] = [
            "externally_verified", "independently_checked", "self_checked", "claimed", "unchecked", nil,
        ].map { grade in
            let style = EvidenceTierStyle.forGrade(grade)
            return (grade ?? "none", style.tint, style.tintBg)
        }
        var treatments: [(DecisionTintClass, Color, Color, DecisionTintClass.Border)] = []
        for decision in DecisionTintClass.allCases {
            for (grade, tint, wash) in tierPairs {
                XCTAssertFalse(
                    decision.text == tint && decision.wash == wash,
                    "decision \(decision) wears the \(grade) tier pair"
                )
            }
            XCTAssertFalse(decision.text == Theme.accent && decision.wash == Theme.tintAccent, "\(decision) wears the control pair")
            XCTAssertTrue([Theme.ink, Theme.muted, Theme.coral].contains(decision.text), "\(decision) text leaves ink/muted/coral")
            XCTAssertNotEqual(decision.wash, Theme.tintAccent, "\(decision) wash")
            XCTAssertNotEqual(decision.wash, Theme.tintAmber, "\(decision) wash")
            for other in treatments {
                XCTAssertFalse(
                    other.1 == decision.text && other.2 == decision.wash && other.3 == decision.border,
                    "\(decision) and \(other.0) render identically"
                )
            }
            treatments.append((decision, decision.text, decision.wash, decision.border))
        }
        XCTAssertEqual(Theme.statusColor("in_progress"), Theme.ink, "live progress is not cobalt")
        XCTAssertEqual(Theme.statusColor("ended_open"), Theme.muted, "an inferred stop is not amber")
    }

    /// Filled marks take fill weights (K47): the decision fill lookup never
    /// returns the amber text weight, and no view fills a shape or a stem
    /// with `Theme.amber` or the text-weight limit color.
    func testFilledMarksTakeTheFillWeight() throws {
        for decision in DecisionTintClass.allCases {
            XCTAssertNotEqual(decision.fill, Theme.amber, "\(decision) fill")
        }
        XCTAssertEqual(Theme.limitFillColor(usedPercent: 80), Theme.amberFill)
        XCTAssertEqual(Theme.limitFillColor(usedPercent: 10), Theme.chartNeutral, "a sub-threshold meter is data, not cobalt")
        XCTAssertNotEqual(Theme.limitFillColor(usedPercent: 10), Theme.accent)

        let pattern = try NSRegularExpression(
            pattern: #"(?:\.fill\(|stem:)[^\n]*(?:Theme\.amber(?!Fill)\b|limitTextColor)"#
        )
        var violations: [String] = []
        for (name, source) in try Self.sources() {
            let ns = source as NSString
            for hit in pattern.matches(in: source, range: NSRange(location: 0, length: ns.length)) {
                let line = ns.substring(to: hit.range.location).reduce(1) { $1 == "\n" ? $0 + 1 : $0 }
                violations.append("\(name):\(line)")
            }
        }
        XCTAssertTrue(violations.isEmpty, "text-weight color used as a fill:\n" + violations.joined(separator: "\n"))
    }

    /// The pip shapes are the evidence-tier grammar (K05): a pip is built only
    /// from a tier key, never from a free shape + tint for a gap, a step, a
    /// bullet or a connection state.
    func testEvidencePipIsConstructedOnlyFromATierKey() throws {
        let pattern = try NSRegularExpression(pattern: #"EvidencePip\((?!grade:)"#)
        var violations: [String] = []
        for (name, source) in try Self.sources() {
            let ns = source as NSString
            for hit in pattern.matches(in: source, range: NSRange(location: 0, length: ns.length)) {
                let line = ns.substring(to: hit.range.location).reduce(1) { $1 == "\n" ? $0 + 1 : $0 }
                violations.append("\(name):\(line)")
            }
        }
        XCTAssertTrue(violations.isEmpty, "EvidencePip built without a tier key:\n" + violations.joined(separator: "\n"))
        // Hollow tiers draw an outlined coverage segment (K90).
        XCTAssertTrue(CoverageSegmentMark.isHollow("unchecked"))
        XCTAssertTrue(CoverageSegmentMark.isHollow("claimed"))
        XCTAssertFalse(CoverageSegmentMark.isHollow("independently_checked"))
        XCTAssertFalse(CoverageSegmentMark.isHollow("self_checked"))
        XCTAssertFalse(CoverageSegmentMark.isHollow("externally_verified"))
    }

    /// Floating surfaces are opaque (K84): every `.popover` content applies
    /// `popoverSurface`, the menu panel paints the canvas, and no AppKit
    /// rounded-border text field (off-palette in dark, K15) remains.
    func testFloatingSurfacesAreOpaqueAndTextFieldsAreAppChrome() throws {
        var violations: [String] = []
        for (name, source) in try Self.sources() {
            let popovers = source.components(separatedBy: ".popover(").count - 1
            let surfaces = source.components(separatedBy: ".popoverSurface(").count - 1
            if popovers > surfaces { violations.append("\(name): \(popovers) popovers, \(surfaces) popoverSurface") }
            if source.contains(".textFieldStyle(.roundedBorder)") { violations.append("\(name): .roundedBorder text field") }
        }
        XCTAssertTrue(violations.isEmpty, violations.joined(separator: "\n"))
        let menu = try XCTUnwrap(try Self.sources().first { $0.0 == "MenuContent.swift" }?.1)
        XCTAssertTrue(menu.contains(".background(Theme.canvas)"), "the menu panel paints the opaque canvas")
    }

    /// A tinted quiet button colors its label with the tint it was given
    /// (K13); an untinted one leaves the label's own foreground alone.
    func testQuietButtonLabelTakesAnExplicitTint() {
        XCTAssertEqual(ButtonFeedback.labelColor(tint: Theme.accent), Theme.accent)
        XCTAssertEqual(ButtonFeedback.labelColor(tint: Theme.muted), Theme.muted)
        XCTAssertNil(ButtonFeedback.labelColor(tint: nil))
    }

    /// Filled chrome drops the accent when disabled (K06). Scoped to the
    /// filled primary chrome; quiet/surface styles keep their label fade.
    func testDisabledPrimaryChromeDropsTheAccentFill() {
        let disabled = ButtonFeedback.primaryChrome(for: .disabled)
        XCTAssertEqual(disabled.fill, Theme.tintNeutral)
        XCTAssertEqual(disabled.label, Theme.muted)
        XCTAssertNotEqual(disabled.fill, Theme.accent)
        XCTAssertEqual(ButtonFeedback.primaryChrome(for: .idle).fill, Theme.accent)
        XCTAssertEqual(ButtonFeedback.primaryChrome(for: .idle).label, Theme.onAccent)
        XCTAssertEqual(ButtonFeedback.primaryChrome(for: .pressed).fill, Theme.accentPressed)
    }

    /// The record reads WORK-first (C1). K17 used to put the computed proof
    /// clause one ramp step ABOVE the task title, so on a Task that had shipped
    /// and tested a function the largest string on the page was `Not gradeable
    /// (only step stopped: handed off)` — the grading system, not the work. The
    /// title now takes the top step and the consequence sits under it; nothing
    /// in the hero ramp may outrank the title, and no step is mono (prose).
    func testTheTaskTitleOutranksEveryOtherStepInTheHeroRamp() {
        for dense in [false, true] {
            let ramp = VerdictHeroTypeRamp(dense: dense)
            XCTAssertGreaterThan(ramp.title.metrics.size, ramp.consequence.metrics.size, "dense=\(dense)")
            XCTAssertFalse(ramp.title.metrics.monospaced)
            XCTAssertFalse(ramp.consequence.metrics.monospaced)
        }
    }

    /// And the proof clause is nowhere in that ramp: the record header must not
    /// render `verdict.badgeClause` at all — it is a tile figure now.
    func testTheHeroHeaderDoesNotRenderTheProofClause() throws {
        let source = try Self.sources().first { $0.0 == "WorkPane.swift" }?.1
        let pane = try XCTUnwrap(source)
        XCTAssertFalse(
            pane.contains("badgeClause"),
            "the record header renders the proof clause again — it belongs to the tile strip (C1)"
        )
    }

    private static func sources() throws -> [(String, String)] {
        let directory = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/agentacct", isDirectory: true)
        return try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "swift" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
            .map { ($0.lastPathComponent, try String(contentsOf: $0, encoding: .utf8)) }
    }

    func testPeriodBarsRestInFullChartBar() {
        XCTAssertEqual(Theme.periodBarColor(isActive: false, hasUserSelection: false), Theme.chartBar)
        XCTAssertEqual(Theme.periodBarColor(isActive: true, hasUserSelection: false), Theme.chartBar)
        XCTAssertEqual(Theme.periodBarColor(isActive: true, hasUserSelection: true), Theme.chartBar)
        XCTAssertEqual(Theme.periodBarColor(isActive: false, hasUserSelection: true), Theme.chartBarDim)
    }

    func testOnlyExternallyVerifiedTierIsGreenAndAmberTiersFillAtFillWeight() {
        for grade in ["independently_checked", "self_checked", "claimed", "unchecked", "strong", nil] {
            XCTAssertNotEqual(EvidenceTierStyle.forGrade(grade).tint, Theme.green, "\(grade ?? "nil")")
        }
        XCTAssertEqual(EvidenceTierStyle.forGrade("externally_verified").tint, Theme.green)
        XCTAssertEqual(EvidenceTierStyle.forGrade("unchecked").fill, Theme.amberFill)
        // A tier mark is DATA: `self_checked` used to fill in `Theme.accent`,
        // the one interactive voice, so the proven half of every coverage bar
        // read as a control (C2). It takes the chart voice; the half-pip SHAPE
        // is what carries the tier.
        XCTAssertEqual(EvidenceTierStyle.forGrade("self_checked").fill, Theme.chartBar)
        XCTAssertNotEqual(EvidenceTierStyle.forGrade("self_checked").tint, Theme.accent)
    }

    func testPeriodChartAxisPlacesLabelsOnGridlinesInsideThePlotHeight() {
        XCTAssertEqual(PeriodChartAxis.gridlineY(index: 0, count: 3, plotHeight: 110), 0)
        XCTAssertEqual(PeriodChartAxis.gridlineY(index: 1, count: 3, plotHeight: 110), 55)
        XCTAssertEqual(PeriodChartAxis.gridlineY(index: 2, count: 3, plotHeight: 110), 110)
    }

    func testHitMinimumScalesAndNeverShrinks() {
        XCTAssertEqual(ButtonFeedback.scaledMinimumHitDimension(for: .large), 28)
        XCTAssertEqual(ButtonFeedback.scaledMinimumHitDimension(for: .xSmall), 28)
        XCTAssertGreaterThan(ButtonFeedback.scaledMinimumHitDimension(for: .accessibility5), 60)
        XCTAssertGreaterThanOrEqual(Type.icon, 12)
        XCTAssertEqual(Metrics.pageMaxWidth, 1172 + 2 * Space.gutter)
    }

    /// CIE76 ΔE in CIELAB (D65), the separation measure the palette
    /// validator reports.
    static func deltaE(_ a: UInt32, _ b: UInt32) -> Double {
        func lab(_ hex: UInt32) -> (Double, Double, Double) {
            func channel(_ shift: UInt32) -> Double {
                let c = Double((hex >> shift) & 0xFF) / 255
                return c <= 0.04045 ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4)
            }
            let (r, g, bl) = (channel(16), channel(8), channel(0))
            let x = (0.4124 * r + 0.3576 * g + 0.1805 * bl) / 0.95047
            let y = 0.2126 * r + 0.7152 * g + 0.0722 * bl
            let z = (0.0193 * r + 0.1192 * g + 0.9505 * bl) / 1.08883
            func f(_ t: Double) -> Double { t > 216.0 / 24389 ? cbrt(t) : (24389.0 / 27 * t + 16) / 116 }
            return (116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z)))
        }
        let (la, aa, ba) = lab(a)
        let (lb, ab, bb) = lab(b)
        return ((la - lb) * (la - lb) + (aa - ab) * (aa - ab) + (ba - bb) * (ba - bb)).squareRoot()
    }

    private static func luminance(_ hex: UInt32) -> Double {
        func channel(_ shift: UInt32) -> Double {
            let c = Double((hex >> shift) & 0xFF) / 255
            return c <= 0.04045 ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4)
        }
        return 0.2126 * channel(16) + 0.7152 * channel(8) + 0.0722 * channel(0)
    }

    private static func contrast(_ a: UInt32, _ b: UInt32) -> Double {
        let (la, lb) = (luminance(a), luminance(b))
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)
    }
}
