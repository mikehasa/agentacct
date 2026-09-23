import XCTest
@testable import agentacct

final class ReceiptOverviewTests: XCTestCase {
    func testOverviewUsesWholeReceiptCountsWithoutSessionDetails() throws {
        var object = try receiptObject()
        object.removeValue(forKey: "sessions")
        var axes = try XCTUnwrap(object["axes"] as? [String: Any])
        axes["evidence_strength"] = [
            "key": "self_checked", "gradeable": true,
            "checkable_total": 10, "checked_total": 3,
        ]
        object["axes"] = axes
        var dimensions = try XCTUnwrap(object["dimensions"] as? [String: Any])
        dimensions["evidence"] = ["checks_total": 68, "checks_passed": 65, "checks_failed": 3]
        object["dimensions"] = dimensions

        let presentation = ReceiptOverviewPresentation(receipt: try decode(object))

        XCTAssertEqual(presentation.coverage.value, "3 of 10")
        XCTAssertEqual(presentation.checks.value, "65 of 68")
        XCTAssertTrue(presentation.checks.qualifier.contains("3 failed"))
        XCTAssertEqual(presentation.sessionsValue, "3")
        XCTAssertEqual(presentation.sessionsQualifier, "sessions in this task")
    }

    func testMissingCountsAndCostNeverBecomeZero() throws {
        var object = try receiptObject()
        object.removeValue(forKey: "sessions")
        var axes = try XCTUnwrap(object["axes"] as? [String: Any])
        axes["evidence_strength"] = ["key": "unknown"]
        object["axes"] = axes
        var dimensions = try XCTUnwrap(object["dimensions"] as? [String: Any])
        dimensions["evidence"] = [:] as [String: Any]
        dimensions["cost"] = [:] as [String: Any]
        dimensions["task"] = [:] as [String: Any]
        object["dimensions"] = dimensions

        let missing = ReceiptOverviewPresentation(receipt: try decode(object))
        XCTAssertEqual(missing.coverage.value, "Not reported")
        XCTAssertEqual(missing.checks.value, "Total not reported")
        XCTAssertEqual(missing.costValue, "Not reported")
        XCTAssertEqual(missing.sessionsValue, "Not reported")

        axes["evidence_strength"] = [
            "key": "not_gradeable", "gradeable": false,
            "checkable_total": 0, "checked_total": 0,
        ]
        object["axes"] = axes
        dimensions["evidence"] = ["checks_total": 0, "checks_passed": 0, "checks_failed": 0]
        dimensions["cost"] = [
            "estimated_cost_usd": 0, "cost_complete": true,
            "cost_confidence": "client_reported", "cost_basis": "local_client_session",
        ]
        dimensions["task"] = ["boundary": ["session_count": 0]]
        object["dimensions"] = dimensions

        let zero = ReceiptOverviewPresentation(receipt: try decode(object))
        XCTAssertEqual(zero.coverage.value, "Not gradeable")
        XCTAssertEqual(zero.checks.value, "None")
        XCTAssertEqual(zero.costValue, "$0.00")
        XCTAssertEqual(zero.sessionsValue, "0")
    }

    func testCostRetainsPartialAndEstimatedMeaning() throws {
        var object = try receiptObject()
        var dimensions = try XCTUnwrap(object["dimensions"] as? [String: Any])
        var cost: [String: Any] = [
            "estimated_cost_usd": 1.25, "cost_complete": false,
            "cost_confidence": "client_reported", "cost_basis": "local_client_session",
        ]
        dimensions["cost"] = cost
        object["dimensions"] = dimensions
        let partial = ReceiptOverviewPresentation(receipt: try decode(object))
        XCTAssertEqual(partial.costValue, "~$1.25")
        XCTAssertEqual(partial.costQualifier, "client-reported · known subtotal · incomplete coverage")

        cost["cost_complete"] = true
        cost["cost_confidence"] = "estimated_from_tokens"
        cost["cost_basis"] = "pricing_table"
        dimensions["cost"] = cost
        object["dimensions"] = dimensions
        let estimate = ReceiptOverviewPresentation(receipt: try decode(object))
        XCTAssertEqual(estimate.costValue, "≈$1.25")
        XCTAssertEqual(estimate.costQualifier, "pricing estimate · complete coverage")

        cost.removeValue(forKey: "cost_complete")
        dimensions["cost"] = cost
        object["dimensions"] = dimensions
        XCTAssertTrue(ReceiptOverviewPresentation(receipt: try decode(object)).costQualifier.contains("coverage not reported"))
    }

    func testListedSessionsAreDeduplicatedWithoutClaimingCompleteScope() throws {
        var object = try receiptObject()
        var dimensions = try XCTUnwrap(object["dimensions"] as? [String: Any])
        dimensions["task"] = [:] as [String: Any]
        object["dimensions"] = dimensions
        let codex: [String: Any] = ["client": "codex", "client_session_id": "same-id"]
        let hermes: [String: Any] = ["client": "hermes", "client_session_id": "same-id"]
        object["sessions"] = [
            ["root": codex, "members": [codex, hermes]],
            ["root": codex, "members": [codex]],
        ]

        let presentation = ReceiptOverviewPresentation(receipt: try decode(object))
        XCTAssertEqual(presentation.sessionsValue, "2")
        XCTAssertEqual(presentation.sessionsQualifier, "listed sessions · total not reported")
    }

    func testPassingChecksDoNotUpgradeRecordedOutcome() throws {
        var object = try receiptObject()
        var axes = try XCTUnwrap(object["axes"] as? [String: Any])
        axes["decision_status"] = [
            "key": "in_progress", "statement": "Still implementing the final step.",
            "asserted_by": "agent_report",
        ]
        object["axes"] = axes
        var dimensions = try XCTUnwrap(object["dimensions"] as? [String: Any])
        dimensions["evidence"] = ["checks_total": 20, "checks_passed": 20, "checks_failed": 0]
        object["dimensions"] = dimensions

        let presentation = ReceiptOverviewPresentation(receipt: try decode(object))
        XCTAssertEqual(presentation.decision.explanation, "Still implementing the final step. — agent reported")
        XCTAssertEqual(presentation.checks.value, "20 of 20")
        XCTAssertFalse(presentation.decision.isAttention)
    }

    private func receiptObject() throws -> [String: Any] {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "dashboard", withExtension: "json"))
        let fixture = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        let work = try XCTUnwrap(fixture["work"] as? [String: Any])
        return try XCTUnwrap(work["receipt"] as? [String: Any])
    }

    private func decode(_ object: [String: Any]) throws -> Receipt {
        try JSONDecoder().decode(Receipt.self, from: JSONSerialization.data(withJSONObject: object))
    }
}
