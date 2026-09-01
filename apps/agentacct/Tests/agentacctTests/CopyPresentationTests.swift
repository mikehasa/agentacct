import XCTest
@testable import agentacct

final class CopyPresentationTests: XCTestCase {
    func testSourceStateMachineMakesCurrentFailuresOutrankRetainedData() {
        XCTAssertEqual(sourcesContentMode(hasSnapshot: false, failure: nil), .loading)
        XCTAssertEqual(sourcesContentMode(hasSnapshot: true, failure: nil), .current)
        XCTAssertEqual(
            sourcesContentMode(hasSnapshot: false, failure: .serviceUnavailable),
            .unavailable
        )
        XCTAssertEqual(
            sourcesContentMode(hasSnapshot: true, failure: .requestFailed("timeout")),
            .retainedFailure
        )
    }

    func testSourceFailuresNameTheActualStateAndRecovery() {
        XCTAssertEqual(
            SourcesUnavailablePresentation(failure: .unsupported),
            SourcesUnavailablePresentation(
                title: "Source health unavailable",
                detail: "This version of agentacct cannot report source health.",
                recovery: "Update agentacct, restart it, then refresh."
            )
        )
        XCTAssertEqual(
            SourcesUnavailablePresentation(failure: .serviceUnavailable),
            SourcesUnavailablePresentation(
                title: "agentacct is not running",
                detail: "Source health and background updates are unavailable.",
                recovery: "In Terminal, run agentacct start, then refresh."
            )
        )
        XCTAssertEqual(
            SourcesUnavailablePresentation(failure: .requestFailed("connection reset")),
            SourcesUnavailablePresentation(
                title: "Source health unavailable",
                detail: "The latest source health request did not complete.",
                recovery: "Refresh after agentacct is reachable.",
                diagnostic: "connection reset"
            )
        )
    }

    func testEvidenceRolesNeverTurnHumanReviewIntoMachineVerification() {
        XCTAssertEqual(
            EvidenceRolePresentation.ci,
            EvidenceRolePresentation(
                name: "CI checks",
                detail: "Not connected",
                outcome: "Independent check evidence"
            )
        )
        XCTAssertEqual(
            EvidenceRolePresentation.human,
            EvidenceRolePresentation(
                name: "Human review",
                detail: "Records review and resolution decisions",
                outcome: "Review assertion"
            )
        )
        XCTAssertNotEqual(EvidenceRolePresentation.human.outcome.lowercased(), "verified")
    }

    func testPrivacyCopySeparatesDefaultCaptureFromRestrictedOptIn() {
        let privacy = LocalStorePrivacyPresentation.current

        XCTAssertEqual(
            privacy.defaultExclusions,
            "Default capture excludes raw prompts, file contents, non-command tool arguments, and tool output. Execute command lines may be stored locally after length limits and best-effort secret masking."
        )
        XCTAssertTrue(privacy.restrictedOptIn.contains("explicit opt-in"))
        XCTAssertTrue(privacy.restrictedOptIn.contains("secret fields are rejected"))
        XCTAssertFalse(privacy.restrictedOptIn.contains("nothing leaves"))
    }

    func testReceiptSourceLabelsHideStorageKeysWithoutChangingThem() {
        XCTAssertEqual(receiptSourceLabel("client_log"), "Client log")
        XCTAssertEqual(receiptSourceLabel("agent_report"), "Agent report")
        XCTAssertEqual(receiptSourceLabel("client_hook"), "Client hook")
        XCTAssertEqual(receiptSourceLabel("pricing_table"), "Pricing table")
        XCTAssertEqual(receiptSourceLabel("mcp"), "Agent recording")
        XCTAssertEqual(receiptSourceLabel("unknown_source"), "Unknown source")
    }

    func testSourceNamesUseProductNamesInsteadOfStorageKeys() {
        XCTAssertEqual(clientDisplayName("claude-code"), "Claude Code")
        XCTAssertEqual(clientDisplayName("codex"), "Codex")
        XCTAssertEqual(clientDisplayName("opencode"), "OpenCode")
        XCTAssertEqual(clientDisplayName("future_client"), "Future Client")
    }

    func testEvidenceLedgerUsesNaturalCountsAndPlainTerms() {
        let evidence = ReceiptEvidence(
            key: "independently_checked",
            gradeable: true,
            strongestTier: "independently_checked",
            checkableTotal: 4,
            checkedTotal: 1,
            byTier: nil,
            notCheckable: 1,
            openOrIncomplete: 1,
            hiddenInSubagents: 1,
            unattributedChecks: 2,
            totalSteps: 6,
            checksTotal: 2,
            checksPassed: 1,
            checksFailed: 1,
            definition: nil
        )

        XCTAssertEqual(
            evidence.ledger,
            "1 step ran in supporting sessions · "
                + "1 research or documentation step has no machine-verifiable claim · "
                + "2 checks are not linked to a step · 1 step is still open"
        )
    }
}
