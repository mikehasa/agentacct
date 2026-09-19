"""Decision-brief and evidence-timeline projection for one agentacct Task."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .display_vocabulary import DECISION_DEFINITIONS, TIMELINE_BEAT_DEFINITION, check_result_label
from .task_timeline import BEAT_KIND, build_timeline_events, task_checks as _checks
from .task_outcome import reduce_task_outcome, step_verification_counts


TASK_INTELLIGENCE_SCHEMA_VERSION = "agent-chronicle.task-intelligence.v1"

# Outcome keys that report the work as done; a recorded next step is not
# surfaced as pending work on these.
_COMPLETED_OUTCOMES = frozenset({"reported", "verified"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _items(task: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = task.get("work_items") if isinstance(task.get("work_items"), list) else []
    return [row for row in rows if isinstance(row, Mapping)]


def _latest_checks(task: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return only the newest result for each stable machine-check identity.

    The timeline keeps historical failures and reruns, but current outcome state
    must not leave a resolved failure permanently open.  This mirrors the Work
    page's latest-per-identity semantics.
    """

    latest: dict[str, tuple[float, int, int, Mapping[str, Any]]] = {}
    for index, row in enumerate(_checks(task)):
        identity = _text(
            row.get("check_identity")
            or row.get("name")
            or row.get("command")
            or row.get("evidence_type")
            or "machine_check"
        )
        observed_at = _number(
            row.get("created_at") or row.get("occurred_at") or row.get("time")
        )
        arrival_sequence = int(_number(row.get("arrival_sequence")))
        candidate = (observed_at, arrival_sequence, index, row)
        if identity not in latest or candidate[:3] > latest[identity][:3]:
            latest[identity] = candidate
    return [value[3] for _identity, value in sorted(latest.items())]


def _latest_attempt(control: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    rows = control.get("attempts") if isinstance(control, Mapping) and isinstance(control.get("attempts"), list) else []
    candidates = [row for row in rows if isinstance(row, Mapping)]
    return max(
        candidates,
        key=lambda row: _number(row.get("created_at") or row.get("started_at") or row.get("ended_at")),
        default=None,
    )


def _state_axes(
    task: Mapping[str, Any],
    control: Mapping[str, Any] | None,
    *,
    latest_store_activity_at: float | None = None,
    session_starts: Mapping[str, float] | None = None,
) -> dict[str, dict[str, str]]:
    items = _items(task)
    attempt = _latest_attempt(control)
    if attempt is not None:
        raw_execution = _text(attempt.get("execution_state"))
        execution = {
            "pending": "queued",
            "launching": "running",
            "running": "running",
            "cancel_requested": "running",
            "succeeded": "finished",
            "failed": "finished",
            "cancelled": "cancelled",
            "lost": "lost",
        }.get(raw_execution, raw_execution or "observed")
    elif any(_text(item.get("latest_status")) in {"started", "checkpoint", "in_progress"} for item in items):
        execution = "running"
    elif items:
        execution = "finished"
    else:
        execution = "observed"

    canonical_outcome = str(
        reduce_task_outcome(
            task,
            latest_store_activity_at=latest_store_activity_at,
            session_starts=session_starts,
        ).get("key")
        or "observed"
    )
    outcome = (
        canonical_outcome
        if canonical_outcome
        in {
            "finding",
            "finding_superseded",
        "finding_resolved_by_user",
        "blocker_resolved_by_user",
            "blocked",
            "verified",
            "reported",
            "resolved",
            "handed_off",
            "ended_open",
            "inactive",
            "mostly_done",
        }
        else "unknown"
    )

    if attempt is not None:
        control_state = _text(attempt.get("control_state")) or "ready"
    else:
        approvals = control.get("approvals") if isinstance(control, Mapping) and isinstance(control.get("approvals"), list) else []
        control_state = "awaiting_approval" if any(
            isinstance(row, Mapping) and _text(row.get("state")) == "pending" for row in approvals
        ) else "ready"
    return {
        "execution": {"key": execution, "label": execution.replace("_", " ").title()},
        "outcome": {"key": outcome, "label": outcome.replace("_", " ").title()},
        "control": {"key": control_state, "label": control_state.replace("_", " ").title()},
    }


def _proof_summary(check: Mapping[str, Any]) -> str:
    return _text(check.get("summary") or check.get("name") or check.get("evidence_type") or "Recorded check")


def _decision_brief(
    task: Mapping[str, Any],
    axes: Mapping[str, Mapping[str, str]],
    *,
    latest_store_activity_at: float | None = None,
    session_starts: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    canonical = reduce_task_outcome(
        task,
        latest_store_activity_at=latest_store_activity_at,
        session_starts=session_starts,
    )
    finding = canonical.get("finding") if isinstance(canonical.get("finding"), Mapping) else None
    verification = (
        canonical.get("verification") if isinstance(canonical.get("verification"), Mapping) else None
    )
    items = _items(task)
    owner = next((_text(item.get("owner") or item.get("action_owner")) for item in reversed(items) if _text(item.get("owner") or item.get("action_owner"))), None)
    outcome = axes["outcome"]["key"]
    # The agent's recorded continuation point: the newest step that carries
    # ``next_step`` text, verbatim. No owner is required (nothing records one),
    # so gating on it left this field permanently empty. It is withheld only
    # when the Task's outcome is a completion (reported/verified), so an old
    # next step never reads as pending work on a finished Task.
    steps_with_next = [item for item in items if _text(item.get("next_step"))]
    next_action = (
        _text(
            max(
                steps_with_next,
                key=lambda item: _number(item.get("updated_at") or item.get("started_at")),
            ).get("next_step")
        )
        if steps_with_next and outcome not in _COMPLETED_OUTCOMES
        else None
    )
    attention_state = _text(canonical.get("finding_attention_state")) or None
    # The one definition per outcome key, shared with the receipt and legend.
    statement = DECISION_DEFINITIONS.get(outcome, DECISION_DEFINITIONS["unknown"])
    if outcome == "finding" and attention_state == "reviewed":
        statement = (
            "Every current finding was reviewed or marked resolved; no passing check has replaced the failed evidence."
        )
    elif outcome == "finding" and attention_state == "resolved":
        statement = "You marked the recorded finding resolved; this is not machine verification."
    # A failure is never "strongest proof": only a positive verification can be.
    # A finding / superseded-finding Task therefore has no positive proof rather
    # than a failed check masquerading as the strongest evidence.
    proof_event = verification
    proof = _proof_summary(proof_event) if proof_event is not None else None
    finding_summary = _proof_summary(finding) if finding is not None else None
    return {
        "outcome_statement": statement,
        "strongest_proof": proof,
        "unresolved_finding": finding_summary,
        "finding_attention_state": attention_state,
        "owner": owner,
        "owner_state": "recorded" if owner else "not_recorded",
        "next_action": next_action,
        "next_action_state": "recorded" if next_action else "not_recorded",
    }


def _finding_inventory(task: Mapping[str, Any]) -> dict[str, Any]:
    episodes = task.get("finding_episodes") if isinstance(task.get("finding_episodes"), list) else []
    current: list[dict[str, Any]] = []
    for episode in episodes:
        if not isinstance(episode, Mapping):
            continue
        failure = episode.get("failure_event") if isinstance(episode.get("failure_event"), Mapping) else {}
        disposition = (
            episode.get("latest_disposition")
            if isinstance(episode.get("latest_disposition"), Mapping)
            else {}
        )
        current.append(
            {
                # Process-scoped form capability, not a durable episode ID.
                "action_token": _text(episode.get("finding_token")) or None,
                "objective_state": _text(episode.get("objective_state")) or "current_failure",
                "result": _text(failure.get("result") or "failed").lower(),
                "result_label": check_result_label(failure.get("result") or "failed"),
                "summary": _proof_summary(failure),
                "evidence_type": _text(failure.get("evidence_type")) or None,
                "observed_at": _number(failure.get("created_at") or failure.get("occurred_at")),
                "attention_state": _text(episode.get("disposition_state") or "open"),
                "attention_open": bool(episode.get("attention_open")),
                "revision": int(episode.get("revision") or 0),
                "disposition": {
                    "note": _text(disposition.get("note")) or None,
                    "updated_at": disposition.get("updated_at"),
                    "chain_valid": disposition.get("chain_valid") is not False,
                },
            }
        )
    # Findings are recorded failures; a check that could not run rides the
    # same list (it is dispositionable) but is counted apart, never as a finding.
    failures = [row for row in current if row["objective_state"] != "check_not_run"]
    not_run = [row for row in current if row["objective_state"] == "check_not_run"]
    return {
        "current": current,
        "current_count": len(failures),
        "open_count": sum(row["attention_state"] == "open" for row in failures),
        "reviewed_count": sum(row["attention_state"] == "reviewed" for row in failures),
        "resolved_count": sum(row["attention_state"] == "resolved" for row in failures),
        "not_run_count": len(not_run),
        "not_run_open_count": sum(row["attention_state"] == "open" for row in not_run),
    }


def _coverage(task: Mapping[str, Any], control: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    items = _items(task)
    checks = _checks(task)
    sessions = task.get("sessions") if isinstance(task.get("sessions"), list) else []
    usage = task.get("usage") if isinstance(task.get("usage"), Mapping) else {}
    usage_rows = int(usage.get("rows") or 0)
    cost = usage.get("estimated_cost_usd")
    attempts = control.get("attempts") if isinstance(control, Mapping) and isinstance(control.get("attempts"), list) else []
    artifacts = [row for row in checks if _text(row.get("evidence_type")) == "artifact" or _text(row.get("event_type")) == "artifact"]
    return [
        {"dimension": "activity", "state": "recorded" if sessions or items else "not_recorded", "source": "client session / work ledger"},
        {"dimension": "semantics", "state": "recorded" if items else "unavailable", "source": "MCP / Work Event" if items else None},
        {"dimension": "usage", "state": "recorded" if usage_rows else "unavailable", "source": "local client log" if usage_rows else None},
        {"dimension": "cost", "state": "recorded" if cost is not None else "partial" if usage_rows else "unavailable", "source": "pricing estimate" if cost is not None else None},
        {"dimension": "checks", "state": "recorded" if checks else "not_recorded", "source": "machine evidence" if checks else None},
        {"dimension": "artifacts", "state": "recorded" if artifacts else "not_recorded", "source": "artifact evidence" if artifacts else None},
        {"dimension": "control", "state": "recorded" if attempts else "unavailable", "source": "agentacct Control Store" if attempts else None},
    ]


def _lanes(task: Mapping[str, Any]) -> list[dict[str, Any]]:
    sessions = [row for row in (task.get("sessions") if isinstance(task.get("sessions"), list) else []) if isinstance(row, Mapping)]
    primary = task.get("primary_root") if isinstance(task.get("primary_root"), Mapping) else {}
    primary_key = (_text(primary.get("client")), _text(primary.get("client_session_id")))
    root_keys = {
        (_text(row.get("client")), _text(row.get("client_session_id")))
        for row in (task.get("root_keys") if isinstance(task.get("root_keys"), list) else [])
        if isinstance(row, Mapping)
    }
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    group_order: list[tuple[str, str]] = []
    for session in sessions:
        key = (_text(session.get("client")), _text(session.get("client_session_id")))
        kind = _text(session.get("session_kind") or "root")
        if key == primary_key:
            role = "primary"
        elif key in root_keys:
            role = "continuation"
        else:
            # Child agents, internal review sessions, and any other non-root
            # session are supporting execution, not dozens of product-level
            # lanes.  Keep their raw identities in forensic evidence while the
            # Task view groups them by client.
            role = "supporting"
        client = key[0] or "unknown"
        group_key = (role, client)
        if group_key not in grouped:
            grouped[group_key] = {
                "role": role,
                "client": client,
                "models": [],
                "session_count": 0,
                "session_kinds": [],
                "usage_rows": 0,
                "total_tokens": 0,
            }
            group_order.append(group_key)
        lane = grouped[group_key]
        lane["session_count"] += 1
        if kind and kind not in lane["session_kinds"]:
            lane["session_kinds"].append(kind)
        usage = session.get("usage") if isinstance(session.get("usage"), Mapping) else {}
        model_rows = usage.get("model_lanes") if isinstance(usage.get("model_lanes"), list) else []
        observed_models = (
            session.get("observed_models") if isinstance(session.get("observed_models"), list) else []
        )
        models = [_text(model) for model in observed_models if _text(model)]
        models.extend(
            _text(row.get("model"))
            for row in model_rows
            if isinstance(row, Mapping) and _text(row.get("model"))
        )
        for model in models:
            if model not in lane["models"]:
                lane["models"].append(model)
        lane["usage_rows"] += int(usage.get("rows") or 0)
        lane["total_tokens"] += int(usage.get("total_tokens") or 0)

    lanes: list[dict[str, Any]] = []
    for index, group_key in enumerate(group_order):
        lane = grouped[group_key]
        count = int(lane["session_count"])
        role = str(lane["role"])
        if role == "primary":
            role_label = "Primary"
        elif role == "continuation":
            role_label = "Continuation" if count == 1 else f"{count} continuations"
        else:
            role_label = "Supporting" if count == 1 else f"{count} supporting sessions"
        lanes.append(
            {
                "lane_id": f"lane-{index + 1}",
                "role": role,
                "role_label": role_label,
                "client": lane["client"],
                "models": list(lane["models"]),
                "session_count": count,
                "session_kinds": list(lane["session_kinds"]),
                "usage": {
                    "rows": lane["usage_rows"],
                    "total_tokens": lane["total_tokens"],
                },
            }
        )
    return lanes


def _task_span_seconds(task: Mapping[str, Any]) -> float | None:
    starts: list[float] = []
    ends: list[float] = []
    sessions = task.get("sessions") if isinstance(task.get("sessions"), list) else []
    for session in sessions:
        if not isinstance(session, Mapping):
            continue
        start = _number(session.get("first_activity_at") or session.get("started_at"))
        end = _number(session.get("last_activity_at") or session.get("updated_at"))
        if start:
            starts.append(start)
        if end:
            ends.append(end)
    if not starts or not ends or max(ends) < min(starts):
        return None
    return max(ends) - min(starts)


def _timeline(
    task: Mapping[str, Any], control: Mapping[str, Any] | None, *, limit: int
) -> tuple[list[dict[str, Any]], int]:
    events = build_timeline_events(task, _checks(task), control)
    total = len(events)
    if total <= limit:
        return events, total
    selected = events[-limit:]
    dropped = events[:-limit]
    # What survives truncation, inside a fixed budget: EVIDENCE first, then the
    # loudest work.
    #
    # Salience used to be constant, so "keep the important ones" kept every
    # older check by accident. Now that it varies, two things would go wrong at
    # once with that rule: a routine passing check would be dropped for not
    # being loud, and a run of salient sections would crowd out the checks that
    # are loud. Checks therefore get first claim on the budget — evidence is the
    # one lane this product exists to show — and salient work fills what is
    # left. Beats are narration under a section retained on its own merits, so
    # they are the first thing truncation gives up.
    budget = 5
    keep = [event for event in dropped if event.get("kind") == "check"][-budget:]
    remaining = budget - len(keep)
    if remaining > 0:
        keep += [event for event in dropped if event.get("important") and event.get("kind") != "check"][-remaining:]
    # Restore source-time order: inserting each at index zero reversed the
    # retained prefix in text/receipt output.
    order = {id(event): index for index, event in enumerate(dropped)}
    keep.sort(key=lambda event: order[id(event)])
    return keep + selected, total


def build_task_intelligence(
    task: Mapping[str, Any],
    *,
    public_task_id: str,
    title: str,
    control: Mapping[str, Any] | None = None,
    timeline_limit: int = 50,
    latest_store_activity_at: float | None = None,
    session_starts: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    if timeline_limit < 1 or timeline_limit > 200:
        raise ValueError("timeline_limit must be between 1 and 200")
    axes = _state_axes(
        task,
        control,
        latest_store_activity_at=latest_store_activity_at,
        session_starts=session_starts,
    )
    timeline, total = _timeline(task, control, limit=timeline_limit)
    usage = task.get("usage") if isinstance(task.get("usage"), Mapping) else {}
    return {
        "schema_version": TASK_INTELLIGENCE_SCHEMA_VERSION,
        "task_id": public_task_id,
        "title": title,
        "states": axes,
        "decision_brief": _decision_brief(
            task,
            axes,
            latest_store_activity_at=latest_store_activity_at,
            session_starts=session_starts,
        ),
        # DECISION 3b: partial verification, not all-or-nothing. Surfaces can say
        # "N of M steps verified" instead of one verified/grey flag for the Task.
        "verification": step_verification_counts(task),
        "findings": _finding_inventory(task),
        # This is the Task's canonical session aggregate. Work-level usage is
        # never added again.
        "usage": dict(usage),
        "duration_seconds": _task_span_seconds(task),
        "models": list(task.get("models") or ()),
        "coverage": _coverage(task, control),
        "lanes": _lanes(task),
        "timeline": {
            "schema_version": "agentacct.task-timeline.v1",
            "events": timeline,
            "shown": len(timeline),
            "total": total,
            "truncated": len(timeline) < total,
            # Beats are recorded progress notes, not steps. They are counted
            # separately and named so a reader of "12 records" can see that
            # three of them are narration: no step count, coverage denominator
            # or check tally anywhere in this payload includes them.
            "beat_count": sum(1 for event in timeline if event.get("kind") == BEAT_KIND),
            "beat_definition": TIMELINE_BEAT_DEFINITION,
        },
        "raw_evidence": {
            "work_item_count": len(_items(task)),
            "check_count": len(_checks(task)),
            "session_count": len(task.get("sessions") if isinstance(task.get("sessions"), list) else []),
            "available": True,
        },
    }


__all__ = ["TASK_INTELLIGENCE_SCHEMA_VERSION", "build_task_intelligence"]
