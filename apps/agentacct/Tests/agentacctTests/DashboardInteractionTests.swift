import AppKit
import Foundation
import XCTest
@testable import agentacct

final class DashboardInteractionTests: XCTestCase {
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
                "reason_label": "Failed check",
                "label": "Failed check",
                "summary": "The reference image changed unexpectedly",
                "next_step": null,
                "observed_at": 1000,
                "source": "mcp",
                "source_label": "Agent-reported"
              }
            }
            """
        )

        let focus = try XCTUnwrap(DashboardAttentionItem(task: task))
        XCTAssertEqual(focus.reasonLabel, "Failed check")
        XCTAssertEqual(focus.summary, "The reference image changed unexpectedly")
        XCTAssertNil(focus.nextStep)
        XCTAssertEqual(focus.project, "agentacct-gui")
        XCTAssertEqual(focus.sourceLabel, "Agent-reported")
    }

    func testReviewBriefContainsOnlyRecordedFactsAndNamesMissingNextStep() throws {
        let task = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "task-finding",
              "title": "Verify dashboard hierarchy",
              "project": "agentacct-gui",
              "decision_status": { "key": "finding", "label": "Finding" },
              "evidence_strength": { "key": "unchecked" },
              "cost": {},
              "primary_root": { "client": "codex", "client_session_id": "session-1" },
              "attention": {
                "kind": "failed_check",
                "reason_label": "Failed check",
                "label": "Failed test check · pytest · exit 2",
                "summary": "The reference image changed unexpectedly",
                "next_step": null,
                "observed_at": 1787889600,
                "source": "ci",
                "source_label": "CI or provider"
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
            Decision: Finding
            Recorded reason: Failed test check · pytest · exit 2
            Recorded summary: The reference image changed unexpectedly
            Recorded next step: No next step recorded.
            Observed: 2026-08-28T04:00:00Z
            Provenance: CI or provider
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
                "source": "hook",
                "source_label": "Hook-captured"
              }
            }
            """
        )

        let focus = try XCTUnwrap(DashboardAttentionItem(task: task))
        XCTAssertEqual(focus.sourceLabel, "Hook-captured")
        XCTAssertTrue(DashboardActionBrief(focus: focus).text.contains("Provenance: Hook-captured"))

        // Without the reducer's label the source is a named absence; the raw
        // key is never re-labelled in Swift.
        let unlabelled = try decode(
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
                "source": "hook"
              }
            }
            """
        )
        let unlabelledFocus = try XCTUnwrap(DashboardAttentionItem(task: unlabelled))
        XCTAssertNil(unlabelledFocus.sourceLabel)
        XCTAssertTrue(DashboardActionBrief(focus: unlabelledFocus).text.contains("Provenance: source not reported"))
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
                "reason_label": "Blocker",
                "label": "Blocker",
                "summary": "Canonical renderer is offline",
                "next_step": "Retry when the renderer is available",
                "source": "mcp",
                "source_label": "Agent-reported"
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
        XCTAssertTrue(brief.text.contains("Recorded reason: Blocker"))
        XCTAssertTrue(brief.text.contains("Recorded summary: Canonical renderer is offline"))
        XCTAssertTrue(brief.text.contains("Recorded next step: Retry when the renderer is available"))
        XCTAssertTrue(brief.text.contains("Observed: time not reported"))
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
            snapshot: nil,
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
              "limit": 1,
              "truncated": true
            }
            """
        )

        let presentation = DashboardAttentionPresentation(payload: payload, error: nil)
        XCTAssertEqual(presentation.dashboardHeadline, "Verify dashboard hierarchy")
        // The queue's count words ride the payload; an older daemon without
        // them reads a neutral count, never a Swift copy of the queue noun.
        XCTAssertEqual(
            presentation.dashboardStatus(queue: AttentionQueueCopy(noun: "Attention", countText: "2 in Attention",
                                                                   openAction: "Open Attention", sortText: nil)),
            "2 in Attention"
        )
        XCTAssertEqual(presentation.dashboardStatus(queue: nil), "2 in queue")
        XCTAssertFalse(presentation.dashboardStatusIsWarning)
    }

    func testSignalRailNeverPresentsRetainedSourceHealthAsCurrentAfterAnError() throws {
        let healthy = try decode(
            V1IngestionSnapshot.self,
            from: """
            {
              "state": "healthy",
              "last_success_at": 1000,
              "state_title": "Sources healthy",
              "issues": []
            }
            """
        )

        // A refresh error supersedes the retained snapshot, and the one-line
        // rail states the CAUSE rather than the raw transport error — which
        // stays reachable on the Sources pane this row opens (K53).
        let unavailable = DashboardIngestionPresentation(snapshot: healthy, error: "source refresh failed")
        XCTAssertEqual(
            unavailable,
            DashboardIngestionPresentation(
                title: "Source status unavailable",
                detail: "The recorder didn't return source health.",
                detailCompact: "The recorder didn't return source health.",
                tone: .warning
            )
        )
        XCTAssertFalse(unavailable.detail.contains("source refresh failed"))
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

    func testEvidenceTrustRendersThePayloadCopyAndKeepsUnknownNeutral() throws {
        let unrecorded = try decode(
            V1IngestionSnapshot.self,
            from: """
            {
              "state": "unknown",
              "last_success_at": null,
              "state_title": "Import history not recorded",
              "state_detail": "Usage from 336 sessions is stored, but no import run was recorded for this store.",
              "issues": []
            }
            """
        )
        XCTAssertEqual(
            DashboardIngestionPresentation(snapshot: unrecorded, error: nil),
            DashboardIngestionPresentation(
                title: "Import history not recorded",
                detail: "Usage from 336 sessions is stored, but no import run was recorded for this store.",
                tone: .muted
            )
        )
        let bare = try decode(V1IngestionSnapshot.self, from: #"{ "state": "degraded", "issues": [] }"#)
        XCTAssertEqual(DashboardIngestionPresentation(snapshot: bare, error: nil).title, "Source status not reported")
        XCTAssertEqual(DashboardIngestionPresentation(snapshot: bare, error: nil).tone, .warning)
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

        // Without the payload's queue words the copy names the queue generically.
        XCTAssertEqual(WorkAttentionEmptyCopy(payload: clear, query: "").title, "Nothing in the review queue")
        XCTAssertEqual(
            WorkAttentionEmptyCopy(payload: filtered, query: "visual").title,
            "Nothing in the review queue matches this filter"
        )
        XCTAssertEqual(
            WorkAttentionEmptyCopy(payload: inconsistent, query: "visual").title,
            "Review queue details unavailable"
        )
        var named = filtered
        named.queue = AttentionQueueCopy(noun: "Attention", countText: "2 in Attention", openAction: nil, sortText: nil)
        XCTAssertEqual(WorkAttentionEmptyCopy(payload: named, query: "visual").title, "Nothing in Attention matches this filter")
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

    func testAttentionRequestGenerationRejectsAStaleResponse() {
        var generation = LatestRequestGeneration()
        let slowRefresh = generation.begin()
        let dispositionRefresh = generation.begin()

        XCTAssertFalse(generation.accepts(slowRefresh))
        XCTAssertTrue(generation.accepts(dispositionRefresh))
    }

    func testAttentionPagesMergeWithoutHidingLaterItems() throws {
        let first = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [{
                "task_id": "task-1",
                "decision_status": { "key": "finding" },
                "evidence_strength": { "key": "unchecked" },
                "cost": {},
                "attention": { "kind": "failed_check", "summary": "snapshot failed" }
              }],
              "total": 2,
              "counts": { "failed_check": 1, "failed_step": 0, "blocker": 1 },
              "snapshot": "queue-v1",
              "offset": 0, "limit": 1, "truncated": true
            }
            """
        )
        let second = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [{
                "task_id": "task-2",
                "decision_status": { "key": "blocked" },
                "evidence_strength": { "key": "unchecked" },
                "cost": {},
                "attention": { "kind": "blocker", "summary": "work blocked" }
              }],
              "total": 2,
              "counts": { "failed_check": 1, "failed_step": 0, "blocker": 1 },
              "snapshot": "queue-v1",
              "offset": 1, "limit": 1, "truncated": false
            }
            """
        )

        let merged = mergedAttentionPages(first, second)

        XCTAssertEqual(merged.items.map(\.taskId), ["task-1", "task-2"])
        XCTAssertEqual(merged.offset, 0)
        XCTAssertEqual(merged.limit, 2)
        XCTAssertFalse(merged.truncated)
        XCTAssertTrue(attentionPageCanAppend(first, second))

        let changedQueue = V1AttentionPayload(
            schema: second.schema,
            items: second.items,
            total: 3,
            counts: second.counts,
            snapshot: "queue-v2",
            offset: second.offset,
            limit: second.limit,
            truncated: true
        )
        XCTAssertFalse(attentionPageCanAppend(first, changedQueue))

    }

    func testRecentWorkProjectionKeepsDecisionEvidenceAndCostSeparate() throws {
        let task = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "task-1",
              "title": "Build reusable snapshot harness",
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
                "cost_complete": true,
                "state": "complete",
                "display_text": "≈$4.82",
                "basis_label": "pricing estimate"
              },
              "primary_root": { "client": "codex", "client_session_id": "session-1" },
              "last_activity_at": 1000
            }
            """
        )

        let item = DashboardWorkItem(task: task)

        XCTAssertEqual(item.title, "Build reusable snapshot harness")
        XCTAssertEqual(item.client, "codex")
        XCTAssertEqual(item.outcome, "Verified")
        XCTAssertEqual(item.evidence, "4/4 checked")
        XCTAssertEqual(item.cost, "≈$4.82")
        XCTAssertEqual(item.costWithBasis, "≈$4.82 · pricing estimate")
    }

    func testRecentWorkCostLabelsDoNotClaimUnknownCompleteness() throws {
        // The row renders the reducer's cost grammar verbatim; with no display
        // string it names the absence instead of re-deriving a figure.
        let cases = [
            (
                cost: #"{"estimated_cost_usd": 4.82, "cost_confidence": "client_reported", "cost_complete": true, "state": "complete", "display_text": "$4.82"}"#,
                expected: "$4.82"
            ),
            (
                cost: #"{"estimated_cost_usd": 4.82, "cost_confidence": "client_reported", "cost_complete": false, "state": "partial", "display_text": "~$4.82"}"#,
                expected: "~$4.82"
            ),
            (
                cost: #"{"estimated_cost_usd": 4.82, "cost_confidence": "estimated", "cost_complete": true, "state": "complete", "display_text": "≈$4.82"}"#,
                expected: "≈$4.82"
            ),
            (
                cost: #"{"state": "no_usage", "display_text": "no usage recorded"}"#,
                expected: "no usage recorded"
            ),
            (cost: #"{"estimated_cost_usd": 4.82, "cost_complete": true}"#, expected: "cost not reported"),
            (cost: #"{}"#, expected: "cost not reported"),
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

        XCTAssertEqual(DashboardUsageSeries.tokens.valueText(for: periods[0]), "1.2M fresh tokens")
        XCTAssertEqual(DashboardUsageSeries.cost.valueText(for: periods[0]), "≈$2.50")
        XCTAssertEqual(DashboardUsageSeries.cost.valueText(for: periods[2]), "unpriced")
        XCTAssertEqual(DashboardUsageSeries.tokens.valueText(for: periods[2]), "fresh tokens not reported")
        XCTAssertEqual(DashboardUsageSeries.tokens.totalText(for: completePeriods), "1.5M fresh tokens total")
        XCTAssertEqual(DashboardUsageSeries.tokens.totalText(for: periods), "~1.5M fresh tokens total")
        XCTAssertEqual(DashboardUsageSeries.tokens.totalText(for: [periods[2]]), "fresh tokens not reported")
        // K105: the cost readout is the cube's range figure with the reducer's
        // own label. Swift never sums buckets into a "total" nor appends it.
        XCTAssertEqual(DashboardUsageSeries.cost.totalText(for: periods), "cost total not reported")
        let totals = try decode([UsageBucket].self, from: """
        [
          {"rows":4,"priced_rows":4,"unpriced_rows":0,"cost_state":"complete","cost_complete":true,
           "estimated_cost_usd":3.75,"known_additive_cost_usd":3.75,"cost_confidence":"estimated_from_tokens",
           "cost_total_label":"total"},
          {"rows":50,"priced_rows":16,"unpriced_rows":34,"cost_state":"partial","cost_complete":false,
           "known_additive_cost_usd":1635.57,"cost_confidence":"estimated_from_tokens",
           "cost_total_label":"Partial subtotal · 34 of 50 usage records unpriced"},
          {"rows":3,"priced_rows":0,"unpriced_rows":3,"cost_state":"unpriced","cost_complete":false,
           "cost_total_label":"no priced usage · 3 of 3 usage records unpriced"}
        ]
        """)
        XCTAssertEqual(DashboardUsageSeries.cost.totalText(for: periods, totals: totals[0]), "≈$3.75 total")
        XCTAssertEqual(
            DashboardUsageSeries.cost.totalText(for: periods, totals: totals[1]),
            "~$1,635.57 Partial subtotal · 34 of 50 usage records unpriced"
        )
        XCTAssertFalse(DashboardUsageSeries.cost.totalText(for: periods, totals: totals[1]).hasSuffix(" total"))
        XCTAssertEqual(
            DashboardUsageSeries.cost.totalText(for: periods, totals: totals[2]),
            "no priced usage · 3 of 3 usage records unpriced"
        )

        // A bucket the cube recorded no usage for names that state and is
        // never charted as a zero.
        let empty = try decode(
            PeriodBucket.self,
            from: #"{ "period": "2026-08-27", "rows": 0, "cost_state": "none_recorded" }"#
        )
        XCTAssertNil(DashboardUsageSeries.tokens.value(for: empty))
        XCTAssertNil(DashboardUsageSeries.cost.value(for: empty))
        XCTAssertEqual(DashboardUsageSeries.tokens.valueText(for: empty), "no usage recorded")
        XCTAssertEqual(DashboardUsageSeries.cost.valueText(for: empty), "no usage recorded")
        XCTAssertEqual(DashboardUsageSeries.cost.totalText(for: [empty]), "no usage recorded")
    }

    func testUsageSeriesDescribesTheSelectedRangeAndEffectiveGranularity() throws {
        let daily = try usagePeriodPresentation(granularity: "daily")
        let weekly = try usagePeriodPresentation(granularity: "weekly")
        let unknown = try usagePeriodPresentation(granularity: nil)

        XCTAssertEqual(
            DashboardUsageSeries.tokens.subtitle(
                rangeDays: 7,
                periodPresentation: daily,
                vocabulary: chartVocabulary
            ),
            "Fresh tokens · last 7 days · client-reported"
        )
        XCTAssertEqual(
            DashboardUsageSeries.tokens.subtitle(
                rangeDays: 90,
                periodPresentation: weekly,
                vocabulary: chartVocabulary
            ),
            "Fresh tokens · last 90 days · weekly buckets · client-reported"
        )
        XCTAssertEqual(
            DashboardUsageSeries.cost.subtitle(
                rangeDays: 90,
                periodPresentation: weekly,
                vocabulary: chartVocabulary
            ),
            "USD · last 90 days · weekly buckets · cost basis not reported"
        )
        XCTAssertEqual(
            DashboardUsageSeries.cost.subtitle(
                rangeDays: 90,
                periodPresentation: weekly,
                costBasis: "pricing estimate",
                vocabulary: chartVocabulary
            ),
            "USD · last 90 days · weekly buckets · pricing estimate"
        )
        XCTAssertEqual(
            DashboardUsageSeries.tokens.subtitle(
                rangeDays: 30,
                periodPresentation: unknown,
                vocabulary: chartVocabulary
            ),
            "Fresh tokens · last 30 days · period buckets · client-reported"
        )
        XCTAssertEqual(weekly.pinAccessibilityHint, "Pins or clears this week's value")
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

        XCTAssertEqual(DashboardUsageSeries.tokens.totalText(for: periods), "2e19 fresh tokens total")
        let axisText = DashboardUsageSeries.tokens.axisText(for: Double(Int.max))
        XCTAssertEqual(axisText, "9e18")
        XCTAssertLessThanOrEqual(axisText.count, 5)
        XCTAssertEqual(
            DashboardUsageSeries.tokens.axisText(for: 999_900_000_000_000),
            "999T",
            "axis labels truncate toward zero so a tick never overstates its value"
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

        XCTAssertNil(DashboardUsageSeries.tokens.value(for: periods[0]))
        XCTAssertEqual(DashboardUsageSeries.tokens.valueText(for: periods[0]), "fresh tokens not reported")
        XCTAssertEqual(DashboardUsageSeries.tokens.totalText(for: periods), "~1.5M fresh tokens total")
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

        XCTAssertNil(DashboardUsageSeries.cost.value(for: periods[0]))
        XCTAssertEqual(DashboardUsageSeries.cost.valueText(for: periods[0]), "unpriced")
        // K39: plain rounded ticks, no cost glyph.
        XCTAssertEqual(DashboardUsageSeries.cost.axisText(for: 9.994), "9.99")
        XCTAssertEqual(DashboardUsageSeries.cost.axisText(for: 12.34), "12")
        XCTAssertEqual(DashboardUsageSeries.cost.axisText(for: 999_999), "999k")
        XCTAssertEqual(DashboardUsageSeries.cost.axisText(for: 9e18), "9e18")

        let overflowingTotal = try decode(
            [PeriodBucket].self,
            from: """
            [
              { "period": "2026-08-24", "estimated_cost_usd": 1.7976931348623157e308 },
              { "period": "2026-08-25", "estimated_cost_usd": 1.7976931348623157e308 }
            ]
            """
        )
        XCTAssertEqual(DashboardUsageSeries.cost.totalText(for: overflowingTotal), "cost total not reported")
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
            "codex · usage · activity 10s ago · 2/2 shown with no work status"
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
              "windows": [{ "kind": "7d", "window_label": "7-day limit", "used_percent": 39, "value_text": "39% used" }]
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
        XCTAssertEqual(available.meterCaption, "7-day limit")
        XCTAssertEqual(
            available.resetText,
            "reset time not reported",
            "no reset time was reported — named, never fabricated"
        )
        XCTAssertEqual(available.detailText, "7-day limit · provider reported · reset time not reported")
        // K11: one capacity wording on every surface — the reducer's value
        // phrase, never a Swift-derived "headroom" figure.
        XCTAssertEqual(available.decisionTitle, "codex · 39% used")

        let exceeded = try decode(
            LimitEntry.self,
            from: """
            { "client": "codex", "windows": [{ "kind": "7d", "used_percent": 104, "value_text": "104% used · limit exceeded" }] }
            """
        )
        let exceededRow = DashboardAgentPlanRow(client: "codex", limit: exceeded, plan: nil, usage: nil)
        XCTAssertEqual(exceededRow.decisionTitle, "codex · 104% used · limit exceeded")

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
            { "client": "codex", "windows": [{ "kind": "7d", "window_label": "7-day limit" }] }
            """
        )
        let missingRow = DashboardAgentPlanRow(client: "codex", limit: missingPercent, plan: nil, usage: nil)
        XCTAssertEqual(missingRow.meterCaption, "7-day limit usage not reported")
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
            "yesterday 22.5M fresh tokens · 12.1M fresh tokens on 2026-08-24 · client-reported"
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
        XCTAssertEqual(dailyPulse.title, "Today so far · 22.5M fresh tokens")
        XCTAssertEqual(dailyPulse.detail, "yesterday 12.1M fresh tokens · client-reported")

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
        XCTAssertEqual(weeklyPulse.title, "This week so far · 22.5M fresh tokens")
        XCTAssertEqual(
            weeklyPulse.detail,
            "last week 12.1M fresh tokens · client-reported"
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

    @MainActor
    func testWorkBrowseStatePreservesNonRoutingFieldsAcrossReceiptDestination() {
        let selection = AppSelection()
        selection.workBrowse.query = "pytest"
        selection.workBrowse.group = .attention
        selection.workBrowse.sort = .cost
        selection.workBrowse.pendingFocusRestorationTaskId = "task-1"

        selection.open(.task("task-1"))
        selection.open(.work)

        XCTAssertEqual(selection.workBrowse.query, "pytest")
        XCTAssertNil(
            selection.workBrowse.group,
            "A recent-task destination must clear a stale attention-queue origin"
        )
        XCTAssertEqual(selection.workBrowse.sort, .cost)
        XCTAssertEqual(selection.workBrowse.pendingFocusRestorationTaskId, "task-1")
    }

    func testWorkSelectionUsesAdaptiveMasterDetailPolicy() {
        XCTAssertEqual(
            workLayoutMode(for: 960, dynamicTypeSize: .medium, hasSelection: true),
            .pushDetail
        )
        XCTAssertEqual(
            workLayoutMode(for: 1120, dynamicTypeSize: .medium, hasSelection: true),
            .split
        )
        XCTAssertEqual(
            workLayoutMode(for: 1600, dynamicTypeSize: .accessibility1, hasSelection: true),
            .pushDetail
        )
        XCTAssertEqual(
            workLayoutMode(for: 960, dynamicTypeSize: .medium, hasSelection: false),
            .table
        )
    }

    func testRequestCancellationIsNeverPublishedAsAFetchFailure() {
        XCTAssertTrue(
            requestWasCancelled(CancellationError(), taskIsCancelled: false)
        )
        XCTAssertTrue(
            requestWasCancelled(URLError(.cancelled), taskIsCancelled: false)
        )
        XCTAssertTrue(
            requestWasCancelled(
                GlanceClientError.transport("cancelled"),
                taskIsCancelled: true
            )
        )
        XCTAssertTrue(
            requestWasCancelled(
                GlanceClientError.noDiscovery("/tmp/missing-local-api.json"),
                taskIsCancelled: true
            )
        )
        XCTAssertFalse(
            requestWasCancelled(
                GlanceClientError.transport("connection refused"),
                taskIsCancelled: false
            )
        )
    }

    func testWorkSelectionOutsideCurrentFiltersIsExplicit() throws {
        let tasks = try decode(
            [ReceiptSummary].self,
            from: """
            [
              {
                "task_id": "finding",
                "group_key": "attention",
                "decision_status": { "key": "finding" },
                "evidence_strength": { "key": "unchecked" },
                "cost": {}
              },
              {
                "task_id": "verified",
                "group_key": "verified",
                "decision_status": { "key": "verified" },
                "evidence_strength": { "key": "independently_checked" },
                "cost": {}
              }
            ]
            """
        )
        let attention = visibleWorkReceipts(tasks, query: "", group: .attention, sort: .latest)

        XCTAssertTrue(
            workSelectionIsOutsideBrowse(
                taskId: "verified",
                allTasks: tasks,
                visibleTasks: attention
            )
        )
        XCTAssertFalse(
            workSelectionIsOutsideBrowse(
                taskId: "finding",
                allTasks: tasks,
                visibleTasks: attention
            )
        )
    }

    func testWorkBrowseCountsNameTheLoadedSlice() {
        XCTAssertEqual(
            workBrowseCountText(visible: 4, loaded: 4, total: 4, truncated: false),
            "4 of 4 tasks"
        )
        XCTAssertEqual(
            workBrowseCountText(visible: 12, loaded: 200, total: 529, truncated: true),
            "12 of 200 loaded · 529 in store"
        )
    }

    func testWorkBrowseNamesUnknownAndExplicitlyTruncatedTotals() {
        XCTAssertEqual(
            workBrowseCountText(visible: 12, loaded: 200, total: nil, truncated: true),
            "12 of 200 loaded · more may exist"
        )
        XCTAssertEqual(
            workBrowseCountText(visible: 12, loaded: 200, total: nil, truncated: nil),
            "12 of 200 loaded · total not reported"
        )
        XCTAssertTrue(workReceiptCollectionIsPartial(loaded: 200, total: nil, truncated: true))
        XCTAssertFalse(workReceiptCollectionIsPartial(loaded: 200, total: nil, truncated: nil))
    }

    @MainActor
    func testARefreshNeverShrinksAQueueTheWorkPaneLoaded() {
        // Nothing has opened the Work queue: the window refresh publishes the
        // Dashboard's short preview and leaves the queue alone (K87).
        XCTAssertEqual(AttentionRefreshPlan(loadedExtent: nil), .previewOnly)
        XCTAssertEqual(AttentionRefreshPlan(loadedExtent: 0), .previewOnly)
        // Once it has, the refresh re-requests the SAME extent — never the
        // preview's five, which is what dropped the Blocked row off a loaded
        // six-item queue on a timer.
        XCTAssertEqual(
            AttentionRefreshPlan(loadedExtent: DashboardStore.attentionPageLimit),
            .reloadQueue(extent: 50)
        )
        XCTAssertNotEqual(DashboardStore.attentionPreviewLimit, DashboardStore.attentionPageLimit)
        // Pages the reviewer added with "Load more" are reloaded too, one
        // whole page at a time and then the remainder.
        XCTAssertEqual(AttentionRefreshPlan(loadedExtent: 120), .reloadQueue(extent: 120))
        XCTAssertEqual(attentionReloadPageLimit(loaded: 0, extent: 50, pageLimit: 50), 50)
        XCTAssertEqual(attentionReloadPageLimit(loaded: 50, extent: 120, pageLimit: 50), 50)
        XCTAssertEqual(attentionReloadPageLimit(loaded: 100, extent: 120, pageLimit: 50), 20)
        XCTAssertEqual(attentionReloadPageLimit(loaded: 120, extent: 120, pageLimit: 50), 1)
    }

    @MainActor
    func testTheDashboardReadsItsOwnAttentionLane() throws {
        let fixtureURL = try XCTUnwrap(
            Bundle.module.url(forResource: "dashboard", withExtension: "json")
        )
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL)
        let store = DashboardStore(preloaded: fixture)

        // With no preview published yet the card falls back to the loaded
        // queue, so the Dashboard is never blank while the two lanes differ.
        XCTAssertEqual(store.dashboardAttention?.total, store.attention?.total)
        XCTAssertNil(store.dashboardAttentionError)
        XCTAssertNil(store.attentionPreview)
    }

    func testLifecycleCountsAreAbsentUntilAPageLoads() {
        // Pending, nothing loaded: no counts (K54).
        XCTAssertFalse(workReceiptPageIsLoaded(loadedCount: 0, isLoading: true, error: nil))
        // Failed, nothing retained: still no counts.
        XCTAssertFalse(workReceiptPageIsLoaded(loadedCount: 0, isLoading: false, error: "receipts fetch failed"))
        // A refresh that fails OVER a loaded page keeps that page's real counts.
        XCTAssertTrue(workReceiptPageIsLoaded(loadedCount: 12, isLoading: true, error: "receipts fetch failed"))
        // Loaded and genuinely empty: zero is a result, and is shown.
        XCTAssertTrue(workReceiptPageIsLoaded(loadedCount: 0, isLoading: false, error: nil))
    }

    func testWorkBrowseCountNamesAnUnloadedPageInsteadOfCountingZero() {
        // No page has arrived: "0 loaded" would be a reported result (K54).
        XCTAssertEqual(
            workBrowseCountText(visible: 0, loaded: 0, total: nil, truncated: nil, pageIsLoaded: false),
            "tasks not loaded"
        )
        XCTAssertEqual(
            workBrowseCountText(visible: 0, loaded: 0, total: nil, truncated: nil, pageIsLoaded: true),
            "0 loaded · total not reported"
        )
    }

    @MainActor
    func testWorkReturnFocusFallsBackWhenReceiptIsOutsideFilters() throws {
        let visible = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "visible",
              "title": "Visible task",
              "group_key": "reported",
              "decision_status": { "key": "reported" },
              "evidence_strength": { "key": "unchecked" },
              "cost": {}
            }
            """
        )
        let hidden = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "hidden",
              "title": "Hidden task",
              "group_key": "attention",
              "decision_status": { "key": "finding" },
              "evidence_strength": { "key": "unchecked" },
              "cost": {}
            }
            """
        )
        let browse = WorkBrowseState()
        browse.group = .reported

        browse.prepareReturnFocus(from: hidden.taskId, in: [visible, hidden])

        XCTAssertNil(browse.pendingFocusRestorationTaskId)
        XCTAssertTrue(browse.shouldFocusSearchOnReturn)

        browse.prepareReturnFocus(from: visible.taskId, in: [visible, hidden])

        XCTAssertEqual(browse.pendingFocusRestorationTaskId, visible.taskId)
        XCTAssertFalse(browse.shouldFocusSearchOnReturn)
    }

    @MainActor
    func testWorkAttentionNavigationKeepsTasksOutsideTheRecentPage() throws {
        let recent = try decode(
            [ReceiptSummary].self,
            from: """
            [{
              "task_id": "task-recent", "title": "Recent task",
              "decision_status": { "key": "reported" },
              "evidence_strength": { "key": "unchecked" }, "cost": {}
            }]
            """
        )
        let attention = try decode(
            V1AttentionPayload.self,
            from: """
            {
              "schema": "agentacct.v1-attention.v1",
              "items": [{
                "task_id": "task-old", "title": "Older review task",
                "decision_status": { "key": "reported" },
                "evidence_strength": { "key": "unchecked" }, "cost": {},
                "attention": { "kind": "failed_check", "summary": "Check needs review" }
              }],
              "total": 2,
              "counts": { "failed_check": 2, "failed_step": 0, "blocker": 0 },
              "limit": 1, "truncated": true
            }
            """
        )
        let browse = WorkBrowseState()
        browse.group = .attention

        let table = WorkTaskPresentation(
            tasks: recent, attention: attention,
            group: browse.group, query: browse.query, sort: browse.sort
        )
        XCTAssertEqual(table.visibleTasks.map(\.taskId), ["task-old"])
        XCTAssertEqual(
            browse.visibleTasks(in: recent, attention: attention).map(\.taskId),
            table.visibleTasks.map(\.taskId),
            "Opening the master list must preserve the authoritative review queue"
        )

        browse.prepareReturnFocus(from: "task-old", in: recent, attention: attention)
        XCTAssertEqual(browse.pendingFocusRestorationTaskId, "task-old")
        XCTAssertFalse(browse.shouldFocusSearchOnReturn)

        browse.query = "recent"
        XCTAssertTrue(browse.visibleTasks(in: recent, attention: attention).isEmpty)
        browse.prepareReturnFocus(from: "task-old", in: recent, attention: attention)
        XCTAssertNil(browse.pendingFocusRestorationTaskId)
        XCTAssertTrue(browse.shouldFocusSearchOnReturn)
    }

    @MainActor
    func testWorkAttentionWithoutProjectionDoesNotInferAQueueFromRecentReceipts() throws {
        let recent = try decode(
            [ReceiptSummary].self,
            from: """
            [{
              "task_id": "task-recent-failure",
              "decision_status": { "key": "finding" },
              "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
              "cost": {}
            }]
            """
        )
        let browse = WorkBrowseState()
        browse.group = .attention

        XCTAssertTrue(browse.visibleTasks(in: recent).isEmpty)
        browse.prepareReturnFocus(from: "task-recent-failure", in: recent)
        XCTAssertNil(browse.pendingFocusRestorationTaskId)
        XCTAssertTrue(browse.shouldFocusSearchOnReturn)

        browse.group = nil
        XCTAssertEqual(browse.visibleTasks(in: recent).map(\.taskId), ["task-recent-failure"])
    }

    func testFailedChecksPutReportedReceiptInAttentionGroupAndSort() throws {
        let reportedFailure = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "reported-failure",
              "attention_open": true,
              "attention_order": 0,
              "group_key": "attention",
              "last_activity_at": 10,
              "decision_status": { "key": "reported" },
              "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
              "cost": {}
            }
            """
        )
        let findingResolved = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "resolved",
              "attention_open": false,
              "group_key": "reported",
              "last_activity_at": 20,
              "decision_status": { "key": "finding_resolved_by_user" },
              "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
              "cost": {}
            }
            """
        )
        // The same failing tally WITHOUT the reducer predicate re-derives
        // nothing in Swift: the row stays in its decision word's bucket.
        let reportedFailureWithoutPredicate = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "legacy",
              "decision_status": { "key": "reported" },
              "evidence_strength": { "key": "unchecked", "checks_failed": 1 },
              "cost": {}
            }
            """
        )

        // The reducer's group key is the only group signal: a row without one
        // re-derives nothing in Swift and sits in Other (never hidden).
        XCTAssertEqual(WorkGroup.forTask(reportedFailureWithoutPredicate), .other)
        XCTAssertEqual(WorkGroup.forTask(reportedFailure), .attention)
        XCTAssertEqual(WorkGroup.forTask(findingResolved), .reported)
        XCTAssertEqual(
            sortedReceipts([findingResolved, reportedFailure], by: .attention).map(\.taskId),
            ["reported-failure", "resolved"]
        )
    }

    @MainActor
    func testDashboardRefreshReadsSelectionAfterCollectionRefresh() async {
        var selection = "task-a"
        var refreshedTaskIds: [String] = []

        await refreshDashboardAndSelectedWork(
            dashboardRefresh: { selection = "task-b" },
            selectedTaskId: { selection },
            receiptRefresh: { refreshedTaskIds.append($0) }
        )

        XCTAssertEqual(refreshedTaskIds, ["task-b"])
    }

    @MainActor
    func testCancelledDashboardRefreshDoesNotStartDetailRefresh() async {
        var didRefreshReceipt = false

        await refreshDashboardAndSelectedWork(
            dashboardRefresh: {
                withUnsafeCurrentTask { $0?.cancel() }
            },
            selectedTaskId: { "task-a" },
            receiptRefresh: { _ in didRefreshReceipt = true }
        )

        XCTAssertFalse(didRefreshReceipt)
    }

    func testReceiptRefreshErrorsStayScopedToTheirTask() {
        XCTAssertEqual(
            workReceiptRefreshError(
                selectedTaskId: "task-a",
                errorTaskId: "task-a",
                error: "offline"
            ),
            "offline"
        )
        XCTAssertNil(
            workReceiptRefreshError(
                selectedTaskId: "task-b",
                errorTaskId: "task-a",
                error: "offline"
            )
        )
    }

    func testMissingEvidenceCountsRemainUnreported() throws {
        let task = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "partial",
              "decision_status": { "key": "reported", "label": "Reported" },
              "evidence_strength": {
                "key": "self_checked",
                "gradeable": true,
                "checkable_total": 3,
                "checks_total": 2
              },
              "cost": {}
            }
            """
        )

        let presentation = WorkReceiptRowPresentation(task: task)
        XCTAssertEqual(presentation.coverageText, "checked count not reported · 3 checkable steps")
        XCTAssertEqual(presentation.checkRunsText, "passes not reported · 2 checks")
        XCTAssertFalse(presentation.accessibilityLabel.contains("0/"))
        XCTAssertFalse(presentation.accessibilityFields.contains { $0.value.contains("0/") })
    }

    func testCompactCheckRunCopyDoesNotCollapseNoRunsToNo() throws {
        let task = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "no-checks",
              "decision_status": { "key": "blocked", "label": "Blocked" },
              "evidence_strength": {
                "key": "undefined",
                "gradeable": false,
                "checkable_total": 0,
                "checked_total": 0,
                "checks_total": 0,
                "checks_passed": 0,
                "checks_failed": 0
              },
              "cost": {}
            }
            """
        )

        XCTAssertEqual(
            WorkReceiptRowPresentation(task: task).compactCheckRunsText,
            "no checks recorded"
        )
    }

    func testReceiptPresentationsNameInconsistentCounts() throws {
        let evidence = try decode(
            ReceiptEvidence.self,
            from: """
            {
              "key": "self_checked",
              "gradeable": true,
              "checkable_total": 0,
              "checked_total": 2,
              "by_tier": { "self_checked": 2 }
            }
            """
        )

        let coverage = ReceiptCoveragePresentation(evidence: evidence)
        let checks = ReceiptCheckRunsPresentation(total: 1, passed: 2, failed: 1)

        XCTAssertEqual(coverage.value, "Inconsistent counts")
        XCTAssertTrue(coverage.isInconsistent)
        XCTAssertEqual(coverage.qualifier, "2 checked · 0 checkable reported")
        XCTAssertEqual(coverage.rowText, "inconsistent coverage · 2 checked of 0 reported")
        XCTAssertEqual(evidence.compactHeadline, coverage.rowText)
        XCTAssertEqual(evidence.headline, "Inconsistent counts (2 checked · 0 checkable reported)")
        XCTAssertEqual(checks.value, "Inconsistent counts")
        XCTAssertTrue(checks.isInconsistent)
        XCTAssertEqual(checks.qualifier, "2 passed · 1 failed · 1 total reported")
        XCTAssertEqual(checks.rowText, "inconsistent checks · 2 passed · 1 failed · 1 total")
        XCTAssertEqual(checks.headerText, "inconsistent · 2 passed · 1 failed · 1 total")
    }

    func testReceiptCheckPresentationMarksZeroTotalTalliesInconsistent() {
        let checks = ReceiptCheckRunsPresentation(total: 0, passed: 1, failed: 0)

        XCTAssertTrue(checks.isInconsistent)
        XCTAssertEqual(checks.value, "0 total reported")
        XCTAssertEqual(checks.qualifier, "1 passed · 0 failed · tallies conflict with total")
    }

    func testEmptyCheckDetailsDistinguishMissingItemsFromNoRuns() {
        XCTAssertEqual(
            receiptEmptyCheckDetailsCopy(total: 3, passed: 2, failed: 1),
            ReceiptEmptyCheckDetailsCopy(
                title: "No itemized check details recorded",
                detail: "Summary counts are available above; this payload did not include per-run details."
            )
        )
        XCTAssertEqual(
            receiptEmptyCheckDetailsCopy(total: nil, passed: nil, failed: nil),
            ReceiptEmptyCheckDetailsCopy(
                title: "No check runs recorded",
                detail: "Machine checks land here when a hook or CI reports one."
            )
        )
    }

    func testCoveragePresentationNamesUnavailableAndConflictingTierBreakdowns() throws {
        let missing = try decode(
            ReceiptEvidence.self,
            from: """
            {
              "key": "self_checked",
              "gradeable": true,
              "checkable_total": 2,
              "checked_total": 1
            }
            """
        )
        let conflicting = try decode(
            ReceiptEvidence.self,
            from: """
            {
              "key": "self_checked",
              "gradeable": true,
              "checkable_total": 2,
              "checked_total": 1,
              "by_tier": { "self_checked": 2 }
            }
            """
        )

        let missingPresentation = ReceiptCoveragePresentation(evidence: missing)
        let conflictingPresentation = ReceiptCoveragePresentation(evidence: conflicting)

        XCTAssertFalse(missingPresentation.isInconsistent)
        XCTAssertFalse(missingPresentation.tierBreakdownAvailable)
        XCTAssertEqual(
            missingPresentation.tierBreakdownNotice,
            "Evidence-tier breakdown not reported."
        )
        XCTAssertTrue(conflictingPresentation.isInconsistent)
        XCTAssertFalse(conflictingPresentation.tierBreakdownAvailable)
        XCTAssertEqual(
            conflictingPresentation.tierBreakdownNotice,
            "Evidence tiers report 2 checked steps; the summary reports 1."
        )
    }

    func testAttentionDecisionSummarySeparatesBlockerAndFailedChecks() throws {
        let receipt = try decode(
            Receipt.self,
            from: """
            {
              "schema_version": "agentacct.receipt.v1",
              "group_key": "attention",
              "task_id": "blocked",
              "axes": {
                "decision_status": {
                  "key": "blocked",
                  "label": "Blocked",
                  "statement": "A representative window is required.",
                  "asserted_by": "agent_report",
                  "asserted_by_label": "Agent-reported",
                  "blocker": { "text": "The sample is too short." }
                },
                "evidence_strength": {
                  "key": "unchecked",
                  "gradeable": true,
                  "checkable_total": 2,
                  "checked_total": 1
                }
              },
              "dimensions": {
                "task": {}, "actors": {}, "actions": {}, "cost": {},
                "evidence": { "checks_total": 2, "checks_passed": 1, "checks_failed": 1 },
                "outcome": {}, "gaps": {}, "provenance": {}
              }
            }
            """
        )

        let presentation = WorkReceiptDecisionPresentation(receipt: receipt)
        XCTAssertTrue(presentation.isAttention, "the reducer's group key places it in Attention")
        XCTAssertEqual(presentation.explanation, "A representative window is required. — Agent-reported")
        XCTAssertEqual(presentation.coverageValue, "1/2")
        XCTAssertEqual(presentation.checksValue, "1/2")
        XCTAssertTrue(presentation.checksQualifier.contains("1 failed"))
        XCTAssertFalse(presentation.explanation.contains("The sample is too short"))
    }

    func testWorkReceiptRowPresentationIncludesDecisionRelevantFields() throws {
        let task = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "task-1",
              "title": "Investigate pytest errors",
              "decision_status": {
                "key": "finding",
                "label": "Finding",
                "statement": "A recorded check found an issue."
              },
              "evidence_strength": {
                "key": "unchecked",
                "gradeable": true,
                "checkable_total": 1,
                "checked_total": 0,
                "checks_total": 3,
                "checks_passed": 2,
                "checks_failed": 1
              },
              "cost": {
                "estimated_cost_usd": 7.66,
                "cost_confidence": "estimated_from_tokens",
                "cost_complete": false,
                "state": "partial",
                "display_text": "~$7.66",
                "basis_label": "pricing estimate"
              },
              "primary_root": { "client": "codex", "client_session_id": "session-1" },
              "last_activity_at": 1000,
              "handed_off": true,
              "lifecycle_marker_text": "Handed off"
            }
            """
        )

        let row = WorkReceiptRowPresentation(task: task)

        XCTAssertEqual(row.coverageText, "0/1 checked")
        XCTAssertEqual(row.checkRunsText, "2/3 passed · 1 failed")
        XCTAssertEqual(row.costText, "~$7.66 · pricing estimate")
        XCTAssertTrue(row.accessibilityLabel.contains("Finding"))
        XCTAssertEqual(row.lifecycleMarkerText, "Handed off")
        XCTAssertTrue(row.accessibilityLabel.contains("Handed off"))
        // The label says what the row IS and what was decided; every measured
        // fact is still there, under the field name its column header prints
        // (K119), instead of one long unlabelled sentence.
        let fields = Dictionary(
            uniqueKeysWithValues: row.accessibilityFields.map { ($0.label, $0.value) }
        )
        XCTAssertEqual(fields["Coverage"], "0/1 checked")
        XCTAssertEqual(fields["Checks"], "2/3 passed, 1 failed")
        XCTAssertEqual(fields["Client"], "codex")
        XCTAssertEqual(fields["Cost"], "~$7.66 · pricing estimate")
        XCTAssertNotNil(fields["Updated"])
        XCTAssertFalse(row.accessibilityLabel.contains("0/1 checked"))
    }

    func testWorkRowLabelNeverDoublesATrailingPeriod() {
        // A recorded reason that already ends in a full stop must not collect
        // a second one from the joiner (the ".." VoiceOver read out, K119).
        XCTAssertEqual(
            joinedRecordedSentences(["Fix the rounding bug", "Finding", "Refresh and validate."]),
            "Fix the rounding bug. Finding. Refresh and validate."
        )
        XCTAssertEqual(
            joinedRecordedSentences(["A title", nil, "", "Verified"]),
            "A title. Verified"
        )
    }

    func testWorkRowFieldsPutTheAttentionReasonFirstAndSpeakItWithTheRow() throws {
        let task = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "task-attention",
              "title": "Add live FX conversion",
              "decision_status": { "key": "blocked", "label": "Blocked" },
              "evidence_strength": { "key": "unchecked", "gradeable": true, "checkable_total": 1 },
              "cost": {},
              "attention": {
                "kind": "blocker",
                "label": "Blocker",
                "reason_label": "Blocker",
                "summary": "Waiting on a provider key.",
                "open": true
              }
            }
            """
        )

        let row = WorkReceiptRowPresentation(task: task)
        let first = try XCTUnwrap(row.accessibilityFields.first)

        XCTAssertEqual(first.label, "Attention")
        XCTAssertTrue(first.value.contains("Waiting on a provider key."))
        XCTAssertTrue(first.isPrimary)
        XCTAssertFalse(row.accessibilityLabel.contains("Waiting on a provider key."))
    }

    func testWorkRowFieldLabelsComeFromTheListPayloadNotTheAppsDefaults() throws {
        // A row without its detail receipt used to fall back to the Swift
        // defaults in `ReceiptFieldLabels`, so the accessibility-size layout —
        // which PRINTS a label beside every value — spoke the app's own words
        // for Client, Updated and Attention while the visible column headers
        // came from `/v1/tasks` `field_labels`. Both now read the payload.
        let task = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "task-labels",
              "title": "Add live FX conversion",
              "decision_status": { "key": "blocked", "label": "Blocked" },
              "evidence_strength": { "key": "unchecked", "gradeable": true, "checkable_total": 1 },
              "cost": {},
              "attention": {
                "kind": "blocker", "label": "Blocker", "reason_label": "Blocker",
                "summary": "Waiting on a provider key.", "open": true
              }
            }
            """
        )
        let listLabels = try decode(
            ReceiptFieldLabels.self,
            from: """
            { "client": "Recorder", "updated": "Last seen", "attention": "Needs you",
              "coverage": "Proof", "checks": "Runs", "cost": "Spend" }
            """
        )

        let appDefaults = WorkReceiptRowPresentation(task: task)
        XCTAssertEqual(appDefaults.fieldLabels.clientLabel, "Client")

        let row = WorkReceiptRowPresentation(task: task, listLabels: listLabels)
        XCTAssertEqual(row.fieldLabels.clientLabel, "Recorder")
        XCTAssertEqual(row.fieldLabels.updatedLabel, "Last seen")
        XCTAssertEqual(row.fieldLabels.attentionLabel, "Needs you")
        // The printed accessibility fields carry the payload's words.
        XCTAssertEqual(row.accessibilityFields.map(\.label).first, "Needs you")
        XCTAssertTrue(row.accessibilityFields.contains { $0.label == "Recorder" })
        XCTAssertTrue(row.accessibilityFields.contains { $0.label == "Last seen" })
    }

    func testWorkReceiptRowPresentationPrefersFreshDetailHandoffState() throws {
        let task = try decode(
            ReceiptSummary.self,
            from: """
            {
              "task_id": "task-1",
              "title": "Investigate pytest errors",
              "decision_status": { "key": "finding", "label": "Finding" },
              "evidence_strength": { "key": "unchecked" },
              "cost": {},
              "handed_off": true
            }
            """
        )
        let detail = try decode(
            Receipt.self,
            from: """
            {
              "schema_version": "agentacct.receipt.v1",
              "task_id": "task-1",
              "title": "Investigate pytest errors",
              "axes": {
                "decision_status": { "key": "finding", "label": "Finding" },
                "evidence_strength": { "key": "unchecked" },
                "handoff": { "handed_off": false }
              },
              "dimensions": {
                "task": {}, "actors": {}, "actions": {}, "cost": {},
                "evidence": {}, "outcome": {}, "gaps": {}, "provenance": {}
              }
            }
            """
        )

        let row = WorkReceiptRowPresentation(task: task, detail: detail)

        XCTAssertNil(row.lifecycleMarkerText)
        XCTAssertFalse(row.accessibilityLabel.contains("Handed off"))
    }

    func testWorkDecisionSummaryKeepsClaimCoverageSeparateFromCheckRuns() throws {
        let fixtureURL = try XCTUnwrap(
            Bundle.module.url(forResource: "dashboard", withExtension: "json")
        )
        let receipt = try XCTUnwrap(DashboardSnapshotFixture.load(from: fixtureURL).work?.receipt)

        let presentation = WorkReceiptDecisionPresentation(receipt: receipt)

        XCTAssertEqual(presentation.headline, "Current outcome")
        XCTAssertEqual(presentation.coverageValue, "4/4")
        // Each qualifier names its unit: steps for coverage, check runs for checks.
        XCTAssertEqual(presentation.coverageQualifier, "independently checked completed steps")
        XCTAssertEqual(presentation.checksValue, "6/6")
        XCTAssertEqual(presentation.checksQualifier, "6 passed")
        XCTAssertFalse(presentation.isAttention)
        XCTAssertTrue(presentation.accessibilityLabel.contains("Coverage: 4/4"))
        XCTAssertTrue(presentation.accessibilityLabel.contains("Checks: 6/6"))
    }

    func testReceiptPresentationsPreservePartialCountsWithoutInventingZeroes() throws {
        let receipt = try decode(
            Receipt.self,
            from: """
            {
              "schema_version": "agentacct.receipt.v1",
              "task_id": "partial",
              "axes": {
                "decision_status": { "key": "reported" },
                "evidence_strength": {
                  "key": "unchecked",
                  "gradeable": true,
                  "checkable_total": 4
                }
              },
              "dimensions": {
                "task": {}, "actors": {}, "actions": {}, "cost": {},
                "evidence": { "checks_passed": 2, "checks_failed": 1 },
                "outcome": {}, "gaps": {}, "provenance": {}
              }
            }
            """
        )

        let decision = WorkReceiptDecisionPresentation(receipt: receipt)
        let coverage = ReceiptCoveragePresentation(evidence: receipt.axes.evidenceStrength)
        let checks = ReceiptCheckRunsPresentation(total: nil, passed: 2, failed: 1)

        XCTAssertEqual(decision.coverageValue, "Not reported")
        XCTAssertEqual(decision.coverageQualifier, "checked count unavailable · 4 checkable steps")
        XCTAssertEqual(coverage.rowText, "checked count not reported · 4 checkable steps")
        XCTAssertEqual(decision.checksValue, "Total not reported")
        XCTAssertEqual(decision.checksQualifier, "2 passed · 1 failed")
        XCTAssertEqual(checks.rowText, "total not reported · 2 passed · 1 failed")
        XCTAssertFalse(decision.accessibilityLabel.contains("0 of"))
    }

    func testGradeableReceiptWithMissingCoverageTotalIsNotCalledNotGradeable() throws {
        let evidence = try decode(
            ReceiptEvidence.self,
            from: """
            {
              "key": "self_checked",
              "gradeable": true,
              "checked_total": 2
            }
            """
        )

        let presentation = ReceiptCoveragePresentation(evidence: evidence)

        XCTAssertEqual(presentation.value, "Total not reported")
        XCTAssertEqual(presentation.rowText, "2 checked · checkable total not reported")
        XCTAssertFalse(presentation.rowText.contains("not gradeable"))
    }

    func testInconsistentZeroCheckTotalNamesSuppliedTallies() {
        let presentation = ReceiptCheckRunsPresentation(total: 0, passed: 1, failed: 1)

        XCTAssertEqual(presentation.value, "0 total reported")
        XCTAssertEqual(presentation.qualifier, "1 passed · 1 failed · tallies conflict with total")
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

    private func usagePeriodPresentation(granularity: String?) throws -> UsagePeriodPresentation {
        let filtersEcho = granularity.map {
            ",\"filters_echo\":{\"granularity\":\"\($0)\"}"
        } ?? ""
        let usage = try decode(
            UsageSummary.self,
            from: "{\"by_client\":[],\"by_model\":[]\(filtersEcho)}"
        )
        return UsagePeriodPresentation(usage: usage)
    }

    /// The payload chart vocabulary as `/usage/summary` serves it.
    private var chartVocabulary: UsageChartVocabulary {
        var vocabulary = UsageChartVocabulary()
        vocabulary.options = [
            UsageSeriesOption(key: "tokens", label: "Fresh tokens"),
            UsageSeriesOption(key: "cost", label: "Cost"),
        ]
        vocabulary.tokenBasis = "client-reported"
        vocabulary.costUnit = "USD"
        vocabulary.costLegend = "~$ partial subtotal · open cap = partial"
        return vocabulary
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
            tokenBasis: "client-reported",
            now: now,
            timeZone: TimeZone(secondsFromGMT: 0)!
        )
    }
}
