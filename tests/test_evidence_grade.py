"""M2 evidence grade engine: the per-step ladder + the task-level coverage ratio.

These lock the honesty spine: an agent-reported check is 'self_checked' (never
independent), the grade agrees with the untouched verified-count on
completed/passed steps, and the task level is per-tier ratios + a ledger, with a
'Not gradeable' floor rather than a fabricated number.
"""

from __future__ import annotations

from typing import Any

from agentacct.receipt import _evidence_strength
from agentacct.task_outcome import (
    EVIDENCE_GRADE_RANK,
    GRADE_SELF_CHECKED,
    _step_is_verified,
    step_evidence_grade,
)


def _check(eid: str, source_type: str, *, result: str = "passed", ident: str | None = None) -> dict[str, Any]:
    return {
        "event_id": eid,
        "result": result,
        "source_type": source_type,
        "name": ident or eid,
        "check_identity": ident or eid,
        "check_identity_stable": True,
    }


_MCP = _check("eA", "mcp_agent_reported", ident="cA")
_HOOK = _check("eB", "client_hook", ident="cB")
# A real external check is trusted via source_type, never the agent-set source.
_CI = _check("eC", "ci", ident="cC")
_FAIL = _check("eA", "mcp_agent_reported", result="failed", ident="cA")


def _grade(status: str, checks: list[dict[str, Any]], **extra: Any) -> str:
    return step_evidence_grade({"latest_status": status, "current_check_events": checks, **extra})["grade"]


def test_step_grade_ladder_by_source() -> None:
    assert _grade("completed", [_MCP]) == "self_checked"
    assert _grade("completed", [_HOOK]) == "independently_checked"
    assert _grade("completed", [_CI]) == "externally_verified"
    # strongest independence wins when several pass
    assert _grade("completed", [_MCP, _HOOK]) == "independently_checked"


def test_agent_cannot_forge_external_tier_via_source() -> None:
    # The honesty keystone: an MCP-recorded check whose agent-set `source` names
    # a CI system, but whose trusted source_type is the ledger's
    # mcp_agent_reported, must grade self_checked — never externally/independently
    # verified. The evidence tier must be un-forgeable by the agent-under-test.
    forged = {
        "result": "passed",
        "source_type": "mcp_agent_reported",
        "source": "github_actions",
        "name": "CI green on 3.11/3.12/3.13",
        "check_identity": "cForge",
        "check_identity_stable": True,
    }
    assert _grade("completed", [forged]) == "self_checked"


def test_step_grade_claimed_and_none() -> None:
    assert _grade("completed", []) == "claimed"
    assert _grade("completed", [_FAIL]) == "claimed"  # failing check → decision axis, not evidence
    assert _grade("in_progress", [_MCP]) == "none"
    assert _grade("failed", [_MCP]) == "none"
    # resolved (evidence-backed blocker clearance) is graded like a completion
    assert _grade("resolved", [_MCP]) == "self_checked"
    # strong log evidence with no linked check series → self_checked
    assert step_evidence_grade({"latest_status": "completed", "evidence_status": "strong"})["grade"] == "self_checked"


def test_step_grade_carries_a_reason() -> None:
    assert "not independent" in _reason("completed", [_MCP])
    assert "independent of the agent" in _reason("completed", [_HOOK])
    assert "no passing machine check" in _reason("completed", [])


def _reason(status: str, checks: list[dict[str, Any]]) -> str:
    return step_evidence_grade({"latest_status": status, "current_check_events": checks})["reason"]


def test_grade_and_verified_count_never_disagree() -> None:
    # _step_is_verified is untouched; a step is verified exactly when its grade
    # reaches self_checked (on completed/passed statuses).
    for status in ("completed", "passed"):
        for checks in ([_MCP], [_HOOK], [], [_FAIL]):
            item = {"latest_status": status, "current_check_events": checks}
            verified = EVIDENCE_GRADE_RANK[step_evidence_grade(item)["grade"]] >= EVIDENCE_GRADE_RANK[GRADE_SELF_CHECKED]
            assert verified == _step_is_verified(item), (status, checks)


def _item(status: str, kind: str, checks: list[dict[str, Any]], sess: str = "S0") -> dict[str, Any]:
    return {"latest_status": status, "kind": kind, "client_session_id": sess,
            "current_check_events": checks, "evidence_events": checks}


def test_task_coverage_ratios_ledger_and_hidden_accounting() -> None:
    task = {
        "primary_root": {"client_session_id": "S0"},
        "work_items": [
            _item("completed", "implementation", [_MCP]),            # self_checked, checkable
            _item("completed", "testing", [_HOOK]),                  # independently_checked, checkable
            _item("completed", "implementation", []),                # unchecked, checkable
            _item("completed", "research", [_check("eD", "mcp_agent_reported", ident="cD")]),  # excused
            _item("in_progress", "implementation", [_MCP]),          # open
            _item("completed", "implementation", [_check("eF", "mcp_agent_reported", ident="cF")], sess="S1"),  # hidden
        ],
        "task_evidence_events": [_check("eZ", "mcp_agent_reported", ident="cZ")],  # orphan, on no step
        "current_check_events": None,
    }
    s = _evidence_strength(task, [], {"verified_step_count": 0, "total_step_count": 6, "agent_reported_step_count": 0})
    assert s["checkable_total"] == 4
    assert s["by_tier"] == {"externally_verified": 0, "independently_checked": 1, "self_checked": 2, "unchecked": 1}
    assert s["strongest_tier"] == "independently_checked"
    assert s["key"] == "independently_checked"
    assert s["not_checkable"] == 1
    assert s["open_or_incomplete"] == 1
    assert s["hidden_in_subagents"] == 1
    assert s["unattributed_checks"] == 1
    assert s["gradeable"] is True


def test_continuation_root_step_is_not_hidden_in_subagents() -> None:
    # A step recorded in a CONTINUATION root (same agent resuming in a new
    # session) is a root, not a subagent — it must not inflate hidden_in_subagents
    # (that would falsely say a continued Task's own work "ran in subagents").
    task = {
        "primary_root": {"client_session_id": "S0"},
        "root_keys": [{"client_session_id": "S0"}, {"client_session_id": "S1"}],
        "work_items": [
            _item("completed", "implementation", [_MCP]),               # S0 primary root
            _item("completed", "implementation", [_MCP], sess="S1"),    # S1 continuation root
        ],
    }
    assert _evidence_strength(task, [], {})["hidden_in_subagents"] == 0
    # A genuine subagent step (a session that is NOT a root) still counts.
    task["work_items"].append(_item("completed", "implementation", [_MCP], sess="SUB"))
    assert _evidence_strength(task, [], {})["hidden_in_subagents"] == 1


def test_task_all_unchecked_has_no_strongest_tier_but_is_gradeable() -> None:
    task = {"primary_root": {"client_session_id": "S0"},
            "work_items": [_item("completed", "implementation", []), _item("completed", "testing", [])]}
    s = _evidence_strength(task, [], {})
    assert s["gradeable"] is True
    assert s["strongest_tier"] is None
    assert s["key"] == "unchecked"
    assert s["by_tier"]["unchecked"] == 2


def test_task_not_gradeable_when_no_checkable_step() -> None:
    task = {"primary_root": {"client_session_id": "S0"},
            "work_items": [_item("completed", "research", []), _item("completed", "docs", [])]}
    s = _evidence_strength(task, [], {})
    assert s["gradeable"] is False
    assert s["key"] == "undefined"
    assert s["checkable_total"] == 0
    assert s["not_checkable"] == 2
