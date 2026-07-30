from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import tempfile
import time
import uuid
from collections import Counter
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .canonical_live import CanonicalLiveRuntime
from .canonical_read import CanonicalReadRuntime
from .confidence import normalize_cost_confidence, normalize_usage_confidence
from .context_bridge import build_usage_context_bridge
from .evidence import is_sensitive_metadata_key
from .evidence_runtime import EvidenceRuntime
from .finding_disposition import (
    FINDING_DISPOSITION_AUTHORITY_SCOPE,
    FINDING_DISPOSITION_CONTRACT_KEY,
    FINDING_DISPOSITION_CONTRACT_VERSION,
    FINDING_DISPOSITION_EVENT_TYPE,
    FINDING_DISPOSITION_SOURCE,
    FindingDispositionConflict,
    FindingDispositionNotFound,
    canonical_event_digest,
    disposition_transition,
    finding_operation_digest,
    finding_target_digest,
    is_trusted_finding_disposition_event,
    reduce_finding_dispositions,
)
from .outcome import build_judge_package, build_machine_check_outcome, compute_advisory_value_score, read_outcome, write_outcome
from .reports import build_run_report_payload
from .storage import RunStore
from .usage_truth import (
    is_local_usage_import_event,
    is_local_session_observation_event,
    local_session_observation_revision,
    local_usage_event_additivity,
    local_session_observation_event_key,
    normalized_local_usage_session_id,
    recognized_local_usage_row_identity,
    mark_trusted_instrumentation_marker_event,
    mark_trusted_local_usage_import_event,
    mark_trusted_local_session_observation_event,
    reduce_local_session_observation_events,
    strip_untrusted_instrumentation_marker_metadata,
    strip_untrusted_session_observation_metadata,
    strip_untrusted_usage_truth_metadata,
)


# Server-authored provenance written only by the MCP section/attach handlers
# (inheritance provenance plus the client_context_keys_authored marker for
# explicitly validated ids). Reserved so no generic recording path (MCP
# record_event, CLI, HTTP API) can forge provenance: ids without the authored
# marker are capped below `exact` by the join rules.
RESERVED_CLIENT_CONTEXT_PROVENANCE_KEYS = (
    "client_context_inherited_keys",
    "client_context_inherited_from_event_id",
    "client_context_source",
    "context_freshness",
    "client_context_inherited_from",
    "client_context_keys_authored",
    "client_context_inheritance_refused",
    "client_context_selection",
    "hook_context_fresh_count",
)

# Only the validated machine-check MCP handler may stamp this marker. Generic
# event writers can carry similarly named metadata for diagnostics, but the
# work ledger will not treat it as authority to change an active blocker.
BLOCKER_RESOLUTION_CONTRACT_KEY = "blocker_resolution_contract"
BLOCKER_RESOLUTION_CONTRACT_VERSION = "server_validated_v1"


class SessionObservationConflict(ValueError):
    """A trusted local observation cannot be safely ordered or namespaced."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _session_observation_conflict_reason(diagnostics: Mapping[str, int]) -> str:
    if int(diagnostics.get("namespace_conflict_sessions") or 0):
        return "source_namespace_conflict"
    if int(diagnostics.get("watermark_conflict_sessions") or 0):
        return "same_watermark_conflict"
    return "source_watermark_unorderable"


def strip_client_context_provenance(event: dict[str, Any]) -> dict[str, Any]:
    metadata = event.get("metadata")
    if not isinstance(metadata, dict) or not any(key in metadata for key in RESERVED_CLIENT_CONTEXT_PROVENANCE_KEYS):
        return event
    sanitized = dict(metadata)
    for key in RESERVED_CLIENT_CONTEXT_PROVENANCE_KEYS:
        sanitized.pop(key, None)
    # frozen metadata key (pre-rename tombstone vocabulary, stored forever).
    sanitized["reserved_client_context_provenance_stripped"] = True
    recorded = dict(event)
    recorded["metadata"] = sanitized
    return recorded


def strip_blocker_resolution_provenance(event: dict[str, Any]) -> dict[str, Any]:
    metadata = event.get("metadata")
    if not isinstance(metadata, dict) or BLOCKER_RESOLUTION_CONTRACT_KEY not in metadata:
        return event
    sanitized = dict(metadata)
    sanitized.pop(BLOCKER_RESOLUTION_CONTRACT_KEY, None)
    sanitized["reserved_blocker_resolution_provenance_stripped"] = True
    recorded = dict(event)
    recorded["metadata"] = sanitized
    return recorded


def mark_trusted_blocker_resolution(event: dict[str, Any]) -> dict[str, Any]:
    recorded = dict(event)
    metadata = dict(event.get("metadata") or {})
    metadata[BLOCKER_RESOLUTION_CONTRACT_KEY] = BLOCKER_RESOLUTION_CONTRACT_VERSION
    recorded["metadata"] = metadata
    return recorded


def strip_finding_disposition_provenance(event: dict[str, Any]) -> dict[str, Any]:
    metadata = event.get("metadata")
    if not isinstance(metadata, dict) or FINDING_DISPOSITION_CONTRACT_KEY not in metadata:
        return event
    sanitized = dict(metadata)
    sanitized.pop(FINDING_DISPOSITION_CONTRACT_KEY, None)
    sanitized["reserved_finding_disposition_provenance_stripped"] = True
    recorded = dict(event)
    recorded["metadata"] = sanitized
    return recorded


def mark_trusted_finding_disposition(event: dict[str, Any]) -> dict[str, Any]:
    recorded = dict(event)
    metadata = dict(event.get("metadata") or {})
    metadata[FINDING_DISPOSITION_CONTRACT_KEY] = FINDING_DISPOSITION_CONTRACT_VERSION
    recorded["metadata"] = metadata
    return recorded


# Secret shapes recognized inside ordinary string VALUES, ordered
# most-specific-first so the reported pattern class is the precise one.
#
# Both anchors are load-bearing. Unanchored, `sk-` matched the middle of
# ordinary English and ordinary identifiers -- "task-", "disk-", "risk-",
# "brisk-" all contain "sk-" -- and "Bearer" followed by any non-delimiter run
# matched the prose phrase "use a Bearer token here". Paired with the old
# whole-string replacement that silently destroyed real ledger values: a path
# ending
# ".../work-task-sessions-4801fa" was stored as a bare placeholder, and
# unrelated section ids that each tripped the pattern all collapsed onto the
# same literal string and merged into one phantom section. A quietly wrong
# record is worse than the leak it was guarding against, so the patterns now
# require a word boundary before `sk-` and something credential-shaped (rather
# than an English word) after `Bearer`.
SECRET_VALUE_PATTERNS = (
    (
        "bearer_token",
        re.compile(
            r"\bBearer\s+(?=[A-Za-z0-9._~+/=-]*[0-9._~+/=-])[A-Za-z0-9._~+/=-]{16,}",
            re.IGNORECASE,
        ),
    ),
    ("openrouter_api_key", re.compile(r"\bsk-or-v1-[A-Za-z0-9_-]{12,}")),
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{12,}")),
)

# Only the matched span is replaced, so surrounding prose, ids and paths
# survive. Deliberately distinct from the "[REDACTED]" used for a sensitive key
# NAME (where dropping the whole value is the right call) so a reader can tell
# "a secret was cut out of this value" from "this field was a credential", and
# built from characters no pattern above can match, so redaction is idempotent.
SECRET_SPAN_PLACEHOLDER = "[REDACTED_SECRET]"

# Server-authored redaction provenance. Written only by the recording paths
# when a span was actually replaced, and stripped from caller input first so an
# agent cannot claim a redaction that never happened. It lives in server-owned
# metadata and never in-band inside the value itself: an in-band marker would
# just be another string eligible for redaction. The names deliberately avoid
# the words is_sensitive_metadata_key() treats as credential-ish ("secret",
# "credential", ...), which would blank the marker's own value.
VALUE_REDACTION_MARKER_KEY = "value_redaction_applied"
VALUE_REDACTION_FIELDS_KEY = "value_redaction_fields"
RESERVED_VALUE_REDACTION_KEYS = (
    VALUE_REDACTION_MARKER_KEY,
    VALUE_REDACTION_FIELDS_KEY,
)


def strip_value_redaction_provenance(event: dict[str, Any]) -> dict[str, Any]:
    metadata = event.get("metadata")
    if not isinstance(metadata, dict) or not any(key in metadata for key in RESERVED_VALUE_REDACTION_KEYS):
        return event
    sanitized = dict(metadata)
    for key in RESERVED_VALUE_REDACTION_KEYS:
        sanitized.pop(key, None)
    sanitized["reserved_value_redaction_provenance_stripped"] = True
    recorded = dict(event)
    recorded["metadata"] = sanitized
    return recorded


def _redact_secret_spans(text: str) -> tuple[str, tuple[str, ...]]:
    """Replace secret-shaped spans in ``text``, keeping everything else verbatim."""

    matched: list[str] = []
    for pattern_class, pattern in SECRET_VALUE_PATTERNS:
        text, replacements = pattern.subn(SECRET_SPAN_PLACEHOLDER, text)
        if replacements:
            matched.append(pattern_class)
    return text, tuple(matched)


def _redact_secrets(
    value: Any,
    *,
    path: str = "",
    found: list[dict[str, str]] | None = None,
) -> Any:
    """Copy ``value`` redacted, appending ``field``/``pattern_class`` rows to ``found``.

    Sensitive key NAMES still lose the whole value: that is a decision about
    the field, not a match against its content. Value patterns only take the
    span they matched.
    """

    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if is_sensitive_metadata_key(key):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = _redact_secrets(child, path=child_path, found=found)
        return redacted
    if isinstance(value, (list, tuple)):
        items = [
            _redact_secrets(
                item,
                path=f"{path}.{index}" if path else str(index),
                found=found,
            )
            for index, item in enumerate(value)
        ]
        return items if isinstance(value, list) else tuple(items)
    if isinstance(value, str):
        text, matched = _redact_secret_spans(value)
        if matched and found is not None:
            found.extend({"field": path, "pattern_class": name} for name in matched)
        return text
    return value


def _stamp_value_redaction_marker(
    event: dict[str, Any],
    redactions: list[dict[str, str]],
) -> dict[str, Any]:
    if not redactions:
        return event
    detail = sorted(redactions, key=lambda row: (row["field"], row["pattern_class"]))
    stamped = dict(event)
    metadata = stamped.get("metadata")
    if metadata is None or isinstance(metadata, dict):
        marked = dict(metadata or {})
        marked[VALUE_REDACTION_MARKER_KEY] = True
        marked[VALUE_REDACTION_FIELDS_KEY] = detail
        stamped["metadata"] = marked
    else:
        # Off-contract metadata is never rewritten, but the repair still has to
        # be visible, so the marker lands beside it instead of replacing it.
        stamped[VALUE_REDACTION_MARKER_KEY] = True
        stamped[VALUE_REDACTION_FIELDS_KEY] = detail
    return stamped


def redact_event_secrets(event: dict[str, Any]) -> dict[str, Any]:
    """Redact one event and stamp server-authored provenance for what was cut."""

    sanitized = strip_value_redaction_provenance(event)
    redactions: list[dict[str, str]] = []
    redacted = _redact_secrets(sanitized, found=redactions)
    return _stamp_value_redaction_marker(redacted, redactions)


def _safe_nonnegative_int(value: Any) -> int:
    try:
        number = float(0 if value is None else value)
    except (OverflowError, TypeError, ValueError):
        return 0
    if not math.isfinite(number) or number <= 0:
        return 0
    return int(number)


def _safe_event_cost(value: Any) -> float:
    try:
        cost = float(0.0 if value is None else value)
    except (OverflowError, TypeError, ValueError):
        return 0.0
    if math.isfinite(cost) and cost > 0:
        return cost
    return 0.0


def _safe_metadata_tokens(event: dict[str, Any], key: str) -> int:
    metadata = event.get("metadata")
    if not isinstance(metadata, dict):
        return 0
    return _safe_nonnegative_int(metadata.get(key))


def _add_tokens(
    target: dict[str, Any],
    *,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
    reasoning_output_tokens: int,
    cost: float,
) -> None:
    target["event_count"] += 1
    target["estimated_cost_usd"] += cost
    target["estimated_input_tokens"] += input_tokens
    target["estimated_output_tokens"] += output_tokens
    target["estimated_total_tokens"] += input_tokens + output_tokens
    target["cached_input_tokens"] += cached_input_tokens
    target["cache_creation_input_tokens"] += cache_creation_input_tokens
    target["cache_read_input_tokens"] += cache_read_input_tokens
    target["reasoning_output_tokens"] += reasoning_output_tokens
    target["total_tokens_including_cached"] += input_tokens + output_tokens + cached_input_tokens


def _empty_token_summary() -> dict[str, Any]:
    return {
        "event_count": 0,
        "estimated_cost_usd": 0.0,
        "estimated_input_tokens": 0,
        "estimated_output_tokens": 0,
        "estimated_total_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens_including_cached": 0,
    }


def summarize_events(
    events: list[dict[str, Any]],
    *,
    limit: int,
    canonical_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Summarize a recent window while keeping canonical bridge truth complete.

    ``events`` remains the caller-visible recent window for backwards-compatible
    event/token aggregates. When ``canonical_events`` is supplied, coverage and
    attribution are computed over that complete scope; only the bridge's verbose
    detail arrays are capped by ``limit``.
    """

    canonical_scope = events if canonical_events is None else canonical_events
    totals = _empty_token_summary()
    tokens_by_provider: dict[str, dict[str, Any]] = {}
    usage_confidence_counts: Counter[str] = Counter()
    cost_confidence_counts: Counter[str] = Counter()
    excluded_non_additive_usage_events = 0
    for event in events:
        if not _is_usage_truth_event(event):
            continue
        usage_additive, _state = local_usage_event_additivity(event)
        if not usage_additive:
            excluded_non_additive_usage_events += 1
            continue
        cost = _safe_event_cost(event.get("estimated_cost_usd"))
        input_tokens = _safe_nonnegative_int(event.get("estimated_input_tokens"))
        output_tokens = _safe_nonnegative_int(event.get("estimated_output_tokens"))
        cached_input_tokens = _safe_metadata_tokens(event, "cached_input_tokens")
        cache_creation_input_tokens = _safe_metadata_tokens(event, "cache_creation_input_tokens")
        cache_read_input_tokens = _safe_metadata_tokens(event, "cache_read_input_tokens")
        if cache_creation_input_tokens + cache_read_input_tokens <= 0:
            cache_read_input_tokens = cached_input_tokens
        reasoning_output_tokens = _safe_metadata_tokens(event, "reasoning_output_tokens")
        usage_confidence_counts[normalize_usage_confidence(event.get("usage_confidence"))] += 1
        cost_confidence_counts[normalize_cost_confidence(event.get("cost_confidence"))] += 1
        next_cost = totals["estimated_cost_usd"] + cost
        if math.isfinite(next_cost):
            safe_cost = cost
        else:
            safe_cost = 0.0
        _add_tokens(
            totals,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            reasoning_output_tokens=reasoning_output_tokens,
            cost=safe_cost,
        )
        provider = event.get("provider")
        if provider:
            provider_key = str(provider)
            provider_summary = tokens_by_provider.setdefault(provider_key, _empty_token_summary())
            provider_next_cost = provider_summary["estimated_cost_usd"] + cost
            provider_safe_cost = cost if math.isfinite(provider_next_cost) else 0.0
            _add_tokens(
                provider_summary,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached_input_tokens,
                cache_creation_input_tokens=cache_creation_input_tokens,
                cache_read_input_tokens=cache_read_input_tokens,
                reasoning_output_tokens=reasoning_output_tokens,
                cost=provider_safe_cost,
            )
    return {
        "event_count": len(events),
        "limit": limit,
        "result_scope": {
            "partial": len(events) < len(canonical_scope),
            "event_limit": limit,
            "events_summarized": len(events),
            "events_total": len(canonical_scope),
            "canonical_metrics_scope": "provided_events" if canonical_events is None else "all_matching_store_events",
        },
        "note_count": sum(1 for event in events if event.get("event_type") == "note"),
        "estimated_cost_usd": totals["estimated_cost_usd"],
        "estimated_input_tokens": totals["estimated_input_tokens"],
        "estimated_output_tokens": totals["estimated_output_tokens"],
        "estimated_total_tokens": totals["estimated_total_tokens"],
        "cached_input_tokens": totals["cached_input_tokens"],
        "cache_creation_input_tokens": totals["cache_creation_input_tokens"],
        "cache_read_input_tokens": totals["cache_read_input_tokens"],
        "reasoning_output_tokens": totals["reasoning_output_tokens"],
        "total_tokens_including_cached": totals["total_tokens_including_cached"],
        "excluded_non_additive_usage_events": excluded_non_additive_usage_events,
        "tokens_by_provider": tokens_by_provider,
        "by_usage_confidence": dict(usage_confidence_counts),
        "by_cost_confidence": dict(cost_confidence_counts),
        "by_source": dict(Counter(str(event.get("source") or "unknown") for event in events)),
        "by_type": dict(Counter(str(event.get("event_type") or "unknown") for event in events)),
        "by_provider": dict(Counter(str(event.get("provider") or "unknown") for event in events if event.get("provider"))),
        "usage_context_bridge": build_usage_context_bridge(canonical_scope, detail_limit=limit),
    }


def _is_usage_truth_event(event: dict[str, Any]) -> bool:
    return is_local_usage_import_event(event)


def _normalize_finding_disposition_note(note: str | None, *, action: str) -> str | None:
    normalized = note.strip() if isinstance(note, str) and note.strip() else None
    if normalized is not None:
        if len(normalized) > 1200 or any(char in normalized for char in "\r\n\x00"):
            raise FindingDispositionConflict("finding disposition note is invalid")
        normalized, _matched = _redact_secret_spans(normalized)
    if action == "resolve" and not normalized:
        raise FindingDispositionConflict("resolving a finding requires a note")
    return normalized


def _finding_event_matches_task_scope(
    event: Mapping[str, Any],
    task_scope: Mapping[str, Any],
) -> bool:
    session_keys = {
        (str(row.get("client") or ""), str(row.get("client_session_id") or ""))
        for row in (
            task_scope.get("session_keys")
            if isinstance(task_scope.get("session_keys"), list)
            else []
        )
        if isinstance(row, Mapping) and row.get("client") and row.get("client_session_id")
    }
    event_session = (
        str(event.get("client") or event.get("source") or ""),
        str(event.get("client_session_id") or ""),
    )
    if event_session[1] and event_session in session_keys:
        return True
    work_ids = {
        str(value)
        for value in (
            task_scope.get("work_ids")
            if isinstance(task_scope.get("work_ids"), list)
            else []
        )
        if value
    }
    if any(str(value or "") in work_ids for value in (event.get("work_id"), event.get("section_id"))):
        return True
    run_ids = {
        str(value)
        for value in (
            task_scope.get("run_ids")
            if isinstance(task_scope.get("run_ids"), list)
            else []
        )
        if value
    }
    return bool(event.get("run_id") and str(event.get("run_id")) in run_ids)


class SentinelService:
    """Core local service shared by CLI, HTTP API, sidecar, and future MCP tools."""

    def __init__(
        self,
        store_dir: Path | str,
        *,
        create: bool = True,
        evidence_v2_enabled: bool | None = None,
        canonical_live_enabled: bool | None = None,
        canonical_read_enabled: bool | None = None,
    ) -> None:
        self.store = RunStore(store_dir, create=create)
        self.events_path = self.store.root / "events.jsonl"
        self._events_lock_path = self.store.root / "events.jsonl.lock"
        # Evidence v2 is an additive shadow boundary. Construction is lazy and
        # disabling it leaves the historical events.jsonl behavior untouched.
        self.evidence = EvidenceRuntime(self.store.root, enabled=evidence_v2_enabled)
        # Canonical live shadow (migration phase 3): same contract, default
        # OFF — nothing is opened or created until the flag enables it.
        self.canonical_live = CanonicalLiveRuntime(
            self.store.root, enabled=canonical_live_enabled
        )
        # Canonical read path (migration phase 4): independently gated from
        # the write flag, default OFF — with the flag off every read surface
        # stays on the proven v1 path, byte-identical.
        self.canonical_read = CanonicalReadRuntime(
            self.store.root, enabled=canonical_read_enabled
        )

    @contextmanager
    def _events_write_lock(self) -> Iterator[None]:
        """Advisory cross-process lock serializing every events.jsonl writer.

        Multiple live processes share one store (dashboards, MCP servers,
        usage watchers, CLI imports). Appends racing a replace_events
        read-modify-replace would otherwise be silently deleted, and two
        concurrent rewrites would interleave into torn JSONL. The lock file is
        separate from events.jsonl so acquiring it never touches ledger bytes.

        POSIX-only (fcntl.flock), like the rest of the product; the blocking
        acquire is fine because the critical sections are short local-file
        rewrites. Readers stay lock-free: they already tolerate the
        atomic-rename pattern.
        """
        # Create owner-only: the default-umask 0644 this used to inherit is
        # both a privacy leak and a hard failure in the rebuild suite's
        # writer-lock gates, which demand exactly 0600.
        descriptor = os.open(self._events_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _read_events_file_order(self, *, run_id: str | None = None) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if run_id is None or event.get("run_id") == run_id:
                events.append(event)
        return events

    def _prepare_recorded_event(self, event: dict[str, Any]) -> dict[str, Any]:
        recorded = redact_event_secrets(dict(event))
        # Server-owned fields are always assigned locally. Caller-provided values
        # must not be able to break sorting or spoof event identity.
        recorded["event_id"] = "evt_" + uuid.uuid4().hex[:12]
        recorded["created_at"] = time.time()
        return recorded

    def _find_idempotent_event(self, event: dict[str, Any], idempotency_key: str) -> dict[str, Any] | None:
        for existing in reversed(self._read_events_file_order()):
            metadata = existing.get("metadata")
            if not isinstance(metadata, dict):
                continue
            if metadata.get("idempotency_key") != idempotency_key:
                continue
            if existing.get("source") != event.get("source"):
                continue
            if existing.get("event_type") != event.get("event_type"):
                continue
            if existing.get("run_id") != event.get("run_id"):
                continue
            return existing
        return None

    def record_event(
        self,
        event: dict[str, Any],
        *,
        trusted_usage_import: bool = False,
        trusted_session_observation_import: bool = False,
        preserve_client_context_provenance: bool = False,
        trusted_instrumentation_marker: bool = False,
        trusted_blocker_resolution: bool = False,
        transport: str | None = None,
    ) -> dict[str, Any]:
        """Record a local agent/runtime event for dashboard and MCP ingestion.

        This is intentionally provider-agnostic: integrations can send model
        usage, task lifecycle, budget decisions, or outcome notes. Secrets are
        redacted before the event is persisted. Client-context inheritance
        provenance is server-authored: only the MCP section handler may pass
        preserve_client_context_provenance=True; every other path gets it
        stripped so callers cannot forge inheritance. Instrumentation-marker
        provenance follows the same rule: only the CLI marker writers pass
        trusted_instrumentation_marker=True, so agent/HTTP events can never
        classify sessions as pre/post instrumentation. Blocker resolutions
        are likewise server-authored only by the validated machine-check MCP
        path; free-form event metadata cannot clear product state.
        """
        if trusted_session_observation_import:
            if trusted_usage_import:
                raise SessionObservationConflict(
                    "mixed_truth_lanes",
                    "an event cannot be both local usage and a session observation",
                )
            return self.record_trusted_session_observation(event, transport=transport)
        event = mark_trusted_local_usage_import_event(event) if trusted_usage_import else strip_untrusted_usage_truth_metadata(event)
        event = strip_untrusted_session_observation_metadata(event)
        event = (
            mark_trusted_instrumentation_marker_event(event)
            if trusted_instrumentation_marker
            else strip_untrusted_instrumentation_marker_metadata(event)
        )
        if not preserve_client_context_provenance:
            event = strip_client_context_provenance(event)
        event = (
            mark_trusted_blocker_resolution(event)
            if trusted_blocker_resolution
            else strip_blocker_resolution_provenance(event)
        )
        # Finding dispositions have stricter revision/idempotency semantics
        # than generic events. Only record_finding_disposition may stamp the
        # reserved contract; generic callers are always stripped.
        event = strip_finding_disposition_provenance(event)
        metadata = event.get("metadata")
        idempotency_key = metadata.get("idempotency_key") if isinstance(metadata, dict) else None
        with self._events_write_lock():
            if isinstance(idempotency_key, str) and idempotency_key:
                existing = self._find_idempotent_event(event, idempotency_key)
                if existing is not None:
                    recorded = existing
                else:
                    recorded = self._prepare_recorded_event(event)
                    with self.events_path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(recorded, sort_keys=True) + "\n")
            else:
                recorded = self._prepare_recorded_event(event)
                with self.events_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(recorded, sort_keys=True) + "\n")
        # Never hold the v1 ledger lock while fsyncing/projecting v2. The
        # shadow call is idempotent, local-only, and fail-open by contract.
        self.evidence.shadow_v1_event(recorded, transport=transport)
        # Canonical live shadow (phase 3.1): same placement and contract as
        # the Evidence-v2 shadow — after the proven v1 write, off the lock.
        self.canonical_live.shadow_v1_event(recorded, transport=transport)
        return recorded

    def _trusted_session_observation_candidate(self, event: dict[str, Any]) -> dict[str, Any]:
        candidate = redact_event_secrets(dict(event))
        candidate = strip_untrusted_usage_truth_metadata(candidate)
        candidate = strip_untrusted_instrumentation_marker_metadata(candidate)
        candidate = strip_client_context_provenance(candidate)
        candidate = strip_blocker_resolution_provenance(candidate)
        candidate = strip_finding_disposition_provenance(candidate)
        candidate = mark_trusted_local_session_observation_event(candidate)
        if not is_local_session_observation_event(candidate):
            raise SessionObservationConflict(
                "invalid_observation",
                "trusted session observation requires event_type=session_observed and valid client/session ids",
            )
        return candidate

    def record_trusted_session_observation(
        self,
        event: dict[str, Any],
        *,
        transport: str | None = "internal",
    ) -> dict[str, Any]:
        """Persist one local observation with source-revision idempotency.

        Only trusted observation rows participate in the comparison.  This is
        deliberately separate from generic ``record_event`` idempotency: an
        untrusted event carrying the same caller key can never pre-occupy this
        lane.  Newer source revisions replace older ones atomically; identical
        content returns the existing row; an older input is a no-op.  Equal
        watermarks with different content and cross-source identities fail
        closed via ``SessionObservationConflict``.
        """

        candidate = self._trusted_session_observation_candidate(event)
        key = local_session_observation_event_key(candidate)
        assert key is not None  # checked by is_local_session_observation_event
        recorded: dict[str, Any] | None = None
        existing_result: dict[str, Any] | None = None
        conflict_error: SessionObservationConflict | None = None
        conflict_rows: list[dict[str, Any]] = []
        with self._events_write_lock():
            existing, preserved_unparseable = self._partition_existing_for_rewrite()
            same_identity = [
                row
                for row in existing
                if is_local_session_observation_event(row)
                and local_session_observation_event_key(row) == key
            ]
            cohort = [*same_identity, candidate]
            selected, diagnostics = reduce_local_session_observation_events(cohort)
            if not selected:
                reason = _session_observation_conflict_reason(diagnostics)
                # Persist the conflicting source fact before refusing it. If
                # only the first writer remained on disk, read-time reduction
                # would keep projecting/donating that row despite the detected
                # collision. The complete trusted cohort is the quarantine:
                # reducers exclude it until an explicit reconciliation.
                candidate_revision = local_session_observation_revision(candidate)
                if not any(
                    local_session_observation_revision(row) == candidate_revision
                    for row in same_identity
                ):
                    conflict_row = self._prepare_recorded_event(candidate)
                    self._write_event_partition_unlocked(
                        [*existing, conflict_row], preserved_unparseable
                    )
                    conflict_rows.append(conflict_row)
                conflict_error = SessionObservationConflict(
                    reason,
                    f"session observation {key[0]}::{key[1]} is ambiguous: {reason}",
                )
            else:
                authority = selected[0]
                if authority is candidate:
                    recorded = self._prepare_recorded_event(candidate)
                    rewritten = [
                        row
                        for row in existing
                        if not (
                            is_local_session_observation_event(row)
                            and local_session_observation_event_key(row) == key
                        )
                    ]
                    self._write_event_partition_unlocked(
                        [*rewritten, recorded], preserved_unparseable
                    )
                else:
                    existing_result = authority
                    # Compact safe historical revisions even on an identical
                    # or older retry. Raw conflict cohorts are never compacted.
                    if len(same_identity) > 1:
                        rewritten = [
                            row
                            for row in existing
                            if not (
                                is_local_session_observation_event(row)
                                and local_session_observation_event_key(row) == key
                            )
                        ]
                        self._write_event_partition_unlocked(
                            [*rewritten, authority], preserved_unparseable
                        )
        if conflict_error is not None:
            if conflict_rows:
                self.evidence.shadow_v1_events(
                    conflict_rows,
                    transport=transport,
                )
                # Quarantined conflict rows shadow too: the canonical
                # absorb/tie machinery keeps the incumbent and the fork stays
                # visible instead of vanishing from the shadow store.
                for conflict_row in conflict_rows:
                    self.canonical_live.shadow_v1_event(conflict_row, transport=transport)
            raise conflict_error
        if recorded is not None:
            self.evidence.shadow_v1_event(recorded, transport=transport)
            self.canonical_live.shadow_v1_event(recorded, transport=transport)
            return recorded
        assert existing_result is not None
        return existing_result

    def reconcile_trusted_session_observation_conflicts(
        self,
        events: list[dict[str, Any]],
        *,
        expected_conflict_revisions: Mapping[
            tuple[str, str], tuple[str, ...]
        ],
        transport: str | None = "internal",
    ) -> list[dict[str, Any]]:
        """Resolve stored conflict cohorts from one verified-complete scan.

        Callers must pass only candidates from client sources whose current
        scan covered the complete configured source set without parse or limit
        errors. A conflict is compacted only when the current candidates for
        that raw identity reduce to one authoritative source revision. Missing
        candidates never delete history, and still-conflicting candidates keep
        the quarantine intact.
        """

        candidates = [
            self._trusted_session_observation_candidate(event)
            for event in events
        ]
        candidates_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for candidate in candidates:
            key = local_session_observation_event_key(candidate)
            assert key is not None
            candidates_by_key.setdefault(key, []).append(candidate)

        recorded: list[dict[str, Any]] = []
        with self._events_write_lock():
            existing, preserved_unparseable = self._partition_existing_for_rewrite()
            # Index every existing session-observation event by key ONCE (O(E)),
            # rather than re-scanning the whole ledger for each candidate key.
            # The previous per-key list comprehension was O(keys x events) —
            # ~6M classification calls on a ~2.4k-session store, ~28s of a single
            # refresh. Iteration order over ``existing`` is preserved, so each
            # key's history is byte-identical to the old per-key filter (same
            # events, same order into reduce_local_session_observation_events).
            existing_observations_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for event in existing:
                if not is_local_session_observation_event(event):
                    continue
                existing_key = local_session_observation_event_key(event)
                if existing_key is None:
                    continue
                existing_observations_by_key.setdefault(existing_key, []).append(event)

            replacements: dict[tuple[str, str], dict[str, Any]] = {}
            for key, current_candidates in candidates_by_key.items():
                history = existing_observations_by_key.get(key, [])
                if len(history) < 2:
                    continue
                expected_revisions = expected_conflict_revisions.get(key)
                if expected_revisions is None or tuple(
                    sorted(
                        local_session_observation_revision(row)
                        for row in history
                    )
                ) != tuple(expected_revisions):
                    # The ledger changed after source discovery. A stale
                    # complete-scan result has no authority to delete a newer
                    # conflicting fact; leave the quarantine untouched.
                    continue
                historical_selected, _historical_diagnostics = (
                    reduce_local_session_observation_events(history)
                )
                if historical_selected:
                    continue
                current_selected, _current_diagnostics = (
                    reduce_local_session_observation_events(current_candidates)
                )
                if len(current_selected) != 1:
                    continue
                replacements[key] = self._prepare_recorded_event(
                    current_selected[0]
                )
            if replacements:
                kept = [
                    event
                    for event in existing
                    if not (
                        is_local_session_observation_event(event)
                        and local_session_observation_event_key(event)
                        in replacements
                    )
                ]
                recorded = list(replacements.values())
                self._write_event_partition_unlocked(
                    [*kept, *recorded], preserved_unparseable
                )
        if recorded:
            self.evidence.shadow_v1_events(recorded, transport=transport)
            for resolved in recorded:
                self.canonical_live.shadow_v1_event(resolved, transport=transport)
        return recorded

    def trusted_session_observation_conflict_snapshot(
        self,
    ) -> dict[tuple[str, str], tuple[str, ...]]:
        """Canonical revisions of currently quarantined observation cohorts."""

        with self._events_write_lock():
            existing, _preserved_unparseable = (
                self._partition_existing_for_rewrite()
            )
            by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for event in existing:
                if not is_local_session_observation_event(event):
                    continue
                key = local_session_observation_event_key(event)
                if key is not None:
                    by_key.setdefault(key, []).append(event)
            snapshot: dict[tuple[str, str], tuple[str, ...]] = {}
            for key, rows in by_key.items():
                selected, _diagnostics = (
                    reduce_local_session_observation_events(rows)
                )
                if selected:
                    continue
                snapshot[key] = tuple(
                    sorted(local_session_observation_revision(row) for row in rows)
                )
            return snapshot

    def record_finding_disposition(
        self,
        *,
        target_event: Mapping[str, Any],
        action: str,
        expected_revision: int,
        note: str | None,
        idempotency_key: str,
        task_scope: Mapping[str, Any] | None = None,
        transport: str = "dashboard",
    ) -> dict[str, Any]:
        """Atomically append one user attention transition for an exact finding.

        This deliberately does not call ``record_event``: ordinary event
        idempotency does not compare payloads.  The target, operation replay,
        revision, transition, and append are checked under one ledger lock.
        """

        if action not in {"mark_reviewed", "resolve", "reopen"}:
            raise FindingDispositionConflict("unsupported finding disposition action")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise FindingDispositionConflict("finding revision must be a non-negative integer")
        if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 240:
            raise FindingDispositionConflict("finding idempotency key is invalid")
        if any(char in idempotency_key for char in "\r\n\x00"):
            raise FindingDispositionConflict("finding idempotency key is invalid")
        normalized_note = _normalize_finding_disposition_note(note, action=action)

        target_event_id = str(target_event.get("event_id") or "").strip()
        target_event_digest = canonical_event_digest(target_event)
        target_finding_digest = finding_target_digest(target_event)
        target_result = str(target_event.get("result") or "").strip().lower()
        if (
            not target_event_id
            or target_finding_digest is None
            or target_result not in {"failed", "error"}
        ):
            raise FindingDispositionNotFound("finding target is not a failed/error episode")
        from .task_outcome import finding_check_key

        target_check_key_digest = hashlib.sha256(
            finding_check_key(target_event).encode("utf-8")
        ).hexdigest()
        operation_digest = finding_operation_digest(
            target_event_id=target_event_id,
            target_event_digest=target_event_digest,
            target_finding_digest=target_finding_digest,
            target_check_key_digest=target_check_key_digest,
            action=action,
            expected_revision=expected_revision,
            note=normalized_note,
        )

        with self._events_write_lock():
            existing_events, unparseable_lines = self._partition_existing_for_rewrite()
            if unparseable_lines:
                raise FindingDispositionConflict(
                    "finding state is unavailable while the event ledger contains unreadable lines"
                )
            disposition_projection = reduce_finding_dispositions(existing_events)
            for existing in existing_events:
                if not is_trusted_finding_disposition_event(existing):
                    continue
                metadata = existing.get("metadata")
                if not isinstance(metadata, dict) or metadata.get("idempotency_key") != idempotency_key:
                    continue
                existing_target = str(metadata.get("target_finding_digest") or "")
                if existing_target in disposition_projection.invalid_targets:
                    raise FindingDispositionConflict(
                        "finding disposition history is conflicting or corrupt"
                    )
                if metadata.get("operation_digest") == operation_digest:
                    return existing
                raise FindingDispositionConflict(
                    "finding idempotency key belongs to a different operation"
                )

            # Keep the source store stable from latest-check validation through
            # the v1 disposition append. The lock order is always v1 -> v2;
            # shadowing acquires v2 only after the v1 lock is released.
            target_source_type = str(target_event.get("source_type") or "")
            mechanical_store = self.evidence.store if target_source_type == "client_hook" else None
            source_lock = mechanical_store._locked() if mechanical_store is not None else nullcontext()
            with source_lock:
                if target_source_type == "mcp_agent_reported":
                    from .task_outcome import latest_check_events
                    from .work_ledger import build_evidence_events

                    projected_events = build_evidence_events(existing_events)
                elif target_source_type == "client_hook":
                    from .mechanical_checks import build_mechanical_check_events
                    from .session_observations import (
                        DEFAULT_MECHANICAL_CONFLICT_GROUP_LIMIT,
                        DEFAULT_MECHANICAL_CONFLICT_ROW_LIMIT,
                        expand_complete_conflict_groups,
                        select_session_projection_envelopes,
                    )
                    from .task_outcome import latest_check_events

                    assert mechanical_store is not None
                    records = mechanical_store.query_recent_source(
                        source_type="client_hook",
                        limit=10_000,
                    )
                    records, complete_conflict_keys = expand_complete_conflict_groups(
                        records,
                        load_group=lambda idempotency_key, limit: mechanical_store.query(
                            limit=limit,
                            order_by="arrival",
                            idempotency_key=idempotency_key,
                        ),
                        group_limit=DEFAULT_MECHANICAL_CONFLICT_GROUP_LIMIT,
                        row_limit=DEFAULT_MECHANICAL_CONFLICT_ROW_LIMIT,
                    )
                    projected_events = build_mechanical_check_events(
                        select_session_projection_envelopes(
                            records,
                            complete_conflict_keys=complete_conflict_keys,
                            diagnostics={},
                        )
                    )
                else:
                    raise FindingDispositionNotFound("finding source is not disposition-capable")

                target_matches = [
                    event
                    for event in projected_events
                    if finding_target_digest(event) == target_finding_digest
                ]
                if len(target_matches) != 1:
                    raise FindingDispositionNotFound("exact finding target is unavailable or ambiguous")
                projected_target = target_matches[0]
                task_scoped = isinstance(task_scope, Mapping)
                scope_events = (
                    [
                        event
                        for event in projected_events
                        if finding_target_digest(event) == target_finding_digest
                        or _finding_event_matches_task_scope(event, task_scope)
                    ]
                    if task_scoped
                    else projected_events
                )
                series_key = finding_check_key(projected_target, task_scoped=task_scoped)
                latest_series = latest_check_events(
                    [
                        event
                        for event in scope_events
                        if finding_check_key(event, task_scoped=task_scoped) == series_key
                    ],
                    task_scoped=task_scoped,
                )
                if (
                    len(latest_series) != 1
                    or finding_target_digest(latest_series[0]) != target_finding_digest
                    or str(latest_series[0].get("result") or "").lower() not in {"failed", "error"}
                ):
                    raise FindingDispositionConflict(
                        "finding changed or was closed by newer evidence"
                    )

                if target_finding_digest in disposition_projection.invalid_targets:
                    raise FindingDispositionConflict(
                        "finding disposition history is conflicting or corrupt"
                    )
                current = disposition_projection.states.get(target_finding_digest)
                current_state = current.state if current is not None else "open"
                current_revision = current.revision if current is not None else 0
                if current_revision != expected_revision:
                    raise FindingDispositionConflict(
                        "finding changed or was closed by newer evidence"
                    )
                next_state = disposition_transition(current_state, action)
                if next_state is None:
                    raise FindingDispositionConflict("finding disposition transition is invalid")

                event = mark_trusted_finding_disposition(
                    {
                        "source": FINDING_DISPOSITION_SOURCE,
                        "event_type": FINDING_DISPOSITION_EVENT_TYPE,
                        "run_id": target_event.get("run_id"),
                        "metadata": {
                            "sentinel_semantic_kind": FINDING_DISPOSITION_EVENT_TYPE,
                            "authority_scope": FINDING_DISPOSITION_AUTHORITY_SCOPE,
                            "authoritative_for_check_result": False,
                            "actor": "dashboard-user",
                            "target_failure_event_id": target_event_id,
                            "target_event_digest": target_event_digest,
                            "target_finding_digest": target_finding_digest,
                            "target_source_type": target_event.get("source_type"),
                            "target_source": target_event.get("source"),
                            "target_client": target_event.get("client"),
                            "target_namespace_fingerprint": (
                                target_event.get("namespace_fingerprint")
                                or target_event.get("session_namespace_fingerprint")
                            ),
                            "target_project_identity": target_event.get("project_identity"),
                            "target_check_identity": target_event.get("check_identity"),
                            "target_check_key_digest": target_check_key_digest,
                            "action": action,
                            "expected_revision": expected_revision,
                            "revision": expected_revision + 1,
                            "prior_state": current_state,
                            "next_state": next_state,
                            "note": normalized_note,
                            "idempotency_key": idempotency_key,
                            "operation_digest": operation_digest,
                        },
                    }
                )
                recorded = self._prepare_recorded_event(event)
                self._ensure_trailing_newline()
                with self.events_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(recorded, sort_keys=True) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())

        self.evidence.shadow_v1_event(recorded, transport=transport)
        # Canonical shadow (phase 3.4): finding dispositions are not yet
        # represented in the canonical model (the importer skips them the
        # same way), so this records a visible skip — the point is that
        # EVERY v1 write flows through one funnel with one contract.
        self.canonical_live.shadow_v1_event(recorded, transport=transport)
        return recorded

    def replay_finding_disposition(
        self,
        *,
        action: str,
        expected_revision: int,
        note: str | None,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        """Replay an exact committed action without reopening raw target lookup.

        The dashboard uses this before resolving a current canonical episode so
        a lost 303 remains retryable even after later machine evidence closes
        the finding. Only server-trusted disposition rows participate.
        """

        if action not in {"mark_reviewed", "resolve", "reopen"}:
            raise FindingDispositionConflict("unsupported finding disposition action")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise FindingDispositionConflict("finding revision must be a non-negative integer")
        if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 240:
            raise FindingDispositionConflict("finding idempotency key is invalid")
        if any(char in idempotency_key for char in "\r\n\x00"):
            raise FindingDispositionConflict("finding idempotency key is invalid")
        normalized_note = _normalize_finding_disposition_note(note, action=action)

        with self._events_write_lock():
            existing_events, unparseable_lines = self._partition_existing_for_rewrite()
            if unparseable_lines:
                raise FindingDispositionConflict(
                    "finding state is unavailable while the event ledger contains unreadable lines"
                )
            disposition_projection = reduce_finding_dispositions(existing_events)
            for existing in existing_events:
                if not is_trusted_finding_disposition_event(existing):
                    continue
                metadata = existing.get("metadata")
                if not isinstance(metadata, Mapping) or metadata.get("idempotency_key") != idempotency_key:
                    continue
                target_finding_digest = str(metadata.get("target_finding_digest") or "")
                if target_finding_digest in disposition_projection.invalid_targets:
                    raise FindingDispositionConflict(
                        "finding disposition history is conflicting or corrupt"
                    )
                expected_operation = finding_operation_digest(
                    target_event_id=str(metadata.get("target_failure_event_id") or ""),
                    target_event_digest=str(metadata.get("target_event_digest") or ""),
                    target_finding_digest=target_finding_digest,
                    target_check_key_digest=str(metadata.get("target_check_key_digest") or ""),
                    action=action,
                    expected_revision=expected_revision,
                    note=normalized_note,
                )
                recorded_operation = str(metadata.get("operation_digest") or "")
                if hmac.compare_digest(recorded_operation, expected_operation):
                    return existing
                raise FindingDispositionConflict(
                    "finding idempotency key belongs to a different operation"
                )
        return None

    def _partition_existing_for_rewrite(self) -> tuple[list[dict[str, Any]], list[str]]:
        """Return (parsed events, unparseable raw lines) preserving both.

        Unlike ``_read_events_file_order`` (which silently drops undecodable
        lines), a whole-file rewrite must carry corrupt-but-recoverable bytes
        through verbatim — a torn line from a killed appender is forensic
        evidence, and dropping it on a routine import would destroy it with no
        notice. Blank lines are dropped (they carry nothing).
        """

        if not self.events_path.exists():
            return [], []
        parsed: list[dict[str, Any]] = []
        unparseable: list[str] = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                unparseable.append(line)
                continue
            if isinstance(value, dict):
                parsed.append(value)
            else:
                # Valid JSON is not automatically a valid event. Preserve the
                # raw line exactly and withhold complete-snapshot authority;
                # downstream event reducers require objects.
                unparseable.append(line)
        return parsed, unparseable

    def _write_event_partition_unlocked(
        self,
        events: list[dict[str, Any]],
        preserved_unparseable: list[str],
    ) -> None:
        """Atomically replace the event file while its write lock is held."""

        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=self.events_path.name + ".",
            suffix=".tmp",
            dir=str(self.events_path.parent),
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
                for raw_line in preserved_unparseable:
                    handle.write(raw_line + "\n")
                for event in events:
                    handle.write(json.dumps(event, sort_keys=True) + "\n")
            os.replace(tmp_name, self.events_path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def bind_local_usage_source_namespaces(
        self,
        bindings: Mapping[tuple[str, str], str],
        *,
        write: bool = True,
    ) -> dict[str, Any]:
        """TOFU-bind legacy usage rows to their explicitly scanned client home.

        The binding is additive metadata, not a usage refresh: existing
        ``event_id`` and ``created_at`` values are preserved. All matching
        legacy lanes are updated in one locked parse and one atomic rewrite.
        A previously bound or internally inconsistent base fails closed.

        Deliberately NOT shadowed to the canonical store (nor to Evidence
        v2): canonical source identity is immutable, so rewriting historical
        rows' namespaces cannot be expressed there — rows written after the
        binding land under the resolved namespace naturally, and the cutover
        import reads the final bound v1 state.
        """

        normalized_bindings: dict[tuple[str, str], str] = {}
        requested_conflict_bases: set[tuple[str, str]] = set()
        for raw_key, namespace in bindings.items():
            if not isinstance(raw_key, tuple) or len(raw_key) != 2:
                raise ValueError("usage namespace binding key must be (client, session_id)")
            client, session_id = (str(raw_key[0]).strip(), str(raw_key[1]).strip())
            if not client or not session_id:
                raise ValueError("usage namespace binding key cannot be empty")
            if not isinstance(namespace, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", namespace):
                raise ValueError("usage source namespace must be a sha256 fingerprint")
            normalized_key = (
                client,
                normalized_local_usage_session_id(client, session_id),
            )
            previous = normalized_bindings.get(normalized_key)
            if previous is not None and previous != namespace:
                requested_conflict_bases.add(normalized_key)
            else:
                normalized_bindings[normalized_key] = namespace
        if not normalized_bindings:
            return {
                "bound_identities": set(),
                "conflict_bases": set(),
                "bound_rows": 0,
                "bound_rows_by_client": {},
            }

        with self._events_write_lock():
            existing, preserved_unparseable = self._partition_existing_for_rewrite()
            all_rows_by_base: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for event in existing:
                identity = recognized_local_usage_row_identity(event)
                if identity is not None:
                    all_rows_by_base.setdefault(identity[:2], []).append(event)
            # A persisted root/child/sibling lineage is one source-identity
            # unit. Binding only the scanned child would split its Task from
            # an unscoped parent or sibling that happened to fall outside the
            # current limit window. Build undirected components from explicit
            # parent ids, then bind every compatible unscoped row in a touched
            # component in this same locked rewrite (or none of them).
            adjacency: dict[tuple[str, str], set[tuple[str, str]]] = {
                base: set() for base in all_rows_by_base
            }
            for base, rows in all_rows_by_base.items():
                for event in rows:
                    metadata = event.get("metadata")
                    if not isinstance(metadata, dict):
                        continue
                    parent_session_id = metadata.get("parent_client_session_id")
                    if not isinstance(parent_session_id, str) or not parent_session_id:
                        continue
                    parent_base = (
                        base[0],
                        normalized_local_usage_session_id(base[0], parent_session_id),
                    )
                    if parent_base != base:
                        adjacency.setdefault(base, set()).add(parent_base)
                        adjacency.setdefault(parent_base, set()).add(base)
            for base in normalized_bindings:
                adjacency.setdefault(base, set())

            conflict_bases: set[tuple[str, str]] = set()
            effective_bindings: dict[tuple[str, str], str] = {}
            visited: set[tuple[str, str]] = set()
            for seed in normalized_bindings:
                if seed in visited:
                    continue
                component: set[tuple[str, str]] = set()
                pending = [seed]
                while pending:
                    current = pending.pop()
                    if current in component:
                        continue
                    component.add(current)
                    pending.extend(adjacency.get(current, ()))
                visited.update(component)
                requested_targets = {
                    normalized_bindings[base]
                    for base in component
                    if base in normalized_bindings
                }
                component_conflicts = bool(component & requested_conflict_bases)
                if len(requested_targets) != 1:
                    component_conflicts = True
                    target = ""
                else:
                    target = next(iter(requested_targets))
                if not component_conflicts:
                    for base in component:
                        for event in all_rows_by_base.get(base, []):
                            metadata = (
                                event.get("metadata")
                                if isinstance(event.get("metadata"), dict)
                                else {}
                            )
                            for key in (
                                "source_namespace_fingerprint",
                                "parent_source_namespace_fingerprint",
                            ):
                                asserted = metadata.get(key)
                                if asserted is not None and asserted != "" and asserted != target:
                                    component_conflicts = True
                                    break
                            if component_conflicts:
                                break
                        if component_conflicts:
                            break
                if component_conflicts:
                    conflict_bases.update(component)
                    continue
                for base in component:
                    effective_bindings[base] = target

            bound_identities: set[tuple[str, str, str]] = set()
            bound_row_count = 0
            bound_rows_by_client: dict[str, int] = {}
            updated: list[dict[str, Any]] = []
            for event in existing:
                identity = recognized_local_usage_row_identity(event)
                if identity is None or identity[:2] not in effective_bindings:
                    updated.append(event)
                    continue
                target = effective_bindings[identity[:2]]
                metadata_value = event.get("metadata")
                metadata = dict(metadata_value) if isinstance(metadata_value, dict) else {}
                changed = False
                if not metadata.get("source_namespace_fingerprint"):
                    metadata["source_namespace_fingerprint"] = target
                    changed = True
                if metadata.get("parent_client_session_id") and not metadata.get(
                    "parent_source_namespace_fingerprint"
                ):
                    metadata["parent_source_namespace_fingerprint"] = target
                    changed = True
                if changed:
                    metadata["source_namespace_binding"] = "tofu_explicit_scan_v1"
                    bound = dict(event)
                    bound["metadata"] = metadata
                    updated.append(bound)
                    bound_identities.add(identity)
                    bound_row_count += 1
                    bound_rows_by_client[identity[0]] = (
                        bound_rows_by_client.get(identity[0], 0) + 1
                    )
                else:
                    updated.append(event)

            if bound_identities and write:
                self._write_event_partition_unlocked(updated, preserved_unparseable)
        return {
            "bound_identities": bound_identities,
            "conflict_bases": conflict_bases,
            "bound_rows": bound_row_count,
            "bound_rows_by_client": bound_rows_by_client,
        }

    def replace_events(
        self,
        should_replace: Callable[[dict[str, Any]], bool],
        replacement_events: list[dict[str, Any]],
        *,
        trusted_usage_import: bool = False,
        trusted_session_observation_import: bool = False,
        dedup_key: Callable[[dict[str, Any]], Any] | None = None,
        replace_guard: Callable[[list[dict[str, Any]]], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """Atomically replace matching local events with freshly recorded events.

        Cross-process safe (POSIX-only, matching the product): the whole
        read-modify-replace runs under the events write lock so a concurrent
        append can never be silently deleted, and the rewrite goes through a
        per-writer unique temp file (mkstemp in the store dir) followed by an
        atomic os.replace, so two racing rewrites can never interleave bytes.

        Unparseable ledger lines are PRESERVED verbatim (never dropped by the
        rewrite). When ``dedup_key`` is given, a replacement event is SKIPPED if
        its key equals the key of any KEPT event (an existing row not replaced):
        the usage importer passes it so a genuinely-new row is inserted only if
        no equivalent row already survives, making a stale default scan unable to
        duplicate a concurrent import or revert a fresher refresh.

        ``replace_guard`` is evaluated against the complete parsed ledger
        while the same write lock is held, before any row predicate runs. A
        false result aborts the whole replacement. The usage importer uses it
        for atomic multi-row revision preconditions; ordinary callers omit it.
        """

        if trusted_usage_import and trusted_session_observation_import:
            raise SessionObservationConflict(
                "mixed_truth_lanes",
                "replacement rows cannot be both local usage and session observations",
            )

        conflict_error: SessionObservationConflict | None = None
        conflict_recorded: list[dict[str, Any]] = []
        with self._events_write_lock():
            existing, preserved_unparseable = self._partition_existing_for_rewrite()
            if replace_guard is not None and not replace_guard(existing):
                return []
            # Generic/usage replacement paths have no authority to clear one
            # side of a trusted observation conflict quarantine. The trusted
            # observation branch below handles complete identity cohorts and
            # removes only keys it has preflighted atomically.
            kept = [
                event
                for event in existing
                if is_local_session_observation_event(event)
                or not should_replace(event)
            ]
            chosen = replacement_events
            if trusted_session_observation_import:
                candidates = [
                    self._trusted_session_observation_candidate(event)
                    for event in replacement_events
                ]
                candidates_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
                for candidate in candidates:
                    key = local_session_observation_event_key(candidate)
                    assert key is not None
                    candidates_by_key.setdefault(key, []).append(candidate)
                resolutions: dict[
                    tuple[str, str], tuple[dict[str, Any], list[dict[str, Any]]]
                ] = {}
                conflicts: list[
                    tuple[
                        tuple[str, str],
                        str,
                        list[dict[str, Any]],
                        list[dict[str, Any]],
                    ]
                ] = []
                for key, key_candidates in candidates_by_key.items():
                    history = [
                        event
                        for event in existing
                        if is_local_session_observation_event(event)
                        and local_session_observation_event_key(event) == key
                    ]
                    selected, diagnostics = reduce_local_session_observation_events(
                        [*history, *key_candidates]
                    )
                    if not selected:
                        reason = _session_observation_conflict_reason(diagnostics)
                        conflicts.append(
                            (key, reason, history, key_candidates)
                        )
                        continue
                    resolutions[key] = (selected[0], key_candidates)
                if conflicts:
                    # Preflight every key before writing. Persist every unseen
                    # conflicting revision in one transaction; abort ordinary
                    # replacements so a multi-key batch cannot quarantine only
                    # the first conflict and leave later first-writer rows live.
                    conflict_rows: list[dict[str, Any]] = []
                    for _key, _reason, history, key_candidates in conflicts:
                        seen_revisions = {
                            local_session_observation_revision(row)
                            for row in history
                        }
                        for candidate in key_candidates:
                            revision = local_session_observation_revision(candidate)
                            if revision in seen_revisions:
                                continue
                            seen_revisions.add(revision)
                            conflict_rows.append(
                                self._prepare_recorded_event(candidate)
                            )
                    if conflict_rows:
                        self._write_event_partition_unlocked(
                            [*existing, *conflict_rows],
                            preserved_unparseable,
                        )
                    conflict_key, conflict_reason, _history, _candidates = (
                        conflicts[0]
                    )
                    conflict_error = SessionObservationConflict(
                        conflict_reason,
                        f"session observation {conflict_key[0]}::{conflict_key[1]} is ambiguous: {conflict_reason}",
                    )
                    conflict_recorded = conflict_rows
                    chosen = []
                else:
                    chosen = []
                    retained_authorities: list[dict[str, Any]] = []
                    for key, (authority, key_candidates) in resolutions.items():
                        if any(authority is candidate for candidate in key_candidates):
                            chosen.append(authority)
                        else:
                            retained_authorities.append(authority)
                    touched_keys = set(candidates_by_key)
                    kept = [
                        event
                        for event in kept
                        if not (
                            is_local_session_observation_event(event)
                            and local_session_observation_event_key(event) in touched_keys
                        )
                    ]
                    kept.extend(retained_authorities)
            elif dedup_key is not None:
                kept_keys = {key for event in kept if (key := dedup_key(event)) is not None}
                chosen = [event for event in replacement_events if (key := dedup_key(event)) is None or key not in kept_keys]
            if conflict_error is None:
                # No replace_events caller writes instrumentation markers, so
                # marker provenance is unconditionally stripped here too.
                prepared_events = [
                    strip_finding_disposition_provenance(
                        strip_blocker_resolution_provenance(
                            strip_client_context_provenance(
                                strip_untrusted_instrumentation_marker_metadata(
                                    mark_trusted_local_session_observation_event(event)
                                    if trusted_session_observation_import
                                    else strip_untrusted_session_observation_metadata(
                                        mark_trusted_local_usage_import_event(event)
                                    )
                                    if trusted_usage_import
                                    else strip_untrusted_session_observation_metadata(
                                        strip_untrusted_usage_truth_metadata(event)
                                    )
                                )
                            )
                        )
                    )
                    for event in chosen
                ]
                recorded = [
                    self._prepare_recorded_event(event)
                    for event in prepared_events
                ]
                self._write_event_partition_unlocked(
                    [*kept, *recorded], preserved_unparseable
                )
            else:
                recorded = conflict_recorded
        self.evidence.shadow_v1_events(recorded, transport="internal")
        # Canonical usage lane (phase 3.3): every freshly written usage row
        # shadows through the same fail-open contract; the repository's
        # content hash keeps unchanged re-observations physical no-ops.
        for replacement in recorded:
            self.canonical_live.shadow_v1_event(replacement, transport="internal")
        if conflict_error is not None:
            raise conflict_error
        return recorded

    def existing_event_ids(self) -> set[str]:
        """Every event_id currently in the store (for dedup-safe merges)."""
        ids: set[str] = set()
        for event in self._read_events_file_order():
            event_id = event.get("event_id")
            if isinstance(event_id, str) and event_id:
                ids.add(event_id)
        return ids

    def _ensure_trailing_newline(self) -> None:
        """If the events file is non-empty and its last byte is not '\\n', append
        one. Guards the append path against a target whose last line lacks a
        trailing newline: without this, the first appended event would fuse onto
        that line (``}{``) and BOTH the pre-existing last row and the new row
        would fail to parse on read (silent data loss). Must be called while
        holding the events write lock.
        """

        if not self.events_path.exists():
            return
        try:
            size = self.events_path.stat().st_size
        except OSError:
            return
        if size == 0:
            return
        with self.events_path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            last_byte = handle.read(1)
        if last_byte != b"\n":
            with self.events_path.open("ab") as handle:
                handle.write(b"\n")

    def merge_events_preserving_identity(
        self,
        source_events: list[dict[str, Any]],
        *,
        kind: str = "all",
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Atomically plan and append a cross-store merge.

        Logical usage/session-observation dedup must be checked against the
        target under the same lock as the append. Event-id-only rechecks are
        insufficient because concurrent importers mint different ids for the
        same client/session/lane.
        """

        from . import store_merge

        appended: list[dict[str, Any]] = []
        with self._events_write_lock():
            existing = self._read_events_file_order()
            target_ids = {
                event_id
                for event in existing
                if isinstance((event_id := event.get("event_id")), str)
                and event_id
            }
            target_identities = store_merge.usage_row_identities(existing)
            plan = store_merge.plan_store_merge(
                source_events,
                target_ids,
                kind=kind,
                target_usage_identities=target_identities,
            )
            plan["target_events_before"] = len(target_ids)
            self._ensure_trailing_newline()
            with self.events_path.open("a", encoding="utf-8") as handle:
                for raw_event in plan["events_to_add"]:
                    if not isinstance(raw_event, dict):
                        continue
                    event = strip_finding_disposition_provenance(raw_event)
                    event_id = event.get("event_id")
                    if not isinstance(event_id, str) or not event_id:
                        continue
                    handle.write(json.dumps(event, sort_keys=True) + "\n")
                    appended.append(event)
        self.evidence.shadow_v1_events(appended, transport="internal")
        # Canonical shadow (phase 3.3): merged rows keep their foreign
        # event_ids, which the canonical lanes key naturally (facts by
        # source_event_id, usage by session/lane/representation).
        for event in appended:
            self.canonical_live.shadow_v1_event(event, transport="internal")
        return plan, appended

    def append_events_preserving_identity(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Append events VERBATIM — preserving each event's own event_id,
        created_at, and provenance/trust markers — deduplicating against event
        ids already in the target store.

        Unlike ``record_event``/``replace_events`` (which mint a fresh event_id
        and created_at, and strip/re-stamp usage-truth and client-context
        provenance), this path is for cross-store MERGES: a merged trusted usage
        row must stay trusted and a merged section must keep its authored
        markers and its ORIGINAL event_id, because the Phase 2.7 client-log
        evidence join pairs sections to sessions by that id. It is additive
        only — existing rows are never read-modified, only appended to — and
        holds the events write lock so a concurrent writer cannot interleave.

        Events without a well-formed ``evt_`` id, or whose id already exists in
        the target, are skipped. Returns the events actually appended.
        """
        appended: list[dict[str, Any]] = []
        with self._events_write_lock():
            present = self.existing_event_ids()
            seen_in_batch: set[str] = set()
            self._ensure_trailing_newline()
            with self.events_path.open("a", encoding="utf-8") as handle:
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    # A dashboard user's attention decision is local-store
                    # authority. Cross-store event merges preserve the audit
                    # row but strip that authority unless a future explicit
                    # migration contract is introduced.
                    event = strip_finding_disposition_provenance(event)
                    event_id = event.get("event_id")
                    if not isinstance(event_id, str) or not event_id:
                        continue
                    if event_id in present or event_id in seen_in_batch:
                        continue
                    seen_in_batch.add(event_id)
                    handle.write(json.dumps(event, sort_keys=True) + "\n")
                    appended.append(event)
        self.evidence.shadow_v1_events(appended, transport="internal")
        # Canonical shadow (phase 3.3): same contract as the merge lane.
        for event in appended:
            self.canonical_live.shadow_v1_event(event, transport="internal")
        return appended

    def list_events(self, *, limit: int = 50, run_id: str | None = None) -> list[dict[str, Any]]:
        events = self._read_events_file_order(run_id=run_id)
        events.sort(key=lambda item: _sortable_created_at(item), reverse=True)
        return events[:limit]

    def list_all_events(self, *, run_id: str | None = None) -> list[dict[str, Any]]:
        return self._read_events_file_order(run_id=run_id)

    def reconcile_evidence_refreshable_usage_snapshot(
        self,
        *,
        complete: bool = True,
        transport: str | None = "internal",
    ) -> dict[str, Any]:
        """Reconcile Evidence current usage from one lock-consistent v1 view.

        A complete snapshot may infer deletion, so it is taken while holding
        the v1 writer lock through the Evidence reconcile. This exceptional
        maintenance path is intentionally stronger than ordinary fail-open
        per-event shadowing: a concurrent v1 writer cannot be omitted and then
        incorrectly tombstoned. Unparseable ledger lines automatically demote
        the snapshot to partial and make that loss of authority explicit.
        """

        if not isinstance(complete, bool):
            raise TypeError("complete must be a bool")
        with self._events_write_lock():
            events, unparseable = self._partition_existing_for_rewrite()
            parse_complete = not unparseable
            result = self.evidence.reconcile_refreshable_usage_snapshot(
                events,
                complete=complete and parse_complete,
                transport=transport,
            ).to_dict()
        result["ledger_snapshot_complete_requested"] = complete
        result["ledger_snapshot_parse_complete"] = parse_complete
        result["ledger_unparseable_lines"] = len(unparseable)
        if complete and not parse_complete:
            errors = result.get("errors")
            rendered_errors = list(errors) if isinstance(errors, list) else []
            rendered_errors.append(
                "events.jsonl contains unparseable lines; deletion authority withheld"
            )
            result["errors"] = rendered_errors
        return result

    def summarize_events(self, *, limit: int = 200, run_id: str | None = None) -> dict[str, Any]:
        all_events = self.list_all_events(run_id=run_id)
        all_events.sort(key=lambda item: _sortable_created_at(item), reverse=True)
        return summarize_events(all_events[:limit], limit=limit, canonical_events=all_events)

    def list_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        runs_root = self.store.runs_root
        if not runs_root.is_dir():
            # Read-only consumers (create=False) may look at a store that has
            # never recorded a run; an empty list is the honest answer.
            return []
        run_dirs = [path for path in runs_root.iterdir() if path.is_dir()]
        run_dirs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        runs: list[dict[str, Any]] = []
        for path in run_dirs[:limit]:
            try:
                metadata = self.store.read_metadata(path.name)
            except FileNotFoundError:
                continue
            runs.append(
                {
                    "run_id": path.name,
                    "status": metadata.get("status"),
                    "command": metadata.get("command"),
                    "started_at": metadata.get("started_at"),
                    "ended_at": metadata.get("ended_at"),
                    "duration_seconds": metadata.get("duration_seconds"),
                }
            )
        return runs

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self.store.read_metadata(run_id)

    def get_report(self, run_id: str) -> dict[str, Any]:
        if run_id == "latest":
            run_id = self.store.latest_run_id()
        return build_run_report_payload(self.store, run_id)

    def record_machine_check(
        self,
        run_id: str,
        *,
        name: str,
        before_exit_code: int | None = None,
        after_exit_code: int | None = None,
        before_summary: str | None = None,
        after_summary: str | None = None,
    ) -> dict[str, Any]:
        if run_id == "latest":
            run_id = self.store.latest_run_id()
        report_payload = build_run_report_payload(self.store, run_id)
        existing = read_outcome(self.store, run_id) or report_payload["outcome"]
        outcome = build_machine_check_outcome(
            existing=existing,
            name=name,
            before_exit_code=before_exit_code,
            after_exit_code=after_exit_code,
            before_summary=before_summary,
            after_summary=after_summary,
        )
        return write_outcome(self.store, run_id, outcome)

    def prepare_judge(self, run_id: str, *, task_goal: str, rubric: str, write_package: bool = True) -> dict[str, Any]:
        if run_id == "latest":
            run_id = self.store.latest_run_id()
        report_payload = build_run_report_payload(self.store, run_id)
        package = build_judge_package(report=report_payload, task_goal=task_goal, rubric=rubric)
        if write_package:
            path = self.store.run_dir(run_id) / "judge_package.json"
            path.write_text(json.dumps(package, indent=2, sort_keys=True), encoding="utf-8")
        return package

    def compute_value(self, run_id: str, *, budget_usd: float | None = None) -> dict[str, Any]:
        if run_id == "latest":
            run_id = self.store.latest_run_id()
        report_payload = build_run_report_payload(self.store, run_id)
        value = compute_advisory_value_score(report_payload, budget_usd=budget_usd)
        existing = read_outcome(self.store, run_id) or report_payload["outcome"]
        existing["value"] = value
        write_outcome(self.store, run_id, existing)
        return value


def _sortable_created_at(event: dict[str, Any]) -> float:
    try:
        return float(event.get("created_at") or 0.0)
    except (TypeError, ValueError):
        return 0.0
