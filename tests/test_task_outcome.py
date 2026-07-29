"""Canonical Task outcome parity across Work cards and Task Intelligence."""

from __future__ import annotations

from typing import Any

import pytest

from agentacct.api import _task_product_state
from agentacct.finding_disposition import finding_target_digest
from agentacct.task_intelligence import build_task_intelligence


def _check(result: str, *, created_at: float, event_id: str, identity: str = "check:test") -> dict[str, Any]:
    return {
        "event_id": event_id,
        "check_identity": identity,
        "result": result,
        "created_at": created_at,
        "summary": f"{result} at {created_at}",
    }


def _task(
    *,
    status: str = "completed",
    updated_at: float = 100.0,
    item_checks: list[dict[str, Any]] | None = None,
    task_checks: list[dict[str, Any]] | None = None,
    blocker: str | None = None,
) -> dict[str, Any]:
    return {
        "work_items": [
            {
                "latest_status": status,
                "updated_at": updated_at,
                "evidence_status": "none",
                "evidence_events": list(item_checks or []),
                "blocker": blocker,
            }
        ],
        "task_evidence_events": list(task_checks or []),
        "sessions": [],
        "usage": {"rows": 0},
    }


def _surface_states(task: dict[str, Any]) -> tuple[str, str]:
    home, _carrier = _task_product_state(task)
    detail = build_task_intelligence(task, public_task_id="task_test", title="Test task")
    return str(home["key"]), str(detail["states"]["outcome"]["key"])


@pytest.mark.parametrize(
    ("check_time", "expected_home", "expected_detail"),
    [
        (50.0, "reported_done", "reported"),
        (100.0, "verified", "verified"),
        (150.0, "verified", "verified"),
    ],
)
def test_passing_check_must_not_predate_the_work_it_verifies(
    check_time: float,
    expected_home: str,
    expected_detail: str,
) -> None:
    task = _task(task_checks=[_check("passed", created_at=check_time, event_id="pass")])

    assert _surface_states(task) == (expected_home, expected_detail)


def test_failed_work_status_cannot_be_overridden_by_task_level_pass() -> None:
    task = _task(
        status="failed",
        task_checks=[_check("passed", created_at=150.0, event_id="pass")],
    )

    assert _surface_states(task) == ("blocked", "blocked")


def test_newer_same_identity_pass_clears_old_item_failure_on_both_surfaces() -> None:
    task = _task(
        item_checks=[_check("failed", created_at=100.0, event_id="old-failure")],
        task_checks=[_check("passed", created_at=150.0, event_id="new-pass")],
    )

    assert _surface_states(task) == ("verified", "verified")


def test_task_level_retry_closes_section_linked_failure_for_same_scoped_check() -> None:
    failure = _check("failed", created_at=100.0, event_id="section-failure")
    failure.update(
        {
            "source": "codex",
            "client": "codex",
            "project_identity": "project:test:1234",
            "work_id": "codex::session::section",
            "section_id": "section",
        }
    )
    retry = _check("passed", created_at=150.0, event_id="task-pass")
    retry.update(
        {
            "source": "codex",
            "client": "codex",
            "project_identity": "project:test:1234",
        }
    )
    task = _task(item_checks=[failure], task_checks=[retry])

    assert _surface_states(task) == ("verified", "verified")


def test_capture_only_failure_is_finding_but_passing_only_is_observed() -> None:
    failed = {
        "work_items": [],
        "task_evidence_events": [_check("failed", created_at=100.0, event_id="failure")],
        "sessions": [{}],
        "usage": {"rows": 0},
    }
    passed = {
        **failed,
        "task_evidence_events": [_check("passed", created_at=100.0, event_id="pass")],
    }

    assert _surface_states(failed) == ("open_finding", "finding")
    assert _surface_states(passed) == ("activity", "unknown")


@pytest.mark.parametrize(
    ("status", "blocker"),
    [
        ("blocked", None),
        ("completed", "Need the user to provide an API key"),
    ],
)
def test_explicit_blocker_outranks_current_failed_check(
    status: str,
    blocker: str | None,
) -> None:
    task = _task(
        status=status,
        blocker=blocker,
        task_checks=[_check("failed", created_at=150.0, event_id="failure")],
    )

    assert _surface_states(task) == ("blocked", "blocked")


def test_blocked_task_carrier_keeps_specific_blocker_across_multiple_steps() -> None:
    task = {
        "work_items": [
            {
                "work_id": "blocked-step",
                "latest_status": "blocked",
                "updated_at": 100.0,
                "blocker": "Need the user to provide an API key",
                "evidence_status": "failed",
                "evidence_events": [
                    _check("failed", created_at=150.0, event_id="blocked-failure")
                ],
            },
            {
                "work_id": "other-step",
                "latest_status": "completed",
                "updated_at": 200.0,
                "blocker": None,
                "evidence_status": "failed",
                "evidence_events": [
                    _check("failed", created_at=250.0, event_id="other-failure")
                ],
            },
        ],
        "task_evidence_events": [],
        "sessions": [],
        "usage": {"rows": 0},
    }

    state, carrier = _task_product_state(task)

    assert state["key"] == "blocked"
    assert carrier is not None
    assert carrier["work_id"] == "blocked-step"
    assert carrier["blocker"] == "Need the user to provide an API key"
    assert _surface_states(task) == ("blocked", "blocked")


def test_resolved_blocker_is_canonical_but_never_verified_or_completed() -> None:
    task = _task(
        status="resolved",
        updated_at=150.0,
        item_checks=[_check("passed", created_at=150.0, event_id="resolution-check")],
    )
    task["work_items"][0]["blocker_resolution"] = {
        "state": "resolved",
        "basis": "agent_claim_with_passed_check",
        "authoritative": False,
        "summary": "The exact blocker was reported resolved.",
    }

    home, carrier = _task_product_state(task)
    detail = build_task_intelligence(
        task,
        public_task_id="task_resolved",
        title="Resolved task",
    )

    assert home["key"] == "resolved"
    assert home["label"] == "Resolved"
    assert carrier is not None
    assert carrier["blocker_resolution"]["state"] == "resolved"
    assert detail["states"]["outcome"]["key"] == "resolved"
    assert "not a verified completion" in detail["decision_brief"]["outcome_statement"]


def test_mixed_reviewed_and_resolved_findings_use_truthful_aggregate_copy() -> None:
    reviewed_failure = _check(
        "failed",
        created_at=150.0,
        event_id="reviewed-failure",
        identity="check:reviewed",
    )
    resolved_failure = _check(
        "failed",
        created_at=160.0,
        event_id="resolved-failure",
        identity="check:resolved",
    )
    task = _task(task_checks=[reviewed_failure, resolved_failure])
    task["finding_episodes"] = [
        {
            "target_digest": finding_target_digest(reviewed_failure),
            "disposition_state": "reviewed",
            "attention_open": False,
            "revision": 1,
            "failure_event": reviewed_failure,
            "latest_disposition": {"state": "reviewed"},
        },
        {
            "target_digest": finding_target_digest(resolved_failure),
            "disposition_state": "resolved",
            "attention_open": False,
            "revision": 1,
            "failure_event": resolved_failure,
            "latest_disposition": {"state": "resolved"},
        },
    ]

    home, _carrier = _task_product_state(task)
    detail = build_task_intelligence(task, public_task_id="task_mixed", title="Mixed findings")

    assert home["key"] == "finding_reviewed"
    assert home["label"] == "Findings reviewed"
    assert "reviewed or marked resolved" in home["why"]
    assert "every current finding reviewed" not in home["why"].lower()
    assert detail["states"]["outcome"]["key"] == "finding"
    assert "reviewed or marked resolved" in detail["decision_brief"]["outcome_statement"]
    assert detail["findings"]["reviewed_count"] == 1
    assert detail["findings"]["resolved_count"] == 1
