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
        let absent = receiptActionSynopsis(counts: nil, storedTotal: nil)
        XCTAssertEqual(absent.integrity, .unavailable)
        XCTAssertEqual(absent.headline, "not instrumented")
        XCTAssertNil(absent.captureBoundary)

        let zero = receiptActionSynopsis(counts: [:], storedTotal: 0)
        XCTAssertEqual(zero.integrity, .captureUnknown)
        XCTAssertEqual(zero.headline, "No captured tool calls")
        XCTAssertEqual(zero.integrityDetail, "Capture coverage unknown")
        XCTAssertTrue(zero.metrics.isEmpty)
        XCTAssertNil(zero.shareDenominator)
        XCTAssertNil(zero.captureBoundary)

        let missingTotal = receiptActionSynopsis(counts: [:], storedTotal: nil)
        XCTAssertEqual(missingTotal.integrity, .totalUnavailable)
        XCTAssertEqual(missingTotal.headline, "stored total unavailable")
        XCTAssertEqual(missingTotal.integrityDetail, "No categorized tool-call counts")
    }

    func testSynopsisRepresentsTotalOnlyWithoutInventingTypeRows() {
        let synopsis = receiptActionSynopsis(counts: nil, storedTotal: 80)
        XCTAssertEqual(synopsis.integrity, .totalOnly)
        XCTAssertEqual(synopsis.headline, "80 tool calls in stored total")
        XCTAssertEqual(synopsis.integrityDetail, "Tool-call type breakdown unavailable")
        XCTAssertTrue(synopsis.metrics.isEmpty)
        XCTAssertNil(synopsis.shareDenominator)
    }

    func testSynopsisReconcilesExactTotalsAndRejectsArithmeticDiscrepancies() {
        let exact = receiptActionSynopsis(
            counts: ["edit": 7, "execute": 24, "read": 38, "search": 11],
            storedTotal: 80
        )
        XCTAssertEqual(exact.integrity, .exact)
        XCTAssertEqual(exact.headline, "80 tool calls captured")
        XCTAssertNil(exact.integrityDetail)
        XCTAssertEqual(exact.shareDenominator, 80)
        XCTAssertEqual(exact.metrics.map(\.key), ["read", "edit", "execute", "search"])
        XCTAssertEqual(
            exact.captureBoundary,
            "No ordered action ledger; captured tool-call counts cannot be linked to results or timing."
        )

        let partial = receiptActionSynopsis(counts: ["read": 7], storedTotal: 10)
        XCTAssertEqual(partial.integrity, .mismatch)
        XCTAssertEqual(partial.headline, "Tool-call totals conflict")
        XCTAssertEqual(partial.integrityDetail, "category counts sum to 7 · stored total is 10")
        XCTAssertNil(partial.shareDenominator)
        XCTAssertEqual(partial.metrics.map(\.key), ["read"])
    }

    func testDistributionAppearsOnlyForAReconciledPositivePartition() {
        let exact = receiptActionSynopsis(counts: ["read": 8], storedTotal: 8)
        let partial = receiptActionSynopsis(counts: ["read": 7], storedTotal: 10)
        XCTAssertTrue(exact.canShowDistribution)
        XCTAssertFalse(partial.canShowDistribution)

        let suppressed = [
            receiptActionSynopsis(counts: nil, storedTotal: nil),
            receiptActionSynopsis(counts: [:], storedTotal: 0),
            receiptActionSynopsis(counts: nil, storedTotal: 8),
            receiptActionSynopsis(counts: ["read": 8], storedTotal: nil),
            receiptActionSynopsis(counts: ["read": 8, "edit": 4], storedTotal: 10),
            receiptActionSynopsis(counts: ["read": -1], storedTotal: 1),
        ]
        XCTAssertTrue(suppressed.allSatisfy { !$0.canShowDistribution })
    }

    func testSynopsisKeepsCountsWhenStoredTotalIsUnavailable() {
        let missingTotal = receiptActionSynopsis(counts: ["read": 1], storedTotal: nil)
        XCTAssertEqual(missingTotal.integrity, .totalUnavailable)
        XCTAssertEqual(missingTotal.headline, "1 categorized tool call")
        XCTAssertEqual(missingTotal.integrityDetail, "Stored tool-call total unavailable")
        XCTAssertNil(missingTotal.shareDenominator)
        XCTAssertNotNil(missingTotal.captureBoundary)
    }

    func testSynopsisNamesMismatchedTotalsAndSuppressesShares() {
        let mismatch = receiptActionSynopsis(
            counts: ["read": 8, "edit": 4],
            storedTotal: 10
        )
        XCTAssertEqual(mismatch.integrity, .mismatch)
        XCTAssertEqual(mismatch.headline, "Tool-call totals conflict")
        XCTAssertEqual(mismatch.integrityDetail, "category counts sum to 12 · stored total is 10")
        XCTAssertNil(mismatch.shareDenominator)
        XCTAssertEqual(mismatch.metrics.map(\.count), [8, 4])
    }

    func testSynopsisNamesInvalidCategoriesAndTotalsWithoutNormalizingThem() {
        let invalidCategory = receiptActionSynopsis(
            counts: ["read": 8, "edit": -2],
            storedTotal: 8
        )
        XCTAssertEqual(invalidCategory.integrity, .invalid)
        XCTAssertEqual(invalidCategory.headline, "Tool-call data incomplete")
        XCTAssertEqual(
            invalidCategory.integrityDetail,
            "1 invalid category was omitted · 8 valid tool calls remain categorized · stored total is 8"
        )
        XCTAssertNil(invalidCategory.shareDenominator)
        XCTAssertEqual(invalidCategory.metrics.map(\.key), ["read"])

        let invalidTotal = receiptActionSynopsis(counts: ["read": 2], storedTotal: -1)
        XCTAssertEqual(invalidTotal.integrity, .invalid)
        XCTAssertEqual(
            invalidTotal.integrityDetail,
            "the stored total is invalid · 2 valid tool calls remain categorized"
        )

        let blankKey = receiptActionSynopsis(counts: ["   ": 4], storedTotal: 4)
        XCTAssertEqual(blankKey.integrity, .invalid)
        XCTAssertEqual(
            blankKey.integrityDetail,
            "1 invalid category was omitted · stored total is 4"
        )
        XCTAssertTrue(blankKey.metrics.isEmpty)

        let overflow = receiptActionSynopsis(
            counts: ["read": Int.max, "edit": 1],
            storedTotal: Int.max
        )
        XCTAssertEqual(overflow.integrity, .invalid)
        XCTAssertEqual(
            overflow.integrityDetail,
            "the categorized tool-call sum overflowed · stored total is \(Int.max)"
        )
        XCTAssertNil(overflow.categorizedTotal)

        let unknownOverflow = receiptActionSynopsis(
            counts: ["future_a": Int.max, "future_b": 1, "": -1],
            storedTotal: Int.max
        )
        XCTAssertEqual(unknownOverflow.integrity, .invalid)
        XCTAssertEqual(
            unknownOverflow.integrityDetail,
            "1 invalid category was omitted · the categorized tool-call sum overflowed · stored total is \(Int.max)"
        )
        XCTAssertNil(unknownOverflow.categorizedTotal)
        XCTAssertTrue(unknownOverflow.metrics.isEmpty)
    }

    func testKPIUsesTheSameIntegrityClassificationAsTheDigest() {
        let cases: [(ReceiptActionSynopsis, ReceiptActionKPI)] = [
            (
                receiptActionSynopsis(counts: nil, storedTotal: nil),
                ReceiptActionKPI(value: nil, qualifier: nil, absent: "not instrumented")
            ),
            (
                receiptActionSynopsis(counts: [:], storedTotal: 0),
                ReceiptActionKPI(value: nil, qualifier: nil, absent: "capture unknown")
            ),
            (
                receiptActionSynopsis(counts: nil, storedTotal: 8),
                ReceiptActionKPI(value: "8", qualifier: "tool calls", absent: nil)
            ),
            (
                receiptActionSynopsis(counts: ["read": 8], storedTotal: 8),
                ReceiptActionKPI(value: "8", qualifier: "tool calls", absent: nil)
            ),
            (
                receiptActionSynopsis(counts: ["read": 3], storedTotal: nil),
                ReceiptActionKPI(value: "3", qualifier: "categorized calls", absent: nil)
            ),
            (
                receiptActionSynopsis(counts: [:], storedTotal: nil),
                ReceiptActionKPI(value: nil, qualifier: nil, absent: "stored total unavailable")
            ),
            (
                receiptActionSynopsis(counts: ["future_type": 2], storedTotal: 2),
                ReceiptActionKPI(value: "2", qualifier: "tool calls · types changed", absent: nil)
            ),
            (
                receiptActionSynopsis(counts: ["future_type": 2], storedTotal: nil),
                ReceiptActionKPI(value: "2", qualifier: "categorized calls · types changed", absent: nil)
            ),
            (
                receiptActionSynopsis(counts: ["read": 4], storedTotal: 3),
                ReceiptActionKPI(value: nil, qualifier: nil, absent: "tool-call totals conflict")
            ),
            (
                receiptActionSynopsis(counts: ["read": -1], storedTotal: -1),
                ReceiptActionKPI(value: nil, qualifier: nil, absent: "tool-call data incomplete")
            ),
        ]

        for (synopsis, expected) in cases {
            XCTAssertEqual(receiptActionKPI(synopsis), expected)
        }
    }

    func testScopeDoesNotMisrepresentRelatedPathsAsActionTargets() {
        XCTAssertEqual(
            receiptActionScope(relatedPathCount: 18),
            "18 unique paths from recorded work, machine checks, or captured edit tool calls"
        )
        XCTAssertEqual(
            receiptActionScope(relatedPathCount: 1),
            "1 unique path from recorded work, machine checks, or captured edit tool calls"
        )
        XCTAssertEqual(
            receiptActionScope(relatedPathCount: 0),
            "0 unique paths from recorded work, machine checks, or captured edit tool calls"
        )
        XCTAssertEqual(receiptActionScope(relatedPathCount: nil), "")
        XCTAssertEqual(receiptActionScope(relatedPathCount: -1), "")
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

        let synopsis = receiptActionSynopsis(counts: counts, storedTotal: 5_002)

        XCTAssertEqual(synopsis.integrity, .unrecognizedCategories)
        XCTAssertEqual(synopsis.metrics.count, 2)
        XCTAssertEqual(synopsis.metrics.last?.label, "Unrecognized types")
        XCTAssertEqual(synopsis.metrics.last?.count, 5_000)
        XCTAssertEqual(synopsis.headline, "5002 tool calls captured")
        XCTAssertEqual(
            synopsis.integrityDetail,
            "5000 unrecognized tool-call types were aggregated · update the app to interpret them"
        )
        XCTAssertFalse(synopsis.canShowDistribution)
        XCTAssertEqual(receiptActionKPI(synopsis).qualifier, "tool calls · types changed")
    }
}
