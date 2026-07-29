"""Pure identity and revision helpers for refreshable local usage facts.

The v1 ledger gives every replacement row a new ``event_id`` and arrival
timestamp.  Those fields are receipt provenance, not the identity or content
of a cumulative client-usage snapshot.  This module deliberately performs no
I/O and does not implement persistence; it only defines the stable material a
store can use for an ordered, current-state reconcile lane.

The ``usage_source`` and ``usage_provenance`` markers are reserved and stamped
only by trusted importer writes.  Functions here reuse that complete stored
trust gate, while still rejecting non-usage and non-cumulative event shapes.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Mapping

from .usage_truth import (
    CODEX_LINEAGE_DELTA_SEMANTICS,
    USAGE_ROW_LANE_PREFIX,
    is_local_usage_import_event,
    local_usage_event_additivity,
    local_usage_row_lane,
    normalized_local_usage_session_id,
    sanitize_session_key_component,
)


REFRESHABLE_USAGE_SLOT_SCHEMA = "agent-chronicle.refreshable-usage-slot.v1"
REFRESHABLE_USAGE_TRUTH_SCHEMA = "agent-chronicle.refreshable-usage-truth.v1"
REFRESHABLE_USAGE_REVISION_SCHEMA = "agent-chronicle.refreshable-usage-revision.v1"
REFRESHABLE_USAGE_USAGE_MATERIAL_SCHEMA = (
    "agent-chronicle.refreshable-usage-usage-material.v1"
)

CANONICAL_CUMULATIVE_USAGE_SEMANTICS = "cumulative_snapshot"
DEFAULT_USAGE_REPRESENTATION = "legacy-v1"
DEFAULT_USAGE_LANE = "session_total"
UNRESOLVED_SOURCE_NAMESPACE = "unresolved-local-usage-source-v1"
_SQLITE_INT64_MAX = 2**63 - 1

# Frozen stored-data vocabulary emitted by the supported local importers.  All
# of these describe a refreshable cumulative snapshot once normalized.  Keep
# this public so adapter/rebuild tests can prove the allowlist rather than
# accidentally broadening it with a substring or default-value match.
CUMULATIVE_USAGE_SEMANTICS = frozenset(
    {
        CANONICAL_CUMULATIVE_USAGE_SEMANTICS,
        "codex_rollout_token_count_events",
        "codex_sqlite_tokens_used_fallback",
        CODEX_LINEAGE_DELTA_SEMANTICS,
        "claude_assistant_message_usage_rows",
        "opencode_step_finish_events",
        "opencode_session_rollup",
        "hermes_state_db_session_rows",
        "openclaw_assistant_usage_rows",
    }
)

NamespaceKind = Literal["explicit", "unresolved"]


@dataclass(frozen=True)
class RefreshableUsageSlot:
    """Stable current-state slot for one cumulative usage measurement lane."""

    namespace_kind: NamespaceKind
    source_namespace: str
    client: str
    client_session_id: str
    lane: str
    representation: str
    update_semantics: str = CANONICAL_CUMULATIVE_USAGE_SEMANTICS
    model_discriminator: str | None = None

    def to_dict(self) -> dict[str, str]:
        result = {
            "schema": REFRESHABLE_USAGE_SLOT_SCHEMA,
            "namespace_kind": self.namespace_kind,
            "source_namespace": self.source_namespace,
            "client": self.client,
            "client_session_id": self.client_session_id,
            "lane": self.lane,
            "representation": self.representation,
            "update_semantics": self.update_semantics,
        }
        if self.model_discriminator is not None:
            result["model_discriminator"] = self.model_discriminator
        return result


def _metadata(event: Mapping[str, Any]) -> Mapping[str, Any]:
    value = event.get("metadata")
    return value if isinstance(value, Mapping) else {}


def _plain_event(event: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(event)
    result["metadata"] = dict(_metadata(event))
    return result


def _stripped_model(event: Mapping[str, Any]) -> str:
    raw_model = event.get("model")
    return raw_model.strip() if isinstance(raw_model, str) and raw_model.strip() else "unknown"


def _model_discriminator(model: str) -> str:
    return "sha256:" + hashlib.sha256(model.encode("utf-8")).hexdigest()


def _valid_source_namespace_fingerprint(value: str) -> bool:
    if not value.startswith("sha256:") or len(value) != len("sha256:") + 64:
        return False
    digest = value.removeprefix("sha256:")
    return all(character in "0123456789abcdef" for character in digest)


def _timestamp_to_us(value: object) -> int | None:
    """Normalize legacy source timestamps without importing canonical code.

    Evidence v2 is independently feature-gated; importing it must not load the
    canonical package when canonical live-write is disabled. Keep this small
    conversion byte-for-byte compatible with canonical's seconds/ms/us rules.
    """

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number) or number < 0:
            return None
        if number >= 10**14:
            if number > _SQLITE_INT64_MAX:
                return None
            result = int(number)
        elif number >= 10**11:
            if number > _SQLITE_INT64_MAX / 1_000:
                return None
            result = int(number * 1_000)
        else:
            if number > _SQLITE_INT64_MAX / 1_000_000:
                return None
            result = int(number * 1_000_000)
        return result if result <= _SQLITE_INT64_MAX else None
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
        return result if result <= _SQLITE_INT64_MAX else None
    return None


def normalized_refreshable_usage_semantics(
    event: Mapping[str, Any],
) -> str | None:
    """Return the canonical cumulative semantic, or ``None`` if unsupported.

    In particular, ``atomic_increment`` is never allowed into this mutable
    current-state lane.  It requires a separate immutable native-event key.
    """

    metadata = _metadata(event)
    raw = metadata.get("usage_update_semantics")
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().lower()
    if normalized not in CUMULATIVE_USAGE_SEMANTICS:
        return None
    return CANONICAL_CUMULATIVE_USAGE_SEMANTICS


def is_trusted_refreshable_local_usage(event: Mapping[str, Any]) -> bool:
    """Whether a stored event is trusted, local, and cumulatively refreshable."""

    return (
        is_local_usage_import_event(_plain_event(event))
        and normalized_refreshable_usage_semantics(event)
        == CANONICAL_CUMULATIVE_USAGE_SEMANTICS
    )


def refreshable_usage_slot_identity(
    event: Mapping[str, Any],
    *,
    unresolved_namespace: str = UNRESOLVED_SOURCE_NAMESPACE,
) -> RefreshableUsageSlot | None:
    """Build a namespace-safe stable slot, or ``None`` for an unsafe event.

    The explicit/unresolved discriminator is part of identity. Explicit
    fingerprints must be canonical lowercase SHA-256 values; missing values
    use a separate store-scoped unresolved sentinel. Claude slots additionally
    bind a bounded digest of the full stripped model so legacy lane sanitation
    and truncation cannot merge different models.
    """

    if not is_trusted_refreshable_local_usage(event):
        return None
    metadata = _metadata(event)
    raw_client = metadata.get("client")
    raw_session = metadata.get("client_session_id")
    if not isinstance(raw_client, str) or not raw_client.strip():
        return None
    if not isinstance(raw_session, str) or not raw_session.strip():
        return None

    client = raw_client.strip()
    session_id = normalized_local_usage_session_id(client, raw_session.strip())
    if not session_id:
        return None

    explicit_namespace = metadata.get("source_namespace_fingerprint")
    if explicit_namespace not in (None, ""):
        if not isinstance(explicit_namespace, str) or not _valid_source_namespace_fingerprint(
            explicit_namespace
        ):
            raise ValueError(
                "source_namespace_fingerprint must be sha256 followed by 64 lowercase hex characters"
            )
        namespace_kind: NamespaceKind = "explicit"
        source_namespace = explicit_namespace
    else:
        if not isinstance(unresolved_namespace, str) or not unresolved_namespace.strip():
            raise ValueError("unresolved_namespace must be a non-empty string")
        namespace_kind = "unresolved"
        source_namespace = unresolved_namespace.strip()

    lane = local_usage_row_lane(_plain_event(event)) or DEFAULT_USAGE_LANE
    lane = lane.strip() if isinstance(lane, str) and lane.strip() else DEFAULT_USAGE_LANE
    model_discriminator: str | None = None
    if client == "claude-code":
        model = _stripped_model(event)
        raw_explicit_lane = metadata.get("usage_row_lane")
        if raw_explicit_lane is not None:
            if not isinstance(raw_explicit_lane, str) or not raw_explicit_lane.strip():
                raise ValueError("claude-code usage_row_lane must be a non-empty string")
            expected_lane = USAGE_ROW_LANE_PREFIX + sanitize_session_key_component(model)
            if raw_explicit_lane.strip() != expected_lane:
                raise ValueError(
                    "claude-code usage_row_lane does not match the exact model"
                )
        model_discriminator = _model_discriminator(model)
    raw_representation = metadata.get("usage_representation")
    representation = (
        raw_representation.strip()
        if isinstance(raw_representation, str) and raw_representation.strip()
        else DEFAULT_USAGE_REPRESENTATION
    )
    return RefreshableUsageSlot(
        namespace_kind=namespace_kind,
        source_namespace=source_namespace,
        client=client,
        client_session_id=session_id,
        lane=lane,
        representation=representation,
        model_discriminator=model_discriminator,
    )


def refreshable_usage_source_order(
    event: Mapping[str, Any],
) -> int | None:
    """Return the best client-source order without using agentacct arrival time.

    ``source_revision_at`` is the highest-resolution source watermark, followed
    by ``source_updated_at`` and the legacy importer ``updated_at`` field.  The
    top-level server-owned ``created_at`` is intentionally never consulted.
    """

    if not is_trusted_refreshable_local_usage(event):
        return None
    metadata = _metadata(event)
    for key in ("source_revision_at", "source_updated_at", "updated_at"):
        normalized = _timestamp_to_us(metadata.get(key))
        if normalized is not None and normalized > 0:
            return normalized
    return None


_TOKEN_FIELDS: tuple[
    tuple[str, Literal["event", "metadata"], str, tuple[str, ...]], ...
] = (
    ("input_tokens", "event", "estimated_input_tokens", ("input_tokens_reported",)),
    ("output_tokens", "event", "estimated_output_tokens", ("output_tokens_reported",)),
    ("cached_input_tokens", "metadata", "cached_input_tokens", ("cached_input_tokens_reported",)),
    (
        "cache_creation_input_tokens",
        "metadata",
        "cache_creation_input_tokens",
        ("cache_creation_input_tokens_reported", "cache_creation_tokens_reported"),
    ),
    (
        "cache_read_input_tokens",
        "metadata",
        "cache_read_input_tokens",
        ("cache_read_input_tokens_reported", "cache_read_tokens_reported"),
    ),
    (
        "reasoning_output_tokens",
        "metadata",
        "reasoning_output_tokens",
        ("reasoning_output_tokens_reported", "reasoning_tokens_reported"),
    ),
    ("total_tokens", "metadata", "total_tokens", ("total_tokens_reported",)),
)


def _reported_flag(
    metadata: Mapping[str, Any], keys: tuple[str, ...]
) -> tuple[bool | None, bool]:
    values: list[bool] = []
    for key in keys:
        if key in metadata:
            value = metadata.get(key)
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be a bool when present")
            values.append(value)
    if not values:
        return None, False
    if any(value != values[0] for value in values[1:]):
        raise ValueError("reported-presence aliases disagree")
    return values[0], True


def _token_fact(
    event: Mapping[str, Any],
    metadata: Mapping[str, Any],
    *,
    container_name: Literal["event", "metadata"],
    key: str,
    reported_keys: tuple[str, ...],
    field_name: str,
) -> dict[str, int | bool | None]:
    container = event if container_name == "event" else metadata
    explicit_reported, explicit_found = _reported_flag(metadata, reported_keys)

    # ClientUsageEvent has historically carried a compatibility cached-input
    # value even when neither split cache counter existed.  Mirror canonical's
    # source-presence rule so a compatibility zero is not promoted to truth.
    if field_name == "cached_input_tokens" and not explicit_found:
        split_flags = [
            flag
            for keys in (
                ("cache_creation_input_tokens_reported", "cache_creation_tokens_reported"),
                ("cache_read_input_tokens_reported", "cache_read_tokens_reported"),
            )
            for flag, found in [_reported_flag(metadata, keys)]
            if found and flag is not None
        ]
        if any(split_flags):
            explicit_reported = True
        elif split_flags:
            explicit_reported = False

    present = key in container and container.get(key) is not None
    reported = explicit_reported if explicit_reported is not None else present
    raw_value = container.get(key)
    valid_value = (
        isinstance(raw_value, int)
        and not isinstance(raw_value, bool)
        and raw_value >= 0
    )
    if reported and not valid_value:
        raise ValueError(
            f"reported {field_name} must be a non-negative integer and not a bool"
        )
    value = raw_value if reported else None
    return {"reported": bool(reported), "value": value}


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_nonnegative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _decimal_text(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    try:
        as_float = float(parsed)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(as_float):
        return None
    if parsed == 0:
        parsed = Decimal(0)
    text = format(parsed, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _client_reported_cost(
    event: Mapping[str, Any], metadata: Mapping[str, Any]
) -> dict[str, str | None]:
    raw_cost = metadata.get("client_reported_cost_usd")
    cost_confidence = _optional_text(event.get("cost_confidence"))
    normalized_cost = _decimal_text(raw_cost)
    if raw_cost is not None and normalized_cost is None:
        raise ValueError(
            "client_reported_cost_usd must be a finite non-negative number"
        )
    if normalized_cost is not None and cost_confidence != "client_reported":
        raise ValueError(
            "client_reported_cost_usd requires cost_confidence=client_reported"
        )
    return {
        "usd": normalized_cost,
        "confidence": cost_confidence if normalized_cost is not None else None,
        "source": (
            _optional_text(metadata.get("client_cost_source"))
            if normalized_cost is not None
            else None
        ),
    }


def _evidence_links(metadata: Mapping[str, Any]) -> dict[str, object]:
    raw_ids = metadata.get("evidenced_event_ids")
    if isinstance(raw_ids, (list, tuple, set, frozenset)):
        event_ids = sorted(
            {
                value.strip()
                for value in raw_ids
                if isinstance(value, str) and value.strip()
            }
        )
    else:
        event_ids = []
    total = _optional_nonnegative_int(metadata.get("evidenced_event_id_total"))
    skipped = _optional_nonnegative_int(metadata.get("evidenced_outputs_skipped"))
    return {
        "event_ids": event_ids,
        "event_id_total": len(event_ids) if total is None else total,
        "outputs_skipped": 0 if skipped is None else skipped,
    }


def refreshable_usage_truth_material(
    event: Mapping[str, Any],
) -> dict[str, object] | None:
    """Return normalized mutable truth, excluding receipt and pricing churn.

    The material is a strict allowlist.  It therefore excludes ``event_id``,
    ``created_at``, polling/source-order timestamps, top-level derived price,
    and all ``pricing_*`` metadata.  Client-reported cost and client-log
    evidence links remain content because they are source facts/provenance.
    """

    if not is_trusted_refreshable_local_usage(event):
        return None
    metadata = _metadata(event)
    usage_confidence = _optional_text(event.get("usage_confidence"))
    if usage_confidence != "client_reported":
        raise ValueError("refreshable usage_confidence must be client_reported")
    tokens = {
        field_name: _token_fact(
            event,
            metadata,
            container_name=container_name,
            key=key,
            reported_keys=reported_keys,
            field_name=field_name,
        )
        for field_name, container_name, key, reported_keys in _TOKEN_FIELDS
    }

    additive, inferred_held_reason = local_usage_event_additivity(_plain_event(event))
    held_reason = _optional_text(
        metadata.get("usage_held_reason")
        or metadata.get("held_reason")
        or inferred_held_reason
    )
    raw_precedence = metadata.get("precedence_role")
    if raw_precedence is None:
        precedence = "authoritative"
    elif not isinstance(raw_precedence, str) or not raw_precedence.strip():
        raise ValueError(
            "precedence_role must be authoritative, fallback, or enrichment"
        )
    else:
        precedence = raw_precedence.strip().lower()
    if precedence not in {
        "authoritative",
        "fallback",
        "enrichment",
    }:
        raise ValueError(
            "precedence_role must be authoritative, fallback, or enrichment"
        )
    if precedence == "enrichment" and additive:
        additive = False
        held_reason = held_reason or "enrichment_not_totals_eligible"

    return {
        "schema": REFRESHABLE_USAGE_TRUTH_SCHEMA,
        "provider": _optional_text(event.get("provider")),
        "model": _optional_text(event.get("model")),
        "usage_confidence": usage_confidence,
        "tokens": tokens,
        "usage_additive": additive,
        "usage_normalization_state": _optional_text(
            metadata.get("usage_normalization_state")
        ),
        "held_reason": held_reason,
        "precedence_role": precedence,
        "client_reported_cost": _client_reported_cost(event, metadata),
        "evidence": _evidence_links(metadata),
    }


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def refreshable_usage_slot_digest(slot: RefreshableUsageSlot) -> str:
    """Stable digest of a slot identity."""

    return _digest(slot.to_dict())


def refreshable_usage_truth_digest(event: Mapping[str, Any]) -> str | None:
    """Stable digest of normalized truth, independent of poll/receipt churn."""

    material = refreshable_usage_truth_material(event)
    return _digest(material) if material is not None else None


def _plain_jsonable(value: object) -> object:
    """Deep-convert mapping proxies/tuples from stored envelopes to plain JSON."""

    if isinstance(value, Mapping):
        return {str(key): _plain_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_jsonable(item) for item in value]
    return value


def refreshable_usage_truth_digest_from_material(
    material: Mapping[str, Any],
) -> str:
    """Digest of an already-normalized truth-material mapping.

    Used by rebuild verification to re-bind a stored envelope's truth payload
    to its recorded content hash without reconstructing a v1 event.
    """

    return _digest(_plain_jsonable(material))


def refreshable_usage_usage_material_from_truth(
    material: Mapping[str, Any],
) -> dict[str, object]:
    """Usage-material-only projection of truth: evidence links removed.

    Client-log evidence links are provenance that legitimately grows while a
    lane's usage stays frozen (a multi-model session keeps appending turns for
    other lanes). Same-slot audits at equal source order must not treat that
    drift as usage divergence; everything else in the truth material remains
    comparable byte-for-byte. The projection carries its own schema tag so its
    digests can never be confused with full-truth digests.
    """

    projected = {
        str(key): _plain_jsonable(value)
        for key, value in material.items()
        if key != "evidence"
    }
    projected["schema"] = REFRESHABLE_USAGE_USAGE_MATERIAL_SCHEMA
    return projected


def refreshable_usage_usage_material_digest_from_material(
    material: Mapping[str, Any],
) -> str:
    """Digest of the usage-material-only projection of a truth mapping."""

    return _digest(refreshable_usage_usage_material_from_truth(material))


def refreshable_usage_usage_material_digest(event: Mapping[str, Any]) -> str | None:
    """Digest of the usage-material-only projection of an event's truth."""

    material = refreshable_usage_truth_material(event)
    if material is None:
        return None
    return refreshable_usage_usage_material_digest_from_material(material)


def stable_refreshable_usage_revision_id(
    slot: RefreshableUsageSlot, truth_digest: str
) -> str:
    """Derive an immutable revision ID from stable slot and truth material."""

    if not (
        isinstance(truth_digest, str)
        and truth_digest.startswith("sha256:")
        and len(truth_digest) == 71
    ):
        raise ValueError("truth_digest must be a sha256 digest")
    try:
        bytes.fromhex(truth_digest.removeprefix("sha256:"))
    except ValueError as exc:
        raise ValueError("truth_digest must be a sha256 digest") from exc
    digest = _digest(
        {
            "schema": REFRESHABLE_USAGE_REVISION_SCHEMA,
            "slot_digest": refreshable_usage_slot_digest(slot),
            "truth_digest": truth_digest,
        }
    )
    return "rurev_" + digest.removeprefix("sha256:")


def refreshable_usage_revision_id(
    event: Mapping[str, Any],
    *,
    unresolved_namespace: str = UNRESOLVED_SOURCE_NAMESPACE,
) -> str | None:
    """Convenience wrapper deriving a stable revision ID from one v1 event."""

    slot = refreshable_usage_slot_identity(
        event, unresolved_namespace=unresolved_namespace
    )
    truth_digest = refreshable_usage_truth_digest(event)
    if slot is None or truth_digest is None:
        return None
    return stable_refreshable_usage_revision_id(slot, truth_digest)


__all__ = [
    "CANONICAL_CUMULATIVE_USAGE_SEMANTICS",
    "CUMULATIVE_USAGE_SEMANTICS",
    "DEFAULT_USAGE_LANE",
    "DEFAULT_USAGE_REPRESENTATION",
    "REFRESHABLE_USAGE_REVISION_SCHEMA",
    "REFRESHABLE_USAGE_SLOT_SCHEMA",
    "REFRESHABLE_USAGE_TRUTH_SCHEMA",
    "RefreshableUsageSlot",
    "UNRESOLVED_SOURCE_NAMESPACE",
    "is_trusted_refreshable_local_usage",
    "normalized_refreshable_usage_semantics",
    "refreshable_usage_revision_id",
    "refreshable_usage_slot_digest",
    "refreshable_usage_slot_identity",
    "refreshable_usage_source_order",
    "refreshable_usage_truth_digest",
    "refreshable_usage_truth_material",
    "stable_refreshable_usage_revision_id",
]
