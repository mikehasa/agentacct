from __future__ import annotations

import json
import math
import re
from collections import Counter
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from .confidence import normalize_cost_confidence, normalize_usage_confidence
from .join_rules import (
    annotate_usage_source_namespace_ambiguity,
    decide_attribution,
    namespace_join_compatible,
    pair_confidence,
    pair_match,
    work_key,
)
from .log_evidence import (
    apply_log_evidence_to_snapshots,
    build_log_evidence_index,
    build_log_evidence_session_blocks,
    summarize_log_evidence_donor_rows,
)
from .store_resolution import claude_worktree_owner_path_text
from .usage_cube import usage_bucket_date
from .usage_truth import (
    INSTRUMENTATION_MARKER_EVENT_TYPE,
    is_instrumentation_marker_event,
    is_local_usage_import_event,
    local_session_observation_event_key,
    local_session_observation_source_watermark,
    local_usage_event_additivity,
    local_usage_row_lane,
    local_usage_source_namespace_ambiguous_identities,
    normalized_local_usage_session_id,
    reduce_local_session_observation_events,
    split_diagnostic_events,
    split_shadowed_legacy_usage_events,
)


WORK_STATUSES = {"started", "checkpoint", "blocked", "completed"}
EVIDENCE_TYPES = {"test", "build", "lint", "typecheck", "smoke", "benchmark", "browser", "security", "artifact", "other"}
EVIDENCE_RESULTS = {"passed", "failed", "skipped", "error", "unknown"}
_BLOCKER_RESOLUTION_CONTRACT = "server_validated_v1"

_JOIN_RANK = {"unjoined": 0, "low": 1, "medium": 2, "high": 3, "exact": 4}

# Explicit reconciliation display rank: attributed rows always come first.
# Unknown states rank after every known one (never hidden, just last).
RECONCILIATION_STATE_RANK = {
    "attributed": 0,
    "ambiguous": 1,
    "context_matched_unallocated": 2,
    "usage_without_mcp_context": 3,
}

# frozen: historical stores/logs/files carry this schema string forever.
SESSION_ROLLUP_SCHEMA_VERSION = "agent-sentinel.session-rollup.v1"


def capped_rows(rows: list[Any], limit: int) -> dict[str, Any]:
    """Shared cap metadata for every truncated table: {rows, total, shown}.

    Renderers must print "Showing {shown} of {total}" whenever shown < total —
    a silent cap is how the one attributed row disappeared from view.
    """

    limit = max(0, int(limit))
    return {"rows": rows[:limit], "total": len(rows), "shown": min(limit, len(rows))}


def build_work_ledger(
    events: list[dict[str, Any]],
    *,
    run_reports: list[dict[str, Any]] | None = None,
    cost_events: list[dict[str, Any]] | None = None,
    session_observations: list[dict[str, Any]] | None = None,
    session_observation_diagnostics: dict[str, int] | None = None,
    store_project_label: str | None = None,
    store_scope: str | None = None,
) -> dict[str, Any]:
    """Build the derived local work ledger from imported facts and MCP meaning.

    Usage events are the usage/cost source of truth. MCP section and evidence
    events only add meaning and evidence. Attribution is computed here with an
    explicit confidence label.

    ``store_project_label`` (optional) is the serving store's OWN project
    label; ``store_scope`` (optional) is EXPLICIT: "project" only when the
    serving store uses the conventional ``<project>/.agent-sentinel/state``
    layout, anything else is "custom". Rollup join blocks gain
    ``context_scope`` so displays can honestly distinguish "no MCP context in
    this store" from "session ran in another project whose context lives in
    its own store" — the latter claim is only ever made for
    ``store_scope == "project"`` (a custom/global store receives every
    project's context, so the per-project-stores explanation would be false
    there by construction).
    """

    # agentacct's own diagnostic tool events (doctor probe / workflow smoke)
    # never reach user-facing builders; they stay visible as a labeled count
    # in insights and in the raw event log.
    events, diagnostic_tool_events = split_diagnostic_events(events)
    # Client-log evidence index: built ONCE per ledger build from trusted
    # usage-import rows (log_evidence.py), passed to every snapshot builder.
    # The markers are derived here on every read and never trusted from
    # stored event metadata.
    log_evidence_index = build_log_evidence_index(events)
    local_session_observations, local_observation_diagnostics = (
        build_local_session_observations(events)
    )
    all_usage_events = build_usage_events(events, include_non_additive=True)
    usage_events = [event for event in all_usage_events if event.get("usage_additive") is not False]
    excluded_usage_events = [event for event in all_usage_events if event.get("usage_additive") is False]
    proxy_usage_events = build_proxy_usage_events(cost_events or [])
    diagnostic_usage_events = build_diagnostic_usage_events(events)
    work_evidence_counters: dict[str, int] = {}
    evidence_evidence_counters: dict[str, int] = {}
    debug_evidence_counters: dict[str, int] = {}
    work_events = build_work_events(
        events, log_evidence_index=log_evidence_index, log_evidence_counters=work_evidence_counters
    )
    evidence_events = build_evidence_events(
        events,
        run_reports=run_reports,
        log_evidence_index=log_evidence_index,
        log_evidence_counters=evidence_evidence_counters,
    )
    usage_debug_events = build_usage_debug_events(
        events, log_evidence_index=log_evidence_index, log_evidence_counters=debug_evidence_counters
    )
    allow_legacy_unscoped_namespace = store_scope == "project"
    projectable_work_events, work_namespace_quarantine = _projectable_work_event_cohorts(
        work_events,
        allow_legacy_unscoped_namespace=allow_legacy_unscoped_namespace,
    )
    attributions = build_attributions(
        usage_events,
        projectable_work_events,
        allow_legacy_unscoped_namespace=allow_legacy_unscoped_namespace,
    )
    work_items = build_work_items(
        projectable_work_events,
        evidence_events,
        attributions,
        allow_legacy_unscoped_namespace=allow_legacy_unscoped_namespace,
    )
    blocker_resolution_diagnostics = apply_blocker_resolutions(
        work_items,
        # Use every raw snapshot so a duplicate target id or quarantined
        # namespace cohort rejects instead of disappearing before validation.
        work_events=work_events,
        evidence_events=evidence_events,
    )
    join_inspector = build_join_inspector(
        usage_events,
        work_items,
        attributions,
        allow_legacy_unscoped_namespace=allow_legacy_unscoped_namespace,
    )
    _attach_join_explanations(work_items, join_inspector["work_item_join_explanations"])
    usage_reconciliation = build_usage_reconciliation(usage_events, work_items, attributions)
    attention_items = build_attention_items(work_items, usage_reconciliation)
    # Session-level evidence blocks (counts every evidenced event present in
    # this store, incl. plain events that never become work items) and the
    # residual-chip label set (projects with context still session-less after
    # enrichment) feed the rollup's honest display states.
    log_evidence_by_key = build_log_evidence_session_blocks(events, log_evidence_index)
    unlinked_context_project_labels = _unlinked_client_context_project_labels(events, log_evidence_index)
    for item in work_items:
        if _optional_str(item.get("client_session_id")) or _optional_str(item.get("client_transcript_id")):
            continue
        item_label = _optional_str(item.get("project_dir"))
        if item_label and item_label != "~":
            unlinked_context_project_labels.add(item_label.casefold())
    # Instrumentation markers (CLI-authored provenance only): pre/post
    # classification and the post-install context-rate KPI live on the rollup.
    instrumentation_markers = build_instrumentation_markers(events)
    session_rollup = build_session_rollup(
        usage_events=all_usage_events,
        work_items=work_items,
        attributions=attributions,
        store_project_label=store_project_label,
        store_scope=store_scope,
        log_evidence_by_key=log_evidence_by_key,
        unlinked_context_project_labels=unlinked_context_project_labels,
        instrumentation_markers=instrumentation_markers,
        session_observations=session_observations,
        local_session_observations=local_session_observations,
    )
    rollup_summary = session_rollup.get("summary")
    if isinstance(rollup_summary, dict):
        rollup_summary["mechanical_projection"] = dict(session_observation_diagnostics or {})
    # Attention example refs reuse the rollup's collision-aware short labels —
    # the ONE session-label assigner (locked redaction decision).
    session_labels = {
        (str(entry.get("client") or ""), str(entry.get("client_session_id") or "")): str(
            entry.get("client_session_id_short") or ""
        )
        for entry in session_rollup.get("sessions", [])
    }
    attention_groups = build_attention_groups(attention_items, session_labels=session_labels)
    overview = build_overview(
        usage_events=usage_events,
        work_items=work_items,
        evidence_events=evidence_events,
        attributions=attributions,
        work_events=work_events,
        usage_debug_events=usage_debug_events,
        proxy_usage_events=proxy_usage_events,
        join_inspector=join_inspector,
        attention_items=attention_items,
        attention_groups=attention_groups,
    )
    insights = build_ledger_insights(
        usage_events=usage_events,
        work_items=work_items,
        evidence_events=evidence_events,
        usage_reconciliation=usage_reconciliation,
        attention_items=attention_items,
        overview=overview,
    )
    # Excluded stale legacy base rows are visible, never silently vanished:
    # the counter explains why an un-migrated store's totals differ from a
    # naive row sum (see usage_truth.is_shadowed_legacy_usage_import_event).
    _kept, shadowed_legacy_rows = split_shadowed_legacy_usage_events(events)
    insights["legacy_shadowed_rows"] = len(shadowed_legacy_rows)
    insights["legacy_shadowed_row_event_ids"] = [str(event.get("event_id") or "") for event in shadowed_legacy_rows]
    insights["usage_additivity_quarantine"] = {
        "excluded_rows": len(excluded_usage_events),
        "by_state": dict(
            Counter(str(event.get("usage_normalization_state") or "unknown") for event in excluded_usage_events)
        ),
        "raw_evidence_preserved": True,
    }
    insights["blocker_resolution"] = blocker_resolution_diagnostics
    insights["mechanical_session_observations"] = {
        "sessions": len(session_observations or []),
        "observations": sum(int(row.get("observation_count") or 0) for row in (session_observations or [])),
        "diagnostics": dict(session_observation_diagnostics or {}),
    }
    insights["local_session_observations"] = {
        "sessions": len(local_session_observations),
        "measurement_basis": "local_client_log_observed",
        "diagnostics": local_observation_diagnostics,
    }
    # Counts only: ambiguous raw snapshots remain available in work_events,
    # while product projections exclude the whole cohort. Never echo work ids,
    # namespaces, titles, or files through this diagnostic.
    insights["work_event_namespace_quarantine"] = work_namespace_quarantine
    # Excluded diagnostic tool events are visible, never silently vanished.
    insights["diagnostic_tool_events"] = {
        "count": len(diagnostic_tool_events),
        "by_event_type": dict(Counter(str(event.get("event_type") or "unknown") for event in diagnostic_tool_events)),
        "by_source": dict(Counter(str(event.get("source") or "unknown") for event in diagnostic_tool_events)),
    }
    # Client-log evidence honesty counters: refusals/conflicts/skips are
    # counted, never silent — a codex wire-format drift shows up here as
    # rising skip counts (or sudden zero totals), not as silent absence.
    donor_summary = summarize_log_evidence_donor_rows(events)
    applier_counters = Counter()
    for counters in (work_evidence_counters, evidence_evidence_counters, debug_evidence_counters):
        applier_counters.update(counters)
    insights["client_log_evidence"] = {
        "donor_usage_rows": donor_summary["donor_usage_rows"],
        "donor_observation_rows": donor_summary["donor_observation_rows"],
        "evidenced_event_ids_total": donor_summary["evidenced_event_ids_total"],
        "observation_evidenced_event_ids_total": donor_summary[
            "observation_evidenced_event_ids_total"
        ],
        "evidenced_events_in_store": sum(
            1 for event in events if str(event.get("event_id") or "") in log_evidence_index
        ),
        "implied_session_keys": int(applier_counters.get("implied_session_keys", 0)),
        "corroborated": int(applier_counters.get("corroborated", 0)),
        # Store-wide claimed-vs-evidenced conflicts (from the session blocks,
        # so plain record_event/task events without a snapshot surface count
        # too).
        "conflicts": sum(int(block.get("conflicts") or 0) for block in log_evidence_by_key.values()),
        "ambiguous_multi_session": int(applier_counters.get("ambiguous_multi_session", 0)),
        "item_conflicts": int(applier_counters.get("item_conflicts", 0)),
        "outputs_skipped": donor_summary["outputs_skipped"],
        "observation_outputs_skipped": donor_summary["observation_outputs_skipped"],
        "replay_rejections": dict(getattr(log_evidence_index, "diagnostics", {})),
    }
    timeline = build_timeline(
        usage_events=usage_events,
        work_events=work_events,
        evidence_events=evidence_events,
        attributions=attributions,
        usage_debug_events=usage_debug_events,
        proxy_usage_events=proxy_usage_events,
        diagnostic_usage_events=diagnostic_usage_events,
    )
    return {
        # v2: session_rollup + attention_groups keys, cache-aware token
        # triples on every total, attributed-first reconciliation ordering,
        # diagnostic-tool-event filtering. (Phase 1's work_id namespacing was
        # already a breaking change; the bump is honest, not cosmetic.)
        # frozen: consumers pin this exact schema string (api.py); pre-rename.
        "schema_version": "agent-sentinel.work-ledger.v2",
        "principles": [
            "Facts are imported.",
            "Meaning is reported.",
            "Evidence is attached.",
            "Attribution is inferred with confidence.",
            "Value is judged separately.",
        ],
        "usage_events": usage_events,
        "excluded_usage_events": excluded_usage_events,
        "proxy_usage_events": proxy_usage_events,
        "diagnostic_usage_events": diagnostic_usage_events,
        "work_events": work_events,
        "evidence_events": evidence_events,
        "usage_debug_events": usage_debug_events,
        "attributions": attributions,
        "join_inspector": join_inspector,
        "usage_reconciliation": usage_reconciliation,
        "attention_items": attention_items,
        "attention_groups": attention_groups,
        "session_observations": list(session_observations or []),
        "local_session_observations": local_session_observations,
        "session_rollup": session_rollup,
        "work_items": work_items,
        "overview": overview,
        "insights": insights,
        "timeline": timeline,
    }


def build_instrumentation_markers(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-client instrumentation-install markers — CLI-authored provenance only.

    Only events passing ``usage_truth.is_instrumentation_marker_event`` count:
    the provenance key is stamped exclusively by the CLI marker writers via the
    service trust gate and stripped from every other recording path, AND the
    predicate re-checks the writer's plausibility invariants at read time
    (sane client, real positive installed_at never after the event's own
    recording), so forged/merge-imported events never classify sessions.
    Every marker-TYPED event that fails classifies nothing but is COUNTED in
    ``invalid_marker_count`` (never silent; it stays visible in the raw event
    log). ``earliest_by_client`` keeps the EARLIEST installed_at per client —
    duplicate markers can only move the boundary toward "more sessions are
    post-install", the non-flattering direction for the context-rate KPI.
    """

    markers: list[dict[str, Any]] = []
    earliest_by_client: dict[str, float] = {}
    invalid_marker_count = 0
    for event in events:
        if not is_instrumentation_marker_event(event):
            if str(event.get("event_type") or "") == INSTRUMENTATION_MARKER_EVENT_TYPE:
                invalid_marker_count += 1
            continue
        metadata = _metadata(event)
        client = _optional_str(metadata.get("client"))
        installed_at = _safe_optional_float(metadata.get("installed_at"))
        if client is None or installed_at is None:
            # A marker without a usable client/time classifies nothing
            # (missing beats wrong); counted, visible in the raw event log.
            invalid_marker_count += 1
            continue
        markers.append(
            {
                "event_id": _event_id(event),
                "client": client,
                "installed_at": installed_at,
                "installed_at_source": _optional_str(metadata.get("installed_at_source")),
                "surface": _optional_str(metadata.get("surface")),
                "recorded_at": _safe_float(event.get("created_at")),
            }
        )
        current = earliest_by_client.get(client)
        if current is None or installed_at < current:
            earliest_by_client[client] = installed_at
    return {
        "earliest_by_client": earliest_by_client,
        "markers": markers,
        "invalid_marker_count": invalid_marker_count,
    }


def build_usage_events(
    events: list[dict[str, Any]],
    *,
    cost_events: list[dict[str, Any]] | None = None,
    include_non_additive: bool = False,
) -> list[dict[str, Any]]:
    # Shared legacy-store rule (usage_truth): a stale claude-code base row
    # shadowed by ':model:'-suffixed sibling rows is excluded from usage truth,
    # otherwise read-time base normalization would double-count AND
    # double-attribute the pre-migration duplicate at high confidence.
    kept_events, _shadowed = split_shadowed_legacy_usage_events(events)
    ambiguous_source_identities = (
        local_usage_source_namespace_ambiguous_identities(kept_events)
    )
    usage_events = [_usage_event(event) for event in kept_events if _is_usage_event(event)]
    usage_events = [event for event in usage_events if event is not None]
    if ambiguous_source_identities:
        quarantined: list[dict[str, Any]] = []
        for event in usage_events:
            identity = (
                str(event.get("client") or ""),
                str(event.get("client_session_id") or ""),
                str(event.get("usage_row_lane") or ""),
            )
            if identity not in ambiguous_source_identities:
                quarantined.append(event)
                continue
            row = dict(event)
            row["raw_cumulative_input_tokens"] = int(
                row.get("input_tokens") or 0
            )
            row["raw_cumulative_output_tokens"] = int(
                row.get("output_tokens") or 0
            )
            row["raw_cumulative_cached_input_tokens"] = int(
                row.get("cached_input_tokens") or 0
            )
            for key in (
                "input_tokens",
                "output_tokens",
                "cached_input_tokens",
                "fresh_tokens",
                "cache_creation_tokens",
                "cache_read_tokens",
                "total_tokens",
            ):
                row[key] = 0
            row["estimated_cost_usd"] = None
            row["cost_confidence"] = "unknown"
            row["usage_additive"] = False
            row["usage_normalization_state"] = (
                "source_namespace_missing_vs_explicit"
            )
            quarantined.append(row)
        usage_events = quarantined
    if not include_non_additive:
        usage_events = [event for event in usage_events if event.get("usage_additive") is not False]
    return annotate_usage_source_namespace_ambiguity(
        sorted(
            usage_events,
            key=lambda event: float(event.get("created_at") or 0.0),
            reverse=True,
        )
    )


def build_local_session_observations(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Project trusted local session rows without manufacturing usage.

    The shared reducer selects one source-watermark revision per raw identity
    and quarantines namespace/watermark conflicts.  Returned rows intentionally
    match only the non-usage parts of the session-rollup observation contract.
    """

    selected, diagnostics = reduce_local_session_observation_events(events)
    observations: list[dict[str, Any]] = []
    for event in selected:
        metadata = _metadata(event)
        key = local_session_observation_event_key(event)
        if key is None:
            continue
        project_value = metadata.get("project_dir") or metadata.get("cwd")
        project, project_source = _project_label_info(project_value)
        started_at = _safe_positive_float(metadata.get("started_at"))
        # The source watermark may be a nanosecond file revision. It orders
        # replacements but is not a Unix-seconds activity timestamp. Keep the
        # client activity clock separate so UI dates and durations remain
        # human-correct.
        updated_at = _safe_positive_float(metadata.get("updated_at"))
        source_revision_at = local_session_observation_source_watermark(event)
        first_activity_at = started_at if started_at is not None else updated_at
        observations.append(
            {
                "observation_channel": "local_client_log",
                "measurement_basis": "local_client_log_observed",
                "client": key[0],
                "client_session_id": key[1],
                "client_transcript_id": _optional_str(metadata.get("client_transcript_id")),
                "parent_client_session_id": _optional_str(metadata.get("parent_client_session_id")),
                "parent_state": "present" if metadata.get("parent_client_session_id") else "absent",
                "session_kind": _optional_str(metadata.get("client_session_kind")) or "root",
                "client_session_title": _trusted_client_session_title(metadata),
                "project": project,
                "project_source": project_source,
                "project_identity": _project_identity(project_value),
                "identity_scope_state": (
                    "explicit" if metadata.get("source_namespace_fingerprint") else "unscoped"
                ),
                "source_namespace_fingerprint": _optional_str(
                    metadata.get("source_namespace_fingerprint")
                ),
                "parent_source_namespace_fingerprint": _optional_str(
                    metadata.get("parent_source_namespace_fingerprint")
                ),
                "activity_time_basis": _optional_str(metadata.get("activity_time_basis"))
                or "client_metadata",
                "first_activity_at": first_activity_at,
                "last_activity_at": updated_at,
                "observed_models": _ordered_unique_text(
                    str(value)
                    for value in (
                        metadata.get("observed_models")
                        if isinstance(metadata.get("observed_models"), list)
                        else []
                    )
                    if isinstance(value, str) and value
                ),
                "source_event_id": _event_id(event),
                "source_updated_at": updated_at,
                "source_revision_at": source_revision_at,
                "observation_count": 1,
            }
        )
    return observations, diagnostics


def build_proxy_usage_events(cost_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted([_proxy_usage_event(event) for event in cost_events], key=lambda event: float(event.get("created_at") or 0.0), reverse=True)


def build_diagnostic_usage_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    diagnostic_events = [_diagnostic_usage_event(event) for event in events if _is_diagnostic_usage_event(event)]
    return sorted([event for event in diagnostic_events if event is not None], key=lambda event: float(event.get("created_at") or 0.0), reverse=True)


def build_work_events(
    events: list[dict[str, Any]],
    *,
    log_evidence_index: dict[str, list[dict[str, Any]]] | None = None,
    log_evidence_counters: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Section snapshots, ALWAYS enriched with client-log evidence.

    The enrichment happens here (not only in build_work_ledger) so every
    caller — canonical attribution, the context bridge's
    ``build_attributions(build_usage_events(events), build_work_events(events))``
    path, tests — sees the identical enriched candidates. ``log_evidence_index``
    avoids a rebuild when the caller already has one;
    ``log_evidence_counters`` (optional dict) receives the applier's honesty
    counters. The group guard is keyed by work_id: a merged item whose
    snapshots are evidenced by more than one session gets NO implied keys.
    """

    work_events = [_work_event(event) for event in events if _is_work_event(event)]
    work_events = sorted([event for event in work_events if event is not None], key=lambda event: float(event.get("created_at") or 0.0))
    index = log_evidence_index if log_evidence_index is not None else build_log_evidence_index(events)
    counters = apply_log_evidence_to_snapshots(work_events, index, group_key=lambda snapshot: str(snapshot.get("work_id") or ""))
    _propagate_log_evidenced_source_constraints(work_events)
    if log_evidence_counters is not None:
        log_evidence_counters.update(counters)
    return work_events


def _propagate_log_evidenced_source_constraints(
    work_events: list[dict[str, Any]],
) -> None:
    """Keep a client-home constraint sticky across one section's revisions.

    Log evidence may attach to the started snapshot while a later completed
    snapshot repeats the raw session id explicitly.  Allocation examines each
    snapshot, so the later one must inherit the donor home's constraint or it
    could join usage from a different home under the same raw id.
    """

    # Downstream aggregation coalesces equal work_id values, while an earlier
    # id-less snapshot can acquire that identity through the traditional
    # (source, run, section) revision cohort. Build the union of BOTH
    # equivalence relations. Otherwise a completed snapshot with the same
    # work_id but a different run_id can shed an evidenced source-home
    # constraint and consume usage from a colliding client home.
    parents = list(range(len(work_events)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    by_work_id: dict[str, int] = {}
    by_section_revision: dict[tuple[str, str, str], int] = {}
    for index, event in enumerate(work_events):
        work_id = _optional_str(event.get("work_id"))
        if work_id is not None:
            prior = by_work_id.setdefault(work_id, index)
            union(index, prior)
        section_id = _optional_str(event.get("section_id"))
        if section_id is not None:
            revision_key = (
                _optional_str(event.get("source")) or "",
                _optional_str(event.get("run_id")) or "",
                section_id,
            )
            prior = by_section_revision.setdefault(revision_key, index)
            union(index, prior)

    cohorts: dict[int, list[dict[str, Any]]] = {}
    for index, event in enumerate(work_events):
        cohorts.setdefault(find(index), []).append(event)

    for cohort in cohorts.values():
        sources = {
            source
            for event in cohort
            if (
                source := _optional_str(
                    event.get("log_evidenced_source_namespace_fingerprint")
                )
            )
        }
        if len(sources) == 1:
            source = next(iter(sources))
            for event in cohort:
                event["log_evidenced_source_namespace_fingerprint"] = source
        elif len(sources) > 1:
            for event in cohort:
                event["log_evidenced_source_namespace_conflict"] = True


def _projectable_work_event_cohorts(
    work_events: list[dict[str, Any]],
    *,
    allow_legacy_unscoped_namespace: bool,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Exclude raw-work cohorts whose semantic or project identities conflict.

    ``build_work_items`` merges snapshots by work_id, including their files.
    Namespace isolation must therefore happen before either the latest-snapshot
    attribution reducer or work-item aggregation sees the cohort. Raw
    ``work_events`` stay untouched on the ledger for forensic visibility.
    """

    cohorts: dict[str, list[dict[str, Any]]] = {}
    for event in work_events:
        work_id = str(
            event.get("work_id")
            or event.get("section_id")
            or event.get("event_id")
            or "work"
        )
        cohorts.setdefault(work_id, []).append(event)

    quarantined_work_ids: set[str] = set()
    quarantined_snapshots = 0
    for work_id, cohort in cohorts.items():
        namespace_compatible = _semantic_namespace_cohort_compatible(
            cohort,
            allow_legacy_unscoped_namespace=allow_legacy_unscoped_namespace,
        )
        project_identities = {
            identity
            for event in cohort
            if (identity := _optional_str(event.get("project_identity")))
        }
        source_namespace_conflict = any(
            event.get("log_evidenced_source_namespace_conflict") is True
            for event in cohort
        )
        if (
            namespace_compatible
            and len(project_identities) <= 1
            and not source_namespace_conflict
        ):
            continue
        quarantined_work_ids.add(work_id)
        quarantined_snapshots += len(cohort)

    return (
        [
            event
            for event in work_events
            if str(
                event.get("work_id")
                or event.get("section_id")
                or event.get("event_id")
                or "work"
            )
            not in quarantined_work_ids
        ],
        {
            "ambiguous_cohorts": len(quarantined_work_ids),
            "quarantined_snapshots": quarantined_snapshots,
        },
    )


def _semantic_namespace_cohort_compatible(
    facts: list[dict[str, Any]],
    *,
    allow_legacy_unscoped_namespace: bool,
) -> bool:
    """Linear equivalent of all pairwise semantic-namespace joins."""

    fingerprints: set[str] = set()
    missing_fingerprint = False
    malformed_explicit = False
    for fact in facts:
        fingerprint = _optional_str(
            fact.get("namespace_fingerprint")
            or fact.get("session_namespace_fingerprint")
        )
        if fingerprint is None:
            missing_fingerprint = True
            if str(fact.get("identity_scope_state") or "") == "explicit":
                malformed_explicit = True
        else:
            fingerprints.add(fingerprint)
    return not (
        malformed_explicit
        or len(fingerprints) > 1
        or (
            bool(fingerprints)
            and missing_fingerprint
            and not allow_legacy_unscoped_namespace
        )
    )


def build_evidence_events(
    events: list[dict[str, Any]],
    *,
    run_reports: list[dict[str, Any]] | None = None,
    log_evidence_index: dict[str, list[dict[str, Any]]] | None = None,
    log_evidence_counters: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    evidence = [_evidence_event(event) for event in events if _is_evidence_event(event)]
    evidence_events = [event for event in evidence if event is not None]
    evidence_events.extend(_run_report_evidence_events(run_reports or []))
    evidence_events = sorted(evidence_events, key=lambda event: float(event.get("created_at") or 0.0), reverse=True)
    index = log_evidence_index if log_evidence_index is not None else build_log_evidence_index(events)
    counters = apply_log_evidence_to_snapshots(evidence_events, index)
    if log_evidence_counters is not None:
        log_evidence_counters.update(counters)
    return evidence_events


def build_usage_debug_events(
    events: list[dict[str, Any]],
    *,
    log_evidence_index: dict[str, list[dict[str, Any]]] | None = None,
    log_evidence_counters: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    debug_events: list[dict[str, Any]] = []
    for event in events:
        metadata = _metadata(event)
        if metadata.get("sentinel_semantic_kind") != "agent_usage_debug":
            continue
        debug_events.append(
            {
                "event_id": _event_id(event),
                "created_at": _safe_float(event.get("created_at")),
                "source": _optional_str(event.get("source")),
                "run_id": _optional_str(event.get("run_id")),
                "client": _optional_str(metadata.get("client")),
                "client_session_id": _optional_str(metadata.get("client_session_id")),
                "provider": _optional_str(event.get("provider") or metadata.get("provider")),
                "model": _optional_str(event.get("model") or metadata.get("model")),
                "reporting_basis": _optional_str(metadata.get("reporting_basis")) or "unknown",
                "summary": _optional_str(metadata.get("summary")),
                "agent_reported_total_tokens": _safe_optional_int(metadata.get("agent_reported_total_tokens")),
                "agent_reported_cost_usd": _safe_optional_float(metadata.get("agent_reported_cost_usd")),
            }
        )
    debug_events = sorted(debug_events, key=lambda event: float(event.get("created_at") or 0.0), reverse=True)
    index = log_evidence_index if log_evidence_index is not None else build_log_evidence_index(events)
    counters = apply_log_evidence_to_snapshots(debug_events, index)
    if log_evidence_counters is not None:
        log_evidence_counters.update(counters)
    return debug_events


def _unlinked_client_context_project_labels(
    events: list[dict[str, Any]], log_evidence_index: dict[str, list[dict[str, Any]]]
) -> set[str]:
    """Casefolded project labels of client_context events STILL session-less after enrichment.

    Feeds the codex-only "Project-level MCP context only" residual chip: a
    context event with no id claims and no single-donor log evidence proves
    that this store holds context from that project which cannot be tied to
    any specific session.
    """

    labels: set[str] = set()
    for event in events:
        metadata = _metadata(event)
        if metadata.get("sentinel_semantic_kind") != "client_context":
            continue
        if _optional_str(metadata.get("client_session_id")) or _optional_str(metadata.get("client_transcript_id")):
            continue
        donors = log_evidence_index.get(_event_id(event))
        if donors and len(donors) == 1:
            # Log evidence ties this context to a session; it is not
            # project-level-only residue.
            continue
        label, _source = _project_label_info(metadata.get("project_dir"))
        if label and label != "~":
            labels.add(label.casefold())
    return labels


def build_attributions(
    usage_events: list[dict[str, Any]],
    work_events: list[dict[str, Any]],
    *,
    allow_legacy_unscoped_namespace: bool = False,
) -> list[dict[str, Any]]:
    projectable_work_events, _diagnostics = _projectable_work_event_cohorts(
        work_events,
        allow_legacy_unscoped_namespace=allow_legacy_unscoped_namespace,
    )
    latest_work_by_id = _latest_work_by_id(projectable_work_events)
    work_candidates = list(latest_work_by_id.values())
    usage_events = annotate_usage_source_namespace_ambiguity(usage_events)
    attributions: list[dict[str, Any]] = []
    for usage in usage_events:
        decision = decide_attribution(
            usage,
            work_candidates,
            allow_legacy_unscoped_namespace=allow_legacy_unscoped_namespace,
        )
        work = decision.get("work")
        strategy = str(decision.get("strategy") or "unjoined")
        attributions.append(
            {
                "usage_event_id": usage.get("usage_event_id"),
                "work_id": work.get("work_id") if work else None,
                "section_id": work.get("section_id") if work else None,
                "join_strategy": strategy,
                "join_confidence": decision.get("confidence"),
                "join_reason": decision.get("reason"),
                # Conflict vetoes from the shared matcher, exposed verbatim so
                # downstream summaries (session rollup context_scope) can tell
                # "no context at all" apart from "context HERE that the
                # matcher refused" without re-deriving join logic.
                "join_vetoes": sorted(str(veto) for veto in (decision.get("vetoes") or [])),
                "ambiguous_candidate_work_ids": list(decision.get("candidate_work_ids") or []) if "ambiguous" in strategy else [],
                "usage_tokens": int(usage.get("total_tokens") or 0),
                "usage_fresh_tokens": int(usage.get("fresh_tokens") or 0),
                "usage_cache_read_tokens": int(usage.get("cache_read_tokens") or 0),
                "usage_cache_creation_tokens": int(usage.get("cache_creation_tokens") or 0),
                "estimated_cost_usd": _safe_optional_float(usage.get("estimated_cost_usd")),
                "usage_confidence": usage.get("usage_confidence"),
                "cost_confidence": usage.get("cost_confidence"),
            }
        )
    return attributions


def build_work_items(
    work_events: list[dict[str, Any]],
    evidence_events: list[dict[str, Any]],
    attributions: list[dict[str, Any]],
    *,
    allow_legacy_unscoped_namespace: bool = False,
) -> list[dict[str, Any]]:
    work_events, _diagnostics = _projectable_work_event_cohorts(
        work_events,
        allow_legacy_unscoped_namespace=allow_legacy_unscoped_namespace,
    )
    grouped: dict[str, dict[str, Any]] = {}
    for event in work_events:
        work_id = str(event.get("work_id") or event.get("section_id") or event.get("event_id") or "work")
        item = grouped.setdefault(
            work_id,
            {
                "work_id": work_id,
                "section_id": event.get("section_id") or work_id,
                "title": event.get("title") or work_id,
                "latest_status": "started",
                "summary": None,
                "client": event.get("client"),
                # The reporting integration is not always the same thing as
                # an evidenced client session, so keep it in a distinct
                # additive field. Product UI can still say who reported an
                # id-less task without inventing a session/model join.
                "reporting_source": event.get("source"),
                "client_session_id": event.get("client_session_id"),
                "client_transcript_id": event.get("client_transcript_id"),
                "parent_client_session_id": event.get("parent_client_session_id"),
                "session_namespace_fingerprint": event.get("session_namespace_fingerprint"),
                "identity_scope_state": event.get("identity_scope_state"),
                "inherited_join_keys": [],
                "inherited_key_sources": {},
                "authored_join_keys": [],
                "log_evidenced_join_keys": [],
                "log_evidence_conflict": None,
                "log_evidence_corroborated": None,
                "log_evidence_ambiguous": None,
                "log_evidence_candidate_sessions": [],
                "claimed_client_session_id": None,
                "claimed_client_transcript_id": None,
                "log_evidenced_by_usage_event_id": None,
                "log_evidenced_by_event_id": None,
                "log_evidence_donor_kind": None,
                "log_evidenced_source_namespace_fingerprint": None,
                "source_namespace_fingerprint": None,
                "client_context_source": None,
                "run_id": event.get("run_id"),
                "project_dir": event.get("project_dir"),
                "kind": event.get("kind"),
                "phase": event.get("phase"),
                "latest_event_id": event.get("event_id"),
                "project_identity": event.get("project_identity"),
                "current_blocked_event_id": (
                    event.get("event_id") if event.get("status") == "blocked" else None
                ),
                "started_at": event.get("created_at"),
                "updated_at": event.get("created_at"),
                "files": [],
                "blocker": None,
                "next_step": None,
            },
        )
        item["title"] = event.get("title") or item["title"]
        item["latest_status"] = event.get("status") or item["latest_status"]
        item["summary"] = event.get("summary") or item["summary"]
        item["client"] = event.get("client") or item["client"]
        item["reporting_source"] = event.get("source") or item["reporting_source"]
        item["client_session_id"] = event.get("client_session_id") or item["client_session_id"]
        item["client_transcript_id"] = event.get("client_transcript_id") or item["client_transcript_id"]
        item["parent_client_session_id"] = event.get("parent_client_session_id") or item["parent_client_session_id"]
        item["session_namespace_fingerprint"] = (
            event.get("session_namespace_fingerprint") or item["session_namespace_fingerprint"]
        )
        item["identity_scope_state"] = event.get("identity_scope_state") or item["identity_scope_state"]
        # Provenance follows the event that supplied the value the item keeps:
        # an id is "inherited" on the item only while its current value came
        # from context inheritance rather than an explicit argument, and the
        # inheritance source (hook vs attach) travels PER KEY — the two ids can
        # be retained from different events with different sources.
        event_inherited = set(event.get("inherited_join_keys") or [])
        event_authored = set(event.get("authored_join_keys") or [])
        event_log_evidenced = set(event.get("log_evidenced_join_keys") or [])
        item_inherited = set(item.get("inherited_join_keys") or [])
        item_authored = set(item.get("authored_join_keys") or [])
        item_log_evidenced = set(item.get("log_evidenced_join_keys") or [])
        key_sources = dict(item.get("inherited_key_sources") or {})
        supplied_id_key = False
        for join_key in ("client_session_id", "client_transcript_id"):
            if event.get(join_key):
                supplied_id_key = True
                if join_key in event_inherited:
                    item_inherited.add(join_key)
                    key_sources[join_key] = event.get("client_context_source")
                else:
                    item_inherited.discard(join_key)
                    key_sources.pop(join_key, None)
                # Server-authored provenance travels with the event that
                # supplied the retained value, exactly like inheritance.
                if join_key in event_authored:
                    item_authored.add(join_key)
                else:
                    item_authored.discard(join_key)
                # Log-evidence provenance travels the same way: the tier of
                # the item's retained value is the tier of the snapshot that
                # supplied it.
                if join_key in event_log_evidenced:
                    item_log_evidenced.add(join_key)
                else:
                    item_log_evidenced.discard(join_key)
        item["inherited_join_keys"] = sorted(item_inherited)
        item["inherited_key_sources"] = key_sources
        item["authored_join_keys"] = sorted(item_authored)
        item["log_evidenced_join_keys"] = sorted(item_log_evidenced)
        if supplied_id_key:
            item["client_context_source"] = event.get("client_context_source")
            # Conflict/corroboration markers travel with the event that
            # supplied the retained id value so the inspector's
            # pair_match(usage, item) applies the same vetoes as the
            # canonical per-snapshot decision.
            item["log_evidence_conflict"] = event.get("log_evidence_conflict")
            item["log_evidence_corroborated"] = event.get("log_evidence_corroborated")
            item["claimed_client_session_id"] = event.get("claimed_client_session_id")
            item["claimed_client_transcript_id"] = event.get("claimed_client_transcript_id")
            item["log_evidenced_by_usage_event_id"] = event.get("log_evidenced_by_usage_event_id")
            item["log_evidenced_by_event_id"] = event.get("log_evidenced_by_event_id")
            item["log_evidence_donor_kind"] = event.get("log_evidence_donor_kind")
            item["log_evidenced_source_namespace_fingerprint"] = event.get(
                "log_evidenced_source_namespace_fingerprint"
            )
            item["source_namespace_fingerprint"] = event.get(
                "log_evidenced_source_namespace_fingerprint"
            )
        # Ambiguity is sticky across snapshots (id-less snapshots supply no
        # key, so it cannot travel with a retained value).
        item["log_evidence_ambiguous"] = event.get("log_evidence_ambiguous") or item.get("log_evidence_ambiguous")
        candidate_sessions = {
            (
                str(candidate.get("client") or ""),
                str(candidate.get("client_session_id") or ""),
                str(candidate.get("source_namespace_fingerprint") or ""),
            )
            for candidate in list(item.get("log_evidence_candidate_sessions") or [])
            if isinstance(candidate, dict)
        }
        candidate_sessions.update(
            (
                str(candidate.get("client") or ""),
                str(candidate.get("client_session_id") or ""),
                str(candidate.get("source_namespace_fingerprint") or ""),
            )
            for candidate in list(event.get("log_evidence_candidate_sessions") or [])
            if isinstance(candidate, dict)
        )
        item["log_evidence_candidate_sessions"] = [
            {
                "client": client,
                "client_session_id": session_id,
                **(
                    {"source_namespace_fingerprint": source_namespace}
                    if source_namespace
                    else {}
                ),
            }
            for client, session_id, source_namespace in sorted(candidate_sessions)
            if client and session_id
        ]
        item["run_id"] = event.get("run_id") or item["run_id"]
        item["project_dir"] = event.get("project_dir") or item["project_dir"]
        item["kind"] = event.get("kind") or item["kind"]
        item["phase"] = event.get("phase") or item["phase"]
        item["latest_event_id"] = event.get("event_id") or item["latest_event_id"]
        item["project_identity"] = event.get("project_identity") or item["project_identity"]
        # MCP optional fields cannot distinguish "not repeated" from an
        # explicit null after persistence. Only the unambiguous terminal
        # ``completed`` status is therefore allowed to clear an older blocker
        # and recovery instruction. Started/checkpoint/blocked snapshots keep
        # the last concrete text unless they provide a replacement. A future
        # non-terminal unblock must use an explicit resolution event.
        if event.get("status") == "completed":
            item["blocker"] = event.get("blocker")
            item["next_step"] = event.get("next_step")
            item["current_blocked_event_id"] = None
        else:
            item["blocker"] = event.get("blocker") or item["blocker"]
            item["next_step"] = event.get("next_step") or item["next_step"]
            if event.get("status") == "blocked":
                item["current_blocked_event_id"] = event.get("event_id")
        item["started_at"] = _min_timestamp(item.get("started_at"), event.get("created_at"))
        item["updated_at"] = _max_timestamp(item.get("updated_at"), event.get("created_at"))
        _extend_unique(item["files"], event.get("files"))

    usage_by_work: dict[str, list[dict[str, Any]]] = {}
    for attribution in attributions:
        work_id = attribution.get("work_id")
        if work_id:
            usage_by_work.setdefault(str(work_id), []).append(attribution)

    evidence_by_work: dict[str, list[dict[str, Any]]] = {}
    for evidence in evidence_events:
        linked_ids = _evidence_link_ids(evidence)
        if linked_ids:
            # Evidence references sections by raw id. With namespaced work
            # items the reference can be ambiguous; link only when exactly
            # one candidate survives client/session filtering — unlinked
            # evidence stays visible rather than being attached to the
            # wrong session's item.
            candidates = _evidence_candidate_work_ids(
                evidence,
                linked_ids,
                grouped,
                allow_legacy_unscoped_namespace=allow_legacy_unscoped_namespace,
            )
            if len(candidates) == 1:
                evidence_by_work.setdefault(candidates[0], []).append(evidence)
            continue
        run_id = evidence.get("run_id")
        if not run_id:
            continue
        for work_id, item in grouped.items():
            if item.get("run_id") == run_id and _evidence_run_context_compatible(
                evidence,
                item,
                allow_legacy_unscoped_namespace=allow_legacy_unscoped_namespace,
            ):
                evidence_by_work.setdefault(work_id, []).append(evidence)

    work_items: list[dict[str, Any]] = []
    for work_id, item in grouped.items():
        usage = usage_by_work.get(work_id, [])
        linked_evidence = _dedupe_by_event_id(evidence_by_work.get(work_id, []))
        usage_total = sum(int(attr.get("usage_tokens") or 0) for attr in usage)
        estimated_cost_total = sum(float(attr.get("estimated_cost_usd") or 0.0) for attr in usage)
        evidence_status = _evidence_status(linked_evidence, item.get("files"))
        work_items.append(
            {
                **item,
                "usage_total": usage_total,
                "usage_fresh_total": sum(int(attr.get("usage_fresh_tokens") or 0) for attr in usage),
                "usage_cache_read_total": sum(int(attr.get("usage_cache_read_tokens") or 0) for attr in usage),
                "usage_cache_creation_total": sum(int(attr.get("usage_cache_creation_tokens") or 0) for attr in usage),
                "linked_usage_records": len(usage),
                "priced_usage_records": sum(
                    1 for attr in usage if _safe_optional_float(attr.get("estimated_cost_usd")) is not None
                ),
                "unpriced_usage_records": sum(
                    1 for attr in usage if _safe_optional_float(attr.get("estimated_cost_usd")) is None
                ),
                "estimated_cost_total": estimated_cost_total,
                "usage_confidence_breakdown": _breakdown_for_attributions(usage, "usage_confidence"),
                "cost_confidence_breakdown": _breakdown_for_attributions(usage, "cost_confidence"),
                "evidence_status": evidence_status,
                "evidence_events": linked_evidence,
                "join_confidence": _best_join_confidence([attr.get("join_confidence") for attr in usage]),
            }
        )
    return sorted(work_items, key=lambda item: float(item.get("updated_at") or 0.0), reverse=True)


def apply_blocker_resolutions(
    work_items: list[dict[str, Any]],
    *,
    work_events: list[dict[str, Any]],
    evidence_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply explicit, server-validated resolutions to exact blocker episodes.

    A resolution is deliberately weaker than a completed/verified outcome. It
    removes one exact blocker from the active user-action surface while
    preserving the original report and labelling the result as an
    evidence-backed *agent-reported* resolution. Every ambiguity rejects.
    """

    item_by_work_id = {
        str(item.get("work_id") or ""): item
        for item in work_items
        if item.get("work_id")
    }
    snapshots_by_event_id: dict[str, list[dict[str, Any]]] = {}
    snapshots_by_work_id: dict[str, list[dict[str, Any]]] = {}
    for snapshot in work_events:
        event_id = str(snapshot.get("event_id") or "")
        work_id = str(snapshot.get("work_id") or "")
        if event_id:
            snapshots_by_event_id.setdefault(event_id, []).append(snapshot)
        if work_id:
            snapshots_by_work_id.setdefault(work_id, []).append(snapshot)

    counters: Counter[str] = Counter()
    attempts = [
        event
        for event in evidence_events
        if _optional_str(event.get("resolves_blocked_event_id"))
    ]
    attempts.sort(
        key=lambda event: (
            float(event.get("created_at") or 0.0),
            str(event.get("event_id") or ""),
        )
    )
    for evidence in attempts:
        counters["attempted"] += 1
        target_id = str(evidence.get("resolves_blocked_event_id") or "")
        candidates = snapshots_by_event_id.get(target_id, [])
        target = candidates[0] if len(candidates) == 1 else None
        item = item_by_work_id.get(str(target.get("work_id") or "")) if target else None
        reason = _blocker_resolution_rejection_reason(
            evidence,
            target=target,
            target_candidate_count=len(candidates),
            item=item,
            snapshots_by_work_id=snapshots_by_work_id,
        )
        attempt_summary = {
            "event_id": evidence.get("event_id"),
            "target_blocked_event_id": target_id,
            "scope": evidence.get("resolution_scope"),
            "state": "rejected" if reason else "accepted",
            "reason": reason,
        }
        if item is not None:
            item.setdefault("blocker_resolution_attempts", []).append(attempt_summary)
        if reason:
            counters["rejected"] += 1
            counters[f"rejected:{reason}"] += 1
            continue

        assert target is not None and item is not None
        scope = str(evidence.get("resolution_scope") or "")
        resolution = {
            "state": "resolved" if scope == "full" else "partially_resolved",
            "scope": scope,
            "basis": "agent_claim_with_passed_check",
            "authoritative": False,
            "summary": evidence.get("resolution_summary"),
            "blocked_event_id": target_id,
            "resolution_event_id": evidence.get("event_id"),
            "resolved_at": evidence.get("created_at"),
            "objective_basis": evidence.get("resolution_objective_basis"),
        }
        item["blocker_resolution"] = resolution
        counters["accepted"] += 1
        counters[f"accepted:{scope}"] += 1
        # Both full and partial resolutions are fresh outcome information.
        # Refresh recency without changing a partial blocker's active state.
        item["outcome_updated_at"] = evidence.get("created_at")
        item["updated_at"] = _max_timestamp(item.get("updated_at"), evidence.get("created_at"))
        if scope == "partial":
            # A partial claim is useful context, never permission to remove an
            # unstructured blocker or its Needs input state.
            continue

        item["semantic_latest_status"] = item.get("latest_status")
        item["latest_status"] = "resolved"
        item["reported_blocked_event_id"] = item.get("current_blocked_event_id")
        item["current_blocked_event_id"] = None
        item["blocker"] = None
        item["next_step"] = None

    work_items.sort(key=lambda item: float(item.get("updated_at") or 0.0), reverse=True)
    return {
        "attempted": int(counters.get("attempted", 0)),
        "accepted": int(counters.get("accepted", 0)),
        "accepted_full": int(counters.get("accepted:full", 0)),
        "accepted_partial": int(counters.get("accepted:partial", 0)),
        "rejected": int(counters.get("rejected", 0)),
        "rejected_by_reason": {
            key.removeprefix("rejected:"): value
            for key, value in sorted(counters.items())
            if key.startswith("rejected:")
        },
        "raw_blocker_history_preserved": True,
        "resolution_basis": "agent_reported",
    }


def _blocker_resolution_rejection_reason(
    evidence: dict[str, Any],
    *,
    target: dict[str, Any] | None,
    target_candidate_count: int,
    item: dict[str, Any] | None,
    snapshots_by_work_id: dict[str, list[dict[str, Any]]],
) -> str | None:
    if target_candidate_count != 1:
        return "target_missing" if target_candidate_count == 0 else "target_not_unique"
    assert target is not None
    if target.get("status") != "blocked":
        return "target_not_blocked"
    if item is None:
        return "target_work_not_projectable"
    if item.get("current_blocked_event_id") != target.get("event_id"):
        return "target_not_current_blocker"
    if evidence.get("result") != "passed":
        return "evidence_not_passed"
    scope = _optional_str(evidence.get("resolution_scope"))
    if scope not in {"full", "partial"}:
        return "invalid_scope"
    if not _optional_str(evidence.get("resolution_summary")):
        return "resolution_summary_missing"
    if _optional_str(evidence.get("resolution_objective_basis")) not in {
        "exit_code",
        "artifact_ref",
        "artifact_path",
        "artifact_url",
    }:
        return "objective_basis_missing"
    if _optional_str(evidence.get("asserted_section_id")) != _optional_str(
        target.get("section_id")
    ):
        return "section_mismatch"
    asserted_work_id = _optional_str(evidence.get("asserted_work_id"))
    if asserted_work_id and asserted_work_id != _optional_str(target.get("work_id")):
        return "work_id_mismatch"
    if _optional_str(evidence.get("source")) != _optional_str(target.get("source")):
        return "source_mismatch"
    if _optional_str(evidence.get("client")) != _optional_str(target.get("client")):
        return "client_mismatch"
    evidence_project = _optional_str(evidence.get("project_identity"))
    target_project = _optional_str(target.get("project_identity"))
    if not evidence_project or not target_project:
        return "project_identity_missing"
    if evidence_project != target_project:
        return "project_identity_mismatch"
    if _optional_str(item.get("project_identity")) != target_project:
        return "merged_work_project_identity_mismatch"
    if not namespace_join_compatible(
        evidence,
        target,
        allow_legacy_unscoped=False,
    ):
        return "namespace_mismatch"
    evidence_at = float(evidence.get("created_at") or 0.0)
    target_at = float(target.get("created_at") or 0.0)
    if evidence_at <= target_at:
        return "evidence_not_after_blocker"
    latest_snapshot_at = max(
        (
            float(snapshot.get("created_at") or 0.0)
            for snapshot in snapshots_by_work_id.get(str(target.get("work_id") or ""), [])
        ),
        default=target_at,
    )
    if evidence_at <= latest_snapshot_at:
        return "newer_work_snapshot_exists"
    return None


def build_join_inspector(
    usage_events: list[dict[str, Any]],
    work_items: list[dict[str, Any]],
    attributions: list[dict[str, Any]],
    *,
    allow_legacy_unscoped_namespace: bool = False,
) -> dict[str, Any]:
    """Explain why each work item did or did not receive usage attribution."""

    usage_events = annotate_usage_source_namespace_ambiguity(usage_events)
    attributions_by_work: dict[str, list[dict[str, Any]]] = {}
    for attribution in attributions:
        work_id = attribution.get("work_id")
        if work_id:
            attributions_by_work.setdefault(str(work_id), []).append(attribution)

    # An item is ambiguous iff it was among the tied candidates of some usage
    # row whose canonical decision was *_ambiguous_sections. Deriving this
    # from the canonical decisions (instead of recounting here) keeps the
    # inspector and the attribution model in perfect agreement.
    ambiguous_work_ids: set[str] = set()
    for attribution in attributions:
        for candidate_id in attribution.get("ambiguous_candidate_work_ids") or []:
            if candidate_id:
                ambiguous_work_ids.add(str(candidate_id))

    explanations: dict[str, dict[str, Any]] = {}
    for item in work_items:
        work_id = str(item.get("work_id") or item.get("section_id") or "")
        attributed = attributions_by_work.get(work_id, [])
        candidates = []
        for usage in usage_events:
            match = pair_match(
                usage,
                item,
                allow_legacy_unscoped_namespace=allow_legacy_unscoped_namespace,
            )
            if match is None or not match.get("join_keys"):
                continue
            candidates.append({"usage": usage, "join_keys": match["join_keys"], "join_confidence": pair_confidence(match)})
        candidate_usage = [candidate["usage"] for candidate in candidates]
        ambiguous = work_id in ambiguous_work_ids
        missing_join_keys = _missing_work_join_keys(item)
        explanation = _work_join_explanation(
            item=item,
            usage_events=usage_events,
            attributed=attributed,
            candidates=candidates,
            ambiguous=ambiguous,
            missing_join_keys=missing_join_keys,
        )
        explanation["candidate_usage_count"] = len(candidate_usage)
        explanation["attributed_usage_count"] = len(attributed)
        explanation["context_matched_usage_count"] = len(candidate_usage)
        explanation["nearest_usage_summary"] = _nearest_usage_summary(item, candidate_usage or usage_events)
        explanations[work_id] = explanation

    attributed_usage = [attr for attr in attributions if _is_attributed_attribution(attr)]
    ambiguous_usage = [attr for attr in attributions if _is_ambiguous_attribution(attr)]
    context_matched_unallocated = [attr for attr in attributions if _is_context_matched_unallocated_attribution(attr)]
    usage_without_context = [attr for attr in attributions if str(attr.get("join_strategy") or "") == "unjoined"]
    unattributed_usage_count = len(ambiguous_usage) + len(context_matched_unallocated) + len(usage_without_context)
    return {
        "work_item_join_explanations": explanations,
        "usage_truth_count": len(attributions),
        "attributed_count": len(attributed_usage),
        "ambiguous_count": len(ambiguous_usage),
        "context_matched_unallocated_count": len(context_matched_unallocated),
        "usage_without_mcp_context_count": len(usage_without_context),
        "unattributed_count": unattributed_usage_count,
        "attributed_usage_count": len(attributed_usage),
        "context_matched_unallocated_usage_count": len(context_matched_unallocated),
        "ambiguous_usage_count": len(ambiguous_usage),
        "unattributed_usage_count": unattributed_usage_count,
        "missing_client_session_work_count": sum(1 for item in work_items if "client_session_id" in _missing_work_join_keys(item)),
    }


def build_usage_reconciliation(
    usage_events: list[dict[str, Any]],
    work_items: list[dict[str, Any]],
    attributions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    work_by_id = {str(item.get("work_id") or ""): item for item in work_items if item.get("work_id")}
    usage_by_id = {str(usage.get("usage_event_id") or ""): usage for usage in usage_events if usage.get("usage_event_id")}
    rows: list[dict[str, Any]] = []
    for attribution in attributions:
        usage = usage_by_id.get(str(attribution.get("usage_event_id") or ""))
        if usage is None:
            continue
        work = work_by_id.get(str(attribution.get("work_id") or ""))
        join_strategy = str(attribution.get("join_strategy") or "unjoined")
        if work is not None:
            state = "attributed"
            recommended = "Usage is attached to this work item with deterministic local join keys."
        elif _is_ambiguous_attribution(attribution):
            state = "ambiguous"
            recommended = "Multiple sections share this client session; agentacct does not allocate section-level usage."
        elif _is_context_matched_unallocated_attribution(attribution):
            state = "context_matched_unallocated"
            if join_strategy == "parent_child_context_hint":
                recommended = (
                    "Record a section in the child session (hook bridge) or attach the child session id explicitly; "
                    "parent/child links group but never allocate."
                )
            else:
                recommended = "MCP context matched, but agentacct does not allocate section-level usage."
        else:
            state = "usage_without_mcp_context"
            recommended = "Usage exists but no MCP work context matched."
        rows.append(
            {
                "usage_event_id": usage.get("usage_event_id"),
                "client": usage.get("client"),
                "client_session_id": usage.get("client_session_id"),
                "provider": usage.get("provider"),
                "model": usage.get("model"),
                "total_tokens": usage.get("total_tokens"),
                "fresh_tokens": usage.get("fresh_tokens"),
                "cache_read_tokens": usage.get("cache_read_tokens"),
                "cache_creation_tokens": usage.get("cache_creation_tokens"),
                "estimated_cost_usd": usage.get("estimated_cost_usd"),
                "usage_confidence": usage.get("usage_confidence"),
                "cost_confidence": usage.get("cost_confidence"),
                "usage_reconciliation_state": state,
                "usage_join_state": state,
                "join_confidence": attribution.get("join_confidence"),
                "join_strategy": join_strategy,
                "join_reason": attribution.get("join_reason"),
                "work_id": work.get("work_id") if work else None,
                "work_title": work.get("title") if work else None,
                "recommended_next_step": recommended,
            }
        )
    # Attributed rows first (the accidental alphabetical-descending state sort
    # buried the only attributed row below hundreds of unattributed ones and
    # render caps truncated it out entirely). Tiebreak: fresh tokens desc —
    # consistent with the fresh-first headline — then full-weight total desc.
    return sorted(
        rows,
        key=lambda row: (
            RECONCILIATION_STATE_RANK.get(str(row.get("usage_join_state") or ""), len(RECONCILIATION_STATE_RANK)),
            -int(row.get("fresh_tokens") or 0),
            -int(row.get("total_tokens") or 0),
        ),
    )


def build_attention_items(work_items: list[dict[str, Any]], usage_reconciliation: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for work in work_items:
        explanation = work.get("join_explanation") if isinstance(work.get("join_explanation"), dict) else {}
        title = str(work.get("title") or work.get("work_id") or "Work item")
        section_id = str(work.get("section_id") or work.get("work_id") or "")
        if work.get("latest_status") == "completed" and work.get("evidence_status") in {"none", "weak"}:
            items.append(
                {
                    "attention_type": "completed_without_strong_evidence",
                    "severity": "high",
                    "title": f"{title}: completed without strong evidence",
                    "work_id": work.get("work_id"),
                    "section_id": section_id,
                    "summary": "The agent marked this work completed, but no passing machine-check evidence is linked.",
                    "recommended_next_step": "Record a test, build, smoke, or artifact machine-check for this completed work.",
                }
            )
        if work.get("latest_status") == "completed" and work.get("evidence_status") == "strong" and int(work.get("linked_usage_records") or 0) == 0:
            items.append(
                {
                    "attention_type": "completed_evidenced_work_without_attributed_usage",
                    "severity": "medium",
                    "title": f"{title}: usage not attributed",
                    "work_id": work.get("work_id"),
                    "section_id": section_id,
                    "summary": _zero_usage_explanation(explanation),
                    "recommended_next_step": explanation.get("recommended_next_step")
                    or "Run local usage import and make sure MCP context includes client_session_id.",
                }
            )
        if explanation.get("usage_join_state") == "ambiguous":
            items.append(
                {
                    "attention_type": "ambiguous_same_session_attribution",
                    "severity": "medium",
                    "title": f"{title}: ambiguous same-session attribution",
                    "work_id": work.get("work_id"),
                    "section_id": section_id,
                    "summary": explanation.get("join_reason"),
                    "recommended_next_step": explanation.get("recommended_next_step"),
                }
            )
        if "client_session_id" in (explanation.get("missing_join_keys") or []):
            items.append(
                {
                    "attention_type": "missing_client_session_id",
                    "severity": "medium",
                    "title": f"{title}: missing client_session_id",
                    "work_id": work.get("work_id"),
                    "section_id": section_id,
                    "summary": "MCP work context is missing the main join key agentacct needs to reconcile usage logs.",
                    "recommended_next_step": "MCP context is missing client_session_id; attach client context at session start.",
                }
            )
    for usage in usage_reconciliation:
        if usage.get("usage_reconciliation_state") != "usage_without_mcp_context" and usage.get("usage_join_state") != "usage_without_mcp_context":
            continue
        items.append(
            {
                "attention_type": "usage_truth_without_mcp_context",
                "severity": "medium",
                "title": f"{_provider_model_label(usage.get('provider'), usage.get('model'))}: usage has no work context",
                "usage_event_id": usage.get("usage_event_id"),
                "summary": usage.get("join_reason"),
                "recommended_next_step": usage.get("recommended_next_step"),
                "client": usage.get("client"),
                "client_session_id": usage.get("client_session_id"),
                "provider": usage.get("provider"),
                "model": usage.get("model"),
                "total_tokens": int(usage.get("total_tokens") or 0),
                "fresh_tokens": int(usage.get("fresh_tokens") or 0),
            }
        )
    severity_rank = {"high": 3, "medium": 2, "low": 1}
    return sorted(items, key=lambda item: severity_rank.get(str(item.get("severity")), 0), reverse=True)


# One shared sentence so the attention group and the matching blind spot never
# drift. It deliberately stops short of blaming the user for all of it: part of
# "no work context" is recording calls agentacct itself refused, which the
# Local logs page now counts (see api._refused_recording_html).
_USAGE_WITHOUT_MCP_CONTEXT_NEXT_STEP = (
    "Enable MCP context attach at session start and record sections for meaningful work; "
    "some of this missing context is recording calls agentacct refused, listed under "
    "\"Recording calls agentacct refused\" in Local logs."
)

# Canonical, cause-level next steps for grouped attention display. Falls back
# to the first item's own recommendation for unknown causes.
_ATTENTION_GROUP_NEXT_STEPS = {
    "completed_without_strong_evidence": "Record a test, build, smoke, or artifact machine-check for completed work.",
    "completed_evidenced_work_without_attributed_usage": "Run local usage import and make sure MCP context includes client_session_id.",
    "ambiguous_same_session_attribution": "Attach client_transcript_id or another narrower join key so shared sessions can be disambiguated.",
    "missing_client_session_id": "MCP context is missing client_session_id; attach client context at session start.",
    "usage_truth_without_mcp_context": _USAGE_WITHOUT_MCP_CONTEXT_NEXT_STEP,
}

_ATTENTION_GROUP_TITLES = {
    "completed_without_strong_evidence": "{count} completed work item(s) without strong evidence",
    "completed_evidenced_work_without_attributed_usage": "{count} evidence-backed completed item(s) with no attributed usage",
    "ambiguous_same_session_attribution": "{count} work item(s) with ambiguous same-session usage",
    "missing_client_session_id": "{count} work item(s) missing client_session_id",
    "usage_truth_without_mcp_context": "{count} usage row(s) have no work context",
}

ATTENTION_GROUP_EXAMPLE_LIMIT = 3


def build_attention_groups(
    attention_items: list[dict[str, Any]],
    *,
    session_labels: dict[tuple[str, str], str] | None = None,
) -> dict[str, Any]:
    """Group attention items by cause: one group per attention_type.

    Derived purely FROM the detail items, so group counts can never disagree
    with the detail list. Replaces flood semantics for display: the headline
    number is the group count; the raw item count travels alongside.

    ``session_labels`` is the rollup's collision-aware label map: example
    refs render the SAME suffixed short label as the session rows, so two
    different sessions can never share an example label.
    """

    groups_by_cause: dict[str, dict[str, Any]] = {}
    for item in attention_items:
        cause = str(item.get("attention_type") or "unknown")
        group = groups_by_cause.get(cause)
        if group is None:
            group = groups_by_cause[cause] = {
                "cause": cause,
                "severity": str(item.get("severity") or "medium"),
                "count": 0,
                "title": "",
                "recommended_next_step": _ATTENTION_GROUP_NEXT_STEPS.get(cause)
                or _optional_str(item.get("recommended_next_step"))
                or "",
                "example_refs": [],
                # Token sums exist only for usage-bearing causes; None is not
                # a zero-usage claim for work-item causes.
                "fresh_tokens": None,
                "total_tokens": None,
            }
        group["count"] += 1
        if item.get("usage_event_id"):
            group["fresh_tokens"] = int(group["fresh_tokens"] or 0) + int(item.get("fresh_tokens") or 0)
            group["total_tokens"] = int(group["total_tokens"] or 0) + int(item.get("total_tokens") or 0)
        if len(group["example_refs"]) < ATTENTION_GROUP_EXAMPLE_LIMIT:
            group["example_refs"].append(_attention_example_ref(item, session_labels or {}))
    severity_rank = {"high": 3, "medium": 2, "low": 1}
    groups = sorted(
        groups_by_cause.values(),
        key=lambda group: (severity_rank.get(str(group.get("severity")), 0), int(group.get("count") or 0)),
        reverse=True,
    )
    for group in groups:
        template = _ATTENTION_GROUP_TITLES.get(str(group["cause"]))
        group["title"] = (
            template.format(count=group["count"]) if template else f"{group['count']} attention item(s): {group['cause']}"
        )
    return {"groups": groups, "total_items": len(attention_items)}


def _attention_example_ref(item: dict[str, Any], session_labels: dict[tuple[str, str], str]) -> dict[str, Any]:
    """Bounded, redacted pointer to one detail item (work item or usage row).

    Session labels come from the rollup's collision-aware map; the bare
    truncation is only a fallback for keys that cannot exist in the map
    (client or session id missing on the row).
    """

    usage_event_id = _optional_str(item.get("usage_event_id"))
    if usage_event_id is not None:
        session_id = _optional_str(item.get("client_session_id"))
        session_label = session_labels.get(
            (str(item.get("client") or ""), str(session_id or ""))
        ) or _base_session_display_label(item.get("client"), session_id)
        parts = [
            _optional_str(item.get("client")) or "unknown client",
            _provider_model_label(item.get("provider"), item.get("model")),
        ]
        if session_label:
            parts.append(session_label)
        return {"kind": "usage", "usage_event_id": usage_event_id, "label": " · ".join(parts)}
    return {
        "kind": "work",
        "work_id": item.get("work_id"),
        "label": _short_public_text(str(item.get("title") or item.get("work_id") or "work item"), max_length=60),
    }


def _session_source_parent_compatible(
    child: Mapping[str, Any],
    parent: Mapping[str, Any],
) -> bool:
    child_source = _optional_str(child.get("source_namespace_fingerprint"))
    parent_source = _optional_str(parent.get("source_namespace_fingerprint"))
    expected_parent = _optional_str(
        child.get("parent_source_namespace_fingerprint")
    )
    if child_source is None and parent_source is None:
        return expected_parent is None
    if (
        child_source is None
        or parent_source is None
        or child_source != parent_source
    ):
        return False
    return expected_parent is None or expected_parent == parent_source


def _log_evidenced_work_source_compatible(
    usage: Mapping[str, Any],
    work: Mapping[str, Any],
) -> bool:
    evidenced_source = _optional_str(
        work.get("log_evidenced_source_namespace_fingerprint")
    )
    if evidenced_source is None:
        return True
    return _optional_str(usage.get("source_namespace_fingerprint")) == evidenced_source


def build_session_rollup(
    *,
    usage_events: list[dict[str, Any]],
    work_items: list[dict[str, Any]],
    attributions: list[dict[str, Any]],
    store_project_label: str | None = None,
    store_scope: str | None = None,
    log_evidence_by_key: dict[tuple[str, str, str | None], dict[str, Any]] | None = None,
    unlinked_context_project_labels: set[str] | None = None,
    instrumentation_markers: dict[str, Any] | None = None,
    session_observations: list[dict[str, Any]] | None = None,
    local_session_observations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One entry per (client, base client session id) — pure derivation.

    Reads ONLY already-computed results: usage rows are ``build_usage_events``
    output (shadow-excluded, base-normalized), join state derives verbatim
    from ``build_attributions`` decisions, and work items attach by their OWN
    exact (client, client_session_id) equality — never a decision's work
    pointer, never transcript fallback. The rollup re-matches nothing, so it
    can never contradict the canonical attribution model.

    Honesty rules baked in:
    - entry ``usage`` totals come ONLY from rows whose own key equals the
      entry key; ``related.children_usage`` is a SEPARATE, labeled descendants
      subtotal (importer-recorded parent pointers) that is never merged in.
    - sessions with sections but no usage rows appear with usage zeros and a
      not-a-zero-cost note; usage-only sessions appear with empty work items.
    - templates render only ``client_session_id_short`` (8-char component
      rule, collision-suffixed); full ids stay JSON-only, per the existing
      /work-items precedent.

    Additive (Phase 3.5c, schema stays v1 — nothing existing changed):
    entry ``duration_seconds`` (own first/last activity span, None unless
    honestly derivable) and ``usage.turns_total`` (importer-recorded turn
    counts summed over the entry's OWN rows only, None when no row carries
    one; children never merged — same rule as tokens).
    """

    attribution_by_usage: dict[str, dict[str, Any]] = {
        str(attr.get("usage_event_id")): attr for attr in attributions if attr.get("usage_event_id")
    }
    item_by_work_id = {str(item.get("work_id") or ""): item for item in work_items if item.get("work_id")}

    rows_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    key_order: list[tuple[str, str]] = []
    for usage in usage_events:
        client = _optional_str(usage.get("client"))
        session_id = _optional_str(usage.get("client_session_id"))
        if client is None or session_id is None:
            # Trusted import rows always carry both; a keyless row cannot form
            # an exact-equality entry and stays visible in usage_events.
            continue
        key = (client, session_id)
        if key not in rows_by_key:
            key_order.append(key)
        rows_by_key.setdefault(key, []).append(usage)

    # Historical/custom stores can contain two trusted usage rows whose bare
    # client/session ids collide across explicit issuer namespaces. The v1
    # rollup key cannot represent both identities without merging them, so
    # quarantine the entire ambiguous cohort from the session projection.
    # Raw usage_events remain untouched and visible to forensic/usage views.
    allow_legacy_unscoped_namespace = store_scope == "project"
    usage_namespace_collision_rows = 0
    usage_namespace_collision_sessions = 0
    usage_namespace_collision_keys: set[tuple[str, str]] = set()
    for key, rows in list(rows_by_key.items()):
        semantic_namespaces_compatible = _semantic_namespace_cohort_compatible(
            rows,
            allow_legacy_unscoped_namespace=allow_legacy_unscoped_namespace,
        )
        source_fingerprints = {
            value
            for row in rows
            if (value := _optional_str(row.get("source_namespace_fingerprint")))
        }
        source_fingerprint_missing = any(
            not _optional_str(row.get("source_namespace_fingerprint"))
            for row in rows
        )
        # Import-source identity is intentionally independent of semantic
        # namespace and has no project-store legacy exception: either every
        # row is legacy (all missing), or every row asserts the same source.
        source_namespaces_compatible = (
            not source_fingerprints
            or (len(source_fingerprints) == 1 and not source_fingerprint_missing)
        )
        if not semantic_namespaces_compatible or not source_namespaces_compatible:
            usage_namespace_collision_sessions += 1
            usage_namespace_collision_rows += len(rows)
            usage_namespace_collision_keys.add(key)
            rows_by_key[key] = []

    items_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    unassigned_items: list[dict[str, Any]] = []
    for item in work_items:
        client = _optional_str(item.get("client"))
        session_id = _optional_str(item.get("client_session_id"))
        if client is None or session_id is None:
            # Transcript-only or id-less items never form/attach to a session
            # entry (exact key equality only). Row-level attributions can
            # still reference them via join.attributed_work, so nothing is
            # lost and nothing is allocated.
            unassigned_items.append(item)
            continue
        key = (client, session_id)
        if key not in rows_by_key and key not in items_by_key:
            key_order.append(key)
        items_by_key.setdefault(key, []).append(item)

    # Namespace isolation happens before session rollup coalesces usage and
    # work. A raw (client, session id) collision is not identity proof when
    # issuer fingerprints disagree. Keep the trusted usage session visible,
    # but quarantine incompatible work as unassigned; the top-level work item
    # remains available and TaskProjection will expose the refusal instead of
    # attaching it to the wrong Task.
    namespace_work_join_refusals = 0
    for key, items in list(items_by_key.items()):
        if key in usage_namespace_collision_keys:
            namespace_work_join_refusals += len(items)
            unassigned_items.extend(items)
            items_by_key[key] = []
            continue
        rows = rows_by_key.get(key, [])
        if rows:
            compatible_items: list[dict[str, Any]] = []
            for item in items:
                if all(
                    namespace_join_compatible(
                        row,
                        item,
                        allow_legacy_unscoped=allow_legacy_unscoped_namespace,
                    )
                    and _log_evidenced_work_source_compatible(row, item)
                    for row in rows
                ):
                    compatible_items.append(item)
                else:
                    namespace_work_join_refusals += 1
                    unassigned_items.append(item)
            items_by_key[key] = compatible_items
            continue

        # With no usage row to anchor the session, several work items may
        # still collide on the same raw key. Do not pick a winning namespace:
        # either the whole cohort is mutually compatible or every item stays
        # visible but unassigned.
        mutually_compatible = _semantic_namespace_cohort_compatible(
            items,
            allow_legacy_unscoped_namespace=allow_legacy_unscoped_namespace,
        )
        if not mutually_compatible:
            namespace_work_join_refusals += len(items)
            unassigned_items.extend(items)
            items_by_key[key] = []

    observations_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for observation in session_observations or []:
        client = _optional_str(observation.get("client"))
        session_id = _optional_str(observation.get("client_session_id"))
        if client is None or session_id is None:
            continue
        key = (client, session_id)
        if key not in rows_by_key and key not in items_by_key and key not in observations_by_key:
            key_order.append(key)
        observations_by_key.setdefault(key, []).append(observation)

    local_observations_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for observation in local_session_observations or []:
        client = _optional_str(observation.get("client"))
        session_id = _optional_str(observation.get("client_session_id"))
        if client is None or session_id is None:
            continue
        key = (client, session_id)
        if (
            key not in rows_by_key
            and key not in items_by_key
            and key not in observations_by_key
            and key not in local_observations_by_key
        ):
            key_order.append(key)
        local_observations_by_key.setdefault(key, []).append(observation)

    namespace_join_refusals = 0
    for key, observations in list(observations_by_key.items()):
        if key in usage_namespace_collision_keys:
            namespace_join_refusals += len(observations)
            observations_by_key[key] = []
            continue
        rows = rows_by_key.get(key, [])
        items = items_by_key.get(key, [])
        if not rows and not items:
            continue
        known_fingerprints = {
            value
            for row in [*rows, *items]
            if (
                value := _optional_str(
                    row.get("session_namespace_fingerprint") or row.get("namespace_fingerprint")
                )
            )
        }
        compatible: list[dict[str, Any]] = []
        for observation in observations:
            fingerprint = _optional_str(observation.get("namespace_fingerprint"))
            if fingerprint:
                if fingerprint in known_fingerprints:
                    compatible.append(observation)
                else:
                    namespace_join_refusals += 1
                continue
            if store_scope == "project":
                compatible.append(observation)
            else:
                # A custom/global store can contain several client homes or
                # projects. An unscoped hook may still create its own session,
                # but it cannot enrich an existing v1 row without a shared
                # issuer namespace.
                namespace_join_refusals += 1
        observations_by_key[key] = compatible

    local_namespace_join_refusals = 0
    for key, local_observations in list(local_observations_by_key.items()):
        if key in usage_namespace_collision_keys:
            local_namespace_join_refusals += len(local_observations)
            local_observations_by_key[key] = []
            continue
        rows = rows_by_key.get(key, [])
        if not rows:
            continue
        row_source_namespaces = {
            _optional_str(row.get("source_namespace_fingerprint")) for row in rows
        }
        compatible = [
            observation
            for observation in local_observations
            if {_optional_str(observation.get("source_namespace_fingerprint"))}
            == row_source_namespaces
        ]
        local_namespace_join_refusals += len(local_observations) - len(compatible)
        local_observations_by_key[key] = compatible

    local_observation_work_namespace_join_refusals = 0
    for key, items in list(items_by_key.items()):
        if rows_by_key.get(key) or not items:
            continue
        local_observations = local_observations_by_key.get(key, [])
        if not local_observations:
            continue
        observation_sources = {
            _optional_str(observation.get("source_namespace_fingerprint"))
            for observation in local_observations
        }
        compatible_items = [
            item
            for item in items
            if len(observation_sources) == 1
            and None not in observation_sources
            and _optional_str(
                item.get("log_evidenced_source_namespace_fingerprint")
            )
            in observation_sources
        ]
        refused_items = [item for item in items if item not in compatible_items]
        local_observation_work_namespace_join_refusals += len(refused_items)
        unassigned_items.extend(refused_items)
        items_by_key[key] = compatible_items

    local_observation_mechanical_namespace_join_refusals = 0
    for key, observations in list(observations_by_key.items()):
        if rows_by_key.get(key) or items_by_key.get(key) or not observations:
            continue
        local_observations = local_observations_by_key.get(key, [])
        if not local_observations:
            continue
        local_sources = {
            _optional_str(observation.get("source_namespace_fingerprint"))
            for observation in local_observations
        }
        compatible_observations = [
            observation
            for observation in observations
            if len(local_sources) == 1
            and None not in local_sources
            and _optional_str(
                observation.get("source_namespace_fingerprint")
            )
            in local_sources
        ]
        local_observation_mechanical_namespace_join_refusals += (
            len(observations) - len(compatible_observations)
        )
        observations_by_key[key] = compatible_observations

    entries: list[dict[str, Any]] = []
    for key in key_order:
        client, session_id = key
        rows = rows_by_key.get(key, [])
        items = items_by_key.get(key, [])
        observations = observations_by_key.get(key, [])
        local_observations = local_observations_by_key.get(key, [])
        if not rows and not items and not observations and not local_observations:
            continue
        entries.append(
            _session_rollup_entry(
                client=client,
                session_id=session_id,
                rows=rows,
                items=items,
                observations=observations,
                local_observations=local_observations,
                attribution_by_usage=attribution_by_usage,
                item_by_work_id=item_by_work_id,
                allow_legacy_unscoped_namespace=allow_legacy_unscoped_namespace,
            )
        )

    # One label universe: entry keys PLUS referenced parent ids (parents can
    # be absent as entries), so a parent reference can never render a label
    # identical to a different session's label.
    label_keys: list[tuple[str, str]] = [(entry["client"], entry["client_session_id"]) for entry in entries]
    seen_label_keys = set(label_keys)
    for entry in entries:
        parent = entry["related"]["parent"]
        if parent is not None:
            parent_key = (entry["client"], str(parent["client_session_id"]))
            if parent_key not in seen_label_keys:
                seen_label_keys.add(parent_key)
                label_keys.append(parent_key)
    labels = _assign_session_display_labels(label_keys)
    for entry in entries:
        entry["client_session_id_short"] = labels[(entry["client"], entry["client_session_id"])]

    entry_by_key = {
        (entry["client"], entry["client_session_id"]): entry
        for entry in entries
    }
    children_by_parent: dict[tuple[str, str], list[dict[str, Any]]] = {}
    parent_source_namespace_join_refusals = 0
    compatible_parent_keys: set[tuple[str, str]] = set()
    for entry in entries:
        parent = entry["related"]["parent"]
        if parent is not None:
            parent_key = (entry["client"], str(parent["client_session_id"]))
            parent_entry = entry_by_key.get(parent_key)
            if parent_entry is not None and not _session_source_parent_compatible(
                entry,
                parent_entry,
            ):
                entry["related"]["parent_source_namespace_mismatch"] = True
                parent_source_namespace_join_refusals += 1
                continue
            compatible_parent_keys.add((entry["client"], entry["client_session_id"]))
            children_by_parent.setdefault(parent_key, []).append(entry)

    for entry in entries:
        key = (entry["client"], entry["client_session_id"])
        parent = entry["related"]["parent"]
        parent_id = str(parent["client_session_id"]) if parent is not None else None
        if parent is not None:
            # Parent referenced but absent as an entry: reference kept, no
            # stub entry — the label still comes from the shared
            # collision-aware assigner (parent keys are in the universe).
            parent["label"] = labels[(entry["client"], parent_id)]
        children = sorted(
            children_by_parent.get(key, []),
            key=lambda child: (child.get("last_activity_at") is not None, child.get("last_activity_at") or 0.0),
            reverse=True,
        )
        entry["related"]["child_session_count"] = len(children)
        entry["related"]["child_session_labels"] = [child["client_session_id_short"] for child in children[:5]]
        if children:
            priced = [
                child["usage"]["estimated_cost_usd"]
                for child in children
                if child["usage"]["estimated_cost_usd"] is not None
            ]
            # Labeled descendants subtotal from the child entries' OWN totals.
            # NEVER added into entry["usage"]; renderers must label it
            # "descendants, not allocated".
            children_usage = {
                "sessions": len(children),
                "fresh_tokens": sum(int(child["usage"]["fresh_tokens"] or 0) for child in children),
                "cache_creation_tokens": sum(int(child["usage"]["cache_creation_tokens"] or 0) for child in children),
                "cache_read_tokens": sum(int(child["usage"]["cache_read_tokens"] or 0) for child in children),
                "total_tokens": sum(int(child["usage"]["total_tokens"] or 0) for child in children),
                "estimated_cost_usd": sum(priced) if priced else None,
            }
            excluded_children_rows = sum(
                int(child["usage"].get("excluded_non_additive_rows") or 0) for child in children
            )
            if excluded_children_rows:
                children_usage["excluded_non_additive_rows"] = excluded_children_rows
            entry["related"]["children_usage"] = children_usage
        parent_is_compatible = (
            entry["client"],
            entry["client_session_id"],
        ) in compatible_parent_keys
        entry["rollup_group_key"] = (
            f"{entry['client']}::{parent_id}"
            if parent_id is not None and parent_is_compatible
            else f"{entry['client']}::{entry['client_session_id']}"
        )

    # Instrumentation pre/post classification: per-client marker vs the
    # entry's OWN first activity; children inherit the root's state (a
    # subagent of a pre-install session is pre-install regardless of when it
    # spawned). Runs AFTER the children pass so parent links are final.
    _apply_instrumentation_states(entries, instrumentation_markers)

    # Cross-store context honesty (Phase 2.6): PROJECT stores are per-project,
    # so a zero-MCP-context session whose project label differs from THIS
    # store's own project most likely recorded its context (if any) in that
    # project's own store. context_scope states that fact without probing any
    # other store's filesystem and without any allocation claim. Missing beats
    # wrong applies to the chip itself — every ambiguity stays "this_store":
    # - "other_project" requires ALL of:
    #   - store_scope == "project" (EXPLICIT, from the serving surface): a
    #     custom/global store receives every project's context, so the
    #     per-project-stores explanation would be false there by construction;
    #   - state unjoined AND ZERO sections in this store AND ZERO conflict-
    #     vetoed rows (a vetoed or section-bearing session has context HERE
    #     and must never claim otherwise — the veto reason is the honest
    #     display);
    #   - both labels known, neither the "~" home-dir label (home is not a
    #     project and the legacy home store is retired), and the labels
    #     differ case-insensitively (case-variant equality is an alias, not
    #     another project).
    # - "this_store": everything else.
    residual_labels = unlinked_context_project_labels or set()
    for entry in entries:
        join = entry["join"]
        entry_project = _optional_str(entry.get("project"))
        # Client-log evidence block (log_evidence.build_log_evidence_session_blocks):
        # counts every evidenced event present in this store for this session,
        # incl. plain events that never become work items. None means no
        # evidence — never a zero-context claim by itself.
        evidence_block = (log_evidence_by_key or {}).get(
            (
                entry["client"],
                entry["client_session_id"],
                _optional_str(entry.get("source_namespace_fingerprint")),
            )
        )
        join["client_log_evidence"] = evidence_block or None
        evidenced_count = int((evidence_block or {}).get("evidenced_event_count") or 0)
        join["context_scope"] = (
            "other_project"
            if (
                store_scope == "project"
                and join.get("state") == "unjoined"
                and int((entry["work"]["counts"] or {}).get("total") or 0) == 0
                and int(join.get("vetoed_rows") or 0) == 0
                # Evidenced context lives HERE by definition; the session can
                # never claim its context is in another project's store.
                and evidenced_count == 0
                and store_project_label is not None
                and store_project_label != "~"
                and entry_project is not None
                and entry_project != "~"
                and entry_project.casefold() != store_project_label.casefold()
            )
            else "this_store"
        )
        # Residual chip (codex-only by decision: the one client that cannot
        # self-identify — do not widen silently): this store HAS project-level
        # MCP context matching the session's project, but nothing ties it to
        # this specific session. Never fires on evidence, vetoes, sections, or
        # cross-store rows; never implies allocation.
        join["project_level_context_only"] = bool(
            entry.get("client") == "codex"
            and join.get("state") == "unjoined"
            and int(join.get("vetoed_rows") or 0) == 0
            and evidenced_count == 0
            and join["context_scope"] == "this_store"
            and int((entry["work"]["counts"] or {}).get("total") or 0) == 0
            and entry_project is not None
            and entry_project != "~"
            and entry_project.casefold() in residual_labels
        )

    entries.sort(
        key=lambda entry: (entry.get("last_activity_at") is not None, entry.get("last_activity_at") or 0.0),
        reverse=True,
    )

    kind_counts = Counter(str(entry.get("session_kind")) for entry in entries if entry.get("session_kind"))
    priced_entries = [
        entry["usage"]["estimated_cost_usd"] for entry in entries if entry["usage"]["estimated_cost_usd"] is not None
    ]
    summary = {
        "total_sessions": len(entries),
        "sessions_with_usage": sum(1 for entry in entries if int(entry["usage"]["rows"] or 0) > 0),
        "sessions_with_mechanical_activity": sum(
            1 for entry in entries if int((entry.get("mechanical_capture") or {}).get("observation_count") or 0) > 0
        ),
        "sessions_with_sections_only": sum(1 for entry in entries if entry["join"]["state"] == "sections_only"),
        "mechanical_namespace_join_refusals": namespace_join_refusals,
        "local_observation_namespace_join_refusals": local_namespace_join_refusals,
        "parent_source_namespace_join_refusals": parent_source_namespace_join_refusals,
        "local_observation_work_namespace_join_refusals": (
            local_observation_work_namespace_join_refusals
        ),
        "local_observation_mechanical_namespace_join_refusals": (
            local_observation_mechanical_namespace_join_refusals
        ),
        "sessions_with_local_client_observation": sum(
            1
            for entry in entries
            if int((entry.get("local_client_observation") or {}).get("observation_count") or 0) > 0
        ),
        "work_namespace_join_refusals": namespace_work_join_refusals,
        "usage_namespace_collision_sessions": usage_namespace_collision_sessions,
        "usage_namespace_collision_rows": usage_namespace_collision_rows,
        "root_sessions": int(kind_counts.get("root", 0)),
        "child_sessions": int(kind_counts.get("child", 0)),
        "internal_sessions": int(kind_counts.get("internal", 0)),
        "attributed_sessions": sum(1 for entry in entries if entry["join"]["state"] == "attributed"),
        "unassigned_work_items": len(unassigned_items),
        "unassigned_work_item_refs": [str(item.get("work_id") or "") for item in unassigned_items[:20]],
        "totals": {
            "fresh_tokens": sum(int(entry["usage"]["fresh_tokens"] or 0) for entry in entries),
            "cache_creation_tokens": sum(int(entry["usage"]["cache_creation_tokens"] or 0) for entry in entries),
            "cache_read_tokens": sum(int(entry["usage"]["cache_read_tokens"] or 0) for entry in entries),
            "total_tokens": sum(int(entry["usage"]["total_tokens"] or 0) for entry in entries),
            "estimated_cost_usd": sum(priced_entries) if priced_entries else None,
        },
        # Additive (instrumentation markers): schema stays v1 — every key
        # below is new, nothing existing changed (same precedent as the v2
        # work-ledger additive notes in api.py endpoint docstrings).
        "instrumentation": _instrumentation_rollup_summary(entries, instrumentation_markers),
    }
    return {"schema_version": SESSION_ROLLUP_SCHEMA_VERSION, "sessions": entries, "summary": summary}


def _apply_instrumentation_states(entries: list[dict[str, Any]], instrumentation_markers: dict[str, Any] | None) -> None:
    """Stamp instrumentation_state / instrumentation_state_basis on every entry.

    Per-entry rules (per-client isolation — a claude-code marker never
    classifies a codex session):
    - no marker for the entry's client -> "unknown" (basis None);
    - marker but no first_activity_at -> "unknown" (basis None);
    - first_activity_at <  marker -> "pre_instrumentation";
    - first_activity_at >= marker -> "post_instrumentation" (boundary equality
      goes to post, the non-flattering direction for the context KPI).
    Children then inherit the ROOT's state (walk client-scoped parent links to
    the topmost entry that exists; basis "inherited_from_root") — a subagent of
    a pre-install session is pre-install even if it spawned after the marker.
    Orphan children (parent referenced but absent as an entry) keep their
    own-time classification. The "unknown -> basis None" contract holds for
    children too: inheriting "unknown" from the root never stamps a basis.
    """

    earliest_by_client = (instrumentation_markers or {}).get("earliest_by_client")
    earliest_by_client = earliest_by_client if isinstance(earliest_by_client, dict) else {}
    entry_by_key: dict[tuple[str, str], dict[str, Any]] = {
        (entry["client"], entry["client_session_id"]): entry for entry in entries
    }
    for entry in entries:
        marker_at = _safe_optional_float(earliest_by_client.get(entry["client"]))
        first_activity_at = _safe_optional_float(entry.get("first_activity_at"))
        entry["instrumentation_installed_at"] = marker_at
        if marker_at is None or first_activity_at is None:
            entry["instrumentation_state"] = "unknown"
            entry["instrumentation_state_basis"] = None
        else:
            entry["instrumentation_state"] = (
                "pre_instrumentation" if first_activity_at < marker_at else "post_instrumentation"
            )
            entry["instrumentation_state_basis"] = "session_start_vs_marker"
    for entry in entries:
        root = entry
        seen: set[tuple[str, str]] = {(entry["client"], entry["client_session_id"])}
        while True:
            parent = (root.get("related") or {}).get("parent")
            if not isinstance(parent, dict):
                break
            parent_key = (root["client"], str(parent.get("client_session_id") or ""))
            if parent_key in seen or parent_key not in entry_by_key:
                # Cycle guard / orphan boundary: inherit from the topmost
                # ancestor that actually exists as an entry.
                break
            seen.add(parent_key)
            root = entry_by_key[parent_key]
        if root is not entry:
            entry["instrumentation_state"] = root["instrumentation_state"]
            entry["instrumentation_state_basis"] = (
                "inherited_from_root" if root["instrumentation_state"] != "unknown" else None
            )


def _instrumentation_rollup_summary(entries: list[dict[str, Any]], instrumentation_markers: dict[str, Any] | None) -> dict[str, Any]:
    """Additive rollup-summary block: marker facts + post-install context KPI.

    "With context" = the session shows ANY recorded MCP work context: a join
    state other than unjoined, at least one section, or client-log-evidenced
    events — presence of context, never an allocation claim. The KPI
    denominator is top-level sessions only (child/internal sessions record
    context through their root). When no marker exists anywhere the KPI is
    ABSENT (context_rate None, no client rows) — never a fake 100%.
    """

    markers = (instrumentation_markers or {}).get("markers")
    markers = markers if isinstance(markers, list) else []
    earliest_by_client = (instrumentation_markers or {}).get("earliest_by_client")
    earliest_by_client = earliest_by_client if isinstance(earliest_by_client, dict) else {}
    state_counts = Counter(str(entry.get("instrumentation_state") or "unknown") for entry in entries)

    def _is_top_level(entry: dict[str, Any]) -> bool:
        return str(entry.get("session_kind") or "") not in {"child", "internal"}

    def _has_context(entry: dict[str, Any]) -> bool:
        join = entry.get("join") if isinstance(entry.get("join"), dict) else {}
        if str(join.get("state") or "") != "unjoined":
            return True
        if int(((entry.get("work") or {}).get("counts") or {}).get("total") or 0) > 0:
            return True
        evidence = join.get("client_log_evidence") if isinstance(join.get("client_log_evidence"), dict) else {}
        return int(evidence.get("evidenced_event_count") or 0) > 0

    client_rows: list[dict[str, Any]] = []
    total_post = 0
    total_post_with_context = 0
    for client in sorted(earliest_by_client):
        post_entries = [
            entry
            for entry in entries
            if entry.get("client") == client
            and _is_top_level(entry)
            and str(entry.get("instrumentation_state") or "") == "post_instrumentation"
        ]
        post_with_context = sum(1 for entry in post_entries if _has_context(entry))
        total_post += len(post_entries)
        total_post_with_context += post_with_context
        client_rows.append(
            {
                "client": client,
                "installed_at": earliest_by_client[client],
                "post_sessions": len(post_entries),
                "post_with_context": post_with_context,
                "context_rate": (post_with_context / len(post_entries)) if post_entries else None,
            }
        )
    return {
        "markers_by_client": {
            client: {
                "installed_at": installed_at,
                "marker_count": sum(1 for marker in markers if marker.get("client") == client),
            }
            for client, installed_at in sorted(earliest_by_client.items())
        },
        # Marker-typed events that failed provenance/plausibility validation:
        # they classify nothing, but the rejection is counted, never silent.
        "invalid_marker_count": int((instrumentation_markers or {}).get("invalid_marker_count") or 0),
        "pre_instrumentation_sessions": int(state_counts.get("pre_instrumentation", 0)),
        "post_instrumentation_sessions": int(state_counts.get("post_instrumentation", 0)),
        "unknown_sessions": int(state_counts.get("unknown", 0)),
        "post_context_kpi": {
            "clients": client_rows,
            "post_sessions": total_post,
            "post_with_context": total_post_with_context,
            "context_rate": (total_post_with_context / total_post) if total_post else None,
        },
    }


def _session_rollup_entry(
    *,
    client: str,
    session_id: str,
    rows: list[dict[str, Any]],
    items: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    local_observations: list[dict[str, Any]],
    attribution_by_usage: dict[str, dict[str, Any]],
    item_by_work_id: dict[str, dict[str, Any]],
    allow_legacy_unscoped_namespace: bool,
) -> dict[str, Any]:
    all_observations = [*observations, *local_observations]
    parent_ids = {
        value
        for value in (
            [_optional_str(row.get("parent_client_session_id")) for row in rows]
            + [_optional_str(row.get("parent_client_session_id")) for row in all_observations]
        )
        if value
    }
    observation_parent_conflict = any(
        str(observation.get("parent_state") or "") == "conflicting" for observation in all_observations
    )
    if observation_parent_conflict:
        parent: dict[str, Any] | None = None
        parent_note = "conflicting parent pointers"
    elif len(parent_ids) == 1 and next(iter(parent_ids)) == session_id:
        # A session cannot be its own parent. Corrupt/hostile client data:
        # drop the pointer (missing beats wrong) so the session renders as a
        # plain top-level row and never duplicates its OWN usage as a
        # "descendants" subtotal.
        parent: dict[str, Any] | None = None
        parent_note = "self-referencing parent pointer ignored"
    elif len(parent_ids) == 1:
        parent = {"client_session_id": next(iter(parent_ids)), "label": None}
        parent_note = None
    elif parent_ids:
        # Rows disagree about the parent: missing beats wrong.
        parent = None
        parent_note = "conflicting parent pointers"
    else:
        parent = None
        parent_note = None

    kind_counts = Counter(value for value in (_optional_str(row.get("client_session_kind")) for row in rows) if value)
    if not kind_counts:
        kind_counts.update(
            value for value in (_optional_str(row.get("session_kind")) for row in all_observations) if value
        )
    session_kind = kind_counts.most_common(1)[0][0] if kind_counts else None

    # Label selection is two-step (Phase 2.6 review fix): the majority LABEL
    # wins on label-only counts — a label whose rows split between
    # worktree-remapped and plain paths must never lose to a less common
    # rival just because its votes were divided across sources. THEN the
    # majority source WITHIN the winning label decides the worktree marker,
    # and a source tie KEEPS the "claude_worktree" marker (half the session's
    # evidence being worktree-recorded is enough to show the chip).
    labeled_pairs = [
        (str(label), _optional_str(source))
        for label, source in (
            [(row.get("project_dir"), row.get("project_source")) for row in rows]
            + [(item.get("project_dir"), item.get("project_source")) for item in items]
            + [(row.get("project"), row.get("project_source")) for row in all_observations]
        )
        if _optional_str(label)
    ]
    if labeled_pairs:
        label_counts = Counter(label for label, _source in labeled_pairs)
        project = label_counts.most_common(1)[0][0]
        source_counts = Counter(source for label, source in labeled_pairs if label == project)
        top_count = max(source_counts.values())
        top_sources = {source for source, count in source_counts.items() if count == top_count}
        project_source = (
            "claude_worktree" if "claude_worktree" in top_sources else source_counts.most_common(1)[0][0]
        )
    else:
        project, project_source = None, None

    project_identities = {
        identity
        for row in [*rows, *items, *all_observations]
        if (identity := _optional_str(row.get("project_identity")))
    }
    project_identity = next(iter(project_identities)) if len(project_identities) == 1 else None
    project_identity_state = (
        "conflicting"
        if len(project_identities) > 1
        else "explicit"
        if project_identity is not None
        else "serving_project_store"
        if project_source == "serving_project_store"
        else "missing"
    )

    session_titles = [
        title
        for title in (
            _optional_str(row.get("client_session_title"))
            for row in [*rows, *local_observations]
        )
        if title
    ]
    client_session_title = Counter(session_titles).most_common(1)[0][0] if session_titles else None

    source_namespace_fingerprints = {
        value
        for row in [*rows, *local_observations]
        if (value := _optional_str(row.get("source_namespace_fingerprint")))
    }
    source_namespace_fingerprint = (
        next(iter(source_namespace_fingerprints))
        if len(source_namespace_fingerprints) == 1
        else None
    )
    parent_source_namespace_fingerprints = {
        value
        for row in [*rows, *local_observations]
        if (value := _optional_str(row.get("parent_source_namespace_fingerprint")))
    }
    parent_source_namespace_fingerprint = (
        next(iter(parent_source_namespace_fingerprints))
        if len(parent_source_namespace_fingerprints) == 1
        else None
    )

    first_candidates: list[float] = []
    last_candidates: list[float] = []
    row_time_sources: set[str] = set()
    for row in rows:
        occurred = _safe_optional_float(row.get("occurred_at"))
        started = _safe_optional_float(row.get("started_at"))
        first = started if started is not None else occurred
        if first is not None:
            first_candidates.append(first)
        if occurred is not None:
            last_candidates.append(occurred)
        source = str(row.get("time_source") or "")
        row_time_sources.add("client_metadata" if source.startswith("metadata.") else "import_time_fallback")
    for item in items:
        item_started = _safe_optional_float(item.get("started_at"))
        item_updated = _safe_optional_float(item.get("updated_at"))
        if item_started is not None:
            first_candidates.append(item_started)
        if item_updated is not None:
            last_candidates.append(item_updated)
    activity_sources = set(row_time_sources)
    if items and any(
        _safe_optional_float(item.get(field)) is not None
        for item in items
        for field in ("started_at", "updated_at")
    ):
        activity_sources.add("mcp_event_time")
    for observation in all_observations:
        observed_first = _safe_optional_float(observation.get("first_activity_at"))
        observed_last = _safe_optional_float(observation.get("last_activity_at"))
        if observed_first is not None:
            first_candidates.append(observed_first)
        if observed_last is not None:
            last_candidates.append(observed_last)
        if observed_first is not None or observed_last is not None:
            observation_basis = str(observation.get("activity_time_basis") or "host_event_time")
            if observation.get("observation_channel") == "local_client_log":
                activity_sources.add("local_client_metadata")
            else:
                activity_sources.add(
                    "client_hook_capture_time"
                    if observation_basis == "capture_observed"
                    else "mixed"
                    if observation_basis == "mixed"
                    else "client_hook_event_time"
                )
    activity_time_source = (
        next(iter(activity_sources)) if len(activity_sources) == 1 else "mixed" if activity_sources else None
    )

    first_activity_at = min(first_candidates) if first_candidates else None
    last_activity_at = max(last_candidates) if last_candidates else None

    observed_models = _ordered_unique_text(
        model
        for observation in all_observations
        for model in (
            observation.get("observed_models")
            if isinstance(observation.get("observed_models"), list)
            else []
        )
    )
    mechanical_models = _ordered_unique_text(
        model
        for observation in observations
        for model in (
            observation.get("observed_models")
            if isinstance(observation.get("observed_models"), list)
            else []
        )
    )
    local_models = _ordered_unique_text(
        model
        for observation in local_observations
        for model in (
            observation.get("observed_models")
            if isinstance(observation.get("observed_models"), list)
            else []
        )
    )
    observation_time_bases = {
        str(observation.get("activity_time_basis") or "host_event_time") for observation in observations
    }
    namespace_fingerprints = {
        value
        for row in [*rows, *items, *observations]
        if (
            value := _optional_str(
                row.get("namespace_fingerprint") or row.get("session_namespace_fingerprint")
            )
        )
    }
    namespace_fingerprint = next(iter(namespace_fingerprints)) if len(namespace_fingerprints) == 1 else None
    explicit_identity_scope = bool(namespace_fingerprints) or any(
        str(row.get("identity_scope_state") or "") == "explicit"
        for row in [*rows, *items, *observations]
    )
    mechanical_capture = {
        "observation_count": sum(int(observation.get("observation_count") or 0) for observation in observations),
        "event_types": _ordered_unique_text(
            event_type
            for observation in observations
            for event_type in (observation.get("event_types") if isinstance(observation.get("event_types"), list) else [])
        ),
        "host_events": _ordered_unique_text(
            event_type
            for observation in observations
            for event_type in (observation.get("host_events") if isinstance(observation.get("host_events"), list) else [])
        ),
        "models": mechanical_models,
        "source_instances": _ordered_unique_text(
            source_instance
            for observation in observations
            for source_instance in (
                observation.get("source_instances") if isinstance(observation.get("source_instances"), list) else []
            )
        ),
        "first_observed_at": min(
            (value for row in observations if (value := _safe_optional_float(row.get("first_activity_at"))) is not None),
            default=None,
        ),
        "last_observed_at": max(
            (value for row in observations if (value := _safe_optional_float(row.get("last_activity_at"))) is not None),
            default=None,
        ),
        "measurement_basis": "client_hook_observed" if observations else None,
        "time_basis": (
            next(iter(observation_time_bases))
            if len(observation_time_bases) == 1
            else "mixed"
            if observation_time_bases
            else None
        ),
        "missing_host_timestamp_count": sum(
            int(observation.get("missing_host_timestamp_count") or 0) for observation in observations
        ),
    }
    local_client_observation = {
        "observation_count": sum(
            int(observation.get("observation_count") or 0)
            for observation in local_observations
        ),
        "models": local_models,
        "source_event_ids": _ordered_unique_text(
            source_event_id
            for observation in local_observations
            if (source_event_id := _optional_str(observation.get("source_event_id")))
        ),
        "first_observed_at": min(
            (
                value
                for row in local_observations
                if (value := _safe_optional_float(row.get("first_activity_at"))) is not None
            ),
            default=None,
        ),
        "last_observed_at": max(
            (
                value
                for row in local_observations
                if (value := _safe_optional_float(row.get("last_activity_at"))) is not None
            ),
            default=None,
        ),
        "measurement_basis": (
            "local_client_log_observed" if local_observations else None
        ),
    }
    usage_summary = _session_usage_summary(rows)
    if not rows:
        usage_note = "usage unknown for this session — no imported usage rows matched; this is not a zero-cost claim"
    elif int(usage_summary.get("excluded_non_additive_rows") or 0):
        usage_note = (
            "Codex cumulative usage for this descendant session is preserved as raw evidence but excluded from totals "
            "until its parent replay baseline can be proven; this is not a zero-usage or zero-cost claim"
        )
    else:
        usage_note = None

    return {
        "session_key": f"{client}::{session_id}",
        "client": client,
        "client_session_id": session_id,
        "client_session_id_short": None,  # collision-aware label pass fills this
        "session_kind": session_kind,
        "client_session_title": client_session_title,
        "project": project,
        "project_source": project_source,
        "project_identity": project_identity,
        "project_identity_state": project_identity_state,
        "observed_models": observed_models,
        "namespace_fingerprint": namespace_fingerprint,
        "source_namespace_fingerprint": source_namespace_fingerprint,
        "parent_source_namespace_fingerprint": parent_source_namespace_fingerprint,
        "identity_scope_state": (
            "explicit"
            if explicit_identity_scope
            else "unscoped"
            if rows or items or observations or local_observations
            else "unknown"
        ),
        # TaskProjection consumes this additive policy bit defensively when a
        # caller passes work items directly. Only a project-scoped store may
        # use its filesystem boundary to bridge one legacy unscoped side.
        "allow_legacy_unscoped_namespace_join": allow_legacy_unscoped_namespace,
        "first_activity_at": first_activity_at,
        "last_activity_at": last_activity_at,
        # Additive (Phase 3.5c): activity span derived from the entry's OWN
        # first/last activity, None whenever the span cannot be an honest
        # wall-clock claim (see _session_duration_seconds).
        "duration_seconds": _session_duration_seconds(first_activity_at, last_activity_at),
        "activity_time_source": activity_time_source,
        "usage": usage_summary,
        "usage_note": usage_note,
        "join": _session_join_summary(
            [row for row in rows if row.get("usage_additive") is not False],
            items,
            attribution_by_usage,
            item_by_work_id,
        ),
        "work": _session_work_summary(items),
        "mechanical_capture": mechanical_capture,
        "local_client_observation": local_client_observation,
        "related": {
            "parent": parent,
            "note": parent_note,
            "child_session_count": 0,
            "child_session_labels": [],
            "children_usage": None,
            "relationship_source": (
                "mixed"
                if sum(bool(value) for value in (rows, observations, local_observations)) > 1
                else "client_hook_observed"
                if observations
                else "local_client_log_observed"
                if local_observations
                else "importer_recorded_parent_id"
            ),
        },
        "rollup_group_key": None,  # filled after the children pass
    }


def _session_duration_seconds(first_activity_at: float | None, last_activity_at: float | None) -> float | None:
    """``last_activity_at - first_activity_at``, or None when the span cannot
    be an honest wall-clock claim — missing beats wrong.

    Guards (the existing bad-timestamp tolerance, shared with
    usage_cube.usage_bucket_date and the dashboard time formatting):
    - both endpoints must exist;
    - both must be real calendar timestamps — client-authored times can be
      absurd-but-finite (ms epoch, year 99999, 1e300) and a duration derived
      from one would be a wrong claim;
    - the difference must be >= 0 (a negative span proves at least one
      endpoint is wrong). A true zero-length span stays 0.0, which is real
      data, not a guess.
    """

    if first_activity_at is None or last_activity_at is None:
        return None
    if usage_bucket_date(first_activity_at) is None or usage_bucket_date(last_activity_at) is None:
        return None
    duration = float(last_activity_at) - float(first_activity_at)
    return duration if duration >= 0 else None


def _token_reporting_status(
    reported_rows: int, unreported_rows: int, unknown_rows: int = 0
) -> str | None:
    """Collapse row-level capability without changing numeric token sums."""

    if reported_rows and (unreported_rows or unknown_rows):
        return "partial"
    if unknown_rows:
        return "unknown"
    if reported_rows:
        return "reported"
    if unreported_rows:
        return "not_reported"
    return None


def _session_usage_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    all_rows = rows
    identity_models = _ordered_unique_text(_optional_str(row.get("model")) for row in all_rows)
    rows = [row for row in all_rows if row.get("usage_additive") is not False]
    excluded_non_additive_rows = len(all_rows) - len(rows)
    priced = [value for value in (_safe_optional_float(row.get("estimated_cost_usd")) for row in rows) if value is not None]
    # Additive (Phase 3.5c): sum of the importer-recorded per-row turn
    # counts over this entry's OWN rows only — children are never merged
    # (same rule as tokens). None (never 0) when no row carries a count,
    # matching the priced-cost pattern above.
    turn_counts = [value for value in (_safe_optional_turn_count(row.get("turn_count")) for row in rows) if value is not None]
    lanes: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    for row in rows:
        lane_key = (_optional_str(row.get("model")), _optional_str(row.get("usage_row_lane")))
        lane = lanes.setdefault(
            lane_key,
            {
                "model": lane_key[0],
                "lane": lane_key[1],
                "rows": 0,
                "fresh_tokens": 0,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_reported_rows": 0,
                "cache_creation_unreported_rows": 0,
                "cache_creation_unknown_rows": 0,
                "cache_read_reported_rows": 0,
                "cache_read_unreported_rows": 0,
                "cache_read_unknown_rows": 0,
                "total_tokens": 0,
                "estimated_cost_usd": None,
            },
        )
        lane["rows"] += 1
        lane["fresh_tokens"] += int(row.get("fresh_tokens") or 0)
        lane["cache_creation_tokens"] += int(row.get("cache_creation_tokens") or 0)
        lane["cache_read_tokens"] += int(row.get("cache_read_tokens") or 0)
        lane[
            "cache_creation_reported_rows"
            if row.get("cache_creation_tokens_reported") is True
            else "cache_creation_unreported_rows"
            if row.get("cache_creation_tokens_reported") is False
            else "cache_creation_unknown_rows"
        ] += 1
        lane[
            "cache_read_reported_rows"
            if row.get("cache_read_tokens_reported") is True
            else "cache_read_unreported_rows"
            if row.get("cache_read_tokens_reported") is False
            else "cache_read_unknown_rows"
        ] += 1
        lane["total_tokens"] += int(row.get("total_tokens") or 0)
        cost = _safe_optional_float(row.get("estimated_cost_usd"))
        if cost is not None:
            lane["estimated_cost_usd"] = float(lane["estimated_cost_usd"] or 0.0) + cost
    cache_creation_reported_rows = sum(
        1 for row in rows if row.get("cache_creation_tokens_reported") is True
    )
    cache_creation_unreported_rows = sum(
        1 for row in rows if row.get("cache_creation_tokens_reported") is False
    )
    cache_creation_unknown_rows = sum(
        1 for row in rows if row.get("cache_creation_tokens_reported") is None
    )
    cache_read_reported_rows = sum(
        1 for row in rows if row.get("cache_read_tokens_reported") is True
    )
    cache_read_unreported_rows = sum(
        1 for row in rows if row.get("cache_read_tokens_reported") is False
    )
    cache_read_unknown_rows = sum(
        1 for row in rows if row.get("cache_read_tokens_reported") is None
    )
    for lane in lanes.values():
        lane["cache_creation_reporting"] = _token_reporting_status(
            lane["cache_creation_reported_rows"],
            lane["cache_creation_unreported_rows"],
            lane["cache_creation_unknown_rows"],
        )
        lane["cache_read_reporting"] = _token_reporting_status(
            lane["cache_read_reported_rows"],
            lane["cache_read_unreported_rows"],
            lane["cache_read_unknown_rows"],
        )
    return {
        "rows": len(all_rows),
        "additive_rows": len(rows),
        "excluded_non_additive_rows": excluded_non_additive_rows,
        "identity_models": identity_models,
        "priced_rows": len(priced),
        "unpriced_rows": len(rows) - len(priced),
        "fresh_tokens": _sum_row_key(rows, "fresh_tokens"),
        "cache_creation_tokens": _sum_row_key(rows, "cache_creation_tokens"),
        "cache_read_tokens": _sum_row_key(rows, "cache_read_tokens"),
        "total_tokens": _sum_row_key(rows, "total_tokens"),
        "cache_creation_reporting": _token_reporting_status(
            cache_creation_reported_rows, cache_creation_unreported_rows, cache_creation_unknown_rows
        ),
        "cache_creation_reported_rows": cache_creation_reported_rows,
        "cache_creation_unreported_rows": cache_creation_unreported_rows,
        "cache_creation_unknown_rows": cache_creation_unknown_rows,
        "cache_read_reporting": _token_reporting_status(
            cache_read_reported_rows, cache_read_unreported_rows, cache_read_unknown_rows
        ),
        "cache_read_reported_rows": cache_read_reported_rows,
        "cache_read_unreported_rows": cache_read_unreported_rows,
        "cache_read_unknown_rows": cache_read_unknown_rows,
        "turns_total": sum(turn_counts) if turn_counts else None,
        "estimated_cost_usd": sum(priced) if priced else None,
        "cost_confidence": _single_or_mixed(row.get("cost_confidence") for row in rows),
        "usage_confidence": _single_or_mixed(row.get("usage_confidence") for row in rows),
        "model_lanes": sorted(
            lanes.values(),
            key=lambda lane: (-int(lane["total_tokens"] or 0), str(lane["model"] or ""), str(lane["lane"] or "")),
        ),
    }


_SESSION_JOIN_STATE_RANK = {"attributed": 3, "ambiguous": 2, "context_matched_unallocated": 1, "unjoined": 0}


def _session_join_summary(
    rows: list[dict[str, Any]],
    items: list[dict[str, Any]],
    attribution_by_usage: dict[str, dict[str, Any]],
    item_by_work_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    row_states = {"attributed": 0, "ambiguous": 0, "context_matched_unallocated": 0, "unjoined": 0}
    attributed_fresh = 0
    attributed_total = 0
    attributed_work: list[dict[str, Any]] = []
    seen_decisions: set[tuple[str, str, str]] = set()
    ambiguous_candidates: list[str] = []
    vetoed_rows = 0
    best: tuple[tuple[int, int], dict[str, Any]] | None = None
    for row in rows:
        attr = attribution_by_usage.get(str(row.get("usage_event_id") or ""))
        if attr is not None and attr.get("join_vetoes"):
            # Conflicting evidence in THIS store vetoed a join for this row:
            # the session demonstrably has related context here, so displays
            # must never claim its context lives in another project's store.
            vetoed_rows += 1
        if attr is None:
            row_state = "unjoined"
        elif _is_attributed_attribution(attr):
            row_state = "attributed"
        elif _is_ambiguous_attribution(attr):
            row_state = "ambiguous"
        elif _is_context_matched_unallocated_attribution(attr):
            row_state = "context_matched_unallocated"
        else:
            row_state = "unjoined"
        row_states[row_state] += 1
        if attr is not None:
            rank = (
                _SESSION_JOIN_STATE_RANK[row_state],
                _JOIN_RANK.get(str(attr.get("join_confidence") or "unjoined"), 0),
            )
            if best is None or rank > best[0]:
                best = (rank, attr)
        if attr is not None and row_state == "attributed":
            attributed_fresh += int(attr.get("usage_fresh_tokens") or 0)
            attributed_total += int(attr.get("usage_tokens") or 0)
            marker = (str(attr.get("work_id")), str(attr.get("join_strategy")), str(attr.get("join_confidence")))
            if marker not in seen_decisions:
                seen_decisions.add(marker)
                linked_item = item_by_work_id.get(str(attr.get("work_id") or ""))
                attributed_work.append(
                    {
                        "work_id": attr.get("work_id"),
                        "section_id": attr.get("section_id"),
                        "title": (linked_item or {}).get("title"),
                        "join_strategy": attr.get("join_strategy"),
                        "join_confidence": attr.get("join_confidence"),
                    }
                )
        if attr is not None and row_state == "ambiguous":
            for candidate_id in attr.get("ambiguous_candidate_work_ids") or []:
                if candidate_id and str(candidate_id) not in ambiguous_candidates:
                    ambiguous_candidates.append(str(candidate_id))
    # State machine (derived verbatim from the canonical decisions):
    # - context_only requires an ACTUAL non-vetoed context match
    #   (row_states["context_matched_unallocated"] > 0). A session whose
    #   sections and rows coexist but where every row is canonically unjoined
    #   (e.g. a transcript-conflict VETO) is `unjoined` — the veto is the
    #   canonical "no match at all", and the chip must never claim a match
    #   the matcher refused. The honest reason (the veto text when a veto
    #   occurred) travels below as the best row's join_reason.
    if row_states["attributed"]:
        state = "attributed"
    elif row_states["ambiguous"]:
        state = "ambiguous"
    elif row_states["context_matched_unallocated"]:
        state = "context_only"
    elif rows:
        state = "unjoined"
    elif items:
        state = "sections_only"
    else:
        state = "unjoined"
    if state == "sections_only":
        reason = None
        for item in items:
            explanation = item.get("join_explanation") if isinstance(item.get("join_explanation"), dict) else {}
            reason = _optional_str(explanation.get("join_reason"))
            if reason:
                break
    else:
        reason = _optional_str(best[1].get("join_reason")) if best is not None else None
    return {
        "state": state,
        "reason": reason,
        "row_states": row_states,
        "vetoed_rows": vetoed_rows,
        "attributed_fresh_tokens": attributed_fresh,
        "attributed_total_tokens": attributed_total,
        "attributed_work": attributed_work,
        "ambiguous_candidate_work_ids": ambiguous_candidates,
    }


def _session_work_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"total": len(items), "completed": 0, "resolved": 0, "active": 0, "blocked": 0}
    evidence = {"strong": 0, "weak": 0, "failed": 0, "none": 0}
    listed: list[dict[str, Any]] = []
    for item in items:
        status = str(item.get("latest_status") or "")
        if status == "completed":
            counts["completed"] += 1
        elif status == "resolved":
            counts["resolved"] += 1
        elif status == "blocked":
            counts["blocked"] += 1
        else:
            counts["active"] += 1
        evidence_status = str(item.get("evidence_status") or "none")
        evidence[evidence_status if evidence_status in evidence else "none"] += 1
        listed.append(
            {
                "work_id": item.get("work_id"),
                "section_id": item.get("section_id"),
                "title": item.get("title"),
                "latest_status": item.get("latest_status"),
                "evidence_status": item.get("evidence_status"),
                # Display flag only (id-free): full claimed ids stay on the
                # ledger work_items JSON, matching the candidate_work_ids
                # precedent. The bool preserves the detail-chip presence test;
                # conflicting_keys (id-free key NAMES only) lets the chip name
                # the key that actually conflicted (session/transcript/client).
                "log_evidence_conflict": bool(item.get("log_evidence_conflict")),
                "log_evidence_conflict_keys": _log_evidence_conflict_keys(item.get("log_evidence_conflict")),
            }
        )
    return {"items": listed, "counts": counts, "evidence": evidence}


def _log_evidence_conflict_keys(conflict: Any) -> list[str]:
    """Id-free key names that conflicted with the client's own log, or []."""

    if isinstance(conflict, dict):
        keys = conflict.get("conflicting_keys")
        if isinstance(keys, (list, tuple)):
            return [str(key) for key in keys]
    return []


def _session_label_component(component: str) -> str:
    text = component.removeprefix("agent-") or component
    return text[:8]


def _base_session_display_label(client: Any, session_id: str | None) -> str | None:
    """8-char display label, component-wise for structured child ids.

    Plain ids keep the first 8 chars. Structured ids ('<base>:<suffix>') keep
    8 chars per component with the 'agent-' prefix stripped, so the 409
    children of one root do not all render as the root's first 8 chars.
    Hermes ids are date-prefixed ('20260617_140948_ad14a7'): the distinctive
    part is the tail, so hermes labels keep the LAST 8 chars instead.
    """

    if session_id is None:
        return None
    if str(client or "") == "hermes":
        return session_id[-8:]
    parts = [part for part in session_id.split(":") if part]
    if not parts:
        return session_id[:8]
    if str(client or "") == "codex" and len(parts) == 1:
        # Codex ids are UUIDv7: the first 8 hex chars encode the launch time
        # (~65 s resolution), so same-minute subagent siblings always share
        # them. The distinctive part is the random tail — same rule as hermes.
        return session_id[-8:]
    return ":".join(_session_label_component(part) for part in parts)


def _assign_session_display_labels(keys: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """Collision-safe display labels: identical truncations get a deterministic suffix.

    Two DIFFERENT sessions must never render the same label (display honesty);
    the suffix is a short hash of the full key, so no full id ever leaks.
    Takes (client, client_session_id) keys so EVERY label consumer — rollup
    rows, parent references, attention example refs — routes through the one
    collision-aware assigner instead of re-deriving a bare truncation.
    """

    base: dict[tuple[str, str], str] = {}
    for raw_key in keys:
        key = (str(raw_key[0]), str(raw_key[1]))
        base[key] = _base_session_display_label(key[0], key[1]) or "unknown"
    counts = Counter(base.values())
    labels: dict[tuple[str, str], str] = {}
    used: set[str] = set()
    for key, label in base.items():
        if counts[label] > 1:
            digest = sha256(f"{key[0]}::{key[1]}".encode("utf-8")).hexdigest()[:4]
            label = f"{label}~{digest}"
        while label in used:  # deterministic final fallback; practically unreachable
            label += "+"
        used.add(label)
        labels[key] = label
    return labels


def _single_or_mixed(values: Any) -> str | None:
    distinct = {str(value) for value in values if value}
    if not distinct:
        return None
    if len(distinct) == 1:
        return next(iter(distinct))
    return "mixed"


def build_overview(
    *,
    usage_events: list[dict[str, Any]],
    work_items: list[dict[str, Any]],
    evidence_events: list[dict[str, Any]],
    attributions: list[dict[str, Any]],
    work_events: list[dict[str, Any]],
    usage_debug_events: list[dict[str, Any]],
    proxy_usage_events: list[dict[str, Any]] | None = None,
    join_inspector: dict[str, Any] | None = None,
    attention_items: list[dict[str, Any]] | None = None,
    attention_groups: dict[str, Any] | None = None,
) -> dict[str, Any]:
    proxy_usage_events = proxy_usage_events or []
    join_inspector = join_inspector or {}
    attention_items = attention_items or []
    attention_groups = attention_groups if isinstance(attention_groups, dict) else build_attention_groups(attention_items)
    usage_only = usage_events
    attributed = [attr for attr in attributions if attr.get("work_id") and attr.get("join_confidence") != "unjoined"]
    ambiguous = [attr for attr in attributions if _is_ambiguous_attribution(attr)]
    context_matched_unallocated = [attr for attr in attributions if _is_context_matched_unallocated_attribution(attr)]
    usage_without_context = [attr for attr in attributions if str(attr.get("join_strategy") or "") == "unjoined"]
    unattributed_count = len(ambiguous) + len(context_matched_unallocated) + len(usage_without_context)
    completed = [item for item in work_items if item.get("latest_status") == "completed"]
    evidence_backed = [item for item in completed if item.get("evidence_status") == "strong"]
    status_counts = _work_status_counts(work_items)
    return {
        # total_tokens keeps its historical everything-included meaning; the
        # cache-aware triple below is the honest split (fresh = input+output).
        "total_tokens": sum(int(event.get("total_tokens") or 0) for event in usage_only),
        "total_fresh_tokens": sum(int(event.get("fresh_tokens") or 0) for event in usage_only),
        "total_cache_read_tokens": sum(int(event.get("cache_read_tokens") or 0) for event in usage_only),
        "total_cache_creation_tokens": sum(int(event.get("cache_creation_tokens") or 0) for event in usage_only),
        "estimated_cost_total": sum(float(event.get("estimated_cost_usd") or 0.0) for event in usage_only),
        "usage_event_count": len(usage_only),
        "usage_truth_count": len(usage_only),
        "usage_truth_event_count": len(usage_only),
        "proxy_usage_event_count": len(proxy_usage_events),
        "proxy_estimated_cost_total": sum(float(event.get("estimated_cost_usd") or 0.0) for event in proxy_usage_events),
        "attributed_count": len(attributed),
        "ambiguous_count": len(ambiguous),
        "context_matched_unallocated_count": len(context_matched_unallocated),
        "usage_without_mcp_context_count": len(usage_without_context),
        "unattributed_count": unattributed_count,
        "attributed_usage_count": len(attributed),
        "context_matched_unallocated_usage_count": len(context_matched_unallocated),
        "unattributed_usage_count": unattributed_count,
        "ambiguous_usage_count": int(join_inspector.get("ambiguous_usage_count", 0) or 0),
        "unattributed_usage_percentage": _percent(unattributed_count, len(attributions)),
        "usage_without_mcp_context_percentage": _percent(len(usage_without_context), len(attributions)),
        "work_status_counts": status_counts,
        "active_work_items": status_counts["active"],
        "completed_work_items": status_counts["completed"],
        "resolved_work_items": status_counts["resolved"],
        "blocked_work_items": status_counts["blocked"],
        "evidence_backed_completion_rate": _percent(len(evidence_backed), len(completed)),
        "last_import_time": max((float(event.get("recorded_at") or 0.0) for event in usage_only if event.get("source_type") == "log_import"), default=None),
        "mcp_status": "active" if work_events or evidence_events else "no_mcp_events",
        "recent_mcp_events": len(work_events) + len(evidence_events),
        "usage_debug_event_count": len(usage_debug_events),
        "cost_confidence_breakdown": _confidence_breakdown(usage_only, "cost_confidence"),
        "usage_confidence_breakdown": _confidence_breakdown(usage_only, "usage_confidence"),
        "evidence_status_counts": dict(Counter(str(item.get("evidence_status") or "none") for item in work_items)),
        "completed_without_evidence": [item for item in completed if item.get("evidence_status") in {"none", "weak"}],
        "failed_evidence_count": sum(1 for event in evidence_events if event.get("result") in {"failed", "error"}),
        "attention_item_count": len(attention_items),
        # Headline number for display: causes needing attention, not one item
        # per flooded usage row. The raw item count stays alongside.
        "attention_group_count": len(attention_groups.get("groups") or []),
        "attention_counts": dict(Counter(str(item.get("attention_type") or "unknown") for item in attention_items)),
    }


def build_ledger_insights(
    *,
    usage_events: list[dict[str, Any]],
    work_items: list[dict[str, Any]],
    evidence_events: list[dict[str, Any]],
    usage_reconciliation: list[dict[str, Any]],
    attention_items: list[dict[str, Any]],
    overview: dict[str, Any],
) -> dict[str, Any]:
    """Convert the canonical work ledger into deterministic user-facing insights."""

    usage_summary = _usage_attribution_insight_summary(usage_reconciliation)
    trust_summary = _trust_insight_summary(work_items, evidence_events)
    blind_spots = _ledger_blind_spots(work_items, usage_reconciliation)
    top_next_actions = _top_next_actions(
        blind_spots=blind_spots,
        usage_events=usage_events,
        work_items=work_items,
        trust_summary=trust_summary,
    )
    health, health_reason = _ledger_health(
        usage_events=usage_events,
        work_items=work_items,
        blind_spots=blind_spots,
        trust_summary=trust_summary,
        attention_items=attention_items,
    )
    return {
        "ledger_health": health,
        "ledger_health_reason": health_reason,
        "usage_attribution_summary": usage_summary,
        "trust_summary": trust_summary,
        "blind_spots": blind_spots,
        "top_next_actions": top_next_actions,
        "what_sentinel_can_claim": [
            "Imported usage rows are local usage truth, not provider bills.",
            "MCP work is agent-reported meaning.",
            "Evidence-backed means machine-check evidence exists.",
            "Unattributed usage means agentacct cannot yet connect usage to work.",
        ],
        "source_overview": {
            "usage_truth_count": int(overview.get("usage_truth_count") or 0),
            "work_item_count": len(work_items),
            "attention_item_count": int(overview.get("attention_item_count") or 0),
            # Grouped headline: causes needing attention (raw item count above).
            "attention_group_count": int(overview.get("attention_group_count") or 0),
        },
    }


def build_timeline(
    *,
    usage_events: list[dict[str, Any]],
    work_events: list[dict[str, Any]],
    evidence_events: list[dict[str, Any]],
    attributions: list[dict[str, Any]],
    usage_debug_events: list[dict[str, Any]],
    proxy_usage_events: list[dict[str, Any]] | None = None,
    diagnostic_usage_events: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    proxy_usage_events = proxy_usage_events or []
    diagnostic_usage_events = diagnostic_usage_events or []
    attribution_by_usage = {str(attr.get("usage_event_id")): attr for attr in attributions if attr.get("usage_event_id")}
    entries: list[dict[str, Any]] = []
    for usage in usage_events:
        attr = attribution_by_usage.get(str(usage.get("usage_event_id") or ""))
        entries.append(
            {
                "time": usage.get("created_at"),
                "source": usage.get("source"),
                "source_type": usage.get("source_type"),
                "event_kind": "usage",
                "event_id": usage.get("usage_event_id"),
                # Additive (v2): client travels on usage-bearing timeline
                # entries so display-level grouping keys on data, not on
                # parsing the human title back apart.
                "client": usage.get("client"),
                "title": f"{usage.get('client') or usage.get('source') or 'Usage'} usage",
                "detail": _provider_model_label(usage.get("provider"), usage.get("model")),
                "tokens": usage.get("total_tokens"),
                "tokens_fresh": usage.get("fresh_tokens"),
                "tokens_cache_read": usage.get("cache_read_tokens"),
                "tokens_cache_creation": usage.get("cache_creation_tokens"),
                "estimated_cost_usd": usage.get("estimated_cost_usd"),
                "usage_confidence": usage.get("usage_confidence"),
                "cost_confidence": usage.get("cost_confidence"),
                "work_id": attr.get("work_id") if attr else None,
                "join_confidence": attr.get("join_confidence") if attr else "unjoined",
                "join_strategy": attr.get("join_strategy") if attr else "unjoined",
            }
        )
    for usage in proxy_usage_events:
        entries.append(
            {
                "time": usage.get("created_at"),
                "source": usage.get("source"),
                "source_type": usage.get("source_type"),
                "event_kind": "proxy_usage",
                "event_id": usage.get("usage_event_id"),
                "client": usage.get("client"),
                "title": "Proxy budget decision",
                "detail": _provider_model_label(usage.get("provider"), usage.get("model")),
                "tokens": usage.get("total_tokens"),
                "tokens_fresh": usage.get("fresh_tokens"),
                "tokens_cache_read": usage.get("cache_read_tokens"),
                "tokens_cache_creation": usage.get("cache_creation_tokens"),
                "estimated_cost_usd": usage.get("estimated_cost_usd"),
                "usage_confidence": usage.get("usage_confidence"),
                "cost_confidence": usage.get("cost_confidence"),
                "work_id": None,
                "join_confidence": "unjoined",
                "join_strategy": "unjoined",
            }
        )
    for usage in diagnostic_usage_events:
        entries.append(
            {
                "time": usage.get("created_at"),
                "source": usage.get("source"),
                "source_type": usage.get("source_type"),
                "event_kind": "usage_diagnostic",
                "event_id": usage.get("usage_event_id"),
                "client": usage.get("client"),
                "title": f"{usage.get('client') or usage.get('source') or 'Usage'} usage diagnostic",
                "detail": _provider_model_label(usage.get("provider"), usage.get("model")),
                "tokens": usage.get("total_tokens"),
                "tokens_fresh": usage.get("fresh_tokens"),
                "tokens_cache_read": usage.get("cache_read_tokens"),
                "tokens_cache_creation": usage.get("cache_creation_tokens"),
                "estimated_cost_usd": usage.get("estimated_cost_usd"),
                "usage_confidence": usage.get("usage_confidence"),
                "cost_confidence": usage.get("cost_confidence"),
                "work_id": None,
                "join_confidence": "unjoined",
                "join_strategy": "diagnostic_not_usage_truth",
            }
        )
    for work in work_events:
        entries.append(
            {
                "time": work.get("created_at"),
                "source": work.get("source"),
                "source_type": "mcp_agent_reported",
                "event_kind": "work",
                "event_id": work.get("event_id"),
                "title": work.get("title") or work.get("work_id"),
                "detail": work.get("summary") or work.get("status"),
                "tokens": None,
                "estimated_cost_usd": None,
                "work_id": work.get("work_id"),
                "status": work.get("status"),
                "join_confidence": "",
            }
        )
    for evidence in evidence_events:
        entries.append(
            {
                "time": evidence.get("created_at"),
                "source": evidence.get("source"),
                "source_type": evidence.get("source_type"),
                "event_kind": "evidence",
                "event_id": evidence.get("event_id"),
                "title": evidence.get("summary") or evidence.get("evidence_type") or "Evidence",
                "detail": evidence.get("command") or evidence.get("artifact_ref") or evidence.get("result"),
                "tokens": None,
                "estimated_cost_usd": None,
                "work_id": evidence.get("work_id") or evidence.get("section_id"),
                "status": evidence.get("result"),
                "join_confidence": "",
            }
        )
    for debug in usage_debug_events:
        entries.append(
            {
                "time": debug.get("created_at"),
                "source": debug.get("source"),
                "source_type": "mcp_agent_reported",
                "event_kind": "usage_debug",
                "event_id": debug.get("event_id"),
                "title": "Agent usage visibility",
                "detail": debug.get("summary") or debug.get("reporting_basis"),
                "tokens": None,
                "estimated_cost_usd": None,
                "work_id": None,
                "status": debug.get("reporting_basis"),
                "join_confidence": "",
            }
        )
    return sorted(entries, key=lambda entry: float(entry.get("time") or 0.0), reverse=True)


def _usage_attribution_insight_summary(usage_reconciliation: list[dict[str, Any]]) -> dict[str, Any]:
    rows_by_state: dict[str, list[dict[str, Any]]] = {}
    for row in usage_reconciliation:
        state = str(row.get("usage_reconciliation_state") or row.get("usage_join_state") or "usage_without_mcp_context")
        rows_by_state.setdefault(state, []).append(row)
    attributed = rows_by_state.get("attributed", [])
    ambiguous = rows_by_state.get("ambiguous", [])
    context_matched_unallocated = rows_by_state.get("context_matched_unallocated", [])
    usage_without_context = rows_by_state.get("usage_without_mcp_context", [])
    unknown_or_unattributed = ambiguous + context_matched_unallocated + usage_without_context
    return {
        "usage_truth_count": len(usage_reconciliation),
        "attributed_count": len(attributed),
        "ambiguous_count": len(ambiguous),
        "context_matched_unallocated_count": len(context_matched_unallocated),
        "usage_without_mcp_context_count": len(usage_without_context),
        "attributed_tokens": _sum_row_tokens(attributed),
        "unknown_or_unattributed_tokens": _sum_row_tokens(unknown_or_unattributed),
        "attributed_fresh_tokens": _sum_row_key(attributed, "fresh_tokens"),
        "unknown_or_unattributed_fresh_tokens": _sum_row_key(unknown_or_unattributed, "fresh_tokens"),
        "attributed_cache_read_tokens": _sum_row_key(attributed, "cache_read_tokens"),
        "unknown_or_unattributed_cache_read_tokens": _sum_row_key(unknown_or_unattributed, "cache_read_tokens"),
        "attributed_cache_creation_tokens": _sum_row_key(attributed, "cache_creation_tokens"),
        "unknown_or_unattributed_cache_creation_tokens": _sum_row_key(unknown_or_unattributed, "cache_creation_tokens"),
        "attributed_cost_usd": _sum_row_cost(attributed),
        "unknown_or_unattributed_cost_usd": _sum_row_cost(unknown_or_unattributed),
    }


def _trust_insight_summary(work_items: list[dict[str, Any]], evidence_events: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [item for item in work_items if item.get("latest_status") == "completed"]
    evidence_backed = [item for item in completed if item.get("evidence_status") == "strong"]
    completed_without_strong = [item for item in completed if item.get("evidence_status") != "strong"]
    return {
        "completed_work_count": len(completed),
        "evidence_backed_completed_count": len(evidence_backed),
        "completed_without_strong_evidence_count": len(completed_without_strong),
        "failed_evidence_count": sum(1 for event in evidence_events if event.get("result") in {"failed", "error"}),
    }


def _ledger_blind_spots(work_items: list[dict[str, Any]], usage_reconciliation: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blind_spots: list[dict[str, Any]] = []
    usage_without_context = [
        row
        for row in usage_reconciliation
        if (row.get("usage_reconciliation_state") or row.get("usage_join_state")) == "usage_without_mcp_context"
    ]
    if usage_without_context:
        blind_spots.append(
            {
                "type": "usage_without_mcp_context",
                "severity": "medium",
                "summary": f"{len(usage_without_context)} imported usage row(s) have no matching MCP work context.",
                "tokens": _sum_row_tokens(usage_without_context),
                "fresh_tokens": _sum_row_key(usage_without_context, "fresh_tokens"),
                "cache_read_tokens": _sum_row_key(usage_without_context, "cache_read_tokens"),
                "cache_creation_tokens": _sum_row_key(usage_without_context, "cache_creation_tokens"),
                "estimated_cost_usd": _sum_row_cost(usage_without_context),
                "recommended_next_step": _USAGE_WITHOUT_MCP_CONTEXT_NEXT_STEP,
            }
        )

    ambiguous = [row for row in usage_reconciliation if (row.get("usage_reconciliation_state") or row.get("usage_join_state")) == "ambiguous"]
    if ambiguous:
        blind_spots.append(
            {
                "type": "ambiguous_attribution",
                "severity": "medium",
                "summary": f"{len(ambiguous)} usage row(s) match multiple MCP sections; agentacct does not allocate section-level billing.",
                "tokens": _sum_row_tokens(ambiguous),
                "fresh_tokens": _sum_row_key(ambiguous, "fresh_tokens"),
                "cache_read_tokens": _sum_row_key(ambiguous, "cache_read_tokens"),
                "cache_creation_tokens": _sum_row_key(ambiguous, "cache_creation_tokens"),
                "estimated_cost_usd": _sum_row_cost(ambiguous),
                "recommended_next_step": "Attach client_transcript_id or another narrower join key so shared sessions can be disambiguated.",
            }
        )

    completed_without_evidence = [
        item for item in work_items if item.get("latest_status") == "completed" and item.get("evidence_status") != "strong"
    ]
    if completed_without_evidence:
        blind_spots.append(
            {
                "type": "completed_without_evidence",
                "severity": "high",
                "summary": f"{len(completed_without_evidence)} completed work item(s) lack strong machine-check evidence.",
                "tokens": sum(int(item.get("usage_total") or 0) for item in completed_without_evidence),
                "fresh_tokens": sum(int(item.get("usage_fresh_total") or 0) for item in completed_without_evidence),
                "cache_read_tokens": sum(int(item.get("usage_cache_read_total") or 0) for item in completed_without_evidence),
                "cache_creation_tokens": sum(int(item.get("usage_cache_creation_total") or 0) for item in completed_without_evidence),
                "estimated_cost_usd": _sum_item_cost(completed_without_evidence),
                "recommended_next_step": "Record machine-check evidence after completion, such as tests, builds, smoke checks, or artifacts.",
            }
        )

    completed_evidenced_without_usage = [
        item
        for item in work_items
        if item.get("latest_status") == "completed" and item.get("evidence_status") == "strong" and int(item.get("linked_usage_records") or 0) == 0
    ]
    if completed_evidenced_without_usage:
        blind_spots.append(
            {
                "type": "completed_without_attributed_usage",
                "severity": "medium",
                "summary": (
                    f"Usage unknown / not attributed for {len(completed_evidenced_without_usage)} evidence-backed completed "
                    "work item(s). This is not a zero-cost claim."
                ),
                "tokens": None,
                "fresh_tokens": None,
                "cache_read_tokens": None,
                "cache_creation_tokens": None,
                "estimated_cost_usd": None,
                "recommended_next_step": "Run local usage import/watch and make sure MCP context includes client_session_id or client_transcript_id.",
            }
        )

    return sorted(blind_spots, key=_blind_spot_sort_key, reverse=True)


def _top_next_actions(
    *,
    blind_spots: list[dict[str, Any]],
    usage_events: list[dict[str, Any]],
    work_items: list[dict[str, Any]],
    trust_summary: dict[str, Any],
) -> list[str]:
    actions: list[str] = []
    if work_items and not usage_events:
        actions.append("Run local usage import/watch to refresh Codex/Claude usage truth.")
    if int(trust_summary.get("failed_evidence_count") or 0) > 0:
        actions.append("Fix or rerun failed machine-check evidence before trusting completed work.")
    for blind_spot in blind_spots:
        action = _optional_str(blind_spot.get("recommended_next_step"))
        if action:
            actions.append(action)
    if not actions:
        actions.append("No urgent reconciliation action. Keep local usage import/watch, MCP context attach, and machine-check evidence enabled.")
    return _dedupe_strings(actions)[:3]


def _ledger_health(
    *,
    usage_events: list[dict[str, Any]],
    work_items: list[dict[str, Any]],
    blind_spots: list[dict[str, Any]],
    trust_summary: dict[str, Any],
    attention_items: list[dict[str, Any]],
) -> tuple[str, str]:
    high_blind_spots = [item for item in blind_spots if item.get("severity") == "high"]
    medium_blind_spots = [item for item in blind_spots if item.get("severity") == "medium"]
    if high_blind_spots:
        return "poor", str(high_blind_spots[0].get("summary") or "High-severity ledger blind spots need attention.")
    if int(trust_summary.get("failed_evidence_count") or 0) > 0:
        return "poor", "One or more machine-check evidence records failed or errored."
    if medium_blind_spots:
        return "partial", str(medium_blind_spots[0].get("summary") or "Some usage or work records need reconciliation.")
    if work_items and not usage_events:
        return "partial", "MCP work exists, but no imported usage truth rows are available yet."
    if usage_events and not work_items:
        return "partial", "Imported usage exists, but no MCP work context has been recorded yet."
    if attention_items:
        return "partial", "The ledger has attention items that should be reviewed."
    return "good", "Usage, MCP work, and evidence are reconciled with no urgent blind spots."


def _blind_spot_sort_key(blind_spot: dict[str, Any]) -> tuple[int, int, float]:
    severity_rank = {"high": 3, "medium": 2, "low": 1}
    return (
        severity_rank.get(str(blind_spot.get("severity") or ""), 0),
        int(blind_spot.get("tokens") or 0),
        float(blind_spot.get("estimated_cost_usd") or 0.0),
    )


def _sum_row_tokens(rows: list[dict[str, Any]]) -> int:
    return sum(int(row.get("total_tokens") or 0) for row in rows)


def _sum_row_key(rows: list[dict[str, Any]], key: str) -> int:
    return sum(int(row.get(key) or 0) for row in rows)


def _sum_row_cost(rows: list[dict[str, Any]]) -> float:
    return sum(float(row.get("estimated_cost_usd") or 0.0) for row in rows)


def _sum_item_cost(items: list[dict[str, Any]]) -> float:
    return sum(float(item.get("estimated_cost_total") or 0.0) for item in items)


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _is_usage_event(event: dict[str, Any]) -> bool:
    return is_local_usage_import_event(event)


def _is_diagnostic_usage_event(event: dict[str, Any]) -> bool:
    return event.get("event_type") == "model_usage" and not _is_usage_event(event)


def _is_work_event(event: dict[str, Any]) -> bool:
    metadata = _metadata(event)
    if metadata.get("sentinel_semantic_kind") == "section":
        return True
    return str(event.get("event_type") or "").startswith("section_") and bool(metadata.get("section_id"))


def _is_evidence_event(event: dict[str, Any]) -> bool:
    metadata = _metadata(event)
    return metadata.get("sentinel_semantic_kind") == "evidence" or event.get("event_type") == "machine_check"


def _trusted_client_session_title(metadata: dict[str, Any]) -> str | None:
    """Accept only importer-sanitized, explicit client title fields.

    Historical rows and agent-authored metadata may carry a similarly named
    value without proving it came from a client's title field. Missing beats
    leaking prompt/transcript text into the product UI.
    """

    if metadata.get("title_redacted") is not False:
        return None
    if metadata.get("client_session_title_source") != "explicit_client_title_field":
        return None
    if metadata.get("client_session_title_sanitized") is not True:
        return None
    return _optional_str(metadata.get("client_session_title"))


def _usage_event(event: dict[str, Any]) -> dict[str, Any] | None:
    metadata = _metadata(event)
    client = _optional_str(metadata.get("client")) or _optional_str(event.get("source"))
    input_tokens = _safe_int(event.get("estimated_input_tokens"))
    output_tokens = _safe_int(event.get("estimated_output_tokens"))
    cache_creation_tokens, cache_read_tokens = _cache_token_split(metadata, _safe_int(metadata.get("cached_input_tokens")))
    # Reconciled cached total (== creation + read by _cache_token_split's
    # invariant) so fresh + creation + read == total_tokens always holds.
    cached_input_tokens = cache_creation_tokens + cache_read_tokens
    usage_additive, usage_normalization_state = local_usage_event_additivity(event)
    raw_input_tokens = input_tokens
    raw_output_tokens = output_tokens
    raw_cached_input_tokens = cached_input_tokens
    if not usage_additive:
        input_tokens = 0
        output_tokens = 0
        cache_creation_tokens = 0
        cache_read_tokens = 0
        cached_input_tokens = 0
    recorded_at = _safe_float(event.get("created_at"))
    occurred_at, time_source = _usage_occurred_at(event)
    project_value = metadata.get("project_dir") or metadata.get("cwd")
    project_label, project_source = _project_label_info(project_value)
    return {
        "usage_event_id": _event_id(event),
        "event_id": _event_id(event),
        "created_at": occurred_at,
        "occurred_at": occurred_at,
        "recorded_at": recorded_at,
        "time_source": time_source,
        "source": _optional_str(event.get("source")),
        "source_type": _source_type(event),
        "run_id": _optional_str(event.get("run_id")),
        "client": client,
        # Read-time base normalization: reverse our own legacy ':model:' key
        # artifact on trusted import rows so un-migrated stores join work by
        # the TRUE client session id. Only claude-code rows ever carried the
        # marker (child ':stem' suffixes are preserved), and normalization is
        # deliberately NOT applied to diagnostic (non-import) usage rows,
        # where stripping could merge unrelated ids — missing beats wrong.
        "client_session_id": _normalized_import_session_id(metadata.get("client"), metadata.get("client_session_id")),
        "client_transcript_id": _optional_str(metadata.get("client_transcript_id")),
        "parent_client_session_id": _optional_str(metadata.get("parent_client_session_id")),
        # Import-source identity is independent of agentacct's semantic
        # namespace. It protects parent lineage when identical client session
        # ids exist in different local client homes.
        "source_namespace_fingerprint": _optional_str(
            metadata.get("source_namespace_fingerprint")
        ),
        "parent_source_namespace_fingerprint": _optional_str(
            metadata.get("parent_source_namespace_fingerprint")
        ),
        "session_namespace_fingerprint": _optional_str(
            metadata.get("session_namespace_fingerprint") or metadata.get("namespace_fingerprint")
        ),
        "identity_scope_state": _optional_str(metadata.get("identity_scope_state")),
        "client_session_kind": _optional_str(metadata.get("client_session_kind")),
        "client_session_title": _trusted_client_session_title(metadata),
        "usage_row_lane": local_usage_row_lane(event),
        # Importer-recorded per-row turn count (every importer stores one).
        # Kept verbatim: absent/unparseable stays None — never guessed.
        "turn_count": _safe_optional_turn_count(metadata.get("turn_count")),
        "started_at": _safe_positive_float(metadata.get("started_at")),
        "project_dir": project_label,
        "project_source": project_source,
        "project_identity": _project_identity(project_value),
        "provider": _optional_str(event.get("provider")),
        "model": _optional_str(event.get("model")),
        "usage_additive": usage_additive,
        "usage_normalization_state": usage_normalization_state,
        "raw_cumulative_input_tokens": raw_input_tokens if not usage_additive else None,
        "raw_cumulative_output_tokens": raw_output_tokens if not usage_additive else None,
        "raw_cumulative_cached_input_tokens": raw_cached_input_tokens if not usage_additive else None,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached_input_tokens,
        # ONE definition everywhere: fresh = input + output only. Cache
        # creation is real billed compute but not fresh; cache reads are
        # separate. total_tokens keeps its historical everything-included
        # meaning — no view may print it without an "incl. cache reads" label.
        "fresh_tokens": input_tokens + output_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens_reported": (
            bool(metadata.get("cache_creation_tokens_reported"))
            if isinstance(metadata.get("cache_creation_tokens_reported"), bool)
            else None
        ),
        "cache_read_tokens_reported": (
            bool(metadata.get("cache_read_tokens_reported"))
            if isinstance(metadata.get("cache_read_tokens_reported"), bool)
            else None
        ),
        "total_tokens": input_tokens + output_tokens + cached_input_tokens,
        "estimated_cost_usd": _safe_optional_float(event.get("estimated_cost_usd")) if usage_additive else None,
        "usage_confidence": normalize_usage_confidence(event.get("usage_confidence")),
        "cost_confidence": normalize_cost_confidence(event.get("cost_confidence")) if usage_additive else "unknown",
    }


def _cache_token_split(metadata: dict[str, Any], cached_input_tokens: int) -> tuple[int, int]:
    """(cache_creation_tokens, cache_read_tokens) reconciled so the triple always adds up.

    Invariant enforced for every metadata shape (all current importers write
    consistent shapes; this guards FUTURE/hostile shapes): the returned pair
    always satisfies ``creation + read == cached total used in total_tokens``,
    so ``fresh + creation + read == total_tokens`` can never be violated.

    Rules, in order:
    - No split fields (or both zero): merged legacy ``cached_input_tokens``
      is honestly treated as cache reads (creation stays 0) — exact rule
      shared with client_usage pricing.
    - Split fields present and consistent with the merged figure: used as-is.
    - Split fields present but the merged figure disagrees (absent while the
      split is non-zero, or larger than the split sum): the cached total is
      ``max(merged, creation + read)`` and the unaccounted remainder goes to
      cache READS — the conservative bucket, since inflating reads never
      inflates the billed-compute figures (fresh + cache writes).
    """

    cache_creation = _safe_int(metadata.get("cache_creation_input_tokens"))
    cache_read = _safe_int(metadata.get("cache_read_input_tokens"))
    if cache_creation + cache_read <= 0:
        return 0, cached_input_tokens
    cached_total = max(cached_input_tokens, cache_creation + cache_read)
    return cache_creation, cached_total - cache_creation


def _normalized_import_session_id(client: Any, value: Any) -> str | None:
    session_id = _optional_str(value)
    if session_id is None:
        return None
    return normalized_local_usage_session_id(client, session_id)


def _diagnostic_usage_event(event: dict[str, Any]) -> dict[str, Any] | None:
    metadata = _metadata(event)
    client = _optional_str(metadata.get("client")) or _optional_str(event.get("source"))
    input_tokens = _safe_int(event.get("estimated_input_tokens"))
    output_tokens = _safe_int(event.get("estimated_output_tokens"))
    cache_creation_tokens, cache_read_tokens = _cache_token_split(metadata, _safe_int(metadata.get("cached_input_tokens")))
    # Same reconciliation as _usage_event: the triple must always add up.
    cached_input_tokens = cache_creation_tokens + cache_read_tokens
    recorded_at = _safe_float(event.get("created_at"))
    project_value = metadata.get("project_dir") or metadata.get("cwd")
    project_label, project_source = _project_label_info(project_value)
    return {
        "usage_event_id": _event_id(event),
        "event_id": _event_id(event),
        "created_at": recorded_at,
        "occurred_at": recorded_at,
        "recorded_at": recorded_at,
        "time_source": "event_created_at",
        "source": _optional_str(event.get("source")),
        "source_type": _source_type(event),
        "run_id": _optional_str(event.get("run_id")),
        "client": client,
        "client_session_id": _optional_str(metadata.get("client_session_id")),
        "client_transcript_id": _optional_str(metadata.get("client_transcript_id")),
        "parent_client_session_id": _optional_str(metadata.get("parent_client_session_id")),
        "session_namespace_fingerprint": _optional_str(
            metadata.get("session_namespace_fingerprint") or metadata.get("namespace_fingerprint")
        ),
        "identity_scope_state": _optional_str(metadata.get("identity_scope_state")),
        # Diagnostic/agent-authored model_usage metadata is not a trusted
        # source of client UI titles, even if it copies similarly named keys.
        "client_session_title": None,
        "project_dir": project_label,
        "project_source": project_source,
        "project_identity": _project_identity(project_value),
        "provider": _optional_str(event.get("provider")),
        "model": _optional_str(event.get("model")),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached_input_tokens,
        "fresh_tokens": input_tokens + output_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens_reported": (
            bool(metadata.get("cache_creation_tokens_reported"))
            if isinstance(metadata.get("cache_creation_tokens_reported"), bool)
            else None
        ),
        "cache_read_tokens_reported": (
            bool(metadata.get("cache_read_tokens_reported"))
            if isinstance(metadata.get("cache_read_tokens_reported"), bool)
            else None
        ),
        "total_tokens": input_tokens + output_tokens + cached_input_tokens,
        "estimated_cost_usd": _safe_optional_float(event.get("estimated_cost_usd")),
        "usage_confidence": normalize_usage_confidence(event.get("usage_confidence")),
        "cost_confidence": normalize_cost_confidence(event.get("cost_confidence")),
        "diagnostic_reason": "model_usage event is not a local usage import row",
    }


def _proxy_usage_event(event: dict[str, Any]) -> dict[str, Any]:
    input_tokens = _safe_int(event.get("estimated_input_tokens"))
    output_tokens = _safe_int(event.get("estimated_output_tokens"))
    recorded_at = _safe_float(event.get("created_at"))
    return {
        "usage_event_id": _event_id(event),
        "event_id": _event_id(event),
        "created_at": recorded_at,
        "occurred_at": recorded_at,
        "recorded_at": recorded_at,
        "time_source": "event_created_at",
        "source": "proxy",
        "source_type": "proxy",
        "run_id": _optional_str(event.get("run_id")),
        "client": "proxy",
        "client_session_id": None,
        "client_transcript_id": None,
        "parent_client_session_id": None,
        "project_dir": None,
        "provider": _optional_str(event.get("provider")),
        "model": _optional_str(event.get("model")),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": 0,
        "fresh_tokens": input_tokens + output_tokens,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens_reported": False,
        "cache_read_tokens_reported": False,
        "total_tokens": input_tokens + output_tokens,
        "estimated_cost_usd": _safe_optional_float(event.get("estimated_cost_usd")),
        "usage_confidence": normalize_usage_confidence(event.get("usage_confidence")),
        "cost_confidence": normalize_cost_confidence(event.get("cost_confidence")),
    }


def _work_event(event: dict[str, Any]) -> dict[str, Any] | None:
    metadata = _metadata(event)
    section_id = _optional_str(metadata.get("section_id"))
    if section_id is None:
        return None
    status = _optional_str(metadata.get("section_status")) or str(event.get("event_type") or "").removeprefix("section_") or "started"
    if status not in WORK_STATUSES:
        status = "checkpoint"
    files = _safe_relative_paths(metadata.get("files"))
    # Composite work identity (client::session::section_id): sections sharing a
    # raw section_id across clients/sessions must not merge into one item, and
    # each session keeps its own latest snapshot as a join candidate.
    client_scope = _optional_str(metadata.get("client")) or _optional_str(event.get("source"))
    session_scope = _optional_str(metadata.get("client_session_id")) or _optional_str(metadata.get("client_transcript_id"))
    project_label, project_source = _project_label_info(metadata.get("project_dir"))
    return {
        "event_id": _event_id(event),
        "work_id": work_key(client_scope, session_scope, section_id),
        "section_id": section_id,
        "created_at": _safe_float(event.get("created_at")),
        "source": _optional_str(event.get("source")),
        "source_type": "mcp_agent_reported",
        "run_id": _optional_str(event.get("run_id")),
        "client": _optional_str(metadata.get("client")),
        "client_session_id": _optional_str(metadata.get("client_session_id")),
        "client_transcript_id": _optional_str(metadata.get("client_transcript_id")),
        "parent_client_session_id": _optional_str(metadata.get("parent_client_session_id")),
        "session_namespace_fingerprint": _optional_str(
            metadata.get("session_namespace_fingerprint") or metadata.get("namespace_fingerprint")
        ),
        "identity_scope_state": _optional_str(metadata.get("identity_scope_state")),
        "inherited_join_keys": _safe_inherited_join_keys(metadata.get("client_context_inherited_keys")),
        "authored_join_keys": _safe_inherited_join_keys(metadata.get("client_context_keys_authored")),
        "client_context_source": _optional_str(metadata.get("client_context_source")),
        "project_dir": project_label,
        "project_source": project_source,
        "project_identity": _project_identity(metadata.get("project_dir")),
        "title": _optional_str(metadata.get("section_title")) or section_id,
        "status": status,
        "summary": _optional_str(metadata.get("summary")),
        "kind": _optional_str(metadata.get("kind")) or _optional_str(metadata.get("phase")) or "unknown",
        "phase": _optional_str(metadata.get("phase")),
        "files": files,
        "blocker": _optional_str(metadata.get("blocker")),
        "next_step": _optional_str(metadata.get("next_step")),
    }


def _evidence_event(event: dict[str, Any]) -> dict[str, Any] | None:
    metadata = _metadata(event)
    result = _optional_str(metadata.get("result")) or _optional_str(metadata.get("status")) or _result_from_exit_code(metadata.get("exit_code"))
    if result not in EVIDENCE_RESULTS:
        result = "unknown"
    project_label, project_source = _project_label_info(metadata.get("project_dir"))
    trusted_resolution = (
        metadata.get("blocker_resolution_contract")
        == _BLOCKER_RESOLUTION_CONTRACT
    )
    evidence_type = _optional_str(metadata.get("evidence_type")) or "other"
    # Preserve retry identity without exposing a command. A name or command
    # differentiates independent checks of the same broad evidence type;
    # type-only legacy events retain their historical retry behavior.
    check_name = _optional_str(metadata.get("name"))
    check_command = _optional_str(metadata.get("command"))
    if check_name or check_command:
        identity_material = "\0".join((evidence_type, check_name or "", check_command or ""))
        check_identity = f"check:{sha256(identity_material.encode('utf-8')).hexdigest()[:16]}"
        check_identity_basis = "name_or_command"
        check_identity_stable = True
    else:
        check_identity = f"type:{evidence_type}"
        check_identity_basis = "type_fallback"
        check_identity_stable = False
    return {
        "event_id": _event_id(event),
        "event_digest": sha256(
            json.dumps(
                event,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest(),
        "created_at": _safe_float(event.get("created_at")),
        "source": _optional_str(event.get("source")),
        "source_type": "mcp_agent_reported",
        "run_id": _optional_str(event.get("run_id")),
        "work_id": _optional_str(metadata.get("work_id")) or _optional_str(metadata.get("section_id")),
        "section_id": _optional_str(metadata.get("section_id")) or _optional_str(metadata.get("work_id")),
        "asserted_work_id": _optional_str(metadata.get("work_id")),
        "asserted_section_id": _optional_str(metadata.get("section_id")),
        "client": _optional_str(metadata.get("client")),
        "client_session_id": _optional_str(metadata.get("client_session_id")),
        "client_transcript_id": _optional_str(metadata.get("client_transcript_id")),
        "session_namespace_fingerprint": _optional_str(
            metadata.get("session_namespace_fingerprint")
            or metadata.get("namespace_fingerprint")
        ),
        "namespace_fingerprint": _optional_str(
            metadata.get("session_namespace_fingerprint")
            or metadata.get("namespace_fingerprint")
        ),
        "identity_scope_state": _optional_str(metadata.get("identity_scope_state")),
        "project_dir": project_label,
        "project_source": project_source,
        "project_identity": _project_identity(metadata.get("project_dir")),
        "evidence_type": evidence_type,
        "check_identity": check_identity,
        "check_identity_basis": check_identity_basis,
        "check_identity_stable": check_identity_stable,
        "result": result,
        "summary": _optional_str(metadata.get("summary")) or _optional_str(metadata.get("after_summary")) or _optional_str(metadata.get("name")),
        "command": None,
        "command_redacted": bool(_optional_str(metadata.get("command"))),
        "exit_code": _safe_optional_int(metadata.get("exit_code")),
        "artifact_ref": _optional_str(metadata.get("artifact_ref")),
        "artifact_path": _safe_artifact_path(metadata.get("artifact_path")),
        "artifact_url": _safe_artifact_url(metadata.get("artifact_url")),
        "artifact_path_redacted": bool(_optional_str(metadata.get("artifact_path")) and _safe_artifact_path(metadata.get("artifact_path")) is None),
        "artifact_url_redacted": bool(_optional_str(metadata.get("artifact_url")) and _safe_artifact_url(metadata.get("artifact_url")) is None),
        "files": _safe_relative_paths(metadata.get("files")),
        "resolves_blocked_event_id": (
            _optional_str(metadata.get("resolves_blocked_event_id"))
            if trusted_resolution
            else None
        ),
        "resolution_scope": (
            _optional_str(metadata.get("resolution_scope"))
            if trusted_resolution
            else None
        ),
        "resolution_summary": (
            _optional_str(metadata.get("resolution_summary"))
            if trusted_resolution
            else None
        ),
        "resolution_objective_basis": (
            _optional_str(metadata.get("resolution_objective_basis"))
            if trusted_resolution
            else None
        ),
        "blocker_resolution_contract": (
            _BLOCKER_RESOLUTION_CONTRACT if trusted_resolution else None
        ),
    }


def _run_report_evidence_events(run_reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for report in run_reports:
        if not isinstance(report, dict):
            continue
        run = report.get("run") if isinstance(report.get("run"), dict) else {}
        outcome = report.get("outcome") if isinstance(report.get("outcome"), dict) else {}
        machine = outcome.get("machine_checks") if isinstance(outcome, dict) else {}
        if not isinstance(machine, dict) or not machine.get("configured"):
            continue
        run_id = _optional_str(run.get("run_id"))
        checks = machine.get("checks")
        if isinstance(checks, list) and checks:
            for index, check in enumerate(checks):
                if not isinstance(check, dict):
                    continue
                after = check.get("after") if isinstance(check.get("after"), dict) else {}
                status = _optional_str(after.get("status") or machine.get("after")) or "unknown"
                events.append(
                    {
                        "event_id": f"run:{run_id}:machine_check:{index}",
                        "created_at": _safe_float(run.get("ended_at") or run.get("started_at")),
                        "source": "run_outcome",
                        "source_type": "mcp_agent_reported",
                        "run_id": run_id,
                        "work_id": None,
                        "section_id": None,
                        "client": None,
                        "client_session_id": None,
                        "project_dir": None,
                        "evidence_type": "test",
                        "result": _result_from_machine_status(status),
                        "summary": _optional_str(after.get("summary")),
                        "command": None,
                        "exit_code": None,
                        "artifact_ref": _optional_str(check.get("name")),
                        "artifact_path": None,
                        "artifact_url": None,
                        "files": [],
                    }
                )
            continue
        status = _optional_str(machine.get("after")) or "unknown"
        events.append(
            {
                "event_id": f"run:{run_id}:machine_check",
                "created_at": _safe_float(run.get("ended_at") or run.get("started_at")),
                "source": "run_outcome",
                "source_type": "mcp_agent_reported",
                "run_id": run_id,
                "work_id": None,
                "section_id": None,
                "client": None,
                "client_session_id": None,
                "project_dir": None,
                "evidence_type": "test",
                "result": _result_from_machine_status(status),
                "summary": None,
                "command": None,
                "exit_code": None,
                "artifact_ref": "machine checks",
                "artifact_path": None,
                "artifact_url": None,
                "files": [],
            }
        )
    return events


def _attach_join_explanations(work_items: list[dict[str, Any]], explanations: dict[str, dict[str, Any]]) -> None:
    for item in work_items:
        work_id = str(item.get("work_id") or item.get("section_id") or "")
        item["join_explanation"] = explanations.get(work_id) or _default_work_join_explanation()


def _default_work_join_explanation() -> dict[str, Any]:
    return {
        "usage_join_state": "no_usage_found",
        "join_confidence": "unjoined",
        "join_strategy": "no_usage_import",
        "join_reason": "No imported usage truth rows are available for this work item.",
        "missing_join_keys": [],
        "candidate_usage_count": 0,
        "attributed_usage_count": 0,
        "context_matched_usage_count": 0,
        "nearest_usage_summary": None,
        "recommended_next_step": "Run local usage import to refresh Codex/Claude usage.",
    }


def _work_join_explanation(
    *,
    item: dict[str, Any],
    usage_events: list[dict[str, Any]],
    attributed: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    ambiguous: bool,
    missing_join_keys: list[str],
) -> dict[str, Any]:
    if not usage_events:
        return _default_work_join_explanation()
    if attributed:
        best = _best_attribution(attributed)
        return {
            "usage_join_state": "attributed",
            "join_confidence": best.get("join_confidence"),
            "join_strategy": best.get("join_strategy"),
            "join_reason": best.get("join_reason") or "Imported usage and MCP work share deterministic join keys.",
            "missing_join_keys": missing_join_keys,
            "recommended_next_step": "Usage is attached to this work item with deterministic local join keys.",
        }
    if ambiguous:
        return {
            "usage_join_state": "ambiguous",
            "join_confidence": "medium",
            "join_strategy": "ambiguous_same_client_session",
            "join_reason": "Usage matches multiple MCP sections in the same client session; agentacct does not allocate section-level usage.",
            "missing_join_keys": [],
            "recommended_next_step": "Multiple sections share this client session; agentacct does not allocate section-level usage.",
        }
    if candidates:
        return {
            "usage_join_state": "context_matched_unallocated",
            "join_confidence": _best_join_confidence([candidate.get("join_confidence") for candidate in candidates]),
            "join_strategy": "context_matched_unallocated",
            "join_reason": "Imported usage shares MCP context, but agentacct did not attach it to this specific work item.",
            "missing_join_keys": [],
            "recommended_next_step": "Multiple sections may share this context; agentacct does not allocate section-level usage.",
        }
    if missing_join_keys:
        return {
            "usage_join_state": "missing_join_keys",
            "join_confidence": "unjoined",
            "join_strategy": "missing_join_keys",
            "join_reason": "MCP work context is missing the client join key needed to reconcile imported usage logs.",
            "missing_join_keys": missing_join_keys,
            "recommended_next_step": "MCP context is missing client_session_id; attach client context at session start.",
        }
    return {
        "usage_join_state": "unattributed",
        "join_confidence": "unjoined",
        "join_strategy": "unjoined",
        "join_reason": "Usage exists but no MCP work context matched this work item.",
        "missing_join_keys": [],
        "recommended_next_step": "Usage exists but no MCP work context matched.",
    }


def _best_attribution(attributions: list[dict[str, Any]]) -> dict[str, Any]:
    return max(attributions, key=lambda attr: _JOIN_RANK.get(str(attr.get("join_confidence") or "unjoined"), 0))


def _missing_work_join_keys(item: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if not item.get("client_session_id") and not item.get("client_transcript_id"):
        missing.append("client_session_id")
    return missing


def _nearest_usage_summary(item: dict[str, Any], usage_events: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not usage_events:
        return None
    same_client = [usage for usage in usage_events if not item.get("client") or not usage.get("client") or usage.get("client") == item.get("client")]
    candidates = same_client or usage_events
    item_time = _safe_optional_float(item.get("updated_at")) or _safe_optional_float(item.get("started_at")) or 0.0
    nearest = min(candidates, key=lambda usage: abs((_safe_optional_float(usage.get("created_at")) or 0.0) - item_time))
    return {
        "client": nearest.get("client"),
        "provider_model": _provider_model_label(nearest.get("provider"), nearest.get("model")),
        "total_tokens": nearest.get("total_tokens"),
        "estimated_cost_usd": nearest.get("estimated_cost_usd"),
        "usage_confidence": nearest.get("usage_confidence"),
        "cost_confidence": nearest.get("cost_confidence"),
    }


def _zero_usage_explanation(explanation: dict[str, Any]) -> str:
    reason = explanation.get("join_reason")
    suffix = f" {reason}" if reason else ""
    return (
        "Usage unknown / not attributed. This work has MCP evidence, but no imported usage is attached to it. "
        "Possible causes: local usage import not run, Codex session id unavailable, or ambiguous session match."
        + suffix
    )


def _is_attributed_attribution(attribution: dict[str, Any]) -> bool:
    return bool(attribution.get("work_id")) and attribution.get("join_confidence") != "unjoined"


def _is_context_matched_unallocated_attribution(attribution: dict[str, Any]) -> bool:
    if _is_attributed_attribution(attribution):
        return False
    if _is_ambiguous_attribution(attribution):
        return False
    strategy = str(attribution.get("join_strategy") or "unjoined")
    return strategy != "unjoined"


def _is_ambiguous_attribution(attribution: dict[str, Any]) -> bool:
    return "ambiguous" in str(attribution.get("join_strategy") or "")


def _latest_work_by_id(work_events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for event in work_events:
        work_id = str(event.get("work_id") or event.get("section_id") or "")
        if not work_id:
            continue
        current = latest.get(work_id)
        if current is None or float(event.get("created_at") or 0.0) >= float(current.get("created_at") or 0.0):
            latest[work_id] = event
    return latest


def _source_type(event: dict[str, Any]) -> str:
    if is_local_usage_import_event(event):
        return "log_import"
    source = str(event.get("source") or "")
    if "proxy" in source or str(event.get("event_type") or "") == "budget_decision":
        return "proxy"
    if source == "manual":
        return "manual"
    return "generic"


def _usage_occurred_at(event: dict[str, Any]) -> tuple[float | None, str]:
    metadata = _metadata(event)
    if _source_type(event) != "log_import":
        return _safe_float(event.get("created_at")), "event_created_at"
    client = str(metadata.get("client") or "").strip()
    if client == "hermes":
        candidates = (
            ("metadata.started_at", metadata.get("started_at")),
            ("metadata.updated_at", metadata.get("updated_at")),
        )
    else:
        candidates = (
            ("metadata.updated_at", metadata.get("updated_at")),
            ("metadata.started_at", metadata.get("started_at")),
        )
    for label, value in candidates:
        timestamp = _safe_positive_float(value)
        if timestamp is not None:
            return timestamp, label
    return _safe_float(event.get("created_at")), "event_created_at"


def _evidence_link_ids(evidence: dict[str, Any]) -> set[str]:
    ids = {
        _optional_str(evidence.get("work_id")),
        _optional_str(evidence.get("section_id")),
    }
    return {value for value in ids if value}


def _evidence_candidate_work_ids(
    evidence: dict[str, Any],
    linked_ids: set[str],
    grouped: dict[str, dict[str, Any]],
    *,
    allow_legacy_unscoped_namespace: bool,
) -> list[str]:
    """Resolve an evidence event's raw section/work references to work items.

    References match either the composite work_id or the raw section_id.
    When the evidence carries client/session context, items with conflicting
    context are dropped, and an exact session match narrows the candidates.
    """

    candidates = [
        work_id
        for work_id, item in grouped.items()
        if work_id in linked_ids or str(item.get("section_id") or "") in linked_ids
    ]
    candidates = [
        work_id
        for work_id in candidates
        if namespace_join_compatible(
            evidence,
            grouped[work_id],
            allow_legacy_unscoped=allow_legacy_unscoped_namespace,
        )
    ]
    client = _optional_str(evidence.get("client"))
    if client:
        candidates = [
            work_id
            for work_id in candidates
            if not grouped[work_id].get("client") or grouped[work_id].get("client") == client
        ]
    session = _optional_str(evidence.get("client_session_id"))
    if session:
        candidates = [
            work_id
            for work_id in candidates
            if not grouped[work_id].get("client_session_id") or grouped[work_id].get("client_session_id") == session
        ]
        exact = [work_id for work_id in candidates if grouped[work_id].get("client_session_id") == session]
        if exact:
            candidates = exact
    return candidates


def _evidence_run_context_compatible(
    evidence: dict[str, Any],
    item: dict[str, Any],
    *,
    allow_legacy_unscoped_namespace: bool,
) -> bool:
    """Run ids are only hints; every asserted context dimension must agree."""

    if not namespace_join_compatible(
        evidence,
        item,
        allow_legacy_unscoped=allow_legacy_unscoped_namespace,
    ):
        return False
    for key in ("client", "client_session_id", "client_transcript_id"):
        asserted = _optional_str(evidence.get(key))
        if asserted and asserted != _optional_str(item.get(key)):
            return False
    evidence_project = _optional_str(evidence.get("project_dir"))
    item_project = _optional_str(item.get("project_dir"))
    return not (
        evidence_project
        and item_project
        and evidence_project != item_project
    )


def _evidence_status(evidence_events: list[dict[str, Any]], files: Any) -> str:
    results = {str(event.get("result") or "unknown") for event in evidence_events}
    if results & {"failed", "error"}:
        return "failed"
    if "passed" in results:
        return "strong"
    if evidence_events:
        return "weak"
    if _str_list(files):
        return "weak"
    return "none"


def _breakdown_for_attributions(attributions: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for attribution in attributions:
        value = attribution.get(key)
        if value:
            counts[str(value)] += 1
    return dict(counts)


def _confidence_breakdown(usage_events: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for event in usage_events:
        label = str(event.get(key) or "unknown")
        row = rows.setdefault(
            label,
            {
                "confidence": label,
                "records": 0,
                "tokens": 0,
                "fresh_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "cost_usd": 0.0,
            },
        )
        row["records"] += 1
        row["tokens"] += int(event.get("total_tokens") or 0)
        row["fresh_tokens"] += int(event.get("fresh_tokens") or 0)
        row["cache_read_tokens"] += int(event.get("cache_read_tokens") or 0)
        row["cache_creation_tokens"] += int(event.get("cache_creation_tokens") or 0)
        row["cost_usd"] += float(event.get("estimated_cost_usd") or 0.0)
    return sorted(rows.values(), key=lambda row: str(row["confidence"]))


def _work_status_counts(work_items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"active": 0, "completed": 0, "resolved": 0, "blocked": 0, "checkpoint": 0}
    for item in work_items:
        status = item.get("latest_status")
        if status == "completed":
            counts["completed"] += 1
        elif status == "resolved":
            counts["resolved"] += 1
        elif status == "blocked":
            counts["blocked"] += 1
        elif status == "checkpoint":
            counts["checkpoint"] += 1
            counts["active"] += 1
        else:
            counts["active"] += 1
    return counts


def _best_join_confidence(values: list[Any]) -> str:
    best = "unjoined"
    for value in values:
        text = str(value or "unjoined")
        if _JOIN_RANK.get(text, 0) > _JOIN_RANK.get(best, 0):
            best = text
    return best


def _dedupe_by_event_id(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for event in events:
        event_id = str(event.get("event_id") or "")
        if event_id and event_id in seen:
            continue
        if event_id:
            seen.add(event_id)
        result.append(event)
    return sorted(result, key=lambda item: float(item.get("created_at") or 0.0), reverse=True)


def _metadata(event: dict[str, Any]) -> dict[str, Any]:
    metadata = event.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _event_id(event: dict[str, Any]) -> str:
    existing = _optional_str(event.get("event_id"))
    if existing is not None:
        return existing
    try:
        payload = json.dumps(event, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload = str(event)
    return "event:" + sha256(payload.encode("utf-8")).hexdigest()[:12]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _ordered_unique_text(values: Any, *, limit: int = 50) -> list[str]:
    """Return a bounded, stable set of non-empty metadata labels."""

    result: list[str] = []
    for value in values:
        normalized = _optional_str(value)
        if normalized and normalized not in result:
            result.append(normalized)
        if len(result) >= limit:
            break
    return result


def _safe_int(value: Any) -> int:
    try:
        number = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _safe_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return _safe_int(value)


def _safe_float(value: Any) -> float | None:
    number = _safe_optional_float(value)
    return number if number is not None else None


def _safe_positive_float(value: Any) -> float | None:
    number = _safe_optional_float(value)
    if number is None or number <= 0:
        return None
    return number


def _safe_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _safe_optional_turn_count(value: Any) -> int | None:
    """Importer-recorded turn count, or None — a count is never guessed.

    Absent, unparseable, and negative (implausible) values all stay None;
    a stored 0 is real importer data and passes through as 0.
    """

    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item]


def _safe_inherited_join_keys(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _safe_relative_paths(value: Any) -> list[str]:
    safe: list[str] = []
    for item in _str_list(value):
        normalized = _safe_relative_posix_path(item)
        if normalized is None:
            continue
        if normalized and normalized not in safe:
            safe.append(normalized)
    return safe


def _project_label_info(value: Any) -> tuple[str | None, str | None]:
    """(display label, source) for a project path.

    Read-time worktree remap (Phase 2.6): a path inside
    ``<owner>/.claude/worktrees/<name>`` labels as the OWNER repo — the
    throwaway worktree folder name (``great-tesla-9cc8ad``) is meaningless to
    the user, while the stored full path still names the owning repo. The
    remap is a PURE string parse (``claude_worktree_owner_path_text``): stored
    metadata paths are historical and must never trigger filesystem calls.
    The raw metadata is untouched; only the label changes, and the sibling
    ``project_source == "claude_worktree"`` signal preserves the
    ran-in-a-worktree fact for renderers (JSON keeps the clean name).
    """
    text = _optional_str(value)
    if text is None:
        return None, None
    owner_text = claude_worktree_owner_path_text(text)
    if owner_text is not None:
        return _plain_project_label(owner_text), "claude_worktree"
    return _plain_project_label(text), None


def _project_identity(value: Any) -> str | None:
    """Pseudonymous full-path identity for safety decisions.

    Display labels intentionally keep only a friendly basename, which is not
    strong enough for blocker reconciliation: two unrelated repositories can
    share that basename. Hash the normalized historical path instead (without
    touching the filesystem) and keep the friendly leaf only as a diagnostic
    prefix. Claude temporary worktrees resolve to their owner path so an
    explicit continuation in the owner repo can still match safely.
    """

    text = _optional_str(value)
    if text is None:
        return None
    identity_text = claude_worktree_owner_path_text(text) or text
    normalized = identity_text.replace("\\", "/").rstrip("/") or identity_text
    # Windows drive letters are case-insensitive identity syntax; normalizing
    # only the drive avoids conflating case-sensitive path segments elsewhere.
    normalized = re.sub(
        r"^([A-Za-z]):",
        lambda match: f"{match.group(1).lower()}:",
        normalized,
    )
    label = _plain_project_label(identity_text) or "project"
    digest = sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"project:{label}:{digest}"


def _safe_project_label(value: Any) -> str | None:
    return _project_label_info(value)[0]


def _plain_project_label(text: str) -> str | None:
    try:
        # A session run from the home directory must render "~", never the
        # account username (the last path segment IS the username there).
        if Path(text.rstrip("/\\")).expanduser() == Path.home():
            return "~"
    except (OSError, RuntimeError, ValueError):
        pass
    normalized = text.replace("\\", "/").rstrip("/")
    normalized = re.sub(r"^[A-Za-z]:", "", normalized)
    normalized = normalized.lstrip("/")
    parts = [part for part in normalized.split("/") if part not in {"", ".", ".."}]
    if parts:
        return _short_public_text(parts[-1])
    return _short_public_text(text)


def _safe_artifact_path(value: Any) -> str | None:
    return _safe_relative_posix_path(value)


def _safe_artifact_url(value: Any) -> str | None:
    text = _optional_str(value)
    if text is None:
        return None
    lowered = text.lower()
    if "?" in text or "#" in text or "token=" in lowered or "key=" in lowered or "signature=" in lowered:
        return None
    if lowered.startswith(("http://localhost/", "https://localhost/", "http://127.0.0.1/", "https://127.0.0.1/")):
        return text[:200]
    return None


def _safe_relative_posix_path(value: Any) -> str | None:
    text = _optional_str(value)
    if text is None:
        return None
    text = text.strip()
    if not text or "\x00" in text:
        return None
    if re.match(r"^[A-Za-z]:", text):
        return None
    if text.startswith(("\\\\", "//")):
        return None
    normalized = text.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        return None
    parts = [part for part in path.parts if part not in {"", "."}]
    if not parts:
        return None
    return PurePosixPath(*parts).as_posix()


def _short_public_text(value: str, *, max_length: int = 80) -> str:
    return value if len(value) <= max_length else value[: max_length - 1] + "…"


def _extend_unique(target: list[str], values: Any) -> None:
    for value in _safe_relative_paths(values):
        if value not in target:
            target.append(value)


def _min_timestamp(left: Any, right: Any) -> float | None:
    left_value = _safe_optional_float(left)
    right_value = _safe_optional_float(right)
    if left_value is None:
        return right_value
    if right_value is None:
        return left_value
    return min(left_value, right_value)


def _max_timestamp(left: Any, right: Any) -> float | None:
    left_value = _safe_optional_float(left)
    right_value = _safe_optional_float(right)
    if left_value is None:
        return right_value
    if right_value is None:
        return left_value
    return max(left_value, right_value)


def _percent(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 100.0, 2)


def _result_from_exit_code(value: Any) -> str:
    if value is None:
        return "unknown"
    try:
        return "passed" if int(value) == 0 else "failed"
    except (TypeError, ValueError):
        return "unknown"


def _result_from_machine_status(value: Any) -> str:
    status = str(value or "").strip()
    if status == "passed":
        return "passed"
    if status == "failed":
        return "failed"
    if status == "not_run":
        return "skipped"
    return "unknown"


def _provider_model_label(provider: Any, model: Any) -> str:
    provider_text = _optional_str(provider)
    model_text = _optional_str(model)
    if provider_text and model_text:
        return f"{provider_text}/{model_text}"
    return provider_text or model_text or "unknown model"
