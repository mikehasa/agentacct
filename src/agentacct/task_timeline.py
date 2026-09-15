"""Shared task timeline records and bounded, immutable history-page snapshots.

The receipt, native UI and text renderers use the same event projection. Clients
own viewport/selection state; they do not infer event identity or relationships.
"""
from __future__ import annotations

from .display_budget import display_label_from_text
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from copy import deepcopy
import hashlib
import json
import math
import threading
import time
from typing import Any
import uuid

SCHEMA = "agentacct.task-timeline.v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def valid_time(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and 0 < number < 253_402_300_800 else None


def _rows(value: Any) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _files(value: Any) -> list[str]:
    return sorted({_text(path) for path in value if isinstance(path, str) and _text(path)}) if isinstance(value, list) else []


def check_source(check: Mapping[str, Any]) -> str:
    """Trust the ledger's source type, never an agent-authored source name."""
    source_type = _text(check.get("source_type")).lower()
    if source_type == "client_hook":
        return "hook"
    if source_type in {"ci", "external", "provider"}:
        return "ci"
    return "mcp"


def _source_label(source: str, kind: str) -> str:
    if kind == "work":
        return "Agent section report"
    return {"mcp": "Agent-reported check", "agent_report": "Agent-reported check",
            "agent": "Agent-reported check", "hook": "Client hook", "client_hook": "Client hook",
            "ci": "CI source", "external": "External source", "provider": "Provider source"}.get(source, source or "Source unavailable")


def task_checks(task: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Keep historical evidence, deduplicating only supplied immutable IDs."""
    rows: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    sources = [task.get("task_evidence_events")]
    sources.extend(item.get("evidence_events") for item in _rows(task.get("work_items")))
    for source in sources:
        for row in _rows(source):
            key = _text(row.get("event_id"))
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            rows.append(row)
    return sorted(rows, key=lambda row: valid_time(row.get("created_at") or row.get("occurred_at")) or 0)


def session_display_titles(task: Mapping[str, Any]) -> dict[tuple[str, str], str]:
    """Use the first recorded section when a supporting session has no title."""
    titles: dict[tuple[str, str], str] = {}
    for item in _rows(task.get("work_items")):
        key = (_text(item.get("client") or item.get("reporting_source")), _text(item.get("client_session_id")))
        title = _text(item.get("title") or item.get("objective")) or display_label_from_text(item.get("summary"))
        if all(key) and title:
            titles.setdefault(key, title)
    for session in _rows(task.get("sessions")):
        key = (_text(session.get("client")), _text(session.get("client_session_id")))
        title = _text(session.get("client_session_title") or session.get("title"))
        if all(key) and title:
            titles[key] = title
    return titles


def build_timeline_events(task: Mapping[str, Any], checks: Sequence[Mapping[str, Any]] | None = None,
                          control: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Project already joined task evidence. Only a supplied event ID deduplicates checks."""
    primary = task.get("primary_root") if isinstance(task.get("primary_root"), Mapping) else {}
    primary_key = (_text(primary.get("client")), _text(primary.get("client_session_id")))
    titles = session_display_titles(task)
    events: list[dict[str, Any]] = []
    sections_by_event: dict[str, list[dict[str, Any]]] = {}
    occurrences: dict[str, int] = {}

    def identity(base: str) -> str:
        occurrences[base] = occurrences.get(base, 0) + 1
        return base if occurrences[base] == 1 else f"{base}#{occurrences[base]}"

    def session_fields(key: tuple[str, str]) -> dict[str, Any]:
        known = bool(key[0] and key[1])
        role = "Root" if key == primary_key else "Supporting"
        title = titles.get(key, "")
        return {"client": key[0] or None, "client_session_id": key[1] or None,
                "session_key": f"{key[0]}::{key[1]}" if known else None,
                "session_title": title or (f"{key[0]} · {key[1][:16]}" if known else "Task evidence"),
                "lineage": f"{role} session · {key[1]}" if known else "Session attribution unavailable"}

    for item in _rows(task.get("work_items")):
        key = (_text(item.get("client") or item.get("reporting_source")), _text(item.get("client_session_id")))
        stable = _text(item.get("work_id") or item.get("section_id"))
        record_id = identity("work:" + _hash([*key, stable] if stable else [*key, dict(item)]))
        first, last = valid_time(item.get("started_at")), valid_time(item.get("updated_at"))
        warning = "The recorded update precedes the start. Timing is inconsistent." if first and last and last < first else None
        end = last if first and last and last > first else None
        source = _text(item.get("reporting_source") or item.get("client"))
        event = {"id": record_id, "event_id": None, "kind": "work", "occurred_at": first or last,
                 "started_at": first or last, "updated_at": end,
                 "time_note": "Source time unavailable" if not (first or last) else "Recorded section start → latest update; not execution duration" if end else "Recorded section point; duration unavailable",
                 "time_warning": warning, "lane": "primary" if key == primary_key else "supporting",
                 # A card title is a LABEL, not a paragraph. Falling back to the
                 # summary handed a 1,200-character paragraph to a card that
                 # renders two lines of 14 pt text in a 200x80 pt box, so the
                 # reader saw an arbitrary middle slice of a sentence. The fallback
                 # now reduces the prose to a statement at the label budget; the
                 # full summary still travels in its own field.
                 "title": _text(item.get("title"))
                 or display_label_from_text(item.get("summary"))
                 or "Recorded work",
                 "status": _text(item.get("latest_status")) or "recorded", "source": source,
                 "source_label": _source_label(source, "work"), "confidence": _text(item.get("join_confidence")) or "claimed",
                 "important": bool(item.get("blocker")), "scope": stable or None,
                 "summary": _text(item.get("summary")) or None, "files": _files(item.get("files")),
                 "resolution": f"Reported blocker: {_text(item['blocker'])}" if item.get("blocker") else None,
                 "identity_note": None if stable else "No stable section identity; content identity is used.",
                 **session_fields(key)}
        events.append(event)
        for check in _rows(item.get("evidence_events")):
            if event_id := _text(check.get("event_id")):
                sections_by_event.setdefault(event_id, []).append(event)

    from .finding_disposition import finding_target_digest
    episodes = {_text(row.get("target_digest")): row for row in _rows(task.get("finding_episodes"))}
    seen: set[str] = set()
    for check in task_checks(task) if checks is None else checks:
        event_id = _text(check.get("event_id"))
        if event_id and event_id in seen:
            continue
        if event_id:
            seen.add(event_id)
        record_id = identity("event:" + event_id if event_id else "anonymous-check:" + _hash(dict(check)))
        parents = list({row["id"]: row for row in sections_by_event.get(event_id, [])}.values())
        parent = parents[0] if len(parents) == 1 else None
        raw_key = (_text(check.get("client")), _text(check.get("client_session_id")))
        parent_key = (parent["client"], parent["client_session_id"]) if parent else ("", "")
        conflict = any(raw and raw != inherited
                       for candidate in parents
                       for raw, inherited in zip(raw_key, (candidate["client"], candidate["client_session_id"])))
        if conflict:
            parents, parent = [], None
        key = raw_key if all(raw_key) else parent_key if parent else ("", "")
        source = check_source(check)
        at = valid_time(check.get("created_at") or check.get("occurred_at"))
        episode = episodes.get(str(finding_target_digest(check) or ""), {})
        disposition = _text(episode.get("disposition_state")) if episode.get("attention_open") is False else None
        note = "Conflicting session identity; no section relationship is assumed." if conflict else None
        if not event_id:
            note = "No event ID; identical anonymous records remain separate."
        event = {"id": record_id, "event_id": event_id or None, "kind": "check", "occurred_at": at,
                 "started_at": at, "updated_at": None, "time_note": "Recorded check point; duration unavailable" if at else "Source time unavailable",
                 "lane": "evidence", "title": _text(check.get("name") or check.get("summary") or check.get("evidence_type")) or "Recorded check",
                 "status": _text(check.get("result")).lower() or "unknown", "source": source,
                 "source_label": _source_label(source, "check"), "confidence": "observed", "important": True,
                 "scope": _text(check.get("check_identity") or check.get("resolution_scope")) or None,
                 "summary": _text(check.get("summary")) or None, "files": _files(check.get("files")),
                 "section_record_id": parent["id"] if parent else None,
                 "section_record_ids": [row["id"] for row in parents], "section_title": parent["title"] if parent else None,
                 "superseded": _text(check.get("supersession_state")) == "superseded",
                 "superseded_by_event_id": _text(check.get("superseded_by_event_id")) or None,
                 "resolution": _text(check.get("resolution_summary")) or None,
                 "resolution_scope": _text(check.get("resolution_scope")) or None,
                 "identity_note": note, "disposition": disposition or None, **session_fields(key)}
        event["exit_code"] = check.get("exit_code") if type(check.get("exit_code")) is int else None
        for field in ("artifact_ref", "artifact_path", "artifact_url"):
            event[field] = _text(check.get(field)) or None
        for field in ("artifact_path_redacted", "artifact_url_redacted", "command_redacted"):
            event[field] = check.get(field) is True
        # A source's explicit redaction flag always wins over accidentally retained values.
        if event.get("artifact_path_redacted"):
            event["artifact_path"] = None
        if event.get("artifact_url_redacted"):
            event["artifact_url"] = None
        events.append(event)

    if isinstance(control, Mapping):
        for kind, rows in [("attempt", _rows(control.get("attempts"))), ("control", _rows(control.get("events")))]:
            for row in rows:
                stable = _text(row.get("attempt_id") if kind == "attempt" else row.get("event_id"))
                at = valid_time(row.get("started_at") or row.get("created_at") if kind == "attempt" else row.get("occurred_at"))
                events.append({"id": identity(kind + ":" + (stable or _hash(dict(row)))), "kind": kind,
                               "occurred_at": at, "started_at": at, "lane": "control",
                               "title": "agentacct-owned execution attempt" if kind == "attempt" else _text(row.get("action") or "Control action").replace("_", " ").title(),
                               "status": _text(row.get("execution_state") if kind == "attempt" else row.get("next_state")) or "recorded",
                               "source": "agentacct Control Store", "source_label": "agentacct Control Store", "confidence": "owned", "important": True,
                               **session_fields(("", ""))})
    return sorted(events, key=lambda event: (event["occurred_at"] is None, event["occurred_at"] or 0, event["id"]))


class TimelineCursorError(ValueError):
    """A malformed, expired or cross-task cursor must never mix snapshots."""


class TimelineSnapshotCache:
    def __init__(self, *, capacity: int = 8, ttl: float = 120, clock=time.monotonic):
        if capacity < 1 or ttl <= 0:
            raise ValueError("snapshot capacity and lifetime must be positive")
        self.capacity, self.ttl, self.clock = capacity, ttl, clock
        self._snapshots: OrderedDict[str, tuple[float, str, list[dict[str, Any]]]] = OrderedDict()
        self._lock = threading.Lock()

    def page(self, task_id: str, *, limit: int, events: list[dict[str, Any]] | None = None,
             cursor: str | None = None) -> dict[str, Any]:
        if not 1 <= limit <= 500:
            raise ValueError("timeline limit must be between 1 and 500")
        with self._lock:
            now = self.clock()
            for key in list(self._snapshots):
                if now - self._snapshots[key][0] >= self.ttl:
                    del self._snapshots[key]
            if cursor is None:
                if events is None:
                    raise ValueError("initial timeline page needs events")
                # Reuse an unchanged snapshot so polling clients can keep their
                # assembled history without downloading every older page again.
                snapshot = next((key for key, saved in reversed(self._snapshots.items())
                                 if saved[1] == task_id and saved[2] == events), uuid.uuid4().hex)
                offset = 0
                if snapshot in self._snapshots:
                    self._snapshots[snapshot] = (now, task_id, self._snapshots[snapshot][2])
                    self._snapshots.move_to_end(snapshot)
                else:
                    self._snapshots[snapshot] = (now, task_id, deepcopy(events))
                while len(self._snapshots) > self.capacity:
                    self._snapshots.popitem(last=False)
            else:
                try:
                    snapshot, encoded = cursor.split(":")
                    offset = int(encoded)
                    if str(offset) != encoded or offset < 0:
                        raise ValueError()
                except (ValueError, TypeError):
                    raise TimelineCursorError("Invalid timeline cursor") from None
            saved = self._snapshots.get(snapshot)
            if saved is None or saved[1] != task_id or offset > len(saved[2]):
                raise TimelineCursorError("Timeline history expired; reload the task")
            self._snapshots.move_to_end(snapshot)
            rows = saved[2]
            # Newest page first, preserving chronological order within each page.
            end = len(rows) - offset
            start = max(0, end - limit)
            shown = end - start
            return {"schema_version": SCHEMA, "task_id": task_id, "snapshot_id": snapshot,
                    "events": deepcopy(rows[start:end]), "offset": offset, "shown": shown, "total": len(rows),
                    "truncated": start > 0, "next_cursor": f"{snapshot}:{offset + shown}" if start else None}
