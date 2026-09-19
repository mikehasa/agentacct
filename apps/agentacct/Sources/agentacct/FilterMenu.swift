import SwiftUI

/// A compact single-choice dropdown for collection controls (status filter,
/// sort order). It wears the search field's chrome — card fill, 1px card
/// line, rx4 — so a filter row reads as one family of inputs instead of the
/// system's grey pop-up bezel. A leading glyph names the control; the visible
/// words are the current choice, so no separate label competes for width.
///
/// `isActive` marks a choice that narrows the collection (a status filter,
/// not a sort order): the control takes the accent wash so a filtered list is
/// visible at a glance. The glyph and the words still carry the state, so
/// color is never the only carrier.
///
/// `showsValue: false` is the narrow fallback: glyph and chevron only, with
/// the choice kept in the tooltip and the accessibility value.
struct FilterMenu<Selection: Hashable, Options: View>: View {
    /// Accessible name of the control.
    let title: String
    /// The menu's section heading; the title when nil.
    var heading: String? = nil
    let systemImage: String
    /// The current choice as the trigger shows it.
    let value: String
    var isActive = false
    var showsValue = true
    /// Matches a neighboring field when the control sits beside one.
    var minHeight: CGFloat = ButtonFeedback.minimumHitDimension
    var help: String? = nil
    var identifier: String? = nil
    @Binding var selection: Selection
    @ViewBuilder let options: () -> Options

    var body: some View {
        // ImageRenderer draws a SwiftUI Menu as a yellow "unsupported
        // control" placeholder, so static renders show the trigger's resting
        // face instead. The native review harness hosts real AppKit views and
        // keeps the live menu.
        if SnapshotMode.enabled && !SnapshotMode.interactiveFixture {
            FilterMenuFace(systemImage: systemImage, value: value, isActive: isActive, showsValue: showsValue, minHeight: minHeight, phase: .idle, focused: false)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(title)
                .accessibilityValue(value)
        } else {
            Menu {
                Picker(heading ?? title, selection: $selection) {
                    options()
                }
                .pickerStyle(.inline)
            } label: {
                // The style draws the face; the label only carries the words
                // AppKit reads for the menu button's title.
                Text(value)
            }
            .menuStyle(.button)
            .menuIndicator(.hidden)
            .buttonStyle(FilterMenuButtonStyle(systemImage: systemImage, value: value, isActive: isActive, showsValue: showsValue, minHeight: minHeight))
            .fixedSize(horizontal: false, vertical: true)
            .help(help ?? (showsValue ? title : "\(title): \(value)"))
            .accessibilityLabel(title)
            .accessibilityValue(value)
            .accessibilityIdentifier(identifier ?? "filter-menu.\(title)")
        }
    }
}

/// Draws the trigger for ``FilterMenu`` with the shared interaction policy:
/// hover and press wash, the 2px accent focus ring, and disabled dimming.
private struct FilterMenuButtonStyle: ButtonStyle {
    let systemImage: String
    let value: String
    let isActive: Bool
    let showsValue: Bool
    let minHeight: CGFloat

    func makeBody(configuration: Configuration) -> some View {
        FilterMenuButtonBody(
            configuration: configuration,
            systemImage: systemImage,
            value: value,
            isActive: isActive,
            showsValue: showsValue,
            minHeight: minHeight
        )
    }
}

private struct FilterMenuButtonBody: View {
    let configuration: ButtonStyleConfiguration
    let systemImage: String
    let value: String
    let isActive: Bool
    let showsValue: Bool
    let minHeight: CGFloat

    @State private var hovering = false
    @Environment(\.isFocused) private var isFocused
    @Environment(\.isEnabled) private var isEnabled

    var body: some View {
        let phase = buttonInteractionPhase(
            isEnabled: isEnabled,
            isPressed: configuration.isPressed,
            isHovering: hovering
        )
        FilterMenuFace(
            systemImage: systemImage,
            value: value,
            isActive: isActive,
            showsValue: showsValue,
            minHeight: minHeight,
            phase: phase,
            focused: isFocused && isEnabled
        )
        .contentShape(Rectangle())
        .onHover { inside in
            withAnimation(Motion.hover) { hovering = inside }
        }
        .animation(Motion.feedback, value: phase)
    }
}

/// The trigger's resting face, shared by the live menu and static renders.
struct FilterMenuFace: View {
    let systemImage: String
    let value: String
    let isActive: Bool
    var showsValue = true
    var minHeight: CGFloat = ButtonFeedback.minimumHitDimension
    let phase: ButtonInteractionPhase
    let focused: Bool

    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @ScaledMetric(relativeTo: .caption) private var systemScale: CGFloat = 1

    /// Glyphs follow the value text's Reading size so the menu cue never
    /// shrinks to a speck beside enlarged words.
    private var glyphScale: CGFloat {
        WorkTypeScale.resolved(base: 1, systemScaled: systemScale, dynamicTypeSize: dynamicTypeSize)
    }

    var body: some View {
        let tone = isActive ? Theme.accent : Theme.muted
        HStack(spacing: 6) {
            Image(systemName: systemImage)
                .font(.system(size: 10 * glyphScale, weight: .semibold))
                .foregroundStyle(tone)
                .accessibilityHidden(true)
            if showsValue {
                Text(value)
                    .workFont(size: 12, weight: .medium, relativeTo: .caption)
                    .foregroundStyle(isActive ? Theme.accent : Theme.ink)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
            Image(systemName: "chevron.down")
                .font(.system(size: 8 * glyphScale, weight: .bold))
                .foregroundStyle(tone)
                .accessibilityHidden(true)
        }
        .padding(.horizontal, 10)
        .frame(minHeight: minHeight)
        .background {
            let shape = RoundedRectangle(cornerRadius: Metrics.radius)
            shape.fill(isActive ? Theme.tintAccent : Theme.card)
                .overlay(shape.fill(Theme.accent.opacity(ButtonFeedback.surfaceFillOpacity(for: phase))))
        }
        .overlay(
            RoundedRectangle(cornerRadius: Metrics.radius)
                .strokeBorder(isActive ? Theme.accent.opacity(0.45) : Theme.cardLine, lineWidth: Metrics.borderW)
        )
        .overlay {
            if focused {
                RoundedRectangle(cornerRadius: Metrics.radius)
                    .strokeBorder(Theme.accent, lineWidth: Metrics.focusW)
            }
        }
        .opacity(ButtonFeedback.labelOpacity(for: phase))
    }
}
