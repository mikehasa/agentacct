"""Canonical product-session observations derived from mechanical evidence.

The Evidence v2 store preserves every source record.  This module builds a
small, body-free read model for the Work product: one observation per
``(client, client_session_id)``.  It never invents usage, semantic work, or an
outcome.  Those dimensions remain owned by local usage import, MCP work events,
and machine-check evidence respectively.
"""

from __future__ import annotations

import json
from hashlib import sha256
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Iterable, Mapping


SESSION_OBSERVATION_SCHEMA_VERSION = "agent-chronicle.session-observation.v1"
_MAX_ID_LENGTH = 512
_MAX_PROVENANCE_VALUES = 50
DEFAULT_MECHANICAL_CONFLICT_GROUP_LIMIT = 100
DEFAULT_MECHANICAL_CONFLICT_ROW_LIMIT = 1_000


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _plain_envelope(value: Any) -> dict[str, Any]:
    arrival_sequence = getattr(value, "first_receipt_sequence", None)
    value = getattr(value, "envelope", value)
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    result = dict(value) if isinstance(value, Mapping) else {}
    if isinstance(arrival_sequence, int) and not isinstance(arrival_sequence, bool):
        result["_arrival_sequence"] = arrival_sequence
    return result


def _text(value: Any, *, maximum: int = _MAX_ID_LENGTH) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    return normalized if 0 < len(normalized) <= maximum else ""


def _timestamp(value: Any) -> float | None:
    if isinstance(value, datetime):
        parsed = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed if parsed > 0 else None
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
        parsed = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        result = parsed.astimezone(UTC).timestamp()
    except (OverflowError, OSError, ValueError):
        return None
    return result if result > 0 else None


def _ordered_unique(values: Iterable[str], *, limit: int = _MAX_PROVENANCE_VALUES) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
        if len(result) >= limit:
            break
    return tuple(result)


def _increment(diagnostics: dict[str, int] | None, key: str, amount: int = 1) -> None:
    if diagnostics is not None:
        diagnostics[key] = int(diagnostics.get(key) or 0) + amount


def _retry_material(envelope: Any) -> str | None:
    """Canonical material for a timestamp-only missing-host-time retry."""

    row = _plain_envelope(envelope)
    payload = _mapping(row.get("payload"))
    attributes = _mapping(payload.get("attributes"))
    completeness = _mapping(row.get("completeness"))
    note_codes = completeness.get("note_codes")
    if attributes.get("time_basis") != "capture_observed":
        return None
    if not isinstance(note_codes, (list, tuple)) or "host_timestamp" not in note_codes:
        return None
    for key in ("event_timestamp", "observed_at", "integrity_hash", "evidence_id", "_arrival_sequence"):
        row.pop(key, None)
    try:
        return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError):
        return None


def select_session_projection_envelopes(
    records: Iterable[Any],
    *,
    complete_conflict_keys: set[str] | None = None,
    diagnostics: dict[str, int] | None = None,
) -> list[Any]:
    """Drop material conflict groups; collapse safe timestamp-only retries.

    Evidence v2 must keep every conflict version.  The product read model is
    stricter: an identity conflict is omitted entirely, while a retry caused
    solely by an unavailable host timestamp is one observation, ordered by
    its earliest local capture time.
    """

    grouped: dict[str, list[tuple[Any, Any, bool]]] = {}
    for record in records:
        _increment(diagnostics, "input_records")
        envelope = getattr(record, "envelope", record)
        plain = _plain_envelope(envelope)
        key = _text(plain.get("idempotency_key"))
        if not key:
            _increment(diagnostics, "records_without_idempotency_key")
            continue
        grouped.setdefault(key, []).append((record, envelope, bool(getattr(record, "is_conflict", False))))

    selected: list[Any] = []
    for key, group in grouped.items():
        if not any(is_conflict for _record, _envelope, is_conflict in group):
            selected.extend(record for record, _envelope, _is_conflict in group)
            continue
        retry_materials = {_retry_material(envelope) for _record, envelope, _is_conflict in group}
        if (
            key not in (complete_conflict_keys or set())
            or len(group) < 2
            or None in retry_materials
            or len(retry_materials) != 1
        ):
            _increment(diagnostics, "material_conflict_groups_dropped")
            continue
        _increment(diagnostics, "timestamp_retry_groups_collapsed")
        selected.append(
            min(
                (record for record, _envelope, _is_conflict in group),
                key=lambda record: _timestamp(_plain_envelope(record).get("observed_at")) or float("inf"),
            )
        )
    if diagnostics is not None:
        diagnostics["selected_envelopes"] = len(selected)
    return selected


def expand_complete_conflict_groups(
    records: Iterable[Any],
    *,
    load_group: Callable[[str, int], list[Any]],
    group_limit: int = DEFAULT_MECHANICAL_CONFLICT_GROUP_LIMIT,
    row_limit: int = DEFAULT_MECHANICAL_CONFLICT_ROW_LIMIT,
    diagnostics: dict[str, int] | None = None,
) -> tuple[list[Any], set[str]]:
    """Boundedly replace partial conflict rows with provably complete groups.

    Dashboard projection and finding mutation must apply the same conflict
    contract. A group that fills a query/global budget is incomplete and is
    omitted rather than partially interpreted.
    """

    rows = list(records)
    conflict_keys = list(
        dict.fromkeys(
            _text(_plain_envelope(getattr(record, "envelope", record)).get("idempotency_key"))
            for record in rows
            if bool(getattr(record, "is_conflict", False))
        )
    )
    conflict_keys = [key for key in conflict_keys if key]
    if diagnostics is not None:
        diagnostics["conflict_groups_discovered"] = len(conflict_keys)
    if not conflict_keys:
        return rows, set()

    conflict_key_set = set(conflict_keys)
    retained = [
        record
        for record in rows
        if _text(
            _plain_envelope(getattr(record, "envelope", record)).get("idempotency_key")
        )
        not in conflict_key_set
    ]
    expanded: list[Any] = []
    complete_conflict_keys: set[str] = set()
    remaining_rows = max(0, int(row_limit))
    queried_groups = 0
    for idempotency_key in conflict_keys[: max(0, int(group_limit))]:
        if remaining_rows <= 0:
            break
        query_limit = min(10_000, remaining_rows)
        group = load_group(idempotency_key, query_limit)
        queried_groups += 1
        remaining_rows -= len(group)
        if 1 < len(group) < query_limit:
            complete_conflict_keys.add(idempotency_key)
            expanded.extend(group)
    if diagnostics is not None:
        diagnostics["conflict_groups_expanded"] = len(complete_conflict_keys)
        diagnostics["conflict_groups_incomplete"] = len(
            conflict_key_set - complete_conflict_keys
        )
        diagnostics["conflict_groups_queried"] = queried_groups
        diagnostics["conflict_rows_read"] = max(0, int(row_limit)) - remaining_rows
        diagnostics["conflict_groups_skipped_by_budget"] = max(
            0,
            len(conflict_keys) - queried_groups,
        )
    return [*retained, *expanded], complete_conflict_keys


@dataclass(frozen=True)
class SessionObservation:
    client: str
    client_session_id: str
    first_activity_at: float | None
    last_activity_at: float | None
    parent_client_session_id: str | None = None
    parent_state: str = "absent"
    session_kind: str = "root"
    project: str | None = None
    project_source: str | None = None
    identity_scope_state: str = "unscoped"
    namespace_fingerprint: str | None = None
    activity_time_basis: str = "host_event_time"
    missing_host_timestamp_count: int = 0
    observed_models: tuple[str, ...] = ()
    source_instances: tuple[str, ...] = ()
    source_event_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    event_types: tuple[str, ...] = ()
    host_events: tuple[str, ...] = ()
    observation_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SESSION_OBSERVATION_SCHEMA_VERSION,
            "client": self.client,
            "client_session_id": self.client_session_id,
            "first_activity_at": self.first_activity_at,
            "last_activity_at": self.last_activity_at,
            "parent_client_session_id": self.parent_client_session_id,
            "parent_state": self.parent_state,
            "session_kind": self.session_kind,
            "project": self.project,
            "project_source": self.project_source,
            "identity_scope_state": self.identity_scope_state,
            "namespace_fingerprint": self.namespace_fingerprint,
            "activity_time_basis": self.activity_time_basis,
            "missing_host_timestamp_count": self.missing_host_timestamp_count,
            "observed_models": list(self.observed_models),
            "source_instances": list(self.source_instances),
            "source_event_ids": list(self.source_event_ids),
            "evidence_ids": list(self.evidence_ids),
            "event_types": list(self.event_types),
            "host_events": list(self.host_events),
            "observation_count": self.observation_count,
            "activity_basis": "client_hook_observed",
            "usage_observed": False,
            "semantic_work_observed": False,
        }


def build_session_observations(
    envelopes: Iterable[Any],
    *,
    default_project_label: str | None = None,
    diagnostics: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Project trusted client-hook envelopes into canonical session rows.

    Raw Evidence v2 remains untouched and source-local.  Cross-source product
    coalescing happens only on the existing canonical client/session key so a
    later usage import or MCP section enriches the same Task instead of
    creating a duplicate card.
    """

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for raw in envelopes:
        _increment(diagnostics, "input_envelopes")
        envelope = _plain_envelope(raw)
        if envelope.get("assertion") != "observed" or envelope.get("source_type") != "client_hook":
            _increment(diagnostics, "ineligible_envelopes_dropped")
            continue
        tags = envelope.get("tags")
        if not isinstance(tags, (list, tuple)) or "mechanical_capture" not in tags:
            _increment(diagnostics, "ineligible_envelopes_dropped")
            continue
        dimensions = envelope.get("dimensions")
        if not isinstance(dimensions, (list, tuple)) or "activity" not in dimensions:
            _increment(diagnostics, "ineligible_envelopes_dropped")
            continue
        client = _text(envelope.get("source_system"), maximum=256)
        subjects = _mapping(envelope.get("subjects"))
        raw_session_id = _text(subjects.get("client_session_id"))
        if not client or not raw_session_id:
            _increment(diagnostics, "envelopes_without_session_identity")
            continue
        # Hook ids are client-authored opaque ids.  In particular, the
        # historical ``:model:`` cleanup is valid only for trusted legacy
        # usage-import rows and must never rewrite a real hook id.
        grouped.setdefault((client, raw_session_id), []).append(envelope)

    observations: list[SessionObservation] = []
    for (client, session_id), rows in grouped.items():
        times = [
            timestamp
            for row in rows
            if (timestamp := _timestamp(row.get("event_timestamp") or row.get("observed_at"))) is not None
        ]
        parents = {
            parent
            for row in rows
            if (parent := _text(_mapping(row.get("subjects")).get("parent_client_session_id")))
        }
        parents.discard(session_id)
        parent_id = next(iter(parents)) if len(parents) == 1 else None
        parent_state = "observed" if parent_id else "conflicting" if parents else "absent"

        identity_scopes = {
            (
                _text(
                    _mapping(row.get("subjects")).get("organization")
                    or _mapping(_mapping(row.get("subjects")).get("extra")).get("organization")
                )
                or "unscoped",
                _text(_mapping(row.get("subjects")).get("project_id")) or "unscoped",
            )
            for row in rows
        }
        if len(identity_scopes) > 1:
            # The current public Task key is still (client, session id).  A
            # cross-scope collision cannot be represented without a store
            # migration, so missing the session is safer than merging it.
            _increment(diagnostics, "namespace_collision_sessions_dropped")
            continue
        identity_scope_state = "explicit" if identity_scopes != {("unscoped", "unscoped")} else "unscoped"
        namespace_fingerprint = None
        if identity_scope_state == "explicit":
            organization, project_id = next(iter(identity_scopes))
            namespace_material = json.dumps(
                {"client": client, "organization": organization, "project_id": project_id},
                sort_keys=True,
                separators=(",", ":"),
            )
            namespace_fingerprint = f"ns:{sha256(namespace_material.encode('utf-8')).hexdigest()[:24]}"
        # project_id is an identity namespace, not a display label.  Only the
        # serving project store may provide the latter.
        project = _text(default_project_label, maximum=240) or None
        project_source = "serving_project_store" if project else None

        payloads = [_mapping(row.get("payload")) for row in rows]
        attributes = [_mapping(payload.get("attributes")) for payload in payloads]
        capture_time_fallbacks = sum(attribute.get("time_basis") == "capture_observed" for attribute in attributes)
        activity_time_basis = (
            "capture_observed"
            if capture_time_fallbacks == len(rows)
            else "mixed"
            if capture_time_fallbacks
            else "host_event_time"
        )
        observations.append(
            SessionObservation(
                client=client,
                client_session_id=session_id,
                first_activity_at=min(times) if times else None,
                last_activity_at=max(times) if times else None,
                parent_client_session_id=parent_id,
                parent_state=parent_state,
                session_kind="child" if parent_id else "unknown" if parent_state == "conflicting" else "root",
                project=project,
                project_source=project_source,
                identity_scope_state=identity_scope_state,
                namespace_fingerprint=namespace_fingerprint,
                activity_time_basis=activity_time_basis,
                missing_host_timestamp_count=capture_time_fallbacks,
                observed_models=_ordered_unique(
                    sorted({_text(attribute.get("model"), maximum=160) for attribute in attributes} - {""})
                ),
                source_instances=_ordered_unique(
                    sorted({_text(row.get("source_instance"), maximum=256) for row in rows} - {""})
                ),
                source_event_ids=_ordered_unique(
                    sorted({_text(row.get("source_event_id")) for row in rows} - {""})
                ),
                evidence_ids=_ordered_unique(sorted({_text(row.get("evidence_id")) for row in rows} - {""})),
                event_types=_ordered_unique(
                    sorted({_text(row.get("event_type"), maximum=160) for row in rows} - {""})
                ),
                host_events=_ordered_unique(
                    sorted({_text(payload.get("host_event"), maximum=160) for payload in payloads} - {""})
                ),
                observation_count=len(rows),
            )
        )

    observations.sort(
        key=lambda row: (
            row.last_activity_at is not None,
            row.last_activity_at or 0.0,
            row.client,
            row.client_session_id,
        ),
        reverse=True,
    )
    if diagnostics is not None:
        diagnostics["projected_sessions"] = len(observations)
    return [row.to_dict() for row in observations]


__all__ = [
    "DEFAULT_MECHANICAL_CONFLICT_GROUP_LIMIT",
    "DEFAULT_MECHANICAL_CONFLICT_ROW_LIMIT",
    "SESSION_OBSERVATION_SCHEMA_VERSION",
    "SessionObservation",
    "build_session_observations",
    "expand_complete_conflict_groups",
    "select_session_projection_envelopes",
]
