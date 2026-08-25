import AppKit
import SwiftUI

// The agentacct visual system — semantic tokens with a light and a dark value
// each, resolved live from the system appearance. Neutral surfaces keep work
// evidence legible; one restrained indigo carries interaction, while status
// colors retain a single semantic meaning. Updating numbers use tabular digits.
//
// Rules of the road:
// * Repeated color and text roles use Theme/Type tokens. Component-specific
//   optical sizes stay beside the symbol or control they tune.
// * Updating metrics use tabular digits so values do not shift horizontally;
//   prose and one-off numbers retain proportional spacing.
// * No `.preferredColorScheme` locks anywhere: the app follows the system,
//   and snapshots pin the scheme explicitly via `SnapshotScheme`.

enum Fmt {
    static let usd: NumberFormatter = {
        let formatter = NumberFormatter()
        formatter.numberStyle = .decimal
        formatter.minimumFractionDigits = 2
        formatter.maximumFractionDigits = 2
        formatter.groupingSeparator = ","
        formatter.usesGroupingSeparator = true
        return formatter
    }()

    static func dollars(_ value: Double, prefix: String = "$") -> String {
        prefix + (usd.string(from: NSNumber(value: value)) ?? String(format: "%.2f", value))
    }
}

/// Snapshot-only scheme pin: offscreen ImageRenderer resolves dynamic NSColors
/// against whatever appearance the process has, so deterministic light/dark
/// renders set this override alongside `.environment(\.colorScheme, ...)`.
/// The live app leaves it nil and follows the system.
enum SnapshotScheme {
    nonisolated(unsafe) static var override: ColorScheme? = nil
}

enum Theme {
    // MARK: dynamic resolution

    private static func rgb(_ hex: UInt32, _ alpha: CGFloat = 1) -> NSColor {
        NSColor(
            srgbRed: CGFloat((hex >> 16) & 0xFF) / 255,
            green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255,
            alpha: alpha
        )
    }

    /// A semantic color with a light and a dark value. Resolves per-draw via
    /// the appearance (live app) or the snapshot override (offscreen).
    static func dynamic(light: UInt32, dark: UInt32) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            if let pinned = SnapshotScheme.override {
                return pinned == .dark ? rgb(dark) : rgb(light)
            }
            let isDark = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
            return isDark ? rgb(dark) : rgb(light)
        })
    }

    /// A semantic color's inspectable light/dark source values. Keeping the
    /// values separate from SwiftUI's dynamic resolver lets accessibility
    /// tests verify the actual palette instead of sampling rendered pixels.
    struct AdaptiveColor {
        let lightHex: UInt32
        let darkHex: UInt32

        var color: Color {
            Theme.dynamic(light: lightHex, dark: darkHex)
        }

        func hex(for scheme: ColorScheme) -> UInt32 {
            scheme == .dark ? darkHex : lightHex
        }
    }

    enum Palette {
        // Surfaces
        static let bg = AdaptiveColor(lightHex: 0xF6F7F9, darkHex: 0x101114)
        static let surface = AdaptiveColor(lightHex: 0xFFFFFF, darkHex: 0x18191E)
        static let card = AdaptiveColor(lightHex: 0xFFFFFF, darkHex: 0x18191E)
        static let cardAlt = AdaptiveColor(lightHex: 0xF1F2F5, darkHex: 0x202128)
        static let border = AdaptiveColor(lightHex: 0xE0E2E7, darkHex: 0x34363E)

        // Text
        static let text = AdaptiveColor(lightHex: 0x19191D, darkHex: 0xF4F4F6)
        static let textMuted = AdaptiveColor(lightHex: 0x5F606A, darkHex: 0xB1B2BA)
        static let textFaint = AdaptiveColor(lightHex: 0x6B6D77, darkHex: 0x8E909A)

        // Brand and semantic accents
        static let accent = AdaptiveColor(lightHex: 0x5B5BD6, darkHex: 0xA7A5FF)
        static let blue = AdaptiveColor(lightHex: 0x4545B8, darkHex: 0xA7A5FF)
        static let purple = AdaptiveColor(lightHex: 0x7048B6, darkHex: 0xBB9AF7)
        // Success is deliberately quieter than the previous lime. Codex has
        // its own identity color below so green retains one semantic meaning.
        static let green = AdaptiveColor(lightHex: 0x0B7A66, darkHex: 0x62C8AD)
        static let codex = AdaptiveColor(lightHex: 0x44647A, darkHex: 0x839CB2)
        static let orange = AdaptiveColor(lightHex: 0x925A08, darkHex: 0xF1BB67)
        static let red = AdaptiveColor(lightHex: 0xC73552, darkHex: 0xFF7D96)
        static let cyan = AdaptiveColor(lightHex: 0x0E7490, darkHex: 0x7DCFFF)
    }

    // MARK: surfaces

    static let bg = Palette.bg.color
    static let surface = Palette.surface.color
    static let card = Palette.card.color
    static let cardAlt = Palette.cardAlt.color
    static let border = Palette.border.color

    // MARK: text

    static let text = Palette.text.color
    static let textMuted = Palette.textMuted.color
    static let textFaint = Palette.textFaint.color

    // MARK: accents — one restrained indigo carries actions and selection.
    // Client identity colors remain separate from semantic success/failure.

    static let accent = Palette.accent.color
    static let blue = Palette.blue.color
    static let purple = Palette.purple.color
    static let green = Palette.green.color
    static let codex = Palette.codex.color
    static let orange = Palette.orange.color
    static let red = Palette.red.color
    static let cyan = Palette.cyan.color

    static func clientColor(_ client: String?) -> Color {
        switch client {
        case "claude-code": return blue
        case "codex": return codex
        case "cursor": return orange
        case "hermes": return purple
        case "opencode": return cyan
        case "openclaw": return red
        default: return .secondary.opacity(0.8)
        }
    }

    static func statusColor(_ status: String?) -> Color {
        switch status {
        case "blocked": return orange
        case "handed_off": return purple
        case "in_progress", "started", "checkpoint": return blue
        case "completed": return green
        default: return .secondary.opacity(0.5)
        }
    }

    static func limitColor(usedPercent: Double) -> Color {
        if usedPercent >= 90 { return red }
        if usedPercent >= 70 { return orange }
        return accent
    }

    static func resetsIn(_ resetsAt: Double?, now: Date = SnapshotMode.currentDate) -> String? {
        guard let resetsAt else { return nil }
        let delta = resetsAt - now.timeIntervalSince1970
        guard delta > 0 else { return nil }
        let total = Int(delta)
        let days = total / 86400
        let hours = (total % 86400) / 3600
        let minutes = (total % 3600) / 60
        if days > 0 { return "\(days)d \(hours)h" }
        if hours > 0 { return "\(hours)h \(minutes)m" }
        return "\(minutes)m"
    }
}

// MARK: - Typography tokens

/// The macOS type ramp. SF Pro's neutral default design carries the interface;
/// metrics use tabular digits without changing the surrounding letterforms.
/// Three weights are enough to express the hierarchy without making every
/// region compete for attention.
enum Type {
    /// Primary ring metric.
    static let hero = Font.system(size: 28, weight: .semibold).monospacedDigit()
    /// Title paired with a hero metric.
    static let heroTitle = Font.system(size: 17, weight: .semibold)
    /// Title inside a summary card.
    static let cardTitle = Font.system(size: 14, weight: .semibold)
    /// Panel-leading metric in top-level stat tiles.
    static let metric = Font.system(size: 24, weight: .semibold).monospacedDigit()
    /// Row-trailing metric (list money/token cells).
    static let metricS = Font.system(size: 12, weight: .semibold).monospacedDigit()
    /// Inline numeric text (meters, percentages).
    static let numeric = Font.system(size: 11.5, weight: .semibold).monospacedDigit()
    /// Row titles.
    static let rowTitle = Font.system(size: 13, weight: .medium)
    /// Body copy.
    static let body = Font.system(size: 13)
    /// Compact control labels and supporting copy.
    static let callout = Font.system(size: 12)
    /// Text actions.
    static let action = Font.system(size: 12, weight: .medium)
    /// Secondary copy.
    static let small = Font.system(size: 11)
    /// Footnotes / axis labels.
    static let tiny = Font.system(size: 10)
    /// Sentence-case section label (see SectionCaption).
    static let caption = Font.system(size: 11, weight: .medium)
    /// Small sentence-case label inside summary tiles.
    static let tileLabel = Font.system(size: 11, weight: .medium)
}

/// The spacing scale. Padding and gaps come from here, not magic numbers.
enum Space {
    static let xs: CGFloat = 4
    static let s: CGFloat = 8
    static let m: CGFloat = 12
    static let l: CGFloat = 16
    static let dashboard: CGFloat = 20
    static let xl: CGFloat = 24
}

/// Productive motion only: quick feedback, a brief content update, and one
/// zero-bounce geometry transition for persistent selection. Views must still
/// disable geometry motion when `accessibilityReduceMotion` is enabled.
enum Motion {
    static let feedback = Animation.easeOut(duration: 0.07)
    static let hover = Animation.easeOut(duration: 0.10)
    static let contentUpdate = Animation.easeOut(duration: 0.16)
    static let selection = Animation.spring(duration: 0.22, bounce: 0)
    static let paneCrossfade = Animation.easeOut(duration: 0.14)
}

// MARK: - Reusable components

/// A caption + big monospaced value, on a soft adaptive card (menu variant).
struct StatTile: View {
    let label: String
    let value: String
    var detail: String? = nil
    var accent: Color = .primary

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label.uppercased())
                .font(Type.tileLabel)
                .tracking(0.6)
                .foregroundStyle(.secondary)
            Text(value)
                .font(Font.system(size: 17, weight: .semibold, design: .rounded).monospacedDigit())
                .foregroundStyle(accent)
            if let detail {
                Text(detail)
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 9, style: .continuous))
    }
}

/// The window's stat tile: white/storm panel, hairline border, tabular value.
struct PanelTile: View {
    let label: String
    let value: String
    var detail: String? = nil
    var accent: Color = Theme.text

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label)
                .font(Type.tileLabel)
                .foregroundStyle(Theme.textFaint)
            Text(value)
                .font(Type.metric)
                .foregroundStyle(accent)
            if let detail {
                Text(detail)
                    .font(Type.tiny)
                    .foregroundStyle(Theme.textFaint)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(13)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(Theme.border, lineWidth: 1)
        )
    }
}

/// The window's panel card: content on card surface with a hairline border.
struct Card<Content: View>: View {
    var padding: CGFloat = Space.m
    /// Equal-height grids opt in; ordinary cards keep their intrinsic height.
    var fillsHeight = false
    @ViewBuilder let content: () -> Content

    var body: some View {
        content()
            .padding(padding)
            .frame(
                maxWidth: .infinity,
                maxHeight: fillsHeight ? .infinity : nil,
                alignment: .topLeading
            )
            .background(Theme.card, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .strokeBorder(Theme.border, lineWidth: 1)
            )
    }
}

/// A quiet macOS action: no fill at rest unless it is the local primary
/// action, then short color-only hover and press feedback. The nested body
/// reads Reduce Motion so every use shares the same accessibility behavior.
struct QuietButtonStyle: ButtonStyle {
    var tint: Color = Theme.accent
    var prominent = false
    var horizontalPadding: CGFloat = 7
    var verticalPadding: CGFloat = 6

    func makeBody(configuration: Configuration) -> some View {
        QuietButtonBody(
            configuration: configuration,
            tint: tint,
            prominent: prominent,
            horizontalPadding: horizontalPadding,
            verticalPadding: verticalPadding
        )
    }
}

private struct QuietButtonBody: View {
    let configuration: ButtonStyleConfiguration
    let tint: Color
    let prominent: Bool
    let horizontalPadding: CGFloat
    let verticalPadding: CGFloat

    @State private var hovering = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.isFocused) private var isFocused

    private var fillOpacity: Double {
        if configuration.isPressed { return 0.14 }
        if hovering { return 0.10 }
        return prominent ? 0.065 : 0
    }

    var body: some View {
        configuration.label
            .padding(.horizontal, horizontalPadding)
            .padding(.vertical, verticalPadding)
            .background(
                tint.opacity(fillOpacity),
                in: RoundedRectangle(cornerRadius: 6, style: .continuous)
            )
            .overlay {
                if isFocused {
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .strokeBorder(Theme.accent, lineWidth: 2)
                }
            }
            .contentShape(Rectangle())
            .onHover { inside in
                withAnimation(reduceMotion ? nil : Motion.hover) {
                    hovering = inside
                }
            }
            .animation(reduceMotion ? nil : Motion.feedback, value: configuration.isPressed)
    }
}

/// A slim rounded meter (limits, shares).
struct MeterBar: View {
    let fraction: Double
    var tint: Color
    var height: CGFloat = 5

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                Capsule().fill(.quaternary.opacity(0.6))
                Capsule()
                    .fill(tint)
                    .frame(width: max(height, proxy.size.width * min(max(fraction, 0), 1)))
            }
        }
        .frame(height: height)
    }
}

/// A small tinted label chip.
struct Chip: View {
    static let backgroundOpacity = 0.08

    let text: String
    var tint: Color = .secondary

    var body: some View {
        Text(text)
            .font(.system(size: 10, weight: .medium))
            .padding(.horizontal, 7)
            .padding(.vertical, 2.5)
            .background(tint.opacity(Self.backgroundOpacity), in: Capsule())
            .foregroundStyle(tint)
    }
}

/// A colored status dot. It stays flat so status never reads as decoration.
struct StatusDot: View {
    let color: Color
    var size: CGFloat = 7

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: size, height: size)
    }
}

/// Section header caption used across the dropdown and window. ``tone``
/// precedes ``text`` so the memberwise init reads ``(tone:text:)`` at window
/// call sites while menu calls stay ``(text:)``.
struct SectionCaption: View {
    var tone: Color? = nil
    let text: String

    var body: some View {
        Text(text)
            .font(Type.caption)
            .foregroundStyle(tone ?? Color.secondary)
    }
}


// MARK: - Snapshot support

/// Offscreen ImageRenderer can't lay out ScrollViews/lazy stacks; snapshot
/// mode swaps them for plain containers so renders match the live app.
enum SnapshotMode {
    nonisolated(unsafe) static var enabled = false

    /// Optional clock override for deterministic fixture renders.
    ///
    /// The normal app leaves this `nil` and uses the real clock. The dashboard
    /// snapshot renderer sets it to the fixture's `glance.generated_at` only
    /// while rendering, then restores it to `nil` in `defer`.
    nonisolated(unsafe) private static var fixtureDate: Date?

    /// The date relative UI copy should use.
    ///
    /// Formatting helpers such as `Theme.resetsIn` and `agoText` read this
    /// instead of calling `Date()` directly. This freezes text like
    /// “resets in 6d 13h” in snapshots without changing the system clock or
    /// affecting normal application behavior.
    static var currentDate: Date { fixtureDate ?? Date() }

    /// Pins or restores the clock used by relative UI copy during a snapshot.
    static func setFixtureDate(_ date: Date?) {
        fixtureDate = date
    }

    /// Dashboard review matrices model a real viewport at the top scroll
    /// position. Legacy all-pane screenshots retain their existing full-content
    /// behavior until each pane has its own review viewport.
    nonisolated(unsafe) static var boundsScrollContentToViewport = false
}

struct ScrollBox<Content: View>: View {
    @ViewBuilder let content: () -> Content

    var body: some View {
        if SnapshotMode.enabled {
            if SnapshotMode.boundsScrollContentToViewport {
                GeometryReader { proxy in
                    content()
                        .frame(width: proxy.size.width, alignment: .topLeading)
                }
                .clipped()
            } else {
                content().frame(maxHeight: .infinity, alignment: .top)
            }
        } else {
            ScrollView(showsIndicators: false) { content() }
        }
    }
}
