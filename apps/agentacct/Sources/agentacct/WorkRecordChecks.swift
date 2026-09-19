import AppKit
import SwiftUI

/// The recorded check table, on screen.
///
/// `dimensions.evidence.checks[]` has always carried a complete evidence
/// table — each run's name, result words, exit code, evidence type, revision,
/// files and summary — and no surface in the app rendered a single row of it.
/// The reviewer saw a tally ("1/1 passed · 1 earlier run failed") with no way
/// to see the runs it counted.
///
/// Two rules the table exists to keep:
/// * EARLIER RUNS ARE SHOWN, GREYED — never hidden. A fail → pass recovery is
///   the story the receipt is for, and it is only legible as two rows.
/// * The header tally counts the FRONTIER only (`history_run` rows are
///   excluded from it by the reducer), so a history row never reads as a peer.
///
/// ## Say each fact once
///
/// The table used to print the stamped revision on EVERY row — four prints of
/// two values on the flagship record — and the reducer's contradiction sentence
/// on every row that carried it, byte-identically, three times. Both are now
/// hoisted: the revision is a group header over a rule, and the contradiction
/// is ONE amber banner under it. The reducer decides the grouping
/// (`revision_groups`, falling back to strict time order rather than split a
/// supersession pair) and CLEARS the sentence from the rows a banner covers, so
/// reading the groups is not optional — a surface that ignores them prints the
/// sentence nowhere.
///
/// The four result words are likewise one line now (`meta_line`), joined by the
/// reducer on the one separator, with whichever of them is uniform across every
/// row hoisted to the section heading. They used to render as four fragments
/// punctuated as four sentences: `Passed. Exit 0. test. Agent-reported`.
///
/// Every word is the payload's: `meta_line`, `revision_label`,
/// `note_text`, `contradiction_text`, `summary_preview`. The table composes
/// none of them.
struct RecordChecksSection: View {
    let receipt: Receipt
    /// The record page's selection channel: choosing a row selects the same
    /// recorded event in the activity surface below.
    var layers: WorkRecordLayers? = nil
    @State private var expandedID: String?
    /// One reveal per press. A press that OPENS a row bumps the request; the
    /// probe reports the value it served. A row rebuilt later (scrolled away
    /// and back, a refreshed receipt) therefore sees nothing to do and never
    /// re-scrolls the page on its own, while re-opening the same row asks for
    /// its own fresh reveal.
    @State private var revealRequest = 0
    @State private var revealsServed = 0

    private var evidence: ReceiptEvidenceDim { receipt.dimensions.evidence }

    /// Time order, oldest first, with undated runs last in payload order. The
    /// reducer already emits each earlier run immediately before the run that
    /// replaced it; sorting by recorded time preserves exactly that.
    private var rows: [ReceiptCheck] {
        (evidence.checks ?? []).enumerated()
            .sorted { lhs, rhs in
                switch (lhs.element.at, rhs.element.at) {
                case let (left?, right?): return left == right ? lhs.offset < rhs.offset : left < right
                case (nil, _?): return false
                case (_?, nil): return true
                default: return lhs.offset < rhs.offset
                }
            }
            .map(\.element)
    }

    /// One rendered block: the reducer's revision group, and the rows it names.
    private struct Block: Identifiable {
        let id: String
        let label: String?
        let contradiction: String?
        let rows: [ReceiptCheck]
    }

    /// The reducer's groups, resolved to rows.
    ///
    /// Walking `revision_groups` and each group's `event_ids` yields every row
    /// exactly once in strict time order under BOTH grouping modes, so the
    /// groups settle the render order too — iterating `checks` directly would
    /// reproduce the old per-identity order, which reads as alphabetical noise.
    /// A payload with no groups (an older daemon) keeps the one-block shape and
    /// each row's own revision line, so nothing is lost.
    private var blocks: [Block] {
        let all = rows
        guard let groups = evidence.revisionGroups, !groups.isEmpty else {
            return [Block(id: "ungrouped", label: nil, contradiction: nil, rows: all)]
        }
        let byEvent = Dictionary(all.map { (PayloadAbsence.text($0.eventId) ?? $0.id, $0) },
                                 uniquingKeysWith: { first, _ in first })
        var placed = Set<String>()
        var result: [Block] = []
        for group in groups {
            let members = (group.eventIds ?? []).compactMap { byEvent[$0] }
            guard !members.isEmpty else { continue }
            for member in members { placed.insert(PayloadAbsence.text(member.eventId) ?? member.id) }
            result.append(Block(
                id: group.id,
                label: PayloadAbsence.text(group.label),
                contradiction: PayloadAbsence.text(group.contradictionText),
                rows: members
            ))
        }
        // A row the groups did not name is still the reviewer's evidence: it
        // keeps its own revision line and is never dropped.
        let orphans = all.filter { !placed.contains(PayloadAbsence.text($0.eventId) ?? $0.id) }
        if !orphans.isEmpty {
            result.append(Block(id: "ungrouped", label: nil, contradiction: nil, rows: orphans))
        }
        return result
    }

    var body: some View {
        if rows.isEmpty {
            // A task with no recorded runs still names that state — the tally
            // carries the reducer's own words for it.
            Text(ReceiptCheckRunsPresentation(evidence: evidence).rowText)
                .workFont(.body).foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("work.record.checks.empty")
        } else {
            // Lazy: a Task whose checks have been re-run many times lists every
            // run, and building the ones scrolled past costs the same as
            // building the ones on screen. Nothing is elided — every run still
            // scrolls into view, earlier ones greyed, exactly as before.
            ScrollContentStack(alignment: .leading, spacing: 0) {
                ForEach(Array(blocks.enumerated()), id: \.element.id) { blockIndex, block in
                    if let label = block.label {
                        groupHeader(label, contradiction: block.contradiction,
                                    first: blockIndex == 0, identifier: block.id)
                    } else if blockIndex > 0 {
                        Rectangle().fill(Theme.hairline).frame(height: 1)
                    }
                    ForEach(Array(block.rows.enumerated()), id: \.element.id) { index, check in
                        if index > 0 { Rectangle().fill(Theme.hairline).frame(height: 1) }
                        row(check)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("work.record.checks")
        }
    }

    /// The stamped revision, once, over a rule — and the group's one
    /// contradiction sentence under it. A rule plus text, never a second card,
    /// so the page gains no new radius.
    @ViewBuilder
    private func groupHeader(_ label: String, contradiction: String?,
                             first: Bool, identifier: String) -> some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            Text(verbatim: label)
                .workFont(FieldFont.qualifier).foregroundStyle(Theme.muted)
                .lineLimit(1).truncationMode(.middle)
                .textSelection(.enabled)
            Rectangle().fill(Theme.hairline).frame(height: 1)
            if let contradiction {
                // The reducer's one sentence for a stamped revision that cannot
                // contain the paths the checks declared. It used to print on
                // every row of the group — three byte-identical copies on the
                // flagship record.
                Label(contradiction, systemImage: "exclamationmark.triangle")
                    .workFont(.caption).foregroundStyle(Theme.amber)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: Metrics.readingMeasure, alignment: .leading)
            }
        }
        .padding(.top, first ? 0 : Space.m)
        .padding(.bottom, Space.xs)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("work.record.checks.revision.\(identifier)")
    }

    // MARK: one run

    @ViewBuilder
    private func row(_ check: ReceiptCheck) -> some View {
        let history = check.historyRun == true || check.superseded == true
        let expanded = expandedID == check.id
        VStack(alignment: .leading, spacing: 6) {
            Button { activate(check) } label: {
                HStack(alignment: .top, spacing: Space.m) {
                    RecordCheckResultGlyph(check: check).padding(.top, 2)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(PayloadAbsence.text(check.title) ?? PayloadAbsence.text(check.name)
                             ?? PayloadAbsence.text(check.evidenceType) ?? PayloadAbsence.checkResult)
                            .workFont(.rowLabel)
                            // A superseded run is HISTORY, not noise: greyed,
                            // still legible, never removed.
                            .foregroundStyle(history ? Theme.muted : Theme.ink)
                            .fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: .infinity, alignment: .leading)
                        // ONE line, one separator, composed by the reducer —
                        // result, exit code, and whichever of evidence type and
                        // source the heading did not hoist. It used to be four
                        // Texts a reader parsed as four sentences.
                        Text(verbatim: check.metaLineText)
                            .workFont(.caption)
                            .foregroundStyle(resultTint(check, history: history))
                            .fixedSize(horizontal: false, vertical: true)
                        // The reducer's named result/exit-code disagreement,
                        // directly under the exit code it disagrees with. It
                        // stays per-row: it is THIS row's disagreement.
                        if let note = PayloadAbsence.text(check.noteText) {
                            Label(note, systemImage: "exclamationmark.triangle")
                                .workFont(.caption).foregroundStyle(Theme.amber)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        // The stamped revision, only when the group header did
                        // NOT already print it.
                        if check.revisionLabelHoisted != true {
                            Text(check.revisionText)
                                .workFont(FieldFont.qualifier).foregroundStyle(Theme.muted)
                                .lineLimit(1).truncationMode(.middle)
                        }
                        // A stamped revision that cannot contain the paths the
                        // check declared. The reducer clears this on rows a
                        // group banner covers, so it renders here only when the
                        // rows' sentences differ and no banner could carry them.
                        if let contradiction = PayloadAbsence.text(check.revisionContradictionText) {
                            Label(contradiction, systemImage: "exclamationmark.triangle")
                                .workFont(.caption).foregroundStyle(Theme.amber)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        // `summary_preview` is a run of WHOLE sentences chosen
                        // by the reducer, so it is deliberately NOT clamped to
                        // a line count: a one-line clamp cut
                        // `raises decimal.InvalidOperation on the st…`
                        // mid-clause.
                        if let summary = PayloadAbsence.text(check.summaryPreview)
                            ?? PayloadAbsence.text(check.summary) {
                            Text(verbatim: summary).workFont(.caption).foregroundStyle(Theme.muted)
                                .fixedSize(horizontal: false, vertical: true)
                                .frame(maxWidth: Metrics.readingMeasure, alignment: .leading)
                        }
                        if let files = check.files, !files.isEmpty {
                            Text(files.joined(separator: " · "))
                                .workFont(.dataSmall).foregroundStyle(Theme.muted)
                                .lineLimit(expanded ? nil : 1).truncationMode(.middle)
                        }
                    }
                }
                .padding(.vertical, Space.s)
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(SurfaceButtonStyle())
            // A check row is the page's primary evidence control — expanding it
            // is how a reviewer reads the proof — so it is a Tab stop, and
            // Return runs the same `activate` a click runs.
            .keyboardStop { activate(check) }
            .accessibilityIdentifier("work.record.checks.row.\(check.id)")
            .accessibilityLabel(spokenLabel(check))
            if expanded { detail(check) }
        }
        // The row and the detail it just opened, measured together: the page
        // scrolls the minimum needed to hold BOTH, and not at all when they
        // already fit.
        .background(reveal(expanded: expanded))
    }

    /// Keeps a freshly expanded row and its detail on screen.
    ///
    /// The static renderer has no scroll view to measure against, so it gets
    /// nothing — the reference images are unchanged.
    @ViewBuilder
    private func reveal(expanded: Bool) -> some View {
        if !SnapshotMode.enabled || SnapshotMode.interactiveFixture {
            // Zero is "nothing to do": this row is closed, or the press that
            // opened it has already been served.
            let pending = expanded && revealRequest > revealsServed
            KeepRegionOnScreen(request: pending ? revealRequest : 0) { revealsServed = revealRequest }
        }
    }

    /// The facts that would crowd a resting row: how the command was handled,
    /// how many runs this identity has, and the supersession pointers that make
    /// a recovery navigable in both directions.
    @ViewBuilder
    private func detail(_ check: ReceiptCheck) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            // The rest of the agent's own account, when the preview did not
            // reach the end of it. The preview is not an excerpt policy — it is
            // the shortest whole-sentence run that still holds the finding — so
            // this is the remainder, never a re-print of what is already shown.
            if check.summaryElided == true, let full = PayloadAbsence.text(check.summary) {
                Text(verbatim: full).workFont(.caption).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: Metrics.readingMeasure, alignment: .leading)
            }
            if let runs = check.runsTotal, runs > 1 {
                Text("\(runs) recorded runs of this check").workFont(.caption).foregroundStyle(Theme.muted)
            }
            if let superseding = PayloadAbsence.text(check.supersededByEventId) {
                factRow("Replaced by", superseding)
            }
            if let earlier = PayloadAbsence.text(check.supersedesCheckEventId) {
                factRow("Replaces", earlier)
                if let basis = PayloadAbsence.text(check.supersedesBasis) {
                    factRow("On the authority of", basis)
                }
            }
            if let absent = check.revisionAbsentFiles, !absent.isEmpty {
                factRow("Absent at that revision", absent.joined(separator: " · "))
            }
            if let event = PayloadAbsence.text(check.eventId) { factRow("Event", event) }
            // `command_state_text`, `superseded_definition` and `scope` are NOT
            // here: each is identical on every row of a record (`scope` is
            // byte-identical on all four rows of the flagship), so each is
            // printed once, in the Evidence section's disclosure.
        }
        .textSelection(.enabled)
        .padding(.bottom, Space.s)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityIdentifier("work.record.checks.detail.\(check.id)")
    }

    private func factRow(_ label: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Space.s) {
            CapsLabel(text: label)
            Text(value).workFont(.dataSmall).foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func resultTint(_ check: ReceiptCheck, history: Bool) -> Color {
        if check.isResolvedFailure { return Theme.coral }
        if history { return Theme.muted }
        return CheckResultTone(payload: check.resultTone).tint(pass: Theme.ink)
    }

    private func spokenLabel(_ check: ReceiptCheck) -> String {
        [PayloadAbsence.text(check.title) ?? PayloadAbsence.text(check.name),
         check.metaLineText,
         check.revisionLabelHoisted == true ? nil : check.revisionText,
         PayloadAbsence.text(check.noteText)]
            .compactMap { $0 }.joined(separator: ", ")
    }

    /// A row both expands its own detail and selects the matching record in
    /// the activity surface, so the table and the canvas/list never disagree
    /// about what the reviewer is looking at.
    private func activate(_ check: ReceiptCheck) {
        let opening = expandedID != check.id
        expandedID = opening ? check.id : nil
        // Only an opening press may move the page, and then only as far as the
        // detail it opened. Collapsing leaves the reading position alone.
        if opening { revealRequest += 1 }
        if let event = PayloadAbsence.text(check.eventId) { layers?.selectEvent(event) }
    }
}

/// The minimum document scroll that brings a region fully on screen.
///
/// Expanding a row in place is not a reason to move the reviewer: this moves
/// the page only when the region would not fit, and then only as far as it
/// must. A region TALLER than the viewport shows its top — the row that was
/// pressed — never its far end.
enum RegionReveal {
    /// The scroll position to move an enclosing clip view to, or `nil` to leave
    /// it exactly where it is. `region` and `visible` are both in the clip
    /// view's coordinates; `flipped` is that clip view's own orientation, and
    /// decides which edge of an oversized region is its top.
    static func contentOffset(region: CGRect, visible: CGRect, document: CGSize,
                              flipped: Bool, margin: CGFloat = Space.s) -> CGPoint? {
        guard region.height > 0, visible.height > 0,
              region.minY.isFinite, region.maxY.isFinite,
              visible.minY.isFinite, document.height.isFinite else { return nil }
        // Already on screen: the reading position is the reviewer's.
        if region.minY >= visible.minY, region.maxY <= visible.maxY { return nil }
        let target: CGFloat
        if region.height + margin * 2 >= visible.height {
            target = flipped ? region.minY - margin : region.maxY + margin - visible.height
        } else if region.minY < visible.minY {
            target = region.minY - margin
        } else {
            target = region.maxY + margin - visible.height
        }
        let limit = max(document.height - visible.height, 0)
        let y = min(max(target, 0), limit)
        guard abs(y - visible.minY) > 0.5 else { return nil }
        return CGPoint(x: visible.minX, y: y)
    }
}

/// Scrolls the region it backs into view once per armed request.
///
/// It measures on demand, never observes scrolling, and asks
/// `RegionReveal` — which declines to move an already-visible region — so it
/// cannot displace a reviewer who can already see what they opened.
/// Internal, not private: `KeyboardStop` reveals a freshly focused control with
/// this same probe, so the app has ONE reveal policy rather than one per caller.
struct KeepRegionOnScreen: NSViewRepresentable {
    /// A positive value that CHANGES arms exactly one reveal; 0 never arms.
    var request: Int
    /// Called after the reveal is attempted, so the request is consumed once.
    var onRevealed: () -> Void

    func makeNSView(context: Context) -> ProbeView { ProbeView() }

    func updateNSView(_ nsView: ProbeView, context: Context) {
        nsView.onRevealed = onRevealed
        nsView.arm(request)
    }

    final class ProbeView: NSView {
        var onRevealed: (() -> Void)?
        private var handled = 0
        private var scheduled = false

        func arm(_ request: Int) {
            guard request > 0, request != handled else { return }
            handled = request
            guard !scheduled else { return }
            scheduled = true
            // Two hops: the first lands after the detail is inserted, the
            // second after it has been laid out at its real height — so the
            // region measured is the one the reviewer will see.
            DispatchQueue.main.async { [weak self] in
                DispatchQueue.main.async { [weak self] in
                    guard let self else { return }
                    scheduled = false
                    reveal()
                    onRevealed?()
                }
            }
        }

        private func reveal() {
            var current = superview
            while let view = current {
                if let scroll = view as? NSScrollView {
                    let clip = scroll.contentView
                    guard let offset = RegionReveal.contentOffset(
                        region: convert(bounds, to: clip), visible: clip.bounds,
                        document: scroll.documentView?.bounds.size ?? .zero,
                        flipped: clip.isFlipped) else { return }
                    clip.scroll(to: offset)
                    scroll.reflectScrolledClipView(clip)
                    return
                }
                current = view.superview
            }
        }
    }
}

/// A recorded run's result mark. Shape carries the state — open for a failure
/// a later run replaced — so colour is never the only carrier and the
/// interactive accent is never a data mark.
struct RecordCheckResultGlyph: View {
    let check: ReceiptCheck

    var body: some View {
        let tone = CheckResultTone(payload: check.resultTone)
        let history = check.historyRun == true || check.superseded == true
        Group {
            if check.isResolvedFailure {
                Image(systemName: "exclamationmark.arrow.circlepath")
            } else if history {
                Image(systemName: "clock.arrow.circlepath")
            } else {
                switch tone {
                case .pass: Image(systemName: "checkmark.circle")
                case .failure: Image(systemName: "xmark.circle")
                case .notRun: Image(systemName: CheckResultTone.notRun.symbol)
                }
            }
        }
        .workFont(.caption)
        .foregroundStyle(check.isResolvedFailure ? Theme.coral
                         : history ? Theme.muted
                         : tone.tint(pass: Theme.chartNeutral))
        .frame(width: 14, alignment: .center)
        .accessibilityHidden(true)  // the row's label names the result in words
    }
}
