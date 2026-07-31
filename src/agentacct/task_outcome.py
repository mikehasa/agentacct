"""Canonical Task outcome reduction shared by every product surface."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Mapping

from .finding_disposition import finding_target_digest


_SUCCESS_STATUSES = {"completed", "passed"}
_RESOLVED_STATUS = "resolved"
_BLOCKED_STATUSES = {"blocked", "failed"}
_ACTIVE_STATUSES = {"started", "checkpoint", "active", "in_progress"}
# A clean stop: the user handed the work off / continued in a new session. It is
# terminal (never "open"), but it is NOT a completion or verification claim and
# NOT a blocker/failure. See DECISION 1.
_HANDED_OFF_STATUS = "handed_off"

# DECISION 3a staleness threshold. When a Task has finished steps but one or more
# steps are still `started`/`checkpoint`, we distinguish "genuinely in progress"
# from "mostly done, some steps left open" by whether the WHOLE Task has gone
# untouched for longer than this. 12h comfortably exceeds a continuous working
# session (so live multi-hour work still reads "in progress" — no over-correction)
# yet is far short of "days", so a Task last touched overnight or longer reads as
# left-open rather than falsely active. This only ever downgrades "in progress"
# to an equally honest partial state; it never infers completion.
_STALE_OPEN_STEP_SECONDS = 12 * 60 * 60


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (OverflowError, TypeError, ValueError):
        return 0.0


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0


def _items(task: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = task.get("work_items") if isinstance(task.get("work_items"), list) else []
    return [row for row in rows if isinstance(row, Mapping)]


def _step_is_verified(item: Mapping[str, Any]) -> bool:
    """Is this single step verified by the SAME two sources the Work card uses?

    DECISION 3b exposes a partial breakdown; it must not invent a third notion of
    "verified". A step counts only when it completed AND either its own latest
    per-identity checks all pass, or (with no checks) its log evidence reached
    ``strong``. This mirrors ``api._work_product_state`` exactly, kept in the
    engine so the HTML and a future CLI read the identical count.
    """

    if _text(item.get("latest_status")).lower() not in _SUCCESS_STATUSES:
        return False
    raw = (
        item.get("current_check_events")
        if isinstance(item.get("current_check_events"), list)
        else item.get("evidence_events")
    )
    events = [event for event in raw if isinstance(event, Mapping)] if isinstance(raw, list) else []
    checks = latest_check_events(events, task_scoped=True)
    latest_all_pass = bool(checks) and all(
        _text(event.get("result")).lower() == "passed" for event in checks
    )
    projected_checks = isinstance(item.get("current_check_events"), list)
    evidence_strong = _text(item.get("evidence_status")).lower() == "strong"
    return latest_all_pass or (evidence_strong and not checks and not projected_checks)


def step_verification_counts(task: Mapping[str, Any]) -> dict[str, int]:
    """Per-Task partial verification: verified vs agent-reported-only step counts.

    Exposed so a surface can honestly say "3 of 5 steps verified" instead of a
    single all-or-nothing verified/grey. ``total_step_count`` is every recorded
    step; ``verified_step_count`` the subset proven by a passing check or strong
    evidence; ``agent_reported_step_count`` the remaining terminal-success steps
    that only have an agent report.
    """

    items = _items(task)
    verified = sum(1 for item in items if _step_is_verified(item))
    success = sum(
        1 for item in items if _text(item.get("latest_status")).lower() in _SUCCESS_STATUSES
    )
    return {
        "verified_step_count": verified,
        "total_step_count": len(items),
        "agent_reported_step_count": max(0, success - verified),
    }


def _check_identity(event: Mapping[str, Any]) -> str:
    return _text(
        event.get("check_identity")
        or event.get("name")
        or event.get("command")
        or event.get("evidence_type")
        or "machine_check"
    )


def finding_check_key(
    event: Mapping[str, Any],
    *,
    task_scoped: bool = False,
) -> str:
    """Stable check identity inside its asserted source/scope namespace.

    A same-named check in another project, client session, or semantic
    namespace must never close this finding. Missing dimensions remain empty;
    when an adapter exposes no scope at all, its stable check identity is the
    only retry signal available and is intentionally source-local.
    """

    namespace = _text(
        event.get("namespace_fingerprint")
        or event.get("session_namespace_fingerprint")
    )
    project = _text(event.get("project_identity") or event.get("project_dir"))
    # A proven Task is already the continuation boundary, so session/work ids
    # must not prevent its Task-level rerun from closing a section failure.
    # Outside that boundary, strict dimensions always remain: same-name checks
    # from different sessions/work must not close each other merely because
    # they share a project.
    session = "" if task_scoped else _text(event.get("client_session_id"))
    transcript = "" if task_scoped else _text(event.get("client_transcript_id"))
    work = "" if task_scoped else _text(event.get("work_id") or event.get("section_id"))
    identity = _check_identity(event)
    stable_marker = event.get("check_identity_stable")
    identity_is_stable = stable_marker is True or (
        stable_marker is None
        and bool(_text(event.get("check_identity")))
        and not identity.startswith("type:")
    )
    if not identity_is_stable:
        episode = _text(
            event.get("event_id")
            or event.get("event_digest")
            or event.get("created_at")
        )
        identity = f"{identity}\x1eepisode:{episode}"
    fields = (
        _text(event.get("source_type")),
        _text(event.get("source")),
        _text(event.get("client")),
        namespace,
        project,
        session,
        transcript,
        work,
        identity,
    )
    return "\x1f".join(fields)


def evidence_event_key(event: Mapping[str, Any]) -> tuple[str, ...]:
    """Source-scoped content identity for deduping one evidence observation."""

    event_id = _text(event.get("event_id"))
    if event_id:
        namespace = _text(
            event.get("namespace_fingerprint")
            or event.get("session_namespace_fingerprint")
            or event.get("project_identity")
            or event.get("client_session_id")
        )
        digest = _text(event.get("event_digest")) or hashlib.sha256(
            json.dumps(
                dict(event),
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        return (
            "event_id",
            _text(event.get("source_type")),
            _text(event.get("source")),
            _text(event.get("client")),
            namespace,
            event_id,
            digest,
        )
    return (
        "legacy",
        json.dumps(
            dict(event),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ),
    )


def _all_check_events(task: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if isinstance(task.get("current_check_events"), list):
        return [
            event
            for event in task.get("current_check_events", [])
            if isinstance(event, Mapping)
        ]
    items = _items(task)
    sources = [
        *(item.get("evidence_events") for item in items),
        task.get("task_evidence_events"),
    ]
    result: list[Mapping[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for source in sources:
        if not isinstance(source, list):
            continue
        for event in source:
            if not isinstance(event, Mapping):
                continue
            dedupe_key = evidence_event_key(event)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            result.append(event)
    return result


def latest_check_events(
    events: list[Mapping[str, Any]],
    *,
    task_scoped: bool = False,
) -> list[Mapping[str, Any]]:
    """Newest result per source-scoped stable check, using receipt order for ties."""

    latest: dict[str, tuple[float, int, int, Mapping[str, Any]]] = {}
    for index, event in enumerate(events):
        result = _text(event.get("result")).lower()
        if result not in {"passed", "failed", "error"}:
            continue
        candidate = (
            _number(event.get("created_at") or event.get("occurred_at") or event.get("time")),
            _integer(event.get("arrival_sequence")),
            index,
            event,
        )
        identity = finding_check_key(event, task_scoped=task_scoped)
        if identity not in latest or candidate[:3] > latest[identity][:3]:
            latest[identity] = candidate
    return [value[3] for _identity, value in sorted(latest.items())]


def latest_task_checks(task: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Newest result per source-scoped stable check across this Task."""

    return latest_check_events(_all_check_events(task), task_scoped=True)


def reduce_task_outcome(
    task: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    """Reduce work status and checks into one honest current Task outcome.

    A Task-level passing check verifies terminal work only when every current
    work step succeeded and every latest check was recorded at or after the
    newest work update. Older checks cannot prove code changed later.

    ``now`` (seconds) is the DECISION 3a staleness reference; it defaults to the
    wall clock and is injectable so tests are deterministic.

    NOTE (DECISION 3c): the product strip has a fourth "outcome" stage that this
    reducer never lights on its own. That stage depends on a run / RunStore /
    outcome.json that the MCP recording flow never creates (the store's runs/ dir
    stays empty in MCP-first usage), so it is a dead feature here and belongs to a
    later model redesign. Deliberately not revived — no behavior change for it.
    """

    items = _items(task)
    statuses = [_text(item.get("latest_status")).lower() for item in items]
    step_counts = step_verification_counts(task)
    max_work_updated_at = max(
        (
            _number(item.get("updated_at") or item.get("started_at"))
            for item in items
        ),
        default=0.0,
    )
    checks = latest_task_checks(task)
    # Explicit work state is the product's user-action contract. A current
    # failed check may explain the blocker, but it must never demote a
    # recorded ``blocked``/``failed`` step (or explicit blocker text) into a
    # non-actionable finding.
    if any(status in _BLOCKED_STATUSES for status in statuses) or any(
        _text(item.get("blocker")) for item in items
    ):
        return {
            "key": "blocked",
            "finding": None,
            "verification": None,
            "latest_checks": checks,
            "max_work_updated_at": max_work_updated_at,
            **step_counts,
        }
    current_failures = [
        event
        for event in checks
        if _text(event.get("result")).lower() in {"failed", "error"}
    ]
    if current_failures:
        episodes = task.get("finding_episodes") if isinstance(task.get("finding_episodes"), list) else []
        dispositions = {
            str(episode.get("target_digest")): str(episode.get("disposition_state") or "open")
            for episode in episodes
            if isinstance(episode, Mapping) and episode.get("target_digest")
        }
        failure_states = [
            dispositions.get(str(finding_target_digest(event) or ""), "open")
            for event in current_failures
        ]
        attention_state = (
            "open"
            if "open" in failure_states
            else "reviewed"
            if "reviewed" in failure_states
            else "resolved"
        )
        return {
            "key": "finding",
            "finding": max(
                current_failures,
                key=lambda event: (
                    _number(event.get("created_at") or event.get("occurred_at") or event.get("time")),
                    _integer(event.get("arrival_sequence")),
                ),
            ),
            "verification": None,
            "latest_checks": checks,
            "findings": current_failures,
            "finding_attention_state": attention_state,
            "max_work_updated_at": max_work_updated_at,
            **step_counts,
        }
    open_step_count = sum(1 for status in statuses if status in _ACTIVE_STATUSES)
    if statuses and any(status == _RESOLVED_STATUS for status in statuses) and all(
        status in _SUCCESS_STATUSES or status == _RESOLVED_STATUS
        for status in statuses
    ):
        # An explicit blocker resolution is an evidence-backed agent claim,
        # not a completion report and not authoritative verification.
        key = "resolved"
    elif open_step_count:
        # DECISION 3a: one un-closed step must not drag a mostly-finished Task to
        # plain "in progress". The open step stays open in the DATA — we only
        # summarise the Task honestly. When at least one step actually completed
        # AND nothing across the Task has been touched within
        # ``_STALE_OPEN_STEP_SECONDS``, the open steps were left open (a switched
        # session / handoff that never recorded a terminal), not actively live.
        # Conservative on both sides: never claims the Task finished, and a
        # genuinely recent open step still reads "in progress".
        reference_now = time.time() if now is None else now
        has_completed_step = any(status in _SUCCESS_STATUSES for status in statuses)
        stale = (
            has_completed_step
            and max_work_updated_at > 0.0
            and (reference_now - max_work_updated_at) > _STALE_OPEN_STEP_SECONDS
        )
        key = "mostly_done" if stale else "in_progress"
    elif not items:
        key = "observed"
    elif any(status == _HANDED_OFF_STATUS for status in statuses):
        # DECISION 1: every step is terminal and at least one was a clean
        # handoff. A deliberate stop — never a completed/verified claim, never a
        # blocker/failure.
        key = "handed_off"
    else:
        all_successful = all(status in _SUCCESS_STATUSES for status in statuses)
        passing_checks = [event for event in checks if _text(event.get("result")).lower() == "passed"]
        checks_are_current = bool(checks) and all(
            _text(event.get("result")).lower() == "passed"
            and _number(event.get("created_at") or event.get("occurred_at") or event.get("time"))
            >= max_work_updated_at
            and _number(event.get("created_at") or event.get("occurred_at") or event.get("time")) > 0
            for event in checks
        )
        strong_without_checks = not isinstance(task.get("current_check_events"), list) and not checks and all(
            _text(item.get("evidence_status")).lower() == "strong" for item in items
        )
        key = (
            "verified"
            if all_successful and (checks_are_current or strong_without_checks)
            else "reported"
        )
    verification = None
    if key == "verified":
        passing = [event for event in checks if _text(event.get("result")).lower() == "passed"]
        verification = max(
            passing,
            key=lambda event: (
                _number(event.get("created_at") or event.get("occurred_at") or event.get("time")),
                _integer(event.get("arrival_sequence")),
            ),
            default=None,
        )
    return {
        "key": key,
        "finding": None,
        "verification": verification,
        "latest_checks": checks,
        "max_work_updated_at": max_work_updated_at,
        "open_step_count": open_step_count,
        **step_counts,
    }


__all__ = [
    "evidence_event_key",
    "finding_check_key",
    "step_verification_counts",
    "latest_check_events",
    "latest_task_checks",
    "reduce_task_outcome",
]
