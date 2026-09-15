"""User-authored folder-anchored Work groupings ("worksets").

A workset is the local dashboard user's own overlay: "the sessions under this
folder are one piece of work." It is an append-only, server-validated grouping
keyed by a directory's cross-source ``project_identity`` (the same normalized
path hash a Claude Code session and a Codex session in the same repo already
share). It NEVER rewrites a session's automatic Task identity, its receipt, its
evidence tier, or its cost attribution — a workset is a human assertion with
``authoritative_for_check_result: False``, exactly like a finding disposition.
Membership is a live query by ``project_identity`` at read time, so a new
session in the folder joins automatically and no per-session claim is ever
minted.

The chain machinery mirrors :mod:`agentacct.finding_disposition`: each mutation
carries an optimistic ``expected_revision`` (a concurrent change is a conflict,
never a silent overwrite), an idempotency key, and a self-authenticating
operation digest. Only server-stamped, internally consistent chains replay.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


WORKSET_CONTRACT_KEY = "workset_contract"
WORKSET_CONTRACT_VERSION = "server_validated_v1"
WORKSET_EVENT_TYPE = "workset_action"
WORKSET_SOURCE = "agent-chronicle-workset"
WORKSET_AUTHORITY_SCOPE = "workset_membership_only"

WORKSET_ACTIONS = {"create", "rename", "redirect", "delete"}

_MAX_NAME = 200
_MAX_IDENTITY = 240
_MAX_ID = 120
_MAX_IDEMPOTENCY = 240
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_CONTROL = "\r\n\x00"


class WorksetError(ValueError):
    """Base error for rejected workset mutations."""


class WorksetNotFound(WorksetError):
    """The referenced workset does not exist (or was deleted)."""


class WorksetConflict(WorksetError):
    """The requested mutation conflicts with current append-only state."""


@dataclass(frozen=True)
class WorksetState:
    workset_id: str
    name: str
    project_identity: str
    revision: int = 0
    deleted: bool = False
    created_at: float | None = None
    updated_at: float | None = None
    event_id: str | None = None
    chain_valid: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "workset_id": self.workset_id,
            "name": self.name,
            "project_identity": self.project_identity,
            "revision": self.revision,
            "deleted": self.deleted,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "event_id": self.event_id,
            "authority_scope": WORKSET_AUTHORITY_SCOPE,
            "authoritative_for_check_result": False,
            "chain_valid": self.chain_valid,
        }


@dataclass(frozen=True)
class WorksetProjection:
    states: dict[str, WorksetState]
    invalid: frozenset[str]
    diagnostics: dict[str, Any]

    def active(self) -> list[WorksetState]:
        """Live (non-deleted, chain-valid) worksets, newest activity first."""

        rows = [
            state
            for workset_id, state in self.states.items()
            if not state.deleted and workset_id not in self.invalid
        ]
        return sorted(
            rows,
            key=lambda state: (state.updated_at or state.created_at or 0.0),
            reverse=True,
        )


def normalize_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > _MAX_NAME or any(char in text for char in _CONTROL):
        return None
    return text


def normalize_identity(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > _MAX_IDENTITY or any(char in text for char in _CONTROL):
        return None
    return text


def normalize_workset_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > _MAX_ID or any(char in text for char in _CONTROL):
        return None
    return text


def valid_idempotency_key(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= _MAX_IDEMPOTENCY
        and not any(char in value for char in _CONTROL)
    )


def workset_operation_digest(
    *,
    workset_id: str,
    action: str,
    name: str | None,
    project_identity: str | None,
    expected_revision: int,
) -> str:
    payload = {
        "action": action,
        "actor": "dashboard-user",
        "authority_scope": WORKSET_AUTHORITY_SCOPE,
        "contract": WORKSET_CONTRACT_VERSION,
        "expected_revision": expected_revision,
        "name": name,
        "project_identity": project_identity,
        "workset_id": workset_id,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def workset_transition(
    prior: WorksetState | None,
    *,
    action: str,
    name: str | None,
    project_identity: str | None,
) -> tuple[str, str, bool] | None:
    """Return the (name, project_identity, deleted) after ``action``, or None.

    ``create`` only applies to an absent id; every other action requires an
    existing, non-deleted workset. Content required by the action must be
    present (create/rename need a name, create/redirect need an identity).
    """

    if action == "create":
        if prior is not None:
            return None
        if name is None or project_identity is None:
            return None
        return (name, project_identity, False)
    if prior is None or prior.deleted:
        return None
    if action == "rename":
        if name is None:
            return None
        return (name, prior.project_identity, False)
    if action == "redirect":
        if project_identity is None:
            return None
        return (prior.name, project_identity, False)
    if action == "delete":
        return (prior.name, prior.project_identity, True)
    return None


def is_trusted_workset_event(event: Mapping[str, Any]) -> bool:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), Mapping) else {}
    return bool(
        event.get("event_type") == WORKSET_EVENT_TYPE
        and event.get("source") == WORKSET_SOURCE
        and metadata.get("sentinel_semantic_kind") == WORKSET_EVENT_TYPE
        and metadata.get(WORKSET_CONTRACT_KEY) == WORKSET_CONTRACT_VERSION
        and metadata.get("authority_scope") == WORKSET_AUTHORITY_SCOPE
        and metadata.get("authoritative_for_check_result") is False
        and metadata.get("actor") == "dashboard-user"
    )


def mark_trusted_workset(event: dict[str, Any]) -> dict[str, Any]:
    """Stamp the server-trusted provenance a workset event must carry.

    A raw MCP/HTTP caller cannot forge one: the reducer only replays events
    that were stamped here (source + contract + actor + false authority), so a
    manual grouping can never masquerade as attributed or verified evidence.
    """

    event["source"] = WORKSET_SOURCE
    event["event_type"] = WORKSET_EVENT_TYPE
    metadata = event.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        event["metadata"] = metadata
    metadata["sentinel_semantic_kind"] = WORKSET_EVENT_TYPE
    metadata[WORKSET_CONTRACT_KEY] = WORKSET_CONTRACT_VERSION
    metadata["authority_scope"] = WORKSET_AUTHORITY_SCOPE
    metadata["authoritative_for_check_result"] = False
    metadata["actor"] = "dashboard-user"
    return event


def _safe_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _safe_time(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def reduce_worksets(events: Iterable[Mapping[str, Any]]) -> WorksetProjection:
    """Replay only server-trusted, internally consistent workset chains."""

    states: dict[str, WorksetState] = {}
    invalid: set[str] = set()
    accepted = 0
    rejected: Counter[str] = Counter()
    seen_idempotency: dict[str, tuple[str, str]] = {}

    for event in events:
        if not is_trusted_workset_event(event):
            continue
        metadata = event.get("metadata") if isinstance(event.get("metadata"), Mapping) else {}
        workset_id = normalize_workset_id(metadata.get("workset_id"))
        action = str(metadata.get("action") or "").strip()
        name = normalize_name(metadata.get("name")) if metadata.get("name") is not None else None
        project_identity = (
            normalize_identity(metadata.get("project_identity"))
            if metadata.get("project_identity") is not None
            else None
        )
        expected_revision = _safe_int(metadata.get("expected_revision"))
        revision = _safe_int(metadata.get("revision"))
        idempotency_key = str(metadata.get("idempotency_key") or "").strip()
        operation_digest = str(metadata.get("operation_digest") or "").strip().lower()

        if workset_id is None or action not in WORKSET_ACTIONS:
            rejected["invalid_fields"] += 1
            if workset_id is not None:
                invalid.add(workset_id)
            continue
        # A field that was SUPPLIED but failed normalization is corruption, not
        # an omission — reject rather than silently treat it as absent.
        if metadata.get("name") is not None and name is None:
            rejected["invalid_name"] += 1
            invalid.add(workset_id)
            continue
        if metadata.get("project_identity") is not None and project_identity is None:
            rejected["invalid_identity"] += 1
            invalid.add(workset_id)
            continue
        if expected_revision is None or expected_revision < 0 or revision != expected_revision + 1:
            rejected["invalid_revision"] += 1
            invalid.add(workset_id)
            continue
        if not valid_idempotency_key(idempotency_key):
            rejected["invalid_idempotency_key"] += 1
            invalid.add(workset_id)
            continue
        expected_digest = workset_operation_digest(
            workset_id=workset_id,
            action=action,
            name=name,
            project_identity=project_identity,
            expected_revision=expected_revision,
        )
        if not _HEX_64.fullmatch(operation_digest) or operation_digest != expected_digest:
            rejected["operation_digest_mismatch"] += 1
            invalid.add(workset_id)
            continue

        prior_idempotency = seen_idempotency.get(idempotency_key)
        if prior_idempotency is not None:
            prior_operation, prior_workset = prior_idempotency
            if prior_operation != operation_digest or prior_workset != workset_id:
                rejected["idempotency_conflict"] += 1
                invalid.add(workset_id)
                invalid.add(prior_workset)
            else:
                rejected["idempotent_duplicate"] += 1
            continue

        current = states.get(workset_id)
        current_revision = current.revision if current is not None else 0
        if current_revision != expected_revision:
            rejected["stale_revision"] += 1
            invalid.add(workset_id)
            continue
        transition = workset_transition(
            current, action=action, name=name, project_identity=project_identity
        )
        if transition is None:
            rejected["invalid_transition"] += 1
            invalid.add(workset_id)
            continue

        next_name, next_identity, next_deleted = transition
        seen_idempotency[idempotency_key] = (operation_digest, workset_id)
        states[workset_id] = WorksetState(
            workset_id=workset_id,
            name=next_name,
            project_identity=next_identity,
            revision=revision,
            deleted=next_deleted,
            created_at=current.created_at if current is not None else _safe_time(event.get("created_at")),
            updated_at=_safe_time(event.get("created_at")),
            event_id=str(event.get("event_id") or "") or None,
        )
        accepted += 1

    for workset_id in invalid:
        state = states.get(workset_id)
        if state is not None:
            states[workset_id] = WorksetState(
                workset_id=state.workset_id,
                name=state.name,
                project_identity=state.project_identity,
                revision=state.revision,
                deleted=state.deleted,
                created_at=state.created_at,
                updated_at=state.updated_at,
                event_id=state.event_id,
                chain_valid=False,
            )

    return WorksetProjection(
        states=states,
        invalid=frozenset(invalid),
        diagnostics={
            "accepted": accepted,
            "rejected": sum(rejected.values()),
            "rejected_by_reason": dict(sorted(rejected.items())),
        },
    )


# --- read-side projection over the session rollup -----------------------------
#
# Membership is a LIVE query: given a folder identity, gather the current
# root-session rollup entries that share it. Nothing here writes; everything is
# a labeled sum of independently-attributed parts, never a re-graded verdict.
# Only ROOT sessions are members — a subagent/continuation folds under its own
# root, exactly as it does in the Sessions tab, so "6 sessions" stays 6.

_NON_ROOT_KINDS = {"child", "internal"}


def _rollup_entries(session_rollup: Any) -> list[Mapping[str, Any]]:
    if isinstance(session_rollup, Mapping):
        sessions = session_rollup.get("sessions")
    else:
        sessions = session_rollup
    return [row for row in sessions if isinstance(row, Mapping)] if isinstance(sessions, list) else []


def _is_root(entry: Mapping[str, Any]) -> bool:
    return str(entry.get("session_kind") or "").strip() not in _NON_ROOT_KINDS


def _entry_identity(entry: Mapping[str, Any]) -> str | None:
    if str(entry.get("project_identity_state") or "").strip() == "conflicting":
        return None
    identity = entry.get("project_identity")
    return identity if isinstance(identity, str) and identity.strip() else None


def _source_label(entry: Mapping[str, Any]) -> str:
    return str(entry.get("client") or "").strip() or "unknown"


def _entry_tokens(entry: Mapping[str, Any]) -> int:
    usage = entry.get("usage") if isinstance(entry.get("usage"), Mapping) else {}
    try:
        return int(usage.get("total_tokens") or 0)
    except (TypeError, ValueError):
        return 0


def workset_candidates(session_rollup: Any) -> list[dict[str, Any]]:
    """Group visible root sessions by folder identity for the "point at a folder" picker.

    Only exposes the friendly leaf label + the pseudonymous identity hash the
    daemon already computes — never a raw absolute path. Sessions whose folder
    is ``conflicting`` (they wandered directories mid-run) have no single home
    and are omitted here; they are reported separately as an honest gap.
    """

    groups: dict[str, dict[str, Any]] = {}
    for entry in _rollup_entries(session_rollup):
        if not _is_root(entry):
            continue
        identity = _entry_identity(entry)
        if identity is None:
            continue
        bucket = groups.setdefault(
            identity,
            {
                "project_identity": identity,
                "label": str(entry.get("project") or "").strip() or "project",
                "session_count": 0,
                "sources": set(),
                "first_activity_at": None,
                "last_activity_at": None,
            },
        )
        bucket["session_count"] += 1
        bucket["sources"].add(_source_label(entry))
        first = _safe_time(entry.get("first_activity_at"))
        last = _safe_time(entry.get("last_activity_at"))
        if first is not None:
            bucket["first_activity_at"] = (
                first if bucket["first_activity_at"] is None else min(bucket["first_activity_at"], first)
            )
        if last is not None:
            bucket["last_activity_at"] = (
                last if bucket["last_activity_at"] is None else max(bucket["last_activity_at"], last)
            )
    rows = []
    for bucket in groups.values():
        bucket["sources"] = sorted(bucket["sources"])
        rows.append(bucket)
    rows.sort(key=lambda row: (row["last_activity_at"] or 0.0), reverse=True)
    return rows


def workset_member_entries(session_rollup: Any, project_identity: str) -> list[Mapping[str, Any]]:
    """The current ROOT sessions whose folder identity matches (live membership)."""

    return [
        entry
        for entry in _rollup_entries(session_rollup)
        if _is_root(entry) and _entry_identity(entry) == project_identity
    ]


def workset_session_lane(entry: Mapping[str, Any]) -> dict[str, Any]:
    """One member session shaped as a timeline lane (a bar on the shared axis).

    Carries only what the folder overview needs; drilling into a session's own
    event history stays in the Sessions tab. Cost is this session's own figure,
    verbatim from its rollup entry — never re-graded here.
    """

    usage = entry.get("usage") if isinstance(entry.get("usage"), Mapping) else {}
    cost = usage.get("estimated_cost_usd")
    return {
        "session_key": entry.get("session_key"),
        "client": entry.get("client"),
        "client_session_id": entry.get("client_session_id"),
        "title": (str(entry.get("client_session_title")).strip() or None)
        if isinstance(entry.get("client_session_title"), str)
        else None,
        "session_kind": entry.get("session_kind"),
        "first_activity_at": _safe_time(entry.get("first_activity_at")),
        "last_activity_at": _safe_time(entry.get("last_activity_at")),
        "duration_seconds": entry.get("duration_seconds"),
        "total_tokens": _entry_tokens(entry),
        "estimated_cost_usd": float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
        "cost_confidence": usage.get("cost_confidence") if isinstance(usage.get("cost_confidence"), str) else None,
    }


def summarize_members(members: list[Mapping[str, Any]]) -> dict[str, Any]:
    """A labeled SUM of the member sessions — never a combined verdict.

    Cost is the sum of the members that are priced, with the unpriced count kept
    visible so the figure is honestly a partial sum when some sessions carry no
    imported cost. No session's evidence tier is read or rolled up here.
    """

    sources: Counter[str] = Counter()
    first: float | None = None
    last: float | None = None
    total_tokens = 0
    priced_cost = 0.0
    priced_sessions = 0
    unpriced_sessions = 0
    cost_confidences: set[str] = set()

    for entry in members:
        sources[_source_label(entry)] += 1
        ef = _safe_time(entry.get("first_activity_at"))
        el = _safe_time(entry.get("last_activity_at"))
        if ef is not None:
            first = ef if first is None else min(first, ef)
        if el is not None:
            last = el if last is None else max(last, el)
        usage = entry.get("usage") if isinstance(entry.get("usage"), Mapping) else {}
        try:
            total_tokens += int(usage.get("total_tokens") or 0)
        except (TypeError, ValueError):
            pass
        cost = usage.get("estimated_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            priced_cost += float(cost)
            priced_sessions += 1
        else:
            unpriced_sessions += 1
        confidence = usage.get("cost_confidence")
        if isinstance(confidence, str) and confidence.strip():
            cost_confidences.add(confidence.strip())

    cost_confidence = (
        next(iter(cost_confidences)) if len(cost_confidences) == 1 else "mixed" if cost_confidences else None
    )
    return {
        "session_count": len(members),
        "sources": [{"client": client, "session_count": count} for client, count in sorted(sources.items())],
        "first_activity_at": first,
        "last_activity_at": last,
        "total_tokens": total_tokens,
        # A partial sum whenever ``unpriced_sessions`` > 0. The client must not
        # render this as a complete, billed total in that case.
        "estimated_cost_usd": priced_cost if priced_sessions else None,
        "cost_complete": priced_sessions > 0 and unpriced_sessions == 0,
        "priced_sessions": priced_sessions,
        "unpriced_sessions": unpriced_sessions,
        "cost_confidence": cost_confidence,
        "cost_basis": "sum_of_independent_receipts",
    }


__all__ = [
    "WORKSET_ACTIONS",
    "WORKSET_AUTHORITY_SCOPE",
    "WORKSET_CONTRACT_KEY",
    "WORKSET_CONTRACT_VERSION",
    "WORKSET_EVENT_TYPE",
    "WORKSET_SOURCE",
    "WorksetConflict",
    "WorksetError",
    "WorksetNotFound",
    "WorksetProjection",
    "WorksetState",
    "is_trusted_workset_event",
    "mark_trusted_workset",
    "normalize_identity",
    "normalize_name",
    "normalize_workset_id",
    "reduce_worksets",
    "summarize_members",
    "valid_idempotency_key",
    "workset_candidates",
    "workset_member_entries",
    "workset_operation_digest",
    "workset_session_lane",
    "workset_transition",
]
