"""Read-only legacy JSONL importer for an isolated canonical candidate.

The importer has deliberately narrow trust boundaries:

* the only source it accepts is a :class:`VerifiedSnapshot` file;
* the only destination it accepts is an already-created ``candidate`` store
  under an explicit scratch root;
* it never discovers a agentacct store, instantiates a legacy service, copies
  a source, or mutates the snapshot;
* malformed or ambiguous legacy observations remain visible in
  ``migration_issues`` instead of being silently coerced.

This is spike code, not a live adapter or cutover path.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import stat
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping

from agentacct.usage_truth import (
    CODEX_LINEAGE_DELTA_SEMANTICS,
    local_usage_additivity,
    local_usage_row_lane,
)

from .snapshot import VerifiedSnapshot
from .v1_events import (
    EXPLICIT_NAMESPACE_SCHEME,
    absorb_observed_session,
    bounded_text as _bounded_text,
    client_for as _client_for,
    event_metadata as _metadata,
    explicit_namespace_digest,
    hash_safe as _hash_safe,
    normalized_work_claim_fields,
    observed_session_fields,
    optional_text as _optional_text,
    session_edge_hash_material,
    session_observation_hash_material,
    timestamp_to_us as _to_us,
    work_claim_occurred_raw,
)
from .sqlite import CanonicalRepository, CanonicalStore, canonical_hash, now_us
from .types import (
    FactInput,
    FactSessionLinkInput,
    SessionEdgeInput,
    SessionInput,
    SourceInstanceInput,
    TOKEN_FIELDS,
    SQLITE_INT64_MAX,
    UsageMeasurementInput,
    WorkClaimInput,
    WriteResult,
)


LEGACY_EVENTS_REPRESENTATION = "legacy-v1"
LEGACY_EVENTS_ADAPTER = "chronicle-v1-readonly-importer"
MAX_JSONL_LINE_BYTES = 16 * 1024 * 1024
_LEGACY_CUMULATIVE_USAGE_SEMANTICS = frozenset(
    {
        "cumulative_snapshot",
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
_VALID_USAGE_GRANULARITY = frozenset({"request", "turn", "session", "task_only", "unavailable"})
_TOKEN_SOURCE_KEYS: Mapping[str, tuple[str, str]] = {
    "input_tokens": ("event", "estimated_input_tokens"),
    "output_tokens": ("event", "estimated_output_tokens"),
    "cached_input_tokens": ("metadata", "cached_input_tokens"),
    "cache_creation_input_tokens": ("metadata", "cache_creation_input_tokens"),
    "cache_read_input_tokens": ("metadata", "cache_read_input_tokens"),
    "reasoning_output_tokens": ("metadata", "reasoning_output_tokens"),
    "total_tokens": ("metadata", "total_tokens"),
}
_REPORTED_FLAG_KEYS: Mapping[str, tuple[str, ...]] = {
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


class LegacyImportError(ValueError):
    """The declared import boundary or legacy input is unsafe."""


@dataclass(frozen=True)
class TruthSummary:
    """A comparable, omission-aware summary of one import scope."""

    source_instance_count: int
    session_count: int
    task_anchor_count: int
    accepted_session_edge_count: int
    rejected_session_edge_count: int
    semantic_fact_count: int
    usage_measurement_count: int
    totals_eligible_usage_measurement_count: int
    held_usage_measurement_count: int
    token_totals: Mapping[str, int | None]
    token_reported_counts: Mapping[str, int]
    token_missing_counts: Mapping[str, int]
    token_zero_counts: Mapping[str, int]
    providers: tuple[str, ...]
    models: tuple[str, ...]
    usage_row_fingerprints: tuple[str, ...]


@dataclass(frozen=True)
class ParityReport:
    """Snapshot/candidate comparison that never converts missing to zero."""

    matches: bool
    differences: Mapping[str, Mapping[str, object]]
    source: TruthSummary
    candidate: TruthSummary
    exclusions_visible: bool
    issue_count: int


@dataclass(frozen=True)
class MigrationReport:
    snapshot_manifest_sha256: str
    source_file: str
    candidate_path: str
    lines_seen: int
    parsed_events: int
    malformed_or_excluded_lines: int
    issue_lines: int
    excluded_lines: int
    processed_with_issues_lines: int
    write_dispositions: Mapping[str, Mapping[str, int]]
    migration_issue_count: int
    projection_rebuilt: bool
    projection: Mapping[str, int]
    parity: ParityReport
    canonical_sequence_before: int
    canonical_sequence_after: int
    # Whether the post-commit WAL fold completed. False (blocked by a reader
    # or a post-commit I/O error) never fails the committed import — the
    # candidate is still complete as db+wal — but the operator must know the
    # transient ~candidate-size WAL is still on disk before archiving.
    wal_folded: bool = True

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _SourceKey:
    client: str
    namespace_digest: bytes
    namespace_scheme: str
    # These are descriptive attributes, not source identity. Ignoring them in
    # equality prevents a mixed-version JSONL file from inventing duplicate
    # session identities for one namespace.
    privacy_label: str = field(compare=False)
    source_schema_version: str | None = field(compare=False)


@dataclass
class _SessionObservation:
    source_key: _SourceKey
    client_session_id: str
    session_kind: str
    started_at_us: int | None
    last_activity_at_us: int | None
    observation_order: int
    latest_line: int
    parent_client_session_id: str | None = None
    parent_source_key: _SourceKey | None = None

    def absorb(self, other: "_SessionObservation") -> None:
        wins = (other.observation_order, other.latest_line) >= (
            self.observation_order,
            self.latest_line,
        )
        incoming_parent = (
            (other.parent_client_session_id, other.parent_source_key)
            if other.parent_client_session_id is not None
            else None
        )
        incumbent_parent = (
            (self.parent_client_session_id, self.parent_source_key)
            if self.parent_client_session_id is not None
            else None
        )
        kind, started, last, parent = absorb_observed_session(
            incumbent_session_kind=self.session_kind,
            incumbent_started_at_us=self.started_at_us,
            incumbent_last_activity_at_us=self.last_activity_at_us,
            incumbent_parent=incumbent_parent,
            incoming_session_kind=other.session_kind,
            incoming_started_at_us=other.started_at_us,
            incoming_last_activity_at_us=other.last_activity_at_us,
            incoming_parent=incoming_parent,
            incoming_wins_last_writes=wins,
        )
        self.session_kind = kind
        self.started_at_us = started
        self.last_activity_at_us = last
        if parent is not None:
            self.parent_client_session_id, self.parent_source_key = parent  # type: ignore[assignment]
        if wins:
            self.observation_order = other.observation_order
            self.latest_line = other.latest_line


@dataclass(frozen=True)
class _EventRecord:
    line_number: int
    event: Mapping[str, Any]
    metadata: Mapping[str, Any]
    source_key: _SourceKey


@dataclass(frozen=True)
class _Issue:
    line_number: int
    reason: str
    disposition: str


@dataclass
class _ScanState:
    lines_seen: int = 0
    parsed_events: int = 0
    issues: list[_Issue] = field(default_factory=list)
    excluded_lines: set[int] = field(default_factory=set)


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _source_key(
    event: Mapping[str, Any],
    metadata: Mapping[str, Any],
    *,
    snapshot: VerifiedSnapshot,
    source_file: str,
    fingerprint_field: str = "source_namespace_fingerprint",
    client: str | None = None,
) -> _SourceKey:
    source_client = client or _client_for(event, metadata)
    fingerprint = metadata.get(fingerprint_field)
    if isinstance(fingerprint, str) and fingerprint.strip():
        digest = explicit_namespace_digest(fingerprint)
        scheme = EXPLICIT_NAMESPACE_SCHEME
        label = f"explicit:{digest.hex()[:12]}"
    else:
        digest = hashlib.sha256(
            b"legacy-unresolved-snapshot-namespace-v1\x00"
            + snapshot.manifest_digest.encode("ascii")
            + b"\x00"
            + source_file.encode("utf-8")
            + b"\x00"
            + source_client.encode("utf-8")
        ).digest()
        scheme = "snapshot-scoped-unresolved-v1"
        label = f"unresolved:{digest.hex()[:12]}"
    schema = _optional_text(event.get("schema_version"), limit=128)
    return _SourceKey(source_client, digest, scheme, label, schema)


def _session_observation(record: _EventRecord, *, snapshot: VerifiedSnapshot, source_file: str) -> _SessionObservation | None:
    event = record.event
    metadata = record.metadata
    fields = observed_session_fields(event, metadata, client=record.source_key.client)
    if fields is None:
        return None
    session_id = fields.client_session_id
    parent_id = fields.parent_client_session_id
    kind = fields.session_kind
    started = fields.started_at_us
    updated = fields.last_activity_at_us
    order = fields.observation_order if fields.observation_order is not None else record.line_number
    parent_source = None
    if parent_id is not None:
        parent_fingerprint = metadata.get("parent_source_namespace_fingerprint")
        if isinstance(parent_fingerprint, str) and parent_fingerprint.strip():
            parent_source = _source_key(
                event,
                metadata,
                snapshot=snapshot,
                source_file=source_file,
                fingerprint_field="parent_source_namespace_fingerprint",
                client=_bounded_text(metadata.get("parent_client"), default=record.source_key.client, limit=64),
            )
        else:
            parent_source = record.source_key
    return _SessionObservation(
        source_key=record.source_key,
        client_session_id=session_id,
        session_kind=kind,
        started_at_us=started,
        last_activity_at_us=updated,
        observation_order=order,
        latest_line=record.line_number,
        parent_client_session_id=parent_id,
        parent_source_key=parent_source,
    )


def _is_usage_event(event: Mapping[str, Any]) -> bool:
    """A usage measurement to import into the canonical store.

    Anchored on the ONE real usage event type, ``model_usage`` — the same
    type every usage writer emits (client_usage.py, evidence_runtime.py) and
    the type v1's usage predicates gate on (usage_truth.is_local_usage_import_event).
    The canonical importer is deliberately broader than v1's *truth* lane: it
    accepts any ``model_usage`` row, not only the trusted local-session-store
    import, so a non-local model_usage still lands in the general usage model.

    Two former arms were false-positive generators and are removed:

    * ``"usage" in event_type`` (a substring) pulled in ``agent_usage_debug_reported``
      — the debug snapshot the MCP contract states is NOT billing truth — and
      would have imported its tokens as authoritative usage.
    * ``key in event`` tested token-key PRESENCE, not value; the event envelope
      ships ``estimated_input_tokens``/``estimated_output_tokens`` set to ``None``
      on many non-usage types (task_completed, task_started, …), so every one of
      them matched.

    A pre-taxonomy row with NO ``event_type`` at all still falls back to a real
    token VALUE (not a merely-present, possibly-None key).
    """

    event_type = str(event.get("event_type") or "").lower()
    if event_type:
        return event_type == "model_usage"
    return any(
        event.get(key) is not None
        for key in ("estimated_input_tokens", "estimated_output_tokens")
    )


def _raw_usage_semantics(event: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
    value = metadata.get("usage_update_semantics") or event.get("usage_update_semantics")
    return str(value or "cumulative_snapshot").lower()


def _canonical_usage_semantics(raw_semantics: str) -> str | None:
    if raw_semantics in _LEGACY_CUMULATIVE_USAGE_SEMANTICS:
        return "cumulative_snapshot"
    return None


def _is_pre_presence_codex_usage_row(
    event: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> bool:
    """Identify only the frozen reader shape known to have erased presence."""

    return (
        str(event.get("event_type") or "").lower() == "model_usage"
        and event.get("source") == "codex-local-session-import"
        and metadata.get("client") == "codex"
        and metadata.get("usage_source") == "local_client_session_store"
    )


def _safe_token(
    event: Mapping[str, Any],
    metadata: Mapping[str, Any],
    field_name: str,
    *,
    pre_presence_codex_row: bool,
) -> tuple[int | None, bool, str | None]:
    container_name, key = _TOKEN_SOURCE_KEYS[field_name]
    container = event if container_name == "event" else metadata
    explicit_flag: bool | None = None
    explicit_flag_key: str | None = None
    for flag_key in _REPORTED_FLAG_KEYS[field_name]:
        if flag_key in metadata:
            explicit_flag_key = flag_key
            if isinstance(metadata[flag_key], bool):
                explicit_flag = bool(metadata[flag_key])
            break
    if field_name == "cached_input_tokens" and explicit_flag_key is None:
        # ClientUsageEvent has always kept a numeric compatibility total for
        # cached input.  Newer rows also carry the two source-capability bits.
        # When both split counters are explicitly unavailable, that numeric
        # compatibility zero must not become a measured canonical zero merely
        # because the metadata key exists.  Conversely, either explicitly
        # reported split is sufficient proof that the combined value is a
        # reported measurement.
        split_flags: list[bool] = []
        for split_field in (
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            split_flag_key: str | None = None
            split_flag: bool | None = None
            for candidate in _REPORTED_FLAG_KEYS[split_field]:
                if candidate in metadata:
                    split_flag_key = candidate
                    if isinstance(metadata[candidate], bool):
                        split_flag = bool(metadata[candidate])
                    break
            if split_flag_key is not None and split_flag is None:
                return None, False, f"invalid_{split_field}_reported_flag"
            if split_flag is not None:
                split_flags.append(split_flag)
        if any(split_flags):
            explicit_flag = True
        elif len(split_flags) == 2:
            explicit_flag = False
    if explicit_flag_key is not None and explicit_flag is None:
        return None, False, f"invalid_{field_name}_reported_flag"
    if (
        explicit_flag is None
        and pre_presence_codex_row
        and field_name
        in {"input_tokens", "output_tokens", "reasoning_output_tokens"}
    ):
        return (
            None,
            False,
            f"legacy_codex_{field_name}_presence_unavailable",
        )
    present = key in container and container.get(key) is not None
    reported = explicit_flag if explicit_flag is not None else present
    if not reported:
        return None, False, None
    value = container.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > SQLITE_INT64_MAX
    ):
        return None, False, f"invalid_{field_name}"
    return value, True, None


def _lane_for(event: Mapping[str, Any], metadata: Mapping[str, Any], *, atomic_identity: str | None) -> str:
    raw = metadata.get("usage_row_lane")
    if not isinstance(raw, str) or not raw.strip():
        # Reuse the existing product's historical Claude ``:model:`` repair
        # and current per-model lane rule.  Other clients intentionally return
        # no derived lane because model drift must not change row identity.
        raw = local_usage_row_lane(dict(event)) or "session_total"
    lane = re.sub(r"[^A-Za-z0-9._:@/-]", "_", raw.strip())
    if atomic_identity is not None:
        lane = f"{lane}:event:{atomic_identity[:16]}"
    if len(lane) > 128:
        digest = hashlib.sha256(lane.encode("utf-8")).hexdigest()[:20]
        lane = f"{lane[:107]}:{digest}"
    return lane or "session_total"


def _usage_input(
    event: Mapping[str, Any],
    metadata: Mapping[str, Any],
    *,
    client: str,
    line_number: int,
    session_id: int,
    issues: list[_Issue],
) -> UsageMeasurementInput:
    """Normalize one v1 usage event into a cumulative measurement input.

    Shared with the live shadow writer (which passes line_number=0 — its
    issues surface through shadow dispositions, not migration_issues), so
    both writers must keep deriving identical content-hash material for the
    same event.
    """

    raw_semantics = _raw_usage_semantics(event, metadata)
    semantics = _canonical_usage_semantics(raw_semantics)
    if semantics is None:
        raise LegacyImportError("usage semantics must be screened before canonical conversion")
    lane = _lane_for(event, metadata, atomic_identity=None)
    pre_presence_codex_row = _is_pre_presence_codex_usage_row(event, metadata)

    values: dict[str, object] = {}
    for field_name in TOKEN_FIELDS:
        value, reported, issue = _safe_token(
            event,
            metadata,
            field_name,
            pre_presence_codex_row=pre_presence_codex_row,
        )
        values[field_name] = value
        values[f"{field_name}_reported"] = reported
        if issue is not None:
            disposition = (
                "requires_choice"
                if issue.startswith("legacy_codex_")
                else "invalid"
            )
            issues.append(_Issue(line_number, issue, disposition))

    explicit_additive = metadata.get("usage_additive")
    if not isinstance(explicit_additive, bool):
        explicit_additive = event.get("usage_additive")
    additive, inferred_held_reason = local_usage_additivity(
        client=client,
        session_kind=metadata.get("client_session_kind") or event.get("client_session_kind"),
        parent_client_session_id=(
            metadata.get("parent_client_session_id") or event.get("parent_client_session_id")
        ),
        usage_update_semantics=raw_semantics,
        explicit_usage_additive=explicit_additive,
        source_namespace_fingerprint=metadata.get("source_namespace_fingerprint"),
        parent_source_namespace_fingerprint=metadata.get(
            "parent_source_namespace_fingerprint"
        ),
    )
    held_reason = None if additive else _bounded_text(
        metadata.get("usage_held_reason")
        or metadata.get("held_reason")
        or inferred_held_reason,
        default="legacy_non_additive",
        limit=512,
    )
    if not additive and explicit_additive is not False:
        issues.append(
            _Issue(
                line_number,
                "legacy_usage_additivity_not_proven",
                "quarantined",
            )
        )
    raw_precedence = metadata.get("precedence_role")
    precedence_value = str(raw_precedence or "authoritative").lower()
    if precedence_value in {"authoritative", "fallback", "enrichment"}:
        precedence = precedence_value
    else:
        precedence = "authoritative"
        issues.append(
            _Issue(line_number, "invalid_usage_precedence_role", "invalid")
        )
    if precedence == "enrichment" and additive:
        additive = False
        held_reason = "enrichment_not_totals_eligible"
    granularity_value = str(metadata.get("usage_granularity") or ("request" if semantics == "atomic_increment" else "session")).lower()
    granularity = granularity_value if granularity_value in _VALID_USAGE_GRANULARITY else "session"
    raw_observed = event.get("created_at") or metadata.get("started_at")
    observed = _to_us(raw_observed)
    if observed is None:
        # Epoch zero is the pinned unknown-instant sentinel; whether the
        # timestamp was absent or present-but-unparseable, the gap must stay
        # visible as a recorded issue instead of silently coercing.
        issues.append(
            _Issue(
                line_number,
                "missing_event_timestamp" if raw_observed is None else "invalid_timestamp",
                "invalid",
            )
        )
        observed = 0
    updated = _to_us(metadata.get("updated_at") or event.get("updated_at") or event.get("created_at")) or observed
    updated = max(updated, observed)
    order = updated if updated else line_number

    client_cost: int | None = None
    raw_cost = metadata.get("client_reported_cost_usd")
    if raw_cost is None and str(event.get("cost_confidence") or "").startswith("client_reported"):
        raw_cost = event.get("estimated_cost_usd")
    if raw_cost is not None and not isinstance(raw_cost, bool):
        try:
            numeric_cost = float(raw_cost)
        except (TypeError, ValueError, OverflowError):
            numeric_cost = math.nan
        if (
            math.isfinite(numeric_cost)
            and 0 <= numeric_cost <= SQLITE_INT64_MAX / 1_000_000
        ):
            converted_cost = int(round(numeric_cost * 1_000_000))
            if converted_cost <= SQLITE_INT64_MAX:
                client_cost = converted_cost
            else:
                issues.append(
                    _Issue(
                        line_number,
                        "invalid_client_reported_cost",
                        "invalid",
                    )
                )
        else:
            issues.append(_Issue(line_number, "invalid_client_reported_cost", "invalid"))

    raw_representation = metadata.get("usage_representation")
    representation = LEGACY_EVENTS_REPRESENTATION
    if raw_representation is not None:
        if (
            isinstance(raw_representation, str)
            and raw_representation.strip()
            and len(raw_representation.strip()) <= 128
        ):
            representation = raw_representation.strip()
        else:
            issues.append(
                _Issue(line_number, "invalid_usage_representation", "invalid")
            )

    return UsageMeasurementInput(
        session_id=session_id,
        lane=lane,
        representation=representation,
        update_semantics=semantics,  # type: ignore[arg-type]
        precedence_role=precedence,  # type: ignore[arg-type]
        granularity=granularity,  # type: ignore[arg-type]
        totals_eligible=additive,
        usage_confidence=_bounded_text(event.get("usage_confidence"), default="legacy_unknown", limit=64),
        observed_at_us=observed,
        updated_at_us=updated,
        provider=_optional_text(event.get("provider"), limit=128),
        model=_optional_text(event.get("model"), limit=256),
        held_reason=held_reason,
        client_reported_cost_microusd=client_cost,
        source_order=order,
        **values,
    )


def _work_claim_input(
    record: _EventRecord,
    *,
    source_instance_id: int,
    repository: CanonicalRepository,
    issues: list[_Issue],
) -> WorkClaimInput | None:
    event, metadata = record.event, record.metadata
    normalized = normalized_work_claim_fields(event, metadata)
    if normalized is None:
        return None

    source_event_id = _optional_text(event.get("event_id"), limit=512)
    if source_event_id is None:
        source_event_id = f"legacy:{canonical_hash(_hash_safe(record.event)).hex()}:{record.line_number}"
    supersedes_id = None
    supersedes_event = normalized["supersedes_event_id"]
    if supersedes_event is not None:
        supersedes_id = repository.find_fact_id(source_instance_id, supersedes_event)
        if supersedes_id is None:
            issues.append(_Issue(record.line_number, "unknown_superseded_event", "requires_choice"))
            return None

    raw_occurred = work_claim_occurred_raw(event, metadata)
    occurred = _to_us(raw_occurred)
    if occurred is None:
        issues.append(
            _Issue(
                record.line_number,
                "missing_event_timestamp" if raw_occurred is None else "invalid_timestamp",
                "invalid",
            )
        )
        occurred = 0
    fact = FactInput(
        source_instance_id=source_instance_id,
        fact_type="work_claim",
        transport="legacy-jsonl",
        strength="recorded_claim",
        occurred_at_us=occurred,
        source_event_id=source_event_id,
        source_order=record.line_number,
        content_hash=canonical_hash(normalized),
        supersedes_fact_id=supersedes_id,
    )
    return WorkClaimInput(fact=fact, **{key: value for key, value in normalized.items() if key != "supersedes_event_id"})  # type: ignore[arg-type]


def _increment(counter: dict[str, Counter[str]], kind: str, result: WriteResult) -> None:
    counter.setdefault(kind, Counter())[result.disposition] += 1


def _summary_from_usage(
    *,
    source_count: int,
    session_count: int,
    task_count: int,
    accepted_edges: int,
    rejected_edges: int,
    semantic_fact_count: int,
    usage: list[UsageMeasurementInput],
    session_identities: Mapping[int, Mapping[str, object]] | None = None,
) -> TruthSummary:
    reported: dict[str, int] = {name: 0 for name in TOKEN_FIELDS}
    missing: dict[str, int] = {name: 0 for name in TOKEN_FIELDS}
    zeros: dict[str, int] = {name: 0 for name in TOKEN_FIELDS}
    totals: dict[str, int | None] = {name: None for name in TOKEN_FIELDS}
    providers: set[str] = set()
    models: set[str] = set()
    fingerprints: list[str] = []
    for row in usage:
        if row.provider is not None:
            providers.add(row.provider)
        if row.model is not None:
            models.add(row.model)
        identity = (
            session_identities.get(row.session_id)
            if session_identities is not None
            else None
        )
        fingerprints.append(
            canonical_hash(
                {
                    "session": identity or {"candidate_session_id": row.session_id},
                    "lane": row.lane,
                    "representation": row.representation,
                    "update_semantics": row.update_semantics,
                    "precedence_role": row.precedence_role,
                    "granularity": row.granularity,
                    "totals_eligible": row.totals_eligible,
                    "provider": row.provider,
                    "model": row.model,
                    "usage_confidence": row.usage_confidence,
                    "held_reason": row.held_reason,
                    "client_reported_cost_microusd": row.client_reported_cost_microusd,
                    "source_order": row.source_order,
                    "source_revision_id": row.source_revision_id,
                    "observed_at_us": row.observed_at_us,
                    "updated_at_us": row.updated_at_us,
                    "tokens": {
                        name: {
                            "reported": bool(getattr(row, f"{name}_reported")),
                            "value": getattr(row, name),
                        }
                        for name in TOKEN_FIELDS
                    },
                }
            ).hex()
        )
        for name in TOKEN_FIELDS:
            if bool(getattr(row, f"{name}_reported")):
                reported[name] += 1
                value = int(getattr(row, name))
                if value == 0:
                    zeros[name] += 1
                if row.totals_eligible:
                    totals[name] = (totals[name] or 0) + value
            else:
                missing[name] += 1
    return TruthSummary(
        source_instance_count=source_count,
        session_count=session_count,
        task_anchor_count=task_count,
        accepted_session_edge_count=accepted_edges,
        rejected_session_edge_count=rejected_edges,
        semantic_fact_count=semantic_fact_count,
        usage_measurement_count=len(usage),
        totals_eligible_usage_measurement_count=sum(row.totals_eligible for row in usage),
        held_usage_measurement_count=sum(not row.totals_eligible for row in usage),
        token_totals=totals,
        token_reported_counts=reported,
        token_missing_counts=missing,
        token_zero_counts=zeros,
        providers=tuple(sorted(providers)),
        models=tuple(sorted(models)),
        usage_row_fingerprints=tuple(sorted(fingerprints)),
    )


def _summary_from_candidate(
    repository: CanonicalRepository,
    *,
    source_ids: set[int],
) -> TruthSummary:
    connection = repository.connection
    if not source_ids:
        return _summary_from_usage(
            source_count=0,
            session_count=0,
            task_count=0,
            accepted_edges=0,
            rejected_edges=0,
            semantic_fact_count=0,
            usage=[],
        )
    placeholders = ",".join("?" for _ in source_ids)
    params = tuple(sorted(source_ids))
    session_rows = connection.execute(
        f"SELECT session_id FROM sessions WHERE source_instance_id IN ({placeholders})", params
    ).fetchall()
    session_ids = {int(row[0]) for row in session_rows}
    if session_ids:
        session_placeholders = ",".join("?" for _ in session_ids)
        session_params = tuple(sorted(session_ids))
        task_count = int(connection.execute(
            f"SELECT COUNT(*) FROM task_anchors WHERE primary_session_id IN ({session_placeholders})", session_params
        ).fetchone()[0])
        edge_rows = connection.execute(
            f"SELECT validation_state, COUNT(*) FROM session_edges WHERE child_session_id IN ({session_placeholders}) "
            "GROUP BY validation_state",
            session_params,
        ).fetchall()
        edge_counts = {str(row[0]): int(row[1]) for row in edge_rows}
        usage_rows = connection.execute(
            f"SELECT usage.*, session.client_session_id, source.client, source.namespace_scheme, "
            f"source.namespace_digest FROM usage_measurements usage "
            f"JOIN sessions session ON session.session_id = usage.session_id "
            f"JOIN source_instances source ON source.source_instance_id = session.source_instance_id "
            f"WHERE usage.session_id IN ({session_placeholders}) "
            "ORDER BY usage_measurement_id",
            session_params,
        ).fetchall()
    else:
        task_count = 0
        edge_counts = {}
        usage_rows = []

    usage_inputs: list[UsageMeasurementInput] = []
    session_identities: dict[int, Mapping[str, object]] = {}
    for row in usage_rows:
        session_identities[int(row["session_id"])] = {
            "client": str(row["client"]),
            "namespace_scheme": str(row["namespace_scheme"]),
            "namespace_digest": bytes(row["namespace_digest"]).hex(),
            "client_session_id": str(row["client_session_id"]),
        }
        values: dict[str, object] = {}
        for name in TOKEN_FIELDS:
            values[name] = row[name]
            values[f"{name}_reported"] = bool(row[f"{name}_reported"])
        usage_inputs.append(
            UsageMeasurementInput(
                session_id=int(row["session_id"]),
                lane=str(row["lane"]),
                representation=str(row["representation"]),
                update_semantics=str(row["update_semantics"]),  # type: ignore[arg-type]
                precedence_role=str(row["precedence_role"]),  # type: ignore[arg-type]
                granularity=str(row["granularity"]),  # type: ignore[arg-type]
                totals_eligible=bool(row["totals_eligible"]),
                usage_confidence=str(row["usage_confidence"]),
                observed_at_us=int(row["observed_at_us"]),
                updated_at_us=int(row["updated_at_us"]),
                provider=row["provider"],
                model=row["model"],
                held_reason=row["held_reason"],
                client_reported_cost_microusd=row["client_reported_cost_microusd"],
                source_order=row["source_order"],
                source_revision_id=row["source_revision_id"],
                **values,
            )
        )
    semantic_count = int(connection.execute(
        f"SELECT COUNT(*) FROM facts WHERE fact_type = 'work_claim' "
        f"AND transport = 'legacy-jsonl' AND source_instance_id IN ({placeholders})",
        params,
    ).fetchone()[0])
    return _summary_from_usage(
        source_count=len(source_ids),
        session_count=len(session_ids),
        task_count=task_count,
        accepted_edges=edge_counts.get("valid", 0),
        rejected_edges=edge_counts.get("rejected", 0),
        semantic_fact_count=semantic_count,
        usage=usage_inputs,
        session_identities=session_identities,
    )


def _parity(
    source: TruthSummary,
    candidate: TruthSummary,
    *,
    issue_count: int,
    unresolved_hard_conflict_count: int,
) -> ParityReport:
    source_values = asdict(source)
    candidate_values = asdict(candidate)
    differences: dict[str, Mapping[str, object]] = {}
    for key, source_value in source_values.items():
        candidate_value = candidate_values[key]
        if source_value != candidate_value:
            differences[key] = {"source": source_value, "candidate": candidate_value}
    if unresolved_hard_conflict_count:
        differences["unresolved_hard_conflicts"] = {
            "source": unresolved_hard_conflict_count,
            "candidate": 0,
        }
    return ParityReport(
        matches=not differences,
        differences=differences,
        source=source,
        candidate=candidate,
        exclusions_visible=issue_count > 0,
        issue_count=issue_count,
    )


class LegacyImporter:
    """Import one manifest-declared v1 event ledger into a candidate store."""

    def __init__(
        self,
        *,
        snapshot: VerifiedSnapshot,
        store: CanonicalStore,
        scratch_root: Path | str,
    ) -> None:
        if not isinstance(snapshot, VerifiedSnapshot):
            raise LegacyImportError("source must be a VerifiedSnapshot")
        if not isinstance(store, CanonicalStore):
            raise LegacyImportError("destination must be a CanonicalStore")
        if store.read_only:
            raise LegacyImportError("destination candidate must be writable")
        # ``VerifiedSnapshot.validate_candidate_target`` is used before a
        # candidate is created. An open WAL-mode CanonicalStore legitimately
        # owns ``-wal``/``-shm`` files, so re-running that pre-create check here
        # would reject the store's own sidecars. Repeat its durable path
        # invariants for the already-open destination instead.
        scratch = Path(scratch_root)
        if not scratch.is_absolute():
            raise LegacyImportError("scratch_root must be an explicit absolute path")
        if scratch.is_symlink() or not scratch.is_dir():
            raise LegacyImportError("scratch_root must be a real directory")
        scratch = scratch.resolve(strict=True)
        candidate = store.path.resolve(strict=True)
        if candidate == scratch or not candidate.is_relative_to(scratch):
            raise LegacyImportError("destination candidate must be strictly under scratch_root")
        if candidate == snapshot.root or candidate.is_relative_to(snapshot.root):
            raise LegacyImportError("destination candidate must be outside the verified snapshot")
        lowered_parts = tuple(part.casefold() for part in candidate.parts)
        forbidden_pairs = {
            (".agent-sentinel", "state"),
            (".agent-sentinel-global", "state"),
            (".agent-chronicle", "state"),
            (".agent-chronicle-global", "state"),
            # New canonical global store: match the agentacct/state pair (the bare
            # name "agentacct" is too generic to forbid on its own).
            ("agentacct", "state"),
        }
        if any(
            lowered_parts[index : index + 2] in forbidden_pairs
            for index in range(max(0, len(lowered_parts) - 1))
        ):
            raise LegacyImportError("destination candidate cannot be agentacct runtime state")
        cursor = candidate.parent
        while True:
            if cursor.is_symlink():
                raise LegacyImportError("destination candidate parent cannot traverse a symlink")
            if cursor == scratch:
                break
            if cursor == cursor.parent or not cursor.is_relative_to(scratch):
                raise LegacyImportError("destination candidate parent escaped scratch_root")
            cursor = cursor.parent
        candidate_stat = candidate.stat(follow_symlinks=False)
        if not candidate.is_file() or candidate.is_symlink() or candidate_stat.st_nlink != 1:
            raise LegacyImportError("destination candidate must be a unique regular non-symlink file")
        role = store.connection.execute(
            "SELECT store_role FROM store_metadata WHERE singleton = 1"
        ).fetchone()
        if role is None or str(role[0]) != "candidate":
            # The live writer reconciles sessions per event; the importer
            # absorbs them. Until the live lane reaches absorb parity
            # (migration phase 3.2), letting a bulk import loose on a live
            # store would quarantine healthy sessions as conflicts.
            raise LegacyImportError(
                "import destination must be a candidate store, not a live store"
            )
        self.snapshot = snapshot
        self.store = store
        self.repository = store.repository()
        self._scratch_root = scratch
        self._verify_candidate_identity()

    def _verify_candidate_identity(self) -> None:
        """Prove the report path still names this store's opened main inode."""

        candidate = self.store.path
        try:
            resolved = candidate.resolve(strict=True)
            observed = candidate.lstat()
            scratch = self._scratch_root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise LegacyImportError(
                "destination candidate identity changed after validation"
            ) from exc
        if resolved != candidate or scratch != self._scratch_root:
            raise LegacyImportError(
                "destination candidate identity changed after validation"
            )
        if candidate == scratch or not candidate.is_relative_to(scratch):
            raise LegacyImportError(
                "destination candidate identity changed after validation"
            )
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or stat.S_IMODE(observed.st_mode) != 0o600
            or (int(observed.st_dev), int(observed.st_ino))
            != self.store.file_identity
        ):
            raise LegacyImportError(
                "destination candidate identity changed after validation"
            )

    def _events(
        self,
        source_file: str,
        *,
        scan: _ScanState | None,
    ) -> Iterator[_EventRecord]:
        with self.snapshot.open_binary(source_file) as handle:
            yield from self._parse_handle(handle, source_file=source_file, scan=scan)

    def _parse_handle(
        self,
        handle: BinaryIO,
        *,
        source_file: str,
        scan: _ScanState | None,
    ) -> Iterator[_EventRecord]:
        line_number = 0
        while True:
            # ``BinaryIO`` iteration buffers through the next newline and can
            # therefore allocate an entire hostile logical line before the
            # size check.  Keep every read bounded, then drain an oversized
            # line in bounded chunks so the following record remains usable.
            raw = handle.readline(MAX_JSONL_LINE_BYTES + 1)
            if not raw:
                break
            line_number += 1
            if scan is not None:
                scan.lines_seen += 1
            if len(raw) > MAX_JSONL_LINE_BYTES:
                while not raw.endswith(b"\n"):
                    raw = handle.readline(MAX_JSONL_LINE_BYTES + 1)
                    if not raw:
                        break
                if scan is not None:
                    scan.issues.append(_Issue(line_number, "jsonl_line_too_large", "invalid"))
                    scan.excluded_lines.add(line_number)
                continue
            try:
                decoded = raw.decode("utf-8", errors="strict")
                value = json.loads(decoded, object_pairs_hook=_reject_duplicate_json_keys)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                if scan is not None:
                    scan.issues.append(_Issue(line_number, "malformed_jsonl", "invalid"))
                    scan.excluded_lines.add(line_number)
                continue
            if not isinstance(value, Mapping):
                if scan is not None:
                    scan.issues.append(_Issue(line_number, "event_not_object", "invalid"))
                    scan.excluded_lines.add(line_number)
                continue
            metadata = _metadata(value)
            namespace_fingerprint = metadata.get("source_namespace_fingerprint")
            if not isinstance(namespace_fingerprint, str) or not namespace_fingerprint.strip():
                if scan is not None:
                    scan.issues.append(
                        _Issue(
                            line_number,
                            "unresolved_source_namespace",
                            "requires_choice",
                        )
                    )
                    scan.excluded_lines.add(line_number)
                continue
            timestamp_values = (
                value.get("created_at"),
                value.get("updated_at"),
                metadata.get("started_at"),
                metadata.get("updated_at"),
                metadata.get("client_event_timestamp"),
            )
            if any(
                timestamp is not None and _to_us(timestamp) is None
                for timestamp in timestamp_values
            ) and scan is not None:
                # A sealed manifest proves bytes, not that their numeric values
                # fit SQLite.  Preserve the row with unavailable time where
                # possible, but make the lossy conversion an explicit NO-GO
                # issue instead of reaching sqlite3's OverflowError later.
                scan.issues.append(
                    _Issue(line_number, "invalid_timestamp", "invalid")
                )
            if scan is not None:
                scan.parsed_events += 1
            yield _EventRecord(
                line_number=line_number,
                event=value,
                metadata=metadata,
                source_key=_source_key(
                    value,
                    metadata,
                    snapshot=self.snapshot,
                    source_file=source_file,
                ),
            )

    def _record_issues(self, source_file: str, issues: list[_Issue]) -> int:
        unique = {(issue.line_number, issue.reason, issue.disposition): issue for issue in issues}
        inserted = 0
        with self.store.transaction(write=True) as connection:
            for issue in sorted(unique.values(), key=lambda item: (item.line_number, item.reason, item.disposition)):
                location_digest = canonical_hash(
                    {
                        "manifest": self.snapshot.manifest_digest,
                        "source_file": source_file,
                        "line": issue.line_number,
                    }
                )
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO migration_issues(legacy_origin, location_digest, reason, disposition, count, first_seen_at_us) "
                    "VALUES (?, ?, ?, ?, 1, ?)",
                    (source_file[:512], location_digest, issue.reason[:128], issue.disposition, now_us()),
                )
                inserted += max(0, int(cursor.rowcount))
            if inserted:
                connection.execute(
                    "UPDATE store_metadata SET canonical_sequence = canonical_sequence + ? WHERE singleton = 1",
                    (inserted,),
                )
        return len(unique)

    def import_events(self, source_file: str = "events.jsonl") -> MigrationReport:
        """Import a verified JSONL payload and return explicit parity evidence."""

        if not isinstance(source_file, str) or not source_file:
            raise LegacyImportError("source_file must name a manifest-declared relative file")
        self._verify_candidate_identity()
        # Resolves only manifest-declared input and rejects traversal before any
        # candidate write. Re-verification also catches post-construction swaps.
        self.snapshot.path_for(source_file)
        self.snapshot.verify_unchanged()
        # One outer transaction makes the whole import atomic: nested
        # repository scopes become savepoints, so an abort at any point rolls
        # the candidate back to its pre-import state instead of leaving a
        # partially-committed run behind.
        with self.store.transaction(write=True):
            report = self._run_import(source_file)
        # The single-transaction design grows the WAL to roughly candidate
        # size until that COMMIT (plan ~2x disk headroom for the 5 GB path);
        # fold it back into the main file so the headroom is transient and
        # the closed candidate is a complete standalone database. The fold is
        # best-effort EVIDENCE, never a second failure point: the import is
        # already durably committed, so a blocked or failed checkpoint must
        # surface through the report instead of destroying it.
        wal_folded = False
        try:
            row = self.store.connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            wal_folded = row is not None and int(row[0]) == 0
        except sqlite3.Error:
            wal_folded = False
        if not wal_folded:
            report = replace(report, wal_folded=False)
        return report

    def _run_import(self, source_file: str) -> MigrationReport:
        sequence_before = self.repository.canonical_sequence()
        scan = _ScanState()
        sessions: dict[tuple[_SourceKey, str], _SessionObservation] = {}
        source_keys: set[_SourceKey] = set()
        for record in self._events(source_file, scan=scan):
            source_keys.add(record.source_key)
            observation = _session_observation(record, snapshot=self.snapshot, source_file=source_file)
            if observation is None:
                if _is_usage_event(record.event):
                    scan.issues.append(_Issue(record.line_number, "usage_missing_session_identity", "quarantined"))
                    scan.excluded_lines.add(record.line_number)
                continue
            identity = (observation.source_key, observation.client_session_id)
            existing = sessions.get(identity)
            if existing is None:
                sessions[identity] = observation
            else:
                existing.absorb(observation)

        source_ids: dict[_SourceKey, int] = {}
        for key in sorted(source_keys, key=lambda item: (item.client, item.namespace_digest, item.namespace_scheme)):
            source = self.repository.get_or_create_source(
                SourceInstanceInput(
                    client=key.client,
                    adapter=LEGACY_EVENTS_ADAPTER,
                    representation=LEGACY_EVENTS_REPRESENTATION,
                    namespace_digest=key.namespace_digest,
                    namespace_scheme=key.namespace_scheme,
                    source_schema_version=key.source_schema_version,
                    privacy_label=key.privacy_label,
                )
            )
            source_ids[key] = source.source_instance_id

        dispositions: dict[str, Counter[str]] = {}
        session_ids: dict[tuple[_SourceKey, str], int] = {}
        accepted_session_observations: set[tuple[_SourceKey, str]] = set()
        for identity, observation in sorted(
            sessions.items(),
            key=lambda item: (item[0][0].client, item[0][0].namespace_digest, item[0][1]),
        ):
            observation_hash = canonical_hash(
                session_observation_hash_material(
                    client_session_id=observation.client_session_id,
                    session_kind=observation.session_kind,
                    started_at_us=observation.started_at_us,
                    last_activity_at_us=observation.last_activity_at_us,
                    parent_client_session_id=observation.parent_client_session_id,
                    parent_namespace_digest=(
                        observation.parent_source_key.namespace_digest
                        if observation.parent_source_key is not None
                        else None
                    ),
                )
            )
            result = self.repository.reconcile_session(
                SessionInput(
                    source_instance_id=source_ids[observation.source_key],
                    client_session_id=observation.client_session_id,
                    session_kind=observation.session_kind,  # type: ignore[arg-type]
                    started_at_us=observation.started_at_us,
                    last_activity_at_us=observation.last_activity_at_us,
                    observation_order=observation.observation_order,
                    observation_hash=observation_hash,
                    parent_client=(
                        observation.parent_source_key.client
                        if observation.parent_source_key is not None
                        else None
                    ),
                    parent_client_session_id=observation.parent_client_session_id,
                    parent_namespace_digest=(
                        observation.parent_source_key.namespace_digest
                        if observation.parent_source_key is not None
                        else None
                    ),
                )
            )
            _increment(dispositions, "sessions", result)
            assert result.row_id is not None
            session_ids[identity] = result.row_id
            if result.disposition in {"inserted", "updated", "noop"}:
                accepted_session_observations.add(identity)

        accepted_edges = 0
        rejected_edges = 0
        valid_parented_session_ids: set[int] = set()
        retired_task_anchor_session_ids: set[int] = set()
        for identity, observation in sessions.items():
            if observation.parent_client_session_id is None or observation.parent_source_key is None:
                continue
            # A stale/divergent session observation must not manufacture
            # lineage from fields that reconciliation explicitly rejected.
            if identity not in accepted_session_observations:
                continue
            parent_identity = (observation.parent_source_key, observation.parent_client_session_id)
            parent_id = session_ids.get(parent_identity)
            child_id = session_ids[identity]
            if parent_id is None:
                scan.issues.append(_Issue(observation.latest_line, "parent_session_not_in_snapshot", "requires_choice"))
                continue
            if parent_id == child_id:
                # A row claiming itself as parent would otherwise abort the
                # whole import on the session_edges child<>parent CHECK; one
                # bad line must never take down a cutover run.
                scan.issues.append(
                    _Issue(observation.latest_line, "self_parent_session", "quarantined")
                )
                continue
            edge_hash = canonical_hash(
                session_edge_hash_material(
                    child_namespace_digest=observation.source_key.namespace_digest,
                    child_client_session_id=observation.client_session_id,
                    parent_namespace_digest=observation.parent_source_key.namespace_digest,
                    parent_client_session_id=observation.parent_client_session_id,
                    relation_kind=observation.session_kind,
                )
            )
            relation = observation.session_kind if observation.session_kind in {"continuation", "retry"} else "parent"
            had_task_anchor = self.repository.connection.execute(
                "SELECT 1 FROM task_anchors WHERE primary_session_id = ?",
                (child_id,),
            ).fetchone() is not None
            result = self.repository.add_session_edge(
                SessionEdgeInput(
                    child_session_id=child_id,
                    parent_session_id=parent_id,
                    relation=relation,  # type: ignore[arg-type]
                    basis="legacy_explicit_parent_session_id",
                    validation_state="valid",
                    content_hash=edge_hash,
                )
            )
            _increment(dispositions, "session_edges", result)
            row = self.repository.connection.execute(
                "SELECT validation_state FROM session_edges WHERE session_edge_id = ?", (result.row_id,)
            ).fetchone()
            edge_observation_accepted = result.disposition in {"inserted", "updated", "noop"}
            if edge_observation_accepted and row is not None and row[0] == "valid":
                accepted_edges += 1
                valid_parented_session_ids.add(child_id)
                if had_task_anchor and self.repository.connection.execute(
                    "SELECT 1 FROM task_anchors WHERE primary_session_id = ?",
                    (child_id,),
                ).fetchone() is None:
                    retired_task_anchor_session_ids.add(child_id)
            elif edge_observation_accepted and row is not None and row[0] == "rejected":
                rejected_edges += 1

        task_ids: set[int] = set()
        for identity, observation in sessions.items():
            if observation.parent_client_session_id is not None:
                child_session_id = session_ids[identity]
                if child_session_id not in valid_parented_session_ids:
                    # Missing, rejected, or conflicted lineage is not evidence
                    # that an established standalone Task ceased to exist.
                    continue
                if child_session_id in retired_task_anchor_session_ids:
                    dispositions.setdefault("task_anchors", Counter())["removed"] += 1
                    scan.issues.append(
                        _Issue(
                            observation.latest_line,
                            "task_anchor_removed_after_parent_reclassification",
                            "quarantined",
                        )
                    )
                continue
            if observation.session_kind not in {"root", "unknown"}:
                continue
            anchor = self.repository.get_or_create_task_anchor(session_ids[identity])
            task_ids.add(anchor.task_anchor_id)

        cumulative_usage: dict[tuple[int, str, str], tuple[int, UsageMeasurementInput]] = {}
        semantic_identities: set[tuple[int, str]] = set()
        unresolved_hard_conflicts = 0
        for record in self._events(source_file, scan=None):
            source_id = source_ids[record.source_key]
            claim = _work_claim_input(
                record,
                source_instance_id=source_id,
                repository=self.repository,
                issues=scan.issues,
            )
            if claim is not None:
                result = self.repository.append_work_claim(claim)
                _increment(dispositions, "facts", result)
                if result.disposition == "conflict":
                    unresolved_hard_conflicts += 1
                    scan.issues.append(
                        _Issue(record.line_number, "semantic_fact_conflict", "quarantined")
                    )
                assert claim.fact.source_event_id is not None
                semantic_identities.add((source_id, claim.fact.source_event_id))
                observation = _session_observation(
                    record,
                    snapshot=self.snapshot,
                    source_file=source_file,
                )
                target_session_id = (
                    session_ids.get((observation.source_key, observation.client_session_id))
                    if observation is not None
                    else None
                )
                if (
                    target_session_id is not None
                    and result.row_id is not None
                    and result.disposition in {"inserted", "noop"}
                ):
                    link_result = self.repository.link_fact_to_session(
                        FactSessionLinkInput(
                            fact_id=result.row_id,
                            session_id=target_session_id,
                            method="legacy_explicit_client_session_id",
                            confidence="exact",
                            rule_version="legacy-import-v1",
                            validation_state="valid",
                        )
                    )
                    _increment(dispositions, "fact_session_links", link_result)

            if not _is_usage_event(record.event):
                continue
            observation = _session_observation(record, snapshot=self.snapshot, source_file=source_file)
            if observation is None:
                continue
            target_session_id = session_ids.get((observation.source_key, observation.client_session_id))
            if target_session_id is None:
                scan.issues.append(_Issue(record.line_number, "usage_session_not_imported", "quarantined"))
                continue
            raw_semantics = _raw_usage_semantics(record.event, record.metadata)
            if raw_semantics == "atomic_increment":
                scan.issues.append(
                    _Issue(
                        record.line_number,
                        "atomic_usage_requires_immutable_event_lane",
                        "excluded",
                    )
                )
                continue
            if _canonical_usage_semantics(raw_semantics) is None:
                scan.issues.append(
                    _Issue(
                        record.line_number,
                        "invalid_usage_update_semantics",
                        "excluded",
                    )
                )
                continue
            usage = _usage_input(
                record.event,
                record.metadata,
                client=record.source_key.client,
                line_number=record.line_number,
                session_id=target_session_id,
                issues=scan.issues,
            )
            key = (usage.session_id, usage.lane, usage.representation)
            incumbent = cumulative_usage.get(key)
            if incumbent is None or (usage.source_order or 0, record.line_number) >= (
                incumbent[1].source_order or 0,
                incumbent[0],
            ):
                cumulative_usage[key] = (record.line_number, usage)

        effective_usage = [item[1] for item in cumulative_usage.values()]
        effective_usage.sort(key=lambda value: (value.session_id, value.lane, value.representation))
        line_by_usage_key = {
            (value.session_id, value.lane, value.representation): line
            for line, value in cumulative_usage.values()
        }
        for usage in effective_usage:
            line = line_by_usage_key[(usage.session_id, usage.lane, usage.representation)]
            checkpoint_key = f"{self.snapshot.manifest_digest[:16]}:{source_file}:{line}"[:256]
            result = self.repository.reconcile_usage(
                usage,
                checkpoint=("migration", checkpoint_key),
            )
            _increment(dispositions, "usage", result)

        issue_count = self._record_issues(source_file, scan.issues)
        sequence_after_writes = self.repository.canonical_sequence()
        projection_rebuilt = sequence_after_writes != sequence_before
        if not projection_rebuilt:
            current = self.repository.connection.execute(
                "SELECT COUNT(*) FROM projection_generations WHERE projection_name IN ('rm_task_current', 'rm_usage_day')"
            ).fetchone()
            projection_rebuilt = current is None or int(current[0]) != 2
        if projection_rebuilt:
            projection = self.repository.rebuild_minimal_read_models()
        else:
            projection = {
                "task_count": int(self.repository.connection.execute("SELECT COUNT(*) FROM rm_task_current").fetchone()[0]),
                "usage_day_count": int(self.repository.connection.execute("SELECT COUNT(*) FROM rm_usage_day").fetchone()[0]),
                "built_through_sequence": self.repository.canonical_sequence(),
            }

        source_session_identities = {
            session_ids[identity]: {
                "client": identity[0].client,
                "namespace_scheme": identity[0].namespace_scheme,
                "namespace_digest": identity[0].namespace_digest.hex(),
                "client_session_id": identity[1],
            }
            for identity in sessions
        }
        source_summary = _summary_from_usage(
            source_count=len(set(source_ids.values())),
            session_count=len(sessions),
            task_count=len(task_ids),
            accepted_edges=accepted_edges,
            rejected_edges=rejected_edges,
            semantic_fact_count=len(semantic_identities),
            usage=effective_usage,
            session_identities=source_session_identities,
        )
        candidate_summary = _summary_from_candidate(self.repository, source_ids=set(source_ids.values()))
        parity = _parity(
            source_summary,
            candidate_summary,
            issue_count=issue_count,
            unresolved_hard_conflict_count=unresolved_hard_conflicts,
        )
        self.snapshot.verify_unchanged()
        self._verify_candidate_identity()
        serialized_dispositions = {
            kind: dict(sorted(counts.items())) for kind, counts in sorted(dispositions.items())
        }
        issue_lines = {issue.line_number for issue in scan.issues}
        return MigrationReport(
            snapshot_manifest_sha256=self.snapshot.manifest_digest,
            source_file=source_file,
            candidate_path=str(self.store.path),
            lines_seen=scan.lines_seen,
            parsed_events=scan.parsed_events,
            # Compatibility alias retained for the spike report v1.  This is
            # the count of distinct lines with any issue, not necessarily the
            # count of fully excluded input rows.
            malformed_or_excluded_lines=len(issue_lines),
            issue_lines=len(issue_lines),
            excluded_lines=len(scan.excluded_lines),
            processed_with_issues_lines=len(issue_lines - scan.excluded_lines),
            write_dispositions=serialized_dispositions,
            migration_issue_count=issue_count,
            projection_rebuilt=projection_rebuilt,
            projection=projection,
            parity=parity,
            canonical_sequence_before=sequence_before,
            canonical_sequence_after=self.repository.canonical_sequence(),
        )


def import_legacy_snapshot(
    *,
    snapshot: VerifiedSnapshot,
    store: CanonicalStore,
    scratch_root: Path | str,
    source_file: str = "events.jsonl",
) -> MigrationReport:
    """Functional entry point for the isolated legacy migration spike."""

    return LegacyImporter(
        snapshot=snapshot,
        store=store,
        scratch_root=scratch_root,
    ).import_events(source_file)


__all__ = [
    "LEGACY_EVENTS_ADAPTER",
    "LEGACY_EVENTS_REPRESENTATION",
    "LegacyImportError",
    "LegacyImporter",
    "MigrationReport",
    "ParityReport",
    "TruthSummary",
    "import_legacy_snapshot",
]
