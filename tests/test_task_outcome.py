"""Canonical Task outcome parity across the reducer and Task Intelligence."""

from __future__ import annotations

from typing import Any

import pytest

from agentacct.finding_disposition import finding_target_digest
from agentacct.task_intelligence import build_task_intelligence
from agentacct.task_outcome import (
    _LEFT_BEHIND_AFTER_ELSEWHERE_SECONDS,
    reduce_task_outcome,
    step_verification_counts,
    task_newest_event_at,
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
    outcome = reduce_task_outcome(task)
    detail = build_task_intelligence(task, public_task_id="task_test", title="Test task")
    return str(outcome["key"]), str(detail["states"]["outcome"]["key"])


@pytest.mark.parametrize(
    ("check_time", "expected_outcome", "expected_detail"),
    [
        (50.0, "reported", "reported"),
        (100.0, "verified", "verified"),
        (150.0, "verified", "verified"),
    ],
)
def test_passing_check_must_not_predate_the_work_it_verifies(
    check_time: float,
    expected_outcome: str,
    expected_detail: str,
) -> None:
    task = _task(task_checks=[_check("passed", created_at=check_time, event_id="pass")])

    assert _surface_states(task) == (expected_outcome, expected_detail)


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

    assert _surface_states(failed) == ("finding", "finding")
    assert _surface_states(passed) == ("observed", "unknown")


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


def test_blocked_step_outranks_other_steps_and_failures_across_multiple_steps() -> None:
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

    outcome = reduce_task_outcome(task)

    assert outcome["key"] == "blocked"
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

    outcome = reduce_task_outcome(task)
    detail = build_task_intelligence(
        task,
        public_task_id="task_resolved",
        title="Resolved task",
    )

    assert outcome["key"] == "resolved"
    assert outcome["key"] not in {"verified", "reported"}
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

    outcome = reduce_task_outcome(task)
    detail = build_task_intelligence(task, public_task_id="task_mixed", title="Mixed findings")

    assert outcome["key"] == "finding"
    assert outcome["finding_attention_state"] == "reviewed"
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
    # fell through to "reported" — never a distinct clean-stop terminal. The
    # explicit handoff is unaffected by the cross-session left-behind rule.
    task = _multi_task([_step("handed_off")])

    outcome = reduce_task_outcome(task)
    assert outcome["key"] == "handed_off"
    assert outcome["key"] not in {"in_progress", "verified", "reported", "reported_done", "blocked"}

    detail = build_task_intelligence(task, public_task_id="task_handoff", title="Handoff")
    assert detail["states"]["outcome"]["key"] == "handed_off"
    # Honesty: a handoff is never dressed up as a completion/verification.
    assert "not a completed or verified" in detail["decision_brief"]["outcome_statement"]


def test_completed_task_left_behind_by_later_elsewhere_activity_is_mostly_done() -> None:
    # DECISION 3a (cross-session): a finished-with-one-open-step Task flips to
    # "mostly done" ONLY because the user demonstrably kept working ELSEWHERE
    # afterward — here the store's latest activity postdates this Task's newest
    # event by more than the buffer. The signal is later activity elsewhere,
    # never mere silence, and it is deterministic (no wall clock).
    task = _multi_task(
        [
            _step("completed", updated_at=_NOW),
            _step("completed", updated_at=_NOW),
            _step("checkpoint", updated_at=_NOW),
        ]
    )
    later_elsewhere = (
        task_newest_event_at(task) + _LEFT_BEHIND_AFTER_ELSEWHERE_SECONDS + 60.0
    )

    outcome = reduce_task_outcome(task, latest_store_activity_at=later_elsewhere)
    assert outcome["key"] == "mostly_done"
    assert outcome["open_step_count"] == 1
    # The open step stays open in the data; the Task is never called finished.
    assert outcome["key"] not in {"verified", "reported_done", "in_progress"}

    detail = build_task_intelligence(
        task,
        public_task_id="task_left_behind",
        title="Left behind",
        latest_store_activity_at=later_elsewhere,
    )
    assert detail["states"]["outcome"]["key"] == "mostly_done"
    assert "not a claim the Task is finished" in detail["decision_brief"]["outcome_statement"]


def test_old_task_with_no_later_activity_anywhere_stays_in_progress() -> None:
    # OWNER'S KEY CASE: absence of activity is NOT abandonment. A Task the user
    # has not returned from anywhere stays "in progress" no matter how old —
    # being away / asleep / out for a day must never look like moving on.
    task = _multi_task(
        [
            _step("completed", updated_at=_NOW),
            _step("checkpoint", updated_at=_NOW),
        ]
    )
    # The store's latest activity IS this Task itself: nothing later happened
    # anywhere, so it can never read itself as "left behind".
    latest = task_newest_event_at(task)
    assert reduce_task_outcome(task, latest_store_activity_at=latest)["key"] == "in_progress"
    # And with NO store signal supplied, it must also stay in_progress — nothing
    # silently flips on missing data.
    assert reduce_task_outcome(task)["key"] == "in_progress"


def test_elsewhere_activity_within_buffer_stays_in_progress() -> None:
    # The buffer must be respected: later activity elsewhere only a couple of
    # hours after this Task froze is a normal pause, not "left behind".
    task = _multi_task(
        [
            _step("completed", updated_at=_NOW),
            _step("checkpoint", updated_at=_NOW),
        ]
    )
    only_two_hours_later = task_newest_event_at(task) + 2 * 60 * 60
    assert only_two_hours_later < task_newest_event_at(task) + _LEFT_BEHIND_AFTER_ELSEWHERE_SECONDS
    assert (
        reduce_task_outcome(task, latest_store_activity_at=only_two_hours_later)["key"]
        == "in_progress"
    )


def test_all_terminal_steps_do_not_read_in_progress_even_with_handoff() -> None:
    # DECISION 3a: when EVERY step is terminal, state is driven by those steps,
    # not "in progress". A completed + handed_off Task is a clean stop.
    task = _multi_task([_step("completed"), _step("handed_off")])

    outcome = reduce_task_outcome(task)
    assert outcome["key"] == "handed_off"
    assert outcome["open_step_count"] == 0


def test_genuinely_live_task_reads_in_progress() -> None:
    # Guard against over-correction (DECISION 3a): a genuinely live Task (recent,
    # with no >24h-later activity elsewhere) stays "in progress". Here the store
    # is actively in use but the latest activity is well within the buffer.
    task = _multi_task(
        [
            _step("completed", updated_at=_NOW),
            _step("checkpoint", updated_at=_NOW),
        ]
    )
    live_store = task_newest_event_at(task) + 30 * 60  # 30 min later, still live
    outcome = reduce_task_outcome(task, latest_store_activity_at=live_store)
    assert outcome["key"] == "in_progress"


def test_open_steps_without_any_completed_step_stay_in_progress() -> None:
    # Conservative: "mostly done" requires at least one completed step. A Task
    # with only open steps and nothing finished is never "mostly done" — even
    # when the user kept working far elsewhere long afterward.
    task = _multi_task(
        [_step("started", updated_at=_NOW), _step("checkpoint", updated_at=_NOW)]
    )
    far_later_elsewhere = (
        task_newest_event_at(task) + _LEFT_BEHIND_AFTER_ELSEWHERE_SECONDS + 60.0
    )

    assert (
        reduce_task_outcome(task, latest_store_activity_at=far_later_elsewhere)["key"]
        == "in_progress"
    )


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

    outcome = reduce_task_outcome(task)
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
    # Honesty guard: neither a handoff nor a left-behind partial nor a check-less
    # "completed" Task may ever read verified/finished, and a step with no
    # passing check or strong evidence is never counted verified.
    handed_off = _multi_task([_step("handed_off"), _step("completed")])
    left_behind = _multi_task(
        [_step("completed", updated_at=_NOW), _step("started", updated_at=_NOW)]
    )
    later_elsewhere = (
        task_newest_event_at(left_behind) + _LEFT_BEHIND_AFTER_ELSEWHERE_SECONDS + 60.0
    )
    completed_no_check = _multi_task([_step("completed"), _step("completed")])

    assert reduce_task_outcome(handed_off)["key"] not in {"verified"}
    left_behind_outcome = reduce_task_outcome(
        left_behind, latest_store_activity_at=later_elsewhere
    )
    assert left_behind_outcome["key"] == "mostly_done"  # the left-behind partial
    assert left_behind_outcome["key"] not in {"verified", "reported_done"}

    completed_outcome = reduce_task_outcome(completed_no_check)
    assert completed_outcome["key"] == "reported"  # not "verified" without a passing check
    assert completed_outcome["verified_step_count"] == 0
    assert completed_outcome["total_step_count"] == 2


def test_every_return_path_carries_open_step_count() -> None:
    # FIX B: the projection shape must be uniform for a future CLI reader. The
    # blocked and finding early returns used to omit open_step_count; every path
    # now carries it plus the sibling verification counts.
    count_keys = {
        "open_step_count",
        "handoff_current",
        "verified_step_count",
        "total_step_count",
        "agent_reported_step_count",
    }
    active = {"started", "checkpoint", "active", "in_progress"}

    blocked = _multi_task(
        [_step("blocked", updated_at=_NOW), _step("started", updated_at=_NOW)]
    )
    finding = _multi_task([_step("completed", updated_at=_NOW)])
    finding["task_evidence_events"] = [
        _check("failed", created_at=_NOW, event_id="finding-fail")
    ]
    verified = _multi_task([_step("completed", updated_at=_NOW)])
    verified["task_evidence_events"] = [
        _check("passed", created_at=_NOW, event_id="verify-pass")
    ]
    observed = {
        "work_items": [],
        "task_evidence_events": [],
        "sessions": [{}],
        "usage": {"rows": 0},
    }
    resolved = _multi_task(
        [_step("resolved", updated_at=_NOW), _step("completed", updated_at=_NOW)]
    )
    in_progress = _multi_task(
        [_step("completed", updated_at=_NOW), _step("started", updated_at=_NOW)]
    )

    cases = {
        "blocked": blocked,
        "finding": finding,
        "verified": verified,
        "observed": observed,
        "resolved": resolved,
        "in_progress": in_progress,
    }
    for label, task in cases.items():
        outcome = reduce_task_outcome(task)
        missing = count_keys - set(outcome)
        assert not missing, f"{label} return path missing {missing}"
        expected_open = sum(
            1
            for item in task["work_items"]
            if str(item.get("latest_status", "")).lower() in active
        )
        assert outcome["open_step_count"] == expected_open, label
    # Sanity: the branches we specifically fixed reached blocked/finding, not a
    # fall-through, and still classify as before.
    assert reduce_task_outcome(blocked)["key"] == "blocked"
    assert reduce_task_outcome(finding)["key"] == "finding"


# --- Handoff as a recency-aware disposition (a clean stop, not "mostly done") ---


def test_handoff_frontier_outranks_a_still_open_step() -> None:
    # THE FIX: a clean handoff that is the Task's frontier (nothing still-open is
    # newer) is the disposition even though one step is still open. Before, the
    # open-step branch was checked first, so a single stray open step buried the
    # handoff under "in progress"/"mostly done".
    task = _multi_task(
        [_step("started", updated_at=_NOW), _step("handed_off", updated_at=_NOW + 100)]
    )
    outcome = reduce_task_outcome(task)
    assert outcome["key"] == "handed_off"
    assert outcome["handoff_current"] is True


def test_handoff_ties_with_open_step_resolve_to_handoff() -> None:
    # A deliberate stop is a more honest summary than an incidental open step at
    # the same instant, so an equal-timestamp tie resolves to the handoff.
    task = _multi_task(
        [_step("started", updated_at=_NOW), _step("handed_off", updated_at=_NOW)]
    )
    outcome = reduce_task_outcome(task)
    assert outcome["key"] == "handed_off"
    assert outcome["handoff_current"] is True


def test_task_resumed_after_handoff_reads_in_progress_not_handed_off() -> None:
    # The recency guard: when a later OPEN step postdates the newest handoff, the
    # Task genuinely resumed — the handoff is history, not the headline, and the
    # parallel marker is off so no surface shows a stale "handed off".
    task = _multi_task(
        [_step("handed_off", updated_at=_NOW), _step("started", updated_at=_NOW + 100)]
    )
    outcome = reduce_task_outcome(task)
    assert outcome["key"] == "in_progress"
    assert outcome["handoff_current"] is False


def test_finding_outranks_handoff_but_the_marker_persists() -> None:
    # A red check at handoff time must still be the headline (you have to see it),
    # AND the handoff fact must not vanish — it rides the parallel marker.
    task = _multi_task([_step("completed"), _step("handed_off", updated_at=_NOW + 100)])
    task["task_evidence_events"] = [_check("failed", created_at=_NOW + 200, event_id="f")]
    outcome = reduce_task_outcome(task)
    assert outcome["key"] == "finding"
    assert outcome["handoff_current"] is True


def test_blocked_outranks_handoff_but_the_marker_persists() -> None:
    task = _multi_task(
        [_step("blocked", updated_at=_NOW), _step("handed_off", updated_at=_NOW + 100)]
    )
    outcome = reduce_task_outcome(task)
    assert outcome["key"] == "blocked"
    assert outcome["handoff_current"] is True


def test_task_without_any_handoff_step_carries_handoff_current_false() -> None:
    # Regression guard for the "byte-identical for non-handoff tasks" property:
    # a Task with no handed_off step must always report the marker off.
    for status in ("started", "completed", "checkpoint", "resolved", "blocked"):
        outcome = reduce_task_outcome(_multi_task([_step(status)]))
        assert outcome["handoff_current"] is False, status
