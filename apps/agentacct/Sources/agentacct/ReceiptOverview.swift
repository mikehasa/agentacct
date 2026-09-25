import SwiftUI

/// Receipt-wide facts must not depend on the separately loaded primary
/// session. Coverage, check runs, and cost retain their own reporting scope.
struct ReceiptOverviewPresentation {
    let decision: WorkReceiptDecisionPresentation
    let coverage: ReceiptCoveragePresentation
    let checks: ReceiptCheckRunsPresentation
    let costValue: String
    let costQualifier: String
    let sessionsValue: String
    let sessionsQualifier: String
    let evidenceNote: String?

    init(receipt: Receipt) {
        decision = WorkReceiptDecisionPresentation(receipt: receipt)
        coverage = ReceiptCoveragePresentation(evidence: receipt.axes.evidenceStrength)
        let evidence = receipt.dimensions.evidence
        checks = ReceiptCheckRunsPresentation(
            total: evidence.checksTotal, passed: evidence.checksPassed, failed: evidence.checksFailed
        )
        evidenceNote = receipt.axes.evidenceStrength.ledger

        let cost = receipt.dimensions.cost
        if let amount = cost.estimatedCostUsd {
            costValue = receiptCostDisplay(amount, complete: cost.costComplete, confidence: cost.costConfidence)
            let scope: String
            switch cost.costComplete {
            case true: scope = "complete coverage"
            case false: scope = "known subtotal · incomplete coverage"
            case nil: scope = "coverage not reported"
            }
            costQualifier = "\(costBasisLabel(cost.costBasis)) · \(scope)"
        } else {
            costValue = "Not reported"
            costQualifier = "cost unavailable for this task"
        }

        if let count = receipt.dimensions.task.boundary?.sessionCount {
            sessionsValue = count < 0 ? "Inconsistent count" : "\(count)"
            sessionsQualifier = count < 0 ? "\(count) sessions reported" : "sessions in this task"
        } else if let groups = receipt.sessions {
            let listed = Set(groups.flatMap(\.members).map(\.id)).count
            sessionsValue = "\(listed)"
            sessionsQualifier = "listed sessions · total not reported"
        } else {
            sessionsValue = "Not reported"
            sessionsQualifier = "session scope unavailable"
        }
    }
}

/// The agent's own account, labelled as such. Every line is agent-reported
/// prose; the counted metrics below it are the app's.
struct ReceiptAgentReportPresentation {
    let goalLabel: String?
    let goal: String?
    let progressLabel: String?
    let progress: String?
    let progressFull: String?
    let nextStep: String?
    let caption: String

    init?(report: ReceiptAgentReport?) {
        guard let report, report.goal != nil || report.progress != nil else { return nil }
        if let goal = report.goal, !goal.text.isEmpty {
            // A step title is how the work began, not necessarily what it is for.
            goalLabel = goal.source == "goal" ? "Goal" : "Started with"
            self.goal = goal.text
        } else {
            goalLabel = nil
            goal = nil
        }
        var captionParts = ["Agent reported"]
        if let progress = report.progress, !progress.text.isEmpty {
            let lead = progress.lead.flatMap { $0.isEmpty ? nil : $0 } ?? progress.text
            switch progress.source {
            case "progress": progressLabel = "Progress"
            case "blocker": progressLabel = "Blocked on"
            default: progressLabel = "Latest step"
            }
            self.progress = lead
            progressFull = lead == progress.text ? nil : progress.text
            if let ago = agoText(progress.writtenAt) { captionParts.append("written \(ago)") }
            if progress.source != "progress", let title = progress.stepTitle, !title.isEmpty {
                // The step title only locates the note; the Steps list has it whole.
                let shown = title.count > 60 ? String(title.prefix(59)).trimmingCharacters(in: .whitespaces) + "\u{2026}" : title
                captionParts.append("from the closing note of \u{201C}\(shown)\u{201D}")
            }
        } else {
            progressLabel = nil
            progress = nil
            progressFull = nil
        }
        nextStep = report.nextStep.flatMap { $0.isEmpty ? nil : $0 }
        if report.activityAfterReport == true { captionParts.append("work continued after this was written") }
        caption = captionParts.joined(separator: " · ")
    }

    /// Whether the card still needs the decision sentence above this account.
    /// "Still in progress" only restates the status badge, and a blocked Task's
    /// decision sentence is the agent's own blocker, which the account already
    /// shows under "Blocked on"; any other decision sentence says something the
    /// account does not, so it stays.
    static func showsDecisionStatement(decisionKey: String?, statement: String?, report: ReceiptAgentReportPresentation?) -> Bool {
        guard let report else { return true }
        if decisionKey == "in_progress" { return false }
        let normalize = { (text: String?) in
            (text ?? "").split(whereSeparator: \.isWhitespace).joined(separator: " ")
                .trimmingCharacters(in: CharacterSet(charactersIn: ".")).lowercased()
        }
        let decision = normalize(statement)
        return decision.isEmpty || decision != normalize(report.progressFull ?? report.progress)
    }

    var accessibilityLabel: String {
        [goalLabel.flatMap { label in goal.map { "\(label): \($0)" } },
         progressLabel.flatMap { label in progress.map { "\(label): \($0)" } },
         nextStep.map { "Next: \($0)" },
         caption].compactMap { $0 }.joined(separator: ". ")
    }
}

struct ReceiptOverview: View {
    let receipt: Receipt
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        let presentation = ReceiptOverviewPresentation(receipt: receipt)
        Card(padding: Space.l) {
            VStack(alignment: .leading, spacing: Space.m) {
                HStack(alignment: .firstTextBaseline) {
                    Text("Recorded outcome").workFont(.rowLabel)
                        .foregroundStyle(Theme.ink)
                        .accessibilityAddTraits(.isHeader)
                    Spacer(minLength: Space.s)
                    Text("Whole task").workFont(.caption).foregroundStyle(Theme.muted)
                }
                let report = ReceiptAgentReportPresentation(report: receipt.dimensions.outcome.agentReport)
                if ReceiptAgentReportPresentation.showsDecisionStatement(
                    decisionKey: receipt.axes.decisionStatus.key,
                    statement: receipt.axes.decisionStatus.statement,
                    report: report
                ) {
                    Text(verbatim: presentation.decision.explanation)
                        .workFont(.body).foregroundStyle(Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
                if let report {
                    agentReport(report)
                }

                Rectangle().fill(Theme.hairline).frame(height: 1)
                if dynamicTypeSize.isAccessibilitySize {
                    metrics(presentation, columns: 1)
                } else {
                    ViewThatFits(in: .horizontal) {
                        metrics(presentation, columns: 4).frame(minWidth: 920)
                        metrics(presentation, columns: 2)
                    }
                }
                if let note = presentation.evidenceNote {
                    Text(note).workFont(.caption).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .accessibilityIdentifier("work.receipt.overview")
    }

    private func agentReport(_ report: ReceiptAgentReportPresentation) -> some View {
        VStack(alignment: .leading, spacing: Space.s) {
            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: Space.m, verticalSpacing: Space.s) {
                if let label = report.goalLabel, let goal = report.goal {
                    reportRow(label, goal)
                }
                if let label = report.progressLabel, let progress = report.progress {
                    reportRow(label, progress, fullText: report.progressFull)
                }
                if let next = report.nextStep {
                    reportRow("Next", next)
                }
            }
            Text(report.caption).workFont(.caption).foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(report.accessibilityLabel)
        .accessibilityIdentifier("work.receipt.agentReport")
    }

    private func reportRow(_ label: String, _ text: String, fullText: String? = nil) -> some View {
        GridRow {
            Text(label.uppercased()).workFont(.labelCaps).foregroundStyle(Theme.muted)
                .gridColumnAlignment(.leading)
            Text(verbatim: text).workFont(.body).foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .textSelection(.enabled)
                .help(fullText ?? "")
        }
    }

    private func metrics(_ presentation: ReceiptOverviewPresentation, columns: Int) -> some View {
        LazyVGrid(columns: Array(repeating: GridItem(.flexible(minimum: 0), spacing: Space.l, alignment: .leading), count: columns),
                  alignment: .leading, spacing: Space.m) {
            metric("Sessions", value: presentation.sessionsValue, detail: presentation.sessionsQualifier)
            metric("Cost", value: presentation.costValue, detail: presentation.costQualifier)
            metric("Claims supported", value: presentation.coverage.value,
                   detail: presentation.coverage.qualifier,
                   tint: presentation.coverage.isInconsistent ? Theme.amber : Theme.ink)
                .help("Claim coverage measures recorded evidence for checkable claims. It is separate from the number of check runs.")
            metric("Check runs", value: presentation.checks.value,
                   detail: presentation.checks.qualifier,
                   tint: presentation.checks.isInconsistent ? Theme.amber : Theme.ink)
        }
    }

    private func metric(_ label: String, value: String, detail: String, tint: Color = Theme.ink) -> some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            Text(label).workFont(.captionSemibold).foregroundStyle(Theme.muted)
            Text(value).workFont(.titleCard).foregroundStyle(tint)
                .fixedSize(horizontal: false, vertical: true)
            Text(detail).workFont(.caption).foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .accessibilityElement(children: .combine)
    }
}
