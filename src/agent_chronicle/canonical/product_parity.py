"""Independent existing-product output parity for an offline legacy snapshot.

This module is deliberately read-only.  The source oracle uses the existing
``build_work_ledger`` and ``build_task_projection`` product functions over
manifest-declared legacy events.  The candidate oracle reads canonical SQLite
tables directly.  It never reuses the legacy importer's source summary, so a
shared importer/candidate bug cannot make this comparison pass.  For recovery,
the sealed plan selects the already-authorized subset; it does not supply the
expected Work or Task output, which is rebuilt independently by those existing
product functions.

The result is intentionally a *core truth-slice* report, not an endpoint or
cutover claim.  Continuation-store memberships, full Task detail, outcomes,
findings, checks/artifacts, and the complete Sessions JSON contract remain
outside the spike schema and are named as unsupported surfaces in every report.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from agent_chronicle.task_projection import build_task_projection
from agent_chronicle.usage_truth import normalized_local_usage_session_id
from agent_chronicle.work_ledger import build_work_ledger

from .legacy_import import MigrationReport
from .migration_archive import RecoveryIdentity, VerifiedRecoveryPlan
from .migration_disposition_policy import (
    build_migration_disposition_policy_evidence,
)
from .snapshot import VerifiedSnapshot
from .sqlite import CanonicalRepository
from .types import (
    RECOVERY_FACT_TRANSPORT,
    RECOVERY_LINK_METHOD,
    RECOVERY_LINK_RULE_VERSION,
)


PRODUCT_PARITY_SCHEMA_VERSION = "agent-chronicle.legacy-product-parity.v2"
RECOVERY_PRODUCT_ORACLE_CONTRACT_VERSION = 5
RECOVERY_PRODUCT_ORACLE_CODE_VERSION = 5
SUPPORTED_LEGACY_MANIFEST_KINDS = frozenset({"legacy", "legacy-chronicle"})
_USAGE_AGGREGATE_PARITY_BASIS_VERSION = (
    "agent-chronicle.usage-aggregate-parity-basis.v1"
)
_CODEX_SQLITE_TOTAL_ONLY_REPRESENTATION = (
    "codex-sqlite-tokens-used-fallback-v1"
)

_TOKEN_SOURCE_KEYS: Mapping[str, tuple[str, str]] = {
    "input_tokens": ("event", "estimated_input_tokens"),
    "output_tokens": ("event", "estimated_output_tokens"),
    "cached_input_tokens": ("metadata", "cached_input_tokens"),
    "cache_creation_input_tokens": ("metadata", "cache_creation_input_tokens"),
    "cache_read_input_tokens": ("metadata", "cache_read_input_tokens"),
    "reasoning_output_tokens": ("metadata", "reasoning_output_tokens"),
    "total_tokens": ("metadata", "total_tokens"),
}
_TOKEN_REPORTED_KEYS: Mapping[str, tuple[str, ...]] = {
    "input_tokens": ("input_tokens_reported",),
    "output_tokens": ("output_tokens_reported",),
    "cached_input_tokens": ("cached_input_tokens_reported",),
    "cache_creation_input_tokens": (
        "cache_creation_input_tokens_reported",
        "cache_creation_tokens_reported",
    ),
    "cache_read_input_tokens": (
        "cache_read_input_tokens_reported",
        "cache_read_tokens_reported",
    ),
    "reasoning_output_tokens": (
        "reasoning_output_tokens_reported",
        "reasoning_tokens_reported",
    ),
    "total_tokens": ("total_tokens_reported",),
}
_USAGE_AGGREGATE_FIELDS = (
    "measurement_count",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


class ProductParityError(ValueError):
    """The offline parity request is malformed or outside the supported slice."""


@dataclass(frozen=True)
class _LegacyRead:
    events: tuple[dict[str, Any], ...]
    event_line_numbers: tuple[int, ...]
    lines_seen: int
    malformed_or_non_object: int


def _metadata(event: Mapping[str, Any]) -> Mapping[str, Any]:
    value = event.get("metadata")
    return value if isinstance(value, Mapping) else {}


_UNMAPPABLE_SPLITLINE_SEPARATORS = frozenset(
    ("\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")
)


def _require_lf_mappable_product_text(text: str) -> None:
    """Reject product line boundaries that cannot map to physical receipts."""

    for index, value in enumerate(text):
        if value in _UNMAPPABLE_SPLITLINE_SEPARATORS:
            raise ProductParityError(
                "existing-product source has a line separator that cannot be "
                "mapped to physical recovery receipts"
            )
        if value == "\r" and (index + 1 == len(text) or text[index + 1] != "\n"):
            raise ProductParityError(
                "existing-product source has a standalone CR line separator "
                "that cannot be mapped to physical recovery receipts"
            )


def _read_existing_product_events(
    snapshot: VerifiedSnapshot,
    source_file: str,
) -> _LegacyRead:
    """Mirror the legacy product's whole-file UTF-8 ``splitlines`` reader.

    The canonical importer intentionally caps individual JSONL lines.  The
    existing product does not.  Reading through the importer-sized cap here
    would let both candidate databases omit the same valid oversized row and
    make the independent Oracle report a false pass.
    """

    events: list[dict[str, Any]] = []
    event_line_numbers: list[int] = []
    lines_seen = 0
    malformed = 0
    with snapshot.open_binary(source_file) as handle:
        raw = handle.read()
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ProductParityError(
            "existing-product source is not valid whole-file UTF-8"
        ) from exc
    _require_lf_mappable_product_text(text)
    for lines_seen, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(value, dict):
            raise ProductParityError(
                "existing-product source contains a JSON value that is not an event object"
            )
        events.append(value)
        event_line_numbers.append(lines_seen)
    return _LegacyRead(
        tuple(events),
        tuple(event_line_numbers),
        lines_seen,
        malformed,
    )


def _sealed_recovery_identity_by_line(
    plan: VerifiedRecoveryPlan,
) -> dict[tuple[str, int], RecoveryIdentity]:
    result: dict[tuple[str, int], RecoveryIdentity] = {}
    for locator, disposition in plan.iter_lines():
        if disposition != "candidate_recovery":
            continue
        decision = plan.recovery_for(locator)
        if decision is None:
            raise ProductParityError("candidate recovery receipt has no decision")
        key = (locator.relative_path, locator.line_number)
        if key in result:
            raise ProductParityError("recovery plan repeats a physical source line")
        result[key] = decision.recovery
    return result


def _overlay_sealed_recovery_identity(
    event: Mapping[str, Any],
    recovery: RecoveryIdentity,
) -> dict[str, Any]:
    """Build the independent source view at the sealed post-recovery boundary."""

    result = dict(event)
    metadata = dict(_metadata(event))
    session_id = normalized_local_usage_session_id(
        recovery.client,
        recovery.client_session_id,
    )
    raw_client = str(metadata.get("client") or "").strip()
    if not raw_client:
        metadata["client"] = recovery.client
        raw_client = recovery.client
    raw_session = str(metadata.get("client_session_id") or "").strip()
    if not raw_session:
        metadata["client_session_id"] = session_id
    elif (
        raw_client == recovery.client
        and normalized_local_usage_session_id(raw_client, raw_session) == session_id
    ):
        metadata["client_session_id"] = session_id
    if not str(metadata.get("source_namespace_fingerprint") or "").strip():
        metadata["source_namespace_fingerprint"] = (
            recovery.source_namespace_fingerprint
        )
    semantic_namespace = str(
        metadata.get("namespace_fingerprint")
        or metadata.get("session_namespace_fingerprint")
        or ""
    ).strip()
    if not semantic_namespace:
        metadata["session_namespace_fingerprint"] = (
            recovery.source_namespace_fingerprint
        )
    if not str(metadata.get("identity_scope_state") or "").strip():
        metadata["identity_scope_state"] = "explicit"
    authored = metadata.get("client_context_keys_authored")
    authored_keys = {
        str(value)
        for value in (authored if isinstance(authored, list) else [])
        if isinstance(value, str)
    }
    authored_keys.add("client_session_id")
    metadata["client_context_keys_authored"] = sorted(authored_keys)
    result["metadata"] = metadata
    return result


def _normalize_source_scoped_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Reflect the canonical session scope of an explicitly sourced row.

    The legacy product stored import-source and semantic-session namespace in
    separate optional fields.  Canonical sessions use the verified source
    namespace.  Fill only a missing semantic value; an asserted mismatch stays
    intact and continues to fail closed.
    """

    result = dict(event)
    metadata = dict(_metadata(event))
    source_namespace = str(metadata.get("source_namespace_fingerprint") or "")
    semantic_namespace = str(
        metadata.get("namespace_fingerprint")
        or metadata.get("session_namespace_fingerprint")
        or ""
    )
    if source_namespace and not semantic_namespace:
        metadata["session_namespace_fingerprint"] = source_namespace
        metadata["identity_scope_state"] = "explicit"
    result["metadata"] = metadata
    return result


def _source_task_session_key(
    value: Mapping[str, Any],
) -> tuple[str, str] | None:
    client = str(value.get("client") or "")
    session_id = str(
        value.get("client_session_id")
        or value.get("session_id")
        or value.get("root_session_id")
        or ""
    )
    return (client, session_id) if client and session_id else None


def _normalized_recovered_lineage_sessions(
    session_rows: list[Mapping[str, Any]],
    *,
    recovered_session_keys: set[tuple[str, str]],
) -> list[Mapping[str, Any]]:
    """Normalize only recovered session lineages to canonical source scope.

    Legacy session rows can assert an import-source namespace while omitting
    the equivalent semantic namespace.  Canonical sessions store the former
    as their namespace.  For recovered lineages, fill only missing semantic
    and expected-parent values from independently read source rows; preserve
    every asserted mismatch so the product's fail-closed checks still fire.
    """

    copied = [dict(row) for row in session_rows]
    by_key = {
        key: row
        for row in copied
        if (key := _source_task_session_key(row)) is not None
    }
    lineage_keys: set[tuple[str, str]] = set()
    pending = list(recovered_session_keys)
    while pending:
        key = pending.pop()
        if key in lineage_keys:
            continue
        row = by_key.get(key)
        if row is None:
            continue
        lineage_keys.add(key)
        related = row.get("related")
        related = related if isinstance(related, Mapping) else {}
        parent = related.get("parent")
        parent = parent if isinstance(parent, Mapping) else {}
        parent_id = str(
            parent.get("client_session_id")
            or row.get("parent_client_session_id")
            or ""
        )
        if parent_id:
            pending.append((key[0], parent_id))

    for key in lineage_keys:
        row = by_key[key]
        source_namespace = str(row.get("source_namespace_fingerprint") or "")
        semantic_namespace = str(
            row.get("namespace_fingerprint")
            or row.get("session_namespace_fingerprint")
            or ""
        )
        if source_namespace and not semantic_namespace:
            row["namespace_fingerprint"] = source_namespace
            row["identity_scope_state"] = "explicit"

        related = row.get("related")
        related = related if isinstance(related, Mapping) else {}
        parent = related.get("parent")
        parent = parent if isinstance(parent, Mapping) else {}
        parent_id = str(
            parent.get("client_session_id")
            or row.get("parent_client_session_id")
            or ""
        )
        parent_row = by_key.get((key[0], parent_id)) if parent_id else None
        parent_source = str(
            parent_row.get("source_namespace_fingerprint") or ""
        ) if parent_row is not None else ""
        if parent_source and not row.get("parent_source_namespace_fingerprint"):
            row["parent_source_namespace_fingerprint"] = parent_source
    return copied


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _comparison(
    surface: str,
    source_rows: list[Mapping[str, object]],
    candidate_rows: list[Mapping[str, object]],
    *,
    required_core: bool = True,
) -> dict[str, object]:
    source_encoded = Counter(_canonical_json(dict(row)) for row in source_rows)
    candidate_encoded = Counter(_canonical_json(dict(row)) for row in candidate_rows)
    source_only = source_encoded - candidate_encoded
    candidate_only = candidate_encoded - source_encoded
    mismatch_count = sum(source_only.values()) + sum(candidate_only.values())
    return {
        "surface": surface,
        "required_core": required_core,
        "matches": mismatch_count == 0,
        "source_count": len(source_rows),
        "candidate_count": len(candidate_rows),
        "mismatch_count": mismatch_count,
    }


def _legacy_namespace_identity(
    value: object,
    *,
    snapshot: VerifiedSnapshot,
    source_file: str,
    client: str,
) -> dict[str, str]:
    """Independently reproduce the documented candidate namespace identity."""

    if isinstance(value, str) and value.strip():
        digest = hashlib.sha256(
            b"legacy-explicit-namespace-v1\x00" + value.strip().encode("utf-8")
        ).hexdigest()
        return {
            "namespace_scheme": "legacy-explicit-fingerprint-sha256-v1",
            "namespace_digest": digest,
        }
    digest = hashlib.sha256(
        b"legacy-unresolved-snapshot-namespace-v1\x00"
        + snapshot.manifest_digest.encode("ascii")
        + b"\x00"
        + source_file.encode("utf-8")
        + b"\x00"
        + client.encode("utf-8")
    ).hexdigest()
    return {
        "namespace_scheme": "snapshot-scoped-unresolved-v1",
        "namespace_digest": digest,
    }


def _source_session_identity(
    row: Mapping[str, Any],
    *,
    snapshot: VerifiedSnapshot,
    source_file: str,
) -> dict[str, str]:
    client = str(row.get("client") or "")
    identity = _legacy_namespace_identity(
        row.get("source_namespace_fingerprint"),
        snapshot=snapshot,
        source_file=source_file,
        client=client,
    )
    return {
        "client": client,
        **identity,
        "client_session_id": str(row.get("client_session_id") or ""),
    }


def _candidate_session_identity(row: Mapping[str, Any]) -> dict[str, str]:
    digest = row.get("namespace_digest")
    return {
        "client": str(row.get("client") or ""),
        "namespace_scheme": str(row.get("namespace_scheme") or ""),
        "namespace_digest": (
            bytes(digest).hex()
            if isinstance(digest, (bytes, bytearray, memoryview))
            else str(digest or "")
        ),
        "client_session_id": str(row.get("client_session_id") or ""),
    }


def _source_sessions_and_tasks(
    task_projection: Mapping[str, Any],
    *,
    snapshot: VerifiedSnapshot,
    source_file: str,
) -> tuple[list[Mapping[str, object]], list[Mapping[str, object]]]:
    sessions_by_identity: dict[str, Mapping[str, object]] = {}
    task_rows: list[Mapping[str, object]] = []
    tasks = task_projection.get("tasks")
    for task in tasks if isinstance(tasks, list) else []:
        if not isinstance(task, Mapping):
            continue
        member_rows: list[Mapping[str, Any]] = [
            value
            for value in (
                task.get("sessions") if isinstance(task.get("sessions"), list) else []
            )
            if isinstance(value, Mapping)
        ]
        member_identities = [
            _source_session_identity(
                value,
                snapshot=snapshot,
                source_file=source_file,
            )
            for value in member_rows
        ]
        primary_ref = task.get("primary_root")
        primary_ref = primary_ref if isinstance(primary_ref, Mapping) else {}
        matching_primary = [
            identity
            for value, identity in zip(member_rows, member_identities)
            if str(value.get("client") or "") == str(primary_ref.get("client") or "")
            and str(value.get("client_session_id") or "")
            == str(primary_ref.get("client_session_id") or "")
        ]
        primary: Mapping[str, object]
        if len(matching_primary) == 1:
            primary = matching_primary[0]
        else:
            primary = {
                "client": str(primary_ref.get("client") or ""),
                "namespace_scheme": "ambiguous",
                "namespace_digest": "ambiguous",
                "client_session_id": str(primary_ref.get("client_session_id") or ""),
            }
        for value, identity in zip(member_rows, member_identities):
            parent_ref = value.get("related")
            parent_ref = parent_ref.get("parent") if isinstance(parent_ref, Mapping) else None
            parent_ref = parent_ref if isinstance(parent_ref, Mapping) else {}
            session_row: Mapping[str, object] = {
                "identity": identity,
                "session_kind": value.get("session_kind"),
                "parent_client_session_id": parent_ref.get("client_session_id"),
            }
            sessions_by_identity[_canonical_json(identity)] = session_row
        task_rows.append(
            {
                "primary": primary,
                "members": sorted(
                    member_identities,
                    key=_canonical_json,
                ),
            }
        )
    return list(sessions_by_identity.values()), task_rows


def _candidate_sessions_and_tasks(
    repository: CanonicalRepository,
) -> tuple[list[Mapping[str, object]], list[Mapping[str, object]]]:
    connection = repository.connection
    raw_sessions = [
        dict(row)
        for row in connection.execute(
            "SELECT session.session_id, session.client_session_id, session.session_kind, "
            "source.client, source.namespace_scheme, source.namespace_digest "
            "FROM sessions session JOIN source_instances source "
            "ON source.source_instance_id = session.source_instance_id"
        ).fetchall()
    ]
    by_id = {int(row["session_id"]): row for row in raw_sessions}
    parent_by_child = {
        int(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT child_session_id, parent_session_id FROM session_edges "
            "WHERE validation_state = 'valid'"
        ).fetchall()
    }

    def lineage_root(session_id: int) -> int | None:
        current = session_id
        seen: set[int] = set()
        while current not in seen:
            seen.add(current)
            parent = parent_by_child.get(current)
            if parent is None:
                return current
            if parent not in by_id:
                return None
            current = parent
        return None

    roots = {session_id: lineage_root(session_id) for session_id in by_id}
    source_rows: list[Mapping[str, object]] = []
    for session_id, row in by_id.items():
        parent = parent_by_child.get(session_id)
        source_rows.append(
            {
                "identity": _candidate_session_identity(row),
                "session_kind": row.get("session_kind"),
                "parent_client_session_id": (
                    by_id[parent].get("client_session_id")
                    if parent is not None and parent in by_id
                    else None
                ),
            }
        )
    task_rows: list[Mapping[str, object]] = []
    for anchor in connection.execute(
        "SELECT primary_session_id FROM task_anchors ORDER BY task_anchor_id"
    ).fetchall():
        primary_id = int(anchor[0])
        members = [
            _candidate_session_identity(by_id[session_id])
            for session_id, root_id in roots.items()
            if root_id == primary_id
        ]
        task_rows.append(
            {
                "primary": _candidate_session_identity(by_id[primary_id]),
                "members": sorted(members, key=_canonical_json),
            }
        )
    return source_rows, task_rows


def _is_pre_presence_codex_row(event: Mapping[str, Any]) -> bool:
    metadata = _metadata(event)
    return (
        str(event.get("event_type") or "").lower() == "model_usage"
        and event.get("source") == "codex-local-session-import"
        and metadata.get("client") == "codex"
        and metadata.get("usage_source") == "local_client_session_store"
    )


def _source_token(
    event: Mapping[str, Any],
    field_name: str,
) -> dict[str, object]:
    metadata = _metadata(event)
    container_name, key = _TOKEN_SOURCE_KEYS[field_name]
    container = event if container_name == "event" else metadata
    explicit: bool | None = None
    invalid_flag = False
    for flag_key in _TOKEN_REPORTED_KEYS[field_name]:
        if flag_key not in metadata:
            continue
        if isinstance(metadata[flag_key], bool):
            explicit = bool(metadata[flag_key])
        else:
            invalid_flag = True
        break
    if field_name == "cached_input_tokens" and explicit is None and not invalid_flag:
        # New client rows retain a numeric combined-cache compatibility field,
        # but source presence lives on the two split capability flags.  Treat
        # both explicit false bits as unavailable and either true bit as proof
        # that the combined value was measured.  Old rows without split flags
        # continue to use valid key presence.
        split_flags: list[bool] = []
        for split_field in (
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            split_value: bool | None = None
            split_declared = False
            for flag_key in _TOKEN_REPORTED_KEYS[split_field]:
                if flag_key not in metadata:
                    continue
                split_declared = True
                if isinstance(metadata[flag_key], bool):
                    split_value = bool(metadata[flag_key])
                else:
                    invalid_flag = True
                break
            if split_declared and split_value is not None:
                split_flags.append(split_value)
        if any(split_flags):
            explicit = True
        elif len(split_flags) == 2:
            explicit = False
    if invalid_flag:
        return {"reported": False, "value": None}
    if (
        explicit is None
        and _is_pre_presence_codex_row(event)
        and field_name
        in {"input_tokens", "output_tokens", "reasoning_output_tokens"}
    ):
        return {"reported": False, "value": None}
    value = container.get(key)
    valid = isinstance(value, int) and not isinstance(value, bool) and value >= 0
    reported = explicit if explicit is not None else key in container and valid
    return {
        "reported": bool(reported and valid),
        "value": int(value) if reported and valid else None,
    }


_SOURCE_CUMULATIVE_USAGE_SEMANTICS = frozenset(
    {
        "cumulative_snapshot",
        "codex_rollout_token_count_events",
        "codex_rollout_lineage_delta_v1",
        "codex_sqlite_tokens_used_fallback",
        "claude_assistant_message_usage_rows",
        "opencode_step_finish_events",
        "hermes_state_db_session_rows",
        "openclaw_assistant_usage_rows",
    }
)
_SOURCE_PRECEDENCE_ROLES = frozenset(
    {"authoritative", "fallback", "enrichment"}
)
_SOURCE_USAGE_GRANULARITIES = frozenset(
    {"request", "turn", "session", "task_only", "unavailable"}
)


def _source_usage_basis(
    event: Mapping[str, Any],
    existing_product_row: Mapping[str, Any],
) -> dict[str, object]:
    """Map typed basis independently from the migration implementation."""

    metadata = _metadata(event)
    raw_semantics = str(
        metadata.get("usage_update_semantics")
        or event.get("usage_update_semantics")
        or "cumulative_snapshot"
    ).lower()
    semantics = (
        "cumulative_snapshot"
        if raw_semantics in _SOURCE_CUMULATIVE_USAGE_SEMANTICS
        else f"unsupported:{raw_semantics[:96]}"
    )
    raw_representation = metadata.get("usage_representation")
    representation = (
        raw_representation.strip()
        if isinstance(raw_representation, str)
        and raw_representation.strip()
        and len(raw_representation.strip()) <= 128
        else "legacy-v1"
    )
    raw_precedence = str(metadata.get("precedence_role") or "authoritative").lower()
    precedence = (
        raw_precedence
        if raw_precedence in _SOURCE_PRECEDENCE_ROLES
        else "authoritative"
    )
    raw_granularity = str(
        metadata.get("usage_granularity") or "session"
    ).lower()
    granularity = (
        raw_granularity
        if raw_granularity in _SOURCE_USAGE_GRANULARITIES
        else "session"
    )
    totals_eligible = existing_product_row.get("usage_additive") is not False
    return {
        "representation": representation,
        "update_semantics": semantics,
        "precedence_role": precedence,
        "granularity": granularity,
        "held_reason_present": not totals_eligible,
    }


def _uses_codex_sqlite_total_only_fallback(
    *,
    client: object,
    basis: Mapping[str, object],
    tokens: Mapping[str, Mapping[str, object]],
) -> bool:
    """Identify the approved total-only fallback without inventing input truth."""

    component_fields = (
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "reasoning_output_tokens",
    )
    total_tokens = tokens.get("total_tokens", {})
    total_value = total_tokens.get("value")
    return bool(
        client == "codex"
        and basis.get("representation") == _CODEX_SQLITE_TOTAL_ONLY_REPRESENTATION
        and basis.get("update_semantics") == "cumulative_snapshot"
        and basis.get("precedence_role") == "fallback"
        and basis.get("granularity") == "session"
        and all(
            tokens.get(field_name, {}).get("reported") is False
            for field_name in component_fields
        )
        and total_tokens.get("reported") is True
        and isinstance(total_value, int)
        and not isinstance(total_value, bool)
        and total_value >= 0
    )


def _source_usage_rows(
    ledger: Mapping[str, Any],
    events: tuple[dict[str, Any], ...],
    *,
    snapshot: VerifiedSnapshot,
    source_file: str,
) -> tuple[list[Mapping[str, object]], list[Mapping[str, object]], int]:
    raw_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        raw_by_id[str(event.get("event_id") or "")].append(event)
    selected = [
        value
        for key in ("usage_events", "excluded_usage_events")
        for value in (ledger.get(key) if isinstance(ledger.get(key), list) else [])
        if isinstance(value, Mapping)
    ]
    presence_rows: list[Mapping[str, object]] = []
    aggregates: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    total_only_fallback_rows = 0
    for row in selected:
        event_id = str(row.get("event_id") or row.get("usage_event_id") or "")
        raw_candidates = raw_by_id.get(event_id, [])
        raw = raw_candidates[0] if len(raw_candidates) == 1 else {}
        raw_metadata = _metadata(raw)
        client = str(row.get("client") or "")
        identity = {
            "client": client,
            **_legacy_namespace_identity(
                raw_metadata.get("source_namespace_fingerprint"),
                snapshot=snapshot,
                source_file=source_file,
                client=client,
            ),
            "client_session_id": str(row.get("client_session_id") or ""),
        }
        tokens = {
            field_name: _source_token(raw, field_name)
            for field_name in _TOKEN_SOURCE_KEYS
        }
        eligible = row.get("usage_additive") is not False
        basis = _source_usage_basis(raw, row)
        if eligible and _uses_codex_sqlite_total_only_fallback(
            client=client,
            basis=basis,
            tokens=tokens,
        ):
            total_only_fallback_rows += 1
        presence_rows.append(
            {
                "identity": identity,
                "lane": str(row.get("usage_row_lane") or "session_total"),
                "provider": row.get("provider"),
                "model": row.get("model"),
                "totals_eligible": eligible,
                "basis": basis,
                "tokens": tokens,
            }
        )
        if not eligible:
            continue
        group = (
            str(row.get("client") or ""),
            str(row.get("provider") or ""),
            str(row.get("model") or ""),
        )
        aggregate = aggregates[group]
        aggregate["measurement_count"] += 1
        for key in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "cache_creation_tokens",
            "cache_read_tokens",
            "total_tokens",
        ):
            aggregate[key] += int(row.get(key) or 0)
        reasoning = raw_metadata.get("reasoning_output_tokens")
        if isinstance(reasoning, int) and not isinstance(reasoning, bool) and reasoning >= 0:
            aggregate["reasoning_output_tokens"] += reasoning
    aggregate_rows = [
        {
            "client": group[0],
            "provider": group[1] or None,
            "model": group[2] or None,
            **{
                field_name: int(values.get(field_name, 0))
                for field_name in _USAGE_AGGREGATE_FIELDS
            },
        }
        for group, values in aggregates.items()
    ]
    return presence_rows, aggregate_rows, total_only_fallback_rows


def _candidate_usage_rows(
    repository: CanonicalRepository,
) -> tuple[list[Mapping[str, object]], list[Mapping[str, object]], int]:
    rows = [
        dict(row)
        for row in repository.connection.execute(
            "SELECT usage.*, session.client_session_id, source.client, "
            "source.namespace_scheme, source.namespace_digest "
            "FROM usage_measurements usage "
            "JOIN sessions session ON session.session_id = usage.session_id "
            "JOIN source_instances source ON source.source_instance_id = session.source_instance_id"
        ).fetchall()
    ]
    presence_rows: list[Mapping[str, object]] = []
    aggregates: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    compatibility_reprojected_rows = 0
    token_columns = tuple(_TOKEN_SOURCE_KEYS)
    for row in rows:
        basis = {
            "representation": row.get("representation"),
            "update_semantics": row.get("update_semantics"),
            "precedence_role": row.get("precedence_role"),
            "granularity": row.get("granularity"),
            "held_reason_present": row.get("held_reason") is not None,
        }
        tokens = {
            field_name: {
                "reported": bool(row.get(f"{field_name}_reported")),
                "value": row.get(field_name),
            }
            for field_name in token_columns
        }
        presence_rows.append(
            {
                "identity": _candidate_session_identity(row),
                "lane": str(row.get("lane") or ""),
                "provider": row.get("provider"),
                "model": row.get("model"),
                "totals_eligible": bool(row.get("totals_eligible")),
                "basis": basis,
                "tokens": tokens,
            }
        )
        if not bool(row.get("totals_eligible")):
            continue
        group = (
            str(row.get("client") or ""),
            str(row.get("provider") or ""),
            str(row.get("model") or ""),
        )
        aggregate = aggregates[group]
        aggregate["measurement_count"] += 1
        if _uses_codex_sqlite_total_only_fallback(
            client=row.get("client"),
            basis=basis,
            tokens=tokens,
        ):
            # ``usage_aggregates`` compares the existing product output, whose
            # compatibility row historically placed SQLite ``tokens_used`` in
            # both the input and total buckets.  Canonical truth remains
            # total-only (the strict ``usage_presence`` surface still compares
            # input=NULL/unreported and total=reported).  Reconstruct that
            # legacy display projection here, visibly accounted for in the
            # report, rather than rewriting or relabelling canonical input.
            compatibility_total = int(tokens["total_tokens"]["value"])
            aggregate["input_tokens"] += compatibility_total
            aggregate["total_tokens"] += compatibility_total
            compatibility_reprojected_rows += 1
            continue
        aggregate["input_tokens"] += int(row.get("input_tokens") or 0)
        aggregate["output_tokens"] += int(row.get("output_tokens") or 0)
        aggregate["cached_input_tokens"] += int(row.get("cached_input_tokens") or 0)
        aggregate["cache_creation_tokens"] += int(
            row.get("cache_creation_input_tokens") or 0
        )
        aggregate["cache_read_tokens"] += int(
            row.get("cache_read_input_tokens") or 0
        )
        aggregate["reasoning_output_tokens"] += int(
            row.get("reasoning_output_tokens") or 0
        )
        # Existing product total means fresh + cached detail, not the raw
        # provider total column's possibly different legacy representation.
        aggregate["total_tokens"] += (
            int(row.get("input_tokens") or 0)
            + int(row.get("output_tokens") or 0)
            + int(row.get("cached_input_tokens") or 0)
        )
    aggregate_rows = [
        {
            "client": group[0],
            "provider": group[1] or None,
            "model": group[2] or None,
            **{
                field_name: int(values.get(field_name, 0))
                for field_name in _USAGE_AGGREGATE_FIELDS
            },
        }
        for group, values in aggregates.items()
    ]
    return presence_rows, aggregate_rows, compatibility_reprojected_rows


_RECOVERY_STATUSES = frozenset(
    {"started", "checkpoint", "completed", "blocked", "reported", "aborted"}
)
_RECOVERY_OUTCOME_AXES = frozenset(
    {"intent", "execution", "verification", "outcome", "finding"}
)
_RECOVERY_CLAIM_FIELDS = (
    "event_kind",
    "status",
    "title",
    "objective",
    "summary",
    "blocker",
    "next_step",
    "work_id",
    "section_id",
    "outcome_axis",
)


def _optional_source_text(value: object, *, limit: int) -> str | None:
    """Normalize source text without calling the migration adapter."""

    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:limit]


def _source_time_us(value: object) -> int | None:
    """Interpret a legacy timestamp independently from candidate rows."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number) or number < 0:
            return None
        if number >= 10**14:
            result = int(number)
        elif number >= 10**11:
            result = int(number * 1_000)
        else:
            result = int(number * 1_000_000)
        return result if result <= (2**63 - 1) else None
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        try:
            result = max(0, int(parsed.timestamp() * 1_000_000))
        except (OverflowError, OSError, ValueError):
            return None
        return result if result <= (2**63 - 1) else None
    return None


def _source_recovery_event_kind(
    event: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> str | None:
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


def _source_recovery_claim_fields(event: Mapping[str, Any]) -> dict[str, object]:
    """Build expected persisted claim fields from source semantics only.

    This intentionally does not call ``legacy_import._work_claim_input``.  The
    recovery runner can therefore catch a deterministic adapter/candidate bug
    even when both fresh SQLite candidates agree with one another.
    """

    metadata = _metadata(event)
    event_kind = _source_recovery_event_kind(event, metadata)
    if event_kind is None:
        raise ProductParityError("sealed recovery row is not source-side Work")
    event_type = str(event.get("event_type") or "legacy_event").lower()
    raw_status = (
        metadata.get("section_status")
        or metadata.get("task_status")
        or event.get("status")
    )
    status = str(raw_status).lower() if isinstance(raw_status, str) else ""
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
    if status not in _RECOVERY_STATUSES:
        status = "reported"
    raw_axis = str(metadata.get("outcome_axis") or "").lower()
    if raw_axis in _RECOVERY_OUTCOME_AXES:
        outcome_axis = raw_axis
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
        "title": _optional_source_text(
            metadata.get("section_title")
            or metadata.get("task_title")
            or event.get("title"),
            limit=512,
        ),
        "objective": _optional_source_text(
            metadata.get("objective") or event.get("objective"),
            limit=4096,
        ),
        "summary": _optional_source_text(
            metadata.get("summary") or event.get("summary"),
            limit=8192,
        ),
        "blocker": _optional_source_text(
            metadata.get("blocker") or event.get("blocker"),
            limit=4096,
        ),
        "next_step": _optional_source_text(
            metadata.get("next_step") or event.get("next_step"),
            limit=4096,
        ),
        "work_id": _optional_source_text(
            metadata.get("work_id") or event.get("work_id"),
            limit=256,
        ),
        "section_id": _optional_source_text(
            metadata.get("section_id") or event.get("section_id"),
            limit=256,
        ),
        "outcome_axis": outcome_axis,
    }


def _source_claim_content_hash(
    fields: Mapping[str, object],
    *,
    supersedes_source_event_id: str | None,
) -> str:
    normalized = {
        **{field_name: fields.get(field_name) for field_name in _RECOVERY_CLAIM_FIELDS},
        "supersedes_event_id": supersedes_source_event_id,
    }
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_recovery_claim_rows(
    *,
    plan: VerifiedRecoveryPlan,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, Any]],
]:
    decisions = []
    locators = []
    for locator, disposition in plan.iter_lines():
        if disposition != "candidate_recovery":
            continue
        decision = plan.recovery_for(locator)
        if decision is None:
            raise ProductParityError("candidate recovery receipt has no decision")
        locators.append(locator)
        decisions.append(decision)
    raw_by_locator = plan.read_events(tuple(decisions))
    rows: list[dict[str, object]] = []
    raw_events: list[dict[str, Any]] = []
    for locator, decision in zip(locators, decisions):
        event = dict(raw_by_locator[locator])
        raw_events.append(event)
        event_id = _optional_source_text(event.get("event_id"), limit=512)
        if event_id is None:
            raise ProductParityError("candidate recovery source event has no stable id")
        recovery = decision.recovery
        source_identity = {
            "client": recovery.client,
            **_legacy_namespace_identity(
                recovery.source_namespace_fingerprint,
                snapshot=plan.archive.snapshot,
                source_file=locator.relative_path,
                client=recovery.client,
            ),
        }
        session_identity = {
            **source_identity,
            "client_session_id": normalized_local_usage_session_id(
                recovery.client,
                recovery.client_session_id,
            ),
        }
        fields = _source_recovery_claim_fields(event)
        metadata = _metadata(event)
        supersedes = _optional_source_text(
            metadata.get("supersedes_event_id")
            or event.get("supersedes_event_id"),
            limit=512,
        )
        rows.append(
            {
                "source": source_identity,
                "source_event_id": event_id,
                "source_order": locator.line_number,
                "occurred_at_us": _source_time_us(
                    event.get("created_at")
                    or metadata.get("client_event_timestamp")
                )
                or 0,
                "content_hash": _source_claim_content_hash(
                    fields,
                    supersedes_source_event_id=supersedes,
                ),
                "supersedes_source_event_id": supersedes,
                "fact_type": "work_claim",
                "transport": RECOVERY_FACT_TRANSPORT,
                "strength": "recorded_claim",
                "idempotency_scope": None,
                "idempotency_key": None,
                "claim": fields,
                "session": session_identity,
                "link": {
                    "method": RECOVERY_LINK_METHOD,
                    "confidence": "high",
                    "rule_version": RECOVERY_LINK_RULE_VERSION,
                    "validation_state": "valid",
                    "veto_reason": None,
                },
            }
        )
    return rows, raw_events


def _candidate_claim_rows(
    repository: CanonicalRepository,
    *,
    recovery_only: bool,
) -> list[dict[str, object]]:
    where = "WHERE fact.fact_type = 'work_claim'"
    parameters: tuple[object, ...] = ()
    if recovery_only:
        where += " AND fact.transport = ?"
        parameters = (RECOVERY_FACT_TRANSPORT,)
    rows = repository.connection.execute(
        "SELECT fact.source_event_id, fact.source_order, fact.occurred_at_us, "
        "fact.content_hash, fact.fact_type, fact.transport, fact.strength, "
        "fact.idempotency_scope, fact.idempotency_key, "
        "predecessor.source_event_id AS supersedes_source_event_id, "
        "fact_source.client AS fact_client, "
        "fact_source.namespace_scheme AS fact_namespace_scheme, "
        "fact_source.namespace_digest AS fact_namespace_digest, "
        "session.client_session_id, "
        "session_source.client AS session_client, "
        "session_source.namespace_scheme AS session_namespace_scheme, "
        "session_source.namespace_digest AS session_namespace_digest, "
        "link.method, link.confidence, "
        "link.rule_version, link.validation_state, link.veto_reason, "
        "claim.event_kind, claim.status, claim.title, claim.objective, "
        "claim.summary, claim.blocker, claim.next_step, claim.work_id, "
        "claim.section_id, claim.outcome_axis "
        "FROM facts fact JOIN work_claims claim ON claim.fact_id = fact.fact_id "
        "JOIN source_instances fact_source "
        "ON fact_source.source_instance_id = fact.source_instance_id "
        "LEFT JOIN facts predecessor ON predecessor.fact_id = fact.supersedes_fact_id "
        "LEFT JOIN fact_session_links link ON link.fact_id = fact.fact_id "
        "LEFT JOIN sessions session ON session.session_id = link.session_id "
        "LEFT JOIN source_instances session_source "
        "ON session_source.source_instance_id = session.source_instance_id "
        f"{where} ORDER BY fact.fact_id, link.fact_session_link_id",
        parameters,
    ).fetchall()
    result: list[dict[str, object]] = []
    for raw in rows:
        row = dict(raw)
        digest = row.get("fact_namespace_digest")
        source_identity = {
            "client": str(row.get("fact_client") or ""),
            "namespace_scheme": str(row.get("fact_namespace_scheme") or ""),
            "namespace_digest": (
                bytes(digest).hex()
                if isinstance(digest, (bytes, bytearray, memoryview))
                else str(digest or "")
            ),
        }
        session_digest = row.get("session_namespace_digest")
        session_identity = {
            "client": str(row.get("session_client") or ""),
            "namespace_scheme": str(row.get("session_namespace_scheme") or ""),
            "namespace_digest": (
                bytes(session_digest).hex()
                if isinstance(
                    session_digest,
                    (bytes, bytearray, memoryview),
                )
                else str(session_digest or "")
            ),
            "client_session_id": str(row.get("client_session_id") or ""),
        }
        result.append(
            {
                "source": source_identity,
                "source_event_id": row.get("source_event_id"),
                "source_order": row.get("source_order"),
                "occurred_at_us": row.get("occurred_at_us"),
                "content_hash": bytes(row["content_hash"]).hex(),
                "supersedes_source_event_id": row.get(
                    "supersedes_source_event_id"
                ),
                "fact_type": row.get("fact_type"),
                "transport": row.get("transport"),
                "strength": row.get("strength"),
                "idempotency_scope": row.get("idempotency_scope"),
                "idempotency_key": row.get("idempotency_key"),
                "claim": {
                    field_name: row.get(field_name)
                    for field_name in _RECOVERY_CLAIM_FIELDS
                },
                "session": session_identity,
                "link": {
                    "method": row.get("method"),
                    "confidence": row.get("confidence"),
                    "rule_version": row.get("rule_version"),
                    "validation_state": row.get("validation_state"),
                    "veto_reason": row.get("veto_reason"),
                },
            }
        )
    return result


def _primary_by_member(
    task_rows: list[Mapping[str, object]],
) -> dict[str, Mapping[str, object]]:
    result: dict[str, Mapping[str, object]] = {}
    for task in task_rows:
        primary = task.get("primary")
        members = task.get("members")
        if not isinstance(primary, Mapping) or not isinstance(members, list):
            continue
        for member in members:
            if isinstance(member, Mapping):
                result[_canonical_json(member)] = primary
    return result


def _membership_rows(
    claim_rows: list[Mapping[str, object]],
    *,
    primary_by_member: Mapping[str, Mapping[str, object]],
) -> list[Mapping[str, object]]:
    result: list[Mapping[str, object]] = []
    for row in claim_rows:
        session = row.get("session")
        if not isinstance(session, Mapping):
            continue
        result.append(
            {
                "source": row.get("source"),
                "source_event_id": row.get("source_event_id"),
                "session": session,
                "task_primary": primary_by_member.get(_canonical_json(session)),
            }
        )
    return result


def _normalized_work_summary(item: Mapping[str, object]) -> dict[str, object]:
    return {
        "section_id": item.get("section_id"),
        "title": item.get("title"),
        "latest_status": item.get("latest_status"),
        "summary": item.get("summary"),
        "blocker": item.get("blocker"),
        "next_step": item.get("next_step"),
    }


def _candidate_namespace_token(value: Mapping[str, object]) -> str | None:
    scheme = str(value.get("namespace_scheme") or "")
    digest = str(value.get("namespace_digest") or "")
    return f"{scheme}:{digest}" if scheme and digest else None


def _claim_surface_key(
    source: Mapping[str, object],
    source_event_id: str,
) -> str:
    return _canonical_json(
        {"source": dict(source), "source_event_id": source_event_id}
    )


def _source_event_source_identity(
    event: Mapping[str, Any],
    *,
    snapshot: VerifiedSnapshot,
    source_file: str,
) -> Mapping[str, object]:
    metadata = _metadata(event)
    client = str(
        metadata.get("client")
        or event.get("client")
        or event.get("provider")
        or event.get("source")
        or "legacy"
    ).strip()[:64] or "legacy"
    return {
        "client": client,
        **_legacy_namespace_identity(
            metadata.get("source_namespace_fingerprint"),
            snapshot=snapshot,
            source_file=source_file,
            client=client,
        ),
    }


def _candidate_product_events(
    *,
    candidate_claim_rows: list[Mapping[str, object]],
    source_product_claim_keys: set[str],
) -> list[dict[str, Any]]:
    """Rebuild product inputs using candidate rows, never source Work keys.

    The source event-id set supplies only the lossy surface classification:
    canonical ``completion``/``checkpoint`` claims do not preserve whether the
    raw row was a Task or Section.  Every value that can affect ordering,
    grouping, fallback identity, or the resulting Work is candidate-derived.
    """

    ordered = sorted(
        candidate_claim_rows,
        key=lambda row: (
            int(row.get("occurred_at_us") or 0),
            int(row.get("source_order") or 0),
            _canonical_json(row.get("source")),
            str(row.get("source_event_id") or ""),
        ),
    )
    result: list[dict[str, Any]] = []
    for candidate in ordered:
        event_id = str(candidate.get("source_event_id") or "")
        claim = candidate.get("claim")
        source = candidate.get("source")
        session = candidate.get("session")
        link = candidate.get("link")
        if (
            not event_id
            or not isinstance(claim, Mapping)
            or not isinstance(source, Mapping)
            or _claim_surface_key(source, event_id)
            not in source_product_claim_keys
        ):
            continue
        if isinstance(link, Mapping):
            validation_state = link.get("validation_state")
            if validation_state not in {None, "valid"}:
                # A rejected/pending/conflicting link is not product session
                # context.  Keeping its session would let two identically
                # corrupted candidates agree on a false cohort.
                continue
        section_id = claim.get("section_id")
        if not isinstance(section_id, str) or not section_id:
            # The real product ignores a Section without this identity too.
            continue
        session = session if isinstance(session, Mapping) else {}
        session_client = str(session.get("client") or "")
        session_id = str(session.get("client_session_id") or "")
        reporting_source = str(source.get("client") or "")
        namespace = _candidate_namespace_token(session)
        status = str(claim.get("status") or "reported")
        metadata: dict[str, object] = {
            "sentinel_semantic_kind": "section",
            "section_id": section_id,
            "section_status": status,
            "section_title": claim.get("title"),
            "summary": claim.get("summary"),
            "blocker": claim.get("blocker"),
            "next_step": claim.get("next_step"),
        }
        if session_client:
            metadata["client"] = session_client
        if session_id:
            metadata["client_session_id"] = session_id
            metadata["client_context_keys_authored"] = ["client_session_id"]
        if namespace:
            metadata["session_namespace_fingerprint"] = namespace
            metadata["identity_scope_state"] = "explicit"
        result.append(
            {
                "event_id": event_id,
                "event_type": f"section_{status}",
                "source": reporting_source,
                "created_at": int(candidate.get("occurred_at_us") or 0)
                / 1_000_000,
                "metadata": metadata,
            }
        )
    return result


def _candidate_product_sessions(
    repository: CanonicalRepository,
) -> list[Mapping[str, object]]:
    rows = [
        dict(row)
        for row in repository.connection.execute(
            "SELECT session.session_id, session.client_session_id, "
            "session.session_kind, session.started_at_us, "
            "session.last_activity_at_us, source.client, "
            "source.namespace_scheme, source.namespace_digest, "
            "parent.client_session_id AS parent_client_session_id, "
            "parent_source.namespace_scheme AS parent_namespace_scheme, "
            "parent_source.namespace_digest AS parent_namespace_digest "
            "FROM sessions session JOIN source_instances source "
            "ON source.source_instance_id = session.source_instance_id "
            "LEFT JOIN session_edges edge ON edge.child_session_id = session.session_id "
            "AND edge.validation_state = 'valid' "
            "LEFT JOIN sessions parent ON parent.session_id = edge.parent_session_id "
            "LEFT JOIN source_instances parent_source "
            "ON parent_source.source_instance_id = parent.source_instance_id "
            "ORDER BY session.session_id"
        ).fetchall()
    ]
    result: list[Mapping[str, object]] = []
    for row in rows:
        identity = _candidate_session_identity(row)
        namespace = _candidate_namespace_token(identity)
        parent_digest = row.get("parent_namespace_digest")
        parent_identity = {
            "namespace_scheme": str(row.get("parent_namespace_scheme") or ""),
            "namespace_digest": (
                bytes(parent_digest).hex()
                if isinstance(parent_digest, (bytes, bytearray, memoryview))
                else str(parent_digest or "")
            ),
        }
        parent_namespace = _candidate_namespace_token(parent_identity)
        parent_session_id = str(row.get("parent_client_session_id") or "")
        result.append(
            {
                "client": identity["client"],
                "client_session_id": identity["client_session_id"],
                "session_kind": row.get("session_kind"),
                "first_activity_at": (
                    int(row["started_at_us"]) / 1_000_000
                    if row.get("started_at_us") is not None
                    else None
                ),
                "last_activity_at": (
                    int(row["last_activity_at_us"]) / 1_000_000
                    if row.get("last_activity_at_us") is not None
                    else None
                ),
                "namespace_fingerprint": namespace,
                "source_namespace_fingerprint": namespace,
                "identity_scope_state": "explicit" if namespace else None,
                "related": {
                    "parent": (
                        {
                            "client": identity["client"],
                            "client_session_id": parent_session_id,
                        }
                        if parent_session_id
                        else None
                    )
                },
                "parent_source_namespace_fingerprint": (
                    parent_namespace if parent_session_id else None
                ),
            }
        )
    return result


def _affected_task_work_summary_rows(
    task_projection: Mapping[str, Any],
    *,
    target_work_ids: set[str],
) -> list[Mapping[str, object]]:
    """Compare recovered Work inside affected Tasks, not unsupported siblings."""

    result: list[Mapping[str, object]] = []
    tasks = task_projection.get("tasks")
    for task in tasks if isinstance(tasks, list) else []:
        if not isinstance(task, Mapping):
            continue
        work_items = task.get("work_items")
        if not isinstance(work_items, list):
            continue
        typed_work_items = [
            item for item in work_items if isinstance(item, Mapping)
        ]
        target_items = [
            item
            for item in typed_work_items
            if str(item.get("work_id") or "") in target_work_ids
        ]
        if not target_items:
            continue
        normalized = [_normalized_work_summary(item) for item in target_items]
        primary_ref = task.get("primary_root")
        primary_ref = primary_ref if isinstance(primary_ref, Mapping) else {}
        result.append(
            {
                "primary": {
                    "client": str(primary_ref.get("client") or ""),
                    "client_session_id": str(
                        primary_ref.get("client_session_id") or ""
                    ),
                },
                "work_summaries": sorted(normalized, key=_canonical_json),
            }
        )
    return result


def _target_work_task_occurrences(
    task_projection: Mapping[str, Any],
    *,
    target_work_ids: set[str],
) -> Counter[str]:
    """Count each target Work's appearances across projected Tasks."""

    occurrences: Counter[str] = Counter()
    tasks = task_projection.get("tasks")
    for task in tasks if isinstance(tasks, list) else []:
        if not isinstance(task, Mapping):
            continue
        work_items = task.get("work_items")
        for item in work_items if isinstance(work_items, list) else []:
            if not isinstance(item, Mapping):
                continue
            work_id = str(item.get("work_id") or "")
            if work_id in target_work_ids:
                occurrences[work_id] += 1
    return occurrences


def _recovered_claim_field_rows(
    rows: list[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    return [
        {
            "source": row.get("source"),
            "source_event_id": row.get("source_event_id"),
            "fact_type": row.get("fact_type"),
            "idempotency_scope": row.get("idempotency_scope"),
            "idempotency_key": row.get("idempotency_key"),
            "claim": row.get("claim"),
        }
        for row in rows
    ]


def _count_facts_for_source_event_ids(
    repository: CanonicalRepository,
    event_ids: list[str],
) -> int:
    total = 0
    for offset in range(0, len(event_ids), 400):
        batch = event_ids[offset : offset + 400]
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        total += int(
            repository.connection.execute(
                f"SELECT COUNT(*) FROM facts WHERE source_event_id IN ({placeholders})",
                batch,
            ).fetchone()[0]
        )
    return total


def build_legacy_recovery_product_oracle_report(
    *,
    snapshot: VerifiedSnapshot,
    plan: VerifiedRecoveryPlan,
    repository: CanonicalRepository,
    quarantined_event_ids: list[str] | tuple[str, ...] = (),
    runner_version: int,
) -> dict[str, int | bool]:
    """Compare sealed recovered Work to an independent existing-product view.

    The public result is deliberately counts/booleans only.  Raw event ids,
    paths, content, Task ids, row fingerprints, and manifest digests remain
    inside the owner-only run directory and never enter this report.
    """

    if not isinstance(snapshot, VerifiedSnapshot):
        raise ProductParityError("recovery oracle requires a VerifiedSnapshot")
    if not isinstance(plan, VerifiedRecoveryPlan):
        raise ProductParityError("recovery oracle requires a VerifiedRecoveryPlan")
    if plan.archive.snapshot.manifest_digest != snapshot.manifest_digest:
        raise ProductParityError("recovery oracle plan and snapshot differ")
    if not isinstance(runner_version, int) or isinstance(runner_version, bool) or runner_version < 1:
        raise ProductParityError("recovery oracle runner_version must be positive")
    quarantine_ids = [
        value for value in quarantined_event_ids
        if isinstance(value, str) and value
    ]
    if len(quarantine_ids) != len(quarantined_event_ids):
        raise ProductParityError("quarantine event ids must be non-empty strings")

    snapshot.verify_unchanged()
    plan.verify_unchanged()
    eligible_rows = sum(
        1
        for _locator, disposition in plan.iter_lines()
        if disposition == "candidate_recovery"
    )
    source_claims, source_recovery_events = _source_recovery_claim_rows(plan=plan)
    recovered_task_event_ids = {
        str(event.get("event_id") or "")
        for event in source_recovery_events
        if (
            str(_metadata(event).get("sentinel_semantic_kind") or "").lower()
            == "task"
            or str(event.get("event_type") or "").lower().startswith("task_")
        )
    }
    recovered_section_event_ids = {
        str(event.get("event_id") or "")
        for event in source_recovery_events
        if str(event.get("event_id") or "") not in recovered_task_event_ids
    }
    recovered_task_rows = len(recovered_task_event_ids)
    recovered_section_rows = eligible_rows - recovered_task_rows

    all_events: list[dict[str, Any]] = []
    source_product_claim_keys: set[str] = set()
    malformed_or_non_object = 0
    recovery_identity_by_line = _sealed_recovery_identity_by_line(plan)
    for source in plan.archive.inventory.files:
        source_read = _read_existing_product_events(snapshot, source.relative_path)
        malformed_or_non_object += source_read.malformed_or_non_object
        for raw_event, line_number in zip(
            source_read.events,
            source_read.event_line_numbers,
            strict=True,
        ):
            recovery = recovery_identity_by_line.get(
                (source.relative_path, line_number)
            )
            event = (
                _overlay_sealed_recovery_identity(raw_event, recovery)
                if recovery is not None
                else dict(raw_event)
            )
            event = _normalize_source_scoped_event(event)
            all_events.append(event)
            metadata = _metadata(event)
            event_id = _optional_source_text(event.get("event_id"), limit=512)
            section_id = _optional_source_text(
                metadata.get("section_id"),
                limit=256,
            )
            is_product_section = (
                metadata.get("sentinel_semantic_kind") == "section"
                or str(event.get("event_type") or "").startswith("section_")
            )
            if event_id is None or section_id is None or not is_product_section:
                continue
            source_product_claim_keys.add(
                _claim_surface_key(
                    _source_event_source_identity(
                        event,
                        snapshot=snapshot,
                        source_file=source.relative_path,
                    ),
                    event_id,
                )
            )
    # Recovered facts intentionally use the evidenced donor source rather than
    # the unscoped raw row's fallback source.  Add those sealed source keys as
    # the independent surface classification for the authorized subset.
    source_product_claim_keys.update(
        _claim_surface_key(source, str(row.get("source_event_id") or ""))
        for row in source_claims
        if isinstance((source := row.get("source")), Mapping)
        and str(row.get("source_event_id") or "") in recovered_section_event_ids
    )
    # The existing-product side gets the complete parsed snapshot.  Restricting
    # it to recovered rows would miss an ordinary scoped sibling whose later
    # status changes the Work users actually see.
    source_ledger = build_work_ledger(all_events, store_scope="project")
    recovered_session_keys = {
        (
            str(session.get("client") or ""),
            str(session.get("client_session_id") or ""),
        )
        for row in source_claims
        if isinstance((session := row.get("session")), Mapping)
        and str(session.get("client") or "")
        and str(session.get("client_session_id") or "")
    }
    raw_source_sessions = source_ledger.get("session_rollup", {}).get(
        "sessions", []
    )
    source_task_sessions = _normalized_recovered_lineage_sessions(
        [
            value
            for value in raw_source_sessions
            if isinstance(value, Mapping)
        ] if isinstance(raw_source_sessions, list) else [],
        recovered_session_keys=recovered_session_keys,
    )
    source_task_projection = build_task_projection(
        source_task_sessions,
        source_ledger.get("work_items", []),
    )
    source_file = (
        plan.archive.inventory.files[0].relative_path
        if plan.archive.inventory.files
        else "events.jsonl"
    )
    source_sessions, source_tasks = _source_sessions_and_tasks(
        source_task_projection,
        snapshot=snapshot,
        source_file=source_file,
    )
    del source_sessions
    candidate_sessions, candidate_tasks = _candidate_sessions_and_tasks(repository)
    del candidate_sessions
    source_primary_by_member = _primary_by_member(source_tasks)
    candidate_primary_by_member = _primary_by_member(candidate_tasks)

    candidate_claims = _candidate_claim_rows(repository, recovery_only=True)
    candidate_context_claims = _candidate_claim_rows(repository, recovery_only=False)
    adapter_comparison = _comparison(
        "recovered_adapter_contract",
        source_claims,
        candidate_claims,
    )
    claim_comparison = _comparison(
        "recovered_claim_fields",
        _recovered_claim_field_rows(source_claims),
        _recovered_claim_field_rows(candidate_claims),
    )
    source_memberships = _membership_rows(
        source_claims,
        primary_by_member=source_primary_by_member,
    )
    candidate_memberships = _membership_rows(
        candidate_claims,
        primary_by_member=candidate_primary_by_member,
    )
    membership_comparison = _comparison(
        "recovered_task_membership",
        source_memberships,
        candidate_memberships,
    )

    raw_source_work_events = source_ledger.get("work_events")
    raw_source_work_events = (
        raw_source_work_events if isinstance(raw_source_work_events, list) else []
    )
    source_work_events = [
        value
        for value in raw_source_work_events
        if isinstance(value, Mapping)
    ]
    candidate_product_events = _candidate_product_events(
        candidate_claim_rows=candidate_context_claims,
        source_product_claim_keys=source_product_claim_keys,
    )
    candidate_ledger = build_work_ledger(
        candidate_product_events,
        store_scope="project",
    )
    raw_candidate_work_events = candidate_ledger.get("work_events")
    raw_candidate_work_events = (
        raw_candidate_work_events
        if isinstance(raw_candidate_work_events, list)
        else []
    )
    candidate_work_events = [
        value
        for value in raw_candidate_work_events
        if isinstance(value, Mapping)
    ]
    candidate_recovery_product_event_ids = {
        str(row.get("source_event_id") or "")
        for row in candidate_claims
        if isinstance((source := row.get("source")), Mapping)
        and _claim_surface_key(source, str(row.get("source_event_id") or ""))
        in source_product_claim_keys
    }
    # A sealed candidate_recovery receipt proves this raw event id had exactly
    # one semantic projection.  The classifier routes duplicate ids (including
    # duplicates across manifest files) to no_proof with
    # ``semantic_projection_not_unique`` before this oracle can run.  Ordinary
    # full-context selection above still uses source identity + event id.
    source_target_work_events = [
        event
        for event in source_work_events
        if str(event.get("event_id") or "") in recovered_section_event_ids
    ]
    candidate_target_work_events = [
        event
        for event in candidate_work_events
        if str(event.get("event_id") or "")
        in candidate_recovery_product_event_ids
    ]
    source_target_identity_rows = [
        {
            "source_event_id": event.get("event_id"),
            "work_id": event.get("work_id"),
        }
        for event in source_target_work_events
    ]
    candidate_target_identity_rows = [
        {
            "source_event_id": event.get("event_id"),
            "work_id": event.get("work_id"),
        }
        for event in candidate_target_work_events
    ]
    target_identity_comparison = _comparison(
        "recovered_existing_product_work_identity",
        source_target_identity_rows,
        candidate_target_identity_rows,
    )
    source_target_work_ids = {
        str(event.get("work_id") or "")
        for event in source_target_work_events
        if str(event.get("work_id") or "")
    }
    candidate_target_work_ids = {
        str(event.get("work_id") or "")
        for event in candidate_target_work_events
        if str(event.get("work_id") or "")
    }
    raw_source_work_items = source_ledger.get("work_items")
    raw_source_work_items = (
        raw_source_work_items if isinstance(raw_source_work_items, list) else []
    )
    source_work_summaries = [
        _normalized_work_summary(value)
        for value in raw_source_work_items
        if isinstance(value, Mapping)
        and str(value.get("work_id") or "") in source_target_work_ids
    ]
    raw_candidate_work_items = candidate_ledger.get("work_items")
    raw_candidate_work_items = (
        raw_candidate_work_items
        if isinstance(raw_candidate_work_items, list)
        else []
    )
    candidate_work_summaries = [
        _normalized_work_summary(value)
        for value in raw_candidate_work_items
        if isinstance(value, Mapping)
        and str(value.get("work_id") or "") in candidate_target_work_ids
    ]
    work_comparison = _comparison(
        "recovered_existing_product_work_outputs",
        source_work_summaries,
        candidate_work_summaries,
    )

    candidate_task_projection = build_task_projection(
        _candidate_product_sessions(repository),
        candidate_ledger.get("work_items", []),
    )
    source_task_summaries = _affected_task_work_summary_rows(
        source_task_projection,
        target_work_ids=source_target_work_ids,
    )
    candidate_task_summaries = _affected_task_work_summary_rows(
        candidate_task_projection,
        target_work_ids=candidate_target_work_ids,
    )
    task_comparison = _comparison(
        "recovered_affected_existing_product_task_outputs",
        source_task_summaries,
        candidate_task_summaries,
    )
    source_distinct_target_work_rows = len(source_target_work_ids)
    candidate_distinct_target_work_rows = len(candidate_target_work_ids)
    target_work_identity_presence_conservation = (
        recovered_section_rows == 0
        or (
            source_distinct_target_work_rows > 0
            and candidate_distinct_target_work_rows > 0
        )
    )
    source_work_output_conservation = (
        len(source_work_summaries) == source_distinct_target_work_rows
    )
    candidate_work_output_conservation = (
        len(candidate_work_summaries) == candidate_distinct_target_work_rows
    )
    source_task_occurrences = _target_work_task_occurrences(
        source_task_projection,
        target_work_ids=source_target_work_ids,
    )
    candidate_task_occurrences = _target_work_task_occurrences(
        candidate_task_projection,
        target_work_ids=candidate_target_work_ids,
    )
    source_task_membership_conservation = (
        set(source_task_occurrences) == source_target_work_ids
        and all(count == 1 for count in source_task_occurrences.values())
    )
    candidate_task_membership_conservation = (
        set(candidate_task_occurrences) == candidate_target_work_ids
        and all(count == 1 for count in candidate_task_occurrences.values())
    )
    task_output_comparison_target_only = True

    quarantine_fact_count = _count_facts_for_source_event_ids(
        repository,
        quarantine_ids,
    )
    recovered_fact_count = int(
        repository.connection.execute(
            "SELECT COUNT(*) FROM facts WHERE transport = ?",
            (RECOVERY_FACT_TRANSPORT,),
        ).fetchone()[0]
    )
    source_adapter_conservation = len(source_claims) == eligible_rows
    candidate_adapter_conservation = (
        len(candidate_claims) == eligible_rows == recovered_fact_count
    )
    source_work_covered_rows = len(source_target_work_events)
    candidate_work_covered_rows = len(candidate_target_work_events)
    source_context_rows = sum(
        1
        for event in source_work_events
        if str(event.get("work_id") or "") in source_target_work_ids
    )
    candidate_context_rows = sum(
        1
        for event in candidate_work_events
        if str(event.get("work_id") or "") in candidate_target_work_ids
    )
    coverage_conservation = (
        recovered_section_rows + recovered_task_rows == eligible_rows
        and source_work_covered_rows == recovered_section_rows
        and candidate_work_covered_rows == recovered_section_rows
    )
    context_rows_match = source_context_rows == candidate_context_rows
    quarantine_zero_contribution = quarantine_fact_count == 0
    passed = all(
        (
            source_adapter_conservation,
            candidate_adapter_conservation,
            coverage_conservation,
            context_rows_match,
            target_work_identity_presence_conservation,
            source_work_output_conservation,
            candidate_work_output_conservation,
            source_task_membership_conservation,
            candidate_task_membership_conservation,
            task_output_comparison_target_only,
            bool(adapter_comparison["matches"]),
            bool(claim_comparison["matches"]),
            bool(membership_comparison["matches"]),
            bool(target_identity_comparison["matches"]),
            bool(work_comparison["matches"]),
            bool(task_comparison["matches"]),
            quarantine_zero_contribution,
        )
    )
    snapshot.verify_unchanged()
    plan.verify_unchanged()
    return {
        "contract_version": RECOVERY_PRODUCT_ORACLE_CONTRACT_VERSION,
        "oracle_code_version": RECOVERY_PRODUCT_ORACLE_CODE_VERSION,
        "runner_version": runner_version,
        "source_manifest_verified": True,
        "existing_product_functions_used": True,
        "source_malformed_or_non_object_rows": malformed_or_non_object,
        "eligible_recovery_rows": eligible_rows,
        "recovered_section_rows": recovered_section_rows,
        "adapter_membership_only_task_rows": recovered_task_rows,
        "all_recovered_rows_have_existing_product_work_output_coverage": (
            recovered_task_rows == 0
        ),
        "source_adapter_rows": len(source_claims),
        "candidate_adapter_rows": len(candidate_claims),
        "recovery_transport_fact_rows": recovered_fact_count,
        "source_adapter_conservation": source_adapter_conservation,
        "candidate_adapter_conservation": candidate_adapter_conservation,
        "adapter_contract_match": bool(adapter_comparison["matches"]),
        "adapter_contract_mismatch_count": int(
            adapter_comparison["mismatch_count"]
        ),
        "claim_fields_match": bool(claim_comparison["matches"]),
        "claim_field_mismatch_count": int(claim_comparison["mismatch_count"]),
        "task_membership_match": bool(membership_comparison["matches"]),
        "task_membership_mismatch_count": int(membership_comparison["mismatch_count"]),
        "source_full_context_work_event_rows": len(source_work_events),
        "candidate_full_context_work_event_rows": len(candidate_work_events),
        "source_existing_product_work_context_rows": source_context_rows,
        "candidate_existing_product_work_context_rows": candidate_context_rows,
        "existing_product_work_context_rows_match": context_rows_match,
        "existing_product_work_covered_recovery_rows": source_work_covered_rows,
        "candidate_existing_product_work_covered_recovery_rows": (
            candidate_work_covered_rows
        ),
        "coverage_conservation": coverage_conservation,
        "target_work_identities_match": bool(
            target_identity_comparison["matches"]
        ),
        "target_work_identity_mismatch_count": int(
            target_identity_comparison["mismatch_count"]
        ),
        "source_distinct_target_work_identity_rows": (
            source_distinct_target_work_rows
        ),
        "candidate_distinct_target_work_identity_rows": (
            candidate_distinct_target_work_rows
        ),
        "target_work_identity_presence_conservation": (
            target_work_identity_presence_conservation
        ),
        "source_target_work_output_rows": len(source_work_summaries),
        "candidate_target_work_output_rows": len(candidate_work_summaries),
        "source_work_output_conservation": source_work_output_conservation,
        "candidate_work_output_conservation": candidate_work_output_conservation,
        "work_outputs_match": bool(work_comparison["matches"]),
        "work_output_mismatch_count": int(work_comparison["mismatch_count"]),
        "source_affected_task_output_rows": len(source_task_summaries),
        "candidate_affected_task_output_rows": len(candidate_task_summaries),
        "source_target_work_affected_task_occurrence_rows": sum(
            source_task_occurrences.values()
        ),
        "candidate_target_work_affected_task_occurrence_rows": sum(
            candidate_task_occurrences.values()
        ),
        "source_target_work_task_membership_conservation": (
            source_task_membership_conservation
        ),
        "candidate_target_work_task_membership_conservation": (
            candidate_task_membership_conservation
        ),
        "task_output_comparison_target_only": (
            task_output_comparison_target_only
        ),
        "task_outputs_match": bool(task_comparison["matches"]),
        "task_output_mismatch_count": int(task_comparison["mismatch_count"]),
        "quarantine_event_ids_checked": len(quarantine_ids),
        "quarantine_canonical_fact_rows": quarantine_fact_count,
        "quarantine_zero_contribution": quarantine_zero_contribution,
        "passed": passed,
    }


def _migration_issue_summary(repository: CanonicalRepository) -> list[dict[str, object]]:
    return [
        {
            "reason": str(row[0]),
            "disposition": str(row[1]),
            "count": int(row[2]),
        }
        for row in repository.connection.execute(
            "SELECT reason, disposition, SUM(count) FROM migration_issues "
            "GROUP BY reason, disposition ORDER BY reason, disposition"
        ).fetchall()
    ]


def build_legacy_product_parity_report(
    *,
    snapshot: VerifiedSnapshot,
    repository: CanonicalRepository,
    migration: MigrationReport,
    source_file: str = "events.jsonl",
    legacy_store_scope: str = "custom",
    rerun_evidence: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Compare existing product outputs to canonical core truth, read-only."""

    if not isinstance(snapshot, VerifiedSnapshot):
        raise ProductParityError("snapshot must be a VerifiedSnapshot")
    if snapshot.kind not in SUPPORTED_LEGACY_MANIFEST_KINDS:
        raise ProductParityError(
            "product parity requires a legacy or legacy-chronicle manifest"
        )
    if legacy_store_scope not in {"project", "custom"}:
        raise ProductParityError("legacy_store_scope must be project or custom")
    snapshot.verify_unchanged()
    source = _read_existing_product_events(snapshot, source_file)
    ledger = build_work_ledger(
        list(source.events),
        store_scope=legacy_store_scope,
    )
    task_projection = build_task_projection(
        ledger.get("session_rollup", {}).get("sessions", []),
        ledger.get("work_items", []),
    )

    source_sessions, source_tasks = _source_sessions_and_tasks(
        task_projection,
        snapshot=snapshot,
        source_file=source_file,
    )
    candidate_sessions, candidate_tasks = _candidate_sessions_and_tasks(repository)
    source_presence, source_aggregates, source_total_only_fallback_rows = _source_usage_rows(
        ledger,
        source.events,
        snapshot=snapshot,
        source_file=source_file,
    )
    (
        candidate_presence,
        candidate_aggregates,
        candidate_compatibility_reprojected_rows,
    ) = _candidate_usage_rows(repository)
    comparisons = [
        _comparison("sessions", source_sessions, candidate_sessions),
        _comparison("task_membership", source_tasks, candidate_tasks),
        _comparison("usage_presence", source_presence, candidate_presence),
        _comparison("usage_aggregates", source_aggregates, candidate_aggregates),
    ]
    snapshot.verify_unchanged()

    quick = repository.store.quick_check()
    foreign_keys = repository.connection.execute("PRAGMA foreign_key_check").fetchall()
    issues = _migration_issue_summary(repository)
    visible_issue_count = sum(int(row["count"]) for row in issues)
    exclusions_visible = visible_issue_count == migration.migration_issue_count
    migration_disposition_policy = build_migration_disposition_policy_evidence(
        snapshot_manifest_sha256=snapshot.manifest_digest,
        issues=issues,
        source_lines_seen=source.lines_seen,
        importer_lines_seen=migration.lines_seen,
        parsed_events=migration.parsed_events,
        malformed_or_excluded_lines=migration.malformed_or_excluded_lines,
        issue_lines=migration.issue_lines,
        excluded_lines=migration.excluded_lines,
        processed_with_issues_lines=migration.processed_with_issues_lines,
        migration_issue_count=migration.migration_issue_count,
    )
    approved_exclusion_count = int(
        migration_disposition_policy["approved_exclusion_count"]
    )
    unapproved_exclusion_count = int(
        migration_disposition_policy["unapproved_exclusion_count"]
    )
    migration_policy_applied = bool(
        migration_disposition_policy["source_line_conservation"] is True
        and migration_disposition_policy["approved_exclusion_conservation"] is True
        and not migration_disposition_policy["unapproved_exclusions"]
        and unapproved_exclusion_count == 0
    )
    unresolved_hard_conflicts = int(
        migration.parity.differences.get("unresolved_hard_conflicts", {}).get(
            "source",
            0,
        )
        if isinstance(
            migration.parity.differences.get("unresolved_hard_conflicts"),
            Mapping,
        )
        else 0
    )
    compatibility_projection_counts_match = bool(
        source_total_only_fallback_rows
        == candidate_compatibility_reprojected_rows
    )
    exact_supported_truth = compatibility_projection_counts_match and all(
        bool(comparison["matches"])
        for comparison in comparisons
        if comparison["required_core"] is True
    )
    rerun = dict(rerun_evidence or {})
    second_import = rerun.get("second_import")
    second_import_dispositions = (
        second_import.get("write_dispositions")
        if isinstance(second_import, Mapping)
        else None
    )
    second_import_noop_only = bool(
        isinstance(second_import_dispositions, Mapping)
        and all(
            isinstance(dispositions, Mapping)
            and set(dispositions).issubset({"noop"})
            for dispositions in second_import_dispositions.values()
        )
    )
    rerun_passed = bool(
        rerun.get("canonical_writes") == 0
        and rerun.get("canonical_sequence_delta") == 0
        and rerun.get("task_ids_stable") is True
        and rerun.get("opaque_task_ids_valid") is True
        and rerun.get("table_counts_stable") is True
        and rerun.get("projection_rebuilt") is False
        and isinstance(second_import, Mapping)
        and second_import.get("internal_parity_matches") is True
        and not second_import.get("internal_parity_difference_keys")
        and second_import_noop_only
    )
    core_truth_slice_passed = bool(
        quick.get("ok")
        and not foreign_keys
        and migration.snapshot_manifest_sha256 == snapshot.manifest_digest
        and migration.parity.matches
        and exact_supported_truth
        and unresolved_hard_conflicts == 0
        and exclusions_visible
        and migration_policy_applied
        and rerun_passed
    )
    product_scope_complete = False
    cutover_gate_passed = bool(
        core_truth_slice_passed
        and product_scope_complete
        and not issues
    )
    return {
        "schema_version": PRODUCT_PARITY_SCHEMA_VERSION,
        "snapshot": {
            "kind": snapshot.kind,
            "manifest_sha256": snapshot.manifest_digest,
            "file_count": len(snapshot.files),
            "bytes": sum(item.size_bytes for item in snapshot.files),
            "verified_before": True,
            "verified_after": True,
        },
        "existing_product": {
            "work_ledger_schema_version": ledger.get("schema_version"),
            "task_projection_schema_version": task_projection.get("schema_version"),
            "store_scope": legacy_store_scope,
            "lines_seen": source.lines_seen,
            "parsed_object_events": len(source.events),
            "malformed_or_non_object": source.malformed_or_non_object,
        },
        "usage_aggregate_parity_basis": {
            "schema_version": _USAGE_AGGREGATE_PARITY_BASIS_VERSION,
            "surface": "existing-product-output",
            "compatibility_projection": (
                "codex-sqlite-total-only-fallback-as-legacy-input-and-total"
            ),
            "source_total_only_fallback_rows": source_total_only_fallback_rows,
            "candidate_compatibility_reprojected_rows": (
                candidate_compatibility_reprojected_rows
            ),
            "counts_match": compatibility_projection_counts_match,
            "canonical_missingness_surface": "usage_presence",
        },
        "candidate": {
            "quick_check": quick,
            "foreign_key_issue_count": len(foreign_keys),
            "table_counts": dict(repository.table_counts()),
        },
        "migration": {
            "lines_seen": migration.lines_seen,
            "parsed_events": migration.parsed_events,
            "malformed_or_excluded_lines": migration.malformed_or_excluded_lines,
            "issue_lines": migration.issue_lines,
            "excluded_lines": migration.excluded_lines,
            "processed_with_issues_lines": migration.processed_with_issues_lines,
            "migration_issue_count": migration.migration_issue_count,
            "write_dispositions": migration.write_dispositions,
            "internal_parity_matches": migration.parity.matches,
            "internal_parity_difference_keys": sorted(migration.parity.differences),
        },
        "comparisons": comparisons,
        "exclusions": issues,
        "migration_disposition_policy": migration_disposition_policy,
        "issue_summary": {
            "issue_instances": visible_issue_count,
            "affected_lines": migration.issue_lines,
            "excluded_lines": migration.excluded_lines,
            "processed_with_issues_lines": migration.processed_with_issues_lines,
        },
        "rerun": rerun,
        "unsupported_surfaces": [
            "continuation_store_memberships",
            "full_task_detail_steps_joins_outcomes_findings",
            "checks_and_artifacts",
            "complete_sessions_json_contract",
            "full_work_and_usage_endpoint_shapes",
        ],
        "acceptance": {
            "manifest_integrity": True,
            "candidate_integrity": bool(quick.get("ok") and not foreign_keys),
            "exact_supported_truth": exact_supported_truth,
            "zero_unresolved_hard_conflicts": unresolved_hard_conflicts == 0,
            "exclusions_visible": exclusions_visible,
            "migration_policy_applied": migration_policy_applied,
            "source_line_conservation": migration_disposition_policy[
                "source_line_conservation"
            ],
            "approved_exclusion_count": approved_exclusion_count,
            "approved_issue_instance_count": approved_exclusion_count,
            "unapproved_exclusion_count": unapproved_exclusion_count,
            "unapproved_issue_instance_count": unapproved_exclusion_count,
            "affected_issue_line_count": migration.issue_lines,
            "rerun_zero_write_and_stable_ids": rerun_passed,
            "core_truth_slice_passed": core_truth_slice_passed,
            "product_scope_complete": product_scope_complete,
            "cutover_gate_passed": cutover_gate_passed,
        },
        "decision": "go-core-truth-slice" if core_truth_slice_passed else "no-go",
        "cutover_decision": "no-go",
    }


__all__ = [
    "PRODUCT_PARITY_SCHEMA_VERSION",
    "RECOVERY_PRODUCT_ORACLE_CODE_VERSION",
    "RECOVERY_PRODUCT_ORACLE_CONTRACT_VERSION",
    "ProductParityError",
    "build_legacy_product_parity_report",
    "build_legacy_recovery_product_oracle_report",
]
