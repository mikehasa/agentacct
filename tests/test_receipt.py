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


def test_agent_reported_done_is_never_evidence_verified() -> None:
    # A completed step with no linked check: the agent SAID done, nothing PROVES it.
    receipt = _receipt(_task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}]))
    assert receipt["axes"]["decision_status"]["key"] == "reported"
    assert receipt["axes"]["decision_status"]["asserted_by"] == "agent_report"
    assert receipt["axes"]["evidence_strength"]["key"] != "verified"
    assert receipt["axes"]["evidence_strength"]["key"] == "reported"


def test_passing_current_check_verifies_on_both_axes() -> None:
    check = _check("passed", at=200.0)
    task = _task(
        [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0, "current_check_events": [check]}],
        task_checks=[check],
    )
    receipt = _receipt(task)
    assert receipt["axes"]["decision_status"]["key"] == "verified"
    assert receipt["axes"]["decision_status"]["asserted_by"] == "machine"
    assert receipt["axes"]["evidence_strength"]["key"] == "verified"
    assert receipt["axes"]["evidence_strength"]["checks_passed"] == 1
    assert receipt["dimensions"]["evidence"]["checks"][0]["source"] == "hook"


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
    assert receipt["axes"]["evidence_strength"]["key"] != "verified"
    # Provenance of the outcome field is the human, not a check.
    assert receipt["dimensions"]["outcome"]["provenance"] == ["human"]


def test_failed_is_distinct_from_blocked() -> None:
    failed = _receipt(_task([{"work_id": "w", "latest_status": "failed", "updated_at": 100.0}]))
    assert failed["axes"]["decision_status"]["key"] == "failed"
    blocked = _receipt(
        _task([{"work_id": "w", "latest_status": "blocked", "updated_at": 100.0, "blocker": "needs a key"}])
    )
    assert blocked["axes"]["decision_status"]["key"] == "blocked"


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


def test_cost_dimension_carries_basis_and_flags_estimate_gap() -> None:
    cost = _receipt(_task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}]))["dimensions"]["cost"]
    assert cost["cost_basis"] == "pricing_table"
    assert any("estimate" in reason for reason in cost["gaps"])


def test_provenance_rollup_covers_every_dimension_with_a_legend() -> None:
    receipt = _receipt(_task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}]))
    provenance = receipt["dimensions"]["provenance"]
    for name in ("task", "actors", "actions", "cost", "evidence", "outcome"):
        assert name in provenance["by_dimension"]
        assert provenance["by_dimension"][name]  # never empty
    for source in provenance["sources_present"]:
        assert source in provenance["legend"]


def test_gaps_rollup_flattens_dimension_gaps_with_labels() -> None:
    receipt = _receipt(_task([{"work_id": "w", "latest_status": "completed", "updated_at": 100.0}]))
    gaps = receipt["dimensions"]["gaps"]
    assert gaps["count"] == len(gaps["items"])
    dimensions_with_gaps = {item["dimension"] for item in gaps["items"]}
    # A no-check completed step surfaces gaps under both cost (estimate) and
    # evidence (no machine checks / unverified step).
    assert "evidence" in dimensions_with_gaps
