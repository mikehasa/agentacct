from __future__ import annotations

import math
from typing import Any

from .join_rules import (
    JOIN_RANK,
    TIER_CONFIDENCE,
    annotate_usage_source_namespace_ambiguity,
    pair_match,
)
from .log_evidence import apply_log_evidence_to_snapshots, build_log_evidence_index
from .usage_truth import (
    is_local_usage_import_event,
    local_usage_event_additivity,
    normalized_local_usage_session_id,
    split_diagnostic_events,
    split_shadowed_legacy_usage_events,
)
from .work_ledger import build_attributions, build_usage_events, build_work_events


SEMANTIC_KINDS = {"client_context", "section", "agent_usage_debug"}


def build_client_context_join_health(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize whether MCP context/section events can join imported usage.

    agentacct attributes imported usage to MCP work sections only through exact
    client_session_id / client_transcript_id equality. This health check makes
    a broken instrumentation loop visible early (e.g. every attach carrying
    only project_dir), instead of surfacing later as unattributed usage rows.
    """

    # Defense in depth (same rule as build_work_ledger intake): agentacct's own
    # diagnostic tool events never reach user-facing join surfaces.
    events, _diagnostic_tool_events = split_diagnostic_events(events)
    # Count the same canonical usage rows as the bridge/ledger. A stale legacy
    # base row shadowed by per-model lanes is not an extra coverage failure.
    events, _shadowed_legacy_usage_events = split_shadowed_legacy_usage_events(events)
    usage_rows = 0
    excluded_non_additive_usage_rows = 0
    usage_rows_with_join_keys = 0
    context_events = 0
    context_events_with_join_keys = 0
    section_events = 0
    section_events_with_join_keys = 0
    for event in events:
        metadata = _metadata(event)
        if is_local_usage_import_event(event):
            usage_additive, _state = local_usage_event_additivity(event)
            if not usage_additive:
                excluded_non_additive_usage_rows += 1
                continue
            usage_rows += 1
            if _optional_str(metadata.get("client_session_id")) or _optional_str(metadata.get("client_transcript_id")):
                usage_rows_with_join_keys += 1
            continue
        kind = _optional_str(metadata.get("sentinel_semantic_kind"))
        # Mirror work_ledger._is_work_event: legacy section events recorded
        # without sentinel_semantic_kind still join the ledger.
        is_section = kind == "section" or (
            kind not in SEMANTIC_KINDS
            and str(event.get("event_type") or "").startswith("section_")
            and _optional_str(metadata.get("section_id")) is not None
        )
        if kind != "client_context" and not is_section:
            continue
        joinable = bool(_optional_str(metadata.get("client_session_id")) or _optional_str(metadata.get("client_transcript_id")))
        if kind == "client_context":
            context_events += 1
            context_events_with_join_keys += 1 if joinable else 0
        else:
            section_events += 1
            section_events_with_join_keys += 1 if joinable else 0

    warnings: list[str] = []
    if context_events and context_events_with_join_keys == 0:
        warnings.append(
            f"None of the {context_events} client_context events carry client_session_id or client_transcript_id, "
            "so imported usage cannot be attributed to them. Pass the real client session id to agentacct_attach_client_context."
        )
    if section_events and section_events_with_join_keys == 0:
        warnings.append(
            f"None of the {section_events} section events carry client_session_id or client_transcript_id. "
            "Call agentacct_attach_client_context with real ids first; sections recorded on the same MCP session inherit them."
        )
    if usage_rows and not section_events:
        warnings.append(
            f"{usage_rows} imported usage rows exist but no MCP work sections were recorded, so usage cannot be explained by work meaning."
        )
    # Join-key presence is only instrumentation readiness. It is not evidence
    # that the existing usage rows were actually matched or allocated. Reuse
    # the canonical bridge/ledger decision over the SAME full event set so one
    # good section cannot make a mostly-unjoined store report healthy.
    bridge = build_usage_context_bridge(events, detail_limit=1)
    context_match_coverage_ratio = bridge["context_match_coverage_ratio"]
    attribution_coverage_ratio = bridge["attribution_coverage_ratio"]
    degraded_reasons: list[str] = []
    if usage_rows_with_join_keys < usage_rows:
        degraded_reasons.append("usage_missing_join_keys")
    if context_events_with_join_keys < context_events:
        degraded_reasons.append("client_context_missing_join_keys")
    if section_events_with_join_keys < section_events:
        degraded_reasons.append("section_missing_join_keys")
    if usage_rows and int(bridge["context_matched_usage_records"]) < usage_rows:
        degraded_reasons.append("usage_without_matching_context")
    if usage_rows and int(bridge["attributed_usage_records"]) < usage_rows:
        degraded_reasons.append("usage_without_work_attribution")

    if degraded_reasons:
        health_status = "degraded"
    elif usage_rows:
        health_status = "healthy"
    elif context_events or section_events:
        health_status = "ready"
    else:
        health_status = "empty"
    # Backward-compatible instrumentation readiness: ``joinable`` has always
    # meant that at least one context/section carries a usable client id (or
    # that no context has been recorded yet).  Coverage completeness is a
    # stronger, additive signal and must not silently change that wire field.
    instrumentation_joinable = bool(
        (context_events_with_join_keys or section_events_with_join_keys)
        or (not context_events and not section_events)
    )
    return {
        "usage_rows": usage_rows,
        "excluded_non_additive_usage_rows": excluded_non_additive_usage_rows,
        "usage_rows_with_join_keys": usage_rows_with_join_keys,
        "usage_join_key_coverage_ratio": _coverage_ratio(usage_rows_with_join_keys, usage_rows),
        "context_events": context_events,
        "context_events_with_join_keys": context_events_with_join_keys,
        "client_context_join_key_coverage_ratio": _coverage_ratio(context_events_with_join_keys, context_events),
        "section_events": section_events,
        "section_events_with_join_keys": section_events_with_join_keys,
        "section_join_key_coverage_ratio": _coverage_ratio(section_events_with_join_keys, section_events),
        "context_matched_usage_rows": bridge["context_matched_usage_records"],
        "attributed_usage_rows": bridge["attributed_usage_records"],
        "context_match_coverage_ratio": context_match_coverage_ratio,
        "attribution_coverage_ratio": attribution_coverage_ratio,
        "health_status": health_status,
        "degraded_reasons": degraded_reasons,
        "joinable": instrumentation_joinable,
        "coverage_complete": health_status != "degraded",
        "warnings": warnings,
    }


def build_usage_context_bridge(events: list[dict[str, Any]], *, detail_limit: int | None = None) -> dict[str, Any]:
    """Join imported usage records with MCP semantic events using local client ids.

    The bridge is intentionally advisory. Usage records remain the token/cost
    source of truth; MCP events contribute human-readable context and join keys.
    """

    # Same read surface rules as the canonical ledger (usage_truth): agentacct's
    # own diagnostic tool events are split out at intake, and stale legacy
    # claude-code base rows shadowed by ':model:' siblings are excluded, so
    # the bridge and the ledger describe the identical usage-truth rows.
    events, _diagnostic_tool_events = split_diagnostic_events(events)
    kept_events, _shadowed = split_shadowed_legacy_usage_events(events)
    excluded_non_additive_usage_records = sum(
        1
        for event in kept_events
        if _is_usage_event(event) and local_usage_event_additivity(event)[0] is False
    )
    usage_records = [_usage_record(event) for event in kept_events if _is_usage_event(event)]
    context_events = [_context_event(event) for event in events if _is_context_event(event)]
    usage_records = [record for record in usage_records if record is not None]
    usage_records = annotate_usage_source_namespace_ambiguity(usage_records)
    context_events = [event for event in context_events if event is not None]

    # Client-log evidence: the same derived-only enrichment as the ledger.
    # Context events (attach/section/debug snapshots) group under their
    # evidenced session for bridge links and the unlinked-context table; the
    # bridge cap is untouched — a canonical-unjoined row still renders at
    # most context_only_client_context/medium, so an evidenced attach without
    # a section NEVER displays high. build_work_events shares the index and
    # applies its own work_id group guard for the canonical decisions.
    log_evidence_index = build_log_evidence_index(events)
    apply_log_evidence_to_snapshots(context_events, log_evidence_index)

    attributions = build_attributions(
        build_usage_events(events), build_work_events(events, log_evidence_index=log_evidence_index)
    )
    attributed_usage_ids = {
        str(attribution.get("usage_event_id"))
        for attribution in attributions
        if attribution.get("work_id") and attribution.get("join_confidence") != "unjoined" and attribution.get("usage_event_id")
    }
    attribution_by_usage_id = {
        str(attribution.get("usage_event_id")): attribution for attribution in attributions if attribution.get("usage_event_id")
    }
    links: list[dict[str, Any]] = []
    linked_context_ids: set[str] = set()
    for usage in usage_records:
        matches = []
        for context in context_events:
            match = pair_match(usage, context)
            if match is None or not match.get("join_keys"):
                # Conflict-vetoed pairs are not context matches: conflicting
                # evidence never earns a displayed join.
                continue
            matches.append({"context": context, "join_keys": match["join_keys"], "id_tiers": match["id_tiers"]})
        usage_event_id = str(usage.get("event_id") or "")
        if not matches:
            links.append({**usage, "join_confidence": "unjoined", "join_strategy": "unjoined", "join_keys": [], "context_event_count": 0, "section_count": 0, "usage_debug_count": 0, "attribution_status": "unjoined"})
            continue
        for match in matches:
            linked_context_ids.add(match["context"]["event_id"])
        contexts = [match["context"] for match in matches]
        join_keys = _ordered_unique(key for match in matches for key in match["join_keys"])
        canonical = attribution_by_usage_id.get(usage_event_id)
        canonical_strategy = str(canonical.get("join_strategy") or "unjoined") if canonical else "unjoined"
        if canonical is not None and canonical_strategy != "unjoined":
            # work_ledger is the canonical attribution model: every link whose
            # usage row received a canonical decision takes that decision's
            # strategy family and confidence verbatim, so the bridge can never
            # present a join above (or differently from) the ledger.
            join_strategy = _bridge_strategy_for_canonical(canonical_strategy)
            join_confidence = str(canonical.get("join_confidence") or "unjoined")
            join_reason = _JOIN_REASONS.get(join_strategy) or str(canonical.get("join_reason") or "usage and MCP context share a join hint")
        else:
            # No section-level decision exists (client_context/debug-only
            # matches): the canonical model says this usage row is UNJOINED,
            # so the displayed link is capped at medium no matter how strong
            # the raw id provenance is. An unattributed row must never show
            # exact/high anywhere — that vocabulary is reserved for real
            # attributions the ledger agrees with.
            join_strategy, join_confidence = _pair_level_link(matches)
            if any(match.get("id_tiers") for match in matches) or JOIN_RANK.get(join_confidence, 0) > JOIN_RANK["medium"]:
                join_strategy, join_confidence = "context_only_client_context", "medium"
            join_reason = _JOIN_REASONS.get(join_strategy, "usage and MCP context share a generic join hint")
        links.append(
            {
                **usage,
                "join_confidence": join_confidence,
                "join_strategy": join_strategy,
                "join_reason": join_reason,
                "join_keys": join_keys,
                "context_event_count": len(contexts),
                "context_matched": True,
                "attribution_status": "attributed" if usage_event_id in attributed_usage_ids else "context_matched_unallocated",
                "client_context_count": sum(1 for context in contexts if context["semantic_kind"] == "client_context"),
                "section_count": sum(1 for context in contexts if context["semantic_kind"] == "section"),
                "usage_debug_count": sum(1 for context in contexts if context["semantic_kind"] == "agent_usage_debug"),
                "sections": _section_summaries(contexts),
                "latest_usage_debug": _latest_usage_debug(contexts),
                "latest_context_summary": _latest_summary(contexts),
                "context_event_ids": [context["event_id"] for context in contexts],
            }
        )
    unlinked_contexts = [context for context in context_events if context["event_id"] not in linked_context_ids]
    links.sort(key=lambda link: (int(link.get("context_event_count") or 0), int(link.get("total_tokens_including_cached") or 0)), reverse=True)
    context_matched_usage_records = sum(1 for link in links if int(link.get("context_event_count") or 0) > 0)
    attributed_usage_records = sum(1 for link in links if link.get("attribution_status") == "attributed")
    context_match_coverage_ratio = _coverage_ratio(context_matched_usage_records, len(usage_records))
    attribution_coverage_ratio = _coverage_ratio(attributed_usage_records, len(usage_records))
    context_link_coverage_ratio = _coverage_ratio(len(linked_context_ids), len(context_events))
    degraded_reasons: list[str] = []
    if usage_records and context_matched_usage_records < len(usage_records):
        degraded_reasons.append("usage_without_matching_context")
    if usage_records and attributed_usage_records < len(usage_records):
        degraded_reasons.append("usage_without_work_attribution")
    if degraded_reasons:
        health_status = "degraded"
    elif usage_records:
        health_status = "healthy"
    elif context_events:
        health_status = "waiting_for_usage"
    else:
        health_status = "empty"

    if detail_limit is None:
        returned_links = links
        returned_attributions = attributions
        unlinked_limit = 50
        exposed_limit: int | None = None
    else:
        normalized_limit = max(0, int(detail_limit))
        returned_links = links[:normalized_limit]
        returned_attributions = attributions[:normalized_limit]
        unlinked_limit = min(50, normalized_limit)
        exposed_limit = normalized_limit
    returned_unlinked_contexts = [_compact_context(context) for context in unlinked_contexts[:unlinked_limit]]
    detail_partial = (
        len(returned_links) < len(links)
        or len(returned_attributions) < len(attributions)
        or len(returned_unlinked_contexts) < len(unlinked_contexts)
    )
    return {
        "usage_records": len(usage_records),
        "excluded_non_additive_usage_records": excluded_non_additive_usage_records,
        "context_events": len(context_events),
        "context_matched_usage_records": context_matched_usage_records,
        "attributed_usage_records": attributed_usage_records,
        "linked_usage_records": attributed_usage_records,
        "unlinked_context_events": len(unlinked_contexts),
        "context_match_coverage_ratio": context_match_coverage_ratio,
        "attribution_coverage_ratio": attribution_coverage_ratio,
        "context_link_coverage_ratio": context_link_coverage_ratio,
        "health_status": health_status,
        "degraded_reasons": degraded_reasons,
        "links": returned_links,
        "attributions": returned_attributions,
        "unlinked_contexts": returned_unlinked_contexts,
        "detail_scope": {
            "partial": detail_partial,
            "limit": exposed_limit,
            "links_returned": len(returned_links),
            "links_total": len(links),
            "attributions_returned": len(returned_attributions),
            "attributions_total": len(attributions),
            "unlinked_contexts_returned": len(returned_unlinked_contexts),
            "unlinked_contexts_total": len(unlinked_contexts),
        },
    }


def _coverage_ratio(covered: int, total: int) -> float | None:
    """Return an explicit coverage ratio; no denominator is reported as null."""

    if total <= 0:
        return None
    return covered / total


def _is_usage_event(event: dict[str, Any]) -> bool:
    return is_local_usage_import_event(event)


def _is_context_event(event: dict[str, Any]) -> bool:
    metadata = _metadata(event)
    kind = metadata.get("sentinel_semantic_kind")
    if isinstance(kind, str) and kind in SEMANTIC_KINDS:
        return True
    # Mirror build_work_events and join-health intake for pre-semantic-marker
    # stores: a real section_* event with a section id remains MCP work context.
    return (
        kind not in SEMANTIC_KINDS
        and str(event.get("event_type") or "").startswith("section_")
        and _optional_str(metadata.get("section_id")) is not None
    )


def _usage_record(event: dict[str, Any]) -> dict[str, Any] | None:
    usage_additive, _state = local_usage_event_additivity(event)
    if not usage_additive:
        return None
    metadata = _metadata(event)
    client = _optional_str(metadata.get("client"))
    session = _optional_str(metadata.get("client_session_id"))
    # Read-time base normalization, byte-identical to work_ledger._usage_event
    # and the dashboard: legacy claude-code ':model:' keys join by the TRUE
    # session id on un-migrated stores, so ledger and bridge cannot disagree.
    if session is not None:
        session = normalized_local_usage_session_id(metadata.get("client"), session)
    return {
        "event_id": _optional_str(event.get("event_id")),
        "run_id": _optional_str(event.get("run_id")),
        "client": client,
        "client_session_id": session,
        "client_transcript_id": _optional_str(metadata.get("client_transcript_id")),
        "source_namespace_fingerprint": _optional_str(
            metadata.get("source_namespace_fingerprint")
        ),
        "parent_client_session_id": _optional_str(metadata.get("parent_client_session_id")),
        "client_session_kind": _optional_str(metadata.get("client_session_kind")) or "root",
        "provider": _optional_str(event.get("provider")),
        "model": _optional_str(event.get("model")),
        "total_tokens_including_cached": _safe_int(event.get("estimated_input_tokens"))
        + _safe_int(event.get("estimated_output_tokens"))
        + _safe_int(metadata.get("cached_input_tokens")),
        "estimated_cost_usd": _safe_float(event.get("estimated_cost_usd")),
        "usage_confidence": _optional_str(event.get("usage_confidence")),
        "cost_confidence": _optional_str(event.get("cost_confidence")),
    }


def _context_event(event: dict[str, Any]) -> dict[str, Any] | None:
    metadata = _metadata(event)
    semantic_kind = _optional_str(metadata.get("sentinel_semantic_kind"))
    if semantic_kind not in SEMANTIC_KINDS:
        if not (
            str(event.get("event_type") or "").startswith("section_")
            and _optional_str(metadata.get("section_id")) is not None
        ):
            return None
        semantic_kind = "section"
    inherited_join_keys = metadata.get("client_context_inherited_keys")
    return {
        "event_id": _optional_str(event.get("event_id")) or "",
        "created_at": _safe_float(event.get("created_at")),
        "run_id": _optional_str(event.get("run_id")),
        "source": _optional_str(event.get("source")),
        "event_type": _optional_str(event.get("event_type")),
        "semantic_kind": semantic_kind,
        "client": _optional_str(metadata.get("client")),
        "client_session_id": _optional_str(metadata.get("client_session_id")),
        "client_transcript_id": _optional_str(metadata.get("client_transcript_id")),
        "parent_client_session_id": _optional_str(metadata.get("parent_client_session_id")),
        "inherited_join_keys": [item for item in inherited_join_keys if isinstance(item, str)] if isinstance(inherited_join_keys, list) else [],
        "authored_join_keys": [item for item in metadata.get("client_context_keys_authored") or [] if isinstance(item, str)]
        if isinstance(metadata.get("client_context_keys_authored"), list)
        else [],
        "client_context_source": _optional_str(metadata.get("client_context_source")),
        "turn_id": _optional_str(metadata.get("turn_id")),
        "message_id": _optional_str(metadata.get("message_id")),
        "request_id": _optional_str(metadata.get("request_id")),
        "section_id": _optional_str(metadata.get("section_id")),
        "section_status": _optional_str(metadata.get("section_status")),
        "section_title": _optional_str(metadata.get("section_title")),
        "phase": _optional_str(metadata.get("phase")),
        "summary": _optional_str(metadata.get("summary")),
        "reporting_basis": _optional_str(metadata.get("reporting_basis")),
    }


_JOIN_REASONS = {
    "exact_client_session_id": "usage and MCP context share client_session_id",
    "exact_client_transcript_id": "usage and MCP context share client_transcript_id",
    "client_derived_client_context": "usage and MCP context share ids captured from the Claude Code hook; client-derived but not session-bound, so the join is high confidence rather than exact",
    "client_log_evidenced_client_context": "usage and MCP context share a session id evidenced by the client's own session log (creation-response pairing at import time); client-derived but paired post-hoc, so the join is high confidence rather than exact",
    "inherited_client_context": "usage and MCP context share ids the section inherited from attach context; freshness is unproven so the join is not exact",
    "unverified_client_context": "usage and MCP context share ids whose provenance cannot be verified (recorded before provenance stamping or via a generic recording path); not eligible for exact",
    "context_only_client_context": "usage shares client ids with attach/debug context only — no work section received this usage, so the canonical ledger treats it as unattributed and the displayed link is capped at medium",
    "exact_run_id_grouping_hint": "usage and MCP context share agentacct run_id only; run_id groups events but never attributes usage",
    "parent_child_context_hint": "child/parent session relationship groups this usage with the section's session; agentacct does not allocate section-level usage across sessions",
    "manual": "usage and MCP context share a generic join hint",
}


def _bridge_strategy_for_canonical(strategy: str) -> str:
    """Map a canonical join strategy to the bridge's link strategy vocabulary."""

    if "ambiguous" in strategy:
        return strategy
    if strategy.startswith("client_derived_"):
        return "client_derived_client_context"
    if strategy.startswith("client_log_evidenced_"):
        return "client_log_evidenced_client_context"
    if strategy.startswith("inherited_"):
        return "inherited_client_context"
    if strategy.startswith("unverified_"):
        return "unverified_client_context"
    return strategy


def _pair_level_link(matches: list[dict[str, Any]]) -> tuple[str, str]:
    """Best provenance tier across context-only matches (no canonical decision)."""

    tiers = {tier for match in matches for tier in (match.get("id_tiers") or {}).values()}
    keys = {key for match in matches for key in match["join_keys"]}
    if "explicit" in tiers:
        if any((match.get("id_tiers") or {}).get("client_session_id") == "explicit" for match in matches):
            return "exact_client_session_id", TIER_CONFIDENCE["explicit"]
        return "exact_client_transcript_id", TIER_CONFIDENCE["explicit"]
    if "hook" in tiers:
        return "client_derived_client_context", TIER_CONFIDENCE["hook"]
    if "unverified" in tiers:
        return "unverified_client_context", TIER_CONFIDENCE["unverified"]
    if "attach" in tiers:
        return "inherited_client_context", TIER_CONFIDENCE["attach"]
    if "run_id" in keys:
        return "exact_run_id_grouping_hint", "low"
    if "parent_client_session_id" in keys:
        return "parent_child_context_hint", "low"
    return "manual", "low"


def _section_summaries(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sections = [context for context in contexts if context["semantic_kind"] == "section"]
    sections.sort(key=lambda context: context.get("created_at") or 0)
    return [
        {
            "section_id": context.get("section_id"),
            "section_status": context.get("section_status"),
            "section_title": context.get("section_title"),
            "phase": context.get("phase"),
            "summary": context.get("summary"),
        }
        for context in sections[:10]
    ]


def _latest_usage_debug(contexts: list[dict[str, Any]]) -> dict[str, Any] | None:
    debug_events = [context for context in contexts if context["semantic_kind"] == "agent_usage_debug"]
    if not debug_events:
        return None
    latest = max(debug_events, key=lambda context: context.get("created_at") or 0)
    return {
        "reporting_basis": latest.get("reporting_basis"),
        "summary": latest.get("summary"),
    }


def _latest_summary(contexts: list[dict[str, Any]]) -> str | None:
    summaries = [context for context in contexts if context.get("summary")]
    if not summaries:
        return None
    latest = max(summaries, key=lambda context: context.get("created_at") or 0)
    return latest.get("summary")


def _compact_context(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": context.get("event_id"),
        "semantic_kind": context.get("semantic_kind"),
        "event_type": context.get("event_type"),
        "client": context.get("client"),
        "client_session_id": context.get("client_session_id"),
        "section_id": context.get("section_id"),
        "reporting_basis": context.get("reporting_basis"),
        "summary": context.get("summary"),
    }


def _metadata(event: dict[str, Any]) -> dict[str, Any]:
    metadata = event.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _safe_int(value: Any) -> int:
    try:
        number = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _ordered_unique(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
