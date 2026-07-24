from __future__ import annotations

import hashlib
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Mapping


WORK_EVENT_SCHEMA_VERSION = "chronicle.work-event.v1"
WORK_EVENT_KINDS = {
    "client_context",
    "event",
    "machine_check",
    "note",
    "section",
    "task",
    "usage_debug",
}
WORK_EVENT_TRANSPORTS = {"cli", "http", "internal", "mcp", "orchestrator", "unknown"}
WORK_EVENT_STATUSES = {"blocked", "checkpoint", "completed", "failed", "passed", "started", "unknown"}

_SAFE_ID = re.compile(r"^[^\r\n\x00]{1,240}$")
_MAX_SUMMARY = 1_200
_MAX_FILES = 50


def _limited(value: Any, *, maximum: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:maximum]


def _identifier(value: Any) -> str | None:
    text = _limited(value, maximum=240)
    if text is None or _SAFE_ID.fullmatch(text) is None:
        return None
    return text


def _timestamp(value: Any) -> float:
    parsed = _optional_timestamp(value)
    return parsed if parsed is not None else time.time()


def _optional_timestamp(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _work_event_idempotency_key(*, transport: str, source: str, source_event_id: str) -> str:
    """Return a bounded, unambiguous identity for one source Work Event.

    Source ids may already be 240 characters, so hashing the length-delimited
    identity keeps the v1 metadata value under the established 240-character
    idempotency limit. Transport remains part of provenance: the same upstream
    id delivered independently over HTTP and CLI is not silently collapsed.
    """

    identity = "".join(
        (
            f"{len(transport)}:{transport}",
            f"{len(source)}:{source}",
            f"{len(source_event_id)}:{source_event_id}",
        )
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"work-event:{transport}:{digest}"


def _relative_files(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    result: list[str] = []
    for item in value[:_MAX_FILES]:
        text = _limited(item, maximum=240)
        if text is None:
            continue
        normalized = text.replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or normalized.startswith("~") or any(part in {"", ".", ".."} for part in path.parts):
            continue
        result.append(normalized)
    return tuple(result)


def _event_kind(event_type: str, semantic_kind: str | None) -> str:
    if semantic_kind == "client_context" or event_type == "client_context_attached":
        return "client_context"
    if semantic_kind == "section" or event_type.startswith("section_"):
        return "section"
    if semantic_kind == "evidence" or event_type == "machine_check":
        return "machine_check"
    if semantic_kind == "agent_usage_debug" or event_type == "agent_usage_debug_reported":
        return "usage_debug"
    if event_type in {"task_started", "task_completed", "task_blocked"}:
        return "task"
    if event_type == "note":
        return "note"
    return "event"


def _event_status(event_type: str, metadata: Mapping[str, Any]) -> str:
    for candidate in (
        metadata.get("section_status"),
        metadata.get("result"),
        event_type.removeprefix("section_") if event_type.startswith("section_") else None,
        event_type.removeprefix("task_") if event_type.startswith("task_") else None,
    ):
        normalized = str(candidate or "").strip().lower()
        if normalized in WORK_EVENT_STATUSES:
            return normalized
    return "unknown"


@dataclass(frozen=True)
class WorkEvent:
    """Transport-neutral semantic work record.

    The model intentionally contains only an allowlist of work meaning and join
    identifiers. It cannot carry prompt, transcript, response, thought, tool
    arguments, tool results, stdout, stderr, auth, or arbitrary metadata.
    Existing MCP tools, HTTP, CLI, and orchestrators may all normalize into this
    contract while retaining their transport as provenance.
    """

    event_kind: str
    source: str
    transport: str = "unknown"
    status: str = "unknown"
    occurred_at: float = field(default_factory=time.time)
    source_event_id: str | None = None
    run_id: str | None = None
    work_id: str | None = None
    section_id: str | None = None
    title: str | None = None
    objective: str | None = None
    summary: str | None = None
    blocker: str | None = None
    next_step: str | None = None
    client: str | None = None
    client_session_id: str | None = None
    client_transcript_id: str | None = None
    parent_client_session_id: str | None = None
    turn_id: str | None = None
    message_id: str | None = None
    request_id: str | None = None
    files: tuple[str, ...] = ()
    original_event_type: str = "event"

    def __post_init__(self) -> None:
        if self.event_kind not in WORK_EVENT_KINDS:
            raise ValueError(f"unsupported work event kind: {self.event_kind}")
        if self.transport not in WORK_EVENT_TRANSPORTS:
            raise ValueError(f"unsupported work event transport: {self.transport}")
        if self.status not in WORK_EVENT_STATUSES:
            raise ValueError(f"unsupported work event status: {self.status}")
        if not self.source.strip() or len(self.source) > 80:
            raise ValueError("source must contain 1-80 characters")
        object.__setattr__(self, "source", self.source.strip())
        object.__setattr__(self, "occurred_at", _timestamp(self.occurred_at))
        for name in (
            "source_event_id",
            "run_id",
            "work_id",
            "section_id",
            "client_session_id",
            "client_transcript_id",
            "parent_client_session_id",
            "turn_id",
            "message_id",
            "request_id",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name)))
        for name, maximum in (
            ("title", 240),
            ("objective", _MAX_SUMMARY),
            ("summary", _MAX_SUMMARY),
            ("blocker", _MAX_SUMMARY),
            ("next_step", _MAX_SUMMARY),
            ("client", 80),
            ("original_event_type", 80),
        ):
            object.__setattr__(self, name, _limited(getattr(self, name), maximum=maximum))
        object.__setattr__(self, "original_event_type", self.original_event_type or "event")
        object.__setattr__(self, "files", _relative_files(self.files))

    @classmethod
    def from_v1_event(cls, event: Mapping[str, Any], *, transport: str = "unknown") -> "WorkEvent":
        metadata_value = event.get("metadata")
        metadata: Mapping[str, Any] = metadata_value if isinstance(metadata_value, Mapping) else {}
        event_type = _limited(event.get("event_type"), maximum=80) or "event"
        semantic_kind = _limited(metadata.get("sentinel_semantic_kind"), maximum=80)
        client_event_timestamp = _optional_timestamp(metadata.get("client_event_timestamp"))
        return cls(
            event_kind=_event_kind(event_type, semantic_kind),
            source=_limited(event.get("source"), maximum=80) or "unknown",
            transport=transport if transport in WORK_EVENT_TRANSPORTS else "unknown",
            status=_event_status(event_type, metadata),
            # A v1 service owns created_at/event_id. When the compatibility
            # metadata contains the original source values, prefer those so a
            # server round-trip cannot erase source occurrence or identity.
            occurred_at=client_event_timestamp if client_event_timestamp is not None else _timestamp(event.get("created_at")),
            source_event_id=_identifier(metadata.get("source_event_id")) or _identifier(event.get("event_id")),
            run_id=_identifier(event.get("run_id")),
            work_id=_identifier(metadata.get("work_id")),
            section_id=_identifier(metadata.get("section_id")),
            title=_limited(metadata.get("section_title") or metadata.get("title"), maximum=240),
            objective=_limited(metadata.get("objective") or metadata.get("goal"), maximum=_MAX_SUMMARY),
            summary=_limited(metadata.get("summary"), maximum=_MAX_SUMMARY),
            blocker=_limited(metadata.get("blocker"), maximum=_MAX_SUMMARY),
            next_step=_limited(metadata.get("next_step"), maximum=_MAX_SUMMARY),
            client=_limited(metadata.get("client"), maximum=80),
            client_session_id=_identifier(metadata.get("client_session_id")),
            client_transcript_id=_identifier(metadata.get("client_transcript_id")),
            parent_client_session_id=_identifier(metadata.get("parent_client_session_id")),
            turn_id=_identifier(metadata.get("turn_id")),
            message_id=_identifier(metadata.get("message_id")),
            request_id=_identifier(metadata.get("request_id")),
            files=_relative_files(metadata.get("files")),
            original_event_type=event_type,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": WORK_EVENT_SCHEMA_VERSION,
            "event_kind": self.event_kind,
            "source": self.source,
            "transport": self.transport,
            "status": self.status,
            "occurred_at": self.occurred_at,
            "source_event_id": self.source_event_id,
            "run_id": self.run_id,
            "work_id": self.work_id,
            "section_id": self.section_id,
            "title": self.title,
            "objective": self.objective,
            "summary": self.summary,
            "blocker": self.blocker,
            "next_step": self.next_step,
            "client": self.client,
            "client_session_id": self.client_session_id,
            "client_transcript_id": self.client_transcript_id,
            "parent_client_session_id": self.parent_client_session_id,
            "turn_id": self.turn_id,
            "message_id": self.message_id,
            "request_id": self.request_id,
            "files": list(self.files),
            "original_event_type": self.original_event_type,
        }

    def to_v1_event(self) -> dict[str, Any]:
        metadata = {
            "work_event_schema_version": WORK_EVENT_SCHEMA_VERSION,
            "work_event_transport": self.transport,
            "sentinel_semantic_kind": self.event_kind,
            "status": self.status,
            "work_id": self.work_id,
            "section_id": self.section_id,
            "section_title": self.title,
            "objective": self.objective,
            "summary": self.summary,
            "blocker": self.blocker,
            "next_step": self.next_step,
            "client": self.client,
            "client_session_id": self.client_session_id,
            "client_transcript_id": self.client_transcript_id,
            "parent_client_session_id": self.parent_client_session_id,
            "turn_id": self.turn_id,
            "message_id": self.message_id,
            "request_id": self.request_id,
            "files": list(self.files),
            "client_event_timestamp": self.occurred_at,
            "source_event_id": self.source_event_id,
            "idempotency_key": (
                _work_event_idempotency_key(
                    transport=self.transport,
                    source=self.source,
                    source_event_id=self.source_event_id,
                )
                if self.source_event_id is not None
                else None
            ),
        }
        return {
            "source": self.source,
            "event_type": self.original_event_type,
            "run_id": self.run_id,
            # This is useful for direct compatibility conversion. The v1
            # service will replace it with its own receipt time while retaining
            # client_event_timestamp above as the source occurrence time.
            "created_at": self.occurred_at,
            "metadata": {key: value for key, value in metadata.items() if value is not None and value != []},
        }
