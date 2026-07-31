"""Canonical Task outcome parity across Work cards and Task Intelligence."""

from __future__ import annotations

from typing import Any

import pytest

from agentacct.api import _task_product_state
from agentacct.finding_disposition import finding_target_digest
from agentacct.task_intelligence import build_task_intelligence
from agentacct.task_outcome import (
    _STALE_OPEN_STEP_SECONDS,
    reduce_task_outcome,
    step_verification_counts,
)


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


# --- DECISION 1 / 3a / 3b: honest work state for handoff and stale open steps ---


_NOW = 1_700_000_000.0


def _step(status: str, *, updated_at: float = _NOW, evidence_status: str = "none", checks: list | None = None) -> dict[str, Any]:
    return {
        "latest_status": status,
        "updated_at": updated_at,
        "evidence_status": evidence_status,
        "evidence_events": list(checks or []),
    }


def _multi_task(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"work_items": items, "task_evidence_events": [], "sessions": [], "usage": {"rows": 0}}


def test_handed_off_step_is_a_clean_terminal_not_in_progress_or_verified() -> None:
    # DECISION 1: a handed_off latest status is terminal. Before the fix
    # reduce_task_outcome had no handed_off branch, so an all-handed_off Task
    # fell through to "reported" — never a distinct clean-stop terminal.
    task = _multi_task([_step("handed_off")])

    outcome = reduce_task_outcome(task, now=_NOW)
    assert outcome["key"] == "handed_off"
    assert outcome["key"] not in {"in_progress", "verified", "reported", "reported_done", "blocked"}

    home, _carrier = _task_product_state(task)
    assert (home["key"], home["label"]) == ("handed_off", "Handed off")
    assert home["action_required"] is False
    # Honesty: a handoff is never dressed up as a completion/verification.
    assert "not a completed or verified" in home["why"]


def test_all_completed_except_stale_open_step_is_mostly_done_not_in_progress() -> None:
    # DECISION 3a: a single un-closed, stale step must NOT drag a mostly-finished
    # Task to plain "in progress". Before the fix ANY active step forced
    # "in_progress"; now a stale open step on a completed Task reads mostly_done.
    stale = _NOW - _STALE_OPEN_STEP_SECONDS - 60.0
    task = _multi_task(
        [
            _step("completed", updated_at=stale),
            _step("completed", updated_at=stale),
            _step("checkpoint", updated_at=stale),
        ]
    )

    outcome = reduce_task_outcome(task, now=_NOW)
    assert outcome["key"] == "mostly_done"
    assert outcome["open_step_count"] == 1
    # The open step stays open in the data; the Task is never called finished.
    assert outcome["key"] not in {"verified", "reported_done", "in_progress"}

    home, _carrier = _task_product_state(task)
    assert home["key"] == "mostly_done"
    assert "1 step left open" in home["label"]
    assert home["action_required"] is False


def test_all_terminal_steps_do_not_read_in_progress_even_with_handoff() -> None:
    # DECISION 3a: when EVERY step is terminal, state is driven by those steps,
    # not "in progress". A completed + handed_off Task is a clean stop.
    task = _multi_task([_step("completed"), _step("handed_off")])

    outcome = reduce_task_outcome(task, now=_NOW)
    assert outcome["key"] == "handed_off"
    assert outcome["open_step_count"] == 0


def test_genuinely_recent_open_step_still_reads_in_progress() -> None:
    # Guard against over-correction (DECISION 3a): a fresh open step on a Task
    # touched within the staleness window stays "in progress".
    recent = _NOW - 60.0
    task = _multi_task(
        [
            _step("completed", updated_at=recent),
            _step("checkpoint", updated_at=recent),
        ]
    )

    outcome = reduce_task_outcome(task, now=_NOW)
    assert outcome["key"] == "in_progress"

    home, _carrier = _task_product_state(_multi_task(
        [
            # _task_product_state uses the wall clock; a genuinely recent step is
            # simulated with import-time "now" so the surface still reads live.
            _step("completed", updated_at=__import__("time").time() - 30.0),
            _step("checkpoint", updated_at=__import__("time").time() - 30.0),
        ]
    ))
    assert home["key"] == "in_progress"


def test_stale_open_step_without_any_completed_step_stays_in_progress() -> None:
    # Conservative: "mostly done" requires at least one completed step. A Task
    # with only open (stale) steps and nothing finished is not "mostly done".
    stale = _NOW - _STALE_OPEN_STEP_SECONDS - 60.0
    task = _multi_task([_step("started", updated_at=stale), _step("checkpoint", updated_at=stale)])

    assert reduce_task_outcome(task, now=_NOW)["key"] == "in_progress"


def test_partial_verification_counts_are_exposed_and_correct() -> None:
    # DECISION 3b: expose per-step verified vs agent-reported-only instead of a
    # single all-or-nothing flag. Before the fix these keys did not exist.
    verified_step = _step(
        "completed",
        checks=[_check("passed", created_at=_NOW, event_id="v-pass")],
    )
    strong_step = _step("completed", evidence_status="strong")
    reported_step = _step("completed")  # completed, no check, weak evidence
    task = _multi_task([verified_step, strong_step, reported_step])

    outcome = reduce_task_outcome(task, now=_NOW)
    assert outcome["verified_step_count"] == 2  # passing check + strong evidence
    assert outcome["total_step_count"] == 3
    assert outcome["agent_reported_step_count"] == 1

    counts = step_verification_counts(task)
    assert counts == {
        "verified_step_count": 2,
        "total_step_count": 3,
        "agent_reported_step_count": 1,
    }

    detail = build_task_intelligence(task, public_task_id="task_partial", title="Partial")
    assert detail["verification"]["verified_step_count"] == 2
    assert detail["verification"]["total_step_count"] == 3


def test_no_task_is_labeled_finished_or_verified_without_evidence() -> None:
    # Honesty guard: neither a handoff nor a stale-partial nor a check-less
    # "completed" Task may ever read verified/finished, and a step with no
    # passing check or strong evidence is never counted verified.
    stale = _NOW - _STALE_OPEN_STEP_SECONDS - 60.0
    handed_off = _multi_task([_step("handed_off"), _step("completed")])
    mostly_done = _multi_task([_step("completed", updated_at=stale), _step("started", updated_at=stale)])
    completed_no_check = _multi_task([_step("completed"), _step("completed")])

    assert reduce_task_outcome(handed_off, now=_NOW)["key"] not in {"verified"}
    assert reduce_task_outcome(mostly_done, now=_NOW)["key"] not in {"verified", "reported_done"}

    completed_outcome = reduce_task_outcome(completed_no_check, now=_NOW)
    assert completed_outcome["key"] == "reported"  # not "verified" without a passing check
    assert completed_outcome["verified_step_count"] == 0
    assert completed_outcome["total_step_count"] == 2
