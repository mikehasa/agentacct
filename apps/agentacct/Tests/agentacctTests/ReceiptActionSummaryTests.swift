import XCTest
@testable import agentacct

final class ReceiptActionSummaryTests: XCTestCase {
    func testMetricsUseStableClosedTaxonomyAndBoundUnknownTypes() {
        let metrics = receiptActionMetrics([
            "search": 11,
            "delegate_task": 3,
            "read": 38,
            "execute": 24,
            "archive": 2,
            "edit": 7,
            "network": 5,
            "agent": 4,
            "plan": 3,
            "mcp": 2,
            "other": 1,
        ])

        XCTAssertEqual(
            metrics,
            [
                ReceiptActionMetric(
                    key: "read", label: "Read",
                    detail: "File or context read tool calls", count: 38
                ),
                ReceiptActionMetric(
                    key: "edit", label: "Edit",
                    detail: "Edit or write tool calls", count: 7
                ),
                ReceiptActionMetric(
                    key: "execute", label: "Execute",
                    detail: "Command or process tool calls", count: 24
                ),
                ReceiptActionMetric(
                    key: "search", label: "Search",
                    detail: "File or text search tool calls", count: 11
                ),
                ReceiptActionMetric(
                    key: "network", label: "Network",
                    detail: "Network access tool calls", count: 5
                ),
                ReceiptActionMetric(
                    key: "agent", label: "Agent",
                    detail: "Agent coordination tool calls", count: 4
                ),
                ReceiptActionMetric(
                    key: "plan", label: "Plan",
                    detail: "Planning tool calls", count: 3
                ),
                ReceiptActionMetric(
                    key: "mcp", label: "Connected tools",
                    detail: "Connected-tool calls", count: 2
                ),
                ReceiptActionMetric(
                    key: "other", label: "Other",
                    detail: "Tool calls outside named categories", count: 1
                ),
                ReceiptActionMetric(
                    key: "__unknown_types__", label: "Unrecognized types",
                    detail: "2 unrecognized categories", count: 5
                ),
            ]
        )
    }

    func testMetricsOmitZeroAndNegativeCountsWithoutEnumeratingUnknownKeys() {
        XCTAssertEqual(
            receiptActionMetrics(["read": 4, "search": 0, "edit": -1]).map(\.key),
            ["read"]
        )
        XCTAssertTrue(receiptActionMetrics(nil).isEmpty)
        XCTAssertTrue(receiptActionMetrics([:]).isEmpty)
        XCTAssertEqual(
            receiptActionMetrics(["plugin_private_name": 2]).first,
            ReceiptActionMetric(
                key: "__unknown_types__", label: "Unrecognized types",
                detail: "1 unrecognized category", count: 2
            )
        )
    }

    func testSynopsisDoesNotInventMeasuredZeroFromUnknownCaptureCoverage() {
        let absent = receiptActionSynopsis(counts: nil, reportedTotal: nil)
        XCTAssertEqual(absent.integrity, .unavailable)
        XCTAssertEqual(absent.headline, "not instrumented")
        XCTAssertNil(absent.captureBoundary)

        let zero = receiptActionSynopsis(counts: [:], reportedTotal: 0)
        XCTAssertEqual(zero.integrity, .captureUnknown)
        XCTAssertEqual(zero.headline, "No action records")
        XCTAssertEqual(zero.integrityDetail, "Capture coverage unknown")
        XCTAssertTrue(zero.metrics.isEmpty)
        XCTAssertNil(zero.shareDenominator)
        XCTAssertNil(zero.captureBoundary)

        let missingTotal = receiptActionSynopsis(counts: [:], reportedTotal: nil)
        XCTAssertEqual(missingTotal.integrity, .totalUnavailable)
        XCTAssertEqual(missingTotal.headline, "action total unavailable")
        XCTAssertEqual(missingTotal.integrityDetail, "No categorized action counts")
    }

    func testSynopsisRepresentsTotalOnlyWithoutInventingTypeRows() {
        let synopsis = receiptActionSynopsis(counts: nil, reportedTotal: 80)
        XCTAssertEqual(synopsis.integrity, .totalOnly)
        XCTAssertEqual(synopsis.headline, "80 actions recorded")
        XCTAssertEqual(synopsis.integrityDetail, "Type breakdown unavailable")
        XCTAssertTrue(synopsis.metrics.isEmpty)
        XCTAssertNil(synopsis.shareDenominator)
    }

    func testSynopsisReconcilesExactTotalsAndRejectsArithmeticDiscrepancies() {
        let exact = receiptActionSynopsis(
            counts: ["edit": 7, "execute": 24, "read": 38, "search": 11],
            reportedTotal: 80
        )
        XCTAssertEqual(exact.integrity, .exact)
        XCTAssertEqual(exact.headline, "80 actions recorded")
        XCTAssertNil(exact.integrityDetail)
        XCTAssertEqual(exact.shareDenominator, 80)
        XCTAssertEqual(exact.metrics.map(\.key), ["read", "edit", "execute", "search"])
        XCTAssertEqual(
            exact.captureBoundary,
            "No ordered action ledger; captured signals cannot be linked to results or timing."
        )

        let partial = receiptActionSynopsis(counts: ["read": 7], reportedTotal: 10)
        XCTAssertEqual(partial.integrity, .mismatch)
        XCTAssertEqual(partial.headline, "Action totals conflict")
        XCTAssertEqual(partial.integrityDetail, "7 categorized · 10 reported")
        XCTAssertNil(partial.shareDenominator)
        XCTAssertEqual(partial.metrics.map(\.key), ["read"])
    }

    func testDistributionAppearsOnlyForAReconciledPositivePartition() {
        let exact = receiptActionSynopsis(counts: ["read": 8], reportedTotal: 8)
        let partial = receiptActionSynopsis(counts: ["read": 7], reportedTotal: 10)
        XCTAssertTrue(exact.canShowDistribution)
        XCTAssertFalse(partial.canShowDistribution)

        let suppressed = [
            receiptActionSynopsis(counts: nil, reportedTotal: nil),
            receiptActionSynopsis(counts: [:], reportedTotal: 0),
            receiptActionSynopsis(counts: nil, reportedTotal: 8),
            receiptActionSynopsis(counts: ["read": 8], reportedTotal: nil),
            receiptActionSynopsis(counts: ["read": 8, "edit": 4], reportedTotal: 10),
            receiptActionSynopsis(counts: ["read": -1], reportedTotal: 1),
        ]
        XCTAssertTrue(suppressed.allSatisfy { !$0.canShowDistribution })
    }

    func testSynopsisKeepsCountsWhenRecordedTotalIsUnavailable() {
        let missingTotal = receiptActionSynopsis(counts: ["read": 1], reportedTotal: nil)
        XCTAssertEqual(missingTotal.integrity, .totalUnavailable)
        XCTAssertEqual(missingTotal.headline, "1 categorized action")
        XCTAssertEqual(missingTotal.integrityDetail, "Recorded total unavailable")
        XCTAssertNil(missingTotal.shareDenominator)
        XCTAssertNotNil(missingTotal.captureBoundary)
    }

    func testSynopsisNamesMismatchedTotalsAndSuppressesShares() {
        let mismatch = receiptActionSynopsis(
            counts: ["read": 8, "edit": 4],
            reportedTotal: 10
        )
        XCTAssertEqual(mismatch.integrity, .mismatch)
        XCTAssertEqual(mismatch.headline, "Action totals conflict")
        XCTAssertEqual(mismatch.integrityDetail, "12 categorized · 10 reported")
        XCTAssertNil(mismatch.shareDenominator)
        XCTAssertEqual(mismatch.metrics.map(\.count), [8, 4])
    }

    func testSynopsisNamesInvalidCategoriesAndTotalsWithoutNormalizingThem() {
        let invalidCategory = receiptActionSynopsis(
            counts: ["read": 8, "edit": -2],
            reportedTotal: 8
        )
        XCTAssertEqual(invalidCategory.integrity, .invalid)
        XCTAssertEqual(invalidCategory.headline, "Action data incomplete")
        XCTAssertEqual(
            invalidCategory.integrityDetail,
            "1 invalid category was omitted · 8 valid actions remain categorized · 8 actions were reported"
        )
        XCTAssertNil(invalidCategory.shareDenominator)
        XCTAssertEqual(invalidCategory.metrics.map(\.key), ["read"])

        let invalidTotal = receiptActionSynopsis(counts: ["read": 2], reportedTotal: -1)
        XCTAssertEqual(invalidTotal.integrity, .invalid)
        XCTAssertEqual(
            invalidTotal.integrityDetail,
            "the reported total is invalid · 2 valid actions remain categorized"
        )

        let blankKey = receiptActionSynopsis(counts: ["   ": 4], reportedTotal: 4)
        XCTAssertEqual(blankKey.integrity, .invalid)
        XCTAssertEqual(
            blankKey.integrityDetail,
            "1 invalid category was omitted · 4 actions were reported"
        )
        XCTAssertTrue(blankKey.metrics.isEmpty)

        let overflow = receiptActionSynopsis(
            counts: ["read": Int.max, "edit": 1],
            reportedTotal: Int.max
        )
        XCTAssertEqual(overflow.integrity, .invalid)
        XCTAssertEqual(
            overflow.integrityDetail,
            "the categorized action sum overflowed · \(Int.max) actions were reported"
        )
        XCTAssertNil(overflow.categorizedTotal)

        let unknownOverflow = receiptActionSynopsis(
            counts: ["future_a": Int.max, "future_b": 1, "": -1],
            reportedTotal: Int.max
        )
        XCTAssertEqual(unknownOverflow.integrity, .invalid)
        XCTAssertEqual(
            unknownOverflow.integrityDetail,
            "1 invalid category was omitted · the categorized action sum overflowed · \(Int.max) actions were reported"
        )
        XCTAssertNil(unknownOverflow.categorizedTotal)
        XCTAssertTrue(unknownOverflow.metrics.isEmpty)
    }

    func testKPIUsesTheSameIntegrityClassificationAsTheDigest() {
        let cases: [(ReceiptActionSynopsis, ReceiptActionKPI)] = [
            (
                receiptActionSynopsis(counts: nil, reportedTotal: nil),
                ReceiptActionKPI(value: nil, qualifier: nil, absent: "not instrumented")
            ),
            (
                receiptActionSynopsis(counts: [:], reportedTotal: 0),
                ReceiptActionKPI(value: nil, qualifier: nil, absent: "capture unknown")
            ),
            (
                receiptActionSynopsis(counts: nil, reportedTotal: 8),
                ReceiptActionKPI(value: "8", qualifier: "recorded", absent: nil)
            ),
            (
                receiptActionSynopsis(counts: ["read": 8], reportedTotal: 8),
                ReceiptActionKPI(value: "8", qualifier: "recorded", absent: nil)
            ),
            (
                receiptActionSynopsis(counts: ["read": 3], reportedTotal: nil),
                ReceiptActionKPI(value: "3", qualifier: "categorized", absent: nil)
            ),
            (
                receiptActionSynopsis(counts: ["read": 4], reportedTotal: 3),
                ReceiptActionKPI(value: nil, qualifier: nil, absent: "totals conflict")
            ),
            (
                receiptActionSynopsis(counts: ["read": -1], reportedTotal: -1),
                ReceiptActionKPI(value: nil, qualifier: nil, absent: "data incomplete")
            ),
        ]

        for (synopsis, expected) in cases {
            XCTAssertEqual(receiptActionKPI(synopsis), expected)
        }
    }

    func testScopeDoesNotMisrepresentRelatedPathsAsActionTargets() {
        XCTAssertEqual(
            receiptActionScope(recordedPathCount: 18),
            "18 paths referenced by captured work and machine checks"
        )
        XCTAssertEqual(
            receiptActionScope(recordedPathCount: 1),
            "1 path referenced by captured work and machine checks"
        )
        XCTAssertEqual(
            receiptActionScope(recordedPathCount: 0),
            "0 paths referenced by captured work and machine checks"
        )
        XCTAssertEqual(receiptActionScope(recordedPathCount: nil), "")
        XCTAssertEqual(receiptActionScope(recordedPathCount: -1), "")
    }

    func testSourceTextUsesPlainHumanLabels() {
        XCTAssertEqual(
            receiptActionSourceText(["mcp", "hook", "transcript_scan", "none", "mcp"]),
            "MCP, Client hook, Transcript scan"
        )
        XCTAssertEqual(receiptActionSourceText(["client_log", "agent_report", "ci"]), "Client log, Agent report, CI")
        XCTAssertEqual(receiptActionSourceText(["none"]), "")
        XCTAssertEqual(receiptActionSourceText(nil), "")
    }

    func testUnrecognizedCategoriesStayBoundedAndSuppressTheChart() {
        var counts: [String: Int] = ["read": 2]
        for index in 0..<5_000 { counts["future_\(index)"] = 1 }

        let synopsis = receiptActionSynopsis(counts: counts, reportedTotal: 5_002)

        XCTAssertEqual(synopsis.integrity, .unrecognizedCategories)
        XCTAssertEqual(synopsis.metrics.count, 2)
        XCTAssertEqual(synopsis.metrics.last?.label, "Unrecognized types")
        XCTAssertEqual(synopsis.metrics.last?.count, 5_000)
        XCTAssertFalse(synopsis.canShowDistribution)
        XCTAssertEqual(receiptActionKPI(synopsis).qualifier, "types changed")
    }
}
