import SwiftUI

// The agentacct visual system — the native sibling of the TUI's Tokyo Night
// theme. Accents carry the brand; surfaces stay adaptive (light/dark) via
// system materials so the app always looks native.

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

enum Theme {
    // The committed dark surface system (Tokyo Night storm): the window owns
    // its identity like the TUI does, instead of dressing up system chrome.
    static let bg = Color(red: 0.086, green: 0.086, blue: 0.118)        // #16161e
    static let surface = Color(red: 0.102, green: 0.106, blue: 0.149)   // #1a1b26
    static let card = Color(red: 0.122, green: 0.137, blue: 0.208)      // #1f2335
    static let cardAlt = Color(red: 0.141, green: 0.157, blue: 0.231)   // #24283b
    static let border = Color(red: 0.161, green: 0.180, blue: 0.259)    // #292e42
    static let text = Color(red: 0.753, green: 0.792, blue: 0.961)      // #c0caf5
    static let textMuted = Color(red: 0.471, green: 0.487, blue: 0.600) // #787c99
    static let textFaint = Color(red: 0.337, green: 0.371, blue: 0.537) // #565f89

    // Tokyo Night accents (shared identity with `agentacct tui`).
    static let blue = Color(red: 0.478, green: 0.635, blue: 0.969)     // #7aa2f7
    static let purple = Color(red: 0.733, green: 0.604, blue: 0.969)   // #bb9af7
    static let green = Color(red: 0.620, green: 0.808, blue: 0.416)    // #9ece6a
    static let orange = Color(red: 0.878, green: 0.686, blue: 0.408)   // #e0af68
    static let red = Color(red: 0.969, green: 0.463, blue: 0.557)      // #f7768e
    static let cyan = Color(red: 0.490, green: 0.812, blue: 1.0)       // #7dcfff

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

// MARK: - Reusable components

/// A caption + big monospaced value, on a soft card.
struct StatTile: View {
    let label: String
    let value: String
    var detail: String? = nil
    var accent: Color = .primary

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label.uppercased())
                .font(.system(size: 9.5, weight: .semibold))
                .tracking(0.6)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.system(size: 17, weight: .semibold, design: .rounded))
                .monospacedDigit()
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
            .background(tint.opacity(0.16), in: Capsule())
            .foregroundStyle(tint)
    }
}

/// A colored status dot.
struct StatusDot: View {
    let color: Color
    var size: CGFloat = 7

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: size, height: size)
            .shadow(color: color.opacity(0.5), radius: 2)
    }
}

/// Section header caption used across the dropdown and window.
struct SectionCaption: View {
    let text: String

    var body: some View {
        Text(text.uppercased())
            .font(.system(size: 10, weight: .semibold))
            .tracking(0.8)
            .foregroundStyle(.secondary)
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
