import AppKit
import Foundation
import SwiftUI
import XCTest
@testable import agentacct

final class DashboardInteractionTests: XCTestCase {
    @MainActor
    func testDashboardAccessibilityTextIsMateriallyLargerInTheNativeHost() throws {
        func renderedSize(_ dynamicTypeSize: DynamicTypeSize) throws -> CGSize {
            try SnapshotImageWriter.renderedSize(
                Text("Readable dashboard")
                    .dashboardFont(.body)
                    .environment(\.dynamicTypeSize, dynamicTypeSize)
            )
        }

        let medium = try renderedSize(.medium)
        let accessibility5 = try renderedSize(.accessibility5)

        XCTAssertGreaterThan(accessibility5.width, medium.width * 1.7)
        XCTAssertGreaterThan(accessibility5.height, medium.height * 1.7)
    }

    func testDashboardAccessibleLayoutStartsBeforeFixedGeometryBecomesUnsafe() {
        XCTAssertFalse(dashboardUsesAccessibilityLayout(.medium))
        XCTAssertFalse(dashboardUsesAccessibilityLayout(.xLarge))
        XCTAssertTrue(dashboardUsesAccessibilityLayout(.xxLarge))
        XCTAssertTrue(dashboardUsesAccessibilityLayout(.xxxLarge))
        XCTAssertTrue(dashboardUsesAccessibilityLayout(.accessibility1))
        XCTAssertTrue(dashboardUsesAccessibilityLayout(.accessibility3))
        XCTAssertTrue(dashboardUsesAccessibilityLayout(.accessibility5))
    }

    func testAttentionPayloadPreservesCompleteCountsAndRecordedReason() throws {
        let payload = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [
                {
                  "task_id": "task-finding",
                  "title": "Verify dashboard hierarchy",
                  "project": "agentacct-gui",
                  "decision_status": { "key": "finding", "label": "Open finding" },
                  "evidence_strength": {
                    "key": "unchecked",
                    "gradeable": true,
                    "checkable_total": 1,
                    "checked_total": 0,
                    "checks_failed": 1
                  },
                  "cost": {},
                  "primary_root": { "client": "codex", "client_session_id": "session-1" },
                  "attention": {
                    "kind": "failed_check",
                    "summary": "The reference image changed unexpectedly",
                    "next_step": "Inspect the reference and rerun the snapshot check",
                    "observed_at": 1000,
                    "source": "mcp"
                  }
                }
              ],
              "total": 3,
              "counts": { "failed_check": 2, "failed_step": 0, "blocker": 1 },
              "limit": 1,
              "truncated": true
            }
            """
        )

        XCTAssertEqual(payload.schema, "agentacct.v1-attention.v1")
        XCTAssertEqual(payload.total, 3)
        XCTAssertEqual(payload.counts.failedCheck, 2)
        XCTAssertEqual(payload.counts.failedStep, 0)
        XCTAssertEqual(payload.counts.blocker, 1)
        XCTAssertEqual(payload.items.first?.project, "agentacct-gui")
        XCTAssertEqual(payload.items.first?.attention?.kind, "failed_check")
        XCTAssertEqual(
            payload.items.first?.attention?.nextStep,
            "Inspect the reference and rerun the snapshot check"
        )
        XCTAssertTrue(payload.truncated)
    }

    func testShiftBriefUsesServerReasonWithoutInventingRecoveryCopy() throws {
        let task = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "task-finding",
              "title": "Verify dashboard hierarchy",
              "project": "agentacct-gui",
              "decision_status": { "key": "finding", "label": "Open finding" },
              "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
              "cost": {},
              "attention": {
                "kind": "failed_check",
                "summary": "The reference image changed unexpectedly",
                "next_step": null,
                "observed_at": 1000,
                "source": "mcp"
              }
            }
            """
        )

        let focus = try XCTUnwrap(DashboardAttentionItem(task: task))
        XCTAssertEqual(focus.reasonLabel, "Failed check")
        XCTAssertEqual(focus.summary, "The reference image changed unexpectedly")
        XCTAssertNil(focus.nextStep)
        XCTAssertEqual(focus.project, "agentacct-gui")
        XCTAssertEqual(focus.sourceLabel, "MCP record")
    }

    func testReviewBriefContainsOnlyRecordedFactsAndNamesMissingNextStep() throws {
        let task = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "task-finding",
              "title": "Verify dashboard hierarchy",
              "project": "agentacct-gui",
              "decision_status": { "key": "finding" },
              "evidence_strength": { "key": "unchecked" },
              "cost": {},
              "primary_root": { "client": "codex", "client_session_id": "session-1" },
              "attention": {
                "kind": "failed_check",
                "summary": "The reference image changed unexpectedly",
                "next_step": null,
                "observed_at": 1787889600,
                "source": "ci"
              }
            }
            """
        )

        let focus = try XCTUnwrap(DashboardAttentionItem(task: task))
        let brief = DashboardActionBrief(focus: focus)

        XCTAssertEqual(brief.kind, .review)
        XCTAssertEqual(brief.buttonTitle, "Copy review brief")
        XCTAssertEqual(brief.copiedAccessibilityLabel, "Review brief copied")
        XCTAssertEqual(
            brief.text,
            """
            Review brief
            Task: Verify dashboard hierarchy
            Task ID: task-finding
            Project: agentacct-gui
            Agent: codex
            Recorded attention: Failed check — The reference image changed unexpectedly
            Recorded next step: None recorded
            Observed: 2026-08-28T04:00:00Z
            Provenance: External CI or provider
            """
        )
        XCTAssertFalse(brief.text.localizedCaseInsensitiveContains("rerun"))
        XCTAssertFalse(brief.text.localizedCaseInsensitiveContains("resume"))
    }

    func testExpandedProvenanceLabelsPreserveMachineCheckSource() throws {
        let task = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "task-machine-check",
              "decision_status": { "key": "finding" },
              "evidence_strength": { "key": "unchecked" },
              "cost": {},
              "attention": {
                "kind": "failed_check",
                "summary": "Snapshot verification failed",
                "source": "machine"
              }
            }
            """
        )

        let focus = try XCTUnwrap(DashboardAttentionItem(task: task))
        XCTAssertEqual(focus.sourceLabel, "Machine check")
        XCTAssertTrue(DashboardActionBrief(focus: focus).text.contains("Provenance: Machine check"))
    }

    func testAttentionProjectionNormalizesOptionalContextAndOpenProvenance() throws {
        let blankTask = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "task-blank-context",
              "project": "   ",
              "decision_status": { "key": "finding" },
              "evidence_strength": { "key": "unchecked" },
              "cost": {},
              "primary_root": { "client": "  ", "client_session_id": "session-1" },
              "attention": {
                "kind": "failed_check",
                "summary": "Snapshot verification failed",
                "source": "   "
              }
            }
            """
        )
        let blankFocus = try XCTUnwrap(DashboardAttentionItem(task: blankTask))

        XCTAssertNil(blankFocus.project)
        XCTAssertNil(blankFocus.client)
        XCTAssertNil(blankFocus.sourceLabel)
        XCTAssertFalse(DashboardActionBrief(focus: blankFocus).text.contains("Project:"))
        XCTAssertFalse(DashboardActionBrief(focus: blankFocus).text.contains("Agent:"))
        XCTAssertTrue(DashboardActionBrief(focus: blankFocus).text.contains("Provenance: Not recorded"))

        let paddedTask = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "task-padded-context",
              "project": " agentacct-gui ",
              "decision_status": { "key": "finding" },
              "evidence_strength": { "key": "unchecked" },
              "cost": {},
              "primary_root": { "client": " codex ", "client_session_id": "session-2" },
              "attention": {
                "kind": "failed_check",
                "summary": "Snapshot verification failed",
                "source": " custom_provider "
              }
            }
            """
        )
        let paddedFocus = try XCTUnwrap(DashboardAttentionItem(task: paddedTask))

        XCTAssertEqual(paddedFocus.project, "agentacct-gui")
        XCTAssertEqual(paddedFocus.client, "codex")
        XCTAssertEqual(paddedFocus.sourceLabel, "Custom Provider")
    }

    func testUnknownHandoffStateDoesNotImplyRecoveryOrContinuation() throws {
        let task = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "task-legacy",
              "decision_status": { "key": "blocked" },
              "evidence_strength": { "key": "none" },
              "cost": {},
              "attention": { "kind": "blocker", "summary": "Approval is missing" }
            }
            """
        )

        let focus = try XCTUnwrap(DashboardAttentionItem(task: task))
        XCTAssertNil(focus.handedOff)
        XCTAssertEqual(DashboardActionBrief(focus: focus).kind, .review)
        XCTAssertEqual(DashboardActionBrief(focus: focus).buttonTitle, "Copy review brief")
    }

    func testHandedOffAttentionProducesContinuationBriefWithoutRewritingAgentGuidance() throws {
        let task = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "task-handoff",
              "title": "Handoff dashboard polish",
              "decision_status": { "key": "finding" },
              "evidence_strength": { "key": "unchecked" },
              "cost": {},
              "handed_off": true,
              "attention": {
                "kind": "blocker",
                "summary": "Canonical renderer is offline",
                "next_step": "Retry when the renderer is available",
                "source": "mcp"
              }
            }
            """
        )

        let focus = try XCTUnwrap(DashboardAttentionItem(task: task))
        let brief = DashboardActionBrief(focus: focus)

        XCTAssertEqual(brief.kind, .continuation)
        XCTAssertEqual(brief.buttonTitle, "Copy continuation brief")
        XCTAssertEqual(brief.copiedAccessibilityLabel, "Continuation brief copied")
        XCTAssertTrue(brief.text.hasPrefix("Continuation brief\n"))
        XCTAssertTrue(brief.text.contains("Recorded attention: Recorded blocker — Canonical renderer is offline"))
        XCTAssertTrue(brief.text.contains("Recorded next step: Retry when the renderer is available"))
        XCTAssertTrue(brief.text.contains("Observed: Not recorded"))
    }

    @MainActor
    func testActionBriefClipboardBoundaryAndFeedbackStates() {
        let pasteboard = NSPasteboard.withUniqueName()
        defer { pasteboard.releaseGlobally() }

        XCTAssertTrue(DashboardClipboard.copy("recorded brief", to: pasteboard))
        XCTAssertEqual(pasteboard.string(forType: .string), "recorded brief")

        var feedback = DashboardCopyFeedback.idle
        feedback.record(succeeded: true, text: "recorded brief")
        XCTAssertEqual(feedback, .copied("recorded brief"))
        feedback.record(succeeded: false, text: "new brief")
        XCTAssertEqual(feedback, .failed("new brief"))
        feedback.clear()
        XCTAssertEqual(feedback, .idle)
    }

    func testShiftBriefNeverTurnsLoadingUnavailableOrMalformedDataIntoAllClear() throws {
        XCTAssertEqual(
            DashboardAttentionPresentation(payload: nil, error: nil),
            .loading
        )
        XCTAssertEqual(
            DashboardAttentionPresentation(payload: nil, error: nil).dashboardHeadline,
            "Checking recorded work"
        )
        XCTAssertEqual(
            DashboardAttentionPresentation(payload: nil, error: "daemon unavailable"),
            .unavailable("daemon unavailable")
        )
        XCTAssertEqual(
            DashboardAttentionPresentation(payload: nil, error: "daemon unavailable").dashboardHeadline,
            "Review status unavailable"
        )

        let clear = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [],
              "total": 0,
              "counts": { "failed_check": 0, "failed_step": 0, "blocker": 0 },
              "limit": 5,
              "truncated": false
            }
            """
        )
        XCTAssertEqual(
            DashboardAttentionPresentation(payload: clear, error: nil),
            .clear
        )
        XCTAssertEqual(
            DashboardAttentionPresentation(payload: clear, error: nil).dashboardHeadline,
            "No recorded work needs review"
        )
        XCTAssertEqual(
            DashboardAttentionPresentation(payload: clear, error: "refresh failed"),
            .unavailable("refresh failed")
        )

        let nonHeadClear = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [],
              "total": 0,
              "counts": { "failed_check": 0, "failed_step": 0, "blocker": 0 },
              "offset": 5,
              "limit": 5,
              "truncated": false
            }
            """
        )
        XCTAssertEqual(
            DashboardAttentionPresentation(payload: nonHeadClear, error: nil),
            .inconsistent(total: 0),
            "Only the head page can support a complete all-clear claim"
        )

        let falseClear = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [{
                "task_id": "hidden-finding",
                "decision_status": { "key": "finding" },
                "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
                "cost": {},
                "attention": { "kind": "failed_check", "summary": "snapshot failed" }
              }],
              "total": 0,
              "counts": { "failed_check": 1, "failed_step": 0, "blocker": 0 },
              "limit": 5,
              "truncated": false
            }
            """
        )
        XCTAssertEqual(
            DashboardAttentionPresentation(payload: falseClear, error: nil),
            .inconsistent(total: 0)
        )

        let inconsistent = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [],
              "total": 1,
              "counts": { "failed_check": 1, "failed_step": 0, "blocker": 0 },
              "limit": 5,
              "truncated": true
            }
            """
        )
        XCTAssertEqual(
            DashboardAttentionPresentation(payload: inconsistent, error: nil),
            .inconsistent(total: 1)
        )
        XCTAssertEqual(
            DashboardAttentionPresentation(payload: inconsistent, error: nil).dashboardHeadline,
            "Review details unavailable"
        )

        let mismatchedCounts = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [{
                "task_id": "finding",
                "decision_status": { "key": "finding" },
                "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
                "cost": {},
                "attention": { "kind": "failed_check", "summary": "snapshot failed" }
              }],
              "total": 2,
              "counts": { "failed_check": 1, "failed_step": 0, "blocker": 0 },
              "limit": 5,
              "truncated": false
            }
            """
        )
        XCTAssertEqual(
            DashboardAttentionPresentation(payload: mismatchedCounts, error: nil),
            .inconsistent(total: 2)
        )
    }

    func testShiftBriefRejectsInvalidAttentionTaskIdentity() throws {
        let item = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "duplicate",
              "decision_status": { "key": "finding" },
              "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
              "cost": {},
              "attention": { "kind": "failed_check", "summary": "snapshot failed" }
            }
            """
        )
        let duplicateIDs = V1AttentionPayload(
            schema: "agentacct.v1-attention.v1",
            items: [item, item],
            total: 2,
            counts: V1AttentionCounts(failedCheck: 2, failedStep: 0, blocker: 0),
            revision: "duplicate-test",
            offset: 0,
            limit: 5,
            truncated: false
        )

        XCTAssertEqual(
            DashboardAttentionPresentation(payload: duplicateIDs, error: nil),
            .inconsistent(total: 2)
        )
    }

    func testShiftBriefHeadlineUsesTheLeadingRecordedTaskInsteadOfStaticCopy() throws {
        let payload = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [{
                "task_id": "task-finding",
                "title": "Verify dashboard hierarchy",
                "decision_status": { "key": "finding" },
                "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
                "cost": {},
                "attention": { "kind": "failed_check", "summary": "snapshot failed" }
              }],
              "total": 2,
              "counts": { "failed_check": 1, "failed_step": 0, "blocker": 1 },
              "snapshot": "queue-headline",
              "offset": 0,
              "limit": 1,
              "truncated": true
            }
            """
        )

        let presentation = DashboardAttentionPresentation(payload: payload, error: nil)
        XCTAssertEqual(presentation.dashboardHeadline, "Verify dashboard hierarchy")
        XCTAssertEqual(presentation.dashboardStatus, "2 review items")
        XCTAssertFalse(presentation.dashboardStatusIsWarning)
    }

    func testUnavailableShiftBriefDoesNotPromiseThatRefreshCanRepairEveryFailure() {
        let presentation = DashboardAttentionPresentation(
            payload: nil,
            error: DashboardDaemonFeature.attention.upgradeMessage
        )

        XCTAssertEqual(presentation.dashboardStatus, "Unavailable")
        XCTAssertEqual(
            DashboardDaemonFeature.attention.upgradeMessage,
            "Update agentacct, then restart its local service to enable review status."
        )
        XCTAssertEqual(
            DashboardDaemonFeature.ingestion.upgradeMessage,
            "Update agentacct, then restart its local service to enable source status."
        )
    }

    func testSignalRailNeverPresentsRetainedSourceHealthAsCurrentAfterAnError() throws {
        let healthy = try decode(
            V1IngestionSnapshot.self,
            from: """
            {
              "state": "healthy",
              "last_success_at": 1000,
              "issues": []
            }
            """
        )

        XCTAssertEqual(
            DashboardIngestionPresentation(snapshot: healthy, error: "source refresh failed"),
            DashboardIngestionPresentation(
                title: "Source status unavailable",
                detail: "source refresh failed",
                tone: .warning
            )
        )
        XCTAssertEqual(
            DashboardIngestionPresentation(snapshot: healthy, error: nil).title,
            "Sources healthy"
        )
        XCTAssertEqual(
            DashboardIngestionPresentation(snapshot: nil, error: nil),
            DashboardIngestionPresentation(
                title: "Checking source status",
                detail: "Waiting for the current ingestion record.",
                tone: .muted
            )
        )
    }

    func testSourcesPaneAlsoLetsCurrentErrorsOutrankRetainedHealth() {
        XCTAssertEqual(
            SourcesHealthAvailability(hasSnapshot: true, error: "network unavailable"),
            .unavailable("network unavailable")
        )
        XCTAssertEqual(
            SourcesHealthAvailability(
                hasSnapshot: false,
                error: DashboardDaemonFeature.ingestion.upgradeMessage
            ),
            .unavailable(DashboardDaemonFeature.ingestion.upgradeMessage)
        )
        XCTAssertEqual(
            SourcesHealthAvailability(hasSnapshot: true, error: nil),
            .connected
        )
        XCTAssertEqual(
            SourcesHealthAvailability(hasSnapshot: false, error: nil),
            .loading
        )
    }

    func testWorkAttentionEmptyCopyRequiresAnAuthoritativeZero() throws {
        let clear = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [], "total": 0,
              "counts": { "failed_check": 0, "failed_step": 0, "blocker": 0 },
              "limit": 5, "truncated": false
            }
            """
        )
        let inconsistent = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [], "total": 2,
              "counts": { "failed_check": 1, "failed_step": 0, "blocker": 1 },
              "limit": 5, "truncated": true
            }
            """
        )
        let filtered = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [{
                "task_id": "task-finding",
                "decision_status": { "key": "finding" },
                "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
                "cost": {},
                "attention": { "kind": "failed_check", "summary": "snapshot failed" }
              }],
              "total": 2,
              "counts": { "failed_check": 1, "failed_step": 0, "blocker": 1 },
              "limit": 1, "truncated": true
            }
            """
        )

        XCTAssertEqual(WorkAttentionEmptyCopy(payload: clear, query: "").title, "No current review items")
        XCTAssertEqual(
            WorkAttentionEmptyCopy(payload: filtered, query: "visual").title,
            "No review items match this filter"
        )
        XCTAssertEqual(
            WorkAttentionEmptyCopy(
                payload: filtered,
                query: " visual ",
                loadedCount: 2
            ).detail,
            "The loaded queue has 2 of 2 review items; adjust the filter to inspect them."
        )
        XCTAssertEqual(
            WorkAttentionEmptyCopy(payload: inconsistent, query: "visual").title,
            "Review queue details unavailable"
        )
    }

    func testAttentionPagesAppendWithoutChangingCompleteCountsOrRepeatingRows() throws {
        let first = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [{
                "task_id": "failed-check",
                "decision_status": { "key": "finding" },
                "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
                "cost": {},
                "attention": { "kind": "failed_check", "summary": "snapshot failed" }
              }],
              "total": 2,
              "counts": { "failed_check": 1, "failed_step": 0, "blocker": 1 },
              "revision": "revision-1",
              "offset": 0, "limit": 1, "truncated": true
            }
            """
        )
        let next = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [{
                "task_id": "blocker",
                "decision_status": { "key": "blocked" },
                "evidence_strength": { "key": "unchecked" },
                "cost": {},
                "attention": { "kind": "blocker", "summary": "waiting for approval" }
              }],
              "total": 2,
              "counts": { "failed_check": 1, "failed_step": 0, "blocker": 1 },
              "revision": "revision-1",
              "offset": 1, "limit": 1, "truncated": false
            }
            """
        )
        let whitespaceDuplicate = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [{
                "task_id": " failed-check ",
                "decision_status": { "key": "finding" },
                "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
                "cost": {},
                "attention": { "kind": "failed_check", "summary": "snapshot failed" }
              }],
              "total": 2,
              "counts": { "failed_check": 1, "failed_step": 0, "blocker": 1 },
              "revision": "revision-1",
              "offset": 1, "limit": 1, "truncated": false
            }
            """
        )
        let blankIdentity = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [{
                "task_id": "   ",
                "decision_status": { "key": "finding" },
                "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
                "cost": {},
                "attention": { "kind": "failed_check", "summary": "snapshot failed" }
              }],
              "total": 2,
              "counts": { "failed_check": 1, "failed_step": 0, "blocker": 1 },
              "revision": "revision-1",
              "offset": 1, "limit": 1, "truncated": false
            }
            """
        )
        let contradictoryCompletion = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [{
                "task_id": "another-failed-check",
                "decision_status": { "key": "finding" },
                "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
                "cost": {},
                "attention": { "kind": "failed_check", "summary": "another failure" }
              }],
              "total": 2,
              "counts": { "failed_check": 1, "failed_step": 0, "blocker": 1 },
              "revision": "revision-1",
              "offset": 1, "limit": 1, "truncated": false
            }
            """
        )

        XCTAssertEqual(first.resolvedOffset, 0)
        XCTAssertEqual(
            mergedAttentionItems(existing: first.items, summary: first, page: next)?.map(\.taskId),
            ["failed-check", "blocker"]
        )
        XCTAssertNil(
            mergedAttentionItems(existing: first.items, summary: first, page: first),
            "a daemon that ignores offset must fail closed instead of repeating page one"
        )
        XCTAssertNil(
            mergedAttentionItems(existing: first.items, summary: first, page: whitespaceDuplicate),
            "whitespace-equivalent task ids must not appear as additional queue coverage"
        )
        XCTAssertNil(
            mergedAttentionItems(existing: first.items, summary: first, page: blankIdentity),
            "a blank task id must not count as additional queue coverage"
        )
        XCTAssertNil(
            mergedAttentionItems(existing: first.items, summary: first, page: contradictoryCompletion),
            "a complete queue must reconcile its recorded reasons with aggregate counts"
        )

        let loaded = try XCTUnwrap(
            mergedAttentionItems(existing: first.items, summary: first, page: next)
        )
        XCTAssertEqual(
            attentionItemsAfterHeadRefresh(existing: loaded, previous: first, refreshed: first)
                .map(\.taskId),
            ["failed-check", "blocker"],
            "an unchanged minute refresh must preserve pages the user already loaded"
        )
    }

    func testAttentionPagesRejectQueueDriftAndImpossibleContinuation() throws {
        let first = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [{
                "task_id": "failed-check",
                "decision_status": { "key": "finding" },
                "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
                "cost": {},
                "attention": { "kind": "failed_check", "summary": "snapshot failed" }
              }],
              "total": 3,
              "counts": { "failed_check": 2, "failed_step": 0, "blocker": 1 },
              "revision": "revision-1",
              "offset": 0, "limit": 1, "truncated": true
            }
            """
        )
        let changedQueue = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [{
                "task_id": "blocker",
                "decision_status": { "key": "blocked" },
                "evidence_strength": { "key": "unchecked" },
                "cost": {},
                "attention": { "kind": "blocker", "summary": "waiting for approval" }
              }],
              "total": 3,
              "counts": { "failed_check": 2, "failed_step": 0, "blocker": 1 },
              "revision": "revision-2",
              "offset": 1, "limit": 1, "truncated": true
            }
            """
        )
        let emptyMiddlePage = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [],
              "total": 3,
              "counts": { "failed_check": 2, "failed_step": 0, "blocker": 1 },
              "revision": "revision-1",
              "offset": 1, "limit": 1, "truncated": true
            }
            """
        )
        let prematureBlocker = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [{
                "task_id": "blocker",
                "decision_status": { "key": "blocked" },
                "evidence_strength": { "key": "unchecked" },
                "cost": {},
                "attention": { "kind": "blocker", "summary": "waiting for approval" }
              }],
              "total": 3,
              "counts": { "failed_check": 2, "failed_step": 0, "blocker": 1 },
              "revision": "revision-1",
              "offset": 1, "limit": 1, "truncated": true
            }
            """
        )

        XCTAssertNil(
            mergedAttentionItems(existing: first.items, summary: first, page: changedQueue),
            "a changed revision means the server-ranked queue moved between requests"
        )
        XCTAssertNil(
            mergedAttentionItems(existing: first.items, summary: first, page: emptyMiddlePage),
            "a truncated continuation cannot make progress with an empty page"
        )
        XCTAssertNil(
            mergedAttentionItems(existing: first.items, summary: first, page: prematureBlocker),
            "a blocker cannot appear while a reported failure-class row remains unseen"
        )
    }

    @MainActor
    func testAttentionRevisionDriftInvalidatesTheDashboardHead() throws {
        let head = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [{
                "task_id": "failed-check",
                "decision_status": { "key": "finding" },
                "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
                "cost": {},
                "attention": { "kind": "failed_check", "summary": "snapshot failed" }
              }],
              "total": 2,
              "counts": { "failed_check": 1, "failed_step": 0, "blocker": 1 },
              "revision": "revision-1",
              "offset": 0, "limit": 1, "truncated": true
            }
            """
        )
        let driftedPage = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [{
                "task_id": "blocker",
                "decision_status": { "key": "blocked" },
                "evidence_strength": { "key": "unchecked" },
                "cost": {},
                "attention": { "kind": "blocker", "summary": "waiting" }
              }],
              "total": 2,
              "counts": { "failed_check": 1, "failed_step": 0, "blocker": 1 },
              "revision": "revision-2",
              "offset": 1, "limit": 1, "truncated": false
            }
            """
        )

        let store = DashboardStore()
        let generation = store.beginAttentionRequest()
        store.publishAttentionHead(head, requestGeneration: generation)
        store.publishAttentionPage(driftedPage, requestGeneration: generation)

        XCTAssertNil(store.attention)
        XCTAssertTrue(store.attentionQueueItems.isEmpty)
        XCTAssertNil(store.attentionPageError)
        XCTAssertEqual(
            store.attentionError,
            "Review queue changed while loading. Refresh before acting on it."
        )
        XCTAssertEqual(
            DashboardAttentionPresentation(payload: store.attention, error: store.attentionError),
            .unavailable("Review queue changed while loading. Refresh before acting on it.")
        )
    }

    @MainActor
    func testNewerDashboardRequestsWinWhenOlderResponsesArriveLast() throws {
        let oldAttention = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1", "items": [], "total": 0,
              "counts": { "failed_check": 0, "failed_step": 0, "blocker": 0 },
              "revision": "old", "offset": 0, "limit": 5, "truncated": false
            }
            """
        )
        let currentAttention = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1", "items": [], "total": 0,
              "counts": { "failed_check": 0, "failed_step": 0, "blocker": 0 },
              "revision": "current", "offset": 0, "limit": 5, "truncated": false
            }
            """
        )
        let oldReceipts = try decode(
            ReceiptTasksPayload.self,
            from: """
            { "schema": "agentacct.receipt.v1", "tasks": [], "total": 0, "limit": 200, "offset": 0 }
            """
        )
        let currentReceipts = try decode(
            ReceiptTasksPayload.self,
            from: """
            { "schema": "agentacct.receipt.v1", "tasks": [], "total": 4, "limit": 200, "offset": 0 }
            """
        )
        let store = DashboardStore()

        let oldAttentionRequest = store.beginAttentionRequest()
        let currentAttentionRequest = store.beginAttentionRequest()
        store.publishAttentionHead(
            currentAttention,
            requestGeneration: currentAttentionRequest
        )
        store.publishAttentionHead(oldAttention, requestGeneration: oldAttentionRequest)
        XCTAssertEqual(store.attention?.revision, "current")

        let oldReceiptRequest = store.beginReceiptListRequest()
        let currentReceiptRequest = store.beginReceiptListRequest()
        store.publishReceiptList(
            currentReceipts,
            requestGeneration: currentReceiptRequest
        )
        store.publishReceiptList(oldReceipts, requestGeneration: oldReceiptRequest)
        store.publishReceiptListFailure(
            "stale failure",
            requestGeneration: oldReceiptRequest
        )
        XCTAssertEqual(store.totalReceiptTasks, 4)
        XCTAssertNil(store.receiptListError)

        let legacyEmptyReceipts = try decode(
            ReceiptTasksPayload.self,
            from: """
            { "schema": "agentacct.receipt.v1", "tasks": [] }
            """
        )
        let legacyRequest = store.beginReceiptListRequest()
        store.publishReceiptList(legacyEmptyReceipts, requestGeneration: legacyRequest)
        XCTAssertTrue(store.hasLoadedReceiptTasks)
        XCTAssertTrue(store.receiptTasks.isEmpty)
        XCTAssertNil(store.totalReceiptTasks)
    }

    @MainActor
    func testMalformedAttentionHeadsNeverReachTheWorkQueue() throws {
        let item = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "failed-check",
              "decision_status": { "key": "finding" },
              "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
              "cost": {},
              "attention": { "kind": "failed_check", "summary": "snapshot failed" }
            }
            """
        )
        let validLegacy = V1AttentionPayload(
            schema: "agentacct.v1-attention.v1",
            items: [item],
            total: 1,
            counts: V1AttentionCounts(failedCheck: 1, failedStep: 0, blocker: 0),
            revision: nil,
            offset: nil,
            limit: 5,
            truncated: false
        )
        XCTAssertTrue(hasConsistentAttentionHeadEnvelope(validLegacy))

        let legacyTruncated = V1AttentionPayload(
            schema: validLegacy.schema,
            items: [item],
            total: 2,
            counts: V1AttentionCounts(failedCheck: 2, failedStep: 0, blocker: 0),
            revision: nil,
            offset: nil,
            limit: 1,
            truncated: true
        )
        XCTAssertTrue(hasConsistentAttentionHeadEnvelope(legacyTruncated))
        let legacyStore = DashboardStore()
        legacyStore.publishAttentionHead(
            legacyTruncated,
            requestGeneration: legacyStore.beginAttentionRequest()
        )
        XCTAssertTrue(legacyStore.hasMoreAttention)
        XCTAssertFalse(legacyStore.supportsAttentionPaging)
        XCTAssertFalse(legacyStore.canLoadMoreAttention)

        let predecessorHead = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [{
                "task_id": "failed-check",
                "decision_status": { "key": "finding" },
                "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
                "cost": {},
                "attention": { "kind": "failed_check", "summary": "snapshot failed" }
              }],
              "total": 2,
              "counts": { "failed_check": 2, "failed_step": 0, "blocker": 0 },
              "snapshot": "predecessor-queue",
              "offset": 0, "limit": 1, "truncated": true
            }
            """
        )
        XCTAssertEqual(predecessorHead.revision, "predecessor-queue")
        XCTAssertTrue(hasConsistentAttentionHeadEnvelope(predecessorHead))
        let predecessorStore = DashboardStore()
        predecessorStore.publishAttentionHead(
            predecessorHead,
            requestGeneration: predecessorStore.beginAttentionRequest()
        )
        XCTAssertTrue(predecessorStore.canLoadMoreAttention)

        XCTAssertThrowsError(try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1", "items": [], "total": 0,
              "counts": { "failed_check": 0, "failed_step": 0, "blocker": 0 },
              "revision": "new", "snapshot": "old",
              "offset": 0, "limit": 5, "truncated": false
            }
            """
        ))

        let whitespaceEquivalentItem = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": " failed-check ",
              "decision_status": { "key": "finding" },
              "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
              "cost": {},
              "attention": { "kind": "failed_check", "summary": "snapshot failed" }
            }
            """
        )
        let blankSummaryItem = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "blank-summary",
              "decision_status": { "key": "finding" },
              "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
              "cost": {},
              "attention": { "kind": "failed_check", "summary": "   " }
            }
            """
        )
        let unknownKindItem = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "unknown-kind",
              "decision_status": { "key": "finding" },
              "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
              "cost": {},
              "attention": { "kind": "future_kind", "summary": "needs review" }
            }
            """
        )
        let blockerItem = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "blocker-kind",
              "decision_status": { "key": "blocked" },
              "evidence_strength": { "key": "unchecked" },
              "cost": {},
              "attention": { "kind": "blocker", "summary": "waiting" }
            }
            """
        )

        let malformed = [
            V1AttentionPayload(
                schema: validLegacy.schema,
                items: [item],
                total: 2,
                counts: validLegacy.counts,
                revision: "counts-mismatch",
                offset: 0,
                limit: 5,
                truncated: true
            ),
            V1AttentionPayload(
                schema: validLegacy.schema,
                items: [item, item],
                total: 2,
                counts: V1AttentionCounts(failedCheck: 2, failedStep: 0, blocker: 0),
                revision: "duplicate-ids",
                offset: 0,
                limit: 5,
                truncated: false
            ),
            V1AttentionPayload(
                schema: validLegacy.schema,
                items: [item, whitespaceEquivalentItem],
                total: 2,
                counts: V1AttentionCounts(failedCheck: 2, failedStep: 0, blocker: 0),
                revision: "trimmed-duplicate-ids",
                offset: 0,
                limit: 5,
                truncated: false
            ),
            V1AttentionPayload(
                schema: validLegacy.schema,
                items: [whitespaceEquivalentItem],
                total: 1,
                counts: validLegacy.counts,
                revision: "padded-id",
                offset: 0,
                limit: 5,
                truncated: false
            ),
            V1AttentionPayload(
                schema: validLegacy.schema,
                items: [blankSummaryItem],
                total: 1,
                counts: validLegacy.counts,
                revision: "blank-summary",
                offset: 0,
                limit: 5,
                truncated: false
            ),
            V1AttentionPayload(
                schema: validLegacy.schema,
                items: [unknownKindItem],
                total: 1,
                counts: validLegacy.counts,
                revision: "unknown-kind",
                offset: 0,
                limit: 5,
                truncated: false
            ),
            V1AttentionPayload(
                schema: validLegacy.schema,
                items: [blockerItem],
                total: 1,
                counts: validLegacy.counts,
                revision: "kind-count-mismatch",
                offset: 0,
                limit: 5,
                truncated: false
            ),
            V1AttentionPayload(
                schema: validLegacy.schema,
                items: [blockerItem],
                total: 2,
                counts: V1AttentionCounts(failedCheck: 1, failedStep: 0, blocker: 1),
                revision: "blocker-before-failure",
                offset: 0,
                limit: 1,
                truncated: true
            ),
            V1AttentionPayload(
                schema: validLegacy.schema,
                items: [item],
                total: 1,
                counts: validLegacy.counts,
                revision: "nonzero-head",
                offset: 1,
                limit: 5,
                truncated: false
            ),
            V1AttentionPayload(
                schema: validLegacy.schema,
                items: [item],
                total: 1,
                counts: validLegacy.counts,
                revision: "missing-offset",
                offset: nil,
                limit: 5,
                truncated: false
            ),
            V1AttentionPayload(
                schema: validLegacy.schema,
                items: [],
                total: 1,
                counts: validLegacy.counts,
                revision: "empty-truncated-head",
                offset: 0,
                limit: 5,
                truncated: true
            ),
        ]

        let store = DashboardStore()
        for payload in malformed {
            XCTAssertFalse(hasConsistentAttentionHeadEnvelope(payload))
            let generation = store.beginAttentionRequest()
            store.publishAttentionHead(payload, requestGeneration: generation)
            XCTAssertNil(store.attention)
            XCTAssertTrue(store.attentionQueueItems.isEmpty)
            XCTAssertFalse(store.hasMoreAttention)
            XCTAssertEqual(
                store.attentionError,
                "Review status response was inconsistent. Refresh before acting on it."
            )
        }
    }

    @MainActor
    func testDestinationsReplaceStaleDashboardSelection() {
        let cases: [(DashboardDestination, MainPane, String?, String?)] = [
            (.work, .work, nil, nil),
            (.task("task-1"), .work, "task-1", nil),
            (.session("codex::session-1"), .work, nil, "codex::session-1"),
            (.limits, .usage, nil, nil),
            (.sources, .sources, nil, nil),
        ]

        for (destination, pane, taskID, sessionID) in cases {
            let selection = AppSelection()
            selection.taskId = "stale-task"
            selection.sessionId = "stale-session"

            selection.open(destination)

            XCTAssertEqual(selection.pane, pane, "destination: \(destination)")
            XCTAssertEqual(selection.taskId, taskID, "destination: \(destination)")
            XCTAssertEqual(selection.sessionId, sessionID, "destination: \(destination)")
        }
    }

    @MainActor
    func testReviewQueueDestinationSelectsTheBoundedAttentionQueue() {
        let selection = AppSelection()
        selection.workSort = .latest
        selection.workGroup = nil

        selection.open(.reviewQueue)

        XCTAssertEqual(selection.pane, .work)
        XCTAssertNil(selection.taskId)
        XCTAssertNil(selection.sessionId)
        XCTAssertEqual(selection.workGroup, .attention)
        XCTAssertEqual(selection.workSort, .attention)
    }

    @MainActor
    func testTaskDestinationCarriesItsQueueOriginExplicitly() {
        let selection = AppSelection()
        selection.workGroup = .attention

        selection.open(.task("recent-task"))
        XCTAssertNil(selection.workGroup, "Recent work must not inherit a stale review filter")

        selection.open(.attentionTask("review-task"))
        XCTAssertEqual(selection.taskId, "review-task")
        XCTAssertEqual(selection.workGroup, .attention, "Review-item back navigation should return to the queue")
    }

    func testRecentWorkProjectionKeepsDecisionEvidenceAndCostSeparate() throws {
        let task = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "task-1",
              "title": "Build reusable snapshot harness",
              "project": "agentacct-gui",
              "decision_status": { "key": "verified", "label": "Verified" },
              "evidence_strength": {
                "key": "independently_checked",
                "gradeable": true,
                "checkable_total": 4,
                "checked_total": 4
              },
              "cost": {
                "estimated_cost_usd": 4.82,
                "cost_basis": "pricing_table",
                "cost_confidence": "estimated",
                "cost_complete": true
              },
              "primary_root": { "client": "codex", "client_session_id": "session-1" },
              "last_activity_at": 1000
            }
            """
        )

        let item = DashboardWorkItem(task: task)

        XCTAssertEqual(item.title, "Build reusable snapshot harness")
        XCTAssertEqual(item.project, "agentacct-gui")
        XCTAssertEqual(item.client, "codex")
        XCTAssertEqual(item.outcome, "Verified")
        XCTAssertEqual(item.evidence, "4/4 checked")
        XCTAssertEqual(item.cost, "≈$4.82")
        XCTAssertEqual(
            DashboardRecentWorkPresentation(
                items: [item], total: 1, hasLoaded: true, error: "refresh failed"
            ),
            .populated,
            "retained rows remain useful even when the latest refresh fails"
        )
    }

    func testWhitespaceTaskTitleFallsBackToRecordedIdentity() throws {
        let task = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "task-fallback",
              "title": "   ",
              "decision_status": { "key": "finding" },
              "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
              "cost": {},
              "attention": {
                "kind": "failed_check", "summary": "  snapshot failed  ",
                "next_step": "   "
              }
            }
            """
        )

        XCTAssertEqual(
            recordedTaskDisplayTitle(task.title, taskId: task.taskId),
            "task-fallback"
        )
        XCTAssertEqual(DashboardWorkItem(task: task).title, "task-fallback")
        let attention = try XCTUnwrap(DashboardAttentionItem(task: task))
        XCTAssertEqual(attention.title, "task-fallback")
        XCTAssertEqual(attention.summary, "snapshot failed")
        XCTAssertNil(attention.nextStep)
    }

    func testRecentWorkLoadingFailureAndEmptyStatesStayDistinct() {
        XCTAssertEqual(
            DashboardRecentWorkPresentation(
                items: [], total: nil, hasLoaded: false, error: nil
            ),
            .loading
        )
        XCTAssertEqual(
            DashboardRecentWorkPresentation(
                items: [], total: nil, hasLoaded: false, error: "daemon unavailable"
            ),
            .unavailable("daemon unavailable")
        )
        XCTAssertEqual(
            DashboardRecentWorkPresentation(
                items: [], total: 0, hasLoaded: true, error: nil
            ),
            .empty
        )
        XCTAssertEqual(
            DashboardRecentWorkPresentation(
                items: [], total: 4, hasLoaded: true, error: nil
            ),
            .unavailable("The receipt count loaded, but no recent rows were returned.")
        )
        XCTAssertEqual(
            DashboardRecentWorkPresentation(
                items: [], total: 0, hasLoaded: true, error: "refresh failed"
            ),
            .unavailable("refresh failed")
        )
        XCTAssertEqual(
            DashboardRecentWorkPresentation(
                items: [], total: nil, hasLoaded: true, error: nil
            ),
            .empty,
            "a successful legacy response without total is loaded, not perpetual loading"
        )
    }

    func testWorkQueryIncludesRecordedProjectContext() throws {
        let task = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "task-1",
              "title": "Review snapshots",
              "project": "agentacct-gui",
              "decision_status": { "key": "finding" },
              "evidence_strength": { "key": "unchecked" },
              "cost": {},
              "primary_root": { "client": "codex", "client_session_id": "session-1" }
            }
            """
        )

        XCTAssertTrue(receiptMatchesWorkQuery(task, query: "agentacct-gui"))
        XCTAssertEqual(receiptProjectContext(task), "agentacct-gui")
        XCTAssertTrue(receiptMatchesWorkQuery(task, query: "CODEX"))
        XCTAssertTrue(receiptMatchesWorkQuery(task, query: "   "))
        XCTAssertFalse(hasWorkQuery(" \n\t "))
        XCTAssertFalse(receiptMatchesWorkQuery(task, query: "another-project"))

        let blankProject = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "task-2", "project": "   ",
              "decision_status": { "key": "reported" },
              "evidence_strength": { "key": "none" },
              "cost": { "estimated_cost_usd": 99 },
              "primary_root": { "client": "codex", "client_session_id": "session-2" }
            }
            """
        )
        XCTAssertNil(DashboardWorkItem(task: blankProject).project)
        XCTAssertNil(receiptProjectContext(blankProject))
        XCTAssertEqual(
            workTableRows(
                receipts: [task, blankProject],
                attention: [task, blankProject],
                group: .attention,
                query: "",
                sort: .cost
            ).map(\.taskId),
            ["task-1", "task-2"],
            "loaded attention pages must retain the server's whole-store ranking"
        )
        XCTAssertEqual(
            workTableRows(
                receipts: [task, blankProject],
                attention: [],
                group: nil,
                query: "",
                sort: .cost
            ).map(\.taskId),
            ["task-2", "task-1"]
        )
    }

    func testWorkFooterNamesLoadedSearchScopeAndRetainedErrors() {
        XCTAssertEqual(
            workReceiptFooterText(
                visibleCount: 2,
                loadedCount: 200,
                totalCount: 500,
                query: "dashboard",
                sort: .latest
            ),
            "2 match filter in latest 200 loaded · 500 total receipts · most recent first"
        )
        XCTAssertEqual(
            retainedWorkListWarning(error: "refresh failed", visibleCount: 4),
            "Showing last loaded receipts · refresh failed"
        )
        XCTAssertNil(retainedWorkListWarning(error: "refresh failed", visibleCount: 0))
    }

    func testRecentWorkCostLabelsDoNotClaimUnknownCompleteness() throws {
        let cases = [
            (
                cost: #"{"estimated_cost_usd": 4.82, "cost_confidence": "client_reported", "cost_complete": true}"#,
                expected: "$4.82"
            ),
            (
                cost: #"{"estimated_cost_usd": 4.82, "cost_confidence": "client_reported", "cost_complete": false}"#,
                expected: "~$4.82"
            ),
            (
                cost: #"{"estimated_cost_usd": 4.82, "cost_confidence": "client_reported"}"#,
                expected: "≈$4.82"
            ),
            (cost: #"{}"#, expected: "—"),
        ]

        for (index, testCase) in cases.enumerated() {
            let task = try decode(
                ReceiptSummary.self,
                from: """
                {
                  "task_id": "task-\(index)",
                  "decision_status": { "key": "verified" },
                  "evidence_strength": { "key": "none" },
                  "cost": \(testCase.cost)
                }
                """
            )

            XCTAssertEqual(DashboardWorkItem(task: task).cost, testCase.expected)
        }
    }

    func testUsageSeriesFormatsAvailableAndMissingValuesHonestly() throws {
        let periods = try decode(
            [PeriodBucket].self,
            from: """
            [
              { "period": "2026-08-24", "fresh_tokens": 1200000,
                "estimated_cost_usd": 2.50, "cost_complete": true },
              { "period": "2026-08-25", "fresh_tokens": 300000,
                "estimated_cost_usd": 1.25, "cost_complete": true },
              { "period": "2026-08-26" }
            ]
            """
        )
        let completePeriods = Array(periods.prefix(2))

        XCTAssertEqual(DashboardUsageSeries.tokens.valueText(for: periods[0]), "1.2M")
        XCTAssertEqual(DashboardUsageSeries.cost.valueText(for: periods[0]), "≈$2.50")
        XCTAssertEqual(DashboardUsageSeries.cost.valueText(for: periods[2]), "—")
        XCTAssertEqual(DashboardUsageSeries.tokens.valueText(for: periods[2]), "—")
        XCTAssertEqual(DashboardUsageSeries.tokens.totalText(for: completePeriods), "1.5M total")
        XCTAssertEqual(DashboardUsageSeries.cost.totalText(for: completePeriods), "≈$3.75 total")
        XCTAssertEqual(DashboardUsageSeries.tokens.totalText(for: periods), "~1.5M total")
        XCTAssertEqual(DashboardUsageSeries.cost.totalText(for: periods), "~$3.75 total")
        XCTAssertEqual(DashboardUsageSeries.tokens.totalText(for: [periods[2]]), "—")
        XCTAssertEqual(DashboardUsageSeries.cost.totalText(for: [periods[2]]), "—")
    }

    func testUsageSeriesFormatsExtremeTokenValuesWithoutOverflow() throws {
        let periods = try decode(
            [PeriodBucket].self,
            from: """
            [
              { "period": "2026-08-24", "fresh_tokens": 9223372036854775807 },
              { "period": "2026-08-25", "fresh_tokens": 9223372036854775807 }
            ]
            """
        )

        XCTAssertEqual(DashboardUsageSeries.tokens.totalText(for: periods), "2e19 total")
        let axisText = DashboardUsageSeries.tokens.axisText(for: Double(Int.max))
        XCTAssertEqual(axisText, "9e18")
        XCTAssertLessThanOrEqual(axisText.count, 5)
        XCTAssertEqual(
            DashboardUsageSeries.tokens.axisText(for: 999_900_000_000_000),
            "1000T"
        )
        XCTAssertEqual(UsageTotals.compact(Int.min), "-9e18")
        XCTAssertEqual(UsageTotals.compact(999.9), "999")
    }

    func testUsageSeriesAndPulseRejectNegativeHistoricalTokens() throws {
        let periods = try decode(
            [PeriodBucket].self,
            from: """
            [
              { "period": "2026-08-23", "fresh_tokens": -5 },
              { "period": "2026-08-24", "fresh_tokens": 1200000 },
              { "period": "2026-08-25", "fresh_tokens": 300000 }
            ]
            """
        )

        XCTAssertEqual(DashboardUsageSeries.tokens.value(for: periods[0]), 0)
        XCTAssertEqual(DashboardUsageSeries.tokens.valueText(for: periods[0]), "—")
        XCTAssertEqual(DashboardUsageSeries.tokens.totalText(for: periods), "~1.5M total")
        XCTAssertEqual(usagePulse(periods: periods).state, .insufficient)
        XCTAssertEqual(usagePulse(periods: periods).title, "Usage comparison incomplete")
    }

    func testUsageSeriesRejectsNegativeCostsAndKeepsAxisLabelsCompact() throws {
        let periods = try decode(
            [PeriodBucket].self,
            from: """
            [
              { "period": "2026-08-24", "estimated_cost_usd": -5 },
              { "period": "2026-08-25", "estimated_cost_usd": 12.34 }
            ]
            """
        )

        XCTAssertEqual(DashboardUsageSeries.cost.value(for: periods[0]), 0)
        XCTAssertEqual(DashboardUsageSeries.cost.valueText(for: periods[0]), "—")
        XCTAssertEqual(DashboardUsageSeries.cost.totalText(for: periods), "~$12.34 total")
        XCTAssertEqual(DashboardUsageSeries.cost.axisText(for: 9.999), "$9.99")
        XCTAssertEqual(DashboardUsageSeries.cost.axisText(for: 12.34), "$12")
        XCTAssertEqual(DashboardUsageSeries.cost.axisText(for: 999_999), "$999k")
        XCTAssertEqual(DashboardUsageSeries.cost.axisText(for: 9e18), "$9e18")

        let overflowingTotal = try decode(
            [PeriodBucket].self,
            from: """
            [
              { "period": "2026-08-24", "estimated_cost_usd": 1.7976931348623157e308 },
              { "period": "2026-08-25", "estimated_cost_usd": 1.7976931348623157e308 }
            ]
            """
        )
        XCTAssertEqual(DashboardUsageSeries.cost.totalText(for: overflowingTotal), "—")
    }

    func testActiveWorkIncludesOnlyRunningStates() {
        let cases: [(status: String?, isActive: Bool)] = [
            ("started", true),
            ("checkpoint", true),
            ("in_progress", true),
            ("blocked", false),
            ("handed_off", false),
            ("completed", false),
            (nil, false),
        ]

        for testCase in cases {
            XCTAssertEqual(
                isActiveWorkStatus(testCase.status),
                testCase.isActive,
                "status: \(testCase.status ?? "nil")"
            )
        }
    }

    func testActiveWorkSignalNamesRecordedInactivityWithoutCallingItStalled() throws {
        let sessions = try decode(
            [RecentSession].self,
            from: """
            [
              {
                "client": "codex", "session_id": "quiet",
                "title": "Snapshot harness", "status": "in_progress",
                "last_activity_at": 100
              },
              {
                "client": "codex", "session_id": "recent",
                "title": "Dashboard hierarchy", "status": "checkpoint",
                "last_activity_at": 990
              },
              {
                "client": "codex", "session_id": "unknown-time",
                "title": "Unknown timestamp", "status": "in_progress"
              }
            ]
            """
        )

        let signal = DashboardActiveWorkSignal(
            sessions: sessions,
            availability: .connected,
            now: Date(timeIntervalSince1970: 1_000)
        )

        XCTAssertEqual(signal.title, "One session last active 15m ago")
        XCTAssertEqual(signal.detail, "Snapshot harness · 3 recent active sessions shown")
        XCTAssertTrue(signal.promotesInactivity)
        XCTAssertTrue(signal.hasConfirmedActiveWork)
        XCTAssertFalse(signal.title.localizedCaseInsensitiveContains("stalled"))
    }

    func testActiveWorkSignalKeepsLoadingEmptyAndMissingTimeDistinct() throws {
        XCTAssertEqual(
            DashboardActiveWorkSignal(
                sessions: [], availability: .loading,
                now: Date(timeIntervalSince1970: 1_000)
            ).title,
            "Checking active work"
        )
        XCTAssertEqual(
            DashboardActiveWorkSignal(
                sessions: [], availability: .connected,
                now: Date(timeIntervalSince1970: 1_000)
            ).title,
            "No recent agent activity"
        )

        let missingTime = try decode(
            [RecentSession].self,
            from: """
            [{ "client": "codex", "session_id": "unknown", "status": "in_progress" }]
            """
        )
        let signal = DashboardActiveWorkSignal(
            sessions: missingTime,
            availability: .connected,
            now: Date(timeIntervalSince1970: 1_000)
        )
        XCTAssertEqual(signal.title, "1 active session shown")
        XCTAssertEqual(signal.detail, "Activity time unavailable for the recorded session.")
        XCTAssertFalse(signal.promotesInactivity)
        XCTAssertTrue(signal.hasConfirmedActiveWork)

        let emptyTitle = try decode(
            [RecentSession].self,
            from: """
            [{
              "client": "codex", "session_id": "unknown", "title": "  ",
              "status": "in_progress", "last_activity_at": 990
            }]
            """
        )
        XCTAssertEqual(
            DashboardActiveWorkSignal(
                sessions: emptyTitle,
                availability: .connected,
                now: Date(timeIntervalSince1970: 1_000)
            ).detail,
            "codex · unknown · activity 10s ago"
        )
    }

    func testStatuslessUsageActivityNeverBecomesANoActiveWorkClaim() throws {
        let sessions = try decode(
            [RecentSession].self,
            from: """
            [
              {
                "client": "codex", "session_id": "usage-only",
                "last_activity_at": 990
              },
              {
                "client": "claude-code", "session_id": "usage-only-2",
                "last_activity_at": 980
              }
            ]
            """
        )

        let signal = DashboardActiveWorkSignal(
            sessions: sessions,
            availability: .connected,
            now: Date(timeIntervalSince1970: 1_000)
        )

        XCTAssertEqual(signal.title, "Work status unavailable")
        XCTAssertEqual(
            signal.detail,
            "codex · usage-on · activity 10s ago · 2/2 shown with no work status"
        )
        XCTAssertFalse(signal.promotesInactivity)
        XCTAssertFalse(signal.hasConfirmedActiveWork)
    }

    func testActiveWorkCountsRemainQualifiedAtTheEightRowGlanceBound() {
        let sessions = (0 ..< 8).map { index in
            RecentSession(
                client: "codex",
                sessionId: "session-\(index)",
                title: nil,
                status: "in_progress",
                lastActivityAt: 990 - Double(index),
                planPct: nil
            )
        }

        let signal = DashboardActiveWorkSignal(
            sessions: sessions,
            availability: .connected,
            now: Date(timeIntervalSince1970: 1_000)
        )

        XCTAssertEqual(signal.title, "8 active sessions shown")
        XCTAssertTrue(signal.hasConfirmedActiveWork)
    }

    func testActiveWorkSignalNamesMixedUnknownStatusCoverage() {
        let active = RecentSession(
            client: "codex",
            sessionId: "active",
            title: nil,
            status: "in_progress",
            lastActivityAt: 990,
            planPct: nil
        )
        let unknown = RecentSession(
            client: "claude-code",
            sessionId: "unknown",
            title: nil,
            status: nil,
            lastActivityAt: 985,
            planPct: nil
        )

        let signal = DashboardActiveWorkSignal(
            sessions: [active, unknown],
            availability: .connected,
            now: Date(timeIntervalSince1970: 1_000)
        )

        XCTAssertEqual(signal.title, "1 active session shown")
        XCTAssertTrue(signal.detail.hasSuffix("1 more shown without work status"))
        XCTAssertTrue(signal.hasConfirmedActiveWork)
    }

    func testAgentPlanRowCopyRequiresARealSevenDayWindow() throws {
        // The per-agent row must never fabricate a meter or a reset time: a
        // 5h-only client says so, a limit-less client says so, and only a
        // provider-reported 7d percent produces a meter value.
        let fiveHourOnly = try decode(
            LimitEntry.self,
            from: """
            {
              "client": "codex",
              "plan_type": "pro",
              "windows": [{ "kind": "5h", "used_percent": 31 }]
            }
            """
        )
        let sevenDay = try decode(
            LimitEntry.self,
            from: """
            {
              "client": "codex",
              "plan_type": "pro",
              "windows": [{ "kind": "7d", "used_percent": 39 }]
            }
            """
        )

        let noLimit = DashboardAgentPlanRow(client: "hermes", limit: nil, plan: nil, usage: nil)
        XCTAssertNil(noLimit.usedPercent)
        XCTAssertEqual(noLimit.meterCaption, "no limits reported")
        XCTAssertNil(noLimit.usageText)

        // A stale reading is hidden, not never-reported — the copy must not lie.
        let stale = DashboardAgentPlanRow(
            client: "claude-code", limit: nil, staleLimit: true, plan: nil, usage: nil
        )
        XCTAssertNil(stale.usedPercent)
        XCTAssertEqual(stale.meterCaption, "limit reading stale — see Usage")

        let unavailable = DashboardAgentPlanRow(client: "codex", limit: fiveHourOnly, plan: nil, usage: nil)
        XCTAssertNil(unavailable.usedPercent)
        XCTAssertEqual(unavailable.meterCaption, "no 7-day window reported")

        let available = DashboardAgentPlanRow(client: "codex", limit: sevenDay, plan: nil, usage: nil)
        XCTAssertEqual(available.usedPercent, 39)
        XCTAssertEqual(available.meterCaption, "39% of 7-day limit")
        XCTAssertNil(available.resetText, "no reset time was reported — never fabricated")
        XCTAssertEqual(available.detailText, "39% of 7-day limit · provider reported")
        XCTAssertEqual(available.decisionTitle, "codex · 61% headroom")

        let exceeded = try decode(
            LimitEntry.self,
            from: """
            { "client": "codex", "windows": [{ "kind": "7d", "used_percent": 104 }] }
            """
        )
        let exceededRow = DashboardAgentPlanRow(client: "codex", limit: exceeded, plan: nil, usage: nil)
        XCTAssertEqual(exceededRow.decisionTitle, "codex · limit exceeded")

        let invalid = try decode(
            LimitEntry.self,
            from: """
            { "client": "codex", "windows": [{ "kind": "7d", "used_percent": -3 }] }
            """
        )
        let invalidRow = DashboardAgentPlanRow(client: "codex", limit: invalid, plan: nil, usage: nil)
        XCTAssertNil(invalidRow.usedPercent)
        XCTAssertEqual(invalidRow.decisionTitle, "codex")

        let missingPercent = try decode(
            LimitEntry.self,
            from: """
            { "client": "codex", "windows": [{ "kind": "7d" }] }
            """
        )
        let missingRow = DashboardAgentPlanRow(client: "codex", limit: missingPercent, plan: nil, usage: nil)
        XCTAssertEqual(missingRow.meterCaption, "7-day usage not reported")
    }

    func testUsagePulseComparesCompletedRecordedBucketsWithoutAnomalyLanguage() throws {
        let periods = try decode(
            [PeriodBucket].self,
            from: """
            [
              { "period": "2026-08-23", "fresh_tokens": 4800000 },
              { "period": "2026-08-24", "fresh_tokens": 12100000 },
              { "period": "2026-08-25", "fresh_tokens": 22500000 }
            ]
            """
        )

        let pulse = usagePulse(periods: periods)
        XCTAssertEqual(pulse.state, .ready)
        XCTAssertEqual(pulse.title, "Fresh tokens 86% higher")
        XCTAssertEqual(
            pulse.detail,
            "22.5M on 2026-08-25 · 12.1M on 2026-08-24 · client reported"
        )
        XCTAssertFalse(pulse.title.localizedCaseInsensitiveContains("anomaly"))
        XCTAssertFalse(pulse.title.localizedCaseInsensitiveContains("caused"))
    }

    func testUsagePulseGuardsLoadingErrorsSparseHistoryAndZeroBaselines() throws {
        XCTAssertEqual(usagePulse(periods: nil, isLoaded: false).state, .loading)
        XCTAssertEqual(usagePulse(periods: nil).title, "Usage history not reported")
        XCTAssertEqual(
            usagePulse(periods: nil, isLoaded: false, error: "usage unavailable").title,
            "Usage comparison unavailable"
        )

        let sparse = try decode(
            [PeriodBucket].self,
            from: """
            [{ "period": "2026-08-25", "fresh_tokens": 10 }]
            """
        )
        XCTAssertEqual(usagePulse(periods: sparse).state, .insufficient)
        XCTAssertEqual(
            usagePulse(periods: sparse, error: "refresh failed").state,
            .unavailable,
            "a refresh error must not leave an old comparison looking current"
        )

        let zeroBaseline = try decode(
            [PeriodBucket].self,
            from: """
            [
              { "period": "2026-08-24", "fresh_tokens": 0 },
              { "period": "2026-08-25", "fresh_tokens": 1200 }
            ]
            """
        )
        XCTAssertEqual(
            usagePulse(periods: zeroBaseline).title,
            "Fresh tokens rose from 0"
        )

        let invalid = try decode(
            [PeriodBucket].self,
            from: """
            [
              { "period": "2026-08-24", "fresh_tokens": -1 },
              { "period": "2026-08-25", "fresh_tokens": 1200 }
            ]
            """
        )
        XCTAssertEqual(usagePulse(periods: invalid).state, .insufficient)

        let duplicateDate = try decode(
            [PeriodBucket].self,
            from: """
            [
              { "period": "2026-08-25", "fresh_tokens": 1200 },
              { "period": "2026-08-25", "fresh_tokens": 1400 }
            ]
            """
        )
        XCTAssertEqual(
            usagePulse(periods: duplicateDate).title,
            "Usage comparison ambiguous"
        )

        let malformedDate = try decode(
            [PeriodBucket].self,
            from: """
            [
              { "period": "2026-02-30", "fresh_tokens": 1200 },
              { "period": "latest", "fresh_tokens": 1400 }
            ]
            """
        )
        XCTAssertEqual(usagePulse(periods: malformedDate).state, .insufficient)
    }

    func testUsagePulseNamesPartialDailyAndWeeklyBucketsWithoutComparingThem() throws {
        let daily = try decode(
            [PeriodBucket].self,
            from: """
            [
              { "period": "2026-08-24", "fresh_tokens": 12100000 },
              { "period": "2026-08-25", "fresh_tokens": 22500000 }
            ]
            """
        )
        let dailyPulse = usagePulse(
            periods: daily,
            now: Date(timeIntervalSince1970: 1_787_659_200)
        )
        XCTAssertEqual(dailyPulse.title, "Today so far · 22.5M")
        XCTAssertEqual(dailyPulse.detail, "12.1M on 2026-08-24 · client reported")

        let weekly = try decode(
            [PeriodBucket].self,
            from: """
            [
              { "period": "2026-08-17", "fresh_tokens": 12100000 },
              { "period": "2026-08-24", "fresh_tokens": 22500000 }
            ]
            """
        )
        let weeklyPulse = usagePulse(
            periods: weekly,
            rangeDays: 90,
            now: Date(timeIntervalSince1970: 1_787_832_000)
        )
        XCTAssertEqual(weeklyPulse.title, "This week so far · 22.5M")
        XCTAssertEqual(
            weeklyPulse.detail,
            "12.1M in week of 2026-08-17 · client reported"
        )
    }

    func testUsagePulseFailsClosedOnNewestInvalidBucketAndBoundsExtremeRatios() throws {
        let newestInvalid = try decode(
            [PeriodBucket].self,
            from: """
            [
              { "period": "2026-08-23", "fresh_tokens": 10 },
              { "period": "2026-08-24", "fresh_tokens": 20 },
              { "period": "2026-08-25", "fresh_tokens": -1 }
            ]
            """
        )
        XCTAssertEqual(usagePulse(periods: newestInvalid).title, "Usage comparison incomplete")

        let malformedNewest = try decode(
            [PeriodBucket].self,
            from: """
            [
              { "period": "2026-08-23", "fresh_tokens": 10 },
              { "period": "2026-08-24", "fresh_tokens": 20 },
              { "period": "latest", "fresh_tokens": 30 }
            ]
            """
        )
        XCTAssertEqual(usagePulse(periods: malformedNewest).title, "Usage comparison ambiguous")

        let extreme = try decode(
            [PeriodBucket].self,
            from: """
            [
              { "period": "2026-08-24", "fresh_tokens": 1 },
              { "period": "2026-08-25", "fresh_tokens": 9223372036854775807 }
            ]
            """
        )
        XCTAssertEqual(usagePulse(periods: extreme).title, "Fresh tokens >999% higher")

        let future = try decode(
            [PeriodBucket].self,
            from: """
            [
              { "period": "2026-08-24", "fresh_tokens": 10 },
              { "period": "2026-08-25", "fresh_tokens": 20 }
            ]
            """
        )
        XCTAssertEqual(
            usagePulse(
                periods: future,
                now: Date(timeIntervalSince1970: 1_787_572_800)
            ).title,
            "Usage history is ahead of local time"
        )
    }

    func testAgentPlanRowsIncludeEveryRecordingClientWithoutFavoritism() throws {
        // Regression for the single-meter dashboard: every non-stale limit
        // client AND every usage-only client gets a row — limit clients first
        // by least headroom, usage-only clients after in cube order.
        let claude = try decode(
            LimitEntry.self,
            from: """
            { "client": "claude-code", "windows": [{ "kind": "7d", "used_percent": 47 }] }
            """
        )
        let codex = try decode(
            LimitEntry.self,
            from: """
            { "client": "codex", "plan_type": "pro", "windows": [{ "kind": "7d", "used_percent": 5 }] }
            """
        )
        let usage = try decode(
            [GlanceClientUsage].self,
            from: """
            [
              { "client": "claude-code", "fresh_tokens": 5140000, "estimated_cost_usd": 1200.5,
                "cost_complete": true, "cost_confidence": "estimated_from_tokens" },
              { "client": "codex", "fresh_tokens": 17670000, "estimated_cost_usd": 310.9,
                "cost_complete": true, "cost_confidence": "estimated_from_tokens" },
              { "client": "hermes", "fresh_tokens": 145000, "estimated_cost_usd": 1.0,
                "cost_complete": true, "cost_confidence": "estimated_from_tokens" }
            ]
            """
        )
        let rows = DashboardAgentPlanRow.rows(limits: [codex, claude], planClients: [], usage: usage)
        XCTAssertEqual(rows.map(\.client), ["claude-code", "codex", "hermes"])
        XCTAssertEqual(rows[0].usedPercent, 47)
        XCTAssertEqual(rows[1].planType, "pro")
        // The usage-only client keeps the honest hatched-track copy.
        XCTAssertNil(rows[2].usedPercent)
        XCTAssertEqual(rows[2].meterCaption, "no limits reported")
        XCTAssertNotNil(rows[2].usageText)
        // Every usage figure is anchored to the card's 7-day window.
        XCTAssertTrue(rows[2].usageText?.hasPrefix("7d · ") == true)

        let invalidDuplicate = try decode(
            LimitEntry.self,
            from: """
            { "client": "codex", "windows": [{ "kind": "7d", "used_percent": -4 }] }
            """
        )
        let deduplicated = DashboardAgentPlanRow.rows(
            limits: [invalidDuplicate, codex], planClients: [], usage: usage
        )
        XCTAssertEqual(
            deduplicated.first(where: { $0.client == "codex" })?.usedPercent,
            5,
            "an invalid duplicate must not hide a valid provider reading"
        )

        let staleOnly = DashboardAgentPlanRow.rows(
            limits: [],
            staleClients: ["claude-code"],
            planClients: [],
            usage: []
        )
        XCTAssertEqual(staleOnly.map(\.client), ["claude-code"])
        XCTAssertEqual(staleOnly[0].meterCaption, "limit reading stale — see Usage")

        let liveShortWindow = try decode(
            LimitEntry.self,
            from: """
            { "client": "codex", "windows": [{ "kind": "5h", "used_percent": 12 }] }
            """
        )
        let staleSevenDay = try decode(
            LimitEntry.self,
            from: """
            {
              "client": "codex", "stale": true,
              "windows": [{ "kind": "7d", "used_percent": 34 }]
            }
            """
        )
        let mixedStaleClients = staleSevenDayLimitClients(
            in: [liveShortWindow, staleSevenDay]
        )
        XCTAssertEqual(mixedStaleClients, Set(["codex"]))
        let mixedWindowRows = DashboardAgentPlanRow.rows(
            limits: [liveShortWindow],
            staleClients: mixedStaleClients,
            planClients: [],
            usage: []
        )
        XCTAssertEqual(mixedWindowRows[0].meterCaption, "limit reading stale — see Usage")
    }

    func testActiveSessionResolutionNeverDropsAnUnmatchedSession() throws {
        let task = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "task-1",
              "decision_status": { "key": "verified" },
              "evidence_strength": { "key": "none" },
              "cost": {},
              "primary_root": { "client": "codex", "client_session_id": "root" }
            }
            """
        )

        XCTAssertEqual(
            workSessionResolution(for: "codex::root", in: [task]),
            .task("task-1")
        )
        XCTAssertEqual(
            workSessionResolution(for: "codex::subagent", in: [task]),
            .unresolved("codex::subagent")
        )
    }

    func testWorkRecordUsesReferenceWidthForSideBySideEvidence() {
        XCTAssertEqual(workRecordColumnMode(for: 823), .stacked)
        XCTAssertEqual(workRecordColumnMode(for: 824), .sideBySide)
        XCTAssertEqual(workRecordColumnMode(for: 860), .sideBySide)
    }

    @MainActor
    func testLocalDataFreshnessUsesTheSnapshotClock() {
        SnapshotMode.setFixtureDate(Date(timeIntervalSince1970: 1_000))
        defer { SnapshotMode.setFixtureDate(nil) }

        XCTAssertEqual(
            dashboardFreshnessText(Date(timeIntervalSince1970: 1_000)),
            "just now"
        )
        XCTAssertEqual(
            dashboardFreshnessText(Date(timeIntervalSince1970: 880)),
            "2m ago"
        )
        XCTAssertEqual(
            dashboardFreshnessText(Date(timeIntervalSince1970: 1_001)),
            "time unavailable"
        )
    }

    func testWindowMaterialRespectsAccessibilityAndSnapshotDeterminism() {
        let cases = [
            (reduceTransparency: false, snapshotMode: false, expected: true),
            (reduceTransparency: true, snapshotMode: false, expected: false),
            (reduceTransparency: false, snapshotMode: true, expected: false),
            (reduceTransparency: true, snapshotMode: true, expected: false),
        ]

        for testCase in cases {
            XCTAssertEqual(
                WindowSurfacePolicy.usesMaterial(
                    reduceTransparency: testCase.reduceTransparency,
                    snapshotMode: testCase.snapshotMode
                ),
                testCase.expected,
                "reduceTransparency: \(testCase.reduceTransparency), "
                    + "snapshotMode: \(testCase.snapshotMode)"
            )
        }
    }

    private func decode<Value: Decodable>(_ type: Value.Type, from json: String) throws -> Value {
        try JSONDecoder().decode(type, from: Data(json.utf8))
    }

    private func usagePulse(
        periods: [PeriodBucket]?,
        isLoaded: Bool = true,
        rangeDays: Int = 7,
        error: String? = nil,
        now: Date = Date(timeIntervalSince1970: 1_787_745_600)
    ) -> DashboardUsagePulse {
        return DashboardUsagePulse(
            periods: periods,
            isLoaded: isLoaded,
            rangeDays: rangeDays,
            error: error,
            now: now,
            timeZone: TimeZone(secondsFromGMT: 0)!
        )
    }
}
