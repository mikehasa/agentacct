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
    task_went_quiet_elsewhere,
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


# The 48h + newer-session rule needs BOTH a genuinely newer session (one not
# belonging to this Task, started strictly after it went quiet) AND the store's
# latest activity to postdate that new session's start by the buffer. These
# helpers build exactly that trigger so the went-quiet fixtures stay honest.
_ELSEWHERE_SID = "elsewhere-session"


def _newer_session_start(
    task: dict[str, Any], *, after: float = 60.0, sid: str = _ELSEWHERE_SID
) -> dict[str, float]:
    """A distinct, genuinely-newer session that began ``after`` seconds past this
    Task's newest event — the first-new-session trigger the 48h rule requires."""

    return {sid: task_newest_event_at(task) + after}


def _quiet_kwargs(
    task: dict[str, Any], *, after: float = 60.0, sid: str = _ELSEWHERE_SID
) -> dict[str, Any]:
    """Reducer kwargs that put ``task`` past the went-quiet-elsewhere threshold: a
    genuinely newer session started just after it went quiet, and the store's
    latest activity is at least the 48h buffer past THAT new session's start."""

    starts = _newer_session_start(task, after=after, sid=sid)
    newer = min(starts.values())
    return {
        "latest_store_activity_at": newer + _LEFT_BEHIND_AFTER_ELSEWHERE_SECONDS + 60.0,
        "session_starts": starts,
    }


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
    quiet = _quiet_kwargs(task)

    outcome = reduce_task_outcome(task, **quiet)
    assert outcome["key"] == "mostly_done"
    assert outcome["open_step_count"] == 1
    # The open step stays open in the data; the Task is never called finished.
    assert outcome["key"] not in {"verified", "reported_done", "in_progress"}

    detail = build_task_intelligence(
        task,
        public_task_id="task_left_behind",
        title="Left behind",
        **quiet,
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
    # The buffer must be respected: a genuinely newer session began, but the store
    # kept working only a couple of hours past THAT new session's start — a normal
    # pause, not "left behind".
    task = _multi_task(
        [
            _step("completed", updated_at=_NOW),
            _step("checkpoint", updated_at=_NOW),
        ]
    )
    starts = _newer_session_start(task, after=60.0)
    newer = min(starts.values())
    only_two_hours_later = newer + 2 * 60 * 60
    assert only_two_hours_later < newer + _LEFT_BEHIND_AFTER_ELSEWHERE_SECONDS
    assert (
        reduce_task_outcome(
            task,
            latest_store_activity_at=only_two_hours_later,
            session_starts=starts,
        )["key"]
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
    # with no newer session that then ran on past the buffer) stays "in progress".
    # Here a newer session exists but the store's latest activity is only 30 min
    # past its start — well within the buffer.
    task = _multi_task(
        [
            _step("completed", updated_at=_NOW),
            _step("checkpoint", updated_at=_NOW),
        ]
    )
    starts = _newer_session_start(task, after=60.0)
    live_store = min(starts.values()) + 30 * 60  # 30 min past the new session
    outcome = reduce_task_outcome(
        task, latest_store_activity_at=live_store, session_starts=starts
    )
    assert outcome["key"] == "in_progress"


def test_open_steps_without_any_completed_step_read_inactive_when_quiet() -> None:
    # inactive: only open steps, NOTHING finished, and the store demonstrably kept
    # working far elsewhere long afterward. This is never "mostly done" (that needs
    # a completed step); it is the honest inferred "went quiet" downgrade of a
    # possibly-misleading "In progress" — never a completion claim.
    task = _multi_task(
        [_step("started", updated_at=_NOW), _step("checkpoint", updated_at=_NOW)]
    )

    outcome = reduce_task_outcome(task, **_quiet_kwargs(task))
    assert outcome["key"] == "inactive"
    assert outcome["key"] not in {"mostly_done", "verified", "reported"}
    assert outcome["went_quiet"] is True
    assert outcome["open_step_count"] == 2


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
    completed_no_check = _multi_task([_step("completed"), _step("completed")])

    assert reduce_task_outcome(handed_off)["key"] not in {"verified"}
    left_behind_outcome = reduce_task_outcome(
        left_behind, **_quiet_kwargs(left_behind)
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


# --- ended_open: inferred disposition when a session stops with a step open ----


def _sitem(status, t, *, ended=None, sid="s1"):
    d = {
        "latest_status": status,
        "updated_at": t,
        "started_at": t,
        "client": "claude-code",
        "client_session_id": sid,
        "kind": "implementation",
    }
    if ended is not None:
        d["session_ended_at"] = ended
    return d


def test_open_step_in_ended_session_reads_ended_open() -> None:
    # THE FIX: a still-open step whose session ended (SessionEnd at/after the
    # step) is not "in progress" — the session stopped without a terminal.
    o = reduce_task_outcome(_multi_task([_sitem("started", 100, ended=200)]))
    assert o["key"] == "ended_open"


def test_open_step_in_a_live_session_stays_in_progress() -> None:
    # No session-end signal (session still live / never observed ending): the
    # inference never fires; behavior is byte-identical to before this feature.
    assert reduce_task_outcome(_multi_task([_sitem("started", 100)]))["key"] == "in_progress"


def test_one_live_open_step_keeps_the_task_in_progress() -> None:
    # ended_open requires EVERY open step to be in an ended session; a single
    # still-live open step means work genuinely continues somewhere.
    task = _multi_task([_sitem("started", 100, ended=200, sid="s1"), _sitem("checkpoint", 150, sid="s2")])
    assert reduce_task_outcome(task)["key"] == "in_progress"


def test_resumed_after_session_end_reads_in_progress() -> None:
    # A newer, still-live open step (a later session resumed the work) keeps the
    # Task in progress; the earlier ended session is history.
    task = _multi_task([_sitem("started", 100, ended=150, sid="s1"), _sitem("started", 200, sid="s2")])
    assert reduce_task_outcome(task)["key"] == "in_progress"


def test_agent_handoff_outranks_inferred_ended_open() -> None:
    # The agent's own word (handed_off) beats agentacct's inference (ended_open).
    task = _multi_task([_step("handed_off", updated_at=200), _sitem("started", 100, ended=150, sid="s2")])
    assert reduce_task_outcome(task)["key"] == "handed_off"


def test_blocked_and_finding_outrank_ended_open() -> None:
    assert reduce_task_outcome(_multi_task([_sitem("blocked", 100, ended=200)]))["key"] == "blocked"
    finding = _multi_task([_sitem("started", 100, ended=200)])
    finding["task_evidence_events"] = [_check("failed", created_at=300, event_id="f")]
    assert reduce_task_outcome(finding)["key"] == "finding"


def test_completed_step_in_ended_session_is_never_ended_open() -> None:
    # ended_open is only for OPEN steps; a completed step whose session ended is
    # reported/verified, never dressed down to ended_open or fabricated done.
    assert reduce_task_outcome(_multi_task([_sitem("completed", 100, ended=200)]))["key"] == "reported"


def test_stale_session_end_before_the_step_does_not_fire() -> None:
    # A session-end that PREDATES the step's last activity is not the step's end
    # (the step was touched afterward) — stays in progress.
    assert reduce_task_outcome(_multi_task([_sitem("started", 200, ended=100)]))["key"] == "in_progress"


def test_session_end_at_the_exact_step_time_reads_ended_open() -> None:
    # The documented "at/after" boundary: a SessionEnd stamped at exactly the
    # step's last-activity time still infers ended_open (>= not >). Pins the tie
    # so a >=-to-> regression is caught.
    assert reduce_task_outcome(_multi_task([_sitem("started", 100, ended=100)]))["key"] == "ended_open"


def test_blocked_outcome_carries_the_newest_texted_blocker_and_staleness() -> None:
    """The blocked early-return must SAY why: the newest blocker that carries the
    agent's own text wins over a newer bare failure, and successes recorded
    after it are counted as a staleness fact (never a re-grade)."""

    task = {
        "work_items": [
            {
                "latest_status": "blocked",
                "title": "older blocker",
                "section_id": "s1",
                "updated_at": 100.0,
                "blocker": "waiting on approval A",
                "next_step": "ask the user",
                "evidence_status": "none",
                "evidence_events": [],
            },
            {
                "latest_status": "failed",
                "title": "newest failure, no text",
                "section_id": "s2",
                "updated_at": 300.0,
                "blocker": None,
                "evidence_status": "none",
                "evidence_events": [],
            },
            {
                "latest_status": "completed",
                "title": "later deploy",
                "section_id": "s3",
                "updated_at": 200.0,
                "evidence_status": "none",
                "evidence_events": [],
            },
        ],
        "task_evidence_events": [],
        "sessions": [],
        "usage": {"rows": 0},
    }
    outcome = reduce_task_outcome(task)
    assert outcome["key"] == "blocked"
    blocker = outcome["blocker"]
    assert blocker["text"] == "waiting on approval A"
    assert blocker["step_title"] == "older blocker"
    assert blocker["next_step"] == "ask the user"
    assert blocker["section_id"] == "s1"
    assert blocker["updated_at"] == 100.0
    assert blocker["blocked_step_count"] == 2
    assert blocker["later_completed_steps"] == 1


def test_bare_failed_blocker_detail_falls_back_to_newest_blocked_step() -> None:
    task = _task(status="failed", updated_at=50.0)
    outcome = reduce_task_outcome(task)
    assert outcome["key"] == "blocked"
    blocker = outcome["blocker"]
    assert blocker["text"] is None
    assert blocker["updated_at"] == 50.0
    assert blocker["blocked_step_count"] == 1
    assert blocker["later_completed_steps"] == 0


def test_non_blocked_outcomes_carry_no_blocker_detail() -> None:
    for status in ("completed", "started", "handed_off"):
        outcome = reduce_task_outcome(_task(status=status))
        assert outcome["key"] != "blocked"
        assert outcome["blocker"] is None


# --- inactive: open + nothing finished + the store moved on ELSEWHERE ----------


def _open_only_task() -> dict[str, Any]:
    # Open steps, NOTHING finished — the population the inactive split governs.
    return _multi_task(
        [_step("started", updated_at=_NOW), _step("checkpoint", updated_at=_NOW)]
    )


def _quiet_after(task: dict[str, Any]) -> float:
    return task_newest_event_at(task) + _LEFT_BEHIND_AFTER_ELSEWHERE_SECONDS + 60.0


def test_inactive_fires_for_open_nothing_finished_and_quiet_elsewhere() -> None:
    # (a) The core case: open steps, nothing recorded finished, and the store
    # demonstrably kept working elsewhere past the threshold -> inactive.
    task = _open_only_task()
    outcome = reduce_task_outcome(task, **_quiet_kwargs(task))
    assert outcome["key"] == "inactive"
    # Never a completion/verification claim.
    assert outcome["key"] not in {"verified", "reported", "mostly_done", "resolved"}
    assert outcome["went_quiet"] is True


def test_completed_step_plus_quiet_is_mostly_done_not_inactive() -> None:
    # (b) With at least one completed step, the SAME quiet signal reads
    # mostly_done, not inactive — the split is only on has_completed_step.
    task = _multi_task(
        [_step("completed", updated_at=_NOW), _step("checkpoint", updated_at=_NOW)]
    )
    outcome = reduce_task_outcome(task, **_quiet_kwargs(task))
    assert outcome["key"] == "mostly_done"
    assert outcome["went_quiet"] is True


def test_open_only_within_threshold_stays_in_progress() -> None:
    # (c) A newer session began, but the store kept working only 2h past its start
    # — UNDER the 48h buffer — so it is a normal pause, not abandonment.
    task = _open_only_task()
    starts = _newer_session_start(task, after=60.0)
    newer = min(starts.values())
    within = newer + 2 * 60 * 60
    assert within < newer + _LEFT_BEHIND_AFTER_ELSEWHERE_SECONDS
    outcome = reduce_task_outcome(
        task, latest_store_activity_at=within, session_starts=starts
    )
    assert outcome["key"] == "in_progress"
    assert outcome["went_quiet"] is False


def test_open_only_that_is_store_latest_never_reads_inactive() -> None:
    # (d) When this Task IS the store's latest activity (no session began strictly
    # after it), there is no newer session, so it can never retire itself — an
    # abandoned crash with nothing after it honestly stays in_progress. A session
    # that starts at the SAME instant (a tie, not strictly after) does not count.
    task = _open_only_task()
    latest = task_newest_event_at(task)
    starts = {"elsewhere-session": latest}  # tie, not strictly newer
    outcome = reduce_task_outcome(
        task, latest_store_activity_at=latest, session_starts=starts
    )
    assert outcome["key"] == "in_progress"
    assert outcome["went_quiet"] is False


def test_open_only_with_no_store_signal_stays_in_progress() -> None:
    # (e) latest_store_activity_at omitted (single-Task read / not plumbed): the
    # inference has no store 'now' to compare against and never fires.
    task = _open_only_task()
    assert reduce_task_outcome(task)["key"] == "in_progress"
    assert reduce_task_outcome(task)["went_quiet"] is False


def test_open_only_with_no_session_index_stays_in_progress() -> None:
    # (e') A store 'now' far past the Task, but NO session-start index supplied:
    # the new rule cannot prove a newer session began, so it never fires. Silence
    # (a weekend, an idle stretch) with no new session must keep the Task active.
    task = _open_only_task()
    far_now = task_newest_event_at(task) + _LEFT_BEHIND_AFTER_ELSEWHERE_SECONDS + 60.0
    outcome = reduce_task_outcome(task, latest_store_activity_at=far_now)
    assert outcome["key"] == "in_progress"
    assert outcome["went_quiet"] is False
    # Even with an index that holds ONLY this Task's own (older) session, there is
    # no genuinely newer session -> still in_progress.
    own_only = {"elsewhere-session": task_newest_event_at(task) - 10.0}
    assert (
        reduce_task_outcome(
            task, latest_store_activity_at=far_now, session_starts=own_only
        )["key"]
        == "in_progress"
    )


def test_open_only_with_zero_task_timestamps_stays_in_progress() -> None:
    # (f) Missing / zero task timestamps guard the arithmetic off — a Task whose
    # newest event is 0.0 can never be judged left behind, even under a huge
    # store 'now' AND a genuinely newer session.
    task = _multi_task(
        [_step("started", updated_at=0.0), _step("checkpoint", updated_at=0.0)]
    )
    assert task_newest_event_at(task) == 0.0
    huge_now = _LEFT_BEHIND_AFTER_ELSEWHERE_SECONDS * 100
    starts = {"elsewhere-session": 10.0}
    assert (
        reduce_task_outcome(
            task, latest_store_activity_at=huge_now, session_starts=starts
        )["key"]
        == "in_progress"
    )


def test_future_dated_task_event_skew_never_retires_the_task() -> None:
    # (g) Clock skew: a Task event dated FAR in the future means no session can
    # start strictly after it, so there is no newer session — skew can only KEEP a
    # Task active, never falsely retire it.
    future = _NOW + 10 * _LEFT_BEHIND_AFTER_ELSEWHERE_SECONDS
    task = _multi_task(
        [_step("started", updated_at=future), _step("checkpoint", updated_at=future)]
    )
    # A newer-looking session at _NOW is still BEFORE the skewed task event, and a
    # store 'now' that predates the skewed task event.
    starts = {"elsewhere-session": _NOW}
    outcome = reduce_task_outcome(
        task, latest_store_activity_at=_NOW, session_starts=starts
    )
    assert outcome["key"] == "in_progress"
    assert outcome["went_quiet"] is False


def test_blocked_and_finding_win_over_would_be_inactive() -> None:
    # (h) blocked / finding early-return before the open-step branch: a blocked or
    # failing open Task can never be relabeled inactive — blocked stays blocked.
    blocked = _multi_task([_step("blocked", updated_at=_NOW), _step("started", updated_at=_NOW)])
    assert reduce_task_outcome(blocked, latest_store_activity_at=_quiet_after(blocked))["key"] == "blocked"

    finding = _open_only_task()
    finding["task_evidence_events"] = [_check("failed", created_at=_NOW, event_id="f")]
    assert reduce_task_outcome(finding, latest_store_activity_at=_quiet_after(finding))["key"] == "finding"


def test_ended_open_wins_over_would_be_inactive() -> None:
    # (i) ended_open is a stronger, more specific stop signal evaluated before the
    # open-step branch — a session that demonstrably ended stays ended_open even
    # when the store also moved on elsewhere.
    task = _multi_task([_sitem("started", _NOW, ended=_NOW + 5)])
    assert reduce_task_outcome(task, latest_store_activity_at=_quiet_after(task))["key"] == "ended_open"


def test_handed_off_frontier_wins_over_would_be_inactive() -> None:
    # (j) A clean handoff frontier (the agent's own word) outranks the inferred
    # inactive downgrade.
    task = _multi_task(
        [_step("started", updated_at=_NOW), _step("handed_off", updated_at=_NOW + 100)]
    )
    assert reduce_task_outcome(task, latest_store_activity_at=_quiet_after(task))["key"] == "handed_off"


def test_inactive_is_deterministic_across_identical_reads() -> None:
    # (k) Two reads with the SAME store timestamps give the SAME key — the whole
    # point of comparing against a stored store 'now', never the wall clock.
    task = _open_only_task()
    quiet = _quiet_kwargs(task)
    first = reduce_task_outcome(task, **quiet)
    second = reduce_task_outcome(task, **quiet)
    assert first["key"] == second["key"] == "inactive"


def test_task_went_quiet_elsewhere_predicate_edge_cases() -> None:
    # The shared predicate directly, on the new (task, latest, session_starts)
    # signature: every guard resolves toward NOT firing.
    task = _open_only_task()
    quiet = _quiet_kwargs(task)
    starts = quiet["session_starts"]
    latest = quiet["latest_store_activity_at"]
    newer = min(starts.values())

    # Fires: a genuinely newer distinct session AND the store ran >= 48h past it.
    assert task_went_quiet_elsewhere(task, latest, starts) is True

    # No session index at all -> False (single-Task read / not plumbed).
    assert task_went_quiet_elsewhere(task, latest) is False
    assert task_went_quiet_elsewhere(task, latest, None) is False
    assert task_went_quiet_elsewhere(task, latest, {}) is False

    # No store 'now' -> False.
    assert task_went_quiet_elsewhere(task, None, starts) is False

    # A newer session exists, but the store's latest is only 1s past it (< 48h).
    just_after = newer + 1.0
    assert task_went_quiet_elsewhere(task, just_after, starts) is False

    # Exactly 48h past the newer session's start -> the boundary is inclusive.
    at_boundary = newer + _LEFT_BEHIND_AFTER_ELSEWHERE_SECONDS
    assert task_went_quiet_elsewhere(task, at_boundary, starts) is True

    # No qualifying newer session (only a same-instant tie / an older session).
    tie = {"elsewhere-session": task_newest_event_at(task)}
    assert task_went_quiet_elsewhere(task, latest, tie) is False
    older = {"elsewhere-session": task_newest_event_at(task) - 100.0}
    assert task_went_quiet_elsewhere(task, latest, older) is False

    # This Task's newest event is zero -> False even under a huge 'now'.
    zero = _multi_task([_step("started", updated_at=0.0)])
    assert (
        task_went_quiet_elsewhere(
            zero,
            _LEFT_BEHIND_AFTER_ELSEWHERE_SECONDS * 100,
            {"elsewhere-session": 10.0},
        )
        is False
    )

    # A newer session that BELONGS to this Task (same client_session_id) never
    # counts as "elsewhere".
    own = _multi_task(
        [_sitem("started", _NOW, sid="mine"), _sitem("checkpoint", _NOW, sid="mine")]
    )
    own_later = {"mine": task_newest_event_at(own) + 60.0}
    own_latest = min(own_later.values()) + _LEFT_BEHIND_AFTER_ELSEWHERE_SECONDS + 60.0
    assert task_went_quiet_elsewhere(own, own_latest, own_later) is False


def test_reducer_48h_boundary_for_inactive() -> None:
    # The reducer honours the exact 48h boundary, measured FROM the newer session's
    # start: 47h59m past it stays in_progress, exactly 48h flips to inactive.
    task = _open_only_task()
    starts = _newer_session_start(task, after=60.0)
    newer = min(starts.values())
    just_under = newer + _LEFT_BEHIND_AFTER_ELSEWHERE_SECONDS - 60.0
    assert (
        reduce_task_outcome(
            task, latest_store_activity_at=just_under, session_starts=starts
        )["key"]
        == "in_progress"
    )
    at_boundary = newer + _LEFT_BEHIND_AFTER_ELSEWHERE_SECONDS
    assert (
        reduce_task_outcome(
            task, latest_store_activity_at=at_boundary, session_starts=starts
        )["key"]
        == "inactive"
    )


def test_reducer_same_task_later_session_never_reads_inactive() -> None:
    # A LATER session that is THIS Task's own (same client_session_id) is not a
    # "newer session elsewhere" — the Task simply continued, so it stays active.
    task = _multi_task(
        [_sitem("started", _NOW, sid="mine"), _sitem("checkpoint", _NOW, sid="mine")]
    )
    later = {"mine": task_newest_event_at(task) + 60.0}
    latest = min(later.values()) + _LEFT_BEHIND_AFTER_ELSEWHERE_SECONDS + 60.0
    outcome = reduce_task_outcome(
        task, latest_store_activity_at=latest, session_starts=later
    )
    assert outcome["key"] == "in_progress"
    assert outcome["went_quiet"] is False


def test_quiet_timestamps_present_on_inactive_and_mostly_done() -> None:
    # The two factual detail-line timestamps ride the outcome dict ONLY on the
    # went-quiet keys. quiet_since = this Task's newest event; newer_session_started_at
    # = the first newer session's start the predicate keyed off. Never a
    # completion claim, just facts.
    inactive_task = _open_only_task()
    quiet = _quiet_kwargs(inactive_task)
    inactive = reduce_task_outcome(inactive_task, **quiet)
    assert inactive["key"] == "inactive"
    assert inactive["quiet_since"] == task_newest_event_at(inactive_task)
    assert inactive["newer_session_started_at"] == min(quiet["session_starts"].values())

    mostly_task = _multi_task(
        [_step("completed", updated_at=_NOW), _step("checkpoint", updated_at=_NOW)]
    )
    mquiet = _quiet_kwargs(mostly_task)
    mostly = reduce_task_outcome(mostly_task, **mquiet)
    assert mostly["key"] == "mostly_done"
    assert mostly["quiet_since"] == task_newest_event_at(mostly_task)
    assert mostly["newer_session_started_at"] == min(mquiet["session_starts"].values())


def test_quiet_timestamps_absent_or_none_on_other_keys() -> None:
    # in_progress and terminal keys carry the fields as None (never a stray
    # timestamp that a surface could misread as a quiet/abandoned signal).
    live = reduce_task_outcome(_open_only_task())
    assert live["key"] == "in_progress"
    assert live["quiet_since"] is None
    assert live["newer_session_started_at"] is None

    reported = reduce_task_outcome(_multi_task([_step("completed"), _step("completed")]))
    assert reported["key"] == "reported"
    assert reported["quiet_since"] is None
    assert reported["newer_session_started_at"] is None
