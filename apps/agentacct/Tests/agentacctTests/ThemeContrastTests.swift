import SwiftUI
import XCTest
@testable import agentacct

final class ThemeContrastTests: XCTestCase {
    private let minimumSmallTextContrast = 4.5

    func testTextRolesMeetContrastTargetOnEverySurface() {
        let foregrounds: [(String, Theme.AdaptiveColor)] = [
            ("ink", Theme.Palette.ink),
            ("muted", Theme.Palette.muted),
        ]
        let surfaces: [(String, Theme.AdaptiveColor)] = [
            ("canvas", Theme.Palette.canvas),
            ("chrome", Theme.Palette.chrome),
            ("card", Theme.Palette.card),
            ("selected", Theme.Palette.selected),
        ]

        for scheme in [ColorScheme.light, .dark] {
            for (foregroundName, foreground) in foregrounds {
                for (surfaceName, surface) in surfaces {
                    assertContrast(
                        foreground.hex(for: scheme),
                        against: surface.hex(for: scheme),
                        minimum: minimumSmallTextContrast,
                        context: "\(foregroundName) on \(surfaceName) in \(scheme) mode"
                    )
                }
            }
        }
    }

    /// Badge and chip text sits on a solid tint wash (v7 badges composite
    /// nothing — the tint is an exact hex), so every text-on-tint pairing the
    /// components produce is asserted directly.
    func testBadgeTextMeetsContrastTargetOnItsTint() {
        let pairings: [(String, Theme.AdaptiveColor, Theme.AdaptiveColor)] = [
            ("accent on tintAccent", Theme.Palette.accent, Theme.Palette.tintAccent),
            ("green on tintGreen", Theme.Palette.green, Theme.Palette.tintGreen),
            ("amber on tintAmber", Theme.Palette.amber, Theme.Palette.tintAmber),
            ("coral on tintCoral", Theme.Palette.coral, Theme.Palette.tintCoral),
            ("ink on tintNeutral", Theme.Palette.ink, Theme.Palette.tintNeutral),
            ("muted on tintNeutral", Theme.Palette.muted, Theme.Palette.tintNeutral),
            ("ink on chip", Theme.Palette.ink, Theme.Palette.chipBg),
            ("muted on chip", Theme.Palette.muted, Theme.Palette.chipBg),
            ("accent on chip", Theme.Palette.accent, Theme.Palette.chipBg),
        ]

        for scheme in [ColorScheme.light, .dark] {
            for (context, foreground, tint) in pairings {
                assertContrast(
                    foreground.hex(for: scheme),
                    against: tint.hex(for: scheme),
                    minimum: minimumSmallTextContrast,
                    context: "\(context) in \(scheme) mode"
                )
            }
        }
    }

    /// Semantic accents also appear as plain text (links, annotations,
    /// threshold percentages) on the three main surfaces.
    func testSemanticTextMeetsContrastTargetOnSurfaces() {
        let accents: [(String, Theme.AdaptiveColor)] = [
            ("accent", Theme.Palette.accent),
            ("green", Theme.Palette.green),
            ("amber", Theme.Palette.amber),
            ("coral", Theme.Palette.coral),
        ]
        let surfaces: [(String, Theme.AdaptiveColor)] = [
            ("canvas", Theme.Palette.canvas),
            ("chrome", Theme.Palette.chrome),
            ("card", Theme.Palette.card),
        ]

        for scheme in [ColorScheme.light, .dark] {
            for (accentName, accent) in accents {
                for (surfaceName, surface) in surfaces {
                    assertContrast(
                        accent.hex(for: scheme),
                        against: surface.hex(for: scheme),
                        minimum: minimumSmallTextContrast,
                        context: "\(accentName) on \(surfaceName) in \(scheme) mode"
                    )
                }
            }
        }
    }

    /// Primary buttons fill with the accent; their copy must stay legible in
    /// both schemes (white on cobalt / near-black on the lighter dark cobalt).
    func testPrimaryButtonCopyMeetsContrastTarget() {
        for scheme in [ColorScheme.light, .dark] {
            assertContrast(
                Theme.Palette.onAccent.hex(for: scheme),
                against: Theme.Palette.accent.hex(for: scheme),
                minimum: minimumSmallTextContrast,
                context: "onAccent on accent in \(scheme) mode"
            )
        }
    }

    private func assertContrast(
        _ foreground: UInt32,
        against background: UInt32,
        minimum: Double,
        context: String,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        let ratio = contrastRatio(foreground, background)
        XCTAssertGreaterThanOrEqual(
            ratio,
            minimum,
            "\(context) is \(String(format: "%.2f", ratio)):1; expected at least \(minimum):1",
            file: file,
            line: line
        )
    }

    private func contrastRatio(_ first: UInt32, _ second: UInt32) -> Double {
        let firstLuminance = relativeLuminance(first)
        let secondLuminance = relativeLuminance(second)
        let lighter = max(firstLuminance, secondLuminance)
        let darker = min(firstLuminance, secondLuminance)
        return (lighter + 0.05) / (darker + 0.05)
    }

    private func relativeLuminance(_ hex: UInt32) -> Double {
        let red = linearComponent(Double((hex >> 16) & 0xFF) / 255)
        let green = linearComponent(Double((hex >> 8) & 0xFF) / 255)
        let blue = linearComponent(Double(hex & 0xFF) / 255)
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue
    }

    private func linearComponent(_ component: Double) -> Double {
        component <= 0.04045
            ? component / 12.92
            : pow((component + 0.055) / 1.055, 2.4)
    }
}
