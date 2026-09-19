import SwiftUI
import XCTest
@testable import agentacct

/// Every `Theme.Palette` token, pinned to its exact 8-bit value.
///
/// This is a HOST-INDEPENDENT guard, and it is deliberately the strictest test
/// of colour in the suite: it fails on a ONE-LEVEL change to any channel of any
/// token, in either scheme.
///
/// It exists because the pixel comparator cannot do this job. Its area budget
/// (`VisualSnapshotTolerance.crossMinorRenderingNoise`, 0.003) has to be wide
/// enough to absorb antialiasing drift between macOS minor versions, and a
/// one-level token change on a small or medium element moves fewer channels
/// than that budget allows while never exceeding the delta-1 ceiling either —
/// so it passes the pixel suite. A table of numbers read as data catches it
/// perfectly, where four megapixels of antialiasing cannot.
///
/// A deliberate palette change updates DESIGN.md and this table in the same
/// commit. That is the point: a colour change should be a reviewed edit to a
/// named list, never an unremarked pixel difference.
final class PaletteValueLintTests: XCTestCase {
    private static let pinned: [(name: String, token: Theme.AdaptiveColor, light: UInt32, dark: UInt32)] = [
        ("canvas", Theme.Palette.canvas, 0xF4F1E9, 0x0D1215),
        ("chrome", Theme.Palette.chrome, 0xFCFBF7, 0x141B1F),
        ("card", Theme.Palette.card, 0xFFFFFF, 0x1B252A),
        ("selected", Theme.Palette.selected, 0xE7EDF8, 0x223049),
        ("ink", Theme.Palette.ink, 0x171A1D, 0xF2F4F3),
        ("muted", Theme.Palette.muted, 0x59636B, 0xA5B0B4),
        ("accent", Theme.Palette.accent, 0x245BDB, 0x82A6FF),
        ("green", Theme.Palette.green, 0x1F7653, 0x78D5A8),
        ("amber", Theme.Palette.amber, 0x7A5A00, 0xE7C66A),
        ("coral", Theme.Palette.coral, 0xB63F2F, 0xFF9B88),
        ("amberFill", Theme.Palette.amberFill, 0x967000, 0xE7C66A),
        ("accentPressed", Theme.Palette.accentPressed, 0x1D4BB8, 0x6F95F2),
        ("tintNeutral", Theme.Palette.tintNeutral, 0xEDEBE3, 0x2A343B),
        ("tintAccent", Theme.Palette.tintAccent, 0xE8EEFB, 0x24365C),
        ("tintGreen", Theme.Palette.tintGreen, 0xE2F0E9, 0x1E3B2F),
        ("tintAmber", Theme.Palette.tintAmber, 0xF7EFDA, 0x3D3420),
        ("tintCoral", Theme.Palette.tintCoral, 0xF8E5E1, 0x412620),
        ("chipBg", Theme.Palette.chipBg, 0xF7F5F0, 0x232E34),
        ("tintAmberOnCanvas", Theme.Palette.tintAmberOnCanvas, 0xF2E6C4, 0x33291A),
        ("tintNeutralOnCanvas", Theme.Palette.tintNeutralOnCanvas, 0xEBE7DC, 0x1A2328),
        ("well", Theme.Palette.well, 0xF9F7F2, 0x151D21),
        ("thumb", Theme.Palette.thumb, 0xFFFFFF, 0x3A464D),
        ("thumbHoverOnCanvas", Theme.Palette.thumbHoverOnCanvas, 0xF6F4EF, 0x2C363C),
        ("meterTrack", Theme.Palette.meterTrack, 0xD9D4C6, 0x34404A),
        ("rule", Theme.Palette.rule, 0x79848B, 0x68777D),
        ("hairline", Theme.Palette.hairline, 0xE4E1D7, 0x2C363C),
        ("cardLine", Theme.Palette.cardLine, 0xDDDACF, 0x313D44),
        ("chipLine", Theme.Palette.chipLine, 0xD8D5CC, 0x3B474E),
        ("chartBar", Theme.Palette.chartBar, 0x7040AA, 0xA872EC),
        ("chartBarDim", Theme.Palette.chartBarDim, 0xCDBCE4, 0x453365),
        ("chartCatRead", Theme.Palette.chartCatRead, 0x7040AA, 0xA872EC),
        ("chartCatExecute", Theme.Palette.chartCatExecute, 0xD9457F, 0xCF4F7C),
        ("chartCatEdit", Theme.Palette.chartCatEdit, 0x1596B4, 0x10A6B0),
        ("chartNeutral", Theme.Palette.chartNeutral, 0x484F54, 0xB0BABE),
        ("chartSourceClaude", Theme.Palette.chartSourceClaude, 0x682AA0, 0x9E5CE6),
        ("chartSourceCodex", Theme.Palette.chartSourceCodex, 0x741146, 0xD24B93),
        ("chartSourceOpencode", Theme.Palette.chartSourceOpencode, 0x0E8494, 0x53C6D6),
        ("chartSourceHermes", Theme.Palette.chartSourceHermes, 0xA5457F, 0xE39AC8),
        ("onAccent", Theme.Palette.onAccent, 0xFFFFFF, 0x0D1215),
    ]

    func testEveryPaletteTokenKeepsItsExactValue() {
        for entry in Self.pinned {
            XCTAssertEqual(
                entry.token.lightHex,
                entry.light,
                """
                Theme.Palette.\(entry.name) light value changed from \
                \(Self.hex(entry.light)) to \(Self.hex(entry.token.lightHex)). \
                A palette change is a reviewed design decision: update DESIGN.md \
                and this table together, or revert the token.
                """
            )
            XCTAssertEqual(
                entry.token.darkHex,
                entry.dark,
                """
                Theme.Palette.\(entry.name) dark value changed from \
                \(Self.hex(entry.dark)) to \(Self.hex(entry.token.darkHex)). \
                A palette change is a reviewed design decision: update DESIGN.md \
                and this table together, or revert the token.
                """
            )
        }
    }

    /// The table has to cover the whole palette, or a token could be added and
    /// then drift unwatched.
    func testEveryPaletteTokenIsPinned() throws {
        let source = try String(
            contentsOf: URL(fileURLWithPath: #filePath)
                .deletingLastPathComponent()
                .deletingLastPathComponent()
                .deletingLastPathComponent()
                .appendingPathComponent("Sources/agentacct/Theme.swift"),
            encoding: .utf8
        )
        guard let range = source.range(of: "enum Palette {"),
              let end = source.range(of: "\n    }", range: range.upperBound..<source.endIndex) else {
            return XCTFail("Could not locate Theme.Palette in Theme.swift")
        }
        let declared = source[range.upperBound..<end.lowerBound]
            .components(separatedBy: "\n")
            .compactMap { line -> String? in
                let trimmed = line.trimmingCharacters(in: .whitespaces)
                guard trimmed.hasPrefix("static let "),
                      trimmed.contains("AdaptiveColor(lightHex:") else { return nil }
                return trimmed
                    .dropFirst("static let ".count)
                    .prefix(while: { $0 != " " })
                    .description
            }
        XCTAssertEqual(
            Set(declared),
            Set(Self.pinned.map(\.name)),
            "Theme.Palette and the pinned table disagree. Add every new token here."
        )
    }

    private static func hex(_ value: UInt32) -> String {
        String(format: "#%06X", value)
    }
}
