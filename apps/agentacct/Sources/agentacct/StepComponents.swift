import SwiftUI

// Shared step / check / subagent-row components for the Work surface session
// drill-down (WorkPane.swift). Previously part of the Sessions pane.

/// Tint for a check's independence: agent-reported is muted (the agent's own
/// word), hook-observed is cyan, CI/provider is green.
func checkIndependenceTint(_ sourceType: String?) -> Color {
    switch sourceType {
    case "ci", "external", "provider": return Theme.green
    case "client_hook": return Theme.cyan
    default: return Theme.textMuted
    }
}

// MARK: - One expandable step

struct StepCard: View {
    let step: V1Step
    @State private var expanded = false

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

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(.easeInOut(duration: 0.15)) { expanded.toggle() }
            } label: {
                HStack(spacing: 9) {
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 8, weight: .semibold))
                        .foregroundStyle(Theme.textFaint)
                        .frame(width: 10)
                    StatusDot(color: Theme.statusColor(step.latestStatus))
                    Text(step.title ?? step.sectionId ?? "untitled")
                        .font(Type.body)
                        .foregroundStyle(Theme.text)
                        .lineLimit(expanded ? nil : 1)
                        .fixedSize(horizontal: false, vertical: expanded)
                    Spacer(minLength: 8)
                    if let kind = step.kind, kind != "unknown" {
                        Chip(text: kind, tint: Theme.textMuted)
                    }
                    if let checks = step.checks, !checks.isEmpty {
                        Text("\(checks.count) check\(checks.count == 1 ? "" : "s")")
                            .font(Type.tiny)
                            .foregroundStyle(Theme.textFaint)
                    }
                    // The M2 per-step grade (self-checked / independent / …) is the
                    // primary signal; fall back to the older evidence_status only
                    // if an older daemon did not send a grade.
                    if let grade = step.evidenceGrade {
                        Chip(text: stepGradeLabel(grade), tint: stepGradeTint(grade))
                    } else if let evidence = step.evidenceStatus {
                        Chip(text: evidence, tint: evidenceTint(evidence))
                    }
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

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
                                .font(Type.tiny)
                                .foregroundStyle(Theme.textFaint)
                        }
                        if let usage = step.usage, let tokens = usage.totalTokens, tokens > 0 {
                            Text("\(UsageTotals.compact(Int(tokens))) tok · \(usage.costText)")
                                .font(Type.tiny.monospacedDigit())
                                .foregroundStyle(Theme.textMuted)
                        }
                        if let models = step.models, !models.isEmpty {
                            Text(models.compactMap { $0.model ?? "unknown model" }.joined(separator: " · "))
                                .font(Type.tiny)
                                .foregroundStyle(Theme.purple)
                        }
                    }
                    // "Why this grade" — the daemon-supplied reason for the M2
                    // grade, else the older evidence_status explanation.
                    if let why = step.evidenceGradeReason {
                        Text(why)
                            .font(Type.tiny)
                            .foregroundStyle(stepGradeTint(step.evidenceGrade))
                            .fixedSize(horizontal: false, vertical: true)
                    } else if let why = evidenceExplanation {
                        Text(why)
                            .font(Type.tiny)
                            .foregroundStyle(evidenceTint(step.evidenceStatus))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let summary = step.summary, !summary.isEmpty {
                        Text(summary)
                            .font(Type.small)
                            .foregroundStyle(Theme.textMuted)
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
                            .font(Type.small)
                            .foregroundStyle(Theme.orange)
                            .fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled)
                    }
                    if let next = step.nextStep, !next.isEmpty {
                        Label(next, systemImage: "arrow.turn.down.right")
                            .font(Type.small)
                            .foregroundStyle(Theme.textMuted)
                            .fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled)
                    }
                    if let files = step.files, !files.isEmpty {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("\(files.count) file\(files.count == 1 ? "" : "s")")
                                .font(Type.tiny.weight(.semibold))
                                .foregroundStyle(Theme.textFaint)
                            ForEach(files, id: \.self) { file in
                                Text(file)
                                    .font(Type.tiny.monospaced())
                                    .foregroundStyle(Theme.textFaint)
                                    .fixedSize(horizontal: false, vertical: true)
                                    .textSelection(.enabled)
                            }
                        }
                    }
                }
                .padding(.horizontal, 12)
                .padding(.bottom, 10)
                .padding(.leading, 18)
            }
        }
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 9, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .strokeBorder(Theme.border, lineWidth: 1)
        )
    }
}

/// One machine check: the TUI's ✓/✗/»/• marks with type, summary, exit code.
struct CheckRow: View {
    let check: V1Check

    private var mark: (String, Color) {
        switch check.result {
        case "passed": return ("checkmark", Theme.green)
        case "failed", "error": return ("xmark", Theme.red)
        case "skipped": return ("chevron.right.2", Theme.orange)
        default: return ("circle.fill", Theme.textFaint)
        }
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 7) {
            Image(systemName: mark.0)
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(mark.1)
                .frame(width: 12)
            Text(check.evidenceType ?? "check")
                .font(Type.tiny.weight(.semibold))
                .foregroundStyle(Theme.textMuted)
            Text(check.summary ?? "")
                .font(Type.tiny)
                .foregroundStyle(Theme.textMuted)
                .lineLimit(2)
            if let exit = check.exitCode {
                Text("(exit \(exit))")
                    .font(Type.tiny.monospacedDigit())
                    .foregroundStyle(Theme.textFaint)
            }
            // Who attested this check — an agent-reported check is the agent's
            // own word no matter what its summary text claims.
            Chip(text: check.independence, tint: checkIndependenceTint(check.sourceType))
            if check.supersessionState == "superseded" {
                Chip(text: "superseded", tint: Theme.textFaint)
            }
            Spacer(minLength: 0)
        }
    }
}
