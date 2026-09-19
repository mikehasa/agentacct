import SwiftUI

/// Supplemental explanation stays quiet until requested. The hover help is a
/// one-line summary (C109) — `summary`, or the title when no summary is given —
/// and the selectable popover carries the full message; keyboard users can
/// open it. The glyph is an `IconButton`, so it gets the scaled icon role and
/// the scaled 28pt hit minimum (C73).
struct ContextHelp: View {
    let title: String
    let message: String
    /// One sentence for the hover tooltip. Long copy belongs in `message`.
    var summary: String? = nil
    var identifier: String? = nil
    @State private var isPresented = false

    var body: some View {
        IconButton(
            systemName: "info.circle",
            label: title,
            help: summary ?? title,
            tint: Theme.muted,
            identifier: identifier ?? "context-help.\(title)"
        ) {
            isPresented.toggle()
        }
        .accessibilityHint("Show explanation")
        .popover(isPresented: $isPresented) {
            VStack(alignment: .leading, spacing: Space.s) {
                Text(title).workFont(.rowLabel)
                Text(message).workFont(.body).textSelection(.enabled)
            }
            .fixedSize(horizontal: false, vertical: true)
            .padding(Space.l).frame(width: 340, alignment: .leading)
            .popoverSurface()
        }
    }
}
