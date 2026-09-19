import SwiftUI

/// The app-owned popup control (C31). Native `.pickerStyle(.menu)` controls
/// are AppKit popup buttons that keep the system control font, so they ignore
/// the app's Reading Size and become the smallest text on screen for a
/// large-text user. This control renders its closed label in a `workFont`
/// role with quiet cobalt chrome, and keeps the native checkmarked list by
/// hosting an inline `Picker` inside a `Menu`.
///
/// The open menu items still draw at system size (NSMenu); the closed control
/// — the part that is always on screen — scales. Callers place a visible title
/// `Text` in `workFont(.caption)` beside it (e.g. "Sort"); `title` here is the
/// accessibility label and the inline picker's name.
struct AppMenuPicker<Value: Hashable>: View {
    let title: String
    @Binding var selection: Value
    let options: [(Value, String)]
    var accessibilityIdentifier: String? = nil

    init(
        title: String,
        selection: Binding<Value>,
        options: [(Value, String)],
        accessibilityIdentifier: String? = nil
    ) {
        self.title = title
        self._selection = selection
        self.options = options
        self.accessibilityIdentifier = accessibilityIdentifier
    }

    /// The label of the current selection. A selection outside `options` is
    /// named with the title rather than shown blank.
    var currentLabel: String {
        options.first(where: { $0.0 == selection })?.1 ?? title
    }

    var body: some View {
        if SnapshotMode.enabled && !SnapshotMode.interactiveFixture {
            // ImageRenderer cannot draw native menu chrome; the static stand-in
            // is the same Chip the panes already used for their pickers.
            Chip(text: currentLabel, tint: Theme.accent)
                .accessibilityLabel(title)
                .accessibilityValue(currentLabel)
                .modifier(OptionalAccessibilityIdentifier(identifier: accessibilityIdentifier))
        } else {
            Menu {
                Picker(title, selection: $selection) {
                    ForEach(options.indices, id: \.self) { index in
                        Text(options[index].1).tag(options[index].0)
                    }
                }
                .pickerStyle(.inline)
                .labelsHidden()
            } label: {
                HStack(spacing: Space.xs) {
                    Text(currentLabel)
                        .workFont(.body)
                        .lineLimit(1)
                        .fixedSize(horizontal: true, vertical: false)
                    Image(systemName: "chevron.down")
                        .workFont(.icon)
                        .accessibilityHidden(true)
                }
                .foregroundStyle(Theme.accent)
                .minimumHitTarget(alignment: .leading)
                .contentShape(Rectangle())
            }
            .menuStyle(.button)
            .menuIndicator(.hidden)
            .buttonStyle(QuietButtonStyle())
            .fixedSize(horizontal: true, vertical: false)
            .accessibilityLabel(title)
            .accessibilityValue(currentLabel)
            .modifier(OptionalAccessibilityIdentifier(identifier: accessibilityIdentifier))
        }
    }
}
