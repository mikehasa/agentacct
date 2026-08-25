import SwiftUI
import XCTest
@testable import agentacct

final class ThemeContrastTests: XCTestCase {
    private let minimumSmallTextContrast = 4.5

    func testSmallTextRolesMeetContrastTargetOnDashboardSurfaces() {
        let foregrounds: [(String, Theme.AdaptiveColor)] = [
            ("text", Theme.Palette.text),
            ("textMuted", Theme.Palette.textMuted),
            ("textFaint", Theme.Palette.textFaint),
        ]
        let surfaces: [(String, Theme.AdaptiveColor)] = [
            ("bg", Theme.Palette.bg),
            ("surface", Theme.Palette.surface),
            ("card", Theme.Palette.card),
            ("cardAlt", Theme.Palette.cardAlt),
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

    func testSemanticColorsMeetContrastTargetInSmallChips() {
        let semanticColors: [(String, Theme.AdaptiveColor)] = [
            ("accent", Theme.Palette.accent),
            ("blue", Theme.Palette.blue),
            ("purple", Theme.Palette.purple),
            ("green", Theme.Palette.green),
            ("codex", Theme.Palette.codex),
            ("orange", Theme.Palette.orange),
            ("red", Theme.Palette.red),
            ("cyan", Theme.Palette.cyan),
        ]
        let chipStyles = [
            (name: "chip", backgroundOpacity: Chip.backgroundOpacity),
            (name: "axis chip", backgroundOpacity: AxisChip.backgroundOpacity),
        ]

        for scheme in [ColorScheme.light, .dark] {
            // Both chip styles are currently used only on card-backed rows and
            // panels. Add another host surface here before placing one elsewhere.
            let card = Theme.Palette.card.hex(for: scheme)
            for style in chipStyles {
                for (name, semanticColor) in semanticColors {
                    let foreground = semanticColor.hex(for: scheme)
                    let chipBackground = composite(
                        foreground: foreground,
                        background: card,
                        opacity: style.backgroundOpacity
                    )
                    assertContrast(
                        foreground,
                        against: chipBackground,
                        minimum: minimumSmallTextContrast,
                        context: "\(name) \(style.name) in \(scheme) mode"
                    )
                }
            }
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

    private func composite(foreground: UInt32, background: UInt32, opacity: Double) -> UInt32 {
        let red = compositeChannel(foreground >> 16, background >> 16, opacity: opacity)
        let green = compositeChannel(foreground >> 8, background >> 8, opacity: opacity)
        let blue = compositeChannel(foreground, background, opacity: opacity)
        return (red << 16) | (green << 8) | blue
    }

    private func compositeChannel(
        _ foreground: UInt32,
        _ background: UInt32,
        opacity: Double
    ) -> UInt32 {
        let foregroundChannel = Double(foreground & 0xFF)
        let backgroundChannel = Double(background & 0xFF)
        let blended = foregroundChannel * opacity + backgroundChannel * (1 - opacity)
        return UInt32(blended.rounded())
    }
}
