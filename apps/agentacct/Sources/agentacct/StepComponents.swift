import SwiftUI

// Shared step / check / subagent-row components for the Work surface session
// drill-down (WorkPane.swift). Previously part of the Sessions pane.

/// Tint for a check's independence: agent-reported is muted (the agent's own
/// word), hook-observed is ink (machine-observed locally), CI/provider is
/// green (independent evidence — the only tier that may wear green).
func checkIndependenceTint(_ sourceType: String?) -> Color {
    switch sourceType {
    case "ci", "external", "provider": return Theme.green
    case "client_hook": return Theme.ink
    default: return Theme.muted
    }
}

// MARK: - One expandable step

struct StepCard: View {
    let step: V1Step
    @State private var expanded: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    init(step: V1Step, initiallyExpanded: Bool = false) {
        self.step = step
        _expanded = State(initialValue: initiallyExpanded)
    }

    /// WHY this confidence level — mirrors the daemon's evidence_status
    /// derivation (failed: any live failing check; strong: any pass; weak:
    /// claims without a pass; none: nothing recorded) using the same inputs
    /// it used, so the label never needs to be taken on faith.
    private var evidenceExplanation: String? {
        guard let status = step.evidenceStatus else { return nil }
        let checks = step.checks ?? []
        let live = checks.filter { $0.supersessionState != "superseded" }
        let passed = live.filter { $0.result == "passed" }.count
        let failed = live.filter { $0.result == "failed" || $0.result == "error" }.count
        let files = step.files?.count ?? 0
        switch status {
        case "failed":
            return "failed — \(failed) failing check\(failed == 1 ? "" : "s") with no later pass superseding \(failed == 1 ? "it" : "them")"
        case "strong":
            return "strong — \(passed) passing machine check\(passed == 1 ? "" : "s") recorded"
        case "weak":
            if checks.isEmpty && files > 0 {
                return "weak — \(files) file\(files == 1 ? "" : "s") claimed, but no machine check proves the work"
            }
            return "weak — checks were recorded but none passed"
        case "none":
            return "none — no machine checks and no file claims were recorded for this step"
        default:
            return nil
        }
    }

    /// The collapsed row's tier pip (falls back to a muted hollow pip when an
    /// older daemon sent no grade).
    private var stepPip: some View {
        let style = EvidenceTierStyle.forGrade(step.evidenceGrade)
        return EvidencePip(shape: style.pip, tint: style.tint)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                expanded.toggle()
            } label: {
                HStack(spacing: 9) {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 8, weight: .semibold))
                        .foregroundStyle(Theme.muted)
                        .frame(width: 10)
                        .rotationEffect(.degrees(expanded ? 90 : 0))
                    // The leading glyph is the step's evidence-tier PIP (shape
                    // carries the tier); a filled lifecycle dot here read as the
                    // independently-checked pip on claimed steps.
                    stepPip
                    if step.latestStatus == "blocked" || step.latestStatus == "failed" {
                        Chip(text: step.latestStatus ?? "", tint: Theme.coral)
                    }
                    Text(step.title ?? step.sectionId ?? "untitled")
                        .font(Type.body)
                        .foregroundStyle(Theme.ink)
                        .lineLimit(expanded ? nil : 1)
                        .fixedSize(horizontal: false, vertical: expanded)
                    Spacer(minLength: 8)
                    if let kind = step.kind, kind != "unknown" {
                        Chip(text: kind, tint: Theme.muted)
                    }
                    if let checks = step.checks, !checks.isEmpty {
                        Text("\(checks.count) check\(checks.count == 1 ? "" : "s")")
                            .font(Type.dataSmall)
                            .foregroundStyle(Theme.muted)
                    }
                    // The M2 per-step grade (self-checked / independent / …) is the
                    // primary signal; fall back to the older evidence_status only
                    // if an older daemon did not send a grade.
                    if let grade = step.evidenceGrade {
                        TierBadge(grade: grade)
                    } else if let evidence = step.evidenceStatus {
                        Chip(text: evidence, tint: evidenceTint(evidence))
                    }
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
                .contentShape(Rectangle())
            }
            .buttonStyle(SurfaceButtonStyle(focusInset: 2))

            if expanded {
                // The expanded step is the DETAILED view: nothing here is
                // truncated — the user opened it to see everything.
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 8) {
                        if let status = step.latestStatus {
                            Chip(text: status.replacingOccurrences(of: "_", with: " "),
                                 tint: Theme.statusColor(step.latestStatus))
                        }
                        if let ago = agoText(step.updatedAt) {
                            Text("updated \(ago)")
                                .font(Type.dataSmall)
                                .foregroundStyle(Theme.muted)
                        }
                        if let usage = step.usage, let tokens = usage.totalTokens, tokens > 0 {
                            Text("\(UsageTotals.compact(tokens)) tok · \(usage.costText)")
                                .font(Type.dataSmall)
                                .foregroundStyle(Theme.muted)
                        }
                        if let models = step.models, !models.isEmpty {
                            Text(models.compactMap { $0.model ?? "unknown model" }.joined(separator: " · "))
                                .font(Type.dataSmall)
                                .foregroundStyle(Theme.muted)
                        }
                    }
                    // "Why this grade" — the daemon-supplied reason for the M2
                    // grade, else the older evidence_status explanation.
                    if let why = step.evidenceGradeReason {
                        // Muted prose: the TierBadge already carries the tier
                        // color; accent-tinted prose here read as a link.
                        Text(why)
                            .font(Type.caption)
                            .foregroundStyle(Theme.muted)
                            .fixedSize(horizontal: false, vertical: true)
                    } else if let why = evidenceExplanation {
                        Text(why)
                            .font(Type.caption)
                            .foregroundStyle(Theme.muted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let summary = step.summary, !summary.isEmpty {
                        Text(summary)
                            .font(Type.caption)
                            .foregroundStyle(Theme.muted)
                            .fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled)
                    }
                    if let checks = step.checks, !checks.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            ForEach(checks) { check in
                                CheckRow(check: check)
                            }
                        }
                    }
                    if let blocker = step.blocker, !blocker.isEmpty {
                        Label(blocker, systemImage: "hand.raised.fill")
                            .font(Type.caption)
                            .foregroundStyle(Theme.amber)
                            .fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled)
                    }
                    if let next = step.nextStep, !next.isEmpty {
                        Label(next, systemImage: "arrow.turn.down.right")
                            .font(Type.caption)
                            .foregroundStyle(Theme.muted)
                            .fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled)
                    }
                    if let files = step.files, !files.isEmpty {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("\(files.count) file\(files.count == 1 ? "" : "s")")
                                .font(Type.dataSmallSemibold)
                                .foregroundStyle(Theme.muted)
                            ForEach(files, id: \.self) { file in
                                Text(file)
                                    .font(Type.dataSmall)
                                    .foregroundStyle(Theme.muted)
                                    .fixedSize(horizontal: false, vertical: true)
                                    .textSelection(.enabled)
                            }
                        }
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
}

/// One machine check: the TUI's ✓/✗/»/• marks with type, summary, exit code.
struct CheckRow: View {
    let check: V1Check

    /// Honesty rule for the pass mark: a green checkmark is reserved for
    /// machine-observed results (CI/external/provider, or a hook-captured exit
    /// code). An agent-REPORTED "passed" is the agent's own word, so its mark
    /// stays ink — never green.
    private var mark: (String, Color) {
        switch check.result {
        case "passed":
            let machineObserved = ["ci", "external", "provider", "client_hook"]
                .contains(check.sourceType ?? "")
            return ("checkmark", machineObserved ? Theme.green : Theme.ink)
        case "failed", "error": return ("xmark", Theme.coral)
        case "skipped": return ("chevron.right.2", Theme.amber)
        default: return ("circle.fill", Theme.muted)
        }
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 7) {
            Image(systemName: mark.0)
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(mark.1)
                .frame(width: 12)
            Text(check.evidenceType ?? "check")
                .font(Type.captionSemibold)
                .foregroundStyle(Theme.muted)
            Text(check.summary ?? "")
                .font(Type.caption)
                .foregroundStyle(Theme.muted)
                .lineLimit(2)
            if let exit = check.exitCode {
                Text("(exit \(exit))")
                    .font(Type.dataSmall)
                    .foregroundStyle(Theme.muted)
            }
            // Who attested this check — an agent-reported check is the agent's
            // own word no matter what its summary text claims.
            Chip(text: check.independence, tint: checkIndependenceTint(check.sourceType))
            if check.supersessionState == "superseded" {
                Chip(text: "superseded", tint: Theme.muted)
            }
            Spacer(minLength: 0)
        }
    }
}
