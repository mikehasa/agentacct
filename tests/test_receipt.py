"""The Receipt projector: 8 dimensions, two orthogonal axes, provenance, gaps.

The load-bearing tests here are the ORTHOGONALITY invariants from M1's
acceptance bar: an agent's "done" never becomes evidence-verified, and a human
review/resolution never becomes machine verification.
"""

from __future__ import annotations

from typing import Any

from agentacct.display_vocabulary import COMMAND_NOT_SHOWN_TEXT, disposition_effects
from agentacct.finding_disposition import finding_target_digest
from agentacct.receipt import (
    RECEIPT_SCHEMA_VERSION,
    build_attention_reason,
    build_receipt,
    build_receipt_summary,
    plan_share_headline,
)
from agentacct.task_outcome import _LEFT_BEHIND_AFTER_ELSEWHERE_SECONDS


def _check(
    result: str,
    *,
    name: str = "pytest",
    kind: str = "test",
    at: float = 200.0,
    exit_code: int = 0,
    source_type: str = "client_hook",
) -> dict[str, Any]:
    return {
        "event_id": f"evt_{name}_{result}",
        "result": result,
        "name": name,
        "evidence_type": kind,
        "created_at": at,
        "exit_code": exit_code,
        "source_type": source_type,
        "source": "claude-code",
        "check_identity": f"check:{name}",
        "check_identity_stable": True,
    }


def _task(
    items: list[dict[str, Any]],
    *,
    task_checks: list[dict[str, Any]] | None = None,
    finding_episodes: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    actions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": "task_x",
        "primary_root": {"client": "claude-code", "client_session_id": "s1"},
        "root_keys": [{"client": "claude-code", "client_session_id": "s1"}],
        "session_keys": [{"client": "claude-code", "client_session_id": "s1"}],
        "sessions": [
            {
                "client": "claude-code",
                "client_session_id": "s1",
                "project": "acme",
                "identity_scope_state": "explicit",
                "last_activity_at": 100.0,
                "usage": {},
            }
        ],
        "session_count": 1,
        "supporting_count": 0,
        "child_count": 0,
        "internal_count": 0,
        "last_activity_at": 100.0,
        "work_items": items,
        "work_associations": [],
        "usage": usage
        if usage is not None
        else {
            "rows": 1,
            "estimated_cost_usd": 0.5,
            "cost_complete": True,
            "cost_basis": "pricing_table",
            "cost_confidence": "estimated_from_tokens",
            "total_tokens": 1000,
            "fresh_tokens": 800,
        },
        "models": ["claude-opus"],
        "actions": actions
        if actions is not None
        else {"tool_category_counts": {}, "tool_category_total": 0, "touched_files": [], "touched_file_count": 0},
    }
    if task_checks is not None:
        task["current_check_events"] = task_checks
    if finding_episodes is not None:
        task["finding_episodes"] = finding_episodes
    return task


def _receipt(task: dict[str, Any]):
    return build_receipt(task, public_task_id="task_x", title="Add rate limit")


def test_receipt_has_all_eight_dimensions_and_two_named_axes() -> None:
    receipt = _receipt(_task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}]))
    assert receipt["schema_version"] == RECEIPT_SCHEMA_VERSION
    assert set(receipt["dimensions"]) == {
        "task",
        "actors",
        "actions",
        "cost",
        "evidence",
        "outcome",
        "gaps",
        "provenance",
    }
    assert "decision_status" in receipt["axes"]
    assert "evidence_strength" in receipt["axes"]
    # Every content dimension carries its own provenance and gaps.
    for name in ("task", "actors", "actions", "cost", "evidence", "outcome"):
        assert "provenance" in receipt["dimensions"][name]
        assert "gaps" in receipt["dimensions"][name]


def test_receipt_exposes_handoff_marker_axis_and_summary_flag() -> None:
    # A handed_off frontier surfaces as BOTH the decision word and the parallel
    # handoff axis; the compact summary carries the flat bool for the list row.
    from agentacct.receipt import build_receipt_summary

    task = _task(
        [
            {"work_id": "a", "latest_status": "completed", "updated_at": 100.0},
            {"work_id": "b", "latest_status": "handed_off", "updated_at": 200.0},
        ]
    )
    receipt = _receipt(task)
    assert receipt["axes"]["handoff"]["handed_off"] is True
    assert receipt["axes"]["handoff"]["statement"]
    assert receipt["axes"]["handoff"]["asserted_by"] == "agent_report"
    assert receipt["axes"]["decision_status"]["key"] == "handed_off"

    summary = build_receipt_summary(task, public_task_id="task_x", title="t")
    assert summary["handed_off"] is True


def test_blocked_receipt_still_carries_the_handoff_marker() -> None:
    # A hard problem (blocked/finding) outranks the handoff in the decision WORD,
    # but the handoff fact must not vanish — it rides the parallel axis so a
    # surface can show it beside the red headline.
    task = _task(
        [
            {"work_id": "a", "latest_status": "blocked", "updated_at": 100.0},
            {"work_id": "b", "latest_status": "handed_off", "updated_at": 200.0},
        ]
    )
    receipt = _receipt(task)
    assert receipt["axes"]["decision_status"]["key"] in {"blocked", "failed"}
    assert receipt["axes"]["handoff"]["handed_off"] is True


def test_ended_open_is_inferred_and_never_claims_completion() -> None:
    # A still-open step whose session ended surfaces as the ``ended_open``
    # decision word, asserted_by=inferred (weaker than the agent's word), with a
    # statement that never claims completion or a deliberate handoff. The outcome
    # dimension's provenance must be the inferred source, not mcp/machine/human.
    task = _task(
        [
            {
                "work_id": "a",
                "latest_status": "started",
                "updated_at": 100.0,
                "client": "claude-code",
                "client_session_id": "s1",
                "session_ended_at": 200.0,
            }
        ]
    )
    receipt = _receipt(task)
    decision = receipt["axes"]["decision_status"]
    assert decision["key"] == "ended_open"
    assert decision["asserted_by"] == "inferred"
    assert "inferred" in decision["statement"].lower()
    assert "complet" not in decision["statement"].lower() or "not a recorded completion" in decision["statement"].lower()
    outcome_dim = receipt["dimensions"]["outcome"]
    assert outcome_dim["provenance"] == ["inferred"]
    # The quiet detail-line timestamps are only for inactive / mostly_done; on any
    # other key (here ended_open) they are present-but-None, never a stray fact.
    assert outcome_dim["quiet_since"] is None
    assert outcome_dim["newer_session_started_at"] is None


def test_inactive_is_inferred_and_never_claims_completion() -> None:
    # A Task with open steps, nothing finished, that the store moved on past by
    # more than the threshold surfaces as the ``inactive`` decision word:
    # asserted_by=inferred (weakest provenance), a statement that never claims
    # completion, an inferred-source outcome provenance, a terminal-outcome gap,
    # and — crucially — it is NOT queued for attention.
    task = _task(
        [
            {"work_id": "a", "latest_status": "started", "updated_at": 100.0},
            {"work_id": "b", "latest_status": "checkpoint", "updated_at": 100.0},
        ]
    )
    # The 48h + newer-session rule: a distinct session began at 160.0 (after this
    # Task's newest event at 100.0) and the store kept working past the buffer.
    newer_session_start = 160.0
    session_starts = {"elsewhere": newer_session_start}
    quiet_now = newer_session_start + _LEFT_BEHIND_AFTER_ELSEWHERE_SECONDS + 60.0
    receipt = build_receipt(
        task,
        public_task_id="task_x",
        title="t",
        latest_store_activity_at=quiet_now,
        session_starts=session_starts,
    )
    decision = receipt["axes"]["decision_status"]
    assert decision["key"] == "inactive"
    assert decision["label"] == "Inactive"
    assert decision["asserted_by"] == "inferred"
    assert "inferred" in decision["statement"].lower()
    assert "not a completion" in decision["statement"].lower()
    # The outcome dimension carries the inferred source and still owes a terminal
    # outcome (a gap), never a settled result.
    outcome_dim = receipt["dimensions"]["outcome"]
    assert outcome_dim["provenance"] == ["inferred"]
    assert any("No terminal outcome" in gap for gap in outcome_dim["gaps"])
    # The factual detail-line timestamps ride the outcome dimension on inactive:
    # quiet_since = when this Task fell silent; newer_session_started_at = the
    # newer session's start. Facts for the app to render, never a completion claim.
    assert outcome_dim["quiet_since"] == 100.0
    assert outcome_dim["newer_session_started_at"] == newer_session_start
    # Inactive is a calm downgrade, not an attention item.
    assert (
        build_attention_reason(
            task, latest_store_activity_at=quiet_now, session_starts=session_starts
        )
        is None
    )


def test_resumed_task_receipt_shows_no_handoff_marker() -> None:
    # A later open step postdates the handoff: the Task resumed, so neither the
    # decision word nor the marker report a handoff. Assert BOTH the detail axis
    # AND the flat summary flag (the two are independent expressions that drive
    # the detail view vs the list row) so a non-recency regression of either is
    # caught — the list row is the exact dogfood surface this PR fixes.
    from agentacct.receipt import build_receipt_summary

    task = _task(
        [
            {"work_id": "a", "latest_status": "handed_off", "updated_at": 100.0},
            {"work_id": "b", "latest_status": "started", "updated_at": 200.0},
        ]
    )
    receipt = _receipt(task)
    assert receipt["axes"]["decision_status"]["key"] == "in_progress"
    assert receipt["axes"]["handoff"]["handed_off"] is False
    summary = build_receipt_summary(task, public_task_id="task_x", title="t")
    assert summary["handed_off"] is False


def test_summary_handed_off_flag_is_false_without_any_handoff() -> None:
    # The flat list-row flag must be False for a task that never handed off — a
    # plain completed task must never paint a handoff chip.
    from agentacct.receipt import build_receipt_summary

    task = _task([{"work_id": "a", "latest_status": "completed", "updated_at": 100.0}])
    summary = build_receipt_summary(task, public_task_id="task_x", title="t")
    assert summary["handed_off"] is False
    assert _receipt(task)["axes"]["handoff"]["handed_off"] is False


def test_agent_reported_done_is_never_evidence_checked() -> None:
    # A completed step with no linked check: the agent SAID done, nothing PROVES
    # it — so it sits in the 'unchecked' tier, never any CHECKED tier.
    receipt = _receipt(_task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}]))
    assert receipt["axes"]["decision_status"]["key"] == "reported"
    assert receipt["axes"]["decision_status"]["asserted_by"] == "agent_report"
    evidence = receipt["axes"]["evidence_strength"]
    assert evidence["strongest_tier"] is None
    assert evidence["key"] == "unchecked"
    assert evidence["by_tier"]["unchecked"] == 1
    assert evidence["by_tier"]["self_checked"] == 0
    assert evidence["checkable_total"] == 1


def test_passing_hook_check_is_independently_checked() -> None:
    # A hook-observed passing check is independent of the agent-under-test — the
    # only local way to reach 'independently_checked'.
    check = _check("passed", at=200.0)  # _check defaults source_type=client_hook
    task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0, "current_check_events": [check]}],
        task_checks=[check],
    )
    receipt = _receipt(task)
    assert receipt["axes"]["decision_status"]["key"] == "verified"
    assert receipt["axes"]["decision_status"]["asserted_by"] == "machine"
    evidence = receipt["axes"]["evidence_strength"]
    assert evidence["strongest_tier"] == "independently_checked"
    assert evidence["by_tier"]["independently_checked"] == 1
    assert evidence["checks_passed"] == 1
    assert receipt["dimensions"]["evidence"]["checks"][0]["source"] == "hook"
    # The shared display label ships with the key — no surface maps keys itself.
    assert receipt["dimensions"]["evidence"]["checks"][0]["source_label"] == "Hook-captured"


def test_every_receipt_check_carries_its_source_label() -> None:
    checks = [
        _check("passed", name="hooked", at=200.0, source_type="client_hook"),
        _check("passed", name="agent", at=210.0, source_type="mcp_agent_reported"),
        _check("passed", name="ci", at=220.0, source_type="ci"),
    ]
    task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0, "current_check_events": checks}],
        task_checks=checks,
    )
    rows = _receipt(task)["dimensions"]["evidence"]["checks"]
    assert {row["name"]: row["source_label"] for row in rows} == {
        "hooked": "Hook-captured",
        "agent": "Agent-reported",
        "ci": "CI or provider",
    }
    assert all(row["superseded_definition"] is None for row in rows)


def test_superseded_failure_is_not_counted_in_the_check_tally() -> None:
    # A failing check that a later, distinct passing check superseded stays in
    # checks_total and stays visible as a (history) row, but must NOT read as a
    # current failure in the header tally — otherwise "N failed" overstates
    # failures the frontier already cleared, contradicting the row that is
    # marked "superseded by a later passing run".
    superseded_fail = {
        "event_id": "evt_fail",
        "result": "failed",
        "name": "lint",
        "evidence_type": "lint",
        "created_at": 100.0,
        "exit_code": 1,
        "source_type": "client_hook",
        "source": "claude-code",
        "check_identity": "check:lint-old",
        "check_identity_stable": True,
        "supersession_state": "superseded",
        "superseded_by_event_id": "evt_pass",
    }
    superseding_pass = {
        "event_id": "evt_pass",
        "result": "passed",
        "name": "lint",
        "evidence_type": "lint",
        "created_at": 200.0,
        "exit_code": 0,
        "source_type": "client_hook",
        "source": "claude-code",
        "check_identity": "check:lint-new",
        "check_identity_stable": True,
    }
    task = _task(
        [
            {
                "work_id": "w",
                "latest_status": "completed",
                "updated_at": 100.0,
                "current_check_events": [superseded_fail, superseding_pass],
            }
        ],
        task_checks=[superseded_fail, superseding_pass],
    )
    receipt = _receipt(task)
    evidence = receipt["axes"]["evidence_strength"]
    # Both runs are still counted in the total…
    assert evidence["checks_total"] == 2
    # …but the superseded failure no longer inflates the failed tally.
    assert evidence["checks_passed"] == 1
    assert evidence["checks_failed"] == 0
    # The superseded failure is still present as a row so the history stays auditable.
    rows = receipt["dimensions"]["evidence"]["checks"]
    assert [row for row in rows if row.get("superseded")] != []
    # The row carries the one shared definition of "superseded" for its help.
    from agentacct.display_vocabulary import SUPERSEDED_CHECK_DEFINITION

    assert {row["superseded_definition"] for row in rows if row.get("superseded")} == {SUPERSEDED_CHECK_DEFINITION}


def test_agent_reported_check_is_self_checked_not_independent() -> None:
    # An mcp_agent_reported passing check is the agent's own word — it is
    # 'self_checked', never promoted to independent verification, no matter what
    # the check's free-text summary claims.
    check = _check("passed", at=200.0, source_type="mcp_agent_reported")
    task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0, "current_check_events": [check]}],
        task_checks=[check],
    )
    evidence = _receipt(task)["axes"]["evidence_strength"]
    assert evidence["strongest_tier"] == "self_checked"
    assert evidence["by_tier"]["self_checked"] == 1
    assert evidence["by_tier"]["independently_checked"] == 0


def test_pure_research_task_is_not_gradeable() -> None:
    # A step that produces no machine-verifiable output is excused from the
    # ratio; with no checkable step at all, the task is honestly Not gradeable —
    # never a fabricated 0.
    task = _task([{"work_id": "w", "latest_status": "completed", "kind": "research", "updated_at": 100.0}])
    evidence = _receipt(task)["axes"]["evidence_strength"]
    assert evidence["gradeable"] is False
    assert evidence["key"] == "undefined"
    assert evidence["checkable_total"] == 0
    assert evidence["not_checkable"] == 1


def test_human_resolved_finding_is_human_asserted_but_not_evidence_verified() -> None:
    # The orthogonality keystone: a human dispositioning a finding changes the
    # DECISION axis to a human assertion but must never touch EVIDENCE strength.
    failing = _check("failed", exit_code=1, at=200.0)
    episodes = [{"target_digest": finding_target_digest(failing), "disposition_state": "resolved"}]
    task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0, "current_check_events": [failing]}],
        task_checks=[failing],
        finding_episodes=episodes,
    )
    receipt = _receipt(task)
    # A fully human-resolved finding gets its own decision WORD (so every
    # surface files it done-ish by vocabulary alone), asserted by the human.
    assert receipt["axes"]["decision_status"]["key"] == "finding_resolved_by_user"
    assert receipt["axes"]["decision_status"]["label"] == "Finding resolved"
    assert receipt["axes"]["decision_status"]["asserted_by"] == "human"
    assert "not machine verification" in receipt["axes"]["decision_status"]["statement"]
    # A failing check is not positive proof — evidence reaches no CHECKED tier,
    # and a human dispositioning it never lifts the evidence axis.
    assert receipt["axes"]["evidence_strength"]["strongest_tier"] is None
    # Provenance of the outcome field is the human, not a check.
    assert receipt["dimensions"]["outcome"]["provenance"] == ["human"]


def test_failed_is_distinct_from_blocked() -> None:
    failed = _receipt(_task([{"work_id": "w", "latest_status": "failed", "updated_at": 100.0}]))
    assert failed["axes"]["decision_status"]["key"] == "failed"
    # A recorded-failed status is an AGENT report, not a machine assertion — its
    # outcome provenance must never borrow an unrelated passing check's source.
    assert failed["axes"]["decision_status"]["asserted_by"] == "agent_report"
    assert failed["dimensions"]["outcome"]["provenance"] == ["mcp"]
    blocked = _receipt(
        _task([{"work_id": "w", "latest_status": "blocked", "updated_at": 100.0, "blocker": "needs a key"}])
    )
    assert blocked["axes"]["decision_status"]["key"] == "blocked"
    assert blocked["axes"]["decision_status"]["asserted_by"] == "agent_report"


def _finding_attention(failing: dict[str, Any], more_text: str | None = None) -> dict[str, Any]:
    """The one attention block a failing check yields (no episode recorded)."""
    return {
        "kind": "failed_check",
        "reason_label": "Failed check",
        "summary": "pytest found a regression",
        "check_name": "pytest",
        "evidence_type": "test",
        "result": "failed",
        "result_label": "Failed",
        "result_tone": "failure",
        "exit_code": 1,
        "section_title": None,
        "label": "Failed test check · pytest · exit 1",
        "note_text": None,
        "next_step": None,
        "observed_at": 200.0,
        "source": "hook",
        "source_label": "Hook-captured",
        "action_token": None,
        "target_digest": finding_target_digest(failing),
        "revision": 0,
        "disposition_state": "open",
        "disposition_note": None,
        "open": True,
        "effects": disposition_effects("finding"),
        "more_text": more_text,
    }


def test_attention_reason_uses_current_canonical_state_and_recorded_words() -> None:
    failing = _check("failed", exit_code=1, at=200.0)
    failing["summary"] = "pytest found a regression"
    finding_task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}],
        task_checks=[failing],
    )
    assert build_attention_reason(finding_task) == (0, _finding_attention(failing))

    resolved_task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}],
        task_checks=[failing],
        finding_episodes=[
            {
                "target_digest": finding_target_digest(failing),
                "disposition_state": "resolved",
            }
        ],
    )
    assert build_attention_reason(resolved_task) is None

    newer_resolved = _check("failed", name="security scan", exit_code=1, at=300.0)
    newer_resolved["summary"] = "newer finding already resolved by the user"
    mixed_disposition_task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}],
        task_checks=[failing, newer_resolved],
        finding_episodes=[
            {
                "target_digest": finding_target_digest(newer_resolved),
                "disposition_state": "resolved",
            }
        ],
    )
    assert build_attention_reason(mixed_disposition_task) == (0, _finding_attention(failing))

    failed_task = _task(
        [
            {
                "work_id": "w",
                "latest_status": "failed",
                "updated_at": 250.0,
                "title": "deploy site",
            }
        ]
    )
    assert build_attention_reason(failed_task) == (
        0,
        {
            "kind": "failed_step",
            "reason_label": "Failed run",
            "summary": "deploy site",
            "check_name": None,
            "evidence_type": None,
            "result": None,
            "result_label": None,
            "result_tone": None,
            "exit_code": None,
            "section_title": "deploy site",
            "label": "deploy site",
            "note_text": None,
            "next_step": None,
            "observed_at": 250.0,
            "source": "mcp",
            "source_label": "Agent-reported",
            "action_token": None,
            "target_digest": None,
            "revision": 0,
            "disposition_state": "open",
            "disposition_note": None,
            "open": True,
            "effects": {"reviewed": None, "resolved": None, "reopen": None},
            "more_text": None,
        },
    )

    blocked_task = _task(
        [
            {
                "work_id": "w",
                "latest_status": "blocked",
                "updated_at": 300.0,
                "title": "publish site",
                "blocker": "need explicit approval",
                "next_step": "ask the user",
            }
        ]
    )
    assert build_attention_reason(blocked_task) == (
        1,
        {
            "kind": "blocker",
            "reason_label": "Blocker",
            "summary": "need explicit approval",
            "check_name": None,
            "evidence_type": None,
            "result": None,
            "result_label": None,
            "result_tone": None,
            "exit_code": None,
            "section_title": "publish site",
            "label": "publish site",
            "note_text": None,
            "next_step": "ask the user",
            "observed_at": 300.0,
            "source": "mcp",
            "source_label": "Agent-reported",
            "action_token": None,
            "target_digest": None,
            "revision": 0,
            "disposition_state": "open",
            "disposition_note": None,
            "open": True,
            "effects": {"reviewed": None, "resolved": None, "reopen": None},
            "more_text": None,
        },
    )

    blocked_with_finding = _task(
        blocked_task["work_items"],
        task_checks=[failing],
    )
    # The lead finding names the blocker standing behind it as a count.
    assert build_attention_reason(blocked_with_finding) == (
        0,
        _finding_attention(failing, more_text="1 more step with recorded blockers"),
    )


def test_actions_shows_touched_files_and_gaps_missing_categories() -> None:
    task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0, "files": ["src/a.py"]}],
        actions={"tool_category_counts": {}, "tool_category_total": 0, "touched_files": ["src/a.py"], "touched_file_count": 1},
    )
    actions = _receipt(task)["dimensions"]["actions"]
    assert actions["touched_files"] == ["src/a.py"]
    assert actions["tool_category_counts"] == {}
    assert any("not instrumented" in reason for reason in actions["gaps"])
    # Touched files are MCP-sourced; the missing categories are an honest gap.
    assert "mcp" in actions["provenance"]


def test_touched_files_preview_caps_and_discloses_overflow() -> None:
    from agentacct.receipt import RECEIPT_TOUCHED_FILES_PREVIEW, touched_files_preview

    files = [f"src/f{i}.py" for i in range(RECEIPT_TOUCHED_FILES_PREVIEW + 5)]
    shown, elided = touched_files_preview({"touched_files": files})
    assert shown == files[:RECEIPT_TOUCHED_FILES_PREVIEW]
    assert elided == 5  # the overflow is disclosed, never silently dropped

    # Under the cap: everything shown, nothing elided; blanks are ignored.
    shown, elided = touched_files_preview({"touched_files": ["src/a.py", "", "  "]})
    assert shown == ["src/a.py"]
    assert elided == 0

    # No files → empty preview, no overflow.
    assert touched_files_preview({}) == ([], 0)


def test_actions_dimension_bakes_the_capped_preview_and_overflow() -> None:
    # The daemon computes the preview slice + overflow ONCE, so every surface
    # renders the same values and the cap has a single source of truth.
    from agentacct.receipt import RECEIPT_TOUCHED_FILES_PREVIEW

    files = [f"src/f{i}.py" for i in range(RECEIPT_TOUCHED_FILES_PREVIEW + 3)]
    task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0, "files": files}],
        actions={"tool_category_counts": {}, "tool_category_total": 0, "touched_files": files, "touched_file_count": len(files)},
    )
    actions = _receipt(task)["dimensions"]["actions"]
    assert actions["touched_files"] == files  # full list still present
    assert actions["touched_files_preview"] == files[:RECEIPT_TOUCHED_FILES_PREVIEW]
    assert actions["touched_files_elided"] == 3


def test_actions_with_categories_declares_hook_provenance() -> None:
    task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}],
        actions={"tool_category_counts": {"read": 3}, "tool_category_total": 3, "touched_files": [], "touched_file_count": 0},
    )
    actions = _receipt(task)["dimensions"]["actions"]
    assert actions["tool_category_counts"] == {"read": 3}
    assert "hook" in actions["provenance"]


def test_actions_dimension_exposes_tool_names_ranked_with_overflow() -> None:
    from agentacct.receipt import RECEIPT_TOOL_NAMES_PREVIEW, tool_names_preview

    # A distinct tool per rank so the top-N-by-count ordering is observable.
    name_counts = {f"tool_{i:02d}": (100 - i) for i in range(RECEIPT_TOOL_NAMES_PREVIEW + 4)}
    task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}],
        actions={
            "tool_category_counts": {"execute": 1},
            "tool_category_total": 1,
            "tool_name_counts": name_counts,
            "tool_name_total": sum(name_counts.values()),
            "touched_files": [],
            "touched_file_count": 0,
        },
    )
    actions = _receipt(task)["dimensions"]["actions"]
    assert actions["tool_name_counts"] == name_counts  # full dict present
    preview = actions["tool_names_preview"]
    assert len(preview) == RECEIPT_TOOL_NAMES_PREVIEW
    assert preview[0] == {"name": "tool_00", "count": 100}  # highest count first
    assert actions["tool_names_elided"] == 4  # overflow disclosed, never dropped
    assert "hook" in actions["provenance"]

    # Unit: ties break by name ascending; blanks/non-positive filtered.
    p, elided = tool_names_preview({"tool_name_counts": {"b": 2, "a": 2, "c": 0}})
    assert [x["name"] for x in p] == ["a", "b"]
    assert elided == 0


def test_cost_dimension_carries_basis_without_gapping_a_complete_estimate() -> None:
    cost = _receipt(_task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}]))["dimensions"]["cost"]
    assert cost["cost_basis"] == "pricing_table"
    # The estimate-ness rides cost_basis / cost_confidence; a complete estimate
    # is provenance, not a gap (only incomplete/missing cost is gapped).
    assert cost["cost_confidence"] in {"estimated_from_tokens", "estimated"}
    if cost["cost_complete"]:
        assert not any("estimate" in reason for reason in cost["gaps"])


def test_identity_scope_is_project_when_project_is_explicit_without_namespace() -> None:
    # A claude-code Task in a known project: an explicit project identity but no
    # cryptographic namespace fingerprint. It is scoped to the project, so
    # identity is NOT a gap (this branch is Fix 1; the namespace branch is not).
    task = _task([{"work_id": "w", "title": "do the thing", "latest_status": "completed", "updated_at": 100.0}])
    task["sessions"][0].pop("identity_scope_state", None)
    task["sessions"][0]["project_identity_state"] = "explicit"
    boundary = _receipt(task)["dimensions"]["task"]["boundary"]
    assert boundary["identity_scope"] == "project"
    assert boundary["project_identity_state"] == "explicit"
    assert not any("could not be bound" in gap for gap in _receipt(task)["dimensions"]["task"]["gaps"])


def test_identity_is_gapped_only_when_truly_unbound() -> None:
    # No namespace fingerprint AND no explicit project identity → genuinely
    # unbound, so the identity gap fires.
    task = _task([{"work_id": "w", "title": "do the thing", "latest_status": "completed", "updated_at": 100.0}])
    task["sessions"][0].pop("identity_scope_state", None)
    task["sessions"][0].pop("project_identity_state", None)
    boundary = _receipt(task)["dimensions"]["task"]["boundary"]
    assert boundary["identity_scope"] == "unscoped"
    # The sentence is worded from the boundary fields: a project IS shown, so
    # the gap names that it was inferred — never "could not be bound".
    gaps = _receipt(task)["dimensions"]["task"]["gaps"]
    assert boundary["gap_text"] == "Project inferred from session paths, not declared."
    assert boundary["gap_text"] in gaps
    assert not any("could not be bound" in gap for gap in gaps)


def test_provenance_rollup_covers_every_dimension_with_a_legend() -> None:
    receipt = _receipt(_task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}]))
    provenance = receipt["dimensions"]["provenance"]
    for name in ("task", "actors", "actions", "cost", "evidence", "outcome"):
        assert name in provenance["by_dimension"]
        assert provenance["by_dimension"][name]  # never empty
    # The provenance map covers ONLY the six content dimensions; the gaps and
    # provenance meta-dimensions have no source of their own and must never be
    # rolled up as a spurious "none".
    assert "gaps" not in provenance["by_dimension"]
    assert "provenance" not in provenance["by_dimension"]
    for source in provenance["sources_present"]:
        assert source in provenance["legend"]


def test_provenance_never_manufactures_a_none_source_when_all_grounded() -> None:
    # A verified task with a passing check grounds every content dimension —
    # the provenance map must not advertise a phantom "none".
    check = _check("passed", at=200.0)
    task = _task(
        [
            {
                "work_id": "w",
                "latest_status": "completed",
                "updated_at": 100.0,
                "current_check_events": [check],
                "files": ["src/a.py"],
                "objective": "add rate limit",
            }
        ],
        task_checks=[check],
        actions={"tool_category_counts": {"read": 2}, "tool_category_total": 2, "touched_files": ["src/a.py"], "touched_file_count": 1},
    )
    provenance = _receipt(task)["dimensions"]["provenance"]
    assert "none" not in provenance["sources_present"]
    assert "none" not in provenance["legend"]


def test_gaps_rollup_flattens_dimension_gaps_with_labels() -> None:
    receipt = _receipt(_task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}]))
    gaps = receipt["dimensions"]["gaps"]
    assert gaps["count"] == len(gaps["items"])
    dimensions_with_gaps = {item["dimension"] for item in gaps["items"]}
    # A no-check completed step surfaces gaps under both cost (estimate) and
    # evidence (no machine checks / unverified step).
    assert "evidence" in dimensions_with_gaps


def test_summary_decision_carries_statement_and_blocker_words() -> None:
    from agentacct.receipt import build_receipt_summary

    task = _task([
        {
            "latest_status": "blocked",
            "title": "publish site",
            "section_id": "s1",
            "updated_at": 100.0,
            "blocker": "need explicit approval to publish",
            "next_step": "ask for approval",
            "kind": "implementation",
            "evidence_status": "none",
            "evidence_events": [],
        },
    ])
    summary = build_receipt_summary(task, public_task_id="task_x", title="t")
    decision = summary["decision_status"]
    assert decision["key"] == "blocked"
    assert decision["statement"]
    assert decision["blocker"]["text"] == "need explicit approval to publish"
    assert decision["blocker"]["next_step"] == "ask for approval"

    receipt = build_receipt(task, public_task_id="task_x", title="t")
    axis = receipt["axes"]["decision_status"]
    assert axis["blocker"]["text"] == "need explicit approval to publish"

    # Non-blocked rows carry the key with a None value (uniform shape).
    done = build_receipt_summary(
        _task([
            {
                "latest_status": "completed",
                "updated_at": 100.0,
                "kind": "implementation",
                "evidence_status": "none",
                "evidence_events": [],
            }
        ]),
        public_task_id="task_y",
        title="t",
    )
    assert done["decision_status"]["blocker"] is None
    assert done["decision_status"]["statement"]


def test_receipt_checks_carry_detail_fields_without_command_text() -> None:
    check = _check("passed", name="pytest", at=300.0)
    check["summary"] = "pytest exited with code 0."
    check["files"] = ["src/agentacct/receipt.py", ""]
    check["command_redacted"] = True
    task = _task(
        [
            {
                "latest_status": "completed",
                "updated_at": 100.0,
                "kind": "implementation",
                "evidence_status": "none",
                "evidence_events": [],
            }
        ],
        task_checks=[check],
    )
    receipt = build_receipt(task, public_task_id="task_x", title="t")
    row = receipt["dimensions"]["evidence"]["checks"][0]
    assert row["summary"] == "pytest exited with code 0."
    assert row["files"] == ["src/agentacct/receipt.py"]
    assert row["command_redacted"] is True
    # The store never records command text; the payload must not invent one.
    assert "command" not in row


def test_plan_share_headline_is_calibrated_or_nothing() -> None:
    # Calibrated: a real percentage, with the <0.1% band and an honest ≈0%.
    assert (
        plan_share_headline({"pct": 12.2, "calibration_state": "calibrated"})
        == "≈12.2% of weekly plan"
    )
    assert (
        plan_share_headline({"pct": 0.05, "calibration_state": "calibrated"})
        == "≈<0.1% of weekly plan"
    )
    assert (
        plan_share_headline({"pct": 0.0, "calibration_state": "calibrated"})
        == "≈0% of weekly plan"
    )
    # Not calibrated: a NAMED state, never a number — even when a pct is present.
    assert (
        plan_share_headline({"pct": 9.9, "calibration_state": "calibrating"})
        == "calibrating — not enough 7-day history yet"
    )
    assert (
        plan_share_headline({"pct": None, "calibration_state": "never"})
        == "not applicable for this client"
    )
    # A calibrated fit without a share, and a fit that will not calibrate at
    # the current ratio, are named states too.
    # A calibrated fit without a share for THIS row, and a fit that will not
    # calibrate at the current ratio, are two different named states.
    assert plan_share_headline({"pct": None, "calibration_state": "calibrated"}) == "no priced usage in this session"
    assert (
        plan_share_headline({"pct": None, "calibration_state": "out_of_band"})
        == "won't calibrate at current ratio"
    )
    # Absent payload is a named absence — never a dash, never a fabricated zero.
    assert plan_share_headline(None) == "plan share not reported"
    assert plan_share_headline({}) == "plan share not reported"


# --- Verdict-first receipt ----------------------------------------------------
# The receipt leads with ONE honest line joining decision + evidence, a single
# gap, and a time-bounded proof claim. These assert the line degrades honestly.

from agentacct.receipt import (  # noqa: E402
    verdict_gap_line,
    verdict_headline,
    verdict_health_window,
)


def _dated_session_task(items: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    """A task whose one session carries first/last activity, so the health
    window has a datable bound."""
    task = _task(items, **kw)
    task["sessions"][0]["first_activity_at"] = 1_700_000_000.0  # 2023-11-14 UTC
    task["sessions"][0]["last_activity_at"] = 1_700_100_000.0
    return task


def test_verdict_reads_the_zero_ratio_when_a_completed_step_has_no_check() -> None:
    receipt = _receipt(_task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}]))
    verdict = receipt["verdict"]
    # One tier vocabulary: the ratio, never "claimed" or "unproven".
    assert verdict["headline"] == "Reported — 0/1 checked"
    assert verdict["proof_clause"] == "0/1 checked"
    assert verdict["decision_key"] == "reported"
    assert verdict["evidence_key"] == "unchecked"
    # The unchecked count is stated once, on the gap line.
    assert verdict["gap_text"] == "1 completed step unchecked"


def test_verdict_upgrades_to_the_coverage_ratio_when_a_check_proves_the_work() -> None:
    check = _check("passed", at=200.0)
    task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0, "current_check_events": [check]}],
        task_checks=[check],
    )
    verdict = _receipt(task)["verdict"]
    assert "unchecked" not in verdict["headline"]
    assert "independently checked" in verdict["headline"]


def test_verdict_is_not_gradeable_for_a_pure_research_task() -> None:
    task = _task([{"work_id": "w", "latest_status": "completed", "kind": "research", "updated_at": 100.0}])
    verdict = _receipt(task)["verdict"]
    assert verdict["headline"] == "Reported — not gradeable (no checkable steps)"
    assert verdict["proof_clause"] == "Not gradeable (no checkable steps)"
    # Scope owes no proof: no "Not yet proven" label, the count stays in the ledger.
    assert verdict["gap_label"] is None
    assert verdict["gap_text"] is None
    assert verdict["ledger_text"] == "1 not check-relevant"


def test_verdict_gap_is_none_when_fully_proven_and_costed() -> None:
    assert (
        verdict_gap_line(
            {"by_tier": {"unchecked": 0}, "hidden_in_subagents": 0, "not_checkable": 0,
             "unattributed_checks": 0, "open_or_incomplete": 0},
            {"estimated_cost_usd": 1.0, "cost_complete": True},
        )
        is None
    )


def test_verdict_gap_names_missing_usage_and_partial_cost() -> None:
    evidence = {"by_tier": {"unchecked": 0}, "hidden_in_subagents": 0, "not_checkable": 0,
                "unattributed_checks": 0, "open_or_incomplete": 0}
    # Absence and partiality are mutually exclusive cost states.
    assert verdict_gap_line(evidence, {"estimated_cost_usd": None}) == "no usage recorded"
    assert verdict_gap_line(evidence, {"estimated_cost_usd": None, "rows": 3}) == "usage unpriced"
    assert (
        verdict_gap_line(evidence, {"estimated_cost_usd": 1.0, "cost_complete": False})
        == "cost is a partial subtotal"
    )


def test_verdict_health_window_is_bounded_by_the_tasks_own_start_date() -> None:
    receipt = _receipt(_dated_session_task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}]))
    window = receipt["verdict"]["health_window"]
    # The LOCAL calendar day — the basis every other date uses — never UTC.
    from datetime import datetime

    from agentacct.display_vocabulary import display_date

    assert window["since_iso"] == datetime.fromtimestamp(1_700_000_000.0).date().isoformat()
    assert window["since_date"] == display_date(1_700_000_000.0)
    assert window["proven"] == 0
    assert window["checkable"] == 1
    assert window["tier_word"] == "checked"
    # The date bound only: the count is stated once, in the verdict headline.
    assert window["text"] == f"Counts since {display_date(1_700_000_000.0)}"


def test_verdict_health_window_is_none_without_a_datable_window() -> None:
    # The default _task session has no first_activity_at / started_at.
    assert _receipt(_task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}]))["verdict"]["health_window"] is None


def test_verdict_health_window_is_none_when_nothing_is_checkable() -> None:
    # A research-only (not gradeable) task has no proof claim to bound in time;
    # "0 of 0 proven" would be noise, so the health window is suppressed.
    task = _dated_session_task([{"work_id": "w", "latest_status": "completed", "kind": "research", "updated_at": 100.0}])
    assert _receipt(task)["verdict"]["health_window"] is None


def test_list_summary_and_full_receipt_word_the_verdict_identically() -> None:
    from agentacct.receipt import build_receipt_summary

    task = _dated_session_task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}])
    full = build_receipt(task, public_task_id="task_x", title="Add rate limit")["verdict"]["headline"]
    row = build_receipt_summary(task, public_task_id="task_x", title="Add rate limit")["verdict"]["headline"]
    assert full == row


# The pluralization helper itself is ``agentacct.plural.count_noun`` and is unit
# tested in ``tests/test_plural.py`` (singular/zero/plural, an explicit irregular
# form, and compound nouns). What stays here is the receipt-level guarantee that
# no rendered surface leaks the ungrammatical counting copy.


def test_no_rendered_receipt_leaks_the_paren_s_or_one_checks_bug() -> None:
    from agentacct.receipt_markdown import render_receipt_markdown

    task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0,
          "client_session_id": "sub", "kind": "code"}],
        actions={"tool_category_counts": {"edit": 1}, "tool_category_total": 1,
                 "touched_files": ["a.py"], "touched_file_count": 1, "command_count": 1},
    )
    out = render_receipt_markdown(build_receipt(task, public_task_id="task_x", title="t"))
    assert "(s)" not in out
    assert "1 checks" not in out


# --- Mechanical git revision on a check ("this revision passed this check") ---


def test_check_surfaces_the_environment_captured_revision() -> None:
    check = _check("passed", at=200.0)
    check.update({
        "git_commit": "abc123def4567890",
        "git_branch": "main",
        "git_dirty": False,
        "git_revision_basis": "server_captured_at_record",
    })
    task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0, "current_check_events": [check]}],
        task_checks=[check],
    )
    revision = _receipt(task)["dimensions"]["evidence"]["checks"][0]["revision"]
    assert revision == {
        "commit": "abc123def4567890",
        "branch": "main",
        "dirty": False,
        "basis": "server_captured_at_record",
    }


def test_check_revision_is_none_when_no_capture_path_stamped_one() -> None:
    check = _check("passed", at=200.0)
    task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0, "current_check_events": [check]}],
        task_checks=[check],
    )
    assert _receipt(task)["dimensions"]["evidence"]["checks"][0]["revision"] is None


# --- One payload vocabulary (B1 receipt contract) ------------------------------


def _passing_step(status: str, *, name: str = "pytest", at: float = 200.0, **extra: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    check = _check("passed", name=name, at=at, source_type="mcp_agent_reported")
    item = {"work_id": f"w-{status}-{name}", "latest_status": status, "updated_at": 100.0,
            "current_check_events": [check], **extra}
    return item, check


def test_terminal_stops_are_named_buckets_not_still_open() -> None:
    blocked = {"work_id": "b", "latest_status": "blocked", "updated_at": 100.0, "blocker": "needs a key"}
    handed = {"work_id": "h", "latest_status": "handed_off", "updated_at": 100.0}
    started = {"work_id": "s", "latest_status": "checkpoint", "updated_at": 100.0}
    evidence = _receipt(_task([blocked, handed, started]))["axes"]["evidence_strength"]
    assert evidence["still_open"] == 1
    assert evidence["stopped_blocked"] == 1
    assert evidence["stopped_handed_off"] == 1
    # The legacy key still counts every step outside the ratio.
    assert evidence["open_or_incomplete"] == 3
    from agentacct.receipt import evidence_gap_parts

    assert evidence_gap_parts(evidence) == [
        "1 step still open",
        "1 step blocked",
        "1 step handed off",
    ]


def test_a_handed_off_step_with_passing_checks_is_graded_for_the_work_it_did() -> None:
    item, check = _passing_step("handed_off")
    receipt = _receipt(_task([item], task_checks=[check]))
    evidence = receipt["axes"]["evidence_strength"]
    assert evidence["gradeable"] is True
    assert evidence["by_tier"]["self_checked"] == 1
    assert evidence["stopped_handed_off"] == 0
    assert "still open" not in (receipt["verdict"]["gap"] or "")
    # The decision word is unchanged by grading.
    assert receipt["axes"]["decision_status"]["key"] == "handed_off"
    assert receipt["axes"]["decision_status"]["label"] == "Handed off"


def test_a_review_step_with_an_attached_check_is_checkable() -> None:
    item, check = _passing_step("completed", kind="review")
    evidence = _receipt(_task([item], task_checks=[check]))["axes"]["evidence_strength"]
    assert evidence["checkable_total"] == 1
    assert evidence["not_checkable"] == 0


def test_gap_parts_follow_actionability_order_with_subagent_share_inside_buckets() -> None:
    from agentacct.receipt import evidence_gap_parts

    evidence = {
        "gradeable": True,
        "by_tier": {"unchecked": 2},
        "still_open": 1,
        "stopped_blocked": 1,
        "stopped_handed_off": 0,
        "stopped_failed": 0,
        "unattributed_checks": 2,
        "not_checkable": 3,
        "hidden_in_subagents": 2,
        "subagents_by_bucket": {"not_checkable": 2},
    }
    assert evidence_gap_parts(evidence) == [
        "2 completed steps unchecked",
        "1 step still open",
        "1 step blocked",
        "2 checks not linked to a step",
        "3 not check-relevant (2 in subagents)",
    ]


def test_verdict_gap_splits_evidence_from_cost_and_labels_only_unproven_work() -> None:
    item, check = _passing_step("completed")
    proven = _receipt(_task([item], task_checks=[check], usage={"rows": 0}))["verdict"]
    assert proven["gap_label"] is None
    assert proven["gap_text"] is None
    assert proven["gap_evidence"] == []
    assert proven["gap_cost"] == ["no usage recorded"]
    # The legacy joined gap keeps working for current readers.
    assert proven["gap"] == "no usage recorded"

    unproven = _receipt(_task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}]))["verdict"]
    assert unproven["gap_label"] == "Not yet proven"
    assert unproven["gap_text"] == "1 completed step unchecked"


def test_evidence_ships_coverage_and_check_strings_with_named_absence() -> None:
    item, check = _passing_step("completed")
    unchecked = {"work_id": "u", "latest_status": "completed", "updated_at": 100.0}
    evidence = _receipt(_task([item, unchecked], task_checks=[check]))["axes"]["evidence_strength"]
    assert evidence["coverage_hero"] == "1/2 self-checked · 1 unchecked"
    assert evidence["coverage_row"] == "1/2 self-checked"
    assert evidence["coverage_tile"] == {"value": "1/2", "absent": None, "qualifier": "self-checked completed steps"}
    assert evidence["checks_tile"] == {"value": "1/1", "absent": None, "qualifier": "1 passed"}
    assert evidence["check_tally_text"] == "1/1 passed"
    assert evidence["check_runs_state"] == "passed"
    assert [row["key"] for row in evidence["tier_legend"]] == [
        "externally_verified", "independently_checked", "self_checked", "unchecked",
    ]

    research = _task([{"work_id": "r", "latest_status": "completed", "kind": "research", "updated_at": 100.0}])
    empty = _receipt(research)["axes"]["evidence_strength"]
    assert empty["coverage_row"] == "not gradeable"
    # Absence is a named state, never a value in the metric face.
    assert empty["coverage_tile"] == {"value": None, "absent": "not gradeable", "qualifier": "no checkable steps"}
    assert empty["checks_tile"] == {"value": None, "absent": "no checks recorded", "qualifier": None}
    assert empty["check_runs_state"] == "none"


def test_check_run_history_names_earlier_failures_and_supersession() -> None:
    from agentacct.receipt import check_tally_text

    first = _check("failed", name="pytest", at=150.0, exit_code=1)
    first["event_id"] = "evt_first_fail"
    later = _check("passed", name="pytest", at=200.0)
    task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0, "current_check_events": [later]}],
        task_checks=[first, later],
    )
    receipt = _receipt(task)
    rows = receipt["dimensions"]["evidence"]["checks"]
    # The failing run is KEPT, marked as history, so the fail→pass story can be
    # rendered; the standing pass is still the frontier the tally counts.
    assert [row["result"] for row in rows] == ["failed", "passed"]
    assert rows[0]["history_run"] is True and rows[0]["superseded"] is True
    assert rows[0]["superseded_by_event_id"] == rows[1]["event_id"]
    # The reciprocal pointer the reducer synthesizes: the pass names the failure.
    assert rows[1]["supersedes_check_event_id"] == "evt_first_fail"
    assert rows[1]["supersedes_basis"] == "reciprocal_of_supersession"
    row = rows[1]
    assert row["history_run"] is False
    assert row["runs_total"] == 2
    assert row["earlier_failed"] == 1
    assert receipt["axes"]["evidence_strength"]["check_tally_text"] == "1/1 passed · 1 earlier run failed"
    assert check_tally_text(
        {"checks_total": 156, "checks_passed": 150, "checks_failed": 4, "checks_superseded": 2}
    ) == "150/156 passed · 4 failed · 2 superseded"


def test_only_the_servers_own_placeholder_summary_reads_as_absent() -> None:
    """The one summary that is hidden is one no agent wrote."""

    from agentacct.receipt import check_recorded_summary

    # Exactly what a released server stored when `summary` was omitted.
    assert check_recorded_summary({"name": "pytest", "result": "passed", "summary": "pytest: passed"}) is None
    # Anything else is the agent's and is shown as written -- including prose
    # that merely resembles the placeholder, which is not ours to second-guess.
    for written in ("pytest passed", "pytest: passed.", "Pytest: Passed", "pytest: passed on CI"):
        check = {"name": "pytest", "result": "passed", "summary": written}
        assert check_recorded_summary(check) == written
    assert check_recorded_summary({"name": "pytest", "result": "passed"}) is None


def test_check_rows_carry_title_revision_artifact_and_command_state() -> None:
    check = _check("passed", name="check", at=200.0)
    check.update({
        "summary": "check: passed",
        "evidence_type": "build",
        "artifact_path": "secret/abs/path",
        "artifact_path_redacted": True,
        "command_redacted": True,
        "git_commit": "8a4e0240abcdef",
        "git_branch": "main",
        "git_dirty": True,
        "git_revision_basis": "server_captured_at_record",
    })
    task = _task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}], task_checks=[check])
    row = _receipt(task)["dimensions"]["evidence"]["checks"][0]
    # "check" is the placeholder a lane writes for an unnamed check, and
    # "<name>: <result>" is what released servers stored for an omitted summary.
    # Neither is the agent's, so the title falls through to the evidence type.
    assert row["name"] is None
    assert row["summary"] is None
    assert row["title"] == "build"
    # Server-at-record basis: the label says WHEN HEAD was read, never "at".
    assert row["revision_label"] == "HEAD when recorded: 8a4e024 · main · uncommitted changes"
    assert row["artifact_path"] is None
    assert row["artifact_path_redacted"] is True
    # No command_state stamped (a legacy row): the old sentence still applies.
    assert row["command_state_text"] == COMMAND_NOT_SHOWN_TEXT
    assert "command" not in row

    plain = _check("passed", name="pytest tests/test_x.py", at=200.0)
    plain["artifact_path"] = "reports/junit.xml"
    task = _task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}], task_checks=[plain])
    row = _receipt(task)["dimensions"]["evidence"]["checks"][0]
    assert row["title"] == "pytest tests/test_x.py"
    assert row["artifact_path"] == "reports/junit.xml"
    assert row["revision_label"] == "revision not captured"
    assert row["command_state_text"] is None


def test_boundary_gap_text_is_worded_from_the_boundary_fields() -> None:
    from agentacct.receipt import boundary_gap_text

    assert boundary_gap_text("agentacct", "conflicting") == "Sessions in this Task report different projects."
    assert boundary_gap_text(None, "missing") == "No project recorded for this Task."
    assert boundary_gap_text("agentacct", "missing") == "Project inferred from session paths, not declared."
    assert boundary_gap_text("agentacct", "explicit") is None

    task = _task([{"work_id": "w", "title": "t", "latest_status": "completed", "updated_at": 100.0}])
    task["sessions"][0]["project_identity_state"] = "conflicting"
    gaps = _receipt(task)["dimensions"]["task"]["gaps"]
    assert "Sessions in this Task report different projects." in gaps


def test_cost_state_is_one_state_and_carries_its_display_strings() -> None:
    from agentacct.display_vocabulary import COST_LEGEND

    complete = _receipt(_task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}]))["dimensions"]["cost"]
    assert complete["state"] == "complete"
    assert complete["display_text"] == "≈$0.50"
    assert complete["basis_label"] == "pricing estimate"
    assert complete["legend"] == COST_LEGEND
    assert complete["gap_text"] is None

    none = _receipt(_task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}], usage={"rows": 0}))["dimensions"]["cost"]
    assert none["state"] == "no_usage"
    assert none["display_text"] == "no usage recorded"
    # Absence never also claims partial coverage.
    assert none["gaps"] == ["No usage was recorded for this Task."]

    unpriced = _receipt(_task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}], usage={"rows": 3}))["dimensions"]["cost"]
    assert unpriced["state"] == "unpriced"
    assert unpriced["display_text"] == "unpriced"
    assert not any("incomplete" in gap for gap in unpriced["gaps"])

    partial = _receipt(_task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}],
        usage={"rows": 3, "estimated_cost_usd": 1554.671, "cost_complete": False, "cost_basis": "pricing_table"},
    ))["dimensions"]["cost"]
    assert partial["state"] == "partial"
    assert partial["display_text"] == "~$1,554.67"
    assert partial["gaps"] == ["Cost is incomplete: some usage rows are unpriced or excluded."]


def test_summary_rows_carry_cost_display_confidence_and_attention() -> None:
    from agentacct.receipt import build_receipt_summary

    failing = _check("failed", exit_code=1, at=200.0)
    task = _task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}], task_checks=[failing],
                 usage={"rows": 1, "estimated_cost_usd": 1.3, "cost_complete": True,
                        "cost_basis": "client_reported", "cost_confidence": "client_reported"})
    row = build_receipt_summary(task, public_task_id="task_x", title="t")
    assert row["cost"]["cost_confidence"] == "client_reported"
    assert row["cost"]["display_text"] == "$1.30"
    assert row["cost"]["basis_label"] == "client-reported"
    assert row["attention"]["reason_label"] == "Failed check"
    assert row["attention_open"] is True
    assert row["decision_status"]["label"] == "Finding"
    full = _receipt(task)
    assert full["attention"] == row["attention"]
    assert full["attention_open"] is True

    clean = build_receipt_summary(_task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}]),
                                  public_task_id="task_y", title="t")
    assert clean["attention"] is None
    assert clean["attention_open"] is False


def test_decision_and_provenance_labels_are_shared_vocabulary() -> None:
    receipt = _receipt(_task([{"work_id": "w", "latest_status": "started", "updated_at": 100.0}]))
    decision = receipt["axes"]["decision_status"]
    assert decision["label"] == "In progress"
    assert decision["asserted_by_label"] == "Agent-reported"
    assert receipt["dimensions"]["outcome"]["asserted_by_label"] == "Agent-reported"
    sources = receipt["dimensions"]["provenance"]["sources"]
    assert {row["key"]: row["label"] for row in sources}["client_log"] == "Client log"
    assert receipt["field_labels"]["checks"] == "Checks"


def test_outcome_carries_the_agent_summary_and_the_recorded_next_step() -> None:
    items = [
        {"work_id": "a", "latest_status": "completed", "updated_at": 100.0, "summary": "old summary"},
        {"work_id": "b", "latest_status": "handed_off", "updated_at": 200.0, "title": "Wire the API",
         "summary": "Wired half the routes.", "next_step": "Wire /v1/tasks next"},
    ]
    outcome = _receipt(_task(items))["dimensions"]["outcome"]
    assert outcome["summary"] == "Wired half the routes."
    assert outcome["summary_label"] == "Agent-reported"
    assert outcome["next_step"] == "Wire /v1/tasks next"

    done = _receipt(_task([{"work_id": "a", "latest_status": "completed", "updated_at": 100.0,
                            "summary": "done", "next_step": "stale"}]))["dimensions"]["outcome"]
    assert done["next_step"] is None


# --- check results: "could not run" is a named gap, never a failure ----------


def _errored(name: str, *, at: float, exit_code: int) -> dict[str, Any]:
    check = _check("error", name=name, at=at, exit_code=exit_code)
    check["event_id"] = f"evt_{name}_error_{int(at)}"
    return check


def test_checks_that_could_not_run_are_a_named_gap_not_a_finding() -> None:
    pytest_error = _errored("pytest", at=200.0, exit_code=4)
    mypy_error = _errored("mypy", at=210.0, exit_code=1)
    mypy_error["evidence_type"] = "typecheck"
    task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}],
        task_checks=[pytest_error, mypy_error],
    )
    receipt = _receipt(task)
    decision = receipt["axes"]["decision_status"]
    evidence = receipt["dimensions"]["evidence"]

    assert decision["key"] != "finding"
    assert decision["asserted_by"] != "machine"
    assert evidence["checks_failed"] == 0
    assert evidence["checks_not_run"] == 2
    assert evidence["check_tally_text"] == "0/2 passed · 2 could not run"
    assert evidence["checks_tile"] == {"value": "0/2", "absent": None, "qualifier": "0 passed · 2 could not run"}
    # A check that could not run owes no step proof claim: a ledger part.
    assert "2 checks could not run" in receipt["verdict"]["ledger_evidence"]
    assert {row["result_label"] for row in evidence["checks"]} == {"Could not run"}
    assert {row["result_tone"] for row in evidence["checks"]} == {"not_run"}

    # It still needs a look — as its own muted reason, never "Failed check".
    attention = receipt["attention"]
    assert attention["kind"] == "check_not_run"
    assert attention["reason_label"] == "Check could not run"
    assert attention["label"] == "Typecheck check · mypy · exit 1"
    assert attention["result_tone"] == "not_run"
    assert attention["more_text"] == "1 more check could not run"
    assert build_attention_reason(task)[0] == 2


def test_a_later_run_that_could_not_run_never_clears_a_standing_failure() -> None:
    failed = _check("failed", at=200.0, exit_code=1)
    errored = _errored("pytest", at=300.0, exit_code=4)
    task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}],
        task_checks=[failed, errored],
    )
    receipt = _receipt(task)
    assert receipt["axes"]["decision_status"]["key"] == "finding"
    assert receipt["attention"]["kind"] == "failed_check"
    assert receipt["dimensions"]["evidence"]["checks_failed"] == 1

    # Only a later PASS supersedes the failure; a run that could not run after
    # that pass leaves the series unproven (never verified, never a finding).
    passed = _check("passed", at=400.0)
    errored_after_pass = _errored("pytest", at=500.0, exit_code=4)
    recovered = _receipt(
        _task(
            [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}],
            task_checks=[failed, passed, errored_after_pass],
        )
    )
    assert recovered["axes"]["decision_status"]["key"] not in {"finding", "verified"}
    assert recovered["dimensions"]["evidence"]["checks_not_run"] == 1


def test_checks_tile_names_the_earlier_failed_run_the_tally_names() -> None:
    failed = _check("failed", at=200.0, exit_code=1)
    passed = _check("passed", at=300.0)
    task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}],
        task_checks=[passed],
    )
    task["task_evidence_events"] = [failed, passed]
    evidence = _receipt(task)["dimensions"]["evidence"]
    assert evidence["check_tally_text"] == "1/1 passed · 1 earlier run failed"
    assert evidence["checks_tile"] == {"value": "1/1", "absent": None, "qualifier": "1 passed · 1 earlier run failed"}


def test_failed_check_that_exited_zero_names_the_disagreement() -> None:
    probe = _check("failed", name="reproduce", kind="build", at=200.0, exit_code=0)
    receipt = _receipt(
        _task(
            [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}],
            task_checks=[probe],
        )
    )
    note = "Recorded as failed although the command exited 0."
    # Display only: the decision stays what the recorded result says.
    assert receipt["axes"]["decision_status"]["key"] == "finding"
    assert receipt["attention"]["note_text"] == note
    assert receipt["dimensions"]["evidence"]["checks"][0]["note_text"] == note


def test_more_text_counts_every_other_open_finding() -> None:
    first = _check("failed", name="pytest", at=200.0, exit_code=1)
    second = _check("failed", name="ruff", kind="lint", at=210.0, exit_code=1)
    receipt = _receipt(
        _task(
            [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}],
            task_checks=[first, second],
        )
    )
    assert receipt["attention"]["check_name"] == "ruff"
    assert receipt["attention"]["more_text"] == "1 more open finding"


def test_attention_lines_print_the_note_and_the_more_count() -> None:
    from agentacct.receipt_markdown import receipt_attention_lines

    probe = _check("failed", name="reproduce", kind="build", at=200.0, exit_code=0)
    other = _check("failed", name="pytest", at=100.0, exit_code=1)
    lines = receipt_attention_lines(
        _receipt(
            _task(
                [{"work_id": "w", "latest_status": "completed", "updated_at": 50.0}],
                task_checks=[probe, other],
            )
        )
    )
    assert "Recorded as failed although the command exited 0." in lines
    assert lines[-1] == "1 more open finding"


def test_session_step_tally_uses_the_receipt_grammar() -> None:
    from agentacct.v1_sessions import step_check_tally_text

    events = [
        {"result": "passed"},
        {"result": "error"},
        {"result": "failed", "supersession_state": "superseded"},
        {"result": "skipped"},
    ]
    assert step_check_tally_text(events) == "1/4 passed · 1 could not run · 1 skipped · 1 superseded"
    assert step_check_tally_text([]) == "no checks recorded"


def test_timeline_check_event_carries_result_words_and_tone() -> None:
    from agentacct.task_timeline import build_timeline_events

    errored = _errored("mypy", at=200.0, exit_code=1)
    task = _task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}])
    task["task_evidence_events"] = [errored]
    events = [event for event in build_timeline_events(task) if event["kind"] == "check"]
    assert events[0]["status_label"] == "Could not run"
    assert events[0]["result_tone"] == "not_run"
    assert events[0]["is_current_failure"] is False


# --- One verdict/coverage/gap vocabulary (round-2 G2) ---------------------------


def test_not_gradeable_reason_names_the_bucket_that_made_the_denominator_zero() -> None:
    from agentacct.receipt import not_gradeable_reason

    # The only step was handed off with a failing check linked to it: the
    # reason names the stop, never "no checkable steps" beside recorded checks.
    failing = _check("failed", exit_code=1, at=200.0)
    handed = {"work_id": "h", "latest_status": "handed_off", "updated_at": 100.0,
              "current_check_events": [failing]}
    receipt = _receipt(_task([handed], task_checks=[failing]))
    assert receipt["axes"]["evidence_strength"]["checks_total"] == 1
    assert receipt["verdict"]["proof_clause"] == "Not gradeable (only step stopped: handed off)"
    assert receipt["verdict"]["headline"].endswith("— not gradeable (only step stopped: handed off)")
    assert receipt["axes"]["evidence_strength"]["coverage_tile"]["qualifier"] == "only step stopped: handed off"

    blocked = {"work_id": "b", "latest_status": "blocked", "updated_at": 100.0, "blocker": "needs a key"}
    assert _receipt(_task([blocked]))["verdict"]["headline"] == "Blocked — not gradeable (only step stopped: blocked)"

    research = {"work_id": "r", "latest_status": "completed", "kind": "research", "updated_at": 100.0}
    assert not_gradeable_reason(_receipt(_task([research]))["axes"]["evidence_strength"]) == "no checkable steps"
    assert not_gradeable_reason(_receipt(_task([]))["axes"]["evidence_strength"]) == "no steps recorded"
    open_step = {"work_id": "o", "latest_status": "checkpoint", "updated_at": 100.0}
    assert not_gradeable_reason(_receipt(_task([open_step]))["axes"]["evidence_strength"]) == "only step still open"
    assert (
        not_gradeable_reason(_receipt(_task([open_step, research]))["axes"]["evidence_strength"])
        == "no finished checkable steps"
    )


def test_not_yet_proven_labels_only_the_unchecked_part_of_a_mixed_task() -> None:
    from agentacct.receipt_markdown import receipt_lead

    item, check = _passing_step("completed")
    unchecked = {"work_id": "u", "latest_status": "completed", "updated_at": 100.0}
    blocked = {"work_id": "b", "latest_status": "blocked", "updated_at": 100.0, "blocker": "key"}
    research = {"work_id": "r", "latest_status": "completed", "kind": "research", "updated_at": 100.0}
    receipt = _receipt(_task([item, unchecked, blocked, research], task_checks=[check]))
    verdict = receipt["verdict"]
    assert verdict["gap_label"] == "Not yet proven"
    assert verdict["gap_text"] == "1 completed step unchecked"
    assert verdict["ledger_text"] == "1 step blocked · 1 not check-relevant"
    evidence = receipt["axes"]["evidence_strength"]
    assert evidence["coverage_ledger"] == "1 step blocked · 1 not check-relevant"
    # The scope term is defined once, never inside each part.
    assert evidence["scope_definition"] == "Not check-relevant: review, research, planning, docs"
    lead = receipt_lead(receipt)
    assert lead["gap_line"] == "Not yet proven: 1 completed step unchecked"
    assert "blocked" not in lead["gap_line"]

    # A stop-only or scope-only task owes no proof: no label on any surface.
    for items in ([blocked], [research]):
        only = _receipt(_task(items))
        assert only["verdict"]["gap_label"] is None
        assert receipt_lead(only)["gap_line"] == ""
        summary = build_receipt_summary(_task(items), public_task_id="task_x", title="Add rate limit")
        assert summary["verdict"]["gap_label"] is None


def test_each_coverage_fact_is_stated_once_in_the_verdict_header() -> None:
    import re

    item, check = _passing_step("completed")
    unchecked = {"work_id": "u", "latest_status": "completed", "updated_at": 100.0}
    task = _task([item, unchecked], task_checks=[check])
    task["sessions"][0]["first_activity_at"] = 1_700_000_000.0
    receipt = _receipt(task)
    verdict = receipt["verdict"]
    header = [verdict["headline"], verdict["gap_text"] or "", verdict["ledger_text"] or "",
              verdict["health_window"]["text"]]
    joined = " | ".join(header)
    # The ratio, its tier word and the unchecked count each appear exactly once.
    assert len(re.findall(r"\b1/2\b", joined)) == 1
    assert joined.count("self-checked") == 1
    assert joined.count("unchecked") == 1
    # One tier noun: never "claimed" or "unproven" in a headline, gap or tile.
    evidence = receipt["axes"]["evidence_strength"]
    tiles = [str(v) for tile in (evidence["coverage_tile"], evidence["checks_tile"]) for v in tile.values() if v]
    for text in [*header, *tiles, verdict["proof_clause"], evidence["coverage_hero"]]:
        assert "claimed" not in text and "unproven" not in text


def test_asserted_by_prose_uses_the_phrase_and_chips_keep_the_label() -> None:
    from agentacct.display_vocabulary import ASSERTED_BY_LABELS, ASSERTED_BY_PHRASES
    from agentacct.receipt_markdown import render_receipt_markdown

    assert set(ASSERTED_BY_PHRASES) == set(ASSERTED_BY_LABELS)
    receipt = _receipt(_task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}]))
    decision = receipt["axes"]["decision_status"]
    assert decision["asserted_by"] == "agent_report"
    assert decision["asserted_by_label"] == "Agent-reported"
    assert decision["asserted_by_phrase"] == "the agent's report"
    md = render_receipt_markdown(receipt)
    assert "asserted by the agent's report" in md
    assert "asserted by Agent-reported" not in md


def test_a_task_with_no_work_steps_names_that_absence() -> None:
    receipt = _receipt(_task([]))
    decision = receipt["axes"]["decision_status"]
    assert decision["key"] == "observed"
    assert decision["label"] == "No work recorded"
    assert decision["statement"] == "agentacct saw this Task but no work steps were recorded."
    assert receipt["verdict"]["headline"] == "No work recorded — not gradeable (no steps recorded)"
    from agentacct.display_vocabulary import decision_label

    assert decision_label("unknown") == "No outcome recorded"


def test_attention_label_never_repeats_the_reason_or_the_task_title() -> None:
    from agentacct.receipt import attention_label
    from agentacct.receipt_markdown import receipt_attention_lines

    assert attention_label("blocker", section_title="Add rate limit", task_title="Add rate limit") == ""
    assert attention_label("blocker", section_title="deploy", task_title="Add rate limit") == "deploy"
    assert attention_label("failed_check", evidence_type="typecheck", check_name="mypy", exit_code=1) == (
        "Failed typecheck check · mypy · exit 1"
    )
    blocked = {"work_id": "b", "latest_status": "blocked", "updated_at": 100.0,
               "blocker": "needs a key", "title": "Add rate limit"}
    receipt = _receipt(_task([blocked]))
    lines = receipt_attention_lines(receipt)
    assert lines[:2] == ["Blocker", ""]
    # A step title is the agent's own words and prints as written, even when it
    # happens to contain the reason noun. Only a failed check's label is split,
    # because only that one is built by the reducer.
    titled = {"attention": {"kind": "blocker", "reason_label": "Blocker",
                            "label": "Triage the release blocker in CI"}}
    assert receipt_attention_lines(titled)[:2] == ["Blocker", "Triage the release blocker in CI"]
    failed = {"attention": {"kind": "failed_check", "reason_label": "Failed check",
                            "label": "Failed typecheck check · mypy · exit 1"}}
    assert receipt_attention_lines(failed)[:2] == ["Failed typecheck check", "mypy · exit 1"]


def test_vocabulary_built_display_strings_never_leak_snake_case_keys() -> None:
    import re

    from agentacct import display_vocabulary as vocab
    from agentacct.task_outcome import step_evidence_grade

    snake = re.compile(r"\b[a-z]+_[a-z]+\b")

    def strings(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [s for v in value.values() for s in strings(v)]
        if isinstance(value, (list, tuple)):
            return [s for v in value for s in strings(v)]
        return []

    # Values only (keys are data). Legends that name raw keys are not display text.
    shown = []
    for name in ("DECISION_LABELS", "ASSERTED_BY_LABELS", "ASSERTED_BY_PHRASES", "STOP_LABELS",
                 "ATTENTION_REASON_LABELS", "CHECK_RESULT_LABELS", "EVIDENCE_GRADE_LABELS", "TIER_LABELS"):
        shown.extend(strings(getattr(vocab, name)))
    for status in ("handed_off", "blocked", "failed", "started", "checkpoint", ""):
        shown.append(step_evidence_grade({"latest_status": status})["reason"])
    handed = {"work_id": "h", "latest_status": "handed_off", "updated_at": 100.0}
    open_step = {"work_id": "o", "latest_status": "in_progress", "updated_at": 100.0}
    research = {"work_id": "r", "latest_status": "completed", "kind": "research", "updated_at": 100.0}
    for items in ([handed], [open_step, research], [], [{"work_id": "u", "latest_status": "completed", "updated_at": 100.0}]):
        receipt = _receipt(_task(items))
        verdict = receipt["verdict"]
        evidence = receipt["axes"]["evidence_strength"]
        shown.extend(strings([verdict.get(k) for k in ("headline", "proof_clause", "gap_label", "gap_text", "ledger_text", "gap")]))
        shown.extend(strings([evidence["coverage_tile"], evidence["checks_tile"], evidence["coverage_hero"],
                              evidence["coverage_row"], evidence["coverage_ledger"]]))
        shown.extend(strings([receipt["axes"]["decision_status"][k] for k in ("label", "asserted_by_label", "asserted_by_phrase")]))
    leaks = [text for text in shown if snake.search(text)]
    assert leaks == []
    assert step_evidence_grade({"latest_status": "handed_off"})["reason"] == "Handed off before completion — not graded"


def test_session_identity_provenance_names_the_rows_actually_read() -> None:
    items = [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0, "objective": "x"}]
    logged = _receipt(_task(items))
    assert logged["dimensions"]["actors"]["provenance"] == ["client_log"]
    assert "client_log" in logged["dimensions"]["task"]["provenance"]

    # No usage rows: the session id is the agent's own MCP report, named as a gap.
    reported = _receipt(_task(items, usage={"rows": 0}))
    assert reported["dimensions"]["actors"]["provenance"] == ["mcp"]
    assert reported["dimensions"]["task"]["provenance"] == ["mcp"]
    assert any("not observed in a client log" in gap for gap in reported["dimensions"]["actors"]["gaps"])

    hooked = _receipt(_task([{**items[0], "client_context_source": "claude_code_hook"}], usage={"rows": 0}))
    assert hooked["dimensions"]["actors"]["provenance"] == ["hook"]


def test_no_source_is_an_absence_not_a_present_source() -> None:
    receipt = _receipt(_task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}], usage={"rows": 0}))
    provenance = receipt["dimensions"]["provenance"]
    assert provenance["by_dimension"]["cost"] == ["none"]
    assert "none" not in provenance["sources_present"]
    assert all(entry["key"] != "none" for entry in provenance["sources"])
    # The legend still explains the label a per-dimension cell prints.
    assert "none" in provenance["legend"]
    # Every gap names its dimension's label.
    assert all(item["dimension_label"] and item["dimension_label"] != item["dimension"]
               for item in receipt["dimensions"]["gaps"]["items"])


def test_actions_tile_names_absence_and_keeps_a_missing_total_missing() -> None:
    item = [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}]
    none = _receipt(_task(item))["dimensions"]["actions"]
    assert none["actions_tile"] == {"value": None, "absent": "not instrumented", "qualifier": None}
    assert none["related_paths_text"] == "no related paths recorded"
    assert any("Tool calls row" in gap for gap in none["gaps"])

    missing = _receipt(_task(item, actions={"touched_files": []}))["dimensions"]["actions"]
    assert missing["tool_category_total"] is None
    assert missing["actions_tile"]["absent"] == "capture coverage unknown"

    captured = _receipt(
        _task(item, actions={"tool_category_counts": {"read": 3}, "tool_category_total": 3, "capture_bases": ["hook"]})
    )["dimensions"]["actions"]
    assert captured["actions_tile"] == {"value": "3", "absent": None, "qualifier": "Hook-captured"}
    assert captured["actions_synopsis"]["can_show_distribution"] is True


def test_summary_carries_attention_order_group_and_lifecycle_marker() -> None:
    failing = _check("failed", exit_code=1, at=200.0)
    finding = build_receipt_summary(
        _task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}], task_checks=[failing]),
        public_task_id="task_x", title="x",
    )
    assert finding["attention_order"] == 0 and finding["group_key"] == "attention"
    blocked = build_receipt_summary(
        _task([{"work_id": "w", "latest_status": "blocked", "blocker": "needs a key", "updated_at": 100.0}]),
        public_task_id="task_x", title="x",
    )
    assert blocked["attention_order"] == 1 and blocked["group_key"] == "attention"
    done = build_receipt_summary(
        _task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}]),
        public_task_id="task_x", title="x",
    )
    assert done["attention_order"] is None and done["group_key"] == "reported"
    assert done["lifecycle_marker_text"] is None


def test_command_state_separates_an_agents_own_command_from_a_hook_digest() -> None:
    """Two different facts stopped sharing one (half-false) sentence.

    The agent volunteered the command with its check, so it IS recorded; the
    receipt prints the name it recorded instead of repeating the text. A
    hook-derived check is the only case where no command text exists anywhere.
    """

    from agentacct.display_vocabulary import COMMAND_AGENT_RECORDED_TEXT

    agent_recorded = _check("passed", name="python -m pytest tests/test_percent.py", at=200.0)
    agent_recorded.update(command_redacted=True, command_state="agent_recorded")
    row = _receipt(_task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}],
                         task_checks=[agent_recorded]))["dimensions"]["evidence"]["checks"][0]
    assert row["command_state"] == "agent_recorded"
    assert row["command_state_text"] == COMMAND_AGENT_RECORDED_TEXT
    # Still never the text itself — the name is what the receipt prints.
    assert "command" not in row

    hook = _check("passed", name="pytest test", at=200.0)
    hook.update(command_redacted=True, command_state="digest_only")
    row = _receipt(_task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}],
                         task_checks=[hook]))["dimensions"]["evidence"]["checks"][0]
    assert row["command_state_text"] == COMMAND_NOT_SHOWN_TEXT


def test_revision_contradiction_is_projected_when_declared_files_are_absent() -> None:
    """A check that declares files its stamped commit does not contain proves
    the stamp is not the revision it ran against."""

    check = _check("passed", name="pytest tests/test_subtract.py", at=200.0)
    check.update({
        "files": ["moneyutil/core.py", "tests/test_subtract.py"],
        "git_commit": "8a4e0240d24ea5ee3fd7e60a01cb1750a767498e",
        "git_branch": "main",
        "git_revision_basis": "server_captured_at_record",
        "git_declared_files_absent": ["tests/test_subtract.py"],
    })
    row = _receipt(_task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}],
                         task_checks=[check]))["dimensions"]["evidence"]["checks"][0]
    assert row["revision_absent_files"] == ["tests/test_subtract.py"]
    assert row["revision_contradiction_text"] == (
        "The stamped revision 8a4e024 does not contain tests/test_subtract.py, "
        "so it is not the revision this check ran against."
    )
    # Nothing stamped, nothing asserted.
    plain = _check("passed", name="pytest", at=200.0)
    plain["files"] = ["a.py"]
    row = _receipt(_task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}],
                         task_checks=[plain]))["dimensions"]["evidence"]["checks"][0]
    assert row["revision_absent_files"] == [] and row["revision_contradiction_text"] is None


def test_superseded_runs_stay_in_the_receipt_so_a_recovery_can_be_rendered() -> None:
    """The failing run used to be dropped, so runs_total said 2 while the
    receipt carried one row and the fail→pass story could not be told."""

    first = _check("failed", name="pytest", at=150.0, exit_code=1)
    first["event_id"] = "evt_first_fail"
    second = _check("failed", name="pytest", at=175.0, exit_code=1)
    second["event_id"] = "evt_second_fail"
    passed = _check("passed", name="pytest", at=200.0)
    passed["event_id"] = "evt_pass"
    task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0,
          "current_check_events": [passed]}],
        task_checks=[first, second, passed],
    )
    receipt = _receipt(task)
    rows = receipt["dimensions"]["evidence"]["checks"]
    assert [row["event_id"] for row in rows] == ["evt_first_fail", "evt_second_fail", "evt_pass"]
    assert [row["history_run"] for row in rows] == [True, True, False]
    assert all(row["superseded"] for row in rows[:2])
    assert all(row["superseded_definition"] for row in rows[:2])
    assert all(row["superseded_by_event_id"] == "evt_pass" for row in rows[:2])
    # The reciprocal pointer names the NEWEST failure the pass recovered from.
    assert rows[-1]["supersedes_check_event_id"] == "evt_second_fail"
    assert rows[-1]["supersedes_basis"] == "reciprocal_of_supersession"
    # History rows never enter the header tally: it counts the frontier.
    evidence = receipt["dimensions"]["evidence"]
    assert evidence["checks_total"] == 1 and evidence["checks_passed"] == 1
    assert evidence["checks_failed"] == 0
    assert evidence["check_tally_text"] == "1/1 passed · 2 earlier runs failed"
    # Each history row carries only the failures that preceded IT.
    assert [row["earlier_failed"] for row in rows] == [0, 1, 2]


def test_an_agent_declared_supersession_pointer_wins_over_the_synthesized_one() -> None:
    first = _check("failed", name="pytest", at=150.0, exit_code=1)
    first["event_id"] = "evt_first_fail"
    passed = _check("passed", name="pytest", at=200.0)
    passed.update(event_id="evt_pass", supersedes_check_event_id="evt_declared")
    task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0,
          "current_check_events": [passed]}],
        task_checks=[first, passed],
    )
    row = _receipt(task)["dimensions"]["evidence"]["checks"][-1]
    assert row["supersedes_check_event_id"] == "evt_declared"
    assert row["supersedes_basis"] == "agent_declared"


def test_actions_state_is_partial_when_the_ledger_holds_records_capture_never_saw() -> None:
    """'exact' has to be earned. The categories can sum perfectly while the
    capture missed the calls that wrote the Task's own records."""

    item = {"work_id": "w", "latest_status": "completed", "updated_at": 100.0}
    actions = {
        "tool_category_counts": {"read": 26, "execute": 25, "agent": 3, "mcp": 5, "other": 1},
        "tool_category_total": 60,
        "tool_name_counts": {"Read": 26, "Bash": 25, "Agent": 3,
                             "mcp__agentacct__agentacct_record_section": 4,
                             "mcp__codegraph__codegraph_explore": 1},
        "tool_name_total": 59,
        "touched_files": [],
        "touched_file_count": 0,
        "capture_bases": ["hook"],
    }
    check = _check("passed", name="pytest", at=200.0, source_type="mcp_agent_reported")
    task = _task([item, {"work_id": "w2", "latest_status": "completed", "updated_at": 100.0}],
                 task_checks=[check], actions=actions)
    dimension = _receipt(task)["dimensions"]["actions"]
    synopsis = dimension["actions_synopsis"]
    assert synopsis["state"] == "partial"
    assert "the ledger holds 2 recorded sections but capture saw no record_section call" not in (
        synopsis["integrity_detail"] or ""
    )
    assert "the ledger holds 1 recorded check but capture saw no record_machine_check call" in (
        synopsis["integrity_detail"] or ""
    )
    # The shortfall is also a named gap, ranked with what blocks a reviewer.
    assert any(gap.startswith("Tool-call capture did not cover this Task") for gap in dimension["gaps"])

    # A capture that saw at least as many calls as the ledger holds records keeps 'exact'.
    covered = dict(actions)
    covered["tool_name_counts"] = {
        **actions["tool_name_counts"],
        "mcp__agentacct__agentacct_record_section": 9,
        "mcp__agentacct__agentacct_record_machine_check": 2,
    }
    task = _task([item], task_checks=[check], actions=covered)
    assert _receipt(task)["dimensions"]["actions"]["actions_synopsis"]["state"] == "exact"


def test_a_capture_without_tool_names_can_never_be_called_partial() -> None:
    """No captured NAME breakdown means nothing to compare — a session recorded
    before name capture shipped must not read as one that missed every call."""

    task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}],
        actions={"tool_category_counts": {"read": 3}, "tool_category_total": 3,
                 "touched_files": [], "touched_file_count": 0, "capture_bases": ["hook"]},
    )
    assert _receipt(task)["dimensions"]["actions"]["actions_synopsis"]["state"] == "exact"


def test_gaps_rank_what_blocks_a_reviewer_above_provenance_bookkeeping() -> None:
    """Gaps used to be ordered by which ingestion source was silent, so four
    bookkeeping lines outranked every question a reviewer actually asks."""

    from agentacct.display_vocabulary import (
        GAP_FILE_OPERATIONS_UNORDERED,
        GAP_NO_CHANGE_DESCRIPTION,
        GAP_NO_COMMIT_RECORDED,
    )

    check = _check("passed", name="pytest", at=200.0, source_type="mcp_agent_reported")
    check["files"] = ["moneyutil/core.py", "tests/test_percent.py"]
    task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}],
        task_checks=[check],
        usage={"rows": 0, "cost_complete": False},
        actions={"tool_category_counts": {}, "tool_category_total": 0,
                 "touched_files": ["moneyutil/core.py"], "touched_file_count": 1},
    )
    task["sessions"][0]["usage"] = {}
    gaps = _receipt(task)["dimensions"]["gaps"]
    kinds = [item["kind"] for item in gaps["items"]]
    reasons = [item["reason"] for item in gaps["items"]]
    # Every reviewer-facing gap comes before every bookkeeping one.
    assert kinds == sorted(kinds, key=lambda kind: 0 if kind == "blocks_review" else 1)
    assert gaps["blocks_review_count"] + gaps["bookkeeping_count"] == gaps["count"]
    assert GAP_NO_COMMIT_RECORDED in reasons
    assert GAP_NO_CHANGE_DESCRIPTION in reasons
    assert GAP_FILE_OPERATIONS_UNORDERED in reasons
    # The cost/actors bookkeeping lines are still there — just last.
    assert any("No usage was recorded" in reason for reason in reasons)
    assert reasons.index(GAP_NO_COMMIT_RECORDED) < reasons.index(
        next(reason for reason in reasons if "No usage was recorded" in reason)
    )
    # Every item names its rank in words a surface can print.
    assert {item["kind_label"] for item in gaps["items"]} <= {"Blocks review", "Provenance bookkeeping"}


def test_silent_subagent_sessions_are_named_as_a_reviewer_gap() -> None:
    task = _task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0,
                   "client_session_id": "s1", "summary": "Did the thing"}])
    task["sessions"].extend(
        {
            "client": "claude-code",
            "client_session_id": f"s1:agent-{index}",
            "session_kind": "child",
            "last_activity_at": 100.0,
            "usage": {"rows": 1, "total_tokens": 798_883},
        }
        for index in range(3)
    )
    reasons = [item["reason"] for item in _receipt(task)["dimensions"]["gaps"]["items"]]
    assert "3 supporting sessions spent 2,396,649 tokens and recorded no work, " \
           "so what they did is unreviewable." in reasons
