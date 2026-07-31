"""Append-only attention state for exact agent finding episodes.

Finding dispositions never rewrite machine evidence.  They only record whether
the local dashboard user still wants a current failed/error episode highlighted.
The original check result stays failed/error until a later same-check pass
supersedes it in the normal outcome reducer.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


FINDING_DISPOSITION_CONTRACT_KEY = "finding_disposition_contract"
FINDING_DISPOSITION_CONTRACT_VERSION = "server_validated_v1"
FINDING_DISPOSITION_EVENT_TYPE = "finding_disposition"
FINDING_DISPOSITION_SOURCE = "agent-chronicle-dashboard"
FINDING_DISPOSITION_AUTHORITY_SCOPE = "finding_attention_only"
FINDING_TARGET_SCHEMA = "agent-chronicle.finding-target.v1"

FINDING_ACTIONS = {"mark_reviewed", "resolve", "reopen", "reinstate"}
FINDING_STATES = {"open", "reviewed", "resolved"}
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")


class FindingDispositionError(ValueError):
    """Base error for rejected finding attention mutations."""


class FindingDispositionNotFound(FindingDispositionError):
    """The exact current failure episode could not be proven."""


class FindingDispositionConflict(FindingDispositionError):
    """The requested mutation conflicts with current append-only state."""


@dataclass(frozen=True)
class FindingDispositionState:
    state: str = "open"
    revision: int = 0
    note: str | None = None
    action: str | None = None
    event_id: str | None = None
    updated_at: float | None = None
    chain_valid: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "revision": self.revision,
            "note": self.note,
            "action": self.action,
            "event_id": self.event_id,
            "updated_at": self.updated_at,
            "authority_scope": FINDING_DISPOSITION_AUTHORITY_SCOPE,
            "authoritative_for_check_result": False,
            "chain_valid": self.chain_valid,
        }


@dataclass(frozen=True)
class FindingDispositionProjection:
    states: dict[str, FindingDispositionState]
    invalid_targets: frozenset[str]
    diagnostics: dict[str, Any]


def canonical_event_digest(event: Mapping[str, Any]) -> str:
    """Digest one raw/projected event without trusting a caller-supplied id."""

    explicit = str(event.get("event_digest") or "").strip().lower()
    if _HEX_64.fullmatch(explicit):
        return explicit
    return hashlib.sha256(
        json.dumps(
            dict(event),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def finding_target_digest(event: Mapping[str, Any]) -> str | None:
    """Source/namespace/content-scoped identity for one exact failure episode."""

    event_id = str(event.get("event_id") or "").strip()
    if not event_id:
        return None
    material = {
        "client": str(event.get("client") or ""),
        "event_digest": canonical_event_digest(event),
        "event_id": event_id,
        "namespace": str(
            event.get("namespace_fingerprint")
            or event.get("session_namespace_fingerprint")
            or ""
        ),
        "project_identity": str(event.get("project_identity") or ""),
        "schema": FINDING_TARGET_SCHEMA,
        "source": str(event.get("source") or ""),
        "source_type": str(event.get("source_type") or ""),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def finding_episode_key(event: Mapping[str, Any]) -> str | None:
    return finding_target_digest(event)


def finding_operation_digest(
    *,
    target_event_id: str,
    target_event_digest: str,
    target_finding_digest: str,
    target_check_key_digest: str,
    action: str,
    expected_revision: int,
    note: str | None,
) -> str:
    payload = {
        "action": action,
        "actor": "dashboard-user",
        "authority_scope": FINDING_DISPOSITION_AUTHORITY_SCOPE,
        "contract": FINDING_DISPOSITION_CONTRACT_VERSION,
        "expected_revision": expected_revision,
        "note": note,
        "target_event_digest": target_event_digest,
        "target_event_id": target_event_id,
        "target_finding_digest": target_finding_digest,
        "target_check_key_digest": target_check_key_digest,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def disposition_transition(prior_state: str, action: str) -> str | None:
    if action == "mark_reviewed" and prior_state == "open":
        return "reviewed"
    if action == "resolve" and prior_state in {"open", "reviewed"}:
        return "resolved"
    if action == "reopen" and prior_state in {"reviewed", "resolved"}:
        return "open"
    if action == "reinstate" and prior_state == "open":
        # Reinstate is the dispute path for an automatically superseded finding:
        # the failure sits at disposition_state ``open`` with no attention (a
        # later same-scope pass demoted it). The state stays ``open`` but the
        # bumped revision makes the user's explicit choice win over the automatic
        # supersession, so the finding returns to Needs attention. It is distinct
        # from ``reopen`` (which pulls a reviewed/resolved finding back) so the
        # reopen contract and its state-machine tests are untouched.
        return "open"
    return None


def is_trusted_finding_disposition_event(event: Mapping[str, Any]) -> bool:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), Mapping) else {}
    return bool(
        event.get("event_type") == FINDING_DISPOSITION_EVENT_TYPE
        and event.get("source") == FINDING_DISPOSITION_SOURCE
        and metadata.get("sentinel_semantic_kind") == FINDING_DISPOSITION_EVENT_TYPE
        and metadata.get(FINDING_DISPOSITION_CONTRACT_KEY)
        == FINDING_DISPOSITION_CONTRACT_VERSION
        and metadata.get("authority_scope") == FINDING_DISPOSITION_AUTHORITY_SCOPE
        and metadata.get("authoritative_for_check_result") is False
        and metadata.get("actor") == "dashboard-user"
    )


def _safe_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _safe_time(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _bounded_text(value: str, *, maximum: int, allow_empty: bool = False) -> bool:
    return bool((allow_empty or value) and len(value) <= maximum and not any(char in value for char in "\r\n\x00"))


def reduce_finding_dispositions(
    events: Iterable[Mapping[str, Any]],
) -> FindingDispositionProjection:
    """Replay only server-trusted, internally consistent disposition chains."""

    states: dict[str, FindingDispositionState] = {}
    accepted = 0
    rejected: Counter[str] = Counter()
    seen_idempotency: dict[str, tuple[str, str]] = {}
    invalid_targets: set[str] = set()

    for event in events:
        if not is_trusted_finding_disposition_event(event):
            continue
        metadata = event.get("metadata") if isinstance(event.get("metadata"), Mapping) else {}
        target_event_id = str(metadata.get("target_failure_event_id") or "").strip()
        target_event_digest = str(metadata.get("target_event_digest") or "").strip().lower()
        target_finding_digest = str(metadata.get("target_finding_digest") or "").strip().lower()
        target_check_key_digest = str(metadata.get("target_check_key_digest") or "").strip().lower()
        action = str(metadata.get("action") or "").strip()
        prior_state = str(metadata.get("prior_state") or "").strip()
        next_state = str(metadata.get("next_state") or "").strip()
        expected_revision = _safe_int(metadata.get("expected_revision"))
        revision = _safe_int(metadata.get("revision"))
        note_value = metadata.get("note")
        note = str(note_value).strip() if isinstance(note_value, str) and note_value.strip() else None
        idempotency_key = str(metadata.get("idempotency_key") or "").strip()
        operation_digest = str(metadata.get("operation_digest") or "").strip().lower()

        reconstructed_target_digest = finding_target_digest(
            {
                "event_id": target_event_id,
                "event_digest": target_event_digest,
                "source_type": metadata.get("target_source_type"),
                "source": metadata.get("target_source"),
                "client": metadata.get("target_client"),
                "namespace_fingerprint": metadata.get("target_namespace_fingerprint"),
                "project_identity": metadata.get("target_project_identity"),
            }
        )

        if (
            not target_event_id
            or not _HEX_64.fullmatch(target_event_digest)
            or not _HEX_64.fullmatch(target_finding_digest)
            or not _HEX_64.fullmatch(target_check_key_digest)
        ):
            rejected["invalid_target"] += 1
            if _HEX_64.fullmatch(target_finding_digest):
                invalid_targets.add(target_finding_digest)
            if reconstructed_target_digest is not None:
                invalid_targets.add(reconstructed_target_digest)
            continue
        if reconstructed_target_digest != target_finding_digest:
            rejected["target_digest_mismatch"] += 1
            invalid_targets.add(target_finding_digest)
            if reconstructed_target_digest is not None:
                invalid_targets.add(reconstructed_target_digest)
            continue
        if action not in FINDING_ACTIONS or prior_state not in FINDING_STATES or next_state not in FINDING_STATES:
            rejected["invalid_transition_fields"] += 1
            invalid_targets.add(target_finding_digest)
            continue
        if expected_revision is None or expected_revision < 0 or revision != expected_revision + 1:
            rejected["invalid_revision"] += 1
            invalid_targets.add(target_finding_digest)
            continue
        if action == "resolve" and not note:
            rejected["resolution_note_missing"] += 1
            invalid_targets.add(target_finding_digest)
            continue
        if note is not None and not _bounded_text(note, maximum=1200):
            rejected["invalid_note"] += 1
            invalid_targets.add(target_finding_digest)
            continue
        expected_digest = finding_operation_digest(
            target_event_id=target_event_id,
            target_event_digest=target_event_digest,
            target_finding_digest=target_finding_digest,
            target_check_key_digest=target_check_key_digest,
            action=action,
            expected_revision=expected_revision,
            note=note,
        )
        if not _HEX_64.fullmatch(operation_digest) or operation_digest != expected_digest:
            rejected["operation_digest_mismatch"] += 1
            invalid_targets.add(target_finding_digest)
            continue
        if not _bounded_text(idempotency_key, maximum=240):
            rejected["idempotency_key_missing"] += 1
            invalid_targets.add(target_finding_digest)
            continue
        prior_idempotency = seen_idempotency.get(idempotency_key)
        if prior_idempotency is not None:
            prior_operation, prior_target = prior_idempotency
            if prior_operation != operation_digest or prior_target != target_finding_digest:
                rejected["idempotency_conflict"] += 1
                invalid_targets.add(target_finding_digest)
                invalid_targets.add(prior_target)
            else:
                rejected["idempotent_duplicate"] += 1
            continue

        key = target_finding_digest
        current = states.get(key, FindingDispositionState())
        expected_next = disposition_transition(current.state, action)
        if (
            current.revision != expected_revision
            or current.state != prior_state
            or expected_next is None
            or expected_next != next_state
        ):
            rejected["stale_or_invalid_transition"] += 1
            invalid_targets.add(target_finding_digest)
            continue
        seen_idempotency[idempotency_key] = (operation_digest, target_finding_digest)
        states[key] = FindingDispositionState(
            state=next_state,
            revision=revision,
            note=note,
            action=action,
            event_id=str(event.get("event_id") or "") or None,
            updated_at=_safe_time(event.get("created_at")),
        )
        accepted += 1

    return FindingDispositionProjection(
        states=states,
        invalid_targets=frozenset(invalid_targets),
        diagnostics={
            "accepted": accepted,
            "rejected": sum(rejected.values()),
            "rejected_by_reason": dict(sorted(rejected.items())),
        },
    )


def disposition_for_event(
    event: Mapping[str, Any],
    projection: FindingDispositionProjection,
) -> FindingDispositionState:
    key = finding_episode_key(event)
    if key is None:
        return FindingDispositionState()
    if key in projection.invalid_targets:
        return FindingDispositionState(chain_valid=False)
    return projection.states.get(key, FindingDispositionState())


__all__ = [
    "FINDING_ACTIONS",
    "FINDING_DISPOSITION_AUTHORITY_SCOPE",
    "FINDING_DISPOSITION_CONTRACT_KEY",
    "FINDING_DISPOSITION_CONTRACT_VERSION",
    "FINDING_DISPOSITION_EVENT_TYPE",
    "FINDING_DISPOSITION_SOURCE",
    "FINDING_STATES",
    "FindingDispositionConflict",
    "FindingDispositionError",
    "FindingDispositionNotFound",
    "FindingDispositionProjection",
    "FindingDispositionState",
    "canonical_event_digest",
    "disposition_for_event",
    "disposition_transition",
    "finding_episode_key",
    "finding_operation_digest",
    "finding_target_digest",
    "is_trusted_finding_disposition_event",
    "reduce_finding_dispositions",
]
