"""Dedup-safe, additive cross-store event merge planning.

Global mode gives one machine-wide store, but MCP work context already recorded
in per-project stores stays stranded there. `usage merge-store` folds a source
store's events into a target store WITHOUT rewriting existing target rows.
Incoming rows are copied verbatim except when a trusted usage or observation
truth row collides with an unlike existing event_id. In that narrow case the
incoming truth row receives a deterministic server-reminted id so neither fact
is lost. The critical payoff: because ordinary section event_ids are preserved,
the Phase 2.7 client-log-evidence join re-links merged sections to the usage
sessions that already reference those ids in the target store.

This module holds the PURE planning logic (which events to copy, counts by
type); the append itself is `SentinelService.append_events_preserving_identity`.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any, Literal

from .usage_truth import (
    is_local_session_observation_event,
    is_local_usage_import_event,
    local_session_observation_event_key,
    local_session_observation_revision,
    local_session_observation_source_watermark,
    local_usage_row_identity,
    reduce_local_session_observation_events,
)

MergeKind = Literal["all", "mcp"]

MERGE_KINDS = ("all", "mcp")

UsageIdentity = tuple[str, str, str]
UsageNamespace = str | None

_OBSERVATION_CONFLICT_SKIP_REASONS = frozenset(
    {
        "source_namespace_conflict",
        "same_watermark_conflict",
        "source_watermark_unorderable",
    }
)


def _observation_reducer_conflict_reason(diagnostics: dict[str, int]) -> str:
    if int(diagnostics.get("namespace_conflict_sessions") or 0):
        return "source_namespace_conflict"
    if int(diagnostics.get("watermark_conflict_sessions") or 0):
        return "same_watermark_conflict"
    return "source_watermark_unorderable"


def _non_authoritative_observation_reason(
    row: dict[str, Any],
    authority: dict[str, Any] | None,
    diagnostics: dict[str, int],
) -> str:
    if authority is None:
        return _observation_reducer_conflict_reason(diagnostics)
    if local_session_observation_revision(row) == local_session_observation_revision(
        authority
    ):
        return "idempotent_revision"
    row_watermark = local_session_observation_source_watermark(row)
    authority_watermark = local_session_observation_source_watermark(authority)
    if (
        row_watermark is not None
        and authority_watermark is not None
        and row_watermark < authority_watermark
    ):
        return "historical_revision"
    # The trusted reducer should only select an authority when every other
    # different source revision is strictly older. Keep a neutral fail-closed
    # bucket for future reducer changes instead of mislabeling it a conflict.
    return "non_authoritative_revision"


class MergeTargetIdentities(set[UsageIdentity]):
    """Backward-compatible usage identities plus trusted observation rows.

    ``usage_row_identities`` predates the observation lane and is already the
    value passed by the CLI into ``plan_store_merge``.  Keeping it a ``set``
    preserves that public contract while carrying the target observation rows
    needed for revision-aware cross-store deduplication.
    """

    def __init__(
        self,
        values: set[UsageIdentity] | None = None,
        *,
        session_observations: list[dict[str, Any]] | None = None,
        usage_namespaces_by_identity: dict[
            UsageIdentity, set[UsageNamespace]
        ]
        | None = None,
        target_events_by_id: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(values or set())
        self.session_observations = list(session_observations or [])
        self.usage_namespaces_by_identity = {
            identity: set(namespaces)
            for identity, namespaces in (usage_namespaces_by_identity or {}).items()
        }
        self.target_events_by_id = dict(target_events_by_id or {})


def _event_id(event: dict[str, Any]) -> str | None:
    event_id = event.get("event_id")
    if isinstance(event_id, str) and event_id:
        return event_id
    return None


def _usage_source_namespace(event: dict[str, Any]) -> UsageNamespace:
    metadata = event.get("metadata")
    if not isinstance(metadata, dict):
        return None
    namespace = metadata.get("source_namespace_fingerprint")
    if not isinstance(namespace, str) or not namespace.strip():
        return None
    return namespace.strip()


def _observation_source_revision_key(
    event: dict[str, Any],
) -> tuple[str | None, str]:
    metadata = event.get("metadata")
    raw_source = (
        metadata.get("source_namespace_fingerprint")
        if isinstance(metadata, dict)
        else None
    )
    source = (
        raw_source.strip()
        if isinstance(raw_source, str) and raw_source.strip()
        else None
    )
    return (source, local_session_observation_revision(event))


def usage_row_identities(events: list[dict[str, Any]]) -> MergeTargetIdentities:
    """Target merge identities derived from a batch of events.

    A usage-truth row's event_id is minted fresh on every import, so the SAME
    logical client session imported into two different stores lands with two
    different event_ids. Dedup by event_id alone (as `--kind all` used to) would
    copy that row in and DOUBLE-COUNT its tokens/cost. The returned object is a
    set of those logical usage identities for backward compatibility. It also
    carries trusted local session-observation rows so the planner can compare
    their source revision across stores. Other non-usage (MCP work) events are
    ignored here and keep pure event_id dedup, since their event_id IS their
    stable identity (the client-log-evidence join depends on it).
    """

    identities: set[UsageIdentity] = set()
    session_observations: list[dict[str, Any]] = []
    usage_namespaces_by_identity: dict[
        UsageIdentity, set[UsageNamespace]
    ] = {}
    target_events_by_id: dict[str, dict[str, Any]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        if (event_id := _event_id(event)) is not None:
            target_events_by_id[event_id] = event
        if is_local_session_observation_event(event):
            session_observations.append(event)
            continue
        if is_local_usage_import_event(event):
            identity = local_usage_row_identity(event)
            if identity is not None:
                identities.add(identity)
                usage_namespaces_by_identity.setdefault(identity, set()).add(
                    _usage_source_namespace(event)
                )
    return MergeTargetIdentities(
        identities,
        session_observations=session_observations,
        usage_namespaces_by_identity=usage_namespaces_by_identity,
        target_events_by_id=target_events_by_id,
    )


def _remint_conflicting_truth_event(
    event: dict[str, Any],
    unavailable_ids: set[str],
) -> dict[str, Any]:
    """Preserve trusted truth when an unrelated row pre-occupies its event id."""

    material = json.dumps(
        event,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    for attempt in range(32):
        digest = hashlib.sha256(
            f"merge-truth-conflict:{attempt}:{material}".encode("utf-8")
        ).hexdigest()
        candidate_id = "evt_" + digest[:12]
        if candidate_id not in unavailable_ids:
            cloned = dict(event)
            cloned["event_id"] = candidate_id
            return cloned
    raise ValueError("unable to mint a collision-free truth event id")


def is_mcp_work_event(event: dict[str, Any]) -> bool:
    """True for MCP/work-context events, excluding both local truth lanes.

    Sections, machine checks, client-context attaches, agent usage-debug,
    artifacts, and notes qualify. Imported ``model_usage`` rows and trusted
    local ``session_observed`` rows do not: both are independently discovered
    from client logs and belong in ``--kind all``, not an MCP-only merge.
    """

    return not (
        is_local_usage_import_event(event)
        or is_local_session_observation_event(event)
    )


def plan_store_merge(
    source_events: list[dict[str, Any]],
    target_event_ids: set[str],
    *,
    kind: MergeKind = "all",
    target_usage_identities: set[UsageIdentity] | None = None,
) -> dict[str, Any]:
    """Decide which source events to copy into the target.

    Returns a report dict:
      - ``events_to_add``: the source events to append (verbatim), in order.
      - ``added_by_type`` / ``skipped_existing_by_type`` /
        ``filtered_out_by_type`` / ``skipped_duplicate_usage_by_type`` /
        ``skipped_duplicate_observation_by_type``: event_type -> count.
      - ``skipped_observation_by_reason`` separates harmless idempotent or
        historical revisions from quarantined reducer conflicts.
      - ``observation_reducer_diagnostics`` exposes identity-cohort outcomes
        (session counts), while skip-reason counts describe source rows.
      - scalar totals: ``source_events``, ``add_count``,
        ``skipped_existing_count``, ``filtered_out_count``,
        ``skipped_duplicate_usage_count``,
        ``skipped_duplicate_observation_count``, ``no_event_id_count``.

    A source event is:
      - filtered out if ``kind == "mcp"`` and it is a local usage-truth or
        trusted local session-observation row;
      - skipped as a duplicate USAGE row (``kind == "all"`` only) if it is a
        usage-truth row whose LOGICAL identity (client, base session, lane) —
        NOT its event_id — already exists in the target or earlier in the
        source. Usage event_ids are minted fresh per import, so the same logical
        session imported into both stores carries different ids; deduping on
        event_id alone would copy it in and double-count tokens/cost. This
        logical dedup closes that footgun. (``kind == "mcp"`` filters all usage
        rows out anyway, so this branch only fires for ``all``.)
        The same logical identity from a DIFFERENT explicit source namespace
        is preserved as an independent source fact. Missing-vs-explicit
        provenance is also preserved so read-time truth can quarantine the
        complete ambiguous cohort instead of trusting whichever row arrived
        first; two legacy missing-namespace rows retain historical idempotent
        behavior.
      - skipped if its event_id already exists in the target (dedup) or is
        duplicated within the source, except that an unlike trusted usage or
        observation truth row is deterministically reminted when target event
        content is available;
      - otherwise added.
      - for trusted local session observations under ``kind == "all"``, the
        shared observation reducer compares the raw client/session identity,
        source namespace, source watermark, and canonical content revision
        across target and source. A strictly authoritative source row is
        added; idempotent and historical revisions are skipped. Conflicting
        raw revisions are preserved so the complete cohort becomes
        unprojectable at read time rather than leaving a first-writer row
        trusted. This prevents replay growth and unsafe identity joining.
    Other non-usage (MCP work) events always keep pure event_id dedup — their
    event_id is their stable identity, which the client-log-evidence join
    depends on. Additive only: the target's own rows are never touched.
    """

    if kind not in MERGE_KINDS:
        raise ValueError(f"unsupported merge kind: {kind!r}")

    target_observations = (
        list(target_usage_identities.session_observations)
        if isinstance(target_usage_identities, MergeTargetIdentities)
        else []
    )
    target_usage_namespaces_by_identity = (
        {
            identity: set(namespaces)
            for identity, namespaces in target_usage_identities.usage_namespaces_by_identity.items()
        }
        if isinstance(target_usage_identities, MergeTargetIdentities)
        else {
            identity: {None}
            for identity in (target_usage_identities or set())
        }
    )
    target_events_by_id = (
        dict(target_usage_identities.target_events_by_id)
        if isinstance(target_usage_identities, MergeTargetIdentities)
        else {}
    )
    target_usage_identities = set(target_usage_identities or set())

    events_to_add: list[dict[str, Any]] = []
    added_by_type: Counter[str] = Counter()
    skipped_existing_by_type: Counter[str] = Counter()
    filtered_out_by_type: Counter[str] = Counter()
    skipped_duplicate_usage_by_type: Counter[str] = Counter()
    skipped_usage_namespace_ambiguous_by_type: Counter[str] = Counter()
    skipped_duplicate_observation_by_type: Counter[str] = Counter()
    skipped_observation_by_reason: Counter[str] = Counter()
    observation_reducer_diagnostics: Counter[str] = Counter()
    no_event_id_count = 0
    seen_in_source: set[str] = set()
    seen_usage_identities: set[UsageIdentity] = set(target_usage_identities)
    seen_usage_namespaces_by_identity: dict[
        UsageIdentity, set[UsageNamespace]
    ] = target_usage_namespaces_by_identity
    preserved_cross_namespace_usage_count = 0
    preserved_ambiguous_usage_namespace_count = 0
    preserved_observation_conflict_count = 0
    reminted_truth_event_id_conflicts = 0
    seen_source_events_by_id: dict[str, dict[str, Any]] = {}

    # Decide observation authority once over complete identity cohorts rather
    # than incrementally in source order.  Incremental comparison could accept
    # an older row before seeing a later conflict.  Object identity is used
    # deliberately: two source rows may share an event_id but have different
    # content, and the trusted reducer must see both before ordinary event-id
    # dedup runs.
    target_observations_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in target_observations:
        key = local_session_observation_event_key(event)
        if key is not None:
            target_observations_by_key.setdefault(key, []).append(event)
    source_observations_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    if kind == "all":
        for event in source_events:
            if not isinstance(event, dict) or not is_local_session_observation_event(event):
                continue
            key = local_session_observation_event_key(event)
            if key is not None:
                source_observations_by_key.setdefault(key, []).append(event)

    selected_source_observation_objects: set[int] = set()
    observation_skip_reason_by_object: dict[int, str] = {}
    for key, source_rows in source_observations_by_key.items():
        target_rows = target_observations_by_key.get(key, [])
        selected, diagnostics = reduce_local_session_observation_events(
            [*target_rows, *source_rows]
        )
        observation_reducer_diagnostics.update(diagnostics)
        authority = selected[0] if selected else None
        if authority is None:
            # Preserve every new raw source revision that proves the conflict.
            # Keeping only the target/first row would let it remain a trusted
            # read-time authority after the merge reported a collision.
            seen_conflict_revisions = {
                _observation_source_revision_key(row) for row in target_rows
            }
            for row in source_rows:
                revision_key = _observation_source_revision_key(row)
                if revision_key in seen_conflict_revisions:
                    observation_skip_reason_by_object[id(row)] = (
                        "idempotent_revision"
                    )
                    continue
                seen_conflict_revisions.add(revision_key)
                selected_source_observation_objects.add(id(row))
                preserved_observation_conflict_count += 1
            continue
        if authority is not None and any(authority is row for row in source_rows):
            selected_source_observation_objects.add(id(authority))
        for row in source_rows:
            if authority is row:
                continue
            observation_skip_reason_by_object[id(row)] = (
                _non_authoritative_observation_reason(row, authority, diagnostics)
            )

    for event in source_events:
        if not isinstance(event, dict):
            no_event_id_count += 1
            continue
        event_type = str(event.get("event_type") or "unknown")
        is_usage = is_local_usage_import_event(event)
        is_observation = is_local_session_observation_event(event)
        if kind == "mcp" and (is_usage or is_observation):
            filtered_out_by_type[event_type] += 1
            continue
        if is_observation and id(event) not in selected_source_observation_objects:
            skipped_duplicate_observation_by_type[event_type] += 1
            skipped_observation_by_reason[
                observation_skip_reason_by_object.get(
                    id(event),
                    "non_authoritative_revision",
                )
            ] += 1
            continue
        planned_event = event
        event_id_reminted = False
        event_id = _event_id(planned_event)
        if event_id is None:
            no_event_id_count += 1
            continue
        if event_id in target_event_ids or event_id in seen_in_source:
            existing_collision = target_events_by_id.get(
                event_id
            ) or seen_source_events_by_id.get(event_id)
            if (
                (is_usage or is_observation)
                and existing_collision is not None
                and existing_collision != event
            ):
                planned_event = _remint_conflicting_truth_event(
                    event,
                    set(target_event_ids) | seen_in_source,
                )
                event_id = str(planned_event["event_id"])
                event_id_reminted = True
            else:
                skipped_existing_by_type[event_type] += 1
                continue
        if is_usage:
            # kind == "all" here (kind == "mcp" filtered usage above).
            identity = local_usage_row_identity(planned_event)
            if identity is not None and identity in seen_usage_identities:
                source_namespace = _usage_source_namespace(planned_event)
                existing_namespaces = seen_usage_namespaces_by_identity.setdefault(
                    identity,
                    {None},
                )
                if source_namespace in existing_namespaces:
                    skipped_duplicate_usage_by_type[event_type] += 1
                    continue
                if source_namespace is None or None in existing_namespaces:
                    # Preserve both sides so read-time truth can quarantine the
                    # complete missing-vs-explicit cohort. Skipping incoming
                    # while keeping the target would silently leave a
                    # first-writer usage row additive.
                    existing_namespaces.add(source_namespace)
                    preserved_ambiguous_usage_namespace_count += 1
                else:
                    # Both sides carry explicit, different homes. Preserve the
                    # source row so downstream namespace-aware projection can
                    # quarantine or keep the homes independent; calling it a
                    # duplicate here would silently delete real provenance.
                    existing_namespaces.add(source_namespace)
                    preserved_cross_namespace_usage_count += 1
            if identity is not None:
                seen_usage_identities.add(identity)
                seen_usage_namespaces_by_identity.setdefault(identity, set()).add(
                    _usage_source_namespace(event)
                )
        if event_id_reminted:
            reminted_truth_event_id_conflicts += 1
        seen_in_source.add(event_id)
        seen_source_events_by_id[event_id] = planned_event
        events_to_add.append(planned_event)
        added_by_type[event_type] += 1

    return {
        "kind": kind,
        "source_events": len(source_events),
        "events_to_add": events_to_add,
        "add_count": len(events_to_add),
        "skipped_existing_count": sum(skipped_existing_by_type.values()),
        "filtered_out_count": sum(filtered_out_by_type.values()),
        "skipped_duplicate_usage_count": sum(skipped_duplicate_usage_by_type.values()),
        "skipped_usage_namespace_ambiguous_count": sum(
            skipped_usage_namespace_ambiguous_by_type.values()
        ),
        "preserved_cross_namespace_usage_count": (
            preserved_cross_namespace_usage_count
        ),
        "preserved_ambiguous_usage_namespace_count": (
            preserved_ambiguous_usage_namespace_count
        ),
        "skipped_duplicate_observation_count": sum(
            skipped_duplicate_observation_by_type.values()
        ),
        "observation_conflict_count": preserved_observation_conflict_count + sum(
            count
            for reason, count in skipped_observation_by_reason.items()
            if reason in _OBSERVATION_CONFLICT_SKIP_REASONS
        ),
        "preserved_observation_conflict_count": (
            preserved_observation_conflict_count
        ),
        "reminted_truth_event_id_conflicts": (
            reminted_truth_event_id_conflicts
        ),
        "observation_non_conflict_skip_count": sum(
            count
            for reason, count in skipped_observation_by_reason.items()
            if reason not in _OBSERVATION_CONFLICT_SKIP_REASONS
        ),
        "no_event_id_count": no_event_id_count,
        "added_by_type": dict(added_by_type),
        "skipped_existing_by_type": dict(skipped_existing_by_type),
        "filtered_out_by_type": dict(filtered_out_by_type),
        "skipped_duplicate_usage_by_type": dict(skipped_duplicate_usage_by_type),
        "skipped_usage_namespace_ambiguous_by_type": dict(
            skipped_usage_namespace_ambiguous_by_type
        ),
        "skipped_duplicate_observation_by_type": dict(
            skipped_duplicate_observation_by_type
        ),
        "skipped_observation_by_reason": dict(skipped_observation_by_reason),
        "observation_reducer_diagnostics": dict(observation_reducer_diagnostics),
    }
