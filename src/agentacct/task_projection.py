"""Pure session-first Task projection for product views.

This module deliberately does not know about HTML, FastAPI, or the agentacct
store.  It projects the existing work-ledger session/work shapes into Tasks:

* a client session's transitive lineage root is the default Task boundary;
* explicit continuation memberships may join multiple roots into one Task;
* ``run_id`` is retained only as metadata inside the resulting Task;
* id-less work may join a Task through candidate-session evidence only when
  every candidate resolves to that same Task.  This never invents an exact
  work-to-session attribution.

The function accepts plain mappings and returns JSON-serializable mappings so
the dashboard can integrate it without coupling the projection to templates.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from .join_rules import namespace_join_compatible


TASK_PROJECTION_SCHEMA_VERSION = "agent-chronicle.task-projection.v1"

SessionKey = tuple[str, str]
TaskKey = tuple[str, ...]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _session_key(value: Mapping[str, Any]) -> SessionKey | None:
    client = _text(value.get("client"))
    session_id = _text(
        value.get("client_session_id")
        or value.get("session_id")
        or value.get("root_session_id")
    )
    return (client, session_id) if client and session_id else None


def _work_session_key(value: Mapping[str, Any]) -> SessionKey | None:
    client = _text(value.get("client") or value.get("reporting_source"))
    session_id = _text(value.get("client_session_id"))
    return (client, session_id) if client and session_id else None


def _parent_key(value: Mapping[str, Any]) -> SessionKey | None:
    client = _text(value.get("client"))
    related = value.get("related") if isinstance(value.get("related"), Mapping) else {}
    parent = related.get("parent") if isinstance(related.get("parent"), Mapping) else {}
    parent_id = _text(parent.get("client_session_id") or value.get("parent_client_session_id"))
    return (client, parent_id) if client and parent_id else None


def _namespace_fingerprint(value: Mapping[str, Any]) -> str:
    return _text(
        value.get("namespace_fingerprint")
        or value.get("session_namespace_fingerprint")
    )


def _lineage_namespaces_compatible(
    child: Mapping[str, Any], parent: Mapping[str, Any]
) -> bool:
    """Return whether a same-client parent edge is safe to resolve.

    A fingerprint is an explicit namespace assertion.  It can only join the
    same fingerprint: an explicit/missing pair is ambiguous, not evidence of
    identity.  Rows from the legacy unscoped projection carry neither
    fingerprint and remain compatible with each other for backwards
    compatibility.  An internally inconsistent ``explicit`` row with no
    fingerprint fails closed as well.
    """

    child_fingerprint = _namespace_fingerprint(child)
    parent_fingerprint = _namespace_fingerprint(parent)
    if child_fingerprint or parent_fingerprint:
        return bool(
            child_fingerprint
            and parent_fingerprint
            and child_fingerprint == parent_fingerprint
        )
    return not (
        _text(child.get("identity_scope_state")) == "explicit"
        or _text(parent.get("identity_scope_state")) == "explicit"
    )


def _lineage_source_namespaces_compatible(
    child: Mapping[str, Any], parent: Mapping[str, Any]
) -> bool:
    """Validate child/parent import-source identity.

    Both session-source fingerprints missing preserves historical lineage.
    Once either side asserts one, both must be present and equal. The child's
    expected-parent fingerprint, when recorded, must also equal the parent's
    source. This makes explicit/missing fail closed in both directions.
    """

    child_source = _text(child.get("source_namespace_fingerprint"))
    parent_source = _text(parent.get("source_namespace_fingerprint"))
    expected_parent = _text(child.get("parent_source_namespace_fingerprint"))
    if not child_source and not parent_source:
        return not expected_parent
    if not child_source or not parent_source or child_source != parent_source:
        return False
    return not expected_parent or expected_parent == parent_source


def _log_evidenced_work_source_namespace_compatible(
    session: Mapping[str, Any],
    work: Mapping[str, Any],
) -> bool:
    """Require post-hoc log evidence to stay inside its client data home."""

    local_observation = (
        session.get("local_client_observation")
        if isinstance(session.get("local_client_observation"), Mapping)
        else {}
    )
    usage = session.get("usage") if isinstance(session.get("usage"), Mapping) else {}
    observation_only = bool(
        _integer(local_observation.get("observation_count")) > 0
        and _integer(usage.get("rows")) == 0
    )
    evidenced = _text(
        work.get("log_evidenced_source_namespace_fingerprint")
        or work.get("source_namespace_fingerprint")
    )
    if not evidenced:
        return not observation_only
    return _text(session.get("source_namespace_fingerprint")) == evidenced


def _session_freshness(value: Mapping[str, Any]) -> tuple[float, float, float]:
    return (
        _number(value.get("last_activity_at")),
        _number(value.get("updated_at")),
        _number(value.get("first_activity_at")),
    )


def _dedupe_sessions(
    session_rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[SessionKey, dict[str, Any]], int]:
    """Keep one canonical row per session identity.

    The session rollup normally already guarantees this.  The guard matters
    when a caller combines projections or repeats a row while assembling a
    Task: usage from that session must still be counted once.
    """

    by_key: dict[SessionKey, dict[str, Any]] = {}
    duplicate_count = 0
    for row in session_rows:
        if not isinstance(row, Mapping):
            continue
        key = _session_key(row)
        if key is None:
            continue
        candidate = dict(row)
        previous = by_key.get(key)
        if previous is None:
            by_key[key] = candidate
            continue
        duplicate_count += 1
        if _session_freshness(candidate) >= _session_freshness(previous):
            by_key[key] = candidate
    return by_key, duplicate_count


def _resolve_roots(
    sessions: Mapping[SessionKey, Mapping[str, Any]],
) -> tuple[dict[SessionKey, SessionKey], dict[SessionKey, str]]:
    """Resolve parent chains transitively, refusing unsafe edges and cycles."""

    parent_of = {key: _parent_key(row) for key, row in sessions.items()}
    resolved: dict[SessionKey, SessionKey] = {}
    states: dict[SessionKey, str] = {}
    for start in sorted(sessions):
        if start in resolved:
            continue
        path: list[SessionKey] = []
        positions: dict[SessionKey, int] = {}
        current = start
        while True:
            if current in resolved:
                root = resolved[current]
                state = states[current]
                break
            if current in positions:
                cycle = path[positions[current] :]
                root = min(cycle)
                state = "cycle"
                break
            positions[current] = len(path)
            path.append(current)
            parent = parent_of.get(current)
            if parent is None:
                root = current
                state = (
                    "resolved_root"
                    if _text(sessions[current].get("session_kind") or "root") == "root"
                    else "orphan_parent_missing"
                )
                break
            if parent not in sessions:
                root = current
                state = "orphan_parent_missing"
                break
            if not _lineage_namespaces_compatible(sessions[current], sessions[parent]):
                root = current
                state = "orphan_parent_namespace_mismatch"
                break
            if not _lineage_source_namespaces_compatible(
                sessions[current], sessions[parent]
            ):
                root = current
                state = "orphan_parent_source_namespace_mismatch"
                break
            current = parent
        for member in path:
            resolved[member] = root
            states[member] = state
    return resolved, states


def _expanded_memberships(
    memberships: Iterable[Mapping[str, Any]] | Mapping[str, Any],
) -> Iterable[dict[str, Any]]:
    """Normalize direct rows, grouped rows, or ContinuationProjection output.

    In addition to this module's compact ``sessions`` adapter, this accepts
    ``ContinuationProjection.to_dict()["tasks"]`` rows whose memberships use
    ``{"session": {...}, "role": "primary|continuation"}``.  Passing the
    complete ``ContinuationProjection.to_dict()`` mapping is supported too.
    """

    if isinstance(memberships, Mapping):
        projected_tasks = memberships.get("tasks")
        records: Iterable[Any] = projected_tasks if isinstance(projected_tasks, list) else [memberships]
    else:
        records = memberships

    for membership in records:
        if not isinstance(membership, Mapping):
            continue
        projected_memberships = membership.get("memberships")
        if isinstance(projected_memberships, list):
            for projected in projected_memberships:
                if not isinstance(projected, Mapping):
                    continue
                session = projected.get("session")
                if not isinstance(session, Mapping):
                    continue
                yield {
                    **dict(session),
                    "task_id": membership.get("task_id"),
                    "title_override": membership.get("title"),
                    "primary": _text(projected.get("role")) == "primary",
                }
            continue
        nested = membership.get("sessions")
        if isinstance(nested, list):
            for member in nested:
                if not isinstance(member, Mapping):
                    continue
                yield {
                    **dict(member),
                    "task_id": member.get("task_id") or membership.get("task_id"),
                    "title_override": member.get("title_override")
                    or membership.get("title_override")
                    or membership.get("title"),
                    "primary": member.get("primary", False),
                }
            continue
        yield dict(membership)


def _continuation_tasks(
    memberships: Iterable[Mapping[str, Any]],
    roots: Mapping[SessionKey, SessionKey],
) -> tuple[
    dict[SessionKey, str],
    dict[str, list[SessionKey]],
    dict[str, SessionKey],
    dict[str, str],
    list[dict[str, Any]],
]:
    task_ids_by_root: dict[SessionKey, set[str]] = defaultdict(set)
    root_order_by_task: dict[str, list[SessionKey]] = defaultdict(list)
    explicit_primary: dict[str, SessionKey] = {}
    title_overrides: dict[str, str] = {}
    issues: list[dict[str, Any]] = []

    for membership in _expanded_memberships(memberships):
        task_id = _text(membership.get("task_id"))
        member = _session_key(membership)
        if not task_id or member is None:
            issues.append({"reason": "invalid_continuation_membership", "membership": membership})
            continue
        root = roots.get(member)
        if root is None:
            issues.append(
                {
                    "reason": "continuation_session_not_observed",
                    "task_id": task_id,
                    "session": {"client": member[0], "client_session_id": member[1]},
                }
            )
            continue
        task_ids_by_root[root].add(task_id)
        if root not in root_order_by_task[task_id]:
            root_order_by_task[task_id].append(root)
        if membership.get("primary") and task_id not in explicit_primary:
            explicit_primary[task_id] = root
        title = _text(membership.get("title_override") or membership.get("title"))
        if title:
            current = title_overrides.get(task_id)
            if current is None:
                title_overrides[task_id] = title
            elif current != title:
                issues.append(
                    {
                        "reason": "conflicting_title_overrides",
                        "task_id": task_id,
                        "titles": sorted({current, title}),
                    }
                )

    task_by_root: dict[SessionKey, str] = {}
    for root, task_ids in task_ids_by_root.items():
        if len(task_ids) == 1:
            task_by_root[root] = next(iter(task_ids))
        else:
            issues.append(
                {
                    "reason": "conflicting_continuation_memberships",
                    "root": {"client": root[0], "client_session_id": root[1]},
                    "task_ids": sorted(task_ids),
                }
            )

    primary_by_task: dict[str, SessionKey] = {}
    for task_id, ordered_roots in root_order_by_task.items():
        usable = [root for root in ordered_roots if task_by_root.get(root) == task_id]
        if not usable:
            continue
        primary_by_task[task_id] = (
            explicit_primary.get(task_id)
            if explicit_primary.get(task_id) in usable
            else usable[0]
        )
    return task_by_root, root_order_by_task, primary_by_task, title_overrides, issues


def _task_key_for_root(root: SessionKey, continuation_by_root: Mapping[SessionKey, str]) -> TaskKey:
    continuation_id = continuation_by_root.get(root)
    if continuation_id:
        return ("continuation", continuation_id)
    return ("session_root", root[0], root[1])


def _public_task_id(task_key: TaskKey) -> str:
    if task_key[0] == "continuation":
        return task_key[1]
    return f"session:{task_key[1]}:{task_key[2]}"


def _candidate_key(value: Mapping[str, Any]) -> SessionKey | None:
    return _session_key(value)


def _embedded_candidates(item: Mapping[str, Any]) -> set[SessionKey]:
    raw = item.get("log_evidence_candidate_sessions")
    if not isinstance(raw, list):
        return set()
    return {
        key
        for candidate in raw
        if isinstance(candidate, Mapping) and (key := _candidate_key(candidate)) is not None
    }


def _embedded_candidate_source_namespace_conflict(
    item: Mapping[str, Any],
    sessions: Mapping[SessionKey, Mapping[str, Any]],
) -> bool:
    raw = item.get("log_evidence_candidate_sessions")
    if not isinstance(raw, list):
        return False
    namespaces_by_key: dict[SessionKey, set[str]] = defaultdict(set)
    for candidate in raw:
        if not isinstance(candidate, Mapping):
            continue
        key = _candidate_key(candidate)
        if key is None:
            continue
        namespace = _text(candidate.get("source_namespace_fingerprint"))
        if namespace:
            namespaces_by_key[key].add(namespace)
    for key, namespaces in namespaces_by_key.items():
        if len(namespaces) > 1:
            return True
        if not namespaces:
            continue
        session = sessions.get(key)
        if session is None:
            continue
        if _text(session.get("source_namespace_fingerprint")) not in namespaces:
            return True
    return False


def _normalized_evidence_candidates(
    evidence: Iterable[Mapping[str, Any]],
) -> dict[str, set[SessionKey]]:
    """Normalize optional donor evidence keyed by work or section id.

    Accepted records are intentionally small and adapter-friendly:

    ``{"work_id": "w", "client": "codex", "client_session_id": "s"}``

    or

    ``{"section_id": "s", "candidate_sessions": [{...}, {...}]}``.
    """

    by_ref: dict[str, set[SessionKey]] = defaultdict(set)
    for row in evidence:
        if not isinstance(row, Mapping):
            continue
        ref = _text(row.get("work_id") or row.get("section_id"))
        if not ref:
            continue
        nested = row.get("candidate_sessions")
        if isinstance(nested, list):
            for candidate in nested:
                if isinstance(candidate, Mapping) and (key := _candidate_key(candidate)) is not None:
                    by_ref[ref].add(key)
            continue
        key = _candidate_key(row)
        if key is not None:
            by_ref[ref].add(key)
    return by_ref


def _work_identity(item: Mapping[str, Any], index: int) -> str:
    return _text(item.get("work_id") or item.get("section_id")) or f"work:{index}"


def _run_hint_key(item: Mapping[str, Any]) -> tuple[str, str, str, str] | None:
    client = _text(item.get("client") or item.get("reporting_source"))
    run_id = _text(item.get("run_id"))
    if not client or not run_id:
        return None
    namespace = _namespace_fingerprint(item)
    if not namespace and _text(item.get("identity_scope_state")) == "explicit":
        # An explicit scope without its fingerprint is internally incomplete.
        # A low-confidence run-id hint must never let that malformed assertion
        # attach work to an otherwise unrelated Task.
        return None
    return (
        client,
        _text(item.get("project_dir")),
        run_id,
        namespace or "unscoped",
    )


def _has_exact_session_attribution(item: Mapping[str, Any]) -> bool:
    """Return true only when upstream provenance explicitly supports ``exact``."""

    authored = item.get("authored_join_keys")
    authored_keys = set(authored) if isinstance(authored, list) else set()
    return _text(item.get("join_confidence")) == "exact" or "client_session_id" in authored_keys


def _candidate_sessions_for_work(
    item: Mapping[str, Any],
    evidence_by_ref: Mapping[str, set[SessionKey]],
) -> tuple[set[SessionKey], list[str]]:
    candidates = _embedded_candidates(item)
    sources: list[str] = []
    if candidates:
        sources.append("log_evidence_candidate_sessions")
    for ref in {_text(item.get("work_id")), _text(item.get("section_id"))} - {""}:
        extra = evidence_by_ref.get(ref, set())
        if extra:
            candidates.update(extra)
            if "work_session_evidence" not in sources:
                sources.append("work_session_evidence")
    return candidates, sources


def _aggregate_usage(
    session_keys: Iterable[SessionKey], sessions: Mapping[SessionKey, Mapping[str, Any]]
) -> tuple[dict[str, Any], list[str]]:
    totals = {
        "rows": 0,
        "additive_rows": 0,
        "excluded_non_additive_rows": 0,
        "priced_rows": 0,
        "unpriced_rows": 0,
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
    }
    models: list[str] = []
    usage_models: list[str] = []
    priced_cost = 0.0
    usage_present = False
    cost_complete = True
    usage_confidences: set[str] = set()
    cost_confidences: set[str] = set()
    cost_bases: set[str] = set()
    partial_without_counts = {"cache_creation": False, "cache_read": False}
    # ``set`` is the final guard against callers repeating member keys.
    for key in sorted(set(session_keys)):
        row = sessions[key]
        observed_models = row.get("observed_models") if isinstance(row.get("observed_models"), list) else []
        for observed_model in observed_models:
            model = _text(observed_model)
            if model and model not in models:
                models.append(model)
        usage = row.get("usage") if isinstance(row.get("usage"), Mapping) else {}
        identity_models = usage.get("identity_models") if isinstance(usage.get("identity_models"), list) else []
        for identity_model in identity_models:
            model = _text(identity_model)
            if model and model not in models:
                models.append(model)
        rows = _integer(usage.get("rows"))
        additive_rows = (
            _integer(usage.get("additive_rows"))
            if "additive_rows" in usage
            else rows
        )
        excluded_non_additive_rows = _integer(usage.get("excluded_non_additive_rows"))
        if "priced_rows" in usage or "unpriced_rows" in usage:
            priced_rows = _integer(usage.get("priced_rows"))
            unpriced_rows = _integer(usage.get("unpriced_rows"))
        elif rows and usage.get("estimated_cost_usd") is not None:
            priced_rows, unpriced_rows = rows, 0
        else:
            priced_rows, unpriced_rows = 0, rows
        totals["rows"] += rows
        totals["additive_rows"] += additive_rows
        totals["excluded_non_additive_rows"] += excluded_non_additive_rows
        totals["priced_rows"] += priced_rows
        totals["unpriced_rows"] += unpriced_rows
        totals["fresh_tokens"] += _integer(usage.get("fresh_tokens"))
        totals["cache_creation_tokens"] += _integer(usage.get("cache_creation_tokens"))
        totals["cache_read_tokens"] += _integer(usage.get("cache_read_tokens"))
        for prefix in ("cache_creation", "cache_read"):
            reported = _integer(usage.get(f"{prefix}_reported_rows"))
            unreported = _integer(usage.get(f"{prefix}_unreported_rows"))
            unknown = _integer(usage.get(f"{prefix}_unknown_rows"))
            # Compatibility with session projections produced before the row
            # counters existed. Missing capability is unknown/unreported, never
            # a measured zero; an explicit partial status remains partial even
            # though its exact reported/unreported row split is unavailable.
            if reported + unreported + unknown == 0 and additive_rows:
                reporting = _text(usage.get(f"{prefix}_reporting"))
                if reporting == "reported":
                    reported = rows
                elif reporting == "not_reported":
                    unreported = rows
                elif reporting == "partial":
                    partial_without_counts[prefix] = True
                else:
                    unknown = rows
            totals[f"{prefix}_reported_rows"] += reported
            totals[f"{prefix}_unreported_rows"] += unreported
            totals[f"{prefix}_unknown_rows"] += unknown
        totals["total_tokens"] += _integer(usage.get("total_tokens"))
        if additive_rows:
            usage_present = True
            usage_confidence = _text(usage.get("usage_confidence"))
            cost_confidence = _text(usage.get("cost_confidence"))
            cost_basis = _text(usage.get("cost_basis"))
            if usage_confidence:
                usage_confidences.add(usage_confidence)
            if cost_confidence:
                cost_confidences.add(cost_confidence)
            if cost_basis:
                cost_bases.add(cost_basis)
            if unpriced_rows or usage.get("estimated_cost_usd") is None:
                cost_complete = False
            else:
                priced_cost += _number(usage.get("estimated_cost_usd"))
        lanes = usage.get("model_lanes") if isinstance(usage.get("model_lanes"), list) else []
        for lane in lanes:
            if not isinstance(lane, Mapping):
                continue
            model = _text(lane.get("model"))
            if model and model not in usage_models:
                usage_models.append(model)
            if model and model not in models:
                models.append(model)
    if totals["excluded_non_additive_rows"]:
        cost_complete = False
    totals["estimated_cost_usd"] = priced_cost if usage_present and cost_complete else None
    totals["known_additive_cost_usd"] = priced_cost if usage_present and priced_cost else None
    totals["cost_complete"] = bool(usage_present and cost_complete)
    totals["usage_availability"] = (
        "partial"
        if totals["additive_rows"] and totals["excluded_non_additive_rows"]
        else "held"
        if totals["excluded_non_additive_rows"]
        else "available"
        if totals["additive_rows"]
        else "unknown"
    )
    totals["usage_confidence"] = (
        next(iter(usage_confidences))
        if len(usage_confidences) == 1
        else "mixed"
        if usage_confidences
        else None
    )
    totals["cost_confidence"] = (
        next(iter(cost_confidences))
        if len(cost_confidences) == 1
        else "mixed"
        if cost_confidences
        else None
    )
    # ``cost_basis`` rides alongside ``cost_confidence`` so a Task's Cost reads
    # both "how sure" and "what kind of number": one basis if every priced row
    # agrees, ``mixed`` when they disagree (e.g. some client-reported, some
    # pricing-table estimate), ``None`` when no additive row carried a basis.
    totals["cost_basis"] = (
        next(iter(cost_bases))
        if len(cost_bases) == 1
        else "mixed"
        if cost_bases
        else None
    )
    for prefix in ("cache_creation", "cache_read"):
        reported = totals[f"{prefix}_reported_rows"]
        unreported = totals[f"{prefix}_unreported_rows"]
        unknown = totals[f"{prefix}_unknown_rows"]
        totals[f"{prefix}_reporting"] = (
            "partial"
            if (reported and (unreported or unknown)) or partial_without_counts[prefix]
            else "unknown"
            if unknown
            else "reported"
            if reported
            else "not_reported"
            if unreported
            else None
        )
    # Hook-observed models identify the agent session but are not usage rows.
    # Keep them in the Task model list without manufacturing usage lanes.
    totals["model_lanes"] = [{"model": model} for model in usage_models]
    return totals, models


def _session_ref(key: SessionKey) -> dict[str, str]:
    return {"client": key[0], "client_session_id": key[1]}


def build_task_projection(
    session_rows: Iterable[Mapping[str, Any]],
    work_items: Iterable[Mapping[str, Any]],
    *,
    continuation_memberships: Iterable[Mapping[str, Any]] | Mapping[str, Any] = (),
    work_session_evidence: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build a session-first, JSON-serializable Task projection.

    ``continuation_memberships`` accepts either one membership per row, a group
    row with ``sessions``, or the task rows/full mapping returned by
    ``ContinuationProjection.to_dict()``.  Compact memberships need ``task_id``,
    ``client``, and one of ``client_session_id``/``session_id``/
    ``root_session_id``; they may also carry ``primary`` and ``title_override``.

    ``work_session_evidence`` is optional normalized donor evidence.  The
    projection also consumes ``work_item.log_evidence_candidate_sessions``
    directly.  Candidate evidence can select a Task, but it never fills or
    claims an exact work-item session id.
    """

    sessions, duplicate_session_rows = _dedupe_sessions(session_rows)
    roots, lineage_states = _resolve_roots(sessions)
    (
        continuation_by_root,
        continuation_root_order,
        continuation_primary,
        title_overrides,
        issues,
    ) = _continuation_tasks(continuation_memberships, roots)

    task_for_session: dict[SessionKey, TaskKey] = {
        session: _task_key_for_root(root, continuation_by_root)
        for session, root in roots.items()
    }
    roots_by_task: dict[TaskKey, set[SessionKey]] = defaultdict(set)
    members_by_task: dict[TaskKey, set[SessionKey]] = defaultdict(set)
    members_by_root: dict[SessionKey, set[SessionKey]] = defaultdict(set)
    for session, root in roots.items():
        task_key = task_for_session[session]
        roots_by_task[task_key].add(root)
        members_by_task[task_key].add(session)
        members_by_root[root].add(session)

    evidence_by_ref = _normalized_evidence_candidates(work_session_evidence)
    associated_work: dict[TaskKey, list[dict[str, Any]]] = defaultdict(list)
    associations: dict[TaskKey, list[dict[str, Any]]] = defaultdict(list)
    unresolved_work: list[dict[str, Any]] = []
    pending_run_hints: list[
        tuple[str, dict[str, Any], tuple[str, str, str, str]]
    ] = []

    for index, raw_item in enumerate(work_items):
        if not isinstance(raw_item, Mapping):
            continue
        item = dict(raw_item)
        work_id = _work_identity(item, index)
        if not _text(item.get("work_id")):
            # Give id-less copies a stable projection-local identity without
            # mutating the caller's work item.
            item["work_id"] = work_id
        candidate_sessions, candidate_sources = _candidate_sessions_for_work(item, evidence_by_ref)
        explicit_session = _work_session_key(item)
        task_key: TaskKey | None = None
        association: dict[str, Any]

        if explicit_session is not None:
            task_key = task_for_session.get(explicit_session)
            if task_key is None:
                unresolved_work.append(
                    {
                        "work_id": work_id,
                        "item": item,
                        "reason": "work_session_not_observed",
                        "session": _session_ref(explicit_session),
                    }
                )
                continue
            session = sessions[explicit_session]
            if not namespace_join_compatible(
                session,
                item,
                allow_legacy_unscoped=bool(
                    session.get("allow_legacy_unscoped_namespace_join")
                ),
            ):
                unresolved_work.append(
                    {
                        "work_id": work_id,
                        "item": item,
                        "reason": "work_session_namespace_mismatch",
                        "session": _session_ref(explicit_session),
                    }
                )
                continue
            if not _log_evidenced_work_source_namespace_compatible(session, item):
                unresolved_work.append(
                    {
                        "work_id": work_id,
                        "item": item,
                        "reason": "work_session_source_namespace_mismatch",
                        "session": _session_ref(explicit_session),
                    }
                )
                continue
            has_exact_session = _has_exact_session_attribution(item)
            association = {
                "work_id": work_id,
                "task_id": _public_task_id(task_key),
                "basis": "work_session",
                "session_unlinked": False,
                "client_session": _session_ref(explicit_session),
                # The projection preserves an upstream session association but
                # does not silently upgrade inherited/log-evidenced identity to
                # exact.  Exactness needs explicit upstream provenance.
                "session_attribution": "exact" if has_exact_session else "upstream",
                "exact_session_id": explicit_session[1] if has_exact_session else None,
                "candidate_sessions": [_session_ref(key) for key in sorted(candidate_sessions)],
                "provenance": ["work_item_client_session_id"],
            }
        elif candidate_sessions:
            if _embedded_candidate_source_namespace_conflict(item, sessions):
                unresolved_work.append(
                    {
                        "work_id": work_id,
                        "item": item,
                        "reason": "candidate_session_source_namespace_mismatch",
                        "candidate_sessions": [
                            _session_ref(key) for key in sorted(candidate_sessions)
                        ],
                    }
                )
                continue
            missing = sorted(key for key in candidate_sessions if key not in task_for_session)
            candidate_tasks = {
                task_for_session[key] for key in candidate_sessions if key in task_for_session
            }
            if missing or len(candidate_tasks) != 1:
                reason = "candidate_session_not_observed" if missing else "candidate_sessions_span_tasks"
                unresolved_work.append(
                    {
                        "work_id": work_id,
                        "item": item,
                        "reason": reason,
                        "candidate_sessions": [_session_ref(key) for key in sorted(candidate_sessions)],
                        "candidate_task_ids": sorted(_public_task_id(key) for key in candidate_tasks),
                    }
                )
                continue
            task_key = next(iter(candidate_tasks))
            association = {
                "work_id": work_id,
                "task_id": _public_task_id(task_key),
                "basis": "common_task_from_candidate_sessions",
                "session_unlinked": True,
                "client_session": None,
                "session_attribution": "unresolved",
                "exact_session_id": None,
                "candidate_sessions": [_session_ref(key) for key in sorted(candidate_sessions)],
                "provenance": candidate_sources,
            }
        else:
            run_hint = _run_hint_key(item)
            if run_hint is not None:
                pending_run_hints.append((work_id, item, run_hint))
            else:
                unresolved_work.append(
                    {
                        "work_id": work_id,
                        "item": item,
                        "reason": "no_session_or_candidate_evidence",
                        "run_id": None,
                    }
                )
            continue

        associated_work[task_key].append(item)
        associations[task_key].append(association)

    # ``run_id`` never creates or merges Tasks.  It is a second-pass placement
    # hint only when session/evidence-resolved work has already established one
    # and only one Task for the same client + project + run tuple.  The hinted
    # work remains session-unlinked and never affects usage allocation.
    resolved_tasks_by_run: dict[tuple[str, str, str, str], set[TaskKey]] = defaultdict(set)
    for task_key, items in associated_work.items():
        for item in items:
            run_hint = _run_hint_key(item)
            if run_hint is not None:
                resolved_tasks_by_run[run_hint].add(task_key)
    for work_id, item, run_hint in pending_run_hints:
        candidate_tasks = resolved_tasks_by_run.get(run_hint, set())
        if len(candidate_tasks) == 1:
            task_key = next(iter(candidate_tasks))
            associated_work[task_key].append(item)
            associations[task_key].append(
                {
                    "work_id": work_id,
                    "task_id": _public_task_id(task_key),
                    "basis": "unique_task_from_run_id",
                    "session_unlinked": True,
                    "client_session": None,
                    "session_attribution": "unresolved",
                    "exact_session_id": None,
                    "candidate_sessions": [],
                    "provenance": ["run_id_grouping_hint"],
                }
            )
            continue
        unresolved_work.append(
            {
                "work_id": work_id,
                "item": item,
                "reason": (
                    "run_id_spans_tasks" if len(candidate_tasks) > 1 else "no_session_or_candidate_evidence"
                ),
                "run_id": run_hint[2],
                "candidate_task_ids": sorted(_public_task_id(key) for key in candidate_tasks),
            }
        )

    tasks: list[dict[str, Any]] = []
    all_task_keys = sorted(
        set(members_by_task) | set(associated_work),
        key=lambda key: _public_task_id(key),
    )
    seen_public_ids: set[str] = set()
    for task_key in all_task_keys:
        task_id = _public_task_id(task_key)
        if task_id in seen_public_ids:
            raise ValueError(f"duplicate public task id: {task_id}")
        seen_public_ids.add(task_id)
        root_keys = roots_by_task.get(task_key, set())
        member_keys = members_by_task.get(task_key, set())
        continuation_id = task_key[1] if task_key[0] == "continuation" else None
        if continuation_id:
            ordered_roots = [
                root
                for root in continuation_root_order.get(continuation_id, [])
                if root in root_keys
            ]
            ordered_roots.extend(sorted(root_keys - set(ordered_roots)))
            primary_root = continuation_primary.get(continuation_id) or ordered_roots[0]
        else:
            ordered_roots = sorted(root_keys)
            primary_root = ordered_roots[0]

        ordered_members: list[SessionKey] = []
        root_groups = []
        for root in ordered_roots:
            root_members = sorted(
                members_by_root[root],
                key=lambda key: (
                    0 if key == root else 1,
                    _number(sessions[key].get("last_activity_at")),
                    key,
                ),
            )
            ordered_members.extend(root_members)
            root_groups.append(
                {
                    "root": _session_ref(root),
                    "session_keys": [_session_ref(key) for key in root_members],
                    "lineage_state": lineage_states.get(root, "resolved_root"),
                    "supporting_count": max(0, len(root_members) - 1),
                }
            )

        usage, models = _aggregate_usage(ordered_members, sessions)
        task_work = sorted(
            associated_work.get(task_key, []),
            key=lambda item: (
                _number(item.get("started_at") or item.get("updated_at")),
                _text(item.get("work_id") or item.get("section_id")),
            ),
        )
        run_work_ids: dict[str, list[str]] = defaultdict(list)
        for index, item in enumerate(task_work):
            run_id = _text(item.get("run_id"))
            if not run_id:
                continue
            work_id = _work_identity(item, index)
            if work_id not in run_work_ids[run_id]:
                run_work_ids[run_id].append(work_id)
        task_associations = associations.get(task_key, [])
        supporting_keys = member_keys - root_keys
        tasks.append(
            {
                "task_id": task_id,
                "identity_basis": "explicit_continuation" if continuation_id else "session_root",
                "continuation_id": continuation_id,
                "title_override": title_overrides.get(continuation_id or ""),
                "primary_root": _session_ref(primary_root),
                "root_keys": [_session_ref(key) for key in ordered_roots],
                "root_groups": root_groups,
                "session_keys": [_session_ref(key) for key in ordered_members],
                "sessions": [dict(sessions[key]) for key in ordered_members],
                "session_count": len(member_keys),
                "supporting_count": len(supporting_keys),
                "child_count": sum(
                    1 for key in supporting_keys if _text(sessions[key].get("session_kind")) == "child"
                ),
                "internal_count": sum(
                    1 for key in supporting_keys if _text(sessions[key].get("session_kind")) == "internal"
                ),
                "last_activity_at": max(
                    (_number(sessions[key].get("last_activity_at")) for key in member_keys),
                    default=0.0,
                ),
                "work_items": task_work,
                "work_associations": task_associations,
                "has_session_unlinked_work": any(
                    association.get("session_unlinked") for association in task_associations
                ),
                "session_unlinked_work_count": sum(
                    1 for association in task_associations if association.get("session_unlinked")
                ),
                "run_subgroups": [
                    {"run_id": run_id, "work_ids": work_ids}
                    for run_id, work_ids in sorted(run_work_ids.items())
                ],
                "usage": usage,
                "models": models,
            }
        )

    return {
        "schema_version": TASK_PROJECTION_SCHEMA_VERSION,
        "tasks": tasks,
        "unresolved_work": unresolved_work,
        "continuation_issues": issues,
        "summary": {
            "task_count": len(tasks),
            "session_count": len(sessions),
            "duplicate_session_rows_ignored": duplicate_session_rows,
            "associated_work_count": sum(len(task["work_items"]) for task in tasks),
            "unresolved_work_count": len(unresolved_work),
            "continuation_issue_count": len(issues),
        },
    }


__all__ = ["TASK_PROJECTION_SCHEMA_VERSION", "build_task_projection"]
