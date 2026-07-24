"""Shared normalization of legacy v1 ledger events into canonical inputs.

Both consumers of a v1 event dict live off this module: the offline legacy
importer (bulk, snapshot-verified) and the live shadow writer (per event,
flag-gated). They MUST derive identical content hashes and source-namespace
digests for the same event — a same-identity, different-content pair lands in
``source_conflicts`` as tampering, so any normalization drift between the two
callers would quarantine healthy rows. Change these functions only with both
callers' tests in hand.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from agent_chronicle.usage_truth import normalized_local_usage_session_id

from .types import SQLITE_INT64_MAX


EXPLICIT_NAMESPACE_SCHEME = "legacy-explicit-fingerprint-sha256-v1"

VALID_SESSION_KINDS = frozenset(
    {"root", "child", "internal", "continuation", "retry", "unknown"}
)
VALID_STATUSES = frozenset({"started", "checkpoint", "completed", "blocked", "reported", "aborted"})
VALID_OUTCOME_AXES = frozenset({"intent", "execution", "verification", "outcome", "finding"})


def bounded_text(value: object, *, default: str, limit: int) -> str:
    text = str(value).strip() if value is not None else ""
    return (text or default)[:limit]


def optional_text(value: object, *, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:limit]


def event_metadata(event: Mapping[str, Any]) -> Mapping[str, Any]:
    value = event.get("metadata")
    return value if isinstance(value, Mapping) else {}


def client_for(event: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
    return bounded_text(
        metadata.get("client") or event.get("client") or event.get("provider") or event.get("source"),
        default="legacy",
        limit=64,
    )


def explicit_namespace_digest(fingerprint: str) -> bytes:
    """Stable digest of an explicit source-namespace fingerprint.

    Both the importer and the live writer key resolved sources off this
    digest, which is what lets a post-cutover live write continue a session
    the import created.
    """

    return hashlib.sha256(
        b"legacy-explicit-namespace-v1\x00" + fingerprint.strip().encode("utf-8")
    ).digest()


def hash_safe(value: object) -> object:
    # canonical_hash rejects floats by design (their storage is never
    # canonical truth); raw legacy events may still carry them in free-form
    # metadata, so hashing input maps each float to tagged repr BYTES, which
    # is deterministic for IEEE doubles in CPython and unforgeable from
    # JSON-origin events (JSON cannot encode bytes, so no string can collide).
    if isinstance(value, float):
        return b"float:" + repr(value).encode("ascii")
    if isinstance(value, Mapping):
        return {key: hash_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [hash_safe(item) for item in value]
    return value


def timestamp_to_us(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number) or number < 0:
            return None
        # Legacy timestamps are normally seconds; already-microsecond values
        # are retained. Milliseconds are also accepted explicitly by scale.
        if number >= 10**14:
            if number > SQLITE_INT64_MAX:
                return None
            result = int(number)
        elif number >= 10**11:
            if number > SQLITE_INT64_MAX / 1_000:
                return None
            result = int(number * 1_000)
        else:
            if number > SQLITE_INT64_MAX / 1_000_000:
                return None
            result = int(number * 1_000_000)
        return result if result <= SQLITE_INT64_MAX else None
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        try:
            result = max(0, int(parsed.timestamp() * 1_000_000))
        except (OverflowError, OSError, ValueError):
            return None
        return result if result <= SQLITE_INT64_MAX else None
    return None


def semantic_kind(event: Mapping[str, Any], metadata: Mapping[str, Any]) -> str | None:
    event_type = str(event.get("event_type") or "").lower()
    semantic = str(metadata.get("sentinel_semantic_kind") or "").lower()
    if semantic not in {"task", "section"} and not (
        event_type.startswith("task_") or event_type.startswith("section_")
    ):
        return None
    if "blocked" in event_type:
        return "blocker"
    if "completed" in event_type:
        return "completion"
    if "checkpoint" in event_type:
        return "checkpoint"
    if semantic in {"task", "section"}:
        return semantic
    return "event"


def normalized_work_claim_fields(
    event: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> dict[str, Any] | None:
    """The semantic work-claim projection whose canonical hash is the fact's
    content hash. Returns None for events the canonical model does not
    represent as work claims."""

    event_kind = semantic_kind(event, metadata)
    if event_kind is None:
        return None
    event_type = str(event.get("event_type") or "legacy_event").lower()
    status_value = metadata.get("section_status") or metadata.get("task_status") or event.get("status")
    status = str(status_value).lower() if isinstance(status_value, str) else ""
    if not status:
        if "completed" in event_type:
            status = "completed"
        elif "blocked" in event_type:
            status = "blocked"
        elif "checkpoint" in event_type:
            status = "checkpoint"
        elif "started" in event_type:
            status = "started"
        else:
            status = "reported"
    if status not in VALID_STATUSES:
        status = "reported"
    outcome_value = str(metadata.get("outcome_axis") or "").lower()
    if outcome_value in VALID_OUTCOME_AXES:
        outcome_axis = outcome_value
    elif status in {"completed", "blocked", "aborted"}:
        outcome_axis = "outcome"
    elif status == "checkpoint":
        outcome_axis = "verification"
    elif event_kind in {"task", "section"}:
        outcome_axis = "execution"
    else:
        outcome_axis = "intent"

    return {
        "event_kind": event_kind,
        "status": status,
        "title": optional_text(metadata.get("section_title") or metadata.get("task_title") or event.get("title"), limit=512),
        "objective": optional_text(metadata.get("objective") or event.get("objective"), limit=4096),
        "summary": optional_text(metadata.get("summary") or event.get("summary"), limit=8192),
        "blocker": optional_text(metadata.get("blocker") or event.get("blocker"), limit=4096),
        "next_step": optional_text(metadata.get("next_step") or event.get("next_step"), limit=4096),
        "work_id": optional_text(metadata.get("work_id") or event.get("work_id"), limit=256),
        "section_id": optional_text(metadata.get("section_id") or event.get("section_id"), limit=256),
        "outcome_axis": outcome_axis,
        "supersedes_event_id": optional_text(
            metadata.get("supersedes_event_id") or event.get("supersedes_event_id"),
            limit=512,
        ),
    }


def work_claim_occurred_raw(event: Mapping[str, Any], metadata: Mapping[str, Any]) -> object:
    return event.get("created_at") or metadata.get("client_event_timestamp")


@dataclass(frozen=True)
class ObservedSessionFields:
    """Session identity/lineage fields one v1 event observes."""

    client_session_id: str
    parent_client_session_id: str | None
    session_kind: str
    started_at_us: int | None
    last_activity_at_us: int | None
    observation_order: int | None


def observed_session_fields(
    event: Mapping[str, Any],
    metadata: Mapping[str, Any],
    *,
    client: str,
) -> ObservedSessionFields | None:
    session_id = optional_text(
        metadata.get("client_session_id") or event.get("client_session_id"),
        limit=512,
    )
    if session_id is None:
        return None
    session_id = normalized_local_usage_session_id(client, session_id)
    parent_id = optional_text(
        metadata.get("parent_client_session_id") or event.get("parent_client_session_id"),
        limit=512,
    )
    if parent_id is not None:
        parent_id = normalized_local_usage_session_id(client, parent_id)
    raw_kind = optional_text(
        metadata.get("client_session_kind") or event.get("client_session_kind"),
        limit=32,
    )
    # A missing kind is only root-like when lineage is also absent.  An
    # explicit parent is positive evidence that the session cannot anchor an
    # independent Task, while ``unknown`` preserves what the source omitted.
    if raw_kind is None:
        kind = "unknown" if parent_id is not None else "root"
    else:
        normalized_kind = raw_kind.lower()
        kind = normalized_kind if normalized_kind in VALID_SESSION_KINDS else "unknown"
    started = timestamp_to_us(metadata.get("started_at") or event.get("started_at"))
    updated = timestamp_to_us(
        metadata.get("source_updated_at")
        or metadata.get("updated_at")
        or event.get("updated_at")
        or event.get("created_at")
    )
    if started is not None and updated is not None and updated < started:
        updated = started
    return ObservedSessionFields(
        client_session_id=session_id,
        parent_client_session_id=parent_id,
        session_kind=kind,
        started_at_us=started,
        last_activity_at_us=updated,
        observation_order=updated,
    )


EDGE_BASIS_EXPLICIT_PARENT = "legacy_explicit_parent_session_id"


def absorb_observed_session(
    *,
    incumbent_session_kind: str,
    incumbent_started_at_us: int | None,
    incumbent_last_activity_at_us: int | None,
    incumbent_parent: object | None,
    incoming_session_kind: str,
    incoming_started_at_us: int | None,
    incoming_last_activity_at_us: int | None,
    incoming_parent: object | None,
    incoming_wins_last_writes: bool,
) -> tuple[str, int | None, int | None, object | None]:
    """One absorb step: min started, max activity, gated last-wins fields.

    The min/max merges are commutative and apply regardless of ordering;
    kind and parent follow the newest observation (and a newer observation
    without a parent claim never clears the incumbent one). The caller owns
    the "newest" decision — the importer breaks order ties by line number,
    the live writer treats them as visible ties.
    """

    started = incumbent_started_at_us
    if incoming_started_at_us is not None and (
        started is None or incoming_started_at_us < started
    ):
        started = incoming_started_at_us
    last = incumbent_last_activity_at_us
    if incoming_last_activity_at_us is not None and (
        last is None or incoming_last_activity_at_us > last
    ):
        last = incoming_last_activity_at_us
    if incoming_wins_last_writes:
        kind = incoming_session_kind
        parent = incoming_parent if incoming_parent is not None else incumbent_parent
    else:
        kind = incumbent_session_kind
        parent = incumbent_parent
    return kind, started, last, parent


def session_edge_hash_material(
    *,
    child_namespace_digest: bytes,
    child_client_session_id: str,
    parent_namespace_digest: bytes,
    parent_client_session_id: str,
    relation_kind: str,
    basis: str = EDGE_BASIS_EXPLICIT_PARENT,
) -> dict[str, Any]:
    """The exact mapping both writers hash into ``session_edges.content_hash``."""

    return {
        "child": (child_namespace_digest, child_client_session_id),
        "parent": (parent_namespace_digest, parent_client_session_id),
        "relation": relation_kind,
        "basis": basis,
    }


def session_observation_hash_material(
    *,
    client_session_id: str,
    session_kind: str,
    started_at_us: int | None,
    last_activity_at_us: int | None,
    parent_client_session_id: str | None,
    parent_namespace_digest: bytes | None,
) -> dict[str, Any]:
    """The exact mapping both writers hash into ``sessions.observation_hash``."""

    return {
        "client_session_id": client_session_id,
        "session_kind": session_kind,
        "started_at_us": started_at_us,
        "last_activity_at_us": last_activity_at_us,
        "parent_client_session_id": parent_client_session_id,
        "parent_namespace_digest": parent_namespace_digest,
    }


__all__ = [
    "EDGE_BASIS_EXPLICIT_PARENT",
    "EXPLICIT_NAMESPACE_SCHEME",
    "ObservedSessionFields",
    "VALID_OUTCOME_AXES",
    "VALID_SESSION_KINDS",
    "VALID_STATUSES",
    "absorb_observed_session",
    "bounded_text",
    "client_for",
    "event_metadata",
    "explicit_namespace_digest",
    "hash_safe",
    "normalized_work_claim_fields",
    "observed_session_fields",
    "optional_text",
    "semantic_kind",
    "session_edge_hash_material",
    "session_observation_hash_material",
    "timestamp_to_us",
    "work_claim_occurred_raw",
]
