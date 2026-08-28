import XCTest
@testable import agentacct

final class ReceiptPresentationSemanticsTests: XCTestCase {
    func testCheckSummaryKeepsMissingTotalDistinctFromZero() {
        XCTAssertEqual(
            receiptCheckSummary(total: nil, passed: nil, failed: nil),
            "check total not reported"
        )
        XCTAssertEqual(
            receiptCheckSummary(total: 0, passed: 0, failed: 0),
            "0 checks · 0 passed · 0 failed"
        )
        XCTAssertEqual(
            receiptCheckSummary(total: nil, passed: 3, failed: 1),
            "check total not reported · 3 passed · 1 failed"
        )
    }

    func testExternalEvidenceNoticeNamesOnlyExternalTierWhenIndependentChecksExist() {
        let tiers = ReceiptByTier(
            externallyVerified: nil,
            independentlyChecked: 4,
            selfChecked: 0,
            unchecked: 0
        )
        XCTAssertEqual(
            receiptExternalEvidenceNotice(byTier: tiers),
            "No externally verified evidence on this receipt"
        )
        XCTAssertEqual(
            receiptExternalEvidenceNotice(
                byTier: ReceiptByTier(
                    externallyVerified: 0,
                    independentlyChecked: 4,
                    selfChecked: 0,
                    unchecked: 0
                )
            ),
            "No externally verified evidence on this receipt"
        )
        XCTAssertNil(
            receiptExternalEvidenceNotice(
                byTier: ReceiptByTier(
                    externallyVerified: 1,
                    independentlyChecked: 0,
                    selfChecked: 0,
                    unchecked: 0
                )
            )
        )
    }

    func testCIEvidenceNoticeKeepsEvidenceAndDecisionAxesSeparate() {
        XCTAssertEqual(
            receiptCIEvidenceNotice(sources: ["mcp", "agent_report"]),
            ReceiptCIEvidenceNotice(
                headline: "No CI evidence on this receipt",
                detail: "CI can strengthen supporting evidence, but it does not change the separately recorded decision status."
            )
        )
        XCTAssertNil(receiptCIEvidenceNotice(sources: ["mcp", "ci"]))
    }
}
