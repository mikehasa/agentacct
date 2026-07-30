"""Canonical Task outcome reduction shared by every product surface."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .finding_disposition import finding_target_digest


_SUCCESS_STATUSES = {"completed", "passed"}
_RESOLVED_STATUS = "resolved"
_BLOCKED_STATUSES = {"blocked", "failed"}
_ACTIVE_STATUSES = {"started", "checkpoint", "active", "in_progress"}


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


def reduce_task_outcome(task: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce work status and checks into one honest current Task outcome.

    A Task-level passing check verifies terminal work only when every current
    work step succeeded and every latest check was recorded at or after the
    newest work update. Older checks cannot prove code changed later.
    """

    items = _items(task)
    statuses = [_text(item.get("latest_status")).lower() for item in items]
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
        }
    current_failures = [
        event
        for event in checks
        if _text(event.get("result")).lower() in {"failed", "error"}
    ]
    if current_failures:
        # A superseded failure is contradicted by a later same-scope pass. It is
        # never removed from the check set (so it can never fall through to a
        # verified outcome and stays reopenable), but it is not a standing
        # finding. Only non-superseded failures pin the Task as an open finding.
        standing_failures = [
            event
            for event in current_failures
            if _text(event.get("supersession_state")).lower() != "superseded"
        ]
        superseded_failures = [
            event
            for event in current_failures
            if _text(event.get("supersession_state")).lower() == "superseded"
        ]
        if standing_failures:
            episodes = task.get("finding_episodes") if isinstance(task.get("finding_episodes"), list) else []
            dispositions = {
                str(episode.get("target_digest")): str(episode.get("disposition_state") or "open")
                for episode in episodes
                if isinstance(episode, Mapping) and episode.get("target_digest")
            }
            failure_states = [
                dispositions.get(str(finding_target_digest(event) or ""), "open")
                for event in standing_failures
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
                    standing_failures,
                    key=lambda event: (
                        _number(event.get("created_at") or event.get("occurred_at") or event.get("time")),
                        _integer(event.get("arrival_sequence")),
                    ),
                ),
                "verification": None,
                "latest_checks": checks,
                "findings": standing_failures,
                "finding_attention_state": attention_state,
                "max_work_updated_at": max_work_updated_at,
            }
        # Every current failure was superseded by a later same-scope pass: the
        # Task drops out of "Needs attention" into its own resolved-in-a-later-
        # check state, still visible and counted, never verified.
        return {
            "key": "finding_superseded",
            "finding": None,
            "verification": None,
            "latest_checks": checks,
            "findings": superseded_failures,
            "superseded_findings": superseded_failures,
            "max_work_updated_at": max_work_updated_at,
        }
    if statuses and any(status == _RESOLVED_STATUS for status in statuses) and all(
        status in _SUCCESS_STATUSES or status == _RESOLVED_STATUS
        for status in statuses
    ):
        # An explicit blocker resolution is an evidence-backed agent claim,
        # not a completion report and not authoritative verification.
        key = "resolved"
    elif any(status in _ACTIVE_STATUSES for status in statuses):
        key = "in_progress"
    elif not items:
        key = "observed"
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
    }


__all__ = [
    "evidence_event_key",
    "finding_check_key",
    "latest_check_events",
    "latest_task_checks",
    "reduce_task_outcome",
]
