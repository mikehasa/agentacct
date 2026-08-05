"""Client-neutral usage view building — the shared "usage/cost rows" data layer.

This module owns the one intake every usage surface shares: turning raw
``ClientUsageEvent`` scan candidates and saved sentinel events into
``DashboardUsageRecord`` rows and the aggregated ``DashboardUsageView``
(additivity normalization, legacy-shadow exclusion, ambiguous-identity
quarantine, refused-recording rollup), plus the ONE canonical row-timestamp
rule (``_usage_record_time``).

Everything here was extracted verbatim from :mod:`agentacct.api` so that
non-web consumers (:mod:`agentacct.usage_snapshot`, :mod:`agentacct.plan_cost`,
and through them the TUI) no longer import the web server module for pure data
logic. :mod:`agentacct.api` re-imports these names and keeps behaving exactly
as before.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field as dataclass_field, replace
from typing import Any

from .client_usage import ClientUsageEvent, is_local_usage_import_event
from .cost import estimate_model_cost_breakdown_usd, has_model_price
from .log_evidence import summarize_refused_recording_attempts
from .usage_truth import (
    local_usage_additivity,
    local_usage_source_namespace_ambiguous_identities,
    normalized_local_usage_session_id,
    recognized_local_usage_row_identity,
    split_shadowed_legacy_usage_events,
)


def _metadata(event: dict[str, Any]) -> dict[str, Any]:
    metadata = event.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _metadata_int(event: dict[str, Any], key: str) -> int:
    try:
        number = int(_metadata(event).get(key) or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _cache_creation_tokens(event: dict[str, Any]) -> int:
    value = _metadata_int(event, "cache_creation_input_tokens")
    if value > 0:
        return value
    metadata = _metadata(event)
    if "cache_read_input_tokens" not in metadata:
        return 0
    cached = _metadata_int(event, "cached_input_tokens")
    read = _metadata_int(event, "cache_read_input_tokens")
    return max(0, cached - read)


def _cache_read_tokens(event: dict[str, Any]) -> int:
    value = _metadata_int(event, "cache_read_input_tokens")
    if value > 0:
        return value
    cached = _metadata_int(event, "cached_input_tokens")
    creation = _metadata_int(event, "cache_creation_input_tokens")
    return max(0, cached - creation)


def _session_activity_time(event: dict[str, Any]) -> float:
    metadata = _metadata(event)
    client = str(metadata.get("client") or "").strip()
    if client == "hermes":
        preferred = metadata.get("started_at") or metadata.get("updated_at")
    else:
        preferred = metadata.get("updated_at") or metadata.get("started_at")
    try:
        timestamp = float(preferred or event.get("created_at") or 0.0)
    except (OverflowError, TypeError, ValueError):
        return 0.0
    return timestamp if math.isfinite(timestamp) and timestamp > 0 else 0.0


def _is_usage_activity(event: dict[str, Any]) -> bool:
    return is_local_usage_import_event(event)


@dataclass(frozen=True)
class DashboardUsageRecord:
    client: str
    provider: str
    model: str | None
    session_id: str
    session_kind: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    usage_additive: bool = True
    usage_normalization_state: str | None = None
    raw_cumulative_input_tokens: int | None = None
    raw_cumulative_output_tokens: int | None = None
    raw_cumulative_cached_input_tokens: int | None = None
    cache_creation_tokens_reported: bool | None = None
    cache_read_tokens_reported: bool | None = None
    cache_creation_5m_input_tokens: int = 0
    cache_creation_1h_input_tokens: int = 0
    reasoning_output_tokens: int = 0
    estimated_cost_usd: float | None = None
    client_reported_cost_usd: float | None = None
    usage_confidence: str | None = None
    cost_confidence: str | None = None
    started_at: float | None = None
    updated_at: float | None = None
    source_file: str | None = None
    project_dir: str | None = None
    raw_event: dict[str, Any] | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def total_tokens_including_cached(self) -> int:
        return self.input_tokens + self.output_tokens + self.cached_input_tokens


@dataclass(frozen=True)
class DashboardUsageView:
    local_records: list[DashboardUsageRecord]
    saved_records: list[DashboardUsageRecord]
    excluded_local_records: list[DashboardUsageRecord]
    excluded_saved_records: list[DashboardUsageRecord]
    local_by_client: dict[str, list[DashboardUsageRecord]]
    saved_by_client: dict[str, list[DashboardUsageRecord]]
    # Recording calls agentacct REFUSED, derived from the same live client-log
    # scan that produced local_records. Nothing about a refusal is stored, so
    # this is re-derived on every scan — which is also why refusals that
    # predate the feature are counted without a backfill.
    refused_recording: dict[str, Any] = dataclass_field(default_factory=dict)


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _usage_record_time(record: DashboardUsageRecord) -> float:
    preferred = record.started_at if record.client == "hermes" else record.updated_at
    timestamp = preferred or record.updated_at or record.started_at
    if timestamp and math.isfinite(timestamp) and timestamp > 0:
        return timestamp
    if record.raw_event is not None:
        return _session_activity_time(record.raw_event)
    return 0.0


def _estimated_equivalent_cost(event: ClientUsageEvent) -> float | None:
    if not event.model or not has_model_price(event.provider, event.model):
        return None
    return estimate_model_cost_breakdown_usd(
        event.provider,
        event.model,
        input_tokens=event.input_tokens,
        output_tokens=event.output_tokens,
        cache_creation_input_tokens=event.cache_creation_input_tokens,
        cache_read_input_tokens=event.cache_read_input_tokens,
        cache_creation_5m_input_tokens=event.cache_creation_5m_input_tokens,
        cache_creation_1h_input_tokens=event.cache_creation_1h_input_tokens,
    )["total_cost_usd"]


def _record_from_client_usage(event: ClientUsageEvent) -> DashboardUsageRecord:
    estimated_cost = _estimated_equivalent_cost(event)
    usage_additive, normalization_state = local_usage_additivity(
        client=event.client,
        session_kind=event.client_session_kind,
        parent_client_session_id=event.parent_client_session_id,
        usage_update_semantics=event.usage_update_semantics,
        source_namespace_fingerprint=event.source_namespace_fingerprint,
        parent_source_namespace_fingerprint=(
            event.source_namespace_fingerprint if event.parent_client_session_id else None
        ),
    )
    return DashboardUsageRecord(
        client=event.client,
        provider=event.provider,
        model=event.model,
        session_id=event.client_session_id,
        session_kind=event.client_session_kind or "root",
        input_tokens=event.input_tokens if usage_additive else 0,
        output_tokens=event.output_tokens if usage_additive else 0,
        cached_input_tokens=event.cached_input_tokens if usage_additive else 0,
        cache_creation_input_tokens=event.cache_creation_input_tokens if usage_additive else 0,
        cache_read_input_tokens=event.cache_read_input_tokens if usage_additive else 0,
        usage_additive=usage_additive,
        usage_normalization_state=normalization_state,
        raw_cumulative_input_tokens=event.input_tokens if not usage_additive else None,
        raw_cumulative_output_tokens=event.output_tokens if not usage_additive else None,
        raw_cumulative_cached_input_tokens=event.cached_input_tokens if not usage_additive else None,
        cache_creation_tokens_reported=getattr(event, "cache_creation_tokens_reported", None),
        cache_read_tokens_reported=getattr(event, "cache_read_tokens_reported", None),
        cache_creation_5m_input_tokens=event.cache_creation_5m_input_tokens if usage_additive else 0,
        cache_creation_1h_input_tokens=event.cache_creation_1h_input_tokens if usage_additive else 0,
        reasoning_output_tokens=event.reasoning_output_tokens if usage_additive else 0,
        estimated_cost_usd=estimated_cost if usage_additive else None,
        client_reported_cost_usd=event.client_reported_cost_usd if usage_additive else None,
        usage_confidence="client_reported",
        cost_confidence=(
            "client_reported"
            if usage_additive and event.client_reported_cost_usd is not None
            else "estimated_from_tokens"
            if usage_additive and estimated_cost is not None
            else None
        ),
        started_at=float(event.started_at) if event.started_at is not None else None,
        updated_at=float(event.updated_at) if event.updated_at is not None else None,
        source_file=event.source_path.name,
        project_dir=event.cwd,
    )


def _record_from_sentinel_event(event: dict[str, Any]) -> DashboardUsageRecord | None:
    if not _is_usage_activity(event):
        return None
    metadata = _metadata(event)
    client = str(metadata.get("client") or event.get("source") or "unknown")
    # Import rows only (guaranteed by _is_usage_activity): normalize away our
    # own legacy ':model:' key artifact so un-migrated stores stop showing
    # phantom per-model sessions. Only claude-code rows ever carried the
    # marker; child ':stem' suffixes are preserved.
    session_id = normalized_local_usage_session_id(
        metadata.get("client"), str(metadata.get("client_session_id") or event.get("run_id") or "")
    )
    cached = _metadata_int(event, "cached_input_tokens")
    cache_creation = _cache_creation_tokens(event)
    cache_read = _cache_read_tokens(event)
    cost = _safe_float(event.get("estimated_cost_usd"))
    cost_confidence = str(event.get("cost_confidence") or "") or None
    usage_additive, normalization_state = local_usage_additivity(
        client=client,
        session_kind=metadata.get("client_session_kind"),
        parent_client_session_id=metadata.get("parent_client_session_id"),
        usage_update_semantics=metadata.get("usage_update_semantics"),
        explicit_usage_additive=metadata.get("usage_additive"),
        source_namespace_fingerprint=metadata.get("source_namespace_fingerprint"),
        parent_source_namespace_fingerprint=metadata.get("parent_source_namespace_fingerprint"),
    )
    input_tokens = _safe_nonnegative_event_int(event, "estimated_input_tokens")
    output_tokens = _safe_nonnegative_event_int(event, "estimated_output_tokens")
    return DashboardUsageRecord(
        client=client,
        provider=str(event.get("provider") or client),
        model=str(event.get("model")) if event.get("model") else None,
        session_id=session_id,
        session_kind=str(metadata.get("client_session_kind") or "root"),
        input_tokens=input_tokens if usage_additive else 0,
        output_tokens=output_tokens if usage_additive else 0,
        cached_input_tokens=cached if usage_additive else 0,
        cache_creation_input_tokens=cache_creation if usage_additive else 0,
        cache_read_input_tokens=cache_read if usage_additive else 0,
        usage_additive=usage_additive,
        usage_normalization_state=normalization_state,
        raw_cumulative_input_tokens=input_tokens if not usage_additive else None,
        raw_cumulative_output_tokens=output_tokens if not usage_additive else None,
        raw_cumulative_cached_input_tokens=cached if not usage_additive else None,
        cache_creation_tokens_reported=(
            bool(metadata.get("cache_creation_tokens_reported"))
            if isinstance(metadata.get("cache_creation_tokens_reported"), bool)
            else None
        ),
        cache_read_tokens_reported=(
            bool(metadata.get("cache_read_tokens_reported"))
            if isinstance(metadata.get("cache_read_tokens_reported"), bool)
            else None
        ),
        cache_creation_5m_input_tokens=_metadata_int(event, "cache_creation_5m_input_tokens"),
        cache_creation_1h_input_tokens=_metadata_int(event, "cache_creation_1h_input_tokens"),
        reasoning_output_tokens=_metadata_int(event, "reasoning_output_tokens") if usage_additive else 0,
        estimated_cost_usd=cost if usage_additive else None,
        client_reported_cost_usd=cost if usage_additive and cost_confidence == "client_reported" else None,
        usage_confidence=str(event.get("usage_confidence") or "") or None,
        cost_confidence=cost_confidence if usage_additive else None,
        started_at=_safe_float(metadata.get("started_at")),
        updated_at=_safe_float(metadata.get("updated_at")),
        source_file=str(metadata.get("source_file")) if metadata.get("source_file") else None,
        project_dir=str(metadata.get("project_dir")) if metadata.get("project_dir") else None,
        raw_event=event,
    )


def _safe_nonnegative_event_int(event: dict[str, Any], key: str) -> int:
    try:
        value = int(event.get(key) or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _usage_records_by_client(records: list[DashboardUsageRecord]) -> dict[str, list[DashboardUsageRecord]]:
    by_client: dict[str, list[DashboardUsageRecord]] = {}
    for record in records:
        by_client.setdefault(record.client, []).append(record)
    return by_client


def _build_usage_view(local_usage_preview: list[ClientUsageEvent], events: list[dict[str, Any]]) -> DashboardUsageView:
    all_local_records = [_record_from_client_usage(event) for event in local_usage_preview]
    # Shared legacy-store rule (usage_truth): stale claude-code base rows
    # shadowed by ':model:' siblings are excluded exactly like the work ledger
    # and the context bridge exclude them, so all three surfaces agree.
    kept_events, _shadowed = split_shadowed_legacy_usage_events(events)
    ambiguous_source_identities = (
        local_usage_source_namespace_ambiguous_identities(kept_events)
    )
    all_saved_records = [record for event in kept_events if (record := _record_from_sentinel_event(event)) is not None]
    if ambiguous_source_identities:
        all_saved_records = [
            _quarantine_ambiguous_dashboard_usage_record(
                record,
                ambiguous_source_identities,
            )
            for record in all_saved_records
        ]
    local_records = [record for record in all_local_records if record.usage_additive]
    excluded_local_records = [record for record in all_local_records if not record.usage_additive]
    saved_records = [record for record in all_saved_records if record.usage_additive]
    excluded_saved_records = [record for record in all_saved_records if not record.usage_additive]
    return DashboardUsageView(
        local_records=local_records,
        saved_records=saved_records,
        excluded_local_records=excluded_local_records,
        excluded_saved_records=excluded_saved_records,
        local_by_client=_usage_records_by_client(local_records),
        saved_by_client=_usage_records_by_client(saved_records),
        # Derived from the raw scan candidates (not the records), because the
        # summary dedups per base session itself: one Claude Code transcript
        # becomes several per-model records carrying the SAME refusal counts.
        refused_recording=summarize_refused_recording_attempts(local_usage_preview),
    )


def _quarantine_ambiguous_dashboard_usage_record(
    record: DashboardUsageRecord,
    ambiguous_identities: set[tuple[str, str, str]],
) -> DashboardUsageRecord:
    raw_event = record.raw_event
    identity = (
        recognized_local_usage_row_identity(raw_event)
        if isinstance(raw_event, dict)
        else None
    )
    if identity not in ambiguous_identities:
        return record
    return replace(
        record,
        input_tokens=0,
        output_tokens=0,
        cached_input_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        usage_additive=False,
        usage_normalization_state="source_namespace_missing_vs_explicit",
        raw_cumulative_input_tokens=record.input_tokens,
        raw_cumulative_output_tokens=record.output_tokens,
        raw_cumulative_cached_input_tokens=record.cached_input_tokens,
        cache_creation_5m_input_tokens=0,
        cache_creation_1h_input_tokens=0,
        reasoning_output_tokens=0,
        estimated_cost_usd=None,
        client_reported_cost_usd=None,
        cost_confidence=None,
    )
