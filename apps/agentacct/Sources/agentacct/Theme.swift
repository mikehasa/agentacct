import AppKit
import SwiftUI

// The agentacct visual system — semantic tokens with a light and a dark value
// each, resolved live from the system appearance. The identity is a precision
// instrument: warm paper surfaces, ink text, one restrained indigo accent,
// hairline borders, monospaced digits everywhere a number lives. Dark mode is
// the Tokyo Night storm variant (shared identity with `agentacct tui`), not a
// separate design.
//
// Rules of the road:
// * Views never hard-code a color or a font size — they use Theme/Type tokens
//   so a retune touches ONE file.
// * Numbers always render in monospaced digits (Type.metric*), so columns of
//   money and tokens align like a meter, not a paragraph.
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

    // MARK: surfaces (light: warm paper + white panels · dark: Tokyo storm)

    static let bg = dynamic(light: 0xF7F5F1, dark: 0x16161E)
    static let surface = dynamic(light: 0xFDFCFA, dark: 0x1A1B26)
    static let card = dynamic(light: 0xFFFFFF, dark: 0x1F2335)
    static let cardAlt = dynamic(light: 0xEEEBE4, dark: 0x24283B)
    static let border = dynamic(light: 0xE3DFD6, dark: 0x292E42)

    // MARK: text (ink on paper · Tokyo foreground)

    static let text = dynamic(light: 0x201E1B, dark: 0xC0CAF5)
    static let textMuted = dynamic(light: 0x6E6960, dark: 0x787C99)
    static let textFaint = dynamic(light: 0x8F8878, dark: 0x565F89)

    // MARK: accents — one restrained indigo carries the brand; the client
    // palette keeps the TUI's hue identities, darkened for paper.

    static let accent = dynamic(light: 0x3B5BDB, dark: 0x7AA2F7)
    static let blue = dynamic(light: 0x3B5BDB, dark: 0x7AA2F7)
    static let purple = dynamic(light: 0x7048B6, dark: 0xBB9AF7)
    static let green = dynamic(light: 0x2F7D46, dark: 0x9ECE6A)
    static let orange = dynamic(light: 0xB26A00, dark: 0xE0AF68)
    static let red = dynamic(light: 0xC03050, dark: 0xF7768E)
    static let cyan = dynamic(light: 0x0E7490, dark: 0x7DCFFF)

    static func clientColor(_ client: String?) -> Color {
        switch client {
        case "claude-code": return blue
        case "codex": return green
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
        return green
    }

    static func resetsIn(_ resetsAt: Double?, now: Date = Date()) -> String? {
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

/// The type ramp. Metrics are rounded + monospaced-digit (instrument dials);
/// labels are compact tracked caps; body copy is the system face.
enum Type {
    /// The one big number (menu hero).
    static let hero = Font.system(size: 30, weight: .bold, design: .rounded).monospacedDigit()
    /// Panel-leading metric (stat tiles).
    static let metric = Font.system(size: 18, weight: .semibold, design: .rounded).monospacedDigit()
    /// Row-trailing metric (list money/token cells).
    static let metricS = Font.system(size: 12, weight: .semibold, design: .rounded).monospacedDigit()
    /// Inline numeric text (meters, percentages).
    static let numeric = Font.system(size: 11.5, weight: .semibold).monospacedDigit()
    /// Row titles.
    static let rowTitle = Font.system(size: 12.5, weight: .medium)
    /// Body copy.
    static let body = Font.system(size: 12)
    /// Secondary copy.
    static let small = Font.system(size: 10.5)
    /// Footnotes / axis labels.
    static let tiny = Font.system(size: 9.5)
    /// Tracked caps caption (see SectionCaption).
    static let caption = Font.system(size: 10, weight: .semibold)
    /// Small tracked caps label inside tiles.
    static let tileLabel = Font.system(size: 9.5, weight: .semibold)
}

/// The spacing scale. Padding and gaps come from here, not magic numbers.
enum Space {
    static let xs: CGFloat = 4
    static let s: CGFloat = 8
    static let m: CGFloat = 12
    static let l: CGFloat = 16
    static let xl: CGFloat = 24
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

/// The window's stat tile: white/storm panel, hairline border, big dial.
struct PanelTile: View {
    let label: String
    let value: String
    var detail: String? = nil
    var accent: Color = Theme.text

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label.uppercased())
                .font(Type.tileLabel)
                .tracking(0.7)
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
        .padding(11)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 9, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .strokeBorder(Theme.border, lineWidth: 1)
        )
    }
}

/// The window's panel card: content on card surface with a hairline border.
struct Card<Content: View>: View {
    var padding: CGFloat = Space.m
    @ViewBuilder let content: () -> Content

    var body: some View {
        content()
            .padding(padding)
            .background(Theme.card, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .strokeBorder(Theme.border, lineWidth: 1)
            )
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
                    .fill(tint.gradient)
                    .frame(width: max(height, proxy.size.width * min(max(fraction, 0), 1)))
            }
        }
        .frame(height: height)
    }
}

/// A small tinted label chip.
struct Chip: View {
    let text: String
    var tint: Color = .secondary

    var body: some View {
        Text(text)
            .font(.system(size: 10, weight: .medium))
            .padding(.horizontal, 7)
            .padding(.vertical, 2.5)
            .background(tint.opacity(0.14), in: Capsule())
            .foregroundStyle(tint)
    }
}

/// A colored status dot (soft glow kept subtle so paper stays clean).
struct StatusDot: View {
    let color: Color
    var size: CGFloat = 7

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: size, height: size)
            .shadow(color: color.opacity(0.35), radius: 1.5)
    }
}

/// Section header caption used across the dropdown and window. ``tone``
/// precedes ``text`` so the memberwise init reads ``(tone:text:)`` at window
/// call sites while menu calls stay ``(text:)``.
struct SectionCaption: View {
    var tone: Color? = nil
    let text: String

    var body: some View {
        Text(text.uppercased())
            .font(Type.caption)
            .tracking(0.8)
            .foregroundStyle(tone ?? Color.secondary)
    }
}


// MARK: - Snapshot support

/// Offscreen ImageRenderer can't lay out ScrollViews/lazy stacks; snapshot
/// mode swaps them for plain containers so renders match the live app.
enum SnapshotMode {
    nonisolated(unsafe) static var enabled = false
}

struct ScrollBox<Content: View>: View {
    @ViewBuilder let content: () -> Content

    var body: some View {
        if SnapshotMode.enabled {
            content().frame(maxHeight: .infinity, alignment: .top)
        } else {
            ScrollView(showsIndicators: false) { content() }
        }
    }
}
