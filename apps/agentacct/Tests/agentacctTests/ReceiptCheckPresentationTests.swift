import XCTest
@testable import agentacct

final class ReceiptCheckPresentationTests: XCTestCase {
    func testCollectionPrioritizesActiveNonPassesWithoutReorderingInsideGroups() {
        let evidence = makeEvidence(checks: [
            makeCheck(name: "pass one", result: "passed", exitCode: 0),
            makeCheck(name: "error one", result: "error", exitCode: 127),
            makeCheck(name: "skip one", result: "skipped"),
            makeCheck(name: "failure one", result: "failed", exitCode: 1),
            makeCheck(name: "pass two", result: "passed", exitCode: 0),
        ])

        let collection = ReceiptCheckCollectionPresentation(evidence: evidence)

        XCTAssertEqual(
            collection.rows(in: .attention).map(\.title),
            ["error one", "failure one"]
        )
        XCTAssertEqual(collection.rows(in: .other).map(\.title), ["skip one"])
        XCTAssertEqual(collection.rows(in: .passed).map(\.title), ["pass one", "pass two"])
    }

    func testSettledAndSupersededFailuresMoveToHistoryWithoutChangingTheirResult() {
        let reviewed = ReceiptCheckFinding(
            targetDigest: "finding-a", state: "reviewed", revision: 1,
            attentionOpen: false, note: nil
        )
        let evidence = makeEvidence(checks: [
            makeCheck(name: "superseded", result: "failed", superseded: true),
            makeCheck(name: "reviewed", result: "failed", finding: reviewed),
            makeCheck(name: "open", result: "failed"),
        ])

        let collection = ReceiptCheckCollectionPresentation(evidence: evidence)

        XCTAssertEqual(collection.rows(in: .history).map(\.title), ["superseded", "reviewed"])
        XCTAssertEqual(collection.rows(in: .history).map(\.resultLabel), ["Failed", "Failed"])
        XCTAssertEqual(collection.rows(in: .attention).map(\.title), ["open"])
    }

    func testRoutineGroupsStartExpandedOnlyForTenOrFewerItemizedRuns() {
        let ten = makeEvidence(checks: (0..<10).map {
            makeCheck(name: "pass \($0)", result: "passed", exitCode: 0)
        })
        let eleven = makeEvidence(checks: (0..<11).map {
            makeCheck(name: "pass \($0)", result: "passed", exitCode: 0)
        })

        let small = ReceiptCheckCollectionPresentation(evidence: ten)
        let large = ReceiptCheckCollectionPresentation(evidence: eleven)

        XCTAssertTrue(small.routineGroupExpanded(userOverride: nil))
        XCTAssertFalse(large.routineGroupExpanded(userOverride: nil))
        XCTAssertTrue(large.routineGroupExpanded(userOverride: true))
        XCTAssertFalse(small.routineGroupExpanded(userOverride: false))
        XCTAssertTrue(large.routineGroupExpanded(userOverride: nil, forcedDefault: true))
    }

    func testSharedScopeIsShownOnceOnlyWhenEveryItemizedRunMatches() {
        let shared = makeEvidence(checks: [
            makeCheck(name: "one", result: "passed", scope: "project:agentacct:abc"),
            makeCheck(name: "two", result: "failed", scope: "project:agentacct:abc"),
        ])
        let mixed = makeEvidence(checks: [
            makeCheck(name: "one", result: "passed", scope: "project:agentacct:abc"),
            makeCheck(name: "two", result: "failed", scope: "project:agentacct:def"),
        ])
        let incomplete = makeEvidence(checks: [
            makeCheck(name: "one", result: "passed", scope: "project:agentacct:abc"),
            makeCheck(name: "two", result: "failed", scope: nil),
        ])

        XCTAssertEqual(
            ReceiptCheckCollectionPresentation(evidence: shared).sharedScope,
            "project:agentacct:abc"
        )
        XCTAssertNil(ReceiptCheckCollectionPresentation(evidence: mixed).sharedScope)
        XCTAssertNil(ReceiptCheckCollectionPresentation(evidence: incomplete).sharedScope)
    }

    func testExactDuplicatesHaveUniqueStableIdentitiesAcrossReordering() {
        let duplicate = makeCheck(
            name: "same check", result: "passed", exitCode: 0,
            scope: "project", source: "hook", at: 123
        )
        let other = makeCheck(name: "other", result: "failed", exitCode: 1)
        let first = ReceiptCheckCollectionPresentation(
            evidence: makeEvidence(checks: [duplicate, duplicate, other])
        )
        let reordered = ReceiptCheckCollectionPresentation(
            evidence: makeEvidence(checks: [other, duplicate, duplicate])
        )

        let firstDuplicateIDs = first.rows.filter { $0.title == "same check" }.map(\.id)
        let reorderedDuplicateIDs = reordered.rows.filter { $0.title == "same check" }.map(\.id)
        XCTAssertEqual(Set(firstDuplicateIDs).count, 2)
        XCTAssertEqual(firstDuplicateIDs, reorderedDuplicateIDs)
        XCTAssertEqual(
            Set(first.rows.map(\.accessibilityIdentifier)).count,
            first.rows.count
        )
    }

    func testMutableEnrichmentAndDispositionPreserveRunIdentity() {
        let original = makeCheck(
            name: "same run", result: "failed", exitCode: 1,
            scope: "project", source: "hook", at: 123
        )
        let resolved = ReceiptCheckFinding(
            targetDigest: "digest", state: "resolved", revision: 4,
            attentionOpen: false, note: "fixed later"
        )
        let enriched = makeCheck(
            name: "same run", result: "failed", exitCode: 1,
            scope: "project", source: "hook", superseded: true, at: 123,
            summary: "Additional detail arrived later.",
            files: ["Sources/Feature.swift"], commandRedacted: true,
            artifactRef: "artifact:later", finding: resolved
        )

        let before = ReceiptCheckRowPresentation(check: original, occurrence: 0)
        let after = ReceiptCheckRowPresentation(check: enriched, occurrence: 0)

        XCTAssertEqual(before.id, after.id)
        XCTAssertEqual(before.accessibilityIdentifier, after.accessibilityIdentifier)
        XCTAssertEqual(before.group, .attention)
        XCTAssertEqual(after.group, .history)
    }

    func testRowPresentationMakesStatusAndTrustMetadataReadable() {
        let check = makeCheck(
            name: "Ruff executable", result: "failed", exitCode: 127,
            scope: "project:agentacct-gui:756ce2", source: "mcp"
        )
        let row = ReceiptCheckRowPresentation(check: check, occurrence: 0)

        XCTAssertEqual(row.resultLabel, "Failed")
        XCTAssertEqual(row.sourceLabel, "Connected tool")
        XCTAssertEqual(row.collapsedExitText, "exit 127")
        XCTAssertEqual(row.runDetailText, "Failed · exit 127 · Connected tool")
        XCTAssertEqual(
            row.accessibilityValue(isExpanded: false),
            "Failed, source Connected tool, exit 127, scope project:agentacct-gui:756ce2, collapsed"
        )
    }

    func testRoutineZeroExitIsHiddenCollapsedButRetainedInExpandedRunDetail() {
        let row = ReceiptCheckRowPresentation(
            check: makeCheck(name: "Swift suite", result: "passed", exitCode: 0, source: "ci"),
            occurrence: 0
        )

        XCTAssertNil(row.collapsedExitText)
        XCTAssertEqual(row.runDetailText, "Passed · exit 0 · CI")
    }

    func testEmptyCopyDistinguishesNoRunsFromSummaryOnlyEvidence() {
        let none = receiptEmptyCheckDetailsCopy(total: nil, passed: nil, failed: nil)
        let summaryOnly = receiptEmptyCheckDetailsCopy(total: 74, passed: 68, failed: 2)

        XCTAssertEqual(none.title, "No check runs recorded")
        XCTAssertEqual(summaryOnly.title, "No itemized check details recorded")
    }

    func testAggregateAndItemizedConflictsAreNamedWithoutRepairingCounts() {
        let evidence = makeEvidence(
            checks: [makeCheck(name: "one", result: "passed")],
            total: 2,
            passed: 2,
            failed: 1
        )
        let collection = ReceiptCheckCollectionPresentation(evidence: evidence)

        XCTAssertEqual(
            collection.aggregateNotice,
            "Reported passed and failed tallies conflict with the total."
        )
        XCTAssertEqual(
            collection.itemizedNotice,
            "1 itemized entry is available for 2 reported check runs."
        )
    }

    func testMissingAndFutureResultsRemainNeutralOtherOutcomes() {
        let evidence = makeEvidence(checks: [
            makeCheck(name: nil, kind: nil, result: nil, source: "future_source"),
            makeCheck(name: "future", result: "timed_out"),
        ])
        let rows = ReceiptCheckCollectionPresentation(evidence: evidence).rows(in: .other)

        XCTAssertEqual(rows.map(\.title), ["Unnamed check", "future"])
        XCTAssertEqual(rows.map(\.resultLabel), ["Unknown", "Unknown"])
        XCTAssertEqual(rows.first?.sourceLabel, "Future Source")
    }

    private func makeEvidence(
        checks: [ReceiptCheck],
        total: Int? = nil,
        passed: Int? = nil,
        failed: Int? = nil
    ) -> ReceiptEvidenceDim {
        ReceiptEvidenceDim(
            checks: checks,
            checksTotal: total ?? checks.count,
            checksPassed: passed,
            checksFailed: failed,
            provenance: nil,
            gaps: nil
        )
    }

    private func makeCheck(
        name: String?,
        kind: String? = "test",
        result: String?,
        exitCode: Int? = nil,
        scope: String? = nil,
        source: String? = nil,
        superseded: Bool? = nil,
        at: Double? = nil,
        summary: String? = nil,
        files: [String]? = nil,
        commandRedacted: Bool? = nil,
        artifactRef: String? = nil,
        artifactUrl: String? = nil,
        finding: ReceiptCheckFinding? = nil
    ) -> ReceiptCheck {
        ReceiptCheck(
            kind: kind,
            name: name,
            result: result,
            exitCode: exitCode,
            scope: scope,
            source: source,
            superseded: superseded,
            at: at,
            summary: summary,
            files: files,
            commandRedacted: commandRedacted,
            artifactRef: artifactRef,
            artifactUrl: artifactUrl,
            finding: finding
        )
    }
}
