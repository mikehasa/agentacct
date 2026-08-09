"""The Receipt projector: 8 dimensions, two orthogonal axes, provenance, gaps.

The load-bearing tests here are the ORTHOGONALITY invariants from M1's
acceptance bar: an agent's "done" never becomes evidence-verified, and a human
review/resolution never becomes machine verification.
"""

from __future__ import annotations

from typing import Any

from agentacct.finding_disposition import finding_target_digest
from agentacct.receipt import RECEIPT_SCHEMA_VERSION, build_receipt


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
    assert receipt["axes"]["decision_status"]["key"] == "finding"
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


def test_actions_with_categories_declares_hook_provenance() -> None:
    task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}],
        actions={"tool_category_counts": {"read": 3}, "tool_category_total": 3, "touched_files": [], "touched_file_count": 0},
    )
    actions = _receipt(task)["dimensions"]["actions"]
    assert actions["tool_category_counts"] == {"read": 3}
    assert "hook" in actions["provenance"]


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
    assert any("could not be bound" in gap for gap in _receipt(task)["dimensions"]["task"]["gaps"])


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
