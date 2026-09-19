import SwiftUI

/// The agent's recorded next step, rendered one way on every surface (C88):
/// the attention callout, the Dashboard attention card and the step card.
///
/// The caps label says "Recorded": the text is the agent's own claim, quoted
/// verbatim and selectable, never a generated suggestion. A missing next step
/// is a named absence, never an empty slot.
///
/// * Regular: `RECORDED NEXT STEP` caps label (muted) above the text in body
///   ink.
/// * Compact: the caps label inline, followed by the text at caption size —
///   falling back to the stacked arrangement when the two cannot share a line.
struct NextStepRow: View {
    let text: String?
    var compact: Bool = false
    /// False under a section heading that already names this row — the record
    /// page's "Next" section. One fact, one name for it: the caps label and the
    /// heading would otherwise say the same thing a line apart.
    var showsLabel: Bool = true

    static let label = "Recorded next step"
    static let absence = "No next step recorded."

    private var recorded: String? {
        guard let text else { return nil }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : text
    }

    var body: some View {
        Group {
            if !showsLabel {
                value(role: .body)
            } else if compact {
                // At large reading sizes in a narrow column the caps label is
                // wider than the card: held on one line it overflowed the
                // leading edge and squeezed the value to a near-zero column,
                // so the recorded step vanished into a ~2000pt blank (K56).
                // When the pair cannot share a line, the label stacks above
                // the value — the arrangement the regular variant already uses.
                ViewThatFits(in: .horizontal) {
                    HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                        CapsLabel(text: Self.label)
                            .lineLimit(1)
                        value(role: .caption)
                    }
                    VStack(alignment: .leading, spacing: Space.xs) {
                        CapsLabel(text: Self.label)
                        value(role: .caption)
                    }
                }
            } else {
                VStack(alignment: .leading, spacing: Space.xs) {
                    CapsLabel(text: Self.label)
                    value(role: .body)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        // NOT `.combine`: merging the caps label with the selectable Text
        // replaced both with one roleless element carrying a synthesized
        // "show menu" action, so the agent's own words lost their text role
        // and could not be navigated or copied by word (K120). Label and value
        // stay siblings, as the recorded reason beside them already is.
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("next-step")
    }

    @ViewBuilder
    private func value(role: WorkFontRole) -> some View {
        if let recorded {
            // verbatim: agent-authored text, never markdown.
            Text(verbatim: recorded)
                .workFont(role)
                .foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: Metrics.readingMeasure, alignment: .leading)
                .textSelection(.enabled)
        } else {
            Text(Self.absence)
                .workFont(role)
                .foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}
