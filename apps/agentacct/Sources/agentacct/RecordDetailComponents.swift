import SwiftUI

// The redesigned record-detail building blocks: the colored outcome segment
// bars (what succeeded / what failed at a glance), the numbered vertical step
// spine, and the mockup step row. These render the ledger's honesty gradient —
// proven → claimed → failed — as the primary visual, reusing the shared pieces
// (EvidencePip / EvidenceTierStyle / StepCheckDigest / CheckRow) so this surface
// can never disagree with the tier grammar the rest of the app speaks.

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

// MARK: - Wrapping legend layout

/// A minimal flow layout: lays children left-to-right, wrapping to the next line
/// when the proposed width runs out. Used for the outcome-bar legends so a bar
/// with many tiers never clips.
struct WrapLayout: Layout {
    var spacing: CGFloat = 16
    var lineSpacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let maxW = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, lineH: CGFloat = 0, widest: CGFloat = 0
        for s in subviews {
            let sz = s.sizeThatFits(.unspecified)
            if x > 0 && x + sz.width > maxW { x = 0; y += lineH + lineSpacing; lineH = 0 }
            x += sz.width + spacing
            lineH = max(lineH, sz.height)
            widest = max(widest, x - spacing)
        }
        let width = maxW.isFinite ? min(maxW, widest) : widest
        return CGSize(width: max(width, 0), height: y + lineH)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let maxW = bounds.width
        var x: CGFloat = 0, y: CGFloat = 0, lineH: CGFloat = 0
        for s in subviews {
            let sz = s.sizeThatFits(.unspecified)
            if x > 0 && x + sz.width > maxW { x = 0; y += lineH + lineSpacing; lineH = 0 }
            s.place(at: CGPoint(x: bounds.minX + x, y: bounds.minY + y),
                    anchor: .topLeading, proposal: ProposedViewSize(sz))
            x += sz.width + spacing
            lineH = max(lineH, sz.height)
        }
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
    /// Stretch to the height the parent offers, so cards laid out side by side
    /// share one bottom edge. A standalone card keeps its intrinsic height.
    var fillsHeight = false

    private var visible: [OutcomeSegment] { segments.filter { $0.count > 0 } }

    var body: some View {
        Card(fillsHeight: fillsHeight) {
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .firstTextBaseline) {
                    Text(title).workFont(.titleCard).foregroundStyle(Theme.ink)
                    Spacer(minLength: Space.s)
                    Text(total).workFont(.dataSmall).foregroundStyle(Theme.muted)
                }
                .padding(.bottom, 10)

                OutcomeSegmentBar(segments: visible)
                    .padding(.bottom, 11)

                WrapLayout(spacing: 16, lineSpacing: 6) {
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

// MARK: - Step outcome + check classification

/// The evidence/decision bucket a step falls into for the Steps outcome bar.
/// Failure and the deliberate hand-off are decision-axis states; the rest are
/// the positive-proof tiers, so the bar reads proven → claimed at a glance and
/// never paints a mere completion green.
enum StepOutcomeBucket: Int, CaseIterable {
    case verified, checked, selfChecked, claimed, none, handedOff, blocked

    var label: String {
        switch self {
        case .verified: return "verified"
        case .checked: return "checked"
        case .selfChecked: return "self-checked"
        case .claimed: return "claimed"
        case .none: return "none"
        case .handedOff: return "handed off"
        case .blocked: return "blocked"
        }
    }

    var color: Color {
        switch self {
        case .verified: return Theme.green
        case .checked: return Theme.green   // independently checked = independent evidence → green
        case .selfChecked: return Theme.accent
        case .claimed: return Theme.amber
        case .none: return Theme.muted
        case .handedOff: return Theme.ink   // a deliberate stop — neutral ink, distinct from muted "none"
        case .blocked: return Theme.coral
        }
    }

    static func of(_ step: V1Step) -> StepOutcomeBucket {
        if step.latestStatus == "blocked" || step.latestStatus == "failed" || step.evidenceStatus == "failed" {
            return .blocked
        }
        if step.latestStatus == "handed_off" { return .handedOff }
        switch step.evidenceGrade {
        case "externally_verified": return .verified
        case "independently_checked": return .checked
        case "self_checked": return .selfChecked
        case "claimed", "unchecked": return .claimed
        default: return .none
        }
    }
}

enum RecordOutcome {
    /// Steps → outcome-bar segments, in proven → failed order, only non-empty.
    static func stepSegments(_ steps: [V1Step]) -> [OutcomeSegment] {
        var counts: [StepOutcomeBucket: Int] = [:]
        for step in steps { counts[StepOutcomeBucket.of(step), default: 0] += 1 }
        return StepOutcomeBucket.allCases.compactMap { bucket in
            let n = counts[bucket] ?? 0
            return n > 0 ? OutcomeSegment(count: n, color: bucket.color, label: bucket.label) : nil
        }
    }

    /// Current (non-superseded) checks across the steps → passed / failed /
    /// skipped segments. Passed is green ONLY when every pass is independently
    /// observed (CI / hook / provider) — an agent-reported pass stays ink, the
    /// same rule CheckRow uses per check, so the bar can't over-claim.
    static func checkSegments(_ digest: StepCheckDigest) -> [OutcomeSegment] {
        var segments: [OutcomeSegment] = []
        let failed = digest.failedCount + digest.errorCount
        if digest.passedCount > 0 {
            segments.append(.init(count: digest.passedCount, color: passedTint(digest.current), label: "passed"))
        }
        if failed > 0 { segments.append(.init(count: failed, color: Theme.coral, label: "failed")) }
        if digest.skippedCount > 0 { segments.append(.init(count: digest.skippedCount, color: Theme.amber, label: "skipped")) }
        let unknown = digest.currentCount - digest.passedCount - failed - digest.skippedCount
        if unknown > 0 { segments.append(.init(count: unknown, color: Theme.muted, label: "unknown")) }
        return segments
    }

    /// Green only when every passed check is independently observed; otherwise
    /// ink — matching CheckPresentation.resultTint so the summary never paints an
    /// agent-reported pass with the reserved verified-green.
    static func passedTint(_ items: [StepCheckItem]) -> Color {
        let observed: Set<String> = ["ci", "external", "provider", "client_hook"]
        let passes = items.filter { $0.check.result == "passed" }
        guard !passes.isEmpty else { return Theme.accent }
        // Green only when independently observed; an agent-reported pass is cobalt
        // (the self-checked voice) — positive, but never the reserved verified-green.
        return passes.allSatisfy { observed.contains($0.check.sourceType ?? "") } ? Theme.green : Theme.accent
    }
}

/// The two outcome cards (Steps + Checks) that lead the record — the "did it
/// succeed, and how strong is the proof" answer, replacing the old flat strip.
struct RecordOutcomeBars: View {
    let steps: [V1Step]

    private var stepSegments: [OutcomeSegment] { RecordOutcome.stepSegments(steps) }
    private var digest: StepCheckDigest { StepCheckDigest(checks: steps.flatMap { $0.checks ?? [] }) }

    private var stepTotal: String {
        let n = steps.count
        return "\(n) total"
    }

    private var checkNote: String? {
        let failed = digest.failedCount + digest.errorCount
        guard failed > 0 else { return nil }
        return "\u{25B3} \(failed) failing check\(failed == 1 ? "" : "s") need\(failed == 1 ? "s" : "") attention"
    }

    var body: some View {
        ViewThatFits(in: .horizontal) {
            // Side by side, both cards take the taller card's height: only
            // Checks carries the attention note, and a shorter Steps card next
            // to it reads as misaligned. fixedSize holds the row at that
            // intrinsic height so the stretched cards never grow past it.
            HStack(alignment: .top, spacing: Space.m) { bars(fillsHeight: true) }
                .fixedSize(horizontal: false, vertical: true)
            VStack(alignment: .leading, spacing: Space.m) { bars(fillsHeight: false) }
        }
    }

    @ViewBuilder private func bars(fillsHeight: Bool) -> some View {
        OutcomeBar(title: "Steps", total: stepTotal, segments: stepSegments, fillsHeight: fillsHeight)
        OutcomeBar(
            title: "Checks",
            total: "\(digest.currentCount) recorded",
            segments: RecordOutcome.checkSegments(digest),
            note: checkNote,
            fillsHeight: fillsHeight
        )
    }
}

// MARK: - Step spine

/// The vertical, numbered step spine: each step is a node on a shared rail so
/// the sequence and each step's evidence pip read top-to-bottom.
struct SessionStepSpine: View {
    let items: [SessionStepItem]
    let openedIDs: Set<String>

    var body: some View {
        VStack(spacing: 0) {
            ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                StepSpineRow(
                    index: index + 1,
                    step: item.step,
                    isFirst: index == 0,
                    isLast: index == items.count - 1,
                    initiallyExpanded: openedIDs.contains(item.id),
                    accessibilityContext: item.id
                )
            }
        }
    }
}

struct StepSpineRow: View {
    let index: Int
    let step: V1Step
    let isFirst: Bool
    let isLast: Bool
    let accessibilityContext: String?
    @State private var expanded: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    init(index: Int, step: V1Step, isFirst: Bool, isLast: Bool,
         initiallyExpanded: Bool = false, accessibilityContext: String? = nil) {
        self.index = index
        self.step = step
        self.isFirst = isFirst
        self.isLast = isLast
        self.accessibilityContext = accessibilityContext
        _expanded = State(initialValue: initiallyExpanded)
    }

    private var digest: StepCheckDigest { StepCheckDigest(checks: step.checks ?? []) }
    private var tier: EvidenceTierStyle { EvidenceTierStyle.forGrade(step.evidenceGrade) }
    private var isAttention: Bool {
        step.latestStatus == "blocked" || step.latestStatus == "failed" || step.evidenceStatus == "failed"
    }
    private var title: String {
        for candidate in [step.title, step.sectionId] {
            if let value = candidate?.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty { return value }
        }
        return "Untitled step"
    }

    /// Node center measured from the row's top — aligns the pip with the header
    /// title. The rail draws clear above it on the first row and clear below it
    /// on the last, so the spine begins and ends at a node.
    private let nodeTop: CGFloat = 17

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            rail
            card
        }
        .animation(reduceMotion ? nil : Motion.contentUpdate, value: expanded)
    }

    private var rail: some View {
        ZStack(alignment: .top) {
            VStack(spacing: 0) {
                Rectangle().fill(isFirst ? Color.clear : Theme.hairline)
                    .frame(width: 1.5, height: nodeTop)
                Rectangle().fill(isLast ? Color.clear : Theme.hairline)
                    .frame(width: 1.5)
                    .frame(maxHeight: .infinity)
            }
            EvidencePip(shape: tier.pip, tint: tier.tint)
                .padding(4)
                .background(Circle().fill(Theme.canvas))
                .offset(y: nodeTop - 8)
        }
        .frame(width: 16)
        .frame(maxHeight: .infinity)
        .accessibilityHidden(true)
    }

    private var card: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { expanded.toggle() } label: { header }
                .buttonStyle(SurfaceButtonStyle(focusInset: 2))
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(accessibilityLabelText)
                .accessibilityValue(accessibilityValue)
                .accessibilityHint(expanded ? "Hides step details" : "Shows step details")

            if expanded {
                StepDetailBody(step: step)
                    .padding(.horizontal, 12)
                    .padding(.bottom, 12)
                    .padding(.leading, 6)
                    .transition(.opacity)
            }
        }
        .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(
            RoundedRectangle(cornerRadius: Metrics.radius)
                .strokeBorder(isAttention ? Theme.coral.opacity(0.55) : Theme.cardLine,
                              lineWidth: isAttention ? 1.5 : Metrics.borderW)
        )
        .padding(.vertical, 3)
    }

    private var header: some View {
        ViewThatFits(in: .horizontal) {
            regularHeader
            stackedHeader
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 11)
        .contentShape(Rectangle())
    }

    private var regularHeader: some View {
        HStack(spacing: 9) {
            stepNumber
            Text(title).workFont(.rowLabel).foregroundStyle(Theme.ink)
                .lineLimit(expanded ? nil : 2).layoutPriority(1)
            if let kind = step.kind, kind != "unknown" { Chip(text: kind, tint: Theme.muted) }
            if isAttention { Chip(text: step.latestStatus ?? "blocked", tint: Theme.coral) }
            Spacer(minLength: 8)
            checkPill
            if let ago = durationOrAgo { Text(ago).workFont(.dataSmall).foregroundStyle(Theme.muted) }
            disclosure
        }
    }

    private var stackedHeader: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top, spacing: 9) {
                stepNumber
                Text(title).workFont(.rowLabel).foregroundStyle(Theme.ink)
                    .lineLimit(expanded ? nil : 2).layoutPriority(1)
                Spacer(minLength: 4)
                disclosure
            }
            HStack(spacing: 8) {
                if let kind = step.kind, kind != "unknown" { Chip(text: kind, tint: Theme.muted) }
                if isAttention { Chip(text: step.latestStatus ?? "blocked", tint: Theme.coral) }
                Spacer(minLength: 4)
                checkPill
                if let ago = durationOrAgo { Text(ago).workFont(.dataSmall).foregroundStyle(Theme.muted) }
            }
            .padding(.leading, 24)
        }
    }

    private var stepNumber: some View {
        Text("\(index)")
            .workFont(.dataSmallSemibold).foregroundStyle(Theme.muted)
            .frame(minWidth: 16, alignment: .leading)
    }

    @ViewBuilder private var checkPill: some View {
        let failed = digest.failedCount + digest.errorCount
        if digest.currentCount == 0 {
            Text("no checks").workFont(.dataSmall).foregroundStyle(Theme.muted)
        } else {
            HStack(spacing: 7) {
                if digest.passedCount > 0 {
                    Text("\u{2713} \(digest.passedCount)").workFont(.dataSmallSemibold)
                        .foregroundStyle(RecordOutcome.passedTint(digest.current))
                }
                if failed > 0 {
                    Text("\u{2717} \(failed)").workFont(.dataSmallSemibold).foregroundStyle(Theme.coral)
                }
                if digest.passedCount == 0 && failed == 0 {
                    Text(digest.summary).workFont(.dataSmall).foregroundStyle(Theme.muted)
                }
            }
            .padding(.horizontal, 9)
            .frame(minHeight: Metrics.chipH)
            .background(Theme.chipBg, in: Capsule())
            .overlay(Capsule().strokeBorder(Theme.chipLine, lineWidth: Metrics.borderW))
        }
    }

    private var disclosure: some View {
        Image(systemName: expanded ? "chevron.down" : "chevron.forward")
            .font(.system(size: 9, weight: .semibold))
            .foregroundStyle(Theme.muted)
            .frame(width: 12, height: 16)
    }

    private var durationOrAgo: String? { agoText(step.updatedAt) }

    private var accessibilityLabelText: String {
        let candidate = accessibilityContext ?? step.sectionId ?? step.workId
        guard let context = candidate?.trimmingCharacters(in: .whitespacesAndNewlines),
              !context.isEmpty, context != title
        else { return "Step \(index), \(title)" }
        let spoken = context
            .replacingOccurrences(of: ":", with: " ")
            .replacingOccurrences(of: "#", with: " ")
        return "Step \(index), \(title), \(spoken)"
    }

    private var accessibilityValue: String {
        var parts = [expanded ? "Expanded" : "Collapsed"]
        if let status = step.latestStatus { parts.append(status.replacingOccurrences(of: "_", with: " ")) }
        parts.append(tier.label)
        parts.append(digest.summary)
        return parts.joined(separator: ", ")
    }
}

// MARK: - Step detail body (shared by the spine; check rendering reuses CheckRow)

/// The expanded content of a step: what it did, why the tier is what it is, any
/// blocker, and the checks — grouped so a failing check comes first, then
/// passes, then superseded history. Reuses StepCheckDigest for grouping and
/// CheckRow for each row, so the ledger's per-check honesty (source, exit code,
/// redaction) is identical to everywhere else checks are shown.
struct StepDetailBody: View {
    let step: V1Step
    @State private var showAllPassed = false
    @State private var showAllOther = false
    @State private var showHistory = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var digest: StepCheckDigest { StepCheckDigest(checks: step.checks ?? []) }
    private var evidenceExplanation: String? {
        digest.evidenceExplanation(status: step.evidenceStatus, claimedFileCount: step.files?.count ?? 0)
    }

    private var metadata: [String] {
        var parts: [String] = []
        if let status = step.latestStatus { parts.append(status.replacingOccurrences(of: "_", with: " ")) }
        if let ago = agoText(step.updatedAt) { parts.append("updated \(ago)") }
        if let usage = step.usage, let tokens = usage.totalTokens, tokens > 0 {
            parts.append("\(UsageTotals.compact(tokens)) tok")
            parts.append(usage.costText)
        }
        if let models = step.models, !models.isEmpty {
            parts.append(models.compactMap { $0.model ?? "unknown model" }.joined(separator: ", "))
        }
        return parts
    }

    private var current: [StepCheckItem] { digest.current }
    /// Every check carries the same redaction: say it once under the list,
    /// not on every row.
    private var allCommandsRedacted: Bool {
        !digest.all.isEmpty && digest.all.allSatisfy { $0.check.commandRedacted == true }
    }
    /// The step header already counts its checks; a single passed group
    /// under it needs no heading repeating the number.
    private var showsPassedHeading: Bool {
        !attention.isEmpty || !other.isEmpty || !digest.history.isEmpty
    }
    private var attention: [StepCheckItem] { current.filter(\.needsAttention) }
    private var passed: [StepCheckItem] { current.filter { !$0.needsAttention && $0.check.result == "passed" } }
    private var other: [StepCheckItem] { current.filter { !$0.needsAttention && $0.check.result != "passed" } }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            if !metadata.isEmpty {
                Text(metadata.joined(separator: " · "))
                    .workFont(.dataSmall).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let summary = step.summary, !summary.isEmpty {
                Text(summary).workFont(.caption).foregroundStyle(Theme.ink)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
            if let why = step.evidenceGradeReason ?? evidenceExplanation {
                Text(why).workFont(.caption).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let blocker = step.blocker, !blocker.isEmpty {
                blockCard(blocker)
            } else if let next = step.nextStep, !next.isEmpty {
                Label(next, systemImage: "arrow.turn.down.right")
                    .workFont(.caption).foregroundStyle(Theme.ink)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
            checksSection
            if let files = step.files, !files.isEmpty { filesSection(files) }
        }
        .animation(reduceMotion ? nil : Motion.contentUpdate, value: showAllPassed)
        .animation(reduceMotion ? nil : Motion.contentUpdate, value: showAllOther)
        .animation(reduceMotion ? nil : Motion.contentUpdate, value: showHistory)
    }

    private func blockCard(_ blocker: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            CapsLabel(text: "Blocker", tone: Theme.coral)
            Text(blocker).workFont(.body).foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            if let next = step.nextStep, !next.isEmpty {
                Text("next \u{2192} \(next)").workFont(.caption).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
        }
        .padding(11)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.tintCoral, in: RoundedRectangle(cornerRadius: Metrics.radius))
    }

    @ViewBuilder private var checksSection: some View {
        if digest.all.isEmpty {
            Text("No machine checks recorded for this step.")
                .workFont(.caption).foregroundStyle(Theme.muted)
        } else {
            VStack(alignment: .leading, spacing: Space.s) {
                if !attention.isEmpty {
                    groupHeading("Failed / error checks", count: attention.count, tint: Theme.muted)
                    checkList(attention)
                }
                if !passed.isEmpty {
                    if showsPassedHeading {
                        groupHeading("Passed", count: passed.count, tint: Theme.muted)
                    }
                    let shown = showAllPassed ? passed : Array(passed.prefix(StepCheckDigest.currentPreviewLimit))
                    checkList(shown)
                    if passed.count > shown.count || (showAllPassed && passed.count > StepCheckDigest.currentPreviewLimit) {
                        moreButton(count: passed.count - StepCheckDigest.currentPreviewLimit,
                                   expanded: showAllPassed, tint: Theme.accent) { showAllPassed.toggle() }
                    }
                }
                if !other.isEmpty {
                    groupHeading("Other results", count: other.count, tint: Theme.muted)
                    let shown = showAllOther ? other : Array(other.prefix(StepCheckDigest.currentPreviewLimit))
                    checkList(shown)
                    if other.count > shown.count || (showAllOther && other.count > StepCheckDigest.currentPreviewLimit) {
                        moreButton(count: other.count - StepCheckDigest.currentPreviewLimit,
                                   expanded: showAllOther, tint: Theme.accent) { showAllOther.toggle() }
                    }
                }
                if allCommandsRedacted {
                    Text("Command details are redacted for these checks.")
                        .workFont(.caption).foregroundStyle(Theme.muted)
                }
                if !digest.history.isEmpty {
                    Divider().overlay(Theme.hairline)
                    Button { showHistory.toggle() } label: {
                        Label(showHistory ? "Hide historical checks"
                              : "Show \(digest.historyCount) historical check\(digest.historyCount == 1 ? "" : "s")",
                              systemImage: showHistory ? "chevron.up" : "clock.arrow.circlepath")
                            .workFont(.captionSemibold).foregroundStyle(Theme.muted)
                            .frame(minHeight: ButtonFeedback.minimumHitDimension, alignment: .leading)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(SurfaceButtonStyle(focusInset: 2))
                    if showHistory { checkList(digest.history) }
                }
            }
        }
    }

    private func groupHeading(_ text: String, count: Int, tint: Color) -> some View {
        Text("\(text) \u{00B7} \(count)")
            .workFont(.dataSmallSemibold).foregroundStyle(tint)
            .accessibilityHeading(.h4)
    }

    @ViewBuilder private func checkList(_ items: [StepCheckItem]) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(items) { item in
                CheckRow(check: item.check, showsRedaction: !allCommandsRedacted)
                if item.id != items.last?.id {
                    Divider().overlay(Theme.hairline).padding(.leading, 22)
                }
            }
        }
    }

    private func moreButton(count: Int, expanded: Bool, tint: Color, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(expanded ? "Show fewer" : "Show \(count) more",
                  systemImage: expanded ? "chevron.up" : "chevron.down")
                .workFont(.captionSemibold).foregroundStyle(tint)
                .frame(minHeight: ButtonFeedback.minimumHitDimension, alignment: .leading)
                .contentShape(Rectangle())
        }
        .buttonStyle(SurfaceButtonStyle(focusInset: 2))
    }

    private func filesSection(_ files: [String]) -> some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            Text(Fmt.count(files.count, "file"))
                .workFont(.captionSemibold).foregroundStyle(Theme.ink)
                .accessibilityHeading(.h3)
            ForEach(files, id: \.self) { file in
                Text(file).workFont(.dataSmall).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
        }
    }
}
