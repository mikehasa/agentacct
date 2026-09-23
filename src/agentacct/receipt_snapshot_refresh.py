"""Prove that a trusted usage replacement may retain an older work snapshot.

This is a retention check, not permission to write or a usage deduplicator. The
caller must hold its write transaction and compare the actual old/new log rows.
Every old row must survive, including duplicates. Appends are allowed. Only
trusted, authoritative local usage rows have a small numeric refresh exception;
arbitrary events and every unrecognized field must remain exactly represented.
"""
from __future__ import annotations

from collections import Counter
import json
import math
import re
from typing import Any

from .confidence import USAGE_CLIENT_REPORTED
from .usage_truth import is_local_usage_import_event


# Keep this aligned with ClientUsageEvent.to_sentinel_event, not an open-ended
# suffix/prefix rule. Replay baselines/prefixes describe refolding, and evidence
# counts describe joins: neither belongs in this allowlist. Presence, lineage,
# normalization and pricing provenance remain immutable even when numeric.
_TOKEN_FIELDS = ("estimated_input_tokens", "estimated_output_tokens")
_USAGE_COUNTER_FIELDS = (
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "cache_creation_5m_input_tokens",
    "cache_creation_1h_input_tokens",
    "reasoning_output_tokens",
    "total_tokens",
    "raw_cumulative_input_tokens",
    "raw_cumulative_cached_input_tokens",
    "raw_cumulative_output_tokens",
    "raw_cumulative_reasoning_output_tokens",
    "turn_count",
    "raw_usage_rows",
    "deduplicated_usage_rows",
)
_GENERATED_EVENT_ID = re.compile(r"evt_[0-9a-f]{12}\Z")


def _normalize_numbers(
    row: dict[str, Any], fields: tuple[str, ...], *, integers: bool = False
) -> None:
    for field in fields:
        if field not in row or row[field] is None:
            # Preserve both field presence and nullability. A previously absent
            # measurement is not treated as an already measured zero.
            continue
        value = row[field]
        if (
            isinstance(value, bool)
            or not isinstance(value, int if integers else (int, float))
            or value < 0
            or (isinstance(value, float) and not math.isfinite(value))
        ):
            raise ValueError("invalid numeric usage refresh field")
        row[field] = 0


def _row_signature(event: dict[str, Any]) -> str:
    if not isinstance(event, dict):
        raise ValueError("a primary log row must be an object")
    metadata = event.get("metadata")
    refreshable = (
        is_local_usage_import_event(event)
        and event.get("usage_confidence") == USAGE_CLIENT_REPORTED
        and isinstance(metadata.get("client"), str)
        and isinstance(metadata.get("client_session_id"), str)
        and event.get("source") == f"{metadata['client']}-local-session-import"
        and metadata.get("usage_additive") is True
        and metadata.get("precedence_role", "authoritative") == "authoritative"
    )
    if not refreshable:
        return "exact:" + json.dumps(event, sort_keys=True, allow_nan=False)

    normalized = dict(event)
    normalized["metadata"] = metadata = dict(metadata)
    _normalize_numbers(normalized, _TOKEN_FIELDS, integers=True)
    _normalize_numbers(normalized, ("estimated_cost_usd", "created_at"))
    _normalize_numbers(metadata, _USAGE_COUNTER_FIELDS, integers=True)
    _normalize_numbers(metadata, ("client_reported_cost_usd", "updated_at", "source_revision_at"))
    # Only service-minted IDs have this exception. Caller IDs and unknown future
    # identity schemes remain exact; don't blanket-drop identity-looking fields.
    event_id = normalized.get("event_id")
    generated_id = isinstance(event_id, str) and _GENERATED_EVENT_ID.fullmatch(event_id)
    if generated_id:
        normalized["event_id"] = "generated-local-usage-event"
    # Distinguish our marker from an actual caller-supplied string with the same
    # spelling: only two recognized generated IDs may compare as equivalent.
    prefix = "usage-generated:" if generated_id else "usage:"
    return prefix + json.dumps(normalized, sort_keys=True, allow_nan=False)


def usage_refresh_preserves_snapshots(
    previous: list[dict[str, Any]], replacement: list[dict[str, Any]]
) -> bool:
    """Whether replacement retains all old facts except allowed usage counters.

    Ordering is immaterial, since import replaces/remints rows at the log tail.
    Multiset containment prevents a merge or dedup from masquerading as refresh.
    Missing/added counter fields, unknown-to-priced provenance changes, legacy or
    non-authoritative usage, redaction, and replay refolding fail conservatively.
    Invalid input also fails closed. Neither input is mutated.
    """
    if len(replacement) < len(previous):
        return False
    try:
        remaining = Counter(_row_signature(event) for event in replacement)
        for event in previous:
            signature = _row_signature(event)
            if remaining[signature] <= 0:
                return False
            remaining[signature] -= 1
    except (ValueError, TypeError, OverflowError, RecursionError):
        return False
    return True
