import SwiftUI

// Shared step / check / subagent-row components for the Work surface session
// drill-down (WorkPane.swift). Previously part of the Sessions pane.

extension V1Check {
    /// One normalization boundary keeps classification and copy from
    /// disagreeing when a legacy or malformed payload contains whitespace.
    var normalizedSupersessionState: String? {
        guard let state = supersessionState?.trimmingCharacters(in: .whitespacesAndNewlines),
              !state.isEmpty
        else { return nil }
        return state
    }
}

/// The reducer's check-result tone key (`result_tone`) → glyph and color. The
/// only result mapping Swift keeps: the words come from `result_label`, and a
/// missing or unknown tone is the muted "proved nothing" tone, never a failure.
/// Coral and the cross belong to a recorded failure alone.
enum CheckResultTone: String {
    case pass
    case failure
    case notRun = "not_run"

    init(payload: String?) {
        let key = payload?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        self = CheckResultTone(rawValue: key) ?? .notRun
    }

    var symbol: String {
        switch self {
        case .pass: return "checkmark"
        case .failure: return "xmark"
        case .notRun: return "minus.circle"
        }
    }

    /// A pass wears the tint its caller supplies (the source's evidence tier,
    /// or ink); a failure is coral; a check that proved nothing is muted.
    func tint(pass: Color) -> Color {
        switch self {
        case .pass: return pass
        case .failure: return Theme.coral
        case .notRun: return Theme.muted
        }
    }
}

/// Stable, occurrence-aware identity for a check inside one step. Older or
/// malformed payloads can omit or duplicate event ids; using V1Check.id in a
/// ForEach would generate a fresh UUID for every render in the missing-id case.
struct StepCheckItem: Identifiable {
    let id: String
    let check: V1Check

    var isHistory: Bool { check.normalizedSupersessionState == "superseded" }
    var needsAttention: Bool {
        !isHistory && CheckResultTone(payload: check.resultTone) == .failure
    }
}

/// The check ledger's view-neutral truth boundary. It keeps current evidence,
/// active failures, and superseded history distinct while preserving server
/// order inside each group.
struct StepCheckDigest {
    static let currentPreviewLimit = 8

    let all: [StepCheckItem]

    init(checks: [V1Check]) {
        let bases = checks.map(Self.identityBase)
        let totals = Dictionary(grouping: bases, by: { $0 }).mapValues(\.count)
        var occurrences: [String: Int] = [:]
        all = zip(checks, bases).map { check, base in
            occurrences[base, default: 0] += 1
            let id = totals[base] == 1 ? base : "\(base)#\(occurrences[base]!)"
            return StepCheckItem(id: id, check: check)
        }
    }

    var current: [StepCheckItem] { all.filter { !$0.isHistory } }
    var attention: [StepCheckItem] { current.filter(\.needsAttention) }
    var ordinaryCurrent: [StepCheckItem] { current.filter { !$0.needsAttention } }
    var history: [StepCheckItem] { all.filter(\.isHistory) }

    /// Active failures are always the first group, but even a malformed or
    /// unusually noisy receipt remains bounded until the reviewer asks for it.
    var attentionPreview: [StepCheckItem] {
        Array(attention.prefix(Self.currentPreviewLimit))
    }

    var currentPreview: [StepCheckItem] {
        attentionPreview + ordinaryPreview
    }

    var ordinaryPreview: [StepCheckItem] {
        let remaining = max(0, Self.currentPreviewLimit - attentionPreview.count)
        return Array(ordinaryCurrent.prefix(remaining))
    }

    var hiddenAttentionCount: Int { max(0, attentionCount - attentionPreview.count) }
    var hiddenOrdinaryCount: Int { max(0, ordinaryCurrent.count - ordinaryPreview.count) }
    var currentCount: Int { current.count }
    var attentionCount: Int { attention.count }
    var historyCount: Int { history.count }
    var passedCount: Int {
        current.filter { CheckResultTone(payload: $0.check.resultTone) == .pass }.count
    }

    func evidenceExplanation(status: String?, claimedFileCount: Int) -> String? {
        guard let status else { return nil }
        switch status {
        case "failed":
            if attentionCount == 0 {
                return "failed — receipt reports failure, but no current failing check is visible"
            }
            return "failed — \(attentionCount) failing check\(attentionCount == 1 ? "" : "s") with no later pass superseding \(attentionCount == 1 ? "it" : "them")"
        case "strong":
            return "strong — \(passedCount) passing machine check\(passedCount == 1 ? "" : "s") recorded"
        case "weak":
            if all.isEmpty && claimedFileCount > 0 {
                return "weak — \(claimedFileCount) file\(claimedFileCount == 1 ? "" : "s") claimed, but no machine check proves the work"
            }
            return "weak — checks were recorded but none passed"
        case "none":
            return "none — no machine checks and no file claims were recorded for this step"
        default:
            return nil
        }
    }

    private static func identityBase(_ check: V1Check) -> String {
        if let eventId = nonempty(check.eventId) { return "event:\(eventId)" }
        if let checkIdentity = nonempty(check.checkIdentity) { return "check:\(checkIdentity)" }
        let createdAt = check.createdAt.map { String($0) } ?? "time-unknown"
        return [
            "legacy",
            createdAt,
            check.evidenceType ?? "type-unknown",
            check.result ?? "result-unknown",
            check.sourceType ?? "source-unknown",
            check.summary ?? "summary-missing",
        ].joined(separator: "|")
    }

    private static func nonempty(_ value: String?) -> String? {
        guard let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines),
              !trimmed.isEmpty
        else { return nil }
        return trimmed
    }
}

/// Stable identity for steps at the list boundary. Valid work/section ids stay
/// stable across server reordering; duplicate legacy ids gain only a local
/// occurrence suffix.
struct SessionStepItem: Identifiable {
    let id: String
    let step: V1Step

    static func make(_ steps: [V1Step]) -> [Self] {
        let bases = steps.map(identityBase)
        let totals = Dictionary(grouping: bases, by: { $0 }).mapValues(\.count)
        var occurrences: [String: Int] = [:]
        return zip(steps, bases).map { step, base in
            occurrences[base, default: 0] += 1
            let id = totals[base] == 1 ? base : "\(base)#\(occurrences[base]!)"
            return Self(id: id, step: step)
        }
    }

    private static func identityBase(_ step: V1Step) -> String {
        if let workId = nonempty(step.workId) { return "work:\(workId)" }
        if let sectionId = nonempty(step.sectionId) { return "section:\(sectionId)" }
        let updatedAt = step.updatedAt.map { String($0) } ?? "time-unknown"
        return [
            "legacy",
            updatedAt,
            step.kind ?? "kind-unknown",
            step.title ?? "title-missing",
        ].joined(separator: "|")
    }

    private static func nonempty(_ value: String?) -> String? {
        guard let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines),
              !trimmed.isEmpty
        else { return nil }
        return trimmed
    }
}

extension SessionStepItem {
    /// The deterministic step open-set snapshots use: the first two
    /// check-bearing steps (so their evidence — command + exit code — shows)
    /// plus one un-checked step (so an honest "no passing check" step is visible
    /// too). One definition shared by every surface that renders a step list, so
    /// a change here can't drift the golden references between them.
    static func snapshotOpenedIDs(_ items: [SessionStepItem]) -> Set<String> {
        Set(
            items.filter { !($0.step.checks?.isEmpty ?? true) }
                .prefix(max(0, SnapshotMode.expandedStepsWithChecks)).map(\.id)
            + items.filter { ($0.step.checks?.isEmpty ?? true) }
                .prefix(max(0, SnapshotMode.expandedStepsWithoutChecks)).map(\.id)
        )
    }
}

/// A single check's readable and accessible vocabulary. Unknown wire values
/// remain unknown; they never inherit the trust level of a known source.
struct CheckPresentation {
    let check: V1Check

    /// The reducer's result words, or their named absence.
    var resultLabel: String {
        PayloadAbsence.text(check.resultLabel) ?? PayloadAbsence.checkResult
    }

    var tone: CheckResultTone { CheckResultTone(payload: check.resultTone) }

    var resultSymbol: String { tone.symbol }

    /// A pass is never green by source (C27): green belongs only to the
    /// externally-verified tier and the live StatusDot. The step's tier pip
    /// and badge carry how independent the evidence is.
    var resultTint: Color { tone.tint(pass: Theme.ink) }

    var evidenceType: String {
        let raw = check.evidenceType?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return raw.isEmpty ? "Check" : raw.replacingOccurrences(of: "_", with: " ").capitalized
    }

    var summary: String {
        let raw = check.summary?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return raw.isEmpty ? "No summary recorded" : raw
    }

    /// Who recorded the check, verbatim from the payload (`source_label`);
    /// its absence is named, never mapped from `source_type` in Swift.
    var sourceLabel: String {
        PayloadAbsence.text(check.sourceLabel) ?? PayloadAbsence.source
    }

    var exitLabel: String? { check.exitCode.map { "exit \($0)" } }

    var resolutionScopeLabel: String? {
        switch check.resolutionScope {
        case "full": return "Full resolution"
        case "partial": return "Partial resolution"
        case .some: return "Resolution scope unknown"
        case nil: return nil
        }
    }

    var supersessionLabel: String? {
        switch check.normalizedSupersessionState {
        case "superseded": return "Historical, superseded"
        case "unconfirmed": return "Supersession unconfirmed"
        case .some: return "Supersession state unknown"
        case nil: return nil
        }
    }

    /// The reducer's redaction sentences for withheld artifact fields.
    var artifactRedactionLabel: String? {
        let parts = [
            check.artifactPathRedacted == true
                ? PayloadAbsence.text(check.artifactPathStateText) ?? PayloadAbsence.artifact : nil,
            check.artifactUrlRedacted == true
                ? PayloadAbsence.text(check.artifactUrlStateText) ?? PayloadAbsence.artifact : nil,
        ].compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: " ")
    }

    /// The reducer's sentence about the recorded command (nil when none).
    var commandStateText: String? {
        guard check.commandRedacted == true else { return nil }
        return PayloadAbsence.text(check.commandStateText) ?? PayloadAbsence.command
    }

    /// The reducer's named result/exit-code disagreement (nil when they agree).
    var resultNote: String? { PayloadAbsence.text(check.noteText) }

    var accessibilitySummary: String {
        var parts = [evidenceType, resultLabel, summary]
        if let exitLabel { parts.append(exitLabel) }
        parts.append(sourceLabel)
        if let supersessionLabel { parts.append(supersessionLabel) }
        if let resultNote { parts.append(resultNote) }
        if let resolutionScopeLabel { parts.append(resolutionScopeLabel) }
        if let resolution = check.resolutionSummary?.trimmingCharacters(in: .whitespacesAndNewlines),
           !resolution.isEmpty {
            parts.append(resolution)
        }
        if let commandStateText { parts.append(commandStateText) }
        if let artifactRedactionLabel { parts.append(artifactRedactionLabel) }
        let files = (check.files ?? []).filter { !$0.isEmpty }
        if !files.isEmpty { parts.append("Check files: \(files.joined(separator: ", "))") }
        if let artifact = [check.artifactRef, check.artifactPath, check.artifactUrl]
            .compactMap({ $0?.trimmingCharacters(in: .whitespacesAndNewlines) })
            .first(where: { !$0.isEmpty }) {
            parts.append("Artifact: \(artifact)")
        }
        return parts.map { part in
            let trimmed = part.trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.hasSuffix(".") ? trimmed : "\(trimmed)."
        }.joined(separator: " ")
    }
}

// MARK: - One expandable step

struct StepCard: View {
    let step: V1Step
    let accessibilityContext: String?
    /// The recorded next step the PAGE already prints, when there is one. A step
    /// whose own next step is the same text does not print it a second time.
    let pageNextStep: String?
    @State private var expanded: Bool
    @State private var showAllAttention: Bool
    @State private var showAllCurrentChecks: Bool
    @State private var showHistory: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    init(
        step: V1Step,
        initiallyExpanded: Bool = false,
        initiallyShowAllAttention: Bool = false,
        initiallyShowAllCurrentChecks: Bool = false,
        initiallyShowHistory: Bool = false,
        accessibilityContext: String? = nil,
        pageNextStep: String? = nil
    ) {
        self.step = step
        self.accessibilityContext = accessibilityContext
        self.pageNextStep = pageNextStep
        _expanded = State(initialValue: initiallyExpanded)
        _showAllAttention = State(initialValue: initiallyShowAllAttention)
        _showAllCurrentChecks = State(initialValue: initiallyShowAllCurrentChecks)
        _showHistory = State(initialValue: initiallyShowHistory)
    }

    /// WHY this confidence level — mirrors the daemon's evidence_status
    /// derivation (failed: any live failing check; strong: any pass; weak:
    /// claims without a pass; none: nothing recorded) using the same inputs
    /// it used, so the label never needs to be taken on faith.
    private var evidenceExplanation: String? {
        StepCheckDigest(checks: step.checks ?? []).evidenceExplanation(
            status: step.evidenceStatus,
            claimedFileCount: step.files?.count ?? 0
        )
    }

    /// The collapsed row's tier pip (falls back to a muted hollow pip when an
    /// older daemon sent no grade).
    private var stepPip: some View {
        EvidencePip(grade: step.evidenceGrade)
    }

    private var title: String {
        for candidate in [step.title, step.sectionId] {
            if let value = candidate?.trimmingCharacters(in: .whitespacesAndNewlines),
               !value.isEmpty {
                return value
            }
        }
        return "Untitled step"
    }
    private var checkDigest: StepCheckDigest { StepCheckDigest(checks: step.checks ?? []) }

    private var tierLabel: String {
        if let label = PayloadAbsence.text(step.evidenceGradeLabel) { return label }
        if let grade = step.evidenceGrade {
            return EvidenceTierStyle.forGrade(grade).label
        }
        return step.evidenceStatus?.replacingOccurrences(of: "_", with: " ") ?? "evidence unknown"
    }

    private var accessibilityValue: String {
        var parts = [expanded ? "Expanded" : "Collapsed"]
        if let status = step.latestStatus {
            parts.append(status.replacingOccurrences(of: "_", with: " "))
        }
        parts.append(tierLabel)
        parts.append(step.checkTallyDisplay)
        return parts.joined(separator: ", ")
    }

    var accessibilityLabelText: String {
        let candidate = accessibilityContext ?? step.sectionId ?? step.workId
        guard let context = candidate?.trimmingCharacters(in: .whitespacesAndNewlines),
              !context.isEmpty,
              context != title
        else { return title }
        let spokenContext = context
            .replacingOccurrences(of: ":", with: " ")
            .replacingOccurrences(of: "#", with: " ")
        return "\(title), \(spokenContext)"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { expanded.toggle() } label: { header }
            .buttonStyle(SurfaceButtonStyle(focusInset: 2))
            .keyboardStop { expanded.toggle() }
            // NOT `.accessibilityElement(children: .ignore)`: a Button already
            // speaks as ONE element, and that modifier REPLACES it — the
            // control loses its button role and its press action with it
            // (K118). The label below is simply what the button says.
            .accessibilityLabel(accessibilityLabelText)
            .accessibilityValue(accessibilityValue)
            .accessibilityHint(expanded ? "Hides step details" : "Shows step details")

            if expanded {
                VStack(alignment: .leading, spacing: 8) {
                    // The agent's outcome summary is the only prose a reader
                    // sees, so it leads the details in body ink (C41).
                    if let summary = PayloadAbsence.text(step.summary) {
                        Text(summary)
                            .workFont(.body)
                            .foregroundStyle(Theme.ink)
                            .lineLimit(nil)
                            .fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled)
                    }
                    if let blocker = step.blocker, !blocker.isEmpty {
                        Label(blocker, systemImage: "hand.raised.fill")
                            .workFont(.body)
                            .foregroundStyle(Theme.amber)
                            .fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled)
                    }
                    // Suppressed when the page above already prints this exact
                    // text: merging two statements of one fact is right, and the
                    // Task's own next step is the one a reviewer acts on.
                    if let next = PayloadAbsence.text(step.nextStep),
                       !restatesPayloadText(next, pageNextStep) {
                        NextStepRow(text: next, compact: true)
                    }
                    checksSection
                    if let files = step.files, !files.isEmpty {
                        VStack(alignment: .leading, spacing: Space.xs) {
                            Text("Files · \(files.count)")
                                .workFont(.captionSemibold)
                                .foregroundStyle(Theme.ink)
                                .accessibilityHeading(.h3)
                            ForEach(files, id: \.self) { file in
                                Text(file)
                                    .workFont(.dataSmall)
                                    .foregroundStyle(Theme.muted)
                                    .fixedSize(horizontal: false, vertical: true)
                                    .textSelection(.enabled)
                            }
                        }
                    }
                    if !detailMetadata.isEmpty {
                        Text(detailMetadata.joined(separator: " · "))
                            .workFont(.dataSmall)
                            .foregroundStyle(Theme.muted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let why = PayloadAbsence.text(step.evidenceGradeReason) ?? evidenceExplanation {
                        Text(why)
                            .workFont(.caption)
                            .foregroundStyle(Theme.muted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(.horizontal, 12)
                .padding(.bottom, 10)
                .padding(.leading, 18)
                .transition(.opacity)
            }
        }
        .animation(reduceMotion ? nil : Motion.contentUpdate, value: expanded)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
        .overlay(
            RoundedRectangle(cornerRadius: Metrics.radius)
                .strokeBorder(Theme.cardLine, lineWidth: Metrics.borderW)
        )
    }

    private var header: some View {
        ViewThatFits(in: .horizontal) {
            regularHeader
            stackedHeader
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .contentShape(Rectangle())
    }

    private var regularHeader: some View {
        HStack(spacing: 9) {
            disclosureGlyph
            stepPip
            if step.latestStatus == "blocked" || step.latestStatus == "failed" {
                Chip(text: step.latestStatus ?? "", tint: Theme.coral)
            }
            Text(title)
                .workFont(.body)
                .foregroundStyle(Theme.ink)
                .lineLimit(expanded ? nil : 2)
                .fixedSize(horizontal: false, vertical: expanded)
                .layoutPriority(1)
            Spacer(minLength: 8)
            if let kind = step.kind, kind != "unknown" {
                Chip(text: kind, tint: Theme.muted)
            }
            Text(step.checkTallyDisplay)
                .workFont(.dataSmall)
                .foregroundStyle(Theme.muted)
            tierBadge
        }
    }

    private var stackedHeader: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top, spacing: 9) {
                disclosureGlyph
                stepPip.padding(.top, 4)
                Text(title)
                    .workFont(.body)
                    .foregroundStyle(Theme.ink)
                    .lineLimit(expanded ? nil : 2)
                    .fixedSize(horizontal: false, vertical: expanded)
                    .layoutPriority(1)
            }
            VStack(alignment: .leading, spacing: Space.xs) {
                // Badges never wrap mid-word; the row does (C32). When the
                // chips and the tier badge cannot share one line, the badge
                // drops below the chips, and the chips themselves flow.
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 8) {
                        statusAndKindChips
                        Spacer(minLength: 4)
                        tierBadge
                    }
                    VStack(alignment: .leading, spacing: Space.xs) {
                        if hasStatusOrKindChip {
                            WrappingRowLayout(horizontalSpacing: 8, verticalSpacing: Space.xs) {
                                statusAndKindChips
                            }
                        }
                        tierBadge
                    }
                }
                Text(step.checkTallyDisplay)
                    .workFont(.dataSmall)
                    .foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.leading, 28)
        }
    }

    private var hasStatusOrKindChip: Bool {
        step.latestStatus == "blocked" || step.latestStatus == "failed"
            || (step.kind != nil && step.kind != "unknown")
    }

    @ViewBuilder
    private var statusAndKindChips: some View {
        if step.latestStatus == "blocked" || step.latestStatus == "failed" {
            Chip(text: step.latestStatus ?? "", tint: Theme.coral)
        }
        if let kind = step.kind, kind != "unknown" {
            Chip(text: kind, tint: Theme.muted)
        }
    }

    private var disclosureGlyph: some View {
        Image(systemName: expanded ? "chevron.down" : "chevron.forward")
            .workFont(.icon)
            .foregroundStyle(Theme.muted)
            .workScaledFrame(width: 12, height: 16, relativeTo: .caption)
    }

    @ViewBuilder
    private var tierBadge: some View {
        if let grade = step.evidenceGrade {
            TierBadge(grade: grade, text: PayloadAbsence.text(step.evidenceGradeLabel))
        } else if let evidence = step.evidenceStatus {
            // Legacy evidence words carry no tier: failure stays coral, every
            // other word takes the ungraded tier style — never green (C27).
            Chip(
                text: evidence.replacingOccurrences(of: "_", with: " "),
                tint: evidence == "failed" ? Theme.coral : EvidenceTierStyle.forGrade(nil).tint
            )
        }
    }

    private var detailMetadata: [String] {
        var parts: [String] = []
        if let status = step.latestStatus {
            parts.append(status.replacingOccurrences(of: "_", with: " "))
        }
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

    private var checksSection: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            ViewThatFits(in: .horizontal) {
                HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                    checksHeading
                    Text(step.checkTallyDisplay)
                        .workFont(.dataSmall)
                        .foregroundStyle(Theme.muted)
                    Spacer(minLength: 0)
                }
                VStack(alignment: .leading, spacing: Space.xs) {
                    checksHeading
                    Text(step.checkTallyDisplay)
                        .workFont(.dataSmall)
                        .foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            if checkDigest.all.isEmpty {
                Text("No machine checks recorded for this step.")
                    .workFont(.caption)
                    .foregroundStyle(Theme.muted)
            } else {
                if !checkDigest.attention.isEmpty {
                    checkGroupHeading("Needs attention", count: checkDigest.attention.count, tint: Theme.coral)
                    CheckRows(
                        items: showAllAttention
                            ? checkDigest.attention
                            : checkDigest.attentionPreview
                    )
                    if checkDigest.hiddenAttentionCount > 0 {
                        Button { showAllAttention.toggle() } label: {
                            disclosureLabel(
                                showAllAttention
                                    ? "Show fewer issues"
                                    : "Show \(checkDigest.hiddenAttentionCount) more issues",
                                systemImage: showAllAttention ? "chevron.up" : "chevron.down",
                                tint: Theme.coral
                            )
                        }
                        .buttonStyle(SurfaceButtonStyle(focusInset: 2))
                        .keyboardStop { showAllAttention.toggle() }
                        .accessibilityValue(showAllAttention ? "Expanded" : "Collapsed")
                    }
                }

                if !checkDigest.ordinaryCurrent.isEmpty {
                    checkGroupHeading(
                        checkDigest.attention.isEmpty ? "Current checks" : "Other current checks",
                        count: checkDigest.ordinaryCurrent.count,
                        tint: Theme.muted
                    )
                    let visibleOrdinary = showAllCurrentChecks
                        ? checkDigest.ordinaryCurrent
                        : checkDigest.ordinaryPreview
                    if !visibleOrdinary.isEmpty {
                        CheckRows(items: visibleOrdinary)
                    }
                    if checkDigest.hiddenOrdinaryCount > 0 {
                        Button {
                            showAllCurrentChecks.toggle()
                        } label: {
                            disclosureLabel(
                                showAllCurrentChecks
                                    ? "Show fewer current checks"
                                    : "Show \(checkDigest.hiddenOrdinaryCount) more current checks",
                                systemImage: showAllCurrentChecks ? "chevron.up" : "chevron.down",
                                tint: Theme.accent
                            )
                        }
                        .buttonStyle(SurfaceButtonStyle(focusInset: 2))
                        .keyboardStop { showAllCurrentChecks.toggle() }
                        .accessibilityValue(showAllCurrentChecks ? "Expanded" : "Collapsed")
                    }
                }

                if !checkDigest.history.isEmpty {
                    Divider().overlay(Theme.hairline)
                    Button { showHistory.toggle() } label: {
                        disclosureLabel(
                            showHistory
                                ? "Hide historical checks"
                                : "Show \(checkDigest.historyCount) historical check\(checkDigest.historyCount == 1 ? "" : "s")",
                            systemImage: showHistory ? "chevron.up" : "clock.arrow.circlepath",
                            tint: Theme.muted
                        )
                    }
                    .buttonStyle(SurfaceButtonStyle(focusInset: 2))
                    .keyboardStop { showHistory.toggle() }
                    .accessibilityValue(showHistory ? "Expanded" : "Collapsed")

                    if showHistory {
                        CheckRows(items: checkDigest.history)
                            .transition(.opacity)
                    }
                }
            }
        }
        .accessibilityElement(children: .contain)
        .animation(reduceMotion ? nil : Motion.contentUpdate, value: showHistory)
        .animation(reduceMotion ? nil : Motion.contentUpdate, value: showAllAttention)
        .animation(reduceMotion ? nil : Motion.contentUpdate, value: showAllCurrentChecks)
    }

    private func checkGroupHeading(_ text: String, count: Int, tint: Color) -> some View {
        Text("\(text) · \(count)")
            .workFont(.dataSmallSemibold)
            .foregroundStyle(tint)
            .accessibilityHeading(.h4)
    }

    private var checksHeading: some View {
        Text("Checks")
            .workFont(.captionSemibold)
            .foregroundStyle(Theme.ink)
            .accessibilityHeading(.h3)
    }

    private func disclosureLabel(_ text: String, systemImage: String, tint: Color) -> some View {
        Label(text, systemImage: systemImage)
            .workFont(.captionSemibold)
            .foregroundStyle(tint)
            .workScaledMinFrame(height: ButtonFeedback.minimumHitDimension, alignment: .leading)
            .contentShape(Rectangle())
    }
}

/// A readable check ledger keeps the result and full summary primary, with
/// provenance and machine metadata on a wrapping second line.
struct CheckRow: View {
    let check: V1Check

    private var presentation: CheckPresentation { CheckPresentation(check: check) }

    private var metadata: [String] {
        var parts: [String] = []
        if let exitLabel = presentation.exitLabel { parts.append(exitLabel) }
        parts.append(presentation.sourceLabel)
        if let ago = agoText(check.createdAt) { parts.append(ago) }
        if check.normalizedSupersessionState == "unconfirmed" {
            parts.append("supersession unconfirmed")
        } else if presentation.supersessionLabel == "Supersession state unknown" {
            parts.append("supersession state unknown")
        }
        // `command_state_text` is NOT here. It is identical on every check of a
        // record — four prints under four checks on the flagship — so the record
        // page states it once, in the Evidence section's disclosure. The spoken
        // summary still carries it per row, where there is no shared line to
        // hoist it to.
        return parts
    }

    private var artifactReference: String? {
        [check.artifactRef, check.artifactPath, check.artifactUrl]
            .compactMap { $0?.trimmingCharacters(in: .whitespacesAndNewlines) }
            .first { !$0.isEmpty }
    }

    private var checkFiles: [String] {
        (check.files ?? []).filter { !$0.isEmpty }
    }

    private var resultLabelText: some View {
        Text(presentation.resultLabel)
            .workFont(.captionSemibold)
            .foregroundStyle(presentation.resultTint)
            .lineLimit(1)
    }

    private var evidenceTypeText: some View {
        Text(presentation.evidenceType)
            .workFont(.dataSmallSemibold)
            .foregroundStyle(Theme.muted)
            .lineLimit(1)
    }

    private var hasRowChips: Bool {
        check.normalizedSupersessionState == "superseded"
    }

    @ViewBuilder
    private var rowChips: some View {
        if check.normalizedSupersessionState == "superseded" {
            Chip(text: "superseded", tint: Theme.muted)
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: Space.s) {
            Image(systemName: presentation.resultSymbol)
                .workFont(.icon)
                .foregroundStyle(presentation.resultTint)
                .workScaledFrame(width: 14, height: 18, relativeTo: .caption)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: Space.xs) {
                // The result line reflows instead of breaking a word (C32).
                ViewThatFits(in: .horizontal) {
                    HStack(alignment: .firstTextBaseline, spacing: 7) {
                        resultLabelText
                        evidenceTypeText
                        rowChips
                        Spacer(minLength: 0)
                    }
                    VStack(alignment: .leading, spacing: Space.xs) {
                        HStack(alignment: .firstTextBaseline, spacing: 7) {
                            resultLabelText
                            evidenceTypeText
                        }
                        if hasRowChips {
                            WrappingRowLayout(horizontalSpacing: 7, verticalSpacing: Space.xs, alignment: .top) {
                                rowChips
                            }
                        }
                    }
                }
                Text(presentation.summary)
                    .workFont(.body)
                    .foregroundStyle(Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
                Text(metadata.joined(separator: " · "))
                    .workFont(.dataSmall)
                    .foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
                if let note = presentation.resultNote {
                    // The reducer's named result/exit-code disagreement.
                    Text(verbatim: note)
                        .workFont(.caption)
                        .foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let resolution = check.resolutionSummary, !resolution.isEmpty {
                    Label(
                        [presentation.resolutionScopeLabel, resolution]
                            .compactMap { $0 }
                            .joined(separator: ": "),
                        systemImage: "arrow.turn.down.right"
                    )
                        .workFont(.caption)
                        .foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                } else if let resolutionScopeLabel = presentation.resolutionScopeLabel {
                    Label(resolutionScopeLabel, systemImage: "arrow.turn.down.right")
                        .workFont(.caption)
                        .foregroundStyle(Theme.muted)
                }
                if let artifactReference {
                    Label(artifactReference, systemImage: "paperclip")
                        .workFont(.dataSmall)
                        .foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
                if let artifactRedactionLabel = presentation.artifactRedactionLabel {
                    Label(artifactRedactionLabel, systemImage: "eye.slash")
                        .workFont(.dataSmall)
                        .foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !checkFiles.isEmpty {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Check files · \(checkFiles.count)")
                            .workFont(.dataSmallSemibold)
                            .foregroundStyle(Theme.muted)
                        ForEach(Array(checkFiles.enumerated()), id: \.offset) { _, file in
                            Text(file)
                                .workFont(.dataSmall)
                                .foregroundStyle(Theme.muted)
                                .fixedSize(horizontal: false, vertical: true)
                                .textSelection(.enabled)
                        }
                    }
                }
            }
        }
        .padding(.vertical, Space.s)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(presentation.accessibilitySummary)
    }
}

private struct CheckRows: View {
    let items: [StepCheckItem]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(items) { item in
                CheckRow(check: item.check)
                if item.id != items.last?.id {
                    Divider()
                        .overlay(Theme.hairline)
                        .padding(.leading, 22)
                }
            }
        }
    }
}
