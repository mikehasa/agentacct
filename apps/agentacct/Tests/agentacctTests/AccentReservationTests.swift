import Foundation
import SwiftUI
import XCTest
@testable import agentacct

/// The cobalt accent is the app's ONE interactive voice (K04). A data mark —
/// a chart bar, a distribution slice, a meter fill, a timeline stem — must
/// therefore never be painted in it: a reader who has learned "cobalt means I
/// can press this" would read the chart as a control.
///
/// Two ways that rule can be broken, and one guard each:
///
/// 1. a chart TOKEN drifts to the accent's value (how K04 happened: both
///    `chartBar` and `chartCatRead` were literally `accent`);
/// 2. a VIEW paints a mark with `Theme.accent` directly.
///
/// The separation floor is OKLab ΔE×100 ≥ 12, measured in both schemes. The
/// scale is calibrated against the palette's own two reference points:
/// `accentPressed` — the deliberate darker SHADE of the accent, which must not
/// pass — is ΔE 6.8 from it, and the data-viz validator's "a full-colour reader
/// can tell these apart" floor is 15.
final class AccentReservationTests: XCTestCase {
    /// Floor between a data-mark token and the accent, OKLab ΔE×100.
    static let separationFloor = 12.0

    /// Every token that can end up painting a data mark, by name.
    /// `testEveryDeclaredChartTokenIsCovered` fails if a new one is added to
    /// the palette without being listed here.
    static let dataMarkTokens: [(String, Theme.AdaptiveColor)] = [
        ("chartBar", Theme.Palette.chartBar),
        ("chartBarDim", Theme.Palette.chartBarDim),
        ("chartCatRead", Theme.Palette.chartCatRead),
        ("chartCatExecute", Theme.Palette.chartCatExecute),
        ("chartCatEdit", Theme.Palette.chartCatEdit),
        ("chartNeutral", Theme.Palette.chartNeutral),
        // The source-identity hues the Worksets timeline paints its lanes with.
        // They are categorical data marks, so they sit under the same accent
        // floor as every other chart token.
        ("chartSourceClaude", Theme.Palette.chartSourceClaude),
        ("chartSourceCodex", Theme.Palette.chartSourceCodex),
        ("chartSourceOpencode", Theme.Palette.chartSourceOpencode),
        ("chartSourceHermes", Theme.Palette.chartSourceHermes),
    ]

    func testNoDataMarkTokenWearsTheInteractiveVoice() {
        for scheme in [ColorScheme.light, .dark] {
            for (markName, mark) in Self.dataMarkTokens {
                let markHex = mark.hex(for: scheme)
                // The floor is measured against the RESTING interactive voice —
                // the colour a reader has learned means "pressable". The
                // pressed shade only exists under the cursor, so it is held to
                // the weaker rule: a data mark may not BE it.
                XCTAssertNotEqual(
                    markHex, Theme.Palette.accentPressed.hex(for: scheme),
                    "\(markName) IS accentPressed in \(scheme) mode — a data mark in the interactive voice"
                )
                let accentHex = Theme.Palette.accent.hex(for: scheme)
                XCTAssertNotEqual(
                    markHex, accentHex,
                    "\(markName) IS accent in \(scheme) mode — a data mark in the interactive voice"
                )
                let separation = Self.deltaE(markHex, accentHex)
                XCTAssertGreaterThanOrEqual(
                    separation, Self.separationFloor,
                    "\(markName) vs accent in \(scheme) mode is ΔE "
                        + String(format: "%.1f", separation)
                        + " — inside the accent's own shade family (floor \(Self.separationFloor))"
                )
            }
        }
    }

    /// The floor is a real threshold, not a value every pair happens to clear:
    /// the accent's own pressed shade must fail it.
    func testTheFloorRejectsAShadeOfTheAccent() {
        for scheme in [ColorScheme.light, .dark] {
            let shade = Self.deltaE(
                Theme.Palette.accentPressed.hex(for: scheme),
                Theme.Palette.accent.hex(for: scheme)
            )
            XCTAssertLessThan(
                shade, Self.separationFloor,
                "accentPressed in \(scheme) is ΔE \(String(format: "%.1f", shade)) from accent — "
                    + "the floor no longer separates a data mark from a shade of the accent"
            )
        }
    }

    /// A chart token added to the palette must be declared above, so it cannot
    /// slip past the separation floor by not being listed.
    func testEveryDeclaredChartTokenIsCovered() throws {
        let source = try String(contentsOf: Self.themeURL, encoding: .utf8)
        let pattern = try NSRegularExpression(pattern: #"static let (chart[A-Za-z]*) = AdaptiveColor"#)
        let range = NSRange(location: 0, length: (source as NSString).length)
        let declared = pattern.matches(in: source, range: range).map {
            (source as NSString).substring(with: $0.range(at: 1))
        }
        XCTAssertFalse(declared.isEmpty, "no chart tokens found in Theme.swift")
        let covered = Set(Self.dataMarkTokens.map(\.0))
        XCTAssertEqual(
            Set(declared).subtracting(covered), [],
            "chart tokens declared in the palette but not checked against the accent"
        )
    }

    /// No view paints a chart mark with `Theme.accent` directly: the mark
    /// tokens above are the only way a bar, slice, stem or plot gets its color.
    func testNoViewPaintsAChartMarkWithTheAccent() throws {
        var violations: [String] = []
        for (name, source) in try Self.sources() {
            for line in Self.accentMarkViolations(in: source) {
                violations.append("\(name):\(line)")
            }
        }
        XCTAssertTrue(
            violations.isEmpty,
            "chart marks painted in the interactive accent (use a Theme.chart* token):\n"
                + violations.joined(separator: "\n")
        )
    }

    /// The scanner catches a mark painted cobalt and leaves controls alone.
    func testMarkScannerCatchesKnownFormsAndIgnoresControls() {
        let bad = """
        Rectangle().fill(Theme.accent).frame(height: barHeight)
        context.fill(slicePath, with: .color(Theme.accent))
        MeterBar(fraction: share, tint: Theme.accent, height: 6)
        """
        XCTAssertEqual(Self.accentMarkViolations(in: bad).count, 3)

        // The shape K04 actually hid in: the token sits alone on its line and
        // only the neighbouring lines say it is a bar.
        let splitAcrossLines = """
        private func actionDistributionRow(_ metric: ReceiptActionMetric) -> some View {
            GeometryReader { proxy in
                Rectangle()
                    .fill(Theme.accent)
                    .frame(width: proxy.size.width * fraction, height: 4)
            }
            .frame(height: 6)
        }
        """
        XCTAssertEqual(Self.accentMarkViolations(in: splitAcrossLines).count, 1)

        let good = """
        Rectangle().fill(Theme.chartBar).frame(height: barHeight)
        Button(action: open) { label }.buttonStyle(QuietButtonStyle(tint: Theme.accent))
        RoundedRectangle(cornerRadius: 4).strokeBorder(Theme.accent, lineWidth: Metrics.focusW)
        """
        XCTAssertEqual(Self.accentMarkViolations(in: good).count, 0)
    }

    private static let markWords = try! NSRegularExpression(
        pattern: #"\b(bar|slice|segment|series|plot|stem|spine|histogram|gridline|chart|meter|sparkline)[A-Za-z]*\b"#,
        options: .caseInsensitive
    )
    private static let accentToken = try! NSRegularExpression(pattern: #"\bTheme\.accent\b"#)
    /// A shape whose EXTENT is computed from data is a data mark, whatever the
    /// surrounding identifiers are called. This is what actually separates a
    /// bar from a 4pt selection rail or a 10pt chrome dot, whose sizes are
    /// literals.
    private static let dataExtent = try! NSRegularExpression(
        pattern: #"\b(fraction|share|ratio|percent|proportion)\b|size\.(width|height)\s*\*|CGFloat\(\s*(count|total)"#,
        options: .caseInsensitive
    )
    private static let paintWords = try! NSRegularExpression(
        pattern: #"\b(fill|stroke|strokeBorder|background|tint|color)\b"#
    )
    /// Chrome that legitimately wears the accent even next to a chart: the
    /// focus ring, a button style, a selection outline.
    private static let controlWords = try! NSRegularExpression(
        pattern: #"\b(focus|Button|ButtonStyle|selected|Selection|ring|Metrics\.focusW)\b"#,
        options: .caseInsensitive
    )

    /// A paint of `Theme.accent` counts as a MARK when the statement around it
    /// names one OR sizes the shape from data. SwiftUI splits a mark across lines — `Rectangle()` / `.fill(…)`
    /// / `.frame(width: … * fraction, height: 4)` — so the window is the few
    /// lines either side, not the one line carrying the token (K04 hid in
    /// exactly that gap: `.fill(Theme.accent)` alone on its line inside
    /// `actionDistributionRow`).
    static let markContextLines = 4

    static func accentMarkViolations(in source: String) -> [Int] {
        let lines = source.components(separatedBy: "\n").map { $0.components(separatedBy: "//").first ?? $0 }
        var results: [Int] = []
        for (index, code) in lines.enumerated() {
            let range = NSRange(location: 0, length: (code as NSString).length)
            guard accentToken.firstMatch(in: code, range: range) != nil else { continue }
            guard paintWords.firstMatch(in: code, range: range) != nil else { continue }
            let lower = max(0, index - markContextLines)
            let upper = min(lines.count - 1, index + markContextLines)
            let window = lines[lower...upper].joined(separator: "\n")
            let windowRange = NSRange(location: 0, length: (window as NSString).length)
            let namesAMark = markWords.firstMatch(in: window, range: windowRange) != nil
            let sizedByData = dataExtent.firstMatch(in: window, range: windowRange) != nil
            guard namesAMark || sizedByData else { continue }
            if controlWords.firstMatch(in: window, range: windowRange) != nil { continue }
            results.append(index + 1)
        }
        return results
    }

    private static var themeURL: URL {
        sourcesDirectory.appendingPathComponent("Theme.swift")
    }

    private static var sourcesDirectory: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/agentacct", isDirectory: true)
    }

    private static func sources() throws -> [(String, String)] {
        try FileManager.default.contentsOfDirectory(at: sourcesDirectory, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "swift" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
            .map { ($0.lastPathComponent, try String(contentsOf: $0, encoding: .utf8)) }
    }

    /// OKLab ΔE×100 — the same measure the data-viz palette validator reports,
    /// so the floor here and the validator's floors are on one scale.
    static func deltaE(_ a: UInt32, _ b: UInt32) -> Double {
        func oklab(_ hex: UInt32) -> (Double, Double, Double) {
            func channel(_ shift: UInt32) -> Double {
                let c = Double((hex >> shift) & 0xFF) / 255
                return c <= 0.04045 ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4)
            }
            let (r, g, b) = (channel(16), channel(8), channel(0))
            let l = cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
            let m = cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
            let s = cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
            return (
                0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
                1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
                0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s
            )
        }
        let (la, aa, ba) = oklab(a)
        let (lb, ab, bb) = oklab(b)
        return 100 * ((la - lb) * (la - lb) + (aa - ab) * (aa - ab) + (ba - bb) * (ba - bb)).squareRoot()
    }
}
