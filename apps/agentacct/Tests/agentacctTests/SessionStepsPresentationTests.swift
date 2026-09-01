import XCTest
@testable import agentacct

final class SessionStepsPresentationTests: XCTestCase {
    func testDigestSeparatesCurrentFailuresFromSupersededHistory() {
        let checks = (0..<23).map { check(id: "pass-\($0)", result: "passed") }
            + [check(id: "live-failure", result: "failed")]
            + [check(id: "old-failure", result: "failed", supersession: "superseded")]

        let digest = StepCheckDigest(checks: checks)

        XCTAssertEqual(digest.currentCount, 24)
        XCTAssertEqual(digest.passedCount, 23)
        XCTAssertEqual(digest.failedCount, 1)
        XCTAssertEqual(digest.historyCount, 1)
        XCTAssertEqual(digest.summary, "23 passed · 1 failed · 1 history")
        XCTAssertEqual(digest.currentPreview.map(\.check.eventId).first, "live-failure")
        XCTAssertEqual(digest.currentPreview.count, 8)
        XCTAssertEqual(digest.hiddenAttentionCount, 0)
        XCTAssertEqual(digest.hiddenOrdinaryCount, 16)
    }

    func testDigestBoundsLargeFailureSetsBeforeOrdinaryChecks() {
        let failures = (0..<10).map { check(id: "failure-\($0)", result: "error") }
        let passes = (0..<20).map { check(id: "pass-\($0)", result: "passed") }

        let digest = StepCheckDigest(checks: passes + failures)

        XCTAssertEqual(digest.attentionPreview.count, 8)
        XCTAssertEqual(digest.currentPreview.map(\.check.eventId), Array(failures.prefix(8)).map(\.eventId))
        XCTAssertEqual(digest.hiddenAttentionCount, 2)
        XCTAssertEqual(digest.hiddenOrdinaryCount, 20)
    }

    func testUnconfirmedFailureRemainsCurrentAndActionable() {
        let digest = StepCheckDigest(checks: [
            check(id: "unconfirmed", result: "failed", supersession: "unconfirmed"),
        ])

        XCTAssertEqual(digest.currentCount, 1)
        XCTAssertEqual(digest.attentionCount, 1)
        XCTAssertEqual(digest.historyCount, 0)
        XCTAssertEqual(digest.summary, "1 failed")
    }

    func testUnknownSupersessionStateRemainsCurrentAndIsDisclosed() {
        let future = check(id: "future", result: "failed", supersession: "future_state")
        let digest = StepCheckDigest(checks: [future])
        let presentation = CheckPresentation(check: future)

        XCTAssertEqual(digest.currentCount, 1)
        XCTAssertEqual(digest.attentionCount, 1)
        XCTAssertEqual(digest.historyCount, 0)
        XCTAssertEqual(presentation.supersessionLabel, "Supersession state unknown")
        XCTAssertTrue(presentation.accessibilitySummary.contains("Supersession state unknown."))
    }

    func testSupersessionWhitespaceUsesOneNormalizedMeaning() {
        let padded = check(id: "padded", result: "failed", supersession: " superseded ")
        let digest = StepCheckDigest(checks: [padded])
        let presentation = CheckPresentation(check: padded)

        XCTAssertEqual(digest.currentCount, 0)
        XCTAssertEqual(digest.historyCount, 1)
        XCTAssertEqual(presentation.supersessionLabel, "Historical, superseded")
        XCTAssertEqual(
            digest.evidenceExplanation(status: "failed", claimedFileCount: 0),
            "failed — receipt reports failure, but no current failing check is visible"
        )
    }

    func testOccurrenceIdentityIsStableForDuplicateAndMissingEventIds() {
        let digest = StepCheckDigest(checks: [
            check(id: "duplicate", result: "passed"),
            check(id: "duplicate", result: "failed"),
            check(id: nil, result: "skipped"),
        ])

        XCTAssertEqual(digest.all.map(\.id), [
            "event:duplicate#1",
            "event:duplicate#2",
            "legacy|1787590000.0|test|skipped|client_hook|Recorded verification result.",
        ])
        XCTAssertEqual(Set(digest.all.map(\.id)).count, 3)
    }

    func testUniqueCheckIdentitySurvivesServerReordering() {
        let first = StepCheckDigest(checks: [
            check(id: "alpha", result: "passed"),
            check(id: "beta", result: "failed"),
        ])
        let reordered = StepCheckDigest(checks: [
            check(id: "beta", result: "failed"),
            check(id: "alpha", result: "passed"),
        ])

        XCTAssertEqual(Set(first.all.map(\.id)), Set(reordered.all.map(\.id)))
        XCTAssertEqual(first.all.first?.id, "event:alpha")
        XCTAssertEqual(reordered.all.last?.id, "event:alpha")
    }

    func testStepIdentityIsStableAndOccurrenceSafe() {
        let first = SessionStepItem.make([
            step(workId: "alpha", title: "First"),
            step(workId: "beta", title: "Second"),
        ])
        let reordered = SessionStepItem.make([
            step(workId: "beta", title: "Second"),
            step(workId: "alpha", title: "First"),
        ])
        let duplicates = SessionStepItem.make([
            step(workId: "alpha", title: "First"),
            step(workId: "alpha", title: "Duplicate"),
        ])

        XCTAssertEqual(Set(first.map(\.id)), Set(reordered.map(\.id)))
        XCTAssertEqual(duplicates.map(\.id), ["work:alpha#1", "work:alpha#2"])
    }

    @MainActor
    func testDuplicateStepTitlesHaveDistinctAccessibilityLabels() {
        let items = SessionStepItem.make([
            step(workId: "alpha", title: "Verify release"),
            step(workId: "alpha", title: "Verify release"),
        ])
        let labels = items.map {
            StepCard(step: $0.step, accessibilityContext: $0.id).accessibilityLabelText
        }

        XCTAssertEqual(Set(labels).count, 2)
        XCTAssertTrue(labels.allSatisfy { $0.hasPrefix("Verify release, work alpha") })
    }

    func testDuplicateSessionTitlesAndRetryExposeDistinctNoncontradictorySemantics() {
        let first = SessionDrillAccessibilityPresentation(
            title: "Review release",
            distinguishingId: "session-a",
            project: "agentacct-gui",
            role: "root",
            sessionKind: nil,
            expanded: true,
            detailSummary: nil,
            loading: true,
            failed: true,
            lastActivity: "2m ago"
        )
        let second = SessionDrillAccessibilityPresentation(
            title: "Review release",
            distinguishingId: "session-b",
            project: "agentacct-gui",
            role: "root",
            sessionKind: nil,
            expanded: true,
            detailSummary: nil,
            loading: false,
            failed: true,
            lastActivity: "3m ago"
        )

        XCTAssertNotEqual(first.label, second.label)
        XCTAssertTrue(first.label.contains("session session-a"))
        XCTAssertTrue(first.label.contains("project agentacct-gui"))
        XCTAssertTrue(first.value.contains("Retrying session steps"))
        XCTAssertFalse(first.value.contains("unavailable"))
        XCTAssertTrue(second.value.contains("Session steps unavailable"))
        XCTAssertFalse(second.value.contains("Loading session steps"))
    }

    func testCheckPresentationUsesTruthfulFallbacksAndExactResultLabels() {
        let unknown = CheckPresentation(
            check: check(id: "unknown", result: nil, summary: nil, source: "future_source")
        )
        let reported = CheckPresentation(
            check: check(id: "reported", result: "passed", source: "mcp_agent_reported")
        )
        let hook = CheckPresentation(
            check: check(id: "hook", result: "error", source: "client_hook")
        )

        XCTAssertEqual(unknown.resultLabel, "Result unknown")
        XCTAssertEqual(unknown.summary, "No summary recorded")
        XCTAssertEqual(unknown.sourceLabel, "source unknown")
        XCTAssertEqual(reported.sourceLabel, "agent-reported")
        XCTAssertEqual(hook.resultLabel, "Error")
        XCTAssertEqual(hook.sourceLabel, "hook")
        for unsupportedHook in ["runtime_hook", "native_hook", "official_hook"] {
            XCTAssertEqual(
                CheckPresentation(check: check(id: unsupportedHook, result: "passed", source: unsupportedHook)).sourceLabel,
                "source unknown"
            )
        }
    }

    func testCheckPresentationFlagsContradictoryExitCodesWithoutRewritingThem() {
        let passedWithFailureExit = CheckPresentation(
            check: check(id: "contradictory-pass", result: "passed", exitCode: 1)
        )
        let failedWithSuccessExit = CheckPresentation(
            check: check(id: "contradictory-failure", result: "failed", exitCode: 0)
        )
        let expected = CheckPresentation(
            check: check(id: "expected", result: "passed", exitCode: 0)
        )

        XCTAssertTrue(passedWithFailureExit.hasInconsistentExitCode)
        XCTAssertTrue(failedWithSuccessExit.hasInconsistentExitCode)
        XCTAssertFalse(expected.hasInconsistentExitCode)
        XCTAssertEqual(passedWithFailureExit.exitLabel, "exit 1")
    }

    func testAccessibilitySummaryIncludesResultSourceExitAndHistoryState() {
        let presentation = CheckPresentation(
            check: check(
                id: "historical",
                result: "failed",
                summary: "The baseline changed.",
                source: "mcp_agent_reported",
                exitCode: 1,
                supersession: "superseded"
            )
        )

        XCTAssertEqual(
            presentation.accessibilitySummary,
            "Test. Failed. The baseline changed. exit 1. agent-reported. Historical, superseded."
        )
    }

    func testResolutionScopeRemainsVisibleAndAccessible() {
        let partial = CheckPresentation(
            check: check(
                id: "partial",
                result: "passed",
                resolutionScope: "partial",
                resolutionSummary: "A narrower rerun passed.",
                resolvesBlockedEventId: "blocked-check",
                files: ["Tests/FocusedTests.swift"],
                artifactRef: "artifacts/focused"
            )
        )
        let historicalFailure = CheckPresentation(
            check: check(
                id: "historical-failure",
                result: "failed",
                supersession: "superseded",
                supersededByEventId: "partial"
            )
        )
        let full = CheckPresentation(
            check: check(id: "full", result: "passed", resolutionScope: "full")
        )

        XCTAssertEqual(partial.resolutionScopeLabel, "Partial resolution")
        XCTAssertEqual(full.resolutionScopeLabel, "Full resolution")
        XCTAssertTrue(partial.accessibilitySummary.contains("Partial resolution."))
        XCTAssertTrue(partial.accessibilitySummary.contains("A narrower rerun passed."))
        XCTAssertTrue(partial.accessibilitySummary.contains("Tests/FocusedTests.swift"))
        XCTAssertTrue(partial.accessibilitySummary.contains("artifacts/focused"))
        XCTAssertNil(historicalFailure.resolutionScopeLabel)
        XCTAssertFalse(historicalFailure.accessibilitySummary.contains("resolution"))
        XCTAssertEqual(historicalFailure.check.supersededByEventId, "partial")
    }

    func testArtifactRedactionFlagsDecodeAndRemainVisibleWithoutRawValues() throws {
        let payload = Data(#"""
        {
            "event_id":"redacted-artifact",
            "evidence_type":"artifact",
            "result":"passed",
            "summary":"A protected artifact was recorded.",
            "artifact_path":null,
            "artifact_url":null,
            "artifact_path_redacted":true,
            "artifact_url_redacted":true
        }
        """#.utf8)

        let decoded = try JSONDecoder().decode(V1Check.self, from: payload)
        let presentation = CheckPresentation(check: decoded)

        XCTAssertTrue(decoded.artifactPathRedacted == true)
        XCTAssertTrue(decoded.artifactUrlRedacted == true)
        XCTAssertNil(decoded.artifactPath)
        XCTAssertNil(decoded.artifactUrl)
        XCTAssertEqual(presentation.artifactRedactionLabel, "Artifact path and URL redacted")
        XCTAssertTrue(presentation.accessibilitySummary.contains("Artifact path and URL redacted."))
        XCTAssertFalse(presentation.accessibilitySummary.contains("artifact_path_redacted"))
    }

    private func check(
        id: String?,
        result: String?,
        summary: String? = "Recorded verification result.",
        source: String? = "client_hook",
        exitCode: Int? = 0,
        supersession: String? = nil,
        supersededByEventId: String? = nil,
        resolutionScope: String? = nil,
        resolutionSummary: String? = nil,
        resolvesBlockedEventId: String? = nil,
        files: [String]? = nil,
        artifactRef: String? = nil,
        artifactPathRedacted: Bool? = nil,
        artifactUrlRedacted: Bool? = nil
    ) -> V1Check {
        V1Check(
            eventId: id,
            createdAt: 1_787_590_000,
            evidenceType: "test",
            result: result,
            summary: summary,
            exitCode: exitCode,
            sourceType: source,
            checkIdentity: id,
            supersessionState: supersession,
            supersededByEventId: supersededByEventId,
            resolutionScope: resolutionScope,
            resolutionSummary: resolutionSummary,
            resolvesBlockedEventId: resolvesBlockedEventId,
            files: files,
            artifactRef: artifactRef,
            artifactPath: nil,
            artifactUrl: nil,
            commandRedacted: nil,
            artifactPathRedacted: artifactPathRedacted,
            artifactUrlRedacted: artifactUrlRedacted
        )
    }

    private func step(workId: String?, title: String) -> V1Step {
        V1Step(
            workId: workId,
            sectionId: nil,
            title: title,
            latestStatus: "completed",
            kind: "test",
            phase: nil,
            startedAt: nil,
            updatedAt: 1_787_590_000,
            summary: nil,
            files: nil,
            blocker: nil,
            nextStep: nil,
            usage: nil,
            joinConfidence: nil,
            evidenceStatus: nil,
            evidenceGrade: nil,
            evidenceGradeReason: nil,
            models: nil,
            checks: nil
        )
    }
}
