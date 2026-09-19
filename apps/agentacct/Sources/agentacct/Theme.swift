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

    /// "1 session" / "3 sessions": every user-facing count carries a
    /// correctly numbered noun. Pass `plural` for irregular nouns. Matches the
    /// Python `agentacct.plural.count_noun` helper word-for-word (the
    /// one-vocabulary rule).
    static func count(_ n: Int, _ singular: String, _ plural: String? = nil) -> String {
        "\(n) \(n == 1 ? singular : (plural ?? singular + "s"))"
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

    // MARK: chart axes, shares, dates

    private static let wholeDollars: NumberFormatter = {
        let formatter = NumberFormatter()
        formatter.numberStyle = .decimal
        formatter.minimumFractionDigits = 0
        formatter.maximumFractionDigits = 0
        formatter.groupingSeparator = ","
        formatter.usesGroupingSeparator = true
        formatter.roundingMode = .down
        return formatter
    }()

    /// Compact magnitude for chart labels: toward zero, one decimal only while
    /// the leading part is a single digit (`1.2k`, `12k`, `121M`), so every
    /// result stays within four characters before any prefix.
    private static func compactMagnitude(_ value: Double) -> String? {
        let units: [(Double, String)] = [(1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "k")]
        for (scale, suffix) in units where value >= scale {
            let scaled = value / scale
            if scaled >= 1_000 { return nil }  // beyond the top unit: exponent form
            if scaled < 10 {
                let tenths = (scaled * 10).rounded(.towardZero) / 10
                let text = tenths == tenths.rounded(.towardZero)
                    ? String(format: "%.0f", tenths)
                    : String(format: "%.1f", tenths)
                return text + suffix
            }
            return String(format: "%.0f", scaled.rounded(.towardZero)) + suffix
        }
        return String(format: "%.0f", value.rounded(.towardZero))
    }

    private static func exponentText(_ value: Double) -> String {
        String(format: "%.0e", value.rounded(.towardZero)).replacingOccurrences(of: "e+", with: "e")
    }

    /// The ONE amount label for cost-chart axes (C44). Whole units below 10k
    /// use the same grouping as `Fmt.dollars` (`1,163`); from 10k the compact
    /// form (`12k`, `1.2M`). Sub-10 scales keep cents so small ranges stay
    /// distinguishable. A non-finite or negative value is not a chart value
    /// and names that absence.
    ///
    /// `scale` is the axis maximum: every label on one axis shares the
    /// precision of that maximum, so `0` never reads `0.00` beside `1,163`.
    /// A tick is a SCALE POSITION, not a measured figure: it carries no cost
    /// glyph (`$` / `≈$` / `~$` would each claim a basis). The unit and basis
    /// are named once in the chart caption from the payload (K39). Values
    /// round to the nearest shown unit.
    static func axisAmount(_ value: Double, scale: Double? = nil) -> String {
        guard value.isFinite, value >= 0 else { return "not charted" }
        let precisionValue = max(value, scale.flatMap { $0.isFinite ? $0 : nil } ?? value)
        if precisionValue < 10 {
            let cents = (value * 100).rounded() / 100
            return String(format: "%.2f", cents)
        }
        if value.rounded() < 10_000, precisionValue < 10_000 {
            return wholeDollars.string(from: NSNumber(value: value.rounded()))
                ?? String(format: "%.0f", value.rounded())
        }
        return compactMagnitude(value) ?? exponentText(value)
    }

    /// Token-count axis label, at most five characters (`999`, `1.2k`,
    /// `121M`, `9e18`). Axes never wrap a label onto two lines.
    static func axisTokens(_ value: Double) -> String {
        guard value.isFinite, value >= 0 else { return "n/a" }
        return compactMagnitude(value) ?? exponentText(value)
    }

    /// A 0…1 share as a whole percent. Mirrors Python
    /// `display_vocabulary.percent_share` exactly: nothing (or ≤0) is `0%`, a
    /// real share below half a percent is `<1%` (never a fabricated zero),
    /// otherwise the half-up rounded integer.
    static func percentShare(_ fraction: Double?) -> String {
        guard let fraction, fraction.isFinite, fraction > 0 else { return "0%" }
        if fraction < 0.005 { return "<1%" }
        return "\(Int((fraction * 100 + 0.5).rounded(.down)))%"
    }

    /// The ONE absolute clock format (C55): locale-following short time
    /// (`10:50 PM` on en_US, `22:50` on a 24-hour locale). Relative phrases
    /// (“resets in 4d 3h”) stay with the payload's `reset_text`.
    static func clockTime(_ date: Date) -> String {
        date.formatted(date: .omitted, time: .shortened)
    }

    /// The same locale clock to the second: for records whose order matters
    /// and for anything spoken aloud, where a minute is not precise enough.
    static func clockTimeWithSeconds(_ date: Date) -> String {
        date.formatted(date: .omitted, time: .standard)
    }

    /// WHEN a saved copy was taken — the one absolute spelling every
    /// saved-copy notice uses (the offline banner and a stale receipt), so the
    /// same fact never reads two ways. Relative ages are not enough here: the
    /// copy's own "updated 2m ago" froze when it was fetched.
    static func savedAt(_ date: Date) -> String {
        date.formatted(date: .abbreviated, time: .standard)
    }

    private static let displayDateFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "MMM d"
        return formatter
    }()

    /// The ONE calendar-date label (`Sep 14`), in local time — the same words
    /// Python `display_vocabulary.display_date` emits.
    static func displayDate(_ date: Date) -> String {
        displayDateFormatter.timeZone = .current
        return displayDateFormatter.string(from: date)
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
        // Fill weight of amber (C58): #7A5A00 is a TEXT weight and reads olive
        // as a bar. Meter/coverage/limit FILLS use this; amber text keeps
        // `amber`. Light #967000 (K44: darkened from #A67B00 so the fill still
        // clears 3:1 on the stronger `meterTrack`): 4.55:1 on card, 3.07:1 on
        // meterTrack, 4.25:1 on well. Dark keeps the text value (9.43:1 on card).
        static let amberFill = AdaptiveColor(lightHex: 0x967000, darkHex: 0xE7C66A)
        // Pressed state of a control filled with `accent`; onAccent stays
        // legible on it (7.63:1 light, 6.51:1 dark).
        static let accentPressed = AdaptiveColor(lightHex: 0x1D4BB8, darkHex: 0x6F95F2)

        // Tints (badge/wash backgrounds; text on a tint uses the matching accent)
        static let tintNeutral = AdaptiveColor(lightHex: 0xEDEBE3, darkHex: 0x2A343B)
        static let tintAccent = AdaptiveColor(lightHex: 0xE8EEFB, darkHex: 0x24365C)
        static let tintGreen = AdaptiveColor(lightHex: 0xE2F0E9, darkHex: 0x1E3B2F)
        static let tintAmber = AdaptiveColor(lightHex: 0xF7EFDA, darkHex: 0x3D3420)
        static let tintCoral = AdaptiveColor(lightHex: 0xF8E5E1, darkHex: 0x412620)
        static let chipBg = AdaptiveColor(lightHex: 0xF7F5F0, darkHex: 0x232E34)
        // Canvas-relative washes (C42). The tints above are tuned against the
        // white/dark CARD; a wash that sits directly on the window canvas uses
        // these instead of an alpha-derived color, so it reads the same in
        // both modes. Amber text on tintAmberOnCanvas: 5.13:1 light, 8.61:1
        // dark; muted on tintNeutralOnCanvas: 4.97:1 light, 7.20:1 dark.
        static let tintAmberOnCanvas = AdaptiveColor(lightHex: 0xF2E6C4, darkHex: 0x33291A)
        static let tintNeutralOnCanvas = AdaptiveColor(lightHex: 0xEBE7DC, darkHex: 0x1A2328)

        // Surfaces nested inside a card (C96): plot areas and timeline wells
        // sit between card and canvas so the hierarchy canvas > card > well
        // never inverts. Ink 16.3:1 / 15.5:1, muted 5.73:1 / 7.70:1.
        static let well = AdaptiveColor(lightHex: 0xF9F7F2, darkHex: 0x151D21)

        // Raised selection thumb (K43): the selected pill of a segmented tray
        // and a card-styled button sitting on a wash. It is LIGHTER than every
        // tray in both modes (ΔE ≥ 7 against tintNeutral and
        // tintNeutralOnCanvas), so a selection reads raised, never recessed.
        // Light stays white; ink on it 17.5:1 light, 8.79:1 dark.
        static let thumb = AdaptiveColor(lightHex: 0xFFFFFF, darkHex: 0x3A464D)
        // Hover preview of the thumb on the canvas tray: the thumb blended 55%
        // into tintNeutralOnCanvas, as a solid token (never an alpha).
        static let thumbHoverOnCanvas = AdaptiveColor(lightHex: 0xF6F4EF, darkHex: 0x2C363C)

        // Meter track (K44): the unfilled denominator of every MeterBar, on
        // the canvas (menu) and on cards alike. Light: 1.31:1 on canvas, 1.48:1
        // on card; dark: 1.78:1 / 1.47:1. Every fill a meter carries clears
        // 3:1 on it (amberFill, coral, chartBar, chartNeutral).
        static let meterTrack = AdaptiveColor(lightHex: 0xD9D4C6, darkHex: 0x34404A)

        // Lines
        static let rule = AdaptiveColor(lightHex: 0x79848B, darkHex: 0x68777D)
        static let hairline = AdaptiveColor(lightHex: 0xE4E1D7, darkHex: 0x2C363C)
        static let cardLine = AdaptiveColor(lightHex: 0xDDDACF, darkHex: 0x313D44)
        static let chipLine = AdaptiveColor(lightHex: 0xD8D5CC, darkHex: 0x3B474E)

        // Chart (one series per chart)
        // The data hue is INDIGO, not the cobalt accent (K04). Cobalt is the
        // app's one interactive voice, so a bar painted in it reads clickable;
        // `chartBar` and `chartCatRead` were literally `accent` and now step one
        // hue stop over. Indigo rather than a second blue by measurement, not
        // taste: with `chartCatExecute`, `chartCatEdit` and `chartNeutral` held
        // fixed, a grid sweep of the sRGB cube found NO colour in the
        // blue arc (OKLCH hue 240–280) that clears the validator's own
        // "different colour" floor (OKLab ΔE×100 ≥ 15) against the accent while
        // keeping the categorical set's CVD and normal-vision checks passing —
        // the ceiling there is ΔE 12.8, and reaching even that needs a pure
        // #0000F0. Indigo 0x7040AA / 0xA872EC sits ΔE 13.0 light and 13.0 dark
        // from the accent (`accentPressed`, the deliberate SHADE of the accent,
        // is 6.8), so it is outside the accent's own family by a wide margin.
        // `AccentReservationTests` pins this and fails if a data mark drifts
        // back toward cobalt.
        // Selected vs dimmed bars hold 3:1 in both modes (K48): 3.97:1 light,
        // 3.28:1 dark. Bar on card 6.99:1 light, 4.67:1 dark; on `meterTrack`
        // 4.72:1 light, 3.18:1 dark.
        static let chartBar = AdaptiveColor(lightHex: 0x7040AA, darkHex: 0xA872EC)
        static let chartBarDim = AdaptiveColor(lightHex: 0xCDBCE4, darkHex: 0x453365)
        // Categorical identity hues for the tool-call distribution. Three,
        // deliberately: the app's non-reserved hue arc (green, amber and coral
        // are semantic, cobalt is the accent) only holds three mutually-distinct
        // hues that pass the colorblind and normal-vision separation checks in
        // BOTH modes on the card surfaces (validated all-pairs; dark steps sit
        // in the dark band). Assigned by entity, never by rank; anything past
        // three folds to Other. `chartCatRead` carries the same indigo as
        // `chartBar` — the app has ONE data blue-violet, and it is not the
        // accent.
        // Validated (dataviz validator, --pairs all): light on #FFFFFF — CVD ΔE
        // 9.6 (#1596B4↔#D9457F, deutan), normal 16.9 (#484F54↔#7040AA); dark on
        // #1B252A — CVD ΔE 7.9 (#10A6B0↔#CF4F7C, deutan; 6–8 band, legal with
        // the 2 pt gaps + labeled legend), normal 15.6. Both runs match the
        // pre-K04 palette's numbers on every check the neutral slot does not
        // own. Dark cyan leans teal to stay apart inside the dark band.
        static let chartCatRead = AdaptiveColor(lightHex: 0x7040AA, darkHex: 0xA872EC)
        static let chartCatExecute = AdaptiveColor(lightHex: 0xD9457F, darkHex: 0xCF4F7C)
        static let chartCatEdit = AdaptiveColor(lightHex: 0x1596B4, darkHex: 0x10A6B0)
        // Neutral chart geometry (C42): timeline spine, activity histogram,
        // span lines and the tool-call "Other" slot. One token instead of
        // muted/accent.opacity, so it holds the same weight in both modes
        // (8.33:1 on card light, 7.89:1 dark; 7.78:1 / 8.63:1 on well).
        // Validated with the categorical hues above (dataviz validator,
        // --pairs all): light worst CVD ΔE 9.6 / normal 18.1, dark worst CVD
        // ΔE 7.9 (the existing cyan↔pink pair) / normal 15.6. A mid-lightness
        // gray was rejected: it collapses into the pink under protan (ΔE <4).
        // Chroma-floor "fails" are by design — this slot is the neutral.
        static let chartNeutral = AdaptiveColor(lightHex: 0x484F54, darkHex: 0xB0BABE)

        // Source identity on the Work timeline ONLY: a scoped categorical
        // encoding so a cross-source folder reads at a glance. Every agent
        // agentacct captures gets a hue; distinct from the rationed semantic
        // palette — it never means good/bad, only "which tool".
        //
        // These are `chart*` tokens on purpose. They paint a legend swatch, a
        // lane bar and a lane label: data marks, so K04 applies and the
        // AccentReservationTests floor (OKLab ΔE ≥ 12 from the resting accent,
        // both schemes) must hold. The `chart` prefix is what makes
        // `testEveryDeclaredChartTokenIsCovered` demand that. Claude Code used
        // to be painted in the bare accent and Codex in 0x6A4BC0/0xB6A2F0 —
        // ΔE 9.6 light / 7.0 dark from the accent, i.e. inside the accent's own
        // shade family. Both now clear the floor:
        //   Claude ΔE 15.6/17.4, Codex 28.6/24.8, Opencode 17.3/12.5,
        //   Hermes 23.4/15.3; mutually ≥ 15.4 in both schemes.
        // They echo chartBar (violet) and chartCatExecute (pink) at a distance,
        // which is harmless: the Worksets timeline draws no chart* token, so
        // the two sets never share a surface.
        static let chartSourceClaude = AdaptiveColor(lightHex: 0x682AA0, darkHex: 0x9E5CE6)
        static let chartSourceCodex = AdaptiveColor(lightHex: 0x741146, darkHex: 0xD24B93)
        static let chartSourceOpencode = AdaptiveColor(lightHex: 0x0E8494, darkHex: 0x53C6D6)
        static let chartSourceHermes = AdaptiveColor(lightHex: 0xA5457F, darkHex: 0xE39AC8)

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
    static let accentPressed = Palette.accentPressed.color
    /// Fill weight of amber — bars, meters, coverage segments. Text keeps `amber`.
    static let amberFill = Palette.amberFill.color

    // MARK: tints

    static let tintNeutral = Palette.tintNeutral.color
    static let tintAccent = Palette.tintAccent.color
    static let tintGreen = Palette.tintGreen.color
    static let tintAmber = Palette.tintAmber.color
    static let tintCoral = Palette.tintCoral.color
    static let chipBg = Palette.chipBg.color
    static let tintAmberOnCanvas = Palette.tintAmberOnCanvas.color
    static let tintNeutralOnCanvas = Palette.tintNeutralOnCanvas.color

    /// Plot areas and wells nested inside a card. Canvas is the window ground only.
    static let well = Palette.well.color
    /// The raised selected pill / card button on a tray (K43).
    static let thumb = Palette.thumb.color
    /// Hover preview of `thumb` on the canvas tray (solid, never an alpha).
    static let thumbHoverOnCanvas = Palette.thumbHoverOnCanvas.color
    /// Unfilled track of every meter (K44).
    static let meterTrack = Palette.meterTrack.color

    // MARK: lines

    static let rule = Palette.rule.color
    static let hairline = Palette.hairline.color
    static let cardLine = Palette.cardLine.color
    static let chipLine = Palette.chipLine.color

    // MARK: chart

    static let chartBar = Palette.chartBar.color
    static let chartBarDim = Palette.chartBarDim.color
    static let chartCatRead = Palette.chartCatRead.color
    static let chartCatExecute = Palette.chartCatExecute.color
    static let chartCatEdit = Palette.chartCatEdit.color
    /// Neutral chart geometry: timeline spine, histogram, span lines, "Other".
    static let chartNeutral = Palette.chartNeutral.color

    /// The ONE period-chart bar color (C25). Bars rest in full `chartBar`;
    /// `chartBarDim` de-emphasizes the other bars only while the user is
    /// actually pointing at, focusing or has pinned one bar. A default
    /// (programmatic) selection never dims the chart.
    static func periodBarColor(isActive: Bool, hasUserSelection: Bool) -> Color {
        hasUserSelection && !isActive ? chartBarDim : chartBar
    }

    // MARK: source identity (Work timeline only)

    static let chartSourceClaude = Palette.chartSourceClaude.color
    static let chartSourceCodex = Palette.chartSourceCodex.color
    static let chartSourceOpencode = Palette.chartSourceOpencode.color
    static let chartSourceHermes = Palette.chartSourceHermes.color

    /// Which agent a session came from → its bar color on the Work timeline.
    /// A scoped categorical encoding for "which tool", never a semantic claim.
    /// Every agent agentacct captures gets a hue; an unknown source stays muted.
    ///
    /// Claude Code gets `chartSourceClaude`, never the bare `accent` (K04): the
    /// value paints a legend swatch, two lane bars and a lane label, and the
    /// reservation is about the reader's learned "cobalt = I can press this",
    /// not about whether the encoding is semantic. Scoping it to one surface
    /// does not buy an exemption — the reader carries the association across.
    static func sourceColor(_ client: String?) -> Color {
        switch (client ?? "").lowercased() {
        case "claude-code", "claude", "claude code": return chartSourceClaude
        case "codex", "openai-codex", "codex-cli": return chartSourceCodex
        case "opencode", "open-code": return chartSourceOpencode
        case "hermes": return chartSourceHermes
        default: return muted
        }
    }

    /// Session/task lifecycle → decision-axis colors, routed through the ONE
    /// decision lookup (`DecisionTintClass.forKey`) so a key can never be coral
    /// on a badge and cobalt or amber elsewhere (C26). The decision axis never
    /// wears green for claims: "completed" is an assertion, so it stays ink.
    /// Coral is failure-only. The decision axis speaks only in ink, muted and
    /// coral (K03): cobalt is the interactive voice and amber the unverified
    /// evidence tier, so live progress is ink and an inferred stop is muted.
    static func statusColor(_ status: String?) -> Color {
        DecisionTintClass.forKey(status) == .neutral && status == "completed"
            ? ink
            : DecisionTintClass.forKey(status).text
    }

    /// Threshold → TEXT color for a limit percentage, one function for every
    /// surface (K11): ink below the 75% marker, amber from it, coral from the
    /// 90% marker. Never cobalt — percent text is not interactive.
    static func limitTextColor(usedPercent: Double) -> Color {
        if usedPercent >= 90 { return coral }
        if usedPercent >= 75 { return amber }
        return ink
    }

    /// Threshold → FILL color for limit meters and capacity stems (C58/K47),
    /// amber at its fill weight. Below the 75% marker the fill is the neutral
    /// chart geometry token, never cobalt (K04): a meter is data, not a
    /// control. (`rule` was considered but measures under 3:1 on
    /// `meterTrack`; `chartNeutral` clears it in both modes.)
    static func limitFillColor(usedPercent: Double) -> Color {
        if usedPercent >= 100 { return coral }
        if usedPercent >= 75 { return amberFill }
        return chartNeutral
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
        // Exercise the same fallback used when bundled faces are unavailable,
        // without changing fonts installed on the user's machine.
        if ProcessInfo.processInfo.arguments.contains("--snapshot-native-fixture"),
           ProcessInfo.processInfo.environment["AGENTACCT_NATIVE_REVIEW_SYSTEM_FONTS"] == "1" { return nil }
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
    /// Glyph roles (C59). SF Symbols and other inline glyphs are sized ONLY
    /// by these, never an ad-hoc `.system(size:)`, so the 12px floor holds.
    /// Apply with `.workFont(.icon)` / `.workFont(.iconLarge)` so they scale
    /// with the reading size (C80).
    static let icon: CGFloat = 12
    static let iconLarge: CGFloat = 16
}

/// Work keeps the design system's compact base sizes, but these equivalents
/// participate in Dynamic Type. WorkPane and its receipt-detail components use
/// them together so accessibility settings scale the whole task record rather
/// than only changing its column arrangement.
enum WorkFontRole {
    case titlePage, titleSection, titleCard, kpi, rowLabel, body, caption, captionSemibold
    case dataSmall, dataSmallSemibold, labelCaps
    /// Glyph roles: `Type.icon` (12) and `Type.iconLarge` (16), scaled.
    case icon, iconLarge

    var metrics: (size: CGFloat, weight: Font.Weight, relativeTo: Font.TextStyle, monospaced: Bool) {
        switch self {
        case .icon: return (Type.icon, .semibold, .caption, false)
        case .iconLarge: return (Type.iconLarge, .regular, .body, false)
        case .titlePage: return (26, .semibold, .title, false)
        case .titleSection: return (20, .semibold, .title2, false)
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
        case .titleSection: return Type.titleSection
        case .titleCard: return Type.titleCard
        case .kpi: return Type.kpi
        case .rowLabel: return Type.rowLabel
        case .body: return Type.body
        case .caption: return Type.caption
        case .captionSemibold: return Type.captionSemibold
        case .dataSmall: return Type.dataSmall
        case .dataSmallSemibold: return Type.dataSmallSemibold
        case .labelCaps: return Type.labelCaps
        case .icon: return Font.system(size: Type.icon, weight: .semibold)
        case .iconLarge: return Font.system(size: Type.iconLarge, weight: .regular)
        }
    }
}

/// One font role per presentation FIELD on every surface (K10). Mono is only
/// for updating numerals, ids, paths, commands and timestamps; the reducer's
/// prose — gap lines, named absences, basis qualifiers, reset phrases,
/// subtitles — is sans wherever it is drawn, so the same string never changes
/// face between the table, the record, the menu and Usage.
/// `FieldFontRoleTests` asserts every role here is sans and scans the sources
/// so these fields are never set in a mono role.
enum FieldFont {
    /// `verdict.gap_label — gap_text`, and the ledger text beside it.
    static let gapLine: WorkFontRole = .caption
    /// A named absence at value position (`no usage recorded`).
    static let absence: WorkFontRole = .caption
    /// The basis / provenance qualifier under or beside a value.
    static let qualifier: WorkFontRole = .caption
    /// A limit window's reset phrase (`resets in 4d 3h`, `reset time not reported`).
    static let resetText: WorkFontRole = .caption
    /// Page and section subtitles and freshness sentences.
    static let subtitle: WorkFontRole = .caption

    static let all: [(String, WorkFontRole)] = [
        ("gapLine", gapLine), ("absence", absence), ("qualifier", qualifier),
        ("resetText", resetText), ("subtitle", subtitle),
    ]

    /// THE face rule (K10). Every surface that draws a payload string at value
    /// position asks this — never its own `if gradeable` — so one string never
    /// changes face between the table, the record, the menu and Usage (the
    /// defect: `not gradeable` was mono in the Work table's COVERAGE cell and
    /// sans in the record's COVERAGE tile, same task, same string).
    ///
    /// `metricRole` is the surface's own metric role (`.kpi` in a tile,
    /// `.dataSmall` in a table cell). A measured figure keeps it; anything that
    /// is prose — a named absence, a named conflict, a reason — takes the sans
    /// role of the same reading size via `prose(_:)`.
    static func value(_ metricRole: WorkFontRole, isMetric: Bool) -> WorkFontRole {
        isMetric ? metricRole : prose(metricRole)
    }

    /// The sans counterpart of a role, at the same reading size. Roles that are
    /// already sans are returned unchanged, so the rule is safe to apply
    /// anywhere.
    static func prose(_ role: WorkFontRole) -> WorkFontRole {
        switch role {
        // A named absence at a KPI's position reads as a short sentence, not as
        // an 18pt bold numeral that happens to be spelled with letters.
        case .kpi: return .body
        case .dataSmall: return .caption
        case .dataSmallSemibold: return .captionSemibold
        default: return role
        }
    }
}

/// Some macOS hosts retain base ScaledMetric values even when the reading-size
/// environment changes. Keep an explicit minimum ramp; respect a larger system
/// scale, and preserve the existing default typography.
enum WorkTypeScale {
    static func resolved(base: CGFloat, systemScaled: CGFloat, dynamicTypeSize: DynamicTypeSize) -> CGFloat {
        let factor: CGFloat
        switch dynamicTypeSize {
        case .xSmall: factor = 0.85
        case .small: factor = 0.925
        case .medium, .large: return base
        case .xLarge: factor = 1.1
        case .xxLarge: factor = 1.2
        case .xxxLarge: factor = 1.3
        case .accessibility1: factor = 1.45
        case .accessibility2: factor = 1.65
        case .accessibility3: factor = 1.85
        case .accessibility4: factor = 2.05
        case .accessibility5: factor = 2.3
        @unknown default: factor = 1
        }
        return factor < 1 ? min(systemScaled, base * factor) : max(systemScaled, base * factor)
    }
}

private struct WorkScaledFontModifier: ViewModifier {
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @ScaledMetric private var scaledSize: CGFloat
    let baseFont: Font
    let baseSize: CGFloat
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
        self.baseSize = size
        self.weight = weight
        self.monospaced = monospaced
    }

    func body(content: Content) -> some View {
        if dynamicTypeSize == .medium || dynamicTypeSize == .large {
            return content.font(baseFont)
        } else {
            let size = max(12, WorkTypeScale.resolved(base: baseSize, systemScaled: scaledSize, dynamicTypeSize: dynamicTypeSize))
            let font: Font
            if monospaced {
                if let name = Face.mono {
                    font = .custom(name, size: size).weight(weight).monospacedDigit()
                } else {
                    font = .system(size: size, weight: weight, design: .monospaced).monospacedDigit()
                }
            } else if let name = Face.sans {
                font = .custom(name, size: size).weight(weight)
            } else {
                font = .system(size: size, weight: weight)
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
    /// Clear space between a control's edge and its focus ring. The ring sits
    /// OUTSIDE the control, so focus can never be read as the selection's
    /// inset stroke (K117).
    static let focusGap: CGFloat = 2

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

    /// Longest comfortable line for prose paragraphs (C61).
    static let readingMeasure: CGFloat = 680
    /// The ONE page content cap (C64): every pane leading-aligns its content
    /// inside this width via `.pageFrame()`.
    static let pageMaxWidth: CGFloat = 1172 + 2 * Space.gutter
}

extension View {
    /// Caps a pane's content at `Metrics.pageMaxWidth`, leading-aligned. Every
    /// pane uses this one rule so wide windows leave the same right margin.
    ///
    /// The outer `.infinity` frame is part of the rule, not the caller's job:
    /// without it the enclosing ScrollView centers the capped column, so
    /// switching tabs slid the whole page sideways on wide windows. Dashboard
    /// was the one pane that had left it off (K112).
    func pageFrame() -> some View {
        frame(maxWidth: Metrics.pageMaxWidth, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// A fixed geometry value (glyph box, dot, hit target) that follows the
/// reading size through the same ramp as `workFont` (C80).
private struct WorkScaledFrameModifier: ViewModifier {
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @ScaledMetric private var scaledWidth: CGFloat
    @ScaledMetric private var scaledHeight: CGFloat
    let width: CGFloat?
    let height: CGFloat?
    let minimum: Bool
    let alignment: Alignment

    init(width: CGFloat?, height: CGFloat?, minimum: Bool, alignment: Alignment, relativeTo: Font.TextStyle) {
        _scaledWidth = ScaledMetric(wrappedValue: width ?? 0, relativeTo: relativeTo)
        _scaledHeight = ScaledMetric(wrappedValue: height ?? 0, relativeTo: relativeTo)
        self.width = width
        self.height = height
        self.minimum = minimum
        self.alignment = alignment
    }

    private func resolved(_ base: CGFloat?, _ scaled: CGFloat) -> CGFloat? {
        guard let base else { return nil }
        return WorkTypeScale.resolved(base: base, systemScaled: scaled, dynamicTypeSize: dynamicTypeSize)
    }

    func body(content: Content) -> some View {
        let w = resolved(width, scaledWidth)
        let h = resolved(height, scaledHeight)
        if minimum {
            content.frame(minWidth: w, minHeight: h, alignment: alignment)
        } else {
            content.frame(width: w, height: h, alignment: alignment)
        }
    }
}

extension View {
    /// An exact frame whose base size scales with the reading size.
    func workScaledFrame(
        width: CGFloat? = nil,
        height: CGFloat? = nil,
        alignment: Alignment = .center,
        relativeTo: Font.TextStyle = .body
    ) -> some View {
        modifier(WorkScaledFrameModifier(width: width, height: height, minimum: false, alignment: alignment, relativeTo: relativeTo))
    }

    /// A minimum frame whose base size scales with the reading size.
    func workScaledMinFrame(
        width: CGFloat? = nil,
        height: CGFloat? = nil,
        alignment: Alignment = .center,
        relativeTo: Font.TextStyle = .body
    ) -> some View {
        modifier(WorkScaledFrameModifier(width: width, height: height, minimum: true, alignment: alignment, relativeTo: relativeTo))
    }

    /// The scaled `ButtonFeedback.minimumHitDimension` square (28pt at the
    /// default reading size). Apply BEFORE `.contentShape` so the whole
    /// target is hittable.
    func minimumHitTarget(alignment: Alignment = .center) -> some View {
        modifier(MinimumHitTargetModifier(alignment: alignment))
    }
}

private struct MinimumHitTargetModifier: ViewModifier {
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    let alignment: Alignment

    func body(content: Content) -> some View {
        let side = ButtonFeedback.scaledMinimumHitDimension(for: dynamicTypeSize)
        content.frame(minWidth: side, minHeight: side, alignment: alignment)
    }
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
    /// This is the BASE value at the default reading size; it scales with the
    /// reading size through `scaledMinimumHitDimension(for:)` and the
    /// `.minimumHitTarget()` modifier (C73/C80).
    static let minimumHitDimension: CGFloat = 28

    /// The hit minimum at a given reading size (never below the 28pt base).
    static func scaledMinimumHitDimension(for dynamicTypeSize: DynamicTypeSize) -> CGFloat {
        max(minimumHitDimension, WorkTypeScale.resolved(
            base: minimumHitDimension,
            systemScaled: minimumHitDimension,
            dynamicTypeSize: dynamicTypeSize
        ))
    }

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

    /// The label color a tinted button style applies (K13): an explicitly
    /// passed tint colors the label; nil leaves the label's own foreground
    /// (ink unless the call site styles it).
    static func labelColor(tint: Color?) -> Color? { tint }

    /// Filled-chrome colors of the primary button per phase (K06). A disabled
    /// control is not interactive, so it drops the accent fill for the
    /// neutral wash with a muted label (5.14:1 light, 5.73:1 dark).
    static func primaryChrome(for phase: ButtonInteractionPhase) -> (fill: Color, label: Color) {
        switch phase {
        case .disabled: return (Theme.tintNeutral, Theme.muted)
        case .pressed: return (Theme.accentPressed, Theme.onAccent)
        case .idle, .hovered: return (Theme.accent, Theme.onAccent)
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
///
/// Green is reserved (C27): in the evidence grammar ONLY
/// `EvidenceTierStyle.forGrade("externally_verified")` returns it, and
/// outside the grammar only the live-connection `StatusDot`. A passed check,
/// a "strong" evidence word or a clear attention state takes its grade's tier
/// color or ink/muted — never green by source or by word.
enum PipShape {
    case filled
    case half
    case hollow
    case verified
}

/// A single evidence pip (r4 by default). Draws the tier shape flat.
///
/// Constructed ONLY from a tier key (K05): the pip shapes are the evidence
/// tier grammar, so a pip never marks a gap, a lifecycle step, a bullet or a
/// connection state. `grade: nil` is the named "none / not gradeable" tier.
/// `inactive` draws a tier's shape in muted — a legend for a tier that is not
/// present (e.g. what a verifier WOULD produce) — never another meaning.
/// Non-tier caveats use `CaveatMarker`; live connection uses `StatusDot`.
struct EvidencePip: View {
    let shape: PipShape
    let tint: Color
    private let baseRadius: CGFloat
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @ScaledMetric private var scaledRadius: CGFloat

    init(grade: String?, inactive: Bool = false, radius: CGFloat = Metrics.pipR) {
        let style = EvidenceTierStyle.forGrade(grade)
        self.shape = style.pip
        self.tint = inactive ? Theme.muted : style.tint
        self.baseRadius = radius
        _scaledRadius = ScaledMetric(wrappedValue: radius, relativeTo: .caption)
    }

    /// Pip SHAPE carries the evidence tier, so the glyph follows the reading
    /// size through the same ramp as the type it sits beside (C80/K26). At
    /// accessibility sizes a fixed 4pt disc could not be told from a hollow
    /// ring next to 40pt text. Stroke and inner disc stay proportional, so the
    /// shape reads identically at every size (and exactly as before at the
    /// default one).
    private var radius: CGFloat {
        WorkTypeScale.resolved(base: baseRadius, systemScaled: scaledRadius, dynamicTypeSize: dynamicTypeSize)
    }

    var body: some View {
        let radius = self.radius
        // The 1.5pt stroke of the base pip, kept in proportion as it grows.
        let stroke = 1.5 * radius / max(baseRadius, 0.001)
        ZStack {
            switch shape {
            case .filled:
                Circle().fill(tint)
            case .half:
                Circle().strokeBorder(tint, lineWidth: stroke)
                HalfDisc().fill(tint)
            case .hollow:
                Circle().strokeBorder(tint, lineWidth: stroke)
            case .verified:
                Circle().strokeBorder(tint, lineWidth: stroke)
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

    /// The FILL weight of the tier color for bars and segments (C58): amber
    /// tiers fill with `amberFill`; every other tier fills with its tint.
    var fillWeight: Color? = nil
    var fill: Color { fillWeight ?? tint }

    static func forGrade(_ grade: String?) -> EvidenceTierStyle {
        // The `label` here is a LAST-RESORT fallback for a payload that carried
        // no tier label, so each one must stay character-identical to
        // `display_vocabulary.TIER_LABELS` / `EVIDENCE_GRADE_LABELS` — a
        // hyphenated variant of "externally verified" is a second vocabulary.
        // `tests/test_surface_parity.py` reads this switch and pins every label
        // to the Python table, so the two can no longer drift apart.
        switch grade {
        case "externally_verified":
            return .init(pip: .verified, tint: Theme.green, tintBg: Theme.tintGreen, label: "externally verified")
        case "independently_checked":
            return .init(pip: .filled, tint: Theme.ink, tintBg: Theme.tintNeutral, label: "independently checked")
        case "self_checked":
            // A tier mark is DATA — a coverage segment, a pip beside a count.
            // It wore `Theme.accent`, the app's one interactive voice, so the
            // filled half of every coverage bar read as a control (K04 again,
            // this time through the tier table rather than a chart token). It
            // takes the chart voice instead; the SHAPE (half pip / filled
            // segment) still carries the tier.
            return .init(pip: .half, tint: Theme.chartBar, tintBg: Theme.tintNeutral, label: "self-checked")
        case "claimed":
            // A step marked done with only the agent's claim reads "unchecked"
            // in Python (`EVIDENCE_GRADE_LABELS["claimed"]`), never "claimed".
            return .init(pip: .hollow, tint: Theme.amber, tintBg: Theme.tintAmber, label: "unchecked", fillWeight: Theme.amberFill)
        case "unchecked":
            return .init(pip: .hollow, tint: Theme.amber, tintBg: Theme.tintAmber, label: "unchecked", fillWeight: Theme.amberFill)
        case "none":
            // Python's `evidence_grade_label` reads the "none" grade — and a
            // missing one — as "not graded". Echoing the raw key here was a
            // second vocabulary the reviewer would see beside the CLI's word.
            return .init(pip: .hollow, tint: Theme.muted, tintBg: Theme.tintNeutral, label: "not graded")
        default:
            // A grade neither side knows: de-snaked, exactly as Python's
            // fallback spells it (`text.replace("_", " ")`); a missing grade is
            // the "none" tier and takes its word.
            return .init(
                pip: .hollow, tint: Theme.muted, tintBg: Theme.tintNeutral,
                label: grade.map { $0.replacingOccurrences(of: "_", with: " ") } ?? "not graded"
            )
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
            EvidencePip(grade: grade)
            Text(text ?? style.label)
                .workFont(.captionSemibold)
                .foregroundStyle(style.tint)
                // A badge never wraps mid-word; the row around it reflows (C32).
                .lineLimit(1)
                .fixedSize(horizontal: true, vertical: false)
        }
        .padding(.horizontal, 11)
        .frame(minHeight: Metrics.tierBadgeH)
        .background(style.tintBg, in: RoundedRectangle(cornerRadius: Metrics.radius))
    }
}

// MARK: - Decision axis

/// Decision badge tint classes. The decision axis carries no evidence shapes
/// (no pip) and speaks ONLY in ink, muted and coral, told apart by container
/// (K03): cobalt stays the one interactive voice, amber stays the unverified
/// evidence tier and green stays live connection + externally-verified
/// evidence, so a decision badge can never read as a tier badge or a button.
/// Loudness follows the strength of the assertion, never the reverse.
/// Families:
/// * danger — coral on the coral wash: needs the user (blocked / failed /
///   finding).
/// * verified — ink, a solid 1pt `rule` outline, no fill: the settled,
///   verified decision.
/// * claimed — ink on the chip wash: done-ish on a claim's strength (the agent
///   said so, or a stop was its deliberate last word).
/// * accent (live) — ink, a dashed `rule` outline, no fill: still in flight.
/// * inferredStop — muted on the chip wash with a dashed rule: agentacct
///   inferred the stop, honestly weaker than a claim.
/// * neutral (inactive / unknown) — muted, no container at all.
enum DecisionTintClass: CaseIterable {
    case neutral
    case accent
    case claimed
    case inferredStop
    case danger
    case verified

    /// The container a decision badge draws around its word.
    enum Border: Equatable {
        case none
        case solid
        case dashed
    }

    static func forKey(_ key: String?) -> DecisionTintClass {
        switch key {
        case "blocked", "failed", "finding": return .danger
        case "in_progress", "started", "checkpoint": return .accent
        case "reported", "resolved", "mostly_done", "handed_off", "finding_superseded",
             "finding_resolved_by_user", "blocker_resolved_by_user":
            return .claimed
        case "ended_open": return .inferredStop
        // Inactive is agentacct's own inference (asserted_by='inferred'), weaker
        // than any claim. It must NOT read done-ish like the claimed family, must
        // NOT alarm like a finding, and must NOT be green. A quiet muted word
        // with no container carries the honest "not a completion, not a stated
        // stop" meaning the Inactive label already states. (Explicit, so it
        // never drifts into the unknown default.)
        case "inactive": return .neutral
        case "verified": return .verified
        default: return .neutral
        }
    }

    /// Text weight of the class.
    var text: Color {
        switch self {
        case .danger: return Theme.coral
        case .verified, .claimed, .accent: return Theme.ink
        case .inferredStop, .neutral: return Theme.muted
        }
    }

    /// FILL weight of the class for filled marks (attention stems, bars),
    /// mirroring `EvidenceTierStyle.fill` (K47): only text takes the text
    /// weight. The decision palette has one weight per hue today; the accessor
    /// keeps stems on the fill lookup if that ever changes.
    var fill: Color {
        switch self {
        case .danger: return Theme.coral
        case .verified, .claimed, .accent: return Theme.ink
        case .inferredStop, .neutral: return Theme.muted
        }
    }

    var wash: Color {
        switch self {
        case .danger: return Theme.tintCoral
        case .claimed, .inferredStop: return Theme.chipBg
        case .verified, .accent, .neutral: return .clear
        }
    }

    var border: Border {
        switch self {
        case .verified: return .solid
        case .accent, .inferredStop: return .dashed
        case .claimed, .danger, .neutral: return .none
        }
    }

    /// Whether the badge draws an outline (solid or dashed).
    var outlined: Bool { border != .none }

    /// A class with no container renders its word alone (no padding box).
    var hasContainer: Bool { self != .neutral }
}

/// Decision badge: h26 page variant / h20 row variant, rx4, no pip.
struct DecisionBadge: View {
    let key: String?
    let label: String
    var compact = false
    /// Optional hover explanation (e.g. the payload decision statement, C71).
    var help: String? = nil

    var body: some View {
        let tint = DecisionTintClass.forKey(key)
        Text(label)
            .workFont(
                size: compact ? 12 : 13,
                weight: .semibold,
                relativeTo: .caption
            )
            .lineLimit(1)
            .fixedSize(horizontal: true, vertical: false)
            .foregroundStyle(tint.text)
            .padding(.horizontal, tint.hasContainer ? (compact ? 8 : 12) : 0)
            .frame(minHeight: compact ? Metrics.decisionBadgeRowH : Metrics.decisionBadgeH)
            .background(tint.wash, in: RoundedRectangle(cornerRadius: Metrics.radius))
            .overlay {
                switch tint.border {
                case .none:
                    EmptyView()
                case .solid:
                    RoundedRectangle(cornerRadius: Metrics.radius)
                        .strokeBorder(Theme.rule, lineWidth: Metrics.borderW)
                case .dashed:
                    RoundedRectangle(cornerRadius: Metrics.radius)
                        .strokeBorder(Theme.rule, style: StrokeStyle(lineWidth: Metrics.borderW, dash: [3, 2]))
                }
            }
            .modifier(OptionalHelp(text: help))
    }
}

/// Attaches `.help` only when there is copy; an empty tooltip is never shown.
struct OptionalHelp: ViewModifier {
    let text: String?

    func body(content: Content) -> some View {
        if let text, !text.isEmpty {
            content.help(text)
        } else {
            content
        }
    }
}

// MARK: - Chips and labels

/// Provenance chip: h20 fully-round, chip wash + border, 12/600 sans muted
/// text (K10: a chip names a source or basis in words — Client log, Agent
/// report, Pricing table — so it takes the label face, not the metric face).
/// `mono: true` is reserved for identifiers (a client slug, a task id).
struct ProvenanceChip: View {
    let text: String
    var tint: Color = Theme.muted
    var mono = false

    var body: some View {
        Text(text)
            .workFont(ChipFace.role(mono: mono))
            .lineLimit(1)
            .fixedSize(horizontal: true, vertical: false)
            .foregroundStyle(tint)
            .padding(.horizontal, 12)
            .frame(minHeight: Metrics.chipH)
            .background(Theme.chipBg, in: Capsule())
            .overlay(Capsule().strokeBorder(Theme.chipLine, lineWidth: Metrics.borderW))
    }
}

/// The face of chip text (K10): state words and bases are prose (12/600
/// sans); only identifiers keep the mono face.
enum ChipFace {
    static func role(mono: Bool) -> WorkFontRole { mono ? .dataSmall : .captionSemibold }
}

/// A small tinted label chip (fully round). The general-purpose pill for
/// inline state words; tier words should use TierBadge instead.
struct Chip: View {
    let text: String
    var tint: Color = Theme.muted
    /// Identifiers only (client slug, id): keeps the mono face.
    var mono = false

    var body: some View {
        Text(text)
            .workFont(ChipFace.role(mono: mono))
            .lineLimit(1)
            .fixedSize(horizontal: true, vertical: false)
            .foregroundStyle(tint)
            .padding(.horizontal, 10)
            .frame(minHeight: Metrics.chipH)
            .background(Theme.chipBg, in: Capsule())
            .overlay(Capsule().strokeBorder(Theme.chipLine, lineWidth: Metrics.borderW))
    }
}

/// A `Chip` that stays inside a narrow column: the one-line capsule when it
/// fits the proposed width, otherwise the same words wrapped inside a
/// squared (radius 4) outline, so a long chip can never spill across a
/// neighbouring column.
struct FittingChip: View {
    let text: String
    var tint: Color = Theme.muted
    var mono = false

    var body: some View {
        ViewThatFits(in: .horizontal) {
            Chip(text: text, tint: tint, mono: mono)
            Text(text)
                .workFont(ChipFace.role(mono: mono))
                .lineLimit(3)
                .multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)
                .foregroundStyle(tint)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .frame(minHeight: Metrics.chipH, alignment: .leading)
                .background(Theme.chipBg, in: RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
                        .strokeBorder(Theme.chipLine, lineWidth: Metrics.borderW)
                )
        }
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
/// Reserved for LIVE CONNECTION (green) — never a decision, a tier or a
/// bullet (K05). A non-connected state uses `DisconnectedMarker`.
struct StatusDot: View {
    let color: Color
    var size: CGFloat = 7

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: size, height: size)
    }
}

/// The marker for a state that is not a live connection (K05): a flat minus,
/// never a circle (circles are the pip/StatusDot family).
struct DisconnectedMarker: View {
    var tint: Color = Theme.muted

    var body: some View {
        Image(systemName: "minus")
            .workFont(.icon)
            .foregroundStyle(tint)
            .accessibilityHidden(true)
    }
}

/// The marker for caveat prose that is NOT an evidence-tier fact (K05): a gap
/// row, a scope note, a plan-share state. Pip shapes are reserved for typed
/// tier facts, so a caveat carries a text glyph in muted.
struct CaveatMarker: View {
    var body: some View {
        Image(systemName: "text.alignleft")
            .workFont(.icon)
            .foregroundStyle(Theme.muted)
            .accessibilityHidden(true)
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
        .background(Theme.tintNeutralOnCanvas, in: RoundedRectangle(cornerRadius: Metrics.radius))
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
        // One VoiceOver stop per tile ("label, value, detail") instead of three.
        .accessibilityElement(children: .combine)
    }
}

/// The app's ONE focus indicator (K117): a 2pt accent stroke drawn OUTSIDE the
/// control with a 2pt gap. Accent is the interactive voice, so focus needs no
/// reserved semantic colour, and the outset shape keeps it distinct from
/// SELECTION, which is an inset accent stroke plus a fill. The ring is an
/// overlay with negative padding: it changes no layout and moves nothing.
///
/// A surface that runs to the edge of its container (a full-bleed table row, a
/// card inside a clipping canvas) would lose an outset ring to the clip, so it
/// passes an `inset` instead and draws the same stroke just inside its edge.
struct FocusRing: ViewModifier {
    let focused: Bool
    var cornerRadius: CGFloat = Metrics.radius
    /// Points INSIDE the control's edge, or nil for the standard outset ring.
    var inset: CGFloat?

    func body(content: Content) -> some View {
        let outset = Metrics.focusGap + Metrics.focusW / 2
        content.overlay {
            if focused {
                RoundedRectangle(
                    cornerRadius: max(cornerRadius + (inset == nil ? outset : -(inset ?? 0)), 0),
                    style: .continuous
                )
                .strokeBorder(Theme.accent, lineWidth: Metrics.focusW)
                .padding(inset ?? -outset)
                .allowsHitTesting(false)
            }
        }
    }
}

extension View {
    /// Draw the app's focus ring while `focused`. Pass `inset` only for a
    /// surface whose ring would otherwise be clipped.
    func focusRing(
        _ focused: Bool,
        cornerRadius: CGFloat = Metrics.radius,
        inset: CGFloat? = nil
    ) -> some View {
        modifier(FocusRing(focused: focused, cornerRadius: cornerRadius, inset: inset))
    }
}

/// Puts a control in the Tab key loop, and makes Return press it.
///
/// macOS ships with Full Keyboard Access OFF, and under that default a plain
/// SwiftUI `Button` is NOT focusable: Tab walks text fields and the handful of
/// containers that asked for focus, so a document made of buttons is
/// unreachable without a mouse — an audit tabbed the whole record page and got
/// ten stops, none of them on it (K130). Every control a reviewer must be able
/// to reach says so here.
///
/// It adds no second focus treatment: the ring comes from the control's own
/// button style, which reads `\.isFocused` from the environment this modifier
/// sets, and the system's effect is disabled so exactly one indicator is drawn.
/// Return is bound explicitly because the key press is answered by THIS
/// focusable wrapper, not by the button inside it.
/// A ring nobody can see is the same defect as no ring at all, so a stop that
/// arrives off screen scrolls itself into view — through the app's ONE reveal
/// policy, `KeepRegionOnScreen`, which declines to move a control that already
/// fits and otherwise moves the page the minimum distance.
struct KeyboardStop: ViewModifier {
    var enabled: Bool = true
    /// What Return does — the same action a click performs. Pass nil only for a
    /// control that answers keys itself (a `Menu`, or a view with its own
    /// `onKeyPress`).
    var activate: (() -> Void)?

    @FocusState private var focused: Bool
    @State private var revealRequest = 0

    func body(content: Content) -> some View {
        // The offscreen renderer draws resting labels and has no key loop.
        if SnapshotMode.enabled && !SnapshotMode.interactiveFixture {
            content
        } else {
            content
                // Stated, not inferred: `\.isFocused` stays false for a style
                // body under its OWN `.focusable()`, so the ring needs the
                // answer handed to it.
                .environment(\.keyboardStopIsFocused, focused && enabled)
                .focusable(enabled)
                .focusEffectDisabled()
                .focused($focused)
                .onChange(of: focused) { _, isFocused in
                    guard isFocused else { return }
                    revealRequest += 1
                }
                .background(KeepRegionOnScreen(request: revealRequest) {})
                .onKeyPress(.return) {
                    guard enabled, let activate else { return .ignored }
                    activate()
                    return .handled
                }
        }
    }
}

extension View {
    /// Make this control a Tab stop that Return activates. See `KeyboardStop`.
    func keyboardStop(_ enabled: Bool = true, activate: (() -> Void)? = nil) -> some View {
        modifier(KeyboardStop(enabled: enabled, activate: activate))
    }
}

/// Does the control under this view hold the keyboard focus?
///
/// SwiftUI's own `\.isFocused` answers only for a focusable ANCESTOR — put
/// `.focusable()` on a styled `Button` itself and the style's body still reads
/// false, so the ring never drew (which is why the canvas cards, focusable
/// since K99, photographed identically focused and not). `KeyboardStop`
/// therefore states the answer, and every button style that draws a ring reads
/// it alongside `\.isFocused`.
private struct KeyboardStopFocusKey: EnvironmentKey {
    static let defaultValue = false
}

extension EnvironmentValues {
    var keyboardStopIsFocused: Bool {
        get { self[KeyboardStopFocusKey.self] }
        set { self[KeyboardStopFocusKey.self] = newValue }
    }
}

/// A quiet macOS action: no fill at rest unless it is the local primary
/// action, then short color-only hover and press feedback. These non-spatial
/// acknowledgements remain enabled when Reduce Motion is on.
///
/// `tint` (K13): when a tint is passed explicitly the label takes it, so a
/// `QuietButtonStyle(tint: Theme.accent)` action reads cobalt at rest, not as
/// static ink text. With no tint the label inherits its own foreground (ink
/// by default) and the hover/press wash is the accent.
struct QuietButtonStyle: ButtonStyle {
    var tint: Color? = nil
    var prominent = false
    var horizontalPadding: CGFloat = 7
    var verticalPadding: CGFloat = 6
    /// Draws a hairline `Theme.cardLine` capsule at rest, for a control that
    /// must read as a switch even before it is hovered. A heading full of
    /// chrome-less quiet buttons reads as prose, which is how a reviewer came
    /// to experience the surface switch as a deleted feature; the affordance is
    /// a hairline rather than a fill because accent is the interactive VOICE
    /// and a resting switch should not shout. The hover and press wash follows
    /// the same shape so nothing pokes out past the outline.
    var resting = false

    func makeBody(configuration: Configuration) -> some View {
        QuietButtonBody(
            configuration: configuration,
            tint: tint,
            prominent: prominent,
            horizontalPadding: horizontalPadding,
            verticalPadding: verticalPadding,
            resting: resting
        )
    }
}

private struct QuietButtonBody: View {
    let configuration: ButtonStyleConfiguration
    let tint: Color?
    let prominent: Bool
    let horizontalPadding: CGFloat
    let verticalPadding: CGFloat
    let resting: Bool

    @State private var hovering = false
    @Environment(\.isFocused) private var ambientFocus
    @Environment(\.keyboardStopIsFocused) private var stopFocus
    @Environment(\.isEnabled) private var isEnabled

    /// A focusable ancestor, or this control's own `KeyboardStop`.
    private var isFocused: Bool { ambientFocus || stopFocus }

    private var phase: ButtonInteractionPhase {
        buttonInteractionPhase(
            isEnabled: isEnabled,
            isPressed: configuration.isPressed,
            isHovering: hovering
        )
    }

    var body: some View {
        configuration.label
            .modifier(OptionalForeground(color: ButtonFeedback.labelColor(tint: tint)))
            .padding(.horizontal, horizontalPadding)
            .padding(.vertical, verticalPadding)
            .modifier(QuietButtonChrome(
                fill: (tint ?? Theme.accent).opacity(ButtonFeedback.quietFillOpacity(for: phase, prominent: prominent)),
                resting: resting
            ))
            .opacity(ButtonFeedback.labelOpacity(for: phase))
            // The ONE focus indicator, following the resting shape: a radius
            // SwiftUI clamps to half the height, so the ring is a capsule
            // around a capsule rather than a rounded box around a pill.
            .focusRing(isFocused && isEnabled,
                       cornerRadius: resting ? Metrics.buttonH / 2 : Metrics.radius)
            .contentShape(Rectangle())
            .onHover { inside in
                withAnimation(Motion.hover) {
                    hovering = inside
                }
            }
            .animation(Motion.feedback, value: phase)
    }
}

/// A quiet button's fill, and — for a control that must read as a switch at
/// rest — its hairline capsule. Both share one shape, so the wash never pokes
/// out past the outline.
private struct QuietButtonChrome: ViewModifier {
    let fill: Color
    let resting: Bool

    @ViewBuilder func body(content: Content) -> some View {
        if resting {
            content
                .background(fill, in: Capsule())
                .overlay(Capsule().strokeBorder(Theme.cardLine, lineWidth: Metrics.borderW))
        } else {
            content.background(fill, in: RoundedRectangle(cornerRadius: Metrics.radius))
        }
    }
}

/// Adds an accessibility hint only when there is one, so a control without a
/// reason keeps whatever hint it already carries.
struct OptionalAccessibilityHint: ViewModifier {
    let hint: String?
    func body(content: Content) -> some View {
        if let hint { content.accessibilityHint(hint) } else { content }
    }
}

/// Applies a foreground style only when there is a color, so an untinted
/// style never overrides the label's inherited foreground.
struct OptionalForeground: ViewModifier {
    let color: Color?

    func body(content: Content) -> some View {
        if let color {
            content.foregroundStyle(color)
        } else {
            content
        }
    }
}

/// Full-width rows, tabs, and disclosure headers that visually own their
/// geometry but still need common hover, press, focus, and disabled feedback.
/// This replaces `.plain`, whose lack of acknowledgement made several controls
/// in Work look like static text. The call site retains all layout ownership.
struct SurfaceButtonStyle: ButtonStyle {
    var tint: Color = Theme.accent
    var cornerRadius: CGFloat = Metrics.radius
    /// Points inside the surface to draw the focus ring, for a full-bleed row
    /// or a card inside a clipping canvas. Everything else takes the standard
    /// outset ring.
    var focusInset: CGFloat? = nil
    /// Who decides this row has keyboard focus.
    ///
    /// nil — the default — reads the ambient `\.isFocused`, which is right for a
    /// button that is itself the focusable element. Pass an explicit value when
    /// an ANCESTOR holds the focus: `\.isFocused` is an ENVIRONMENT value, so a
    /// focusable container sets it for every descendant and each of them draws
    /// its own ring. The task table is deliberately ONE keyboard stop (K77);
    /// before this, entering it ringed every visible row at once — ~4,054
    /// accent border pixels where one ring belongs (K130) — and the roving row
    /// was indistinguishable from the rest. Those rows pass `false` and wear
    /// the reduced-weight selection cue instead.
    var isFocused: Bool? = nil

    /// The call site's answer when it gave one, else whichever focus source
    /// actually holds the keyboard: a focusable ancestor (`ambient`) or this
    /// control's own `KeyboardStop`.
    static func resolvedFocus(declared: Bool?, ambient: Bool, stop: Bool = false) -> Bool {
        declared ?? (ambient || stop)
    }

    @ViewBuilder
    func makeBody(configuration: Configuration) -> some View {
        if SnapshotMode.enabled && !SnapshotMode.interactiveFixture {
            // Static ImageRenderer output needs only the resting label. Native
            // review retains the same full hit area and feedback as the app.
            configuration.label.contentShape(Rectangle())
        } else {
            SurfaceButtonBody(
                configuration: configuration,
                tint: tint,
                cornerRadius: cornerRadius,
                focusInset: focusInset,
                declaredFocus: isFocused
            )
        }
    }
}

private struct SurfaceButtonBody: View {
    let configuration: ButtonStyleConfiguration
    let tint: Color
    let cornerRadius: CGFloat
    let focusInset: CGFloat?
    let declaredFocus: Bool?

    @State private var hovering = false
    @Environment(\.isFocused) private var ambientFocus
    @Environment(\.keyboardStopIsFocused) private var stopFocus
    @Environment(\.isEnabled) private var isEnabled

    private var isFocused: Bool {
        SurfaceButtonStyle.resolvedFocus(
            declared: declaredFocus, ambient: ambientFocus, stop: stopFocus
        )
    }

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
            .focusRing(isFocused && isEnabled, cornerRadius: cornerRadius, inset: focusInset)
            .contentShape(Rectangle())
            .onHover { inside in
                withAnimation(Motion.hover) {
                    hovering = inside
                }
            }
            .animation(Motion.feedback, value: phase)
    }
}

/// The ONE empty / unavailable state (K53).
///
/// Every one of these states says the same things in the same order:
///
/// * a TRUE title naming what is not there, in the surface's own noun;
/// * one short human cause — never the raw developer error, never jargon
///   ("the recorder", not "the daemon");
/// * the raw error text, kept but behind a keyboard-reachable disclosure, so
///   it is copyable without being the message and without hiding in a tooltip;
/// * at most one cobalt action, naming a real destination or the exact retry.
///
/// The action renders in review snapshots too: hiding it behind
/// `!SnapshotMode.enabled` meant every render of a failure state showed a
/// screen with no way out that the live app does offer.
struct EmptyStateView: View {
    struct Action {
        let label: String
        var identifier: String? = nil
        let perform: () -> Void
    }

    let title: String
    var cause: String? = nil
    /// The raw error, behind a disclosure. Never the title, never only help.
    var detailDisclosure: String? = nil
    var action: Action? = nil
    var identifier: String? = nil

    /// The disclosure's one name, wherever a raw error hides behind it.
    static let detailsLabel = "Error details"

    var body: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            Text(title)
                .workFont(.rowLabel)
                .foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
            if let cause {
                Text(cause)
                    .workFont(.caption)
                    .foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: Metrics.readingMeasure, alignment: .leading)
            }
            if let detailDisclosure {
                DisclosureGroup(Self.detailsLabel) {
                    Text(detailDisclosure)
                        .workFont(.caption)
                        .foregroundStyle(Theme.muted)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: Metrics.readingMeasure, alignment: .leading)
                        .padding(.top, Space.xs)
                }
                .workFont(.caption)
                .foregroundStyle(Theme.muted)
                .accessibilityIdentifier("empty-state.details")
            }
            if let action {
                Button(action.label, action: action.perform)
                    .buttonStyle(QuietButtonStyle(tint: Theme.accent))
                    .hangingLeading()
                    .modifier(OptionalAccessibilityIdentifier(identifier: action.identifier))
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .contain)
        .modifier(OptionalAccessibilityIdentifier(identifier: identifier))
    }
}

/// The ONE segmented choice control (K108).
///
/// macOS `.pickerStyle(.segmented)` paints its selected segment in the SYSTEM
/// control accent (a second, brighter blue than `Theme.accent`), takes the
/// system control font instead of the reading-size ramp, and cannot be
/// retinted with `.tint` — so the Usage pane was showing an interactive voice
/// the design language does not have. It also had to be swapped for a single
/// `Chip` in snapshot mode, which hid every unchosen option from review.
///
/// This control is app-drawn: a neutral track, a `Theme.thumb` card under the
/// selected option, ink/muted labels, and the accent used ONLY for hover and
/// focus. It draws itself statically under ImageRenderer with every option
/// visible, so a render shows the shipped control.
struct SegmentedChoice<Value: Hashable>: View {
    let options: [(value: Value, label: String)]
    @Binding var selection: Value
    var accessibilityLabel: String? = nil
    var accessibilityIdentifier: String? = nil

    init(
        options: [(value: Value, label: String)],
        selection: Binding<Value>,
        accessibilityLabel: String? = nil,
        accessibilityIdentifier: String? = nil
    ) {
        self.options = options
        self._selection = selection
        self.accessibilityLabel = accessibilityLabel
        self.accessibilityIdentifier = accessibilityIdentifier
    }

    var body: some View {
        HStack(spacing: 2) {
            ForEach(Array(options.enumerated()), id: \.offset) { _, option in
                let isSelected = option.value == selection
                Button {
                    selection = option.value
                } label: {
                    Text(option.label)
                        .workFont(.captionSemibold)
                        .padding(.horizontal, 9)
                        .frame(height: 24)
                }
                .buttonStyle(SegmentedChoiceButtonStyle(selected: isSelected))
                .accessibilityAddTraits(isSelected ? .isSelected : [])
            }
        }
        .padding(2)
        .background(Theme.tintNeutral, in: RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous))
        .accessibilityElement(children: .contain)
        .modifier(OptionalAccessibilityLabel(label: accessibilityLabel))
        .modifier(OptionalAccessibilityIdentifier(identifier: accessibilityIdentifier))
    }
}

/// One segment of `SegmentedChoice`. The selected option is a `Theme.thumb`
/// card on the neutral track; cobalt appears only as the hover wash and the
/// focus ring, so the control keeps the one interactive voice without
/// inventing a solid cobalt fill that would clash with `PaneTab`.
struct SegmentedChoiceButtonStyle: ButtonStyle {
    let selected: Bool

    @ViewBuilder
    func makeBody(configuration: Configuration) -> some View {
        if SnapshotMode.enabled && !SnapshotMode.interactiveFixture {
            // A render shows the real control with every option: the resting
            // look only, no hover or focus state.
            configuration.label
                .foregroundStyle(selected ? Theme.ink : Theme.muted)
                .background {
                    RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
                        .fill(selected ? Theme.thumb : Color.clear)
                }
        } else {
            SegmentedChoiceButtonBody(configuration: configuration, selected: selected)
        }
    }
}

private struct SegmentedChoiceButtonBody: View {
    let configuration: ButtonStyleConfiguration
    let selected: Bool
    /// The one interactive voice, held as a value so hover comes from the
    /// shared `ButtonFeedback` ramp rather than a second named tint token.
    private let interactive = Theme.accent
    @State private var hovering = false
    @Environment(\.isFocused) private var ambientFocus
    @Environment(\.keyboardStopIsFocused) private var stopFocus
    @Environment(\.isEnabled) private var isEnabled

    /// A focusable ancestor, or this control's own `KeyboardStop`.
    private var isFocused: Bool { ambientFocus || stopFocus }

    private var phase: ButtonInteractionPhase {
        buttonInteractionPhase(
            isEnabled: isEnabled,
            isPressed: configuration.isPressed,
            isHovering: hovering
        )
    }

    var body: some View {
        configuration.label
            .foregroundStyle(selected ? Theme.ink : Theme.muted)
            .background {
                RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
                    .fill(selected ? Theme.thumb : Color.clear)
                RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
                    .fill(interactive.opacity(ButtonFeedback.surfaceFillOpacity(for: phase)))
            }
            .opacity(ButtonFeedback.labelOpacity(for: phase, pressed: 0.82))
            .overlay {
                if isFocused && isEnabled {
                    RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
                        .strokeBorder(Theme.accent, lineWidth: Metrics.focusW)
                }
            }
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
    @Environment(\.isFocused) private var ambientFocus
    @Environment(\.keyboardStopIsFocused) private var stopFocus
    @Environment(\.isEnabled) private var isEnabled

    /// A focusable ancestor, or this control's own `KeyboardStop`.
    private var isFocused: Bool { ambientFocus || stopFocus }

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
            .focusRing(isFocused && isEnabled, cornerRadius: cornerRadius)
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

/// The ONE control filled with the accent (C36). Any control whose fill is
/// `Theme.accent` labels itself with `Theme.onAccent` — never a literal white,
/// which fails contrast on the lighter dark-mode cobalt. Radius 4, pressed
/// fill `accentPressed`, 2pt focus ring. Disabled drops the accent fill for
/// `tintNeutral` with a `muted` label (K06): the control keeps its shape but
/// no longer wears the interactive voice.
struct PrimaryButtonStyle: ButtonStyle {
    var height: CGFloat = Metrics.buttonH

    func makeBody(configuration: Configuration) -> some View {
        PrimaryButtonBody(configuration: configuration, height: height)
    }
}

private struct PrimaryButtonBody: View {
    let configuration: ButtonStyleConfiguration
    let height: CGFloat

    @State private var hovering = false
    @Environment(\.isFocused) private var ambientFocus
    @Environment(\.keyboardStopIsFocused) private var stopFocus
    @Environment(\.isEnabled) private var isEnabled

    /// A focusable ancestor, or this control's own `KeyboardStop`.
    private var isFocused: Bool { ambientFocus || stopFocus }
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    private var phase: ButtonInteractionPhase {
        buttonInteractionPhase(
            isEnabled: isEnabled,
            isPressed: configuration.isPressed,
            isHovering: hovering
        )
    }

    var body: some View {
        let chrome = ButtonFeedback.primaryChrome(for: phase)
        configuration.label
            .modifier(PrimaryButtonChrome(
                height: height,
                fill: chrome.fill,
                label: chrome.label
            ))
            .focusRing(isFocused && isEnabled)
            .contentShape(RoundedRectangle(cornerRadius: Metrics.radius))
            .onHover { inside in
                withAnimation(Motion.hover) { hovering = inside }
            }
            .animation(Motion.feedback, value: phase)
    }
}

/// Static chrome of `PrimaryButtonStyle`: accent fill, onAccent label, rx4.
/// Snapshot stand-ins (`SnapshotMode.rendersStaticControls`) use this so the
/// docs render and the live button can never disagree about label color.
struct PrimaryButtonChrome: ViewModifier {
    var height: CGFloat = Metrics.buttonH
    var fill: Color = Theme.accent
    var label: Color = Theme.onAccent

    func body(content: Content) -> some View {
        content
            .workFont(size: 13, weight: .semibold, relativeTo: .body)
            .lineLimit(1)
            .fixedSize(horizontal: true, vertical: false)
            .foregroundStyle(label)
            .padding(.horizontal, Space.l)
            .workScaledMinFrame(height: height)
            .background(fill, in: RoundedRectangle(cornerRadius: Metrics.radius))
    }
}

/// An icon-only control (C73/C80). The glyph uses the scaled `icon` role, the
/// hit target is the scaled 28pt minimum applied BEFORE the content shape,
/// and the control always carries its accessibility label and hover help —
/// so no call site can ship an unlabeled or undersized glyph button.
struct IconButton: View {
    let systemName: String
    let label: String
    var help: String? = nil
    var tint: Color = Theme.accent
    var role: WorkFontRole = .icon
    var identifier: String? = nil
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: systemName)
                .workFont(role)
                .foregroundStyle(tint)
                .minimumHitTarget()
                .contentShape(Rectangle())
        }
        .buttonStyle(QuietButtonStyle(tint: tint, horizontalPadding: 0, verticalPadding: 0))
        // A glyph-only control is the easiest one to lose: it carries no words,
        // so a reader who cannot use a pointer has nothing to find it by unless
        // it is in the key loop. These are the status legend, every section's
        // help, and every "Copy …" glyph.
        .keyboardStop(activate: action)
        .help(help ?? label)
        .accessibilityLabel(label)
        .modifier(OptionalAccessibilityIdentifier(identifier: identifier))
    }
}

/// A `Menu` that survives the offscreen renderer (K68).
///
/// `ImageRenderer` cannot draw AppKit-backed menu chrome: every record render
/// showed a solid #FFCC00 block with a red prohibition glyph where the
/// timeline's Activity menu belongs — a colour that is in no token and reads
/// as a failure. Every AppKit-backed control in the app goes through a wrapper
/// with a static stand-in (`AppMenuPicker`, the timeline search field); this is
/// that wrapper for menus. The stand-in is the SAME quiet glyph button the
/// live app draws, so a review render never shows a control the app does not.
struct SnapshotSafeMenu<Content: View>: View {
    /// The glyph the live menu uses as its label.
    let systemName: String
    /// What the menu does, for the tooltip and VoiceOver.
    let label: String
    var identifier: String? = nil
    var tint: Color = Theme.muted
    @ViewBuilder let content: () -> Content

    var body: some View {
        if SnapshotMode.enabled && !SnapshotMode.interactiveFixture {
            Image(systemName: systemName)
                .workFont(.icon)
                .foregroundStyle(tint)
                .minimumHitTarget()
                .padding(.horizontal, 8)
                .accessibilityLabel(label)
                .modifier(OptionalAccessibilityIdentifier(identifier: identifier))
        } else {
            Menu(content: content) {
                Image(systemName: systemName)
                    .workFont(.icon)
                    .foregroundStyle(tint)
            }
            .menuStyle(.borderlessButton)
            .buttonStyle(QuietButtonStyle(horizontalPadding: 8))
            .fixedSize()
            .help(label)
            .accessibilityLabel(label)
            .modifier(OptionalAccessibilityIdentifier(identifier: identifier))
        }
    }
}

/// A borderless `Button` that survives the offscreen renderer (K68).
///
/// `.buttonStyle(.borderless)` is AppKit-backed chrome, and `ImageRenderer`
/// paints a solid #FFCC00 block with a red prohibition glyph where it belongs
/// — a colour that is in no token and reads as a failure. Snapshot mode draws
/// the button's OWN label, in the accent the live control tints it with, so a
/// review render shows the control the app actually has. The live app and the
/// interactive fixture keep the real button.
struct SnapshotSafeBorderlessButton<Label: View>: View {
    let action: () -> Void
    @ViewBuilder let label: () -> Label

    var body: some View {
        if SnapshotMode.enabled && !SnapshotMode.interactiveFixture {
            label().foregroundStyle(Theme.accent)
        } else {
            Button(action: action, label: label).buttonStyle(.borderless)
        }
    }
}

/// A `Menu` with a text+glyph label, stood in for offscreen rendering (K68).
struct SnapshotSafeLabelMenu<Content: View>: View {
    let title: String
    let systemName: String
    var help: String? = nil
    var identifier: String? = nil
    @ViewBuilder let content: () -> Content

    var body: some View {
        if SnapshotMode.enabled && !SnapshotMode.interactiveFixture {
            Label(title, systemImage: systemName)
                .workFont(.captionSemibold)
                .foregroundStyle(Theme.accent)
                .padding(.horizontal, 8)
                .workScaledMinFrame(height: ButtonFeedback.minimumHitDimension)
                .modifier(OptionalHelp(text: help))
                .modifier(OptionalAccessibilityIdentifier(identifier: identifier))
        } else {
            Menu(content: content) {
                Label(title, systemImage: systemName)
            }
            .menuStyle(.borderlessButton)
            .buttonStyle(QuietButtonStyle())
            .fixedSize()
            .modifier(OptionalHelp(text: help))
            .modifier(OptionalAccessibilityIdentifier(identifier: identifier))
        }
    }
}

/// The app's one disclosure behaviour: the WHOLE header row toggles the fold,
/// not the small chevron alone (K100).
///
/// The hand-built folds (`OverflowDisclosure`, the receipt check groups, the
/// step headers) already work this way, so a native `DisclosureGroup` — which
/// on macOS only responds to its triangle — made two controls that look alike
/// behave differently. The expanded/collapsed state stays in the element's
/// accessibility value, matching those folds word for word.
struct FullRowDisclosureStyle: DisclosureGroupStyle {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var hovering = false

    func makeBody(configuration: Configuration) -> some View {
        VStack(alignment: .leading, spacing: Space.s) {
            Button {
                withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.15)) {
                    configuration.isExpanded.toggle()
                }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "chevron.right")
                        .workFont(.icon)
                        .rotationEffect(.degrees(configuration.isExpanded ? 90 : 0))
                        .offset(x: hovering && !configuration.isExpanded ? 1 : 0)
                        .accessibilityHidden(true)
                    configuration.label
                    Spacer(minLength: 0)
                }
                .frame(minHeight: 24, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(SurfaceButtonStyle())
            .onHover { inside in
                withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.12)) { hovering = inside }
            }
            .accessibilityValue(configuration.isExpanded ? "Expanded" : "Collapsed")
            if configuration.isExpanded { configuration.content }
        }
    }
}

/// Reserves at least the width of `text`'s longest WORD.
///
/// A container that measures its children at a zero-width proposal (an
/// equal-column `Layout`) is told by `Text` that it can fit in the width of a
/// single glyph, because `Text` will break inside a word rather than report
/// that it does not fit. That is how a tile qualifier came out as
/// "independen / tly / checked" at accessibility sizes (K26). The hidden twin
/// is the longest word alone, unbreakable, set in the same role at the same
/// reading size, so the minimum the container sees is a width the text can
/// actually wrap into at word boundaries.
struct WordSafeWidth: ViewModifier {
    let text: String
    let role: WorkFontRole
    var uppercased: Bool = false
    var tracking: CGFloat = 0

    private var longestWord: String {
        let words: [String] = text.split(separator: " ").flatMap { $0.split(separator: "\n") }.map(String.init)
        let word = words.max(by: { $0.count < $1.count }) ?? text
        return uppercased ? word.uppercased() : word
    }

    func body(content: Content) -> some View {
        ZStack(alignment: .topLeading) {
            Text(longestWord)
                .workFont(role)
                .tracking(tracking)
                .fixedSize(horizontal: true, vertical: false)
                .hidden()
                .accessibilityHidden(true)
            content
        }
    }
}

extension View {
    /// See `WordSafeWidth`.
    func wordSafeWidth(
        of text: String,
        role: WorkFontRole,
        uppercased: Bool = false,
        tracking: CGFloat = 0
    ) -> some View {
        modifier(WordSafeWidth(text: text, role: role, uppercased: uppercased, tracking: tracking))
    }
}

struct OptionalAccessibilityIdentifier: ViewModifier {
    let identifier: String?

    func body(content: Content) -> some View {
        if let identifier {
            content.accessibilityIdentifier(identifier)
        } else {
            content
        }
    }
}

// MARK: - Meters and coverage

/// A slim v7 meter (limits, shares): rx2 `meterTrack` track (K44) — one track
/// token that stays visible on the canvas (menu) and on cards.
///
/// Pass FILL-weight tints (`Theme.limitFillColor`, `Theme.amberFill`,
/// `Theme.chartBar`), never the amber text token. `thresholds` (fractions,
/// e.g. `[0.75, 0.9]`) draw ink ticks 1.5pt wide that sit OUTSIDE the bar —
/// 2pt above and 2pt below it — so a tick never depends on the fill's
/// luminance and reads the same in both modes (C78/C43).
struct MeterBar: View {
    let fraction: Double
    var tint: Color
    var height: CGFloat = Metrics.meterH
    var thresholds: [Double] = []
    @Environment(\.displayScale) private var displayScale

    static let tickWidth: CGFloat = 1.5
    static let tickOverhang: CGFloat = 2

    /// Fill length stays PROPORTIONAL. The only floor is one device pixel, so a
    /// real non-zero share is never drawn as nothing — and never as the same
    /// length as a share many times its size. The old floor was the bar's own
    /// height, which drew every share below 7.5% of an 80pt track identically:
    /// 9% and <1% ended one pixel apart (K111).
    static func fillWidth(fraction: Double, trackWidth: CGFloat, displayScale: CGFloat) -> CGFloat {
        guard fraction > 0, trackWidth > 0 else { return 0 }
        let proportional = trackWidth * min(fraction, 1)
        let onePixel = 1 / max(displayScale, 1)
        return min(trackWidth, max(onePixel, proportional))
    }

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 2).fill(Theme.meterTrack)
                if fraction > 0 {
                    RoundedRectangle(cornerRadius: 2)
                        .fill(tint)
                        .frame(width: Self.fillWidth(
                            fraction: fraction,
                            trackWidth: proxy.size.width,
                            displayScale: displayScale
                        ))
                }
            }
            .overlay(alignment: .topLeading) {
                ForEach(thresholds.filter { $0 > 0 && $0 < 1 }, id: \.self) { threshold in
                    let x = proxy.size.width * threshold - Self.tickWidth / 2
                    Rectangle()
                        .fill(Theme.ink)
                        .frame(width: Self.tickWidth, height: Self.tickOverhang)
                        .offset(x: x, y: -Self.tickOverhang)
                    Rectangle()
                        .fill(Theme.ink)
                        .frame(width: Self.tickWidth, height: Self.tickOverhang)
                        .offset(x: x, y: proxy.size.height)
                }
            }
        }
        .frame(height: height)
        .padding(.vertical, thresholds.isEmpty ? 0 : Self.tickOverhang)
        .accessibilityHidden(true)
    }
}

extension Theme {
    /// Namespaced alias so call sites may write `Theme.MeterBar(...)`.
    typealias MeterBar = agentacct.MeterBar
}

/// What one coverage-bar segment counts. The bar's denominator is EVERY
/// recorded step, so the three classes together are `total_steps` — a ratio
/// over `checkable_total` alone let `1/1 self-checked` fill a bar while three
/// more steps of the same Task were open or out of scope (C2).
enum CoverageSegmentClass: Hashable {
    /// A checkable step, at the evidence tier the reducer counted it under.
    case tier(String)
    /// A step still open, or stopped without a check (blocked, handed off, failed).
    case open
    /// A step the reducer counts outside the ratio (review, research, planning, docs).
    case notCheckRelevant
}

/// One segment of an evidence coverage bar.
struct CoverageSegment {
    let count: Int
    let kind: CoverageSegmentClass
    /// The tier word from the payload's `tier_legend`; nil falls back to the
    /// shared `EvidenceTierStyle` label. Only a TIER segment carries one: the
    /// open and out-of-scope classes have no per-segment word in the payload,
    /// so the reducer's `coverage_ledger` sentence names them beneath the bar.
    var label: String? = nil

    init(count: Int, kind: CoverageSegmentClass, label: String? = nil) {
        self.count = count
        self.kind = kind
        self.label = label
    }

    init(count: Int, grade: String, label: String? = nil) {
        self.init(count: count, kind: .tier(grade), label: label)
    }

    /// The tier key when this segment is a tier, nil otherwise.
    var grade: String? {
        if case .tier(let key) = kind { return key }
        return nil
    }
}

/// Coverage bar: h8, rx2 segments, 4px gaps, widths strictly proportional to
/// counts — with its counted legend BUILT IN (C33). The legend is derived from
/// the same segments that size the bar: one entry per non-zero tier, in bar
/// order, each an `EvidencePip` shape + tier word + mono count, wrapping when
/// narrow. Tier is therefore never carried by color alone at any call site.
struct CoverageBar: View {
    let segments: [CoverageSegment]
    var height: CGFloat = Metrics.meterH
    var showsLegend = true

    /// The canonical four-tier bar from the receipt's `by_tier` counts, in
    /// strongest-first order. Labels come from the payload `tier_legend`.
    init(
        byTier: ReceiptByTier?,
        tierLegend: [ReceiptTierDefinition]? = nil,
        open: Int = 0,
        notCheckRelevant: Int = 0,
        height: CGFloat = Metrics.meterH,
        showsLegend: Bool = true
    ) {
        self.segments = Self.segments(
            byTier: byTier, tierLegend: tierLegend, open: open, notCheckRelevant: notCheckRelevant
        )
        self.height = height
        self.showsLegend = showsLegend
    }

    /// The bar a receipt draws: the tier counts PLUS the steps the ratio does
    /// not cover, so the bar's length is every recorded step. `1/1 self-checked`
    /// of a five-step Task therefore fills one fifth of the track, not all of it.
    init(
        evidence: ReceiptEvidence,
        height: CGFloat = Metrics.meterH,
        showsLegend: Bool = true
    ) {
        self.init(
            byTier: evidence.byTier,
            tierLegend: evidence.tierLegend,
            open: evidence.openOrIncomplete ?? 0,
            notCheckRelevant: evidence.notCheckable ?? 0,
            height: height,
            showsLegend: showsLegend
        )
    }

    /// Bar order: strongest proof first, then the weaker tiers, then the steps
    /// that own no proof claim at all.
    static func segments(
        byTier: ReceiptByTier?,
        tierLegend: [ReceiptTierDefinition]?,
        open: Int,
        notCheckRelevant: Int
    ) -> [CoverageSegment] {
        func label(_ key: String) -> String? {
            PayloadAbsence.text(tierLegend?.first(where: { $0.key == key })?.label)
        }
        return [
            CoverageSegment(count: byTier?.externallyVerified ?? 0, grade: "externally_verified", label: label("externally_verified")),
            CoverageSegment(count: byTier?.independentlyChecked ?? 0, grade: "independently_checked", label: label("independently_checked")),
            CoverageSegment(count: byTier?.selfChecked ?? 0, grade: "self_checked", label: label("self_checked")),
            CoverageSegment(count: byTier?.unchecked ?? 0, grade: "unchecked", label: label("unchecked")),
            CoverageSegment(count: max(open, 0), kind: .open),
            CoverageSegment(count: max(notCheckRelevant, 0), kind: .notCheckRelevant),
        ]
    }

    init(segments: [CoverageSegment], height: CGFloat = Metrics.meterH, showsLegend: Bool = true) {
        self.segments = segments
        self.height = height
        self.showsLegend = showsLegend
    }

    var visible: [CoverageSegment] { segments.filter { $0.count > 0 } }
    /// The denominator the bar is drawn over: every recorded step it was given.
    var denominator: Int { visible.reduce(0) { $0 + $1.count } }
    /// Tier segments only — the classes that own a payload word, and so the
    /// only ones a counted legend can name.
    private var tiers: [CoverageSegment] { visible.filter { $0.grade != nil } }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            GeometryReader { proxy in
                let gaps = CGFloat(max(visible.count - 1, 0)) * 4
                let unit = denominator > 0 ? (proxy.size.width - gaps) / CGFloat(denominator) : 0
                HStack(spacing: 4) {
                    ForEach(Array(visible.enumerated()), id: \.offset) { _, segment in
                        CoverageSegmentMark(kind: segment.kind)
                            .frame(width: max(unit * CGFloat(segment.count), 2))
                    }
                }
            }
            .frame(height: height)
            .accessibilityHidden(true)

            // A legend only distinguishes tiers: with one tier the headline
            // already names it, so a single-entry legend would restate it. The
            // open and out-of-scope segments carry no payload word, so the
            // reducer's `coverage_ledger` names them beneath the bar instead —
            // inventing "still open" here would be a second vocabulary.
            if showsLegend && tiers.count >= 2 {
                WrappingRowLayout(horizontalSpacing: Space.m, verticalSpacing: Space.xs) {
                    ForEach(Array(tiers.enumerated()), id: \.offset) { _, segment in
                        CoverageLegendEntry(segment: segment)
                    }
                }
                // The counted tiers are a named group: the bar itself is
                // hidden, so nothing else says what these counts describe.
                .accessibilityElement(children: .contain)
                .accessibilityLabel("Evidence by tier")
            }
        }
    }
}

/// One coverage-bar segment (K90). Segment fill follows the pip shape: a tier
/// whose pip is hollow (claimed / unchecked — claim ≠ proof) draws an OUTLINED
/// segment (1.5pt stroke in the tier color over the card), so a bar with
/// nothing checked never reads as a full, finished meter; checked tiers fill.
struct CoverageSegmentMark: View {
    let kind: CoverageSegmentClass

    init(kind: CoverageSegmentClass) { self.kind = kind }
    init(grade: String) { self.kind = .tier(grade) }

    static func isHollow(_ grade: String) -> Bool {
        EvidenceTierStyle.forGrade(grade).pip == .hollow
    }

    var body: some View {
        switch kind {
        case .tier(let grade):
            let style = EvidenceTierStyle.forGrade(grade)
            if Self.isHollow(grade) {
                RoundedRectangle(cornerRadius: 2)
                    .fill(Theme.card)
                    .overlay(
                        RoundedRectangle(cornerRadius: 2)
                            .strokeBorder(style.tint, lineWidth: 1.5)
                    )
            } else {
                RoundedRectangle(cornerRadius: 2).fill(style.fill)
            }
        case .open:
            // A step still running or stopped without a check: a DASHED
            // outline. Shape, not hue, separates it from the amber `unchecked`
            // tier beside it — an outline alone would differ only by colour.
            RoundedRectangle(cornerRadius: 2)
                .fill(Theme.card)
                .overlay(
                    RoundedRectangle(cornerRadius: 2)
                        .strokeBorder(Theme.muted, style: StrokeStyle(lineWidth: 1.5, dash: [3, 2]))
                )
        case .notCheckRelevant:
            // Out of the ratio entirely: the meter's own track tone, so the
            // span reads as length the proof was never claimed over.
            RoundedRectangle(cornerRadius: 2).fill(Theme.meterTrack)
        }
    }
}

/// One counted legend entry: tier pip shape, tier word, mono count.
struct CoverageLegendEntry: View {
    let segment: CoverageSegment

    var body: some View {
        let style = EvidenceTierStyle.forGrade(segment.grade)
        let label = segment.label ?? style.label
        HStack(spacing: 6) {
            EvidencePip(grade: segment.grade)
            Text(label)
                .workFont(.caption)
                .foregroundStyle(Theme.ink)
                .lineLimit(1)
                .fixedSize(horizontal: true, vertical: false)
            Text("\(segment.count)")
                .workFont(.dataSmallSemibold)
                .foregroundStyle(Theme.ink)
                .fixedSize(horizontal: true, vertical: false)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(label): \(segment.count)")
        // A legend entry is text, not an unknown element (K124).
        .accessibilityAddTraits(.isStaticText)
    }
}

/// A row that wraps its children onto further lines when the proposed width
/// runs out (a flow layout). Badges and chips never wrap mid-word; the row
/// holding them does (C32). Children keep their ideal size, clamped to the
/// available width as a last resort.
struct WrappingRowLayout: Layout {
    var horizontalSpacing: CGFloat = Space.s
    var verticalSpacing: CGFloat = Space.xs
    var alignment: VerticalAlignment = .center

    private struct Row {
        var indices: [Int] = []
        var width: CGFloat = 0
        var height: CGFloat = 0
    }

    private func rows(maxWidth: CGFloat, subviews: Subviews) -> ([Row], [CGSize]) {
        let sizes = subviews.map { subview -> CGSize in
            let ideal = subview.sizeThatFits(.unspecified)
            guard ideal.width > maxWidth else { return ideal }
            return subview.sizeThatFits(ProposedViewSize(width: maxWidth, height: nil))
        }
        var rows: [Row] = []
        var current = Row()
        for (index, size) in sizes.enumerated() {
            let needed = current.indices.isEmpty ? size.width : current.width + horizontalSpacing + size.width
            if !current.indices.isEmpty && needed > maxWidth {
                rows.append(current)
                current = Row()
            }
            current.width = current.indices.isEmpty ? size.width : current.width + horizontalSpacing + size.width
            current.height = max(current.height, size.height)
            current.indices.append(index)
        }
        if !current.indices.isEmpty { rows.append(current) }
        return (rows, sizes)
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let maxWidth = proposal.width ?? .infinity
        let (rows, _) = rows(maxWidth: maxWidth, subviews: subviews)
        let width = rows.map(\.width).max() ?? 0
        let height = rows.map(\.height).reduce(0, +) + CGFloat(max(rows.count - 1, 0)) * verticalSpacing
        return CGSize(width: proposal.width.map { min($0, width) } ?? width, height: height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let (rows, sizes) = rows(maxWidth: bounds.width, subviews: subviews)
        var y = bounds.minY
        for row in rows {
            var x = bounds.minX
            for index in row.indices {
                let size = sizes[index]
                let offset: CGFloat
                switch alignment {
                case .top: offset = 0
                case .bottom: offset = row.height - size.height
                default: offset = (row.height - size.height) / 2
                }
                subviews[index].place(
                    at: CGPoint(x: x, y: y + offset),
                    anchor: .topLeading,
                    proposal: ProposedViewSize(width: size.width, height: size.height)
                )
                x += size.width + horizontalSpacing
            }
            y += row.height + verticalSpacing
        }
    }
}

// MARK: - Text fields

/// The ONE app-chrome text field (K15): a `.plain` field on the card fill with
/// a 1pt `rule` boundary (3.83:1 light / 3.36:1 dark on card, so the control
/// edge meets the 3:1 non-text target) and a 2pt accent stroke while focused.
/// It replaces AppKit `.roundedBorder`, whose dark fill and bezel sit off the
/// palette. Snapshot renders keep this exact chrome and swap only the inner
/// `TextField` for `Text` (ImageRenderer cannot draw AppKit text fields).
struct AppTextField: View {
    let placeholder: String
    @Binding var text: String
    var systemImage: String? = nil
    var font: WorkFontRole = .caption
    var axis: Axis = .horizontal
    var lineLimit: ClosedRange<Int>? = nil
    var focus: FocusState<Bool>.Binding? = nil
    var accessibilityFocus: AccessibilityFocusState<Bool>.Binding? = nil
    var accessibilityLabel: String? = nil
    var accessibilityIdentifier: String? = nil

    @FocusState private var ownFocus: Bool

    init(
        placeholder: String,
        text: Binding<String>,
        systemImage: String? = nil,
        font: WorkFontRole = .caption,
        axis: Axis = .horizontal,
        lineLimit: ClosedRange<Int>? = nil,
        focus: FocusState<Bool>.Binding? = nil,
        accessibilityFocus: AccessibilityFocusState<Bool>.Binding? = nil,
        accessibilityLabel: String? = nil,
        accessibilityIdentifier: String? = nil
    ) {
        self.placeholder = placeholder
        self._text = text
        self.systemImage = systemImage
        self.font = font
        self.axis = axis
        self.lineLimit = lineLimit
        self.focus = focus
        self.accessibilityFocus = accessibilityFocus
        self.accessibilityLabel = accessibilityLabel
        self.accessibilityIdentifier = accessibilityIdentifier
    }

    private var focused: Bool { focus?.wrappedValue ?? ownFocus }

    var body: some View {
        HStack(alignment: axis == .vertical ? .firstTextBaseline : .center, spacing: 6) {
            if let systemImage {
                Image(systemName: systemImage)
                    .workFont(.icon).foregroundStyle(Theme.muted)
                    .accessibilityHidden(true)
            }
            if SnapshotMode.enabled && !SnapshotMode.interactiveFixture {
                Text(text.isEmpty ? placeholder : text)
                    .workFont(font)
                    .foregroundStyle(text.isEmpty ? Theme.muted : Theme.ink)
                    .lineLimit(lineLimit?.upperBound ?? 1)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                TextField(placeholder, text: $text, axis: axis)
                    .textFieldStyle(.plain)
                    .workFont(font)
                    .foregroundStyle(Theme.ink)
                    .modifier(OptionalLineLimit(range: lineLimit))
                    .focused(focus ?? $ownFocus)
                    .modifier(OptionalAccessibilityFocus(binding: accessibilityFocus))
                    .modifier(OptionalAccessibilityLabel(label: accessibilityLabel))
                    .modifier(OptionalAccessibilityIdentifier(identifier: accessibilityIdentifier))
            }
        }
        .padding(.horizontal, Space.m)
        .padding(.vertical, Space.xs)
        .workScaledMinFrame(height: 32, alignment: .leading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(
            RoundedRectangle(cornerRadius: Metrics.radius)
                .strokeBorder(
                    focused ? Theme.accent : Theme.rule,
                    lineWidth: focused ? Metrics.focusW : Metrics.borderW
                )
        )
    }
}

private struct OptionalLineLimit: ViewModifier {
    let range: ClosedRange<Int>?

    func body(content: Content) -> some View {
        if let range {
            content.lineLimit(range)
        } else {
            content.lineLimit(1)
        }
    }
}

private struct OptionalAccessibilityFocus: ViewModifier {
    let binding: AccessibilityFocusState<Bool>.Binding?

    func body(content: Content) -> some View {
        if let binding {
            content.accessibilityFocused(binding)
        } else {
            content
        }
    }
}

private struct OptionalAccessibilityLabel: ViewModifier {
    let label: String?

    func body(content: Content) -> some View {
        if let label {
            content.accessibilityLabel(label)
        } else {
            content
        }
    }
}

// MARK: - Floating surfaces and alignment

extension View {
    /// The ONE ground for popover content (K84): an opaque palette surface,
    /// so window text never bleeds through captions and every contrast pair
    /// in the palette holds. Applied to the content of every `.popover`.
    /// `card` by default; a popover that lays its own cards out passes
    /// `Theme.canvas` so the hierarchy canvas > card never inverts.
    func popoverSurface(_ ground: Color = Theme.card) -> some View {
        background(ground)
            .presentationBackground(ground)
    }

    /// Optical alignment for a row of quiet text actions that starts a text
    /// column (K27): pulls the row left by the buttons' horizontal padding so
    /// the first label's glyphs sit on the text edge while the hover wash and
    /// focus ring hang into the margin. Apply to the row, never per button.
    func hangingLeading(_ padding: CGFloat = 7) -> some View {
        self.padding(.leading, -padding)
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
                        .workFont(FieldFont.qualifier)
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
    nonisolated(unsafe) static var reviewExpandSetupDetails = false
    nonisolated(unsafe) static var interactiveFixture = false
    nonisolated(unsafe) static var enabled = false

    /// Optional clock override for deterministic fixture renders.
    ///
    /// The normal app leaves this `nil` and uses the real clock. The dashboard
    /// snapshot renderer sets it to the fixture's `glance.generated_at` only
    /// while rendering, then restores it to `nil` in `defer`.
    nonisolated(unsafe) private static var fixtureDate: Date?

    /// The date relative UI copy should use.
    ///
    /// Formatting helpers such as `agoText` read this instead of calling
    /// `Date()` directly. This freezes text like “12m ago” in snapshots without changing the system clock or
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

    /// Draw the interactive AppKit control chrome (bordered/prominent `Button`,
    /// SF Symbol labels inside them) as static primitives instead of the real
    /// controls. SwiftUI's offscreen `ImageRenderer` only paints native button
    /// chrome on the pinned golden toolchain (macOS 26); on other versions those
    /// controls collapse to an unrendered fill with dropped labels. The README
    /// all-pane `--snapshot` path (`SnapshotRunner`) sets this so its docs
    /// screenshots render faithfully on any host. The live app and the pinned
    /// fixture renderers leave it `false` and keep the real interactive controls
    /// — so the golden references are unchanged.
    nonisolated(unsafe) static var rendersStaticControls = false

    /// How many steps a snapshot opens: check-bearing steps first, then
    /// un-checked ones. The golden fixture renders keep the default (2 + 1) so
    /// their references are unchanged; the README `--snapshot` path can narrow
    /// it (AGENTACCT_SNAPSHOT_EXPANDED_STEPS, e.g. "1,0") so a single docs
    /// screenshot fits the step spine and the activity timeline together.
    nonisolated(unsafe) static var expandedStepsWithChecks = 2
    nonisolated(unsafe) static var expandedStepsWithoutChecks = 1
}

struct ScrollBox<Content: View>: View {
    @ViewBuilder let content: () -> Content

    var body: some View {
        if SnapshotMode.enabled && !SnapshotMode.interactiveFixture {
            if SnapshotMode.boundsScrollContentToViewport {
                // A review viewport shows the TOP of the page exactly as the
                // live ScrollView would: the content lays out at its ideal
                // height (C66) and is cut at the fold. Proposing the viewport
                // height instead squeezed the page to fit — the minimum render
                // showed row spacing no real layout produces and sliced its
                // last row while the footer still claimed every row was shown
                // (K57). What falls below the fold is honestly not drawn.
                GeometryReader { proxy in
                    content()
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(width: proxy.size.width, alignment: .topLeading)
                        .frame(height: proxy.size.height, alignment: .top)
                        .clipped()
                }
            } else {
                // Never propose the fixed snapshot frame's slack height to the
                // content (C66): it lays out at its ideal height, top-aligned,
                // so no height-greedy card grows an empty void.
                content()
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxHeight: .infinity, alignment: .top)
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
        if SnapshotMode.enabled && !SnapshotMode.interactiveFixture {
            VStack(alignment: alignment, spacing: spacing, content: content)
        } else {
            LazyVStack(alignment: alignment, spacing: spacing, content: content)
        }
    }
}

extension WorkTimelineRecord {
    /// The shared card-text color for a record, routed through the ONE
    /// decision tint (`DecisionTintClass.forKey`, C26): superseded or
    /// dispositioned records stay muted; a current failing check, or a step
    /// whose reported status is in the danger family (blocked / failed /
    /// finding), is coral; everything else is ink. Cobalt stays the
    /// interactive voice, so a "Reported blocked" step can never read as a
    /// link. One definition serves the canvas cards and the detail region.
    var presentationTint: Color {
        if superseded || disposition != nil { return Theme.muted }
        if isCurrentFailure { return Theme.coral }
        if kind == .step, DecisionTintClass.forKey(result) == .danger { return Theme.coral }
        // A progress note is narration INSIDE a section, not a step beside it:
        // it reports no outcome of its own, so it never takes the ink weight a
        // reported state carries.
        if isBeat { return Theme.muted }
        return Theme.ink
    }

    /// How a record's salience is DRAWN. The reducer decides whether a record
    /// is salient and says why (`salience` / `salience_reason`); this only
    /// turns that fact into a mark, and deliberately into none of the voices
    /// already spoken for:
    ///
    /// * never the cobalt accent — that is the interactive voice, and a reader
    ///   who has learned "cobalt means pressable" would read a loud row as a
    ///   control;
    /// * never coral / amber / green — those are the evidence and failure
    ///   tones, and salience is not a result: a step "reported completed while
    ///   still unchecked" is salient and has failed nothing.
    ///
    /// What is left is a WEIGHT and a RULE: a neutral bar on the record's
    /// leading edge, drawn in the palette's line token, and the row label at
    /// its semibold step. Both survive greyscale and neither invents a tone.
    var isSalient: Bool { important && PayloadAbsence.text(salience) != nil }

    /// The tint of this record's MARK on the canvas — its dot, its span and
    /// its glyph — which is a data mark and therefore never the interactive
    /// accent (K04). Three distinguishable states, not two:
    /// * a current failure and a danger step: coral, filled;
    /// * a failure a later run replaced: coral, HOLLOW (`markIsHollow`) — the
    ///   failure happened and is answered, so the colour still admits it while
    ///   the open shape says nothing is owed;
    /// * everything else: the neutral chart tone.
    var markTint: Color {
        if isCurrentFailure || isResolvedFailure { return Theme.coral }
        if isDanger { return Theme.coral }
        return Theme.chartNeutral
    }

    /// A resolved failure's mark is drawn open. Nothing else is, so the hollow
    /// ring means exactly one thing on the canvas.
    var markIsHollow: Bool { isResolvedFailure }
}
