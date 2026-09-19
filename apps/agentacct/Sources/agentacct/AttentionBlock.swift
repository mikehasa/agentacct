import SwiftUI

/// The ONE meta line order: who ran it, where, then how fresh it is.
///
/// The Dashboard's attention card printed `project · client` while the record
/// header printed `client · project`, so opening a card reversed the same two
/// facts (K21). Every surface joins its meta parts here instead. The freshness
/// part stays a caller's argument because it is read from the viewer's clock,
/// not from the payload.
func workMetaLine(client: String?, project: String?, trailing: [String?] = []) -> String {
    ([client, project] + trailing)
        .compactMap { $0 }
        .filter { !$0.isEmpty }
        .joined(separator: " · ")
}

/// The ONE attention block: the same recorded facts, in the same order and at
/// the same weights, on the Dashboard and on the record page (C03/K21).
///
/// Order and emphasis, both surfaces:
/// * the reducer's reason noun as the caps eyebrow — reason first (C61),
/// * the agent's or hook's own sentence in body INK: it says what went wrong,
/// * the machine identity line beneath it in muted `dataSmall` (C40),
/// * the reducer's note, when it named one,
/// * the recorded next step through the one `NextStepRow` (C88).
///
/// The Dashboard used to invert the middle two — a loud mono identity line
/// above a muted sentence — so the same payload read two ways on two screens,
/// and the explanation was the quietest thing on the card.
struct AttentionBlockBody: View {
    enum Variant {
        /// A Dashboard card competing for the first viewport. It is the ONLY
        /// renderer of the recorded next step on that surface, so it carries it
        /// inline.
        case dashboard
        /// The record page's callout. It does NOT carry the next step: the
        /// record page shows it as a permanent row under the outcome summary,
        /// so the one line that says what happens next survives a finding being
        /// marked reviewed and does not depend on an attention item existing at
        /// all (F6). Two renderers on one page would also print it twice — and
        /// the positive twin (`EvidenceCallout`) passed nil, so a record whose
        /// checks all passed printed "No next step recorded" over a payload
        /// that had one.
        case record

        var showsNextStep: Bool { self == .dashboard }
        var nextStepIsCompact: Bool { self == .dashboard }
    }

    /// The reducer's reason noun (`Failed check`, `Blocker`).
    let reasonLabel: String?
    /// The agent's or hook's sentence, verbatim.
    let summary: String?
    /// The recorded identity of the evidence the reason rests on.
    let label: String?
    /// A named result/exit-code disagreement, when the reducer reported one.
    var noteText: String? = nil
    let nextStep: String?
    let variant: Variant
    /// Coral only for a recorded failure that still needs you; the eyebrow and
    /// glyph share this tone, everything else stays ink or muted.
    var tone: Color = Theme.muted
    /// The record page's leading state glyph; the Dashboard shows none.
    var icon: String? = nil
    /// The record page's recency phrase, aligned with the eyebrow.
    var recency: String? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            if reasonLabel != nil || icon != nil || recency != nil {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    if let icon {
                        Image(systemName: icon)
                            .workFont(.icon)
                            .foregroundStyle(tone)
                            .accessibilityHidden(true)
                    }
                    if let reasonLabel {
                        CapsLabel(text: reasonLabel, tone: tone)
                    }
                    Spacer(minLength: Space.s)
                    if let recency {
                        Text(recency).workFont(.dataSmall).foregroundStyle(Theme.muted)
                    }
                }
            }
            if let summary {
                // verbatim: agent- or hook-authored text, never markdown.
                Text(verbatim: summary)
                    .workFont(.body).foregroundStyle(Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: Metrics.readingMeasure, alignment: .leading)
                    .textSelection(.enabled)
            }
            // The identity line earns its own line only when it says more than
            // the eyebrow already did; a payload whose `label` is just the
            // reason noun would otherwise print the same words twice.
            if let label, label.caseInsensitiveCompare(reasonLabel ?? "") != .orderedSame {
                Text(verbatim: label)
                    .workFont(.dataSmall).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            if let noteText {
                Text(verbatim: noteText)
                    .workFont(.caption).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if variant.showsNextStep {
                NextStepRow(text: nextStep, compact: variant.nextStepIsCompact)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
