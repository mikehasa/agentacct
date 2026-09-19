import SwiftUI

// The record-detail building blocks introduced with the readable Session and
// Work detail views: the source monogram, the proportional outcome segment bar
// (now used by the Worksets pane), and the numbered step spine.
//
// The step ROW and the check row are NOT redefined here. This file once carried
// a second step row (StepSpineRow), a second step detail body, and a second
// outcome summary (RecordOutcomeBars) that duplicated the record page's
// coverage meter and Checks block. Two rows meant two vocabularies and two
// tier rules for the same facts, so the spine now renders `StepCard`
// (StepComponents.swift), which is the one step row: reducer-worded tally,
// grouped current/superseded checks through `CheckRow`, and the shared
// EvidencePip / EvidenceTierStyle tier grammar.

// MARK: - Source monogram

/// The two-letter source badge used in the session header (Claude Code → "CC",
/// Codex → "cx", …). Neutral fill; the source hue lives on the timeline lanes,
/// so the monogram stays quiet and the title carries identity.
struct SourceMonogram: View {
    let client: String?
    var size: CGFloat = 40

    private var initials: String {
        switch (client ?? "").lowercased() {
        case "claude-code", "claude", "claude code": return "CC"
        case "codex", "openai-codex", "codex-cli": return "cx"
        case "opencode", "open-code": return "oc"
        case "hermes": return "he"
        default:
            let letters = (client ?? "?").filter { $0.isLetter }
            return letters.isEmpty ? "?" : String(letters.prefix(2))
        }
    }

    var body: some View {
        RoundedRectangle(cornerRadius: Metrics.radius)
            .fill(Theme.tintNeutral)
            .frame(width: size, height: size)
            .overlay(
                Text(initials)
                    .font(Face.monoFont(size * 0.32, .bold))
                    .foregroundStyle(Theme.muted)
            )
            .accessibilityHidden(true)
    }
}

// MARK: - Outcome segment bar

struct OutcomeSegment: Identifiable {
    /// The label is unique within a bar (one segment per bucket), so it is a
    /// stable ForEach identity — unlike a per-render UUID, which would tear down
    /// and rebuild every segment on each body evaluation.
    var id: String { label }
    let count: Int
    let color: Color
    let label: String
}

/// A proportional segmented bar: each visible segment's width tracks its count.
struct OutcomeSegmentBar: View {
    let segments: [OutcomeSegment]
    var height: CGFloat = 12

    private var total: Int { segments.reduce(0) { $0 + $1.count } }

    var body: some View {
        GeometryReader { proxy in
            let visible = segments.filter { $0.count > 0 }
            let gaps = CGFloat(max(visible.count - 1, 0)) * 2
            let unit = total > 0 ? (proxy.size.width - gaps) / CGFloat(total) : 0
            HStack(spacing: 2) {
                ForEach(visible) { segment in
                    RoundedRectangle(cornerRadius: 2)
                        .fill(segment.color)
                        .frame(width: max(unit * CGFloat(segment.count), 3))
                }
            }
        }
        .frame(height: height)
        .accessibilityHidden(true)
    }
}

/// One outcome card: title + total, the segmented bar, a wrapping counted
/// legend, and an optional attention note (coral).
struct OutcomeBar: View {
    let title: String
    let total: String
    let segments: [OutcomeSegment]
    var note: String? = nil
    var noteTint: Color = Theme.coral

    private var visible: [OutcomeSegment] { segments.filter { $0.count > 0 } }

    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .firstTextBaseline) {
                    Text(title).workFont(.titleCard).foregroundStyle(Theme.ink)
                    Spacer(minLength: Space.s)
                    Text(total).workFont(.dataSmall).foregroundStyle(Theme.muted)
                }
                .padding(.bottom, 10)

                OutcomeSegmentBar(segments: visible)
                    .padding(.bottom, 11)

                WrappingRowLayout(horizontalSpacing: 16, verticalSpacing: 6) {
                    ForEach(visible) { segment in
                        HStack(spacing: 7) {
                            RoundedRectangle(cornerRadius: 2)
                                .fill(segment.color)
                                .frame(width: 9, height: 9)
                            HStack(spacing: 4) {
                                Text("\(segment.count)").workFont(.captionSemibold).foregroundStyle(Theme.ink)
                                Text(segment.label).workFont(.caption).foregroundStyle(Theme.muted)
                            }
                        }
                    }
                }

                if let note {
                    Text(note)
                        .workFont(.caption).foregroundStyle(noteTint)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 10)
                }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityLabel)
    }

    private var accessibilityLabel: String {
        var parts = ["\(title), \(total)"]
        parts += visible.map { "\($0.count) \($0.label)" }
        if let note { parts.append(note) }
        return parts.joined(separator: ", ")
    }
}

// MARK: - Step spine

/// The vertical, numbered step spine: each step is a node on a shared rail so
/// the sequence reads top-to-bottom.
///
/// The ROW is `StepCard` — the one step row in the app. Both surfaces that list
/// session steps (the record page's Steps section and the saved-session view)
/// render through this spine, so there is exactly one step row, one check row
/// (`CheckRow`, via `StepCard`'s grouped checks) and one tally string
/// (`step.checkTallyDisplay`, worded by the reducer). The spine contributes the
/// ordinal and the rail; it composes no display wording of its own.
struct SessionStepSpine: View {
    let items: [SessionStepItem]
    let openedIDs: Set<String>
    /// The Task's own recorded next step, when the page above already prints it.
    /// A step whose next step is the SAME text does not print it again — on the
    /// flagship record the two were measured identical, so the one line that
    /// says what happens next appeared twice on one page.
    var taskNextStep: String? = nil

    var body: some View {
        VStack(spacing: 0) {
            ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                HStack(alignment: .top, spacing: 8) {
                    rail(index: index + 1, isFirst: index == 0, isLast: index == items.count - 1)
                    StepCard(
                        step: item.step,
                        initiallyExpanded: openedIDs.contains(item.id),
                        accessibilityContext: item.id,
                        pageNextStep: taskNextStep
                    )
                    .padding(.vertical, 3)
                }
            }
        }
    }

    /// The ordinal sits where the card's title sits; the hairline runs clear
    /// above the first ordinal and clear below the last, so the sequence begins
    /// and ends on a step. Decorative only — the card speaks for itself.
    private func rail(index: Int, isFirst: Bool, isLast: Bool) -> some View {
        let nodeTop: CGFloat = 17
        return ZStack(alignment: .top) {
            VStack(spacing: 0) {
                Rectangle().fill(isFirst ? Color.clear : Theme.hairline)
                    .frame(width: 1.5, height: nodeTop)
                Rectangle().fill(isLast ? Color.clear : Theme.hairline)
                    .frame(width: 1.5)
                    .frame(maxHeight: .infinity)
            }
            Text("\(index)")
                .workFont(.dataSmallSemibold)
                .foregroundStyle(Theme.muted)
                .padding(.horizontal, 2)
                .background(Theme.canvas)
                .offset(y: nodeTop - 8)
        }
        .frame(width: 18)
        .frame(maxHeight: .infinity)
        .accessibilityHidden(true)
    }
}
