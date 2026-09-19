import SwiftUI

/// The loaded records as an ORDERED LIST — the same linear content the review
/// export has always produced, on screen.
///
/// A proportional time axis is the wrong instrument for a burst: three records
/// inside a third of a second collapse into one unreadable column, and the
/// reviewer is left dragging a canvas looking for evidence that is already in
/// hand. The list states the order, the lane, the result and the revision
/// directly; the overview strip above it keeps the zoom and filter the canvas
/// used to own.
///
/// Every word here is the payload's: `lane_label`, `status_label`,
/// `revision_label`, the recorded summary. The list composes none of them.
struct WorkTimelineRecordList: View {
    let records: [WorkTimelineRecord]
    let selectedID: String?
    /// The loaded range, so a row's time carries a DATE exactly when the task
    /// spans more than one day — the same rule the canvas axis uses (C55).
    let range: WorkTimelineInterval?
    /// The record the payload marks as this one's successor, if it is loaded —
    /// so a fail → pass recovery reads as two adjacent rows, not two unrelated
    /// ones.
    var onSelect: (WorkTimelineRecord) -> Void
    var onFocusEnter: (() -> Void)? = nil
    @FocusState private var focusedRow: String?

    var body: some View {
        // Lazy: each row formats its own dates and builds its own accessibility
        // sentence, so an offscreen row is not free. Every record still
        // renders, in the same order, as it scrolls into view.
        ScrollContentStack(alignment: .leading, spacing: 0) {
            if records.isEmpty {
                Text("No activity in this time window")
                    .workFont(.body).foregroundStyle(Theme.muted)
                    .frame(maxWidth: .infinity, minHeight: 120)
            } else {
                ForEach(Array(records.enumerated()), id: \.element.id) { index, record in
                    if index > 0 {
                        Rectangle().fill(Theme.hairline).frame(height: 1)
                    }
                    row(record)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Theme.cardLine))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("work.timeline.record-list")
    }

    @ViewBuilder
    private func row(_ record: WorkTimelineRecord) -> some View {
        if record.isBeat { beatRow(record) } else { stepRow(record) }
    }

    private func stepRow(_ record: WorkTimelineRecord) -> some View {
        let selected = record.id == selectedID
        return Button { onSelect(record) } label: {
            HStack(alignment: .top, spacing: Space.m) {
                WorkRecordResultGlyph(record: record)
                    .padding(.top, 2)
                Text(record.start.map { WorkTimelineTimeAxis.label($0, range: range ?? .init(lower: $0, upper: $0)) }
                     ?? PayloadAbsence.activityTime)
                    .workFont(.dataSmall).foregroundStyle(Theme.muted)
                    .frame(width: 112, alignment: .leading)
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: Space.s) {
                        if let lane = record.laneLabel {
                            Text(lane).workFont(.caption).foregroundStyle(Theme.muted).lineLimit(1)
                        }
                        if let kind = record.sectionKind {
                            Text(kind).workFont(.caption).foregroundStyle(Theme.muted).lineLimit(1)
                        }
                    }
                    Text(record.displayTitle)
                        .workFont(.rowLabel).foregroundStyle(Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    HStack(spacing: Space.s) {
                        Text(record.resultLabel)
                            .workFont(.caption).foregroundStyle(record.presentationTint)
                        if record.noteText != nil {
                            Image(systemName: "exclamationmark.triangle")
                                .workFont(.caption).foregroundStyle(Theme.amber)
                                .accessibilityHidden(true)
                        }
                        Text(record.source).workFont(.caption).foregroundStyle(Theme.muted).lineLimit(1)
                    }
                    if let revision = record.revisionLabel {
                        Text(revision).workFont(FieldFont.qualifier).foregroundStyle(Theme.muted)
                            .lineLimit(1).truncationMode(.middle)
                    }
                    if let summary = WorkTimelineProjection.nonempty(record.summary), summary != record.title {
                        Text(summary).workFont(.caption).foregroundStyle(Theme.muted).lineLimit(1)
                    }
                    // WHY this row is marked, in the reducer's own sentence.
                    // The rule alone says "look here"; without the sentence a
                    // reviewer has to guess what it noticed (F3).
                    if let reason = record.salienceReason, record.isSalient {
                        Text(reason).workFont(FieldFont.qualifier).foregroundStyle(Theme.muted)
                            .fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: Metrics.readingMeasure, alignment: .leading)
                    }
                }
            }
            .padding(.vertical, Space.s)
            .padding(.horizontal, Space.m)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(selected ? Theme.selected : Color.clear)
            .modifier(SalienceRule(record: record))
            .contentShape(Rectangle())
        }
        .buttonStyle(SurfaceButtonStyle())
        .focused($focusedRow, equals: record.id)
        .onChange(of: focusedRow) { _, id in
            guard id == record.id else { return }
            onFocusEnter?()
        }
        .accessibilityIdentifier("work.timeline.list.record.\(record.id)")
        .accessibilityLabel([record.displayTitle, record.resultLabel, record.laneLabel ?? record.laneTitle,
                             record.source, record.noteText,
                             record.isSalient ? record.salienceReason : nil,
                             record.start.map { WorkTimelineTimeAxis.spokenLabel($0) } ?? PayloadAbsence.activityTime]
            .compactMap { $0 }.joined(separator: ", "))
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    /// A progress note: narration the section recorded while it was still
    /// open, drawn SUBORDINATE to it and unmistakable from a check.
    ///
    /// * Indented past the result-glyph column, under a vertical hairline that
    ///   runs the height of the row — the note belongs to the section above it,
    ///   not beside it.
    /// * NO result glyph and no result tone. A beat reports no outcome; giving
    ///   it a check's circle or a step's square would say it did.
    /// * Its prose at caption weight in muted ink, so a page of beats can never
    ///   out-shout one recorded step.
    ///
    /// Every word is the payload's: the reducer's `status_label` and the note's
    /// own sentence. The section it belongs to is named with `section_title`.
    private func beatRow(_ record: WorkTimelineRecord) -> some View {
        let selected = record.id == selectedID
        return Button { onSelect(record) } label: {
            HStack(alignment: .top, spacing: Space.m) {
                // The subordination mark: a rule in the glyph column, not a
                // shape — shapes on this surface carry evidence tiers (K05).
                // The palette's line token, not the divider hairline: this is
                // a deliberate mark a reader is meant to see, and the row's
                // dividers are drawn in the fainter one.
                Rectangle().fill(Theme.rule)
                    .frame(width: 1)
                    .frame(width: 14)
                Text(record.start.map { WorkTimelineTimeAxis.label($0, range: range ?? .init(lower: $0, upper: $0)) }
                     ?? PayloadAbsence.activityTime)
                    .workFont(.dataSmall).foregroundStyle(Theme.muted)
                    .frame(width: 112, alignment: .leading)
                VStack(alignment: .leading, spacing: 3) {
                    if let section = WorkTimelineProjection.nonempty(record.sectionTitle) {
                        Text(section).workFont(.caption).foregroundStyle(Theme.muted)
                            .lineLimit(1).truncationMode(.tail)
                    }
                    Text(WorkTimelineProjection.nonempty(record.summary) ?? record.displayTitle)
                        .workFont(.caption).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: Metrics.readingMeasure, alignment: .leading)
                    Text(record.resultLabel)
                        .workFont(.caption).foregroundStyle(Theme.muted)
                }
                .padding(.leading, Space.m)  // indented under its section
            }
            .padding(.vertical, Space.s)
            .padding(.horizontal, Space.m)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(selected ? Theme.selected : Color.clear)
            .contentShape(Rectangle())
        }
        .buttonStyle(SurfaceButtonStyle())
        .focused($focusedRow, equals: record.id)
        .onChange(of: focusedRow) { _, id in
            guard id == record.id else { return }
            onFocusEnter?()
        }
        .accessibilityIdentifier("work.timeline.list.beat.\(record.id)")
        .accessibilityLabel([WorkTimelineProjection.nonempty(record.summary) ?? record.displayTitle,
                             record.resultLabel,
                             WorkTimelineProjection.nonempty(record.sectionTitle),
                             record.start.map { WorkTimelineTimeAxis.spokenLabel($0) } ?? PayloadAbsence.activityTime]
            .compactMap { $0 }.joined(separator: ", "))
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

/// The ONE salience mark (F3): a neutral rule on the record's leading edge.
///
/// Salience is a reducer decision about READER IMPACT, not a result, so it
/// borrows no existing voice — not the cobalt accent (the interactive voice),
/// and not coral/amber/green (the failure and evidence tones). A line in the
/// palette's own rule token says "this one" without claiming anything about
/// what the record proved, and it survives greyscale.
struct SalienceRule: ViewModifier {
    let record: WorkTimelineRecord?

    func body(content: Content) -> some View {
        content.overlay(alignment: .leading) {
            Rectangle()
                .fill(record?.isSalient == true ? Theme.rule : Color.clear)
                .frame(width: 3)
                .accessibilityHidden(true)  // the row's label speaks the reason
        }
    }
}

/// One record's result mark, shared by the list and the record page's Checks
/// table. Shape carries the state — filled for a live failure, OPEN for a
/// failure a later run replaced, the tone's own glyph otherwise — so colour is
/// never the only carrier and the interactive accent is never a data mark.
struct WorkRecordResultGlyph: View {
    let record: WorkTimelineRecord

    var body: some View {
        Group {
            if record.isBeat {
                // Narration has no result to mark. A rule, never a shape:
                // shapes on this surface belong to evidence tiers (K05).
                Rectangle().fill(Theme.rule).frame(width: 1, height: 12)
            } else if record.isResolvedFailure {
                Image(systemName: "exclamationmark.arrow.circlepath")
            } else if record.kind == .step {
                // A lifecycle step is not an evidence tier: a flat square,
                // never a pip-family circle (K05).
                Rectangle().frame(width: 6, height: 6)
            } else {
                switch record.checkTone {
                case .pass: Image(systemName: "checkmark.circle")
                case .failure: Image(systemName: record.isCurrentFailure ? "xmark.circle" : "minus")
                case .notRun: Image(systemName: CheckResultTone.notRun.symbol)
                }
            }
        }
        .workFont(.caption)
        .foregroundStyle(record.markTint)
        .frame(width: 14, alignment: .center)
        .accessibilityHidden(true)  // the row's label names the result in words
    }
}
