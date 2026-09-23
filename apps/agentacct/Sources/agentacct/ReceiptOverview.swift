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

struct ReceiptOverview: View {
    let receipt: Receipt

    var body: some View {
        let presentation = ReceiptOverviewPresentation(receipt: receipt)
        Card(padding: Space.l) {
            VStack(alignment: .leading, spacing: Space.m) {
                HStack(alignment: .firstTextBaseline) {
                    Text("Current outcome").workFont(.rowLabel)
                        .foregroundStyle(Theme.ink)
                        .accessibilityAddTraits(.isHeader)
                    Spacer(minLength: Space.s)
                    Text("Whole task").workFont(.caption).foregroundStyle(Theme.muted)
                }
                Text(verbatim: presentation.decision.explanation)
                    .workFont(.body).foregroundStyle(Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)

                Rectangle().fill(Theme.hairline).frame(height: 1)
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 230), spacing: Space.l, alignment: .leading)],
                          alignment: .leading, spacing: Space.m) {
                    metric("Claims supported", value: presentation.coverage.value,
                           detail: presentation.coverage.qualifier,
                           tint: presentation.coverage.isInconsistent ? Theme.amber : Theme.ink)
                        .help("Claim coverage measures recorded evidence for checkable claims. It is separate from the number of check runs.")
                    metric("Check runs", value: presentation.checks.value,
                           detail: presentation.checks.qualifier,
                           tint: presentation.checks.isInconsistent ? Theme.amber : Theme.ink)
                    metric("Cost", value: presentation.costValue, detail: presentation.costQualifier)
                    metric("Sessions", value: presentation.sessionsValue, detail: presentation.sessionsQualifier)
                }
                if let note = presentation.evidenceNote {
                    Text(note).workFont(.caption).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .accessibilityIdentifier("work.receipt.overview")
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
