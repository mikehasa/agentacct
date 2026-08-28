import AppKit
import SwiftUI

// The agentacct visual system — the v7 brand token sheet (DESIGN.md v10) as
// Swift. Cream canvas and white cards are the ground; one cobalt accent is the
// only interactive voice; green, amber and coral are semantic and rationed:
// green speaks only for live-connection facts and independently verified
// evidence, amber for the unverified tier and thresholds, coral for failure.
//
// Rules of the road (v10):
// * Pip shape carries the evidence tier everywhere; color is never the only
//   carrier. The decision axis never merges with evidence tiers.
// * Absence is a named state — never a dash-as-value, a blank, or a fabricated
//   number. Every cost carries its basis.
// * Nothing below 12px; no shadows; no gradients; radius caps at 4
//   (fully-round chips excepted).
// * Updating metrics use the mono face with tabular digits so values do not
//   shift horizontally.
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

    /// The app-wide cost grammar (v10 rule 5 — every cost carries its basis):
    /// a bare `$` only for a COMPLETE figure whose confidence is reported or
    /// billed; a complete estimate wears `≈$`; a known-partial subtotal `~$`;
    /// nothing priced returns nil so callers name the absence.
    static func costDisplay(
        usd: Double?,
        knownAdditive: Double? = nil,
        complete: Bool?,
        confidence: String?
    ) -> String? {
        let reported = ["client_reported", "provider_billed"].contains(confidence ?? "")
        if complete == true, let usd {
            return dollars(usd, prefix: reported ? "$" : "≈$")
        }
        if let knownAdditive {
            return dollars(knownAdditive, prefix: "~$")
        }
        if let usd {
            return dollars(usd, prefix: "≈$")
        }
        return nil
    }

    /// Human phrasing for a cost-confidence key (raw keys stay in payloads).
    static func costConfidenceLabel(_ confidence: String?) -> String? {
        switch confidence {
        case "estimated_from_tokens": return "pricing estimate"
        case "client_reported": return "client-reported"
        case "provider_billed": return "provider billed"
        case "subscription_equivalent": return "subscription equivalent"
        case "approximate_subscription_allocation": return "approximate subscription share"
        case "mixed": return "mixed confidence"
        case nil, "unknown": return nil
        case .some(let other): return other.replacingOccurrences(of: "_", with: " ")
        }
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

    /// DESIGN.md v10 semantic tokens, verbatim. Light / dark.
    enum Palette {
        // Surfaces
        static let canvas = AdaptiveColor(lightHex: 0xF4F1E9, darkHex: 0x0D1215)
        static let chrome = AdaptiveColor(lightHex: 0xFCFBF7, darkHex: 0x141B1F)
        static let card = AdaptiveColor(lightHex: 0xFFFFFF, darkHex: 0x1B252A)
        static let selected = AdaptiveColor(lightHex: 0xE7EDF8, darkHex: 0x223049)

        // Ink
        static let ink = AdaptiveColor(lightHex: 0x171A1D, darkHex: 0xF2F4F3)
        static let muted = AdaptiveColor(lightHex: 0x59636B, darkHex: 0xA5B0B4)

        // Accents — each has exactly one job.
        static let accent = AdaptiveColor(lightHex: 0x245BDB, darkHex: 0x82A6FF)
        static let green = AdaptiveColor(lightHex: 0x1F7653, darkHex: 0x78D5A8)
        static let amber = AdaptiveColor(lightHex: 0x7A5A00, darkHex: 0xE7C66A)
        static let coral = AdaptiveColor(lightHex: 0xB63F2F, darkHex: 0xFF9B88)

        // Tints (badge/wash backgrounds; text on a tint uses the matching accent)
        static let tintNeutral = AdaptiveColor(lightHex: 0xEDEBE3, darkHex: 0x2A343B)
        static let tintAccent = AdaptiveColor(lightHex: 0xE8EEFB, darkHex: 0x24365C)
        static let tintGreen = AdaptiveColor(lightHex: 0xE2F0E9, darkHex: 0x1E3B2F)
        static let tintAmber = AdaptiveColor(lightHex: 0xF7EFDA, darkHex: 0x3D3420)
        static let tintCoral = AdaptiveColor(lightHex: 0xF8E5E1, darkHex: 0x412620)
        static let chipBg = AdaptiveColor(lightHex: 0xF7F5F0, darkHex: 0x232E34)

        // Lines
        static let rule = AdaptiveColor(lightHex: 0x79848B, darkHex: 0x68777D)
        static let hairline = AdaptiveColor(lightHex: 0xE4E1D7, darkHex: 0x2C363C)
        static let cardLine = AdaptiveColor(lightHex: 0xDDDACF, darkHex: 0x313D44)
        static let chipLine = AdaptiveColor(lightHex: 0xD8D5CC, darkHex: 0x3B474E)

        // Chart (one series per chart)
        static let chartBar = AdaptiveColor(lightHex: 0x245BDB, darkHex: 0x5B82E0)
        static let chartBarDim = AdaptiveColor(lightHex: 0xB9CBF2, darkHex: 0x31456F)

        // Copy that sits ON a filled accent (primary buttons): white in light,
        // near-black on the lighter dark-mode cobalt.
        static let onAccent = AdaptiveColor(lightHex: 0xFFFFFF, darkHex: 0x0D1215)
    }

    // MARK: surfaces

    static let canvas = Palette.canvas.color
    static let chrome = Palette.chrome.color
    static let card = Palette.card.color
    static let selected = Palette.selected.color

    // MARK: ink

    static let ink = Palette.ink.color
    static let muted = Palette.muted.color

    // MARK: accents

    static let accent = Palette.accent.color
    static let green = Palette.green.color
    static let amber = Palette.amber.color
    static let coral = Palette.coral.color
    static let onAccent = Palette.onAccent.color

    // MARK: tints

    static let tintNeutral = Palette.tintNeutral.color
    static let tintAccent = Palette.tintAccent.color
    static let tintGreen = Palette.tintGreen.color
    static let tintAmber = Palette.tintAmber.color
    static let tintCoral = Palette.tintCoral.color
    static let chipBg = Palette.chipBg.color

    // MARK: lines

    static let rule = Palette.rule.color
    static let hairline = Palette.hairline.color
    static let cardLine = Palette.cardLine.color
    static let chipLine = Palette.chipLine.color

    // MARK: chart

    static let chartBar = Palette.chartBar.color
    static let chartBarDim = Palette.chartBarDim.color

    /// Session/task lifecycle → decision-axis colors. The decision axis never
    /// wears green for claims: "completed" is an assertion, so it stays ink.
    /// Coral is failure-only; the interactive cobalt marks live progress.
    static func statusColor(_ status: String?) -> Color {
        switch status {
        case "blocked", "failed": return coral
        case "in_progress", "started", "checkpoint": return accent
        case "completed", "handed_off", "resolved": return ink
        default: return muted
        }
    }

    /// Threshold → color for limit meters: attention from 75%, failure at the
    /// limit itself (v10: notches sit at 75/90).
    static func limitColor(usedPercent: Double) -> Color {
        if usedPercent >= 100 { return coral }
        if usedPercent >= 75 { return amber }
        return accent
    }

    static func resetText(_ resetsAt: Double?) -> String {
        TemporalText.providerReset(epoch: resetsAt).text
    }
}

// MARK: - Typography tokens

/// Brand faces with graceful degradation. The v7 spec sets Instrument Sans
/// for UI and JetBrains Mono for data; neither ships with macOS, so resolution
/// happens once at startup: use the brand face when it is installed or
/// bundled, otherwise fall back to the system faces (SF / SF Mono) with the
/// same sizes and weights. Registration of bundled fonts can slot in here
/// later without touching any call site.
enum Face {
    static let sans: String? = resolve(["Instrument Sans", "InstrumentSans-Regular"])
    static let mono: String? = resolve(["JetBrains Mono", "JetBrainsMono-Regular"])

    private static func resolve(_ candidates: [String]) -> String? {
        for name in candidates where NSFont(name: name, size: 13) != nil { return name }
        return nil
    }

    static func sansFont(_ size: CGFloat, _ weight: Font.Weight) -> Font {
        if let sans { return Font.custom(sans, size: size).weight(weight) }
        return Font.system(size: size, weight: weight)
    }

    /// Data face. Tabular digits always, so updating values hold still.
    static func monoFont(_ size: CGFloat, _ weight: Font.Weight) -> Font {
        if let mono { return Font.custom(mono, size: size).weight(weight).monospacedDigit() }
        return Font.system(size: size, weight: weight, design: .monospaced).monospacedDigit()
    }
}

/// The v7 nine-role type ramp (DESIGN.md v10). Hard floor: nothing below 12px.
/// Tracking rides beside the roles that need it (SwiftUI fonts cannot carry
/// letter-spacing, so title call sites pair the font with its tracking token).
enum Type {
    /// Record/page titles — 26/650, tracking −0.6.
    static let titlePage = Face.sansFont(26, .semibold)
    static let titlePageTracking: CGFloat = -0.6
    /// Page section heads — 20/650, tracking −0.4.
    static let titleSection = Face.sansFont(20, .semibold)
    static let titleSectionTracking: CGFloat = -0.4
    /// Card headers — 15/650.
    static let titleCard = Face.sansFont(15, .semibold)
    /// Summary-strip values — 18/700 mono.
    static let kpi = Face.monoFont(18, .bold)
    /// Row labels, dimension names, source names — 14/600.
    static let rowLabel = Face.sansFont(14, .semibold)
    /// Values and sentences — 14/400.
    static let body = Face.sansFont(14, .regular)
    /// Inline data strings — 13/400 mono.
    static let data = Face.monoFont(13, .regular)
    /// Chips, captions, meta — 12/400 (semibold variant for badge text).
    static let caption = Face.sansFont(12, .regular)
    static let captionSemibold = Face.sansFont(12, .semibold)
    /// Small data annotations (12 mono) — timestamps, counts in captions.
    static let dataSmall = Face.monoFont(12, .regular)
    static let dataSmallSemibold = Face.monoFont(12, .semibold)
    /// Eyebrows, column headers, strip captions — 12/700 mono, tracking +0.9.
    /// Reserved for the label species; row content stays sentence case.
    static let labelCaps = Face.monoFont(12, .bold)
    static let labelCapsTracking: CGFloat = 0.9
}

/// Work keeps the design system's compact base sizes, but these equivalents
/// participate in Dynamic Type. WorkPane and its receipt-detail components use
/// them together so accessibility settings scale the whole task record rather
/// than only changing its column arrangement.
enum WorkFontRole {
    case titlePage, titleCard, kpi, rowLabel, body, caption, captionSemibold
    case dataSmall, dataSmallSemibold, labelCaps

    var metrics: (size: CGFloat, weight: Font.Weight, relativeTo: Font.TextStyle, monospaced: Bool) {
        switch self {
        case .titlePage: return (26, .semibold, .title, false)
        case .titleCard: return (15, .semibold, .headline, false)
        case .kpi: return (18, .bold, .title3, true)
        case .rowLabel: return (14, .semibold, .body, false)
        case .body: return (14, .regular, .body, false)
        case .caption: return (12, .regular, .caption, false)
        case .captionSemibold: return (12, .semibold, .caption, false)
        case .dataSmall: return (12, .regular, .caption, true)
        case .dataSmallSemibold: return (12, .semibold, .caption, true)
        case .labelCaps: return (12, .bold, .caption, true)
        }
    }

    var baseFont: Font {
        switch self {
        case .titlePage: return Type.titlePage
        case .titleCard: return Type.titleCard
        case .kpi: return Type.kpi
        case .rowLabel: return Type.rowLabel
        case .body: return Type.body
        case .caption: return Type.caption
        case .captionSemibold: return Type.captionSemibold
        case .dataSmall: return Type.dataSmall
        case .dataSmallSemibold: return Type.dataSmallSemibold
        case .labelCaps: return Type.labelCaps
        }
    }
}

private struct WorkScaledFontModifier: ViewModifier {
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @ScaledMetric private var scaledSize: CGFloat
    let baseFont: Font
    let weight: Font.Weight
    let monospaced: Bool

    init(
        size: CGFloat,
        baseFont: Font,
        weight: Font.Weight,
        relativeTo: Font.TextStyle,
        monospaced: Bool
    ) {
        _scaledSize = ScaledMetric(wrappedValue: size, relativeTo: relativeTo)
        self.baseFont = baseFont
        self.weight = weight
        self.monospaced = monospaced
    }

    func body(content: Content) -> some View {
        if dynamicTypeSize == .medium || dynamicTypeSize == .large {
            return content.font(baseFont)
        } else {
            let font: Font
            if monospaced {
                if let name = Face.mono {
                    font = .custom(name, size: scaledSize).weight(weight).monospacedDigit()
                } else {
                    font = .system(size: scaledSize, weight: weight, design: .monospaced).monospacedDigit()
                }
            } else if let name = Face.sans {
                font = .custom(name, size: scaledSize).weight(weight)
            } else {
                font = .system(size: scaledSize, weight: weight)
            }
            return content.font(font)
        }
    }
}

extension View {
    func workFont(_ role: WorkFontRole) -> some View {
        let metrics = role.metrics
        return modifier(WorkScaledFontModifier(
            size: metrics.size,
            baseFont: role.baseFont,
            weight: metrics.weight,
            relativeTo: metrics.relativeTo,
            monospaced: metrics.monospaced
        ))
    }

    func workFont(
        size: CGFloat,
        weight: Font.Weight,
        relativeTo: Font.TextStyle,
        monospaced: Bool = false
    ) -> some View {
        modifier(WorkScaledFontModifier(
            size: size,
            baseFont: monospaced ? Face.monoFont(size, weight) : Face.sansFont(size, weight),
            weight: weight,
            relativeTo: relativeTo,
            monospaced: monospaced
        ))
    }
}

/// The spacing scale — 4px base grid. Padding and gaps come from here.
enum Space {
    static let xs: CGFloat = 4
    static let s: CGFloat = 8
    static let m: CGFloat = 12
    static let l: CGFloat = 16
    static let xl: CGFloat = 24
    /// Standard card inset (v7: 24).
    static let cardPad: CGFloat = 24
    /// Page gutter (v7: 28–32).
    static let gutter: CGFloat = 28
}

/// Normative component geometry (DESIGN.md v10). One radius for everything
/// but chips; hairlines are 1px, secondary-button strokes 1.5px, focus 2px.
enum Metrics {
    static let radius: CGFloat = 4
    static let borderW: CGFloat = 1
    static let borderWSecondary: CGFloat = 1.5
    static let focusW: CGFloat = 2

    static let rowLedger: CGFloat = 64
    static let rowTable: CGFloat = 52
    static let rowSource: CGFloat = 72
    static let rowHeader: CGFloat = 40

    static let tierBadgeH: CGFloat = 22
    static let decisionBadgeH: CGFloat = 26
    static let decisionBadgeRowH: CGFloat = 20
    static let chipH: CGFloat = 20
    static let buttonH: CGFloat = 36
    static let buttonHCompact: CGFloat = 32
    static let meterH: CGFloat = 8
    static let pipR: CGFloat = 4
}

/// Productive motion only. Color and opacity feedback remains available with
/// Reduce Motion; every call site that moves geometry must opt out explicitly.
enum Motion {
    static let feedback = Animation.easeOut(duration: 0.10)
    static let hover = Animation.easeOut(duration: 0.10)
    static let contentUpdate = Animation.easeInOut(duration: 0.18)
    static let selection = Animation.spring(duration: 0.22, bounce: 0)
    static let paneCrossfade = Animation.easeOut(duration: 0.18)
    static let detailNavigation = Animation.easeOut(duration: 0.20)
    static let phaseCrossfade = Animation.easeOut(duration: 0.16)
    static let reducedCrossfade = Animation.easeOut(duration: 0.12)

    static func animatesChartGeometry(bucketCount: Int, reduceMotion: Bool) -> Bool {
        !reduceMotion && bucketCount <= 30
    }
}

/// One deterministic interaction state for every custom button. Disabled wins
/// over pressed, and pressed wins over hover, so rapid pointer/keyboard input
/// cannot leave competing visual states behind.
enum ButtonInteractionPhase: Equatable {
    case idle
    case hovered
    case pressed
    case disabled
}

func buttonInteractionPhase(
    isEnabled: Bool,
    isPressed: Bool,
    isHovering: Bool
) -> ButtonInteractionPhase {
    guard isEnabled else { return .disabled }
    if isPressed { return .pressed }
    if isHovering { return .hovered }
    return .idle
}

enum ButtonFeedback {
    /// Compact macOS chrome can stay visually small while preserving a target
    /// above WCAG 2.5.8's 24pt floor.
    static let minimumHitDimension: CGFloat = 28

    static func quietFillOpacity(
        for phase: ButtonInteractionPhase,
        prominent: Bool
    ) -> Double {
        switch phase {
        case .idle: return prominent ? 0.065 : 0
        case .hovered: return 0.10
        case .pressed: return 0.14
        case .disabled: return prominent ? 0.035 : 0
        }
    }

    static func surfaceFillOpacity(for phase: ButtonInteractionPhase) -> Double {
        switch phase {
        case .idle, .disabled: return 0
        case .hovered: return 0.055
        case .pressed: return 0.10
        }
    }

    static func labelOpacity(
        for phase: ButtonInteractionPhase,
        pressed: Double = 1
    ) -> Double {
        switch phase {
        case .pressed: return pressed
        case .disabled: return 0.42
        case .idle, .hovered: return 1
        }
    }
}

// MARK: - Evidence tier grammar

/// The four pip shapes that carry evidence tiers everywhere (v10 rule 1:
/// shape is the tier; color is never the only carrier).
///
/// Mapping from the daemon's evidence vocabulary — labels always keep the
/// daemon's words, the shape/color pair is presentation only:
/// * externally_verified → verified pip (filled inside a ring), green —
///   the reserved tier; appears only with independent evidence.
/// * independently_checked → filled pip, ink — machine-observed locally
///   (hook-captured exit codes).
/// * self_checked → half pip, accent — a supported claim.
/// * claimed / unchecked → hollow pip, amber — claim ≠ proof.
/// * none / not gradeable → hollow pip, muted.
enum PipShape {
    case filled
    case half
    case hollow
    case verified
}

/// A single evidence pip (r4 by default). Draws the tier shape flat.
struct EvidencePip: View {
    let shape: PipShape
    var tint: Color
    var radius: CGFloat = Metrics.pipR

    var body: some View {
        ZStack {
            switch shape {
            case .filled:
                Circle().fill(tint)
            case .half:
                Circle().strokeBorder(tint, lineWidth: 1.5)
                HalfDisc().fill(tint)
            case .hollow:
                Circle().strokeBorder(tint, lineWidth: 1.5)
            case .verified:
                Circle().strokeBorder(tint, lineWidth: 1.5)
                Circle().fill(tint).padding(radius * 0.45)
            }
        }
        .frame(width: radius * 2, height: radius * 2)
        .accessibilityHidden(true)  // the badge text names the tier
    }

    /// Left half of the pip disc (the "supported" glyph).
    private struct HalfDisc: Shape {
        func path(in rect: CGRect) -> Path {
            var path = Path()
            path.addArc(
                center: CGPoint(x: rect.midX, y: rect.midY),
                radius: rect.width / 2,
                startAngle: .degrees(90),
                endAngle: .degrees(270),
                clockwise: false
            )
            path.closeSubpath()
            return path
        }
    }
}

/// Evidence tier presentation: shape + colors + display label for each daemon
/// grade word. The single lookup every surface shares so list, detail, and
/// step rows can never disagree.
struct EvidenceTierStyle {
    let pip: PipShape
    let tint: Color
    let tintBg: Color
    let label: String

    static func forGrade(_ grade: String?) -> EvidenceTierStyle {
        switch grade {
        case "externally_verified":
            return .init(pip: .verified, tint: Theme.green, tintBg: Theme.tintGreen, label: "externally-verified")
        case "independently_checked":
            return .init(pip: .filled, tint: Theme.ink, tintBg: Theme.tintNeutral, label: "independently-checked")
        case "self_checked":
            return .init(pip: .half, tint: Theme.accent, tintBg: Theme.tintAccent, label: "self-checked")
        case "claimed":
            return .init(pip: .hollow, tint: Theme.amber, tintBg: Theme.tintAmber, label: "claimed")
        case "unchecked":
            return .init(pip: .hollow, tint: Theme.amber, tintBg: Theme.tintAmber, label: "unchecked")
        default:
            return .init(pip: .hollow, tint: Theme.muted, tintBg: Theme.tintNeutral, label: grade ?? "none")
        }
    }
}

/// Tier badge: h22, rx4, tint wash, pip + 12/600 sentence-case text.
struct TierBadge: View {
    let grade: String?
    var text: String? = nil

    var body: some View {
        let style = EvidenceTierStyle.forGrade(grade)
        HStack(spacing: 6) {
            EvidencePip(shape: style.pip, tint: style.tint)
            Text(text ?? style.label)
                .workFont(.captionSemibold)
                .foregroundStyle(style.tint)
        }
        .padding(.horizontal, 11)
        .frame(minHeight: Metrics.tierBadgeH)
        .background(style.tintBg, in: RoundedRectangle(cornerRadius: Metrics.radius))
    }
}

// MARK: - Decision axis

/// Decision badge tint classes (no pip — the decision axis carries no
/// evidence shapes). Green is reserved for machine-verified completion, the
/// one decision key backed by independent evidence. Families:
/// * danger (coral) — needs the user: blocked / failed / failing check.
/// * accent (cobalt, OUTLINED) — live right now; the outline (unfilled = still
///   in flight) keeps it apart from the settled claimed family below.
/// * claimed (cobalt on the accent wash) — done-ish on a claim's strength:
///   the agent said so, or a stop was its deliberate last word. Never green.
/// * inferredStop (amber) — agentacct inferred the stop; honestly weaker than
///   a claim, so it wears the same claim≠proof amber as unverified evidence.
/// * verified (green) — machine-verified completion only.
enum DecisionTintClass {
    case neutral
    case accent
    case claimed
    case inferredStop
    case danger
    case verified

    static func forKey(_ key: String?) -> DecisionTintClass {
        switch key {
        case "blocked", "failed", "finding": return .danger
        case "in_progress", "started", "checkpoint": return .accent
        case "reported", "resolved", "mostly_done", "handed_off", "finding_superseded",
             "finding_resolved_by_user", "blocker_resolved_by_user":
            return .claimed
        case "ended_open": return .inferredStop
        case "verified": return .verified
        default: return .neutral
        }
    }

    var text: Color {
        switch self {
        case .neutral: return Theme.ink
        case .accent, .claimed: return Theme.accent
        case .inferredStop: return Theme.amber
        case .danger: return Theme.coral
        case .verified: return Theme.green
        }
    }

    var wash: Color {
        switch self {
        case .neutral: return Theme.tintNeutral
        case .accent: return .clear
        case .claimed: return Theme.tintAccent
        case .inferredStop: return Theme.tintAmber
        case .danger: return Theme.tintCoral
        case .verified: return Theme.tintGreen
        }
    }

    /// Live states render as an outline; every settled class wears its wash.
    var outlined: Bool { self == .accent }
}

/// Decision badge: h26 page variant / h20 row variant, rx4, no pip.
struct DecisionBadge: View {
    let key: String?
    let label: String
    var compact = false

    var body: some View {
        let tint = DecisionTintClass.forKey(key)
        Text(label)
            .workFont(
                size: compact ? 12 : 13,
                weight: .semibold,
                relativeTo: .caption
            )
            .foregroundStyle(tint.text)
            .padding(.horizontal, compact ? 8 : 12)
            .frame(minHeight: compact ? Metrics.decisionBadgeRowH : Metrics.decisionBadgeH)
            .background(tint.wash, in: RoundedRectangle(cornerRadius: Metrics.radius))
            .overlay {
                if tint.outlined {
                    RoundedRectangle(cornerRadius: Metrics.radius)
                        .strokeBorder(tint.text.opacity(0.55), lineWidth: Metrics.borderW)
                }
            }
    }
}

// MARK: - Chips and labels

/// Provenance chip: h20 fully-round, chip wash + border, 12 mono muted text.
/// Names a source or basis (Client log, Agent report, Pricing table…).
struct ProvenanceChip: View {
    let text: String
    var tint: Color = Theme.muted

    var body: some View {
        Text(text)
            .workFont(.dataSmall)
            .foregroundStyle(tint)
            .padding(.horizontal, 12)
            .frame(minHeight: Metrics.chipH)
            .background(Theme.chipBg, in: Capsule())
            .overlay(Capsule().strokeBorder(Theme.chipLine, lineWidth: Metrics.borderW))
    }
}

/// A small tinted label chip (fully round). The general-purpose pill for
/// inline state words; tier words should use TierBadge instead.
struct Chip: View {
    let text: String
    var tint: Color = Theme.muted

    var body: some View {
        Text(text)
            .workFont(.dataSmall)
            .foregroundStyle(tint)
            .padding(.horizontal, 10)
            .frame(minHeight: Metrics.chipH)
            .background(Theme.chipBg, in: Capsule())
            .overlay(Capsule().strokeBorder(Theme.chipLine, lineWidth: Metrics.borderW))
    }
}

/// The caps-mono label species: eyebrows, column headers, strip captions.
/// 12/700 mono, +0.9 tracking, uppercased. Content never shouts — only labels.
struct CapsLabel: View {
    let text: String
    var tone: Color = Theme.muted

    var body: some View {
        Text(text.uppercased())
            .workFont(.labelCaps)
            .tracking(Type.labelCapsTracking)
            .foregroundStyle(tone)
    }
}

/// Section header caption used across the dropdown and window. ``tone``
/// precedes ``text`` so the memberwise init reads ``(tone:text:)`` at window
/// call sites while menu calls stay ``(text:)``.
struct SectionCaption: View {
    var tone: Color? = nil
    let text: String

    var body: some View {
        CapsLabel(text: text, tone: tone ?? Theme.muted)
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

// MARK: - Cards, tiles, buttons

/// The window's panel card: content on the card surface, 1px card border,
/// v7 radius 4, no shadow.
struct Card<Content: View>: View {
    var padding: CGFloat = Space.l
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
            .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
            .overlay(
                RoundedRectangle(cornerRadius: Metrics.radius)
                    .strokeBorder(Theme.cardLine, lineWidth: Metrics.borderW)
            )
    }
}

/// A caption + big monospaced value, on a soft adaptive card (menu variant).
struct StatTile: View {
    let label: String
    let value: String
    var detail: String? = nil
    var accent: Color = .primary

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            CapsLabel(text: label)
            Text(value)
                .font(Face.monoFont(17, .bold))
                .foregroundStyle(accent)
            if let detail {
                Text(detail)
                    .font(Type.dataSmall)
                    .foregroundStyle(Theme.muted)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(Theme.tintNeutral.opacity(0.5), in: RoundedRectangle(cornerRadius: Metrics.radius))
    }
}

/// The window's stat tile: card panel, hairline border, tabular value.
struct PanelTile: View {
    let label: String
    let value: String
    var detail: String? = nil
    var accent: Color = Theme.ink

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            CapsLabel(text: label)
            Text(value)
                .font(Type.kpi)
                .foregroundStyle(accent)
            if let detail {
                Text(detail)
                    .font(Type.dataSmall)
                    .foregroundStyle(Theme.muted)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(Space.m)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(
            RoundedRectangle(cornerRadius: Metrics.radius)
                .strokeBorder(Theme.cardLine, lineWidth: Metrics.borderW)
        )
    }
}

/// A quiet macOS action: no fill at rest unless it is the local primary
/// action, then short color-only hover and press feedback. These non-spatial
/// acknowledgements remain enabled when Reduce Motion is on.
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
    @Environment(\.isFocused) private var isFocused
    @Environment(\.isEnabled) private var isEnabled

    private var phase: ButtonInteractionPhase {
        buttonInteractionPhase(
            isEnabled: isEnabled,
            isPressed: configuration.isPressed,
            isHovering: hovering
        )
    }

    var body: some View {
        configuration.label
            .padding(.horizontal, horizontalPadding)
            .padding(.vertical, verticalPadding)
            .background(
                tint.opacity(ButtonFeedback.quietFillOpacity(for: phase, prominent: prominent)),
                in: RoundedRectangle(cornerRadius: Metrics.radius)
            )
            .opacity(ButtonFeedback.labelOpacity(for: phase))
            .overlay {
                if isFocused && isEnabled {
                    RoundedRectangle(cornerRadius: Metrics.radius)
                        .strokeBorder(Theme.accent, lineWidth: Metrics.focusW)
                }
            }
            .contentShape(Rectangle())
            .onHover { inside in
                withAnimation(Motion.hover) {
                    hovering = inside
                }
            }
            .animation(Motion.feedback, value: phase)
    }
}

/// Full-width rows, tabs, and disclosure headers that visually own their
/// geometry but still need common hover, press, focus, and disabled feedback.
/// This replaces `.plain`, whose lack of acknowledgement made several controls
/// in Work look like static text. The call site retains all layout ownership.
struct SurfaceButtonStyle: ButtonStyle {
    var tint: Color = Theme.accent
    var cornerRadius: CGFloat = Metrics.radius
    var focusInset: CGFloat = 0

    @ViewBuilder
    func makeBody(configuration: Configuration) -> some View {
        if SnapshotMode.enabled {
            // Stateful styles inside an offscreen ScrollView alter its
            // unbounded size proposal. Snapshots verify resting endpoints, so
            // render the identical resting label and reserve stateful feedback
            // for the live app where hover, press, and focus can occur.
            configuration.label
        } else {
            SurfaceButtonBody(
                configuration: configuration,
                tint: tint,
                cornerRadius: cornerRadius,
                focusInset: focusInset
            )
        }
    }
}

private struct SurfaceButtonBody: View {
    let configuration: ButtonStyleConfiguration
    let tint: Color
    let cornerRadius: CGFloat
    let focusInset: CGFloat

    @State private var hovering = false
    @Environment(\.isFocused) private var isFocused
    @Environment(\.isEnabled) private var isEnabled

    private var phase: ButtonInteractionPhase {
        buttonInteractionPhase(
            isEnabled: isEnabled,
            isPressed: configuration.isPressed,
            isHovering: hovering
        )
    }

    var body: some View {
        configuration.label
            .background(tint.opacity(ButtonFeedback.surfaceFillOpacity(for: phase)))
            .opacity(ButtonFeedback.labelOpacity(for: phase))
            .overlay {
                if isFocused && isEnabled {
                    RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                        .strokeBorder(Theme.accent, lineWidth: Metrics.focusW)
                        .padding(focusInset)
                }
            }
            .contentShape(Rectangle())
            .onHover { inside in
                withAnimation(Motion.hover) {
                    hovering = inside
                }
            }
            .animation(Motion.feedback, value: phase)
    }
}

/// Controls such as chart bars already encode hover/selection in their own
/// geometry. They still receive press, focus, disabled, and hit-target feedback
/// without adding a second background treatment.
struct TransparentButtonStyle: ButtonStyle {
    var cornerRadius: CGFloat = Metrics.radius

    func makeBody(configuration: Configuration) -> some View {
        TransparentButtonBody(
            configuration: configuration,
            cornerRadius: cornerRadius
        )
    }
}

private struct TransparentButtonBody: View {
    let configuration: ButtonStyleConfiguration
    let cornerRadius: CGFloat

    @State private var hovering = false
    @Environment(\.isFocused) private var isFocused
    @Environment(\.isEnabled) private var isEnabled

    private var phase: ButtonInteractionPhase {
        buttonInteractionPhase(
            isEnabled: isEnabled,
            isPressed: configuration.isPressed,
            isHovering: hovering
        )
    }

    var body: some View {
        configuration.label
            .opacity(ButtonFeedback.labelOpacity(for: phase, pressed: 0.72))
            .overlay {
                if isFocused && isEnabled {
                    RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                        .strokeBorder(Theme.accent, lineWidth: Metrics.focusW)
                }
            }
            .contentShape(Rectangle())
            .onHover { hovering = $0 }
            .animation(Motion.feedback, value: phase)
    }
}

/// The v7 secondary button chrome: card fill + 1.5px card border, rx4.
/// Wrap a label; height comes from Metrics.buttonH at the call site.
struct SecondaryButtonChrome: ViewModifier {
    var height: CGFloat = Metrics.buttonH

    func body(content: Content) -> some View {
        content
            .font(Face.sansFont(13, .semibold))
            .foregroundStyle(Theme.ink)
            .padding(.horizontal, Space.l)
            .frame(height: height)
            .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
            .overlay(
                RoundedRectangle(cornerRadius: Metrics.radius)
                    .strokeBorder(Theme.cardLine, lineWidth: Metrics.borderWSecondary)
            )
    }
}

// MARK: - Meters and coverage

/// A slim v7 meter (limits, shares): rx2 track on the neutral wash.
struct MeterBar: View {
    let fraction: Double
    var tint: Color
    var height: CGFloat = Metrics.meterH

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 2).fill(Theme.tintNeutral)
                RoundedRectangle(cornerRadius: 2)
                    .fill(tint)
                    .frame(width: max(height, proxy.size.width * min(max(fraction, 0), 1)))
            }
        }
        .frame(height: height)
    }
}

/// One segment of an evidence coverage bar.
struct CoverageSegment {
    let count: Int
    let grade: String
}

/// Coverage bar: h8, rx2 segments, 4px gaps, widths strictly proportional to
/// counts. Always pair with a counted legend at the call site.
struct CoverageBar: View {
    let segments: [CoverageSegment]
    var height: CGFloat = Metrics.meterH

    private var total: Int { segments.reduce(0) { $0 + $1.count } }

    var body: some View {
        GeometryReader { proxy in
            let visible = segments.filter { $0.count > 0 }
            let gaps = CGFloat(max(visible.count - 1, 0)) * 4
            let unit = total > 0 ? (proxy.size.width - gaps) / CGFloat(total) : 0
            HStack(spacing: 4) {
                ForEach(Array(visible.enumerated()), id: \.offset) { _, segment in
                    RoundedRectangle(cornerRadius: 2)
                        .fill(EvidenceTierStyle.forGrade(segment.grade).tint)
                        .frame(width: max(unit * CGFloat(segment.count), 2))
                }
            }
        }
        .frame(height: height)
    }
}

// MARK: - Summary strip

/// One cell of a v7 summary strip: caps-mono caption over an 18/700 mono
/// value, with an optional muted qualifier riding the value line.
struct SummaryCell: View {
    let label: String
    let value: String
    var qualifier: String? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            CapsLabel(text: label)
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(value)
                    .font(Type.kpi)
                    .foregroundStyle(Theme.ink)
                if let qualifier {
                    Text(qualifier)
                        .font(Type.dataSmall)
                        .foregroundStyle(Theme.muted)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
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
    /// Temporal formatting helpers read this
    /// instead of calling `Date()` directly. This freezes text like
    /// “Resets in 6 days 13 hr” in snapshots without changing the system clock or
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

/// Repeated content inside a ScrollBox: lazy in the live app, eager in the
/// deterministic ImageRenderer path (which cannot lay out lazy containers).
/// Keeping that renderer exception here prevents performance fixes from
/// forking production and review markup at every large collection.
struct ScrollContentStack<Content: View>: View {
    let alignment: HorizontalAlignment
    let spacing: CGFloat?
    @ViewBuilder let content: () -> Content

    init(
        alignment: HorizontalAlignment = .center,
        spacing: CGFloat? = nil,
        @ViewBuilder content: @escaping () -> Content
    ) {
        self.alignment = alignment
        self.spacing = spacing
        self.content = content
    }

    var body: some View {
        if SnapshotMode.enabled {
            VStack(alignment: alignment, spacing: spacing, content: content)
        } else {
            LazyVStack(alignment: alignment, spacing: spacing, content: content)
        }
    }
}
