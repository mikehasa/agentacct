import SwiftUI

/// The house segmented control: a tinted track holding one 26 pt button per
/// choice, each at least 52 pt wide, the selected one lifted onto a card fill. Designed surfaces use it
/// instead of the native segmented picker — the Dashboard's Tokens/Cost
/// switch, the Usage page's recorded-usage range and its chart measure — so
/// every choice control shares one shape, one type role (`captionSemibold`)
/// and one hover/press/focus feedback model. Buttons render under
/// ImageRenderer, so snapshots show the real control rather than a stand-in.
struct SegmentedChoice<Option: Hashable>: View {
    /// Every segment is at least this wide, so short labels (7d, 30d, 90d,
    /// Cost) get equal, roomy cells instead of hugging their glyphs, while a
    /// long label still grows to fit.
    static var minimumSegmentWidth: CGFloat { 52 }
    static var segmentHeight: CGFloat { 26 }

    let options: [Option]
    let label: (Option) -> String
    @Binding var selection: Option
    /// The accessibility identifier for one segment, for automation.
    var identifier: ((Option) -> String)? = nil

    var body: some View {
        HStack(spacing: 2) {
            ForEach(options, id: \.self) { option in
                let selected = option == selection
                let button = Button {
                    selection = option
                } label: {
                    Text(label(option)).workFont(.captionSemibold)
                        .padding(.horizontal, 12)
                        .frame(minWidth: Self.minimumSegmentWidth)
                        .frame(height: Self.segmentHeight)
                }
                .buttonStyle(SegmentedChoiceButtonStyle(selected: selected))
                .accessibilityAddTraits(selected ? .isSelected : [])
                if let identifier = identifier?(option) {
                    button.accessibilityIdentifier(identifier)
                } else {
                    button
                }
            }
        }
        .padding(2)
        .background(Theme.tintNeutral, in: RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous))
    }
}

struct SegmentedChoiceButtonStyle: ButtonStyle {
    let selected: Bool

    func makeBody(configuration: Configuration) -> some View {
        SegmentedChoiceButtonBody(configuration: configuration, selected: selected)
    }
}

private struct SegmentedChoiceButtonBody: View {
    let configuration: ButtonStyleConfiguration
    let selected: Bool
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
            .foregroundStyle(selected ? Theme.ink : Theme.muted)
            .background {
                RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
                    .fill(selected ? Theme.card : Color.clear)
                RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
                    .fill(Theme.accent.opacity(ButtonFeedback.surfaceFillOpacity(for: phase)))
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
