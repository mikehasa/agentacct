from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from .source_policy import DEFAULT_SOURCE_AUTHORITY_POLICY


EVIDENCE_PRODUCT_SCHEMA_VERSION = "agent-chronicle.evidence-product.v1"
WORK_EVIDENCE_RECORD_LIMIT = 20

_SUBJECT_KINDS = (
    "project_id",
    "agent_id",
    "work_id",
    "run_id",
    "client_session_id",
    "parent_client_session_id",
    "turn_id",
    "message_id",
    "request_id",
    "section_id",
    "tool_call_id",
    "work_item",
    "parent_work_item",
    "execution",
    "client_session",
    "trace_id",
    "span_id",
    "artifact",
    "commit",
    "organization",
    "principal",
)
_ANCHOR_ORDER = (
    "work_id",
    "work_item",
    "run_id",
    "execution",
    "client_session_id",
    "client_session",
    "section_id",
    "turn_id",
    "artifact",
    "commit",
)
_CLIENT_ID_KINDS = frozenset(
    {
        "client_session_id",
        "parent_client_session_id",
        "client_session",
        "turn_id",
        "message_id",
        "request_id",
        "tool_call_id",
    }
)
_FACT_ID_ORDER = (
    "request_id",
    "message_id",
    "tool_call_id",
    "span_id",
    "trace_id",
    "turn_id",
    "section_id",
    "artifact",
    "commit",
)
_MEASUREMENT_WINDOW_KEYS = frozenset(
    {
        "billing_period",
        "period_start",
        "period_end",
        "window_start",
        "window_end",
        "measurement_id",
    }
)
_VALUE_KEYS = {
    "cost": ("provider_billed_cost_usd", "cost_usd", "estimated_cost_usd", "amount_usd"),
    "usage": (
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "reasoning_output_tokens",
    ),
    "machine_check": ("result", "exit_code", "after_exit_code", "status"),
    "outcome": ("result", "status", "outcome"),
}


def _plain_envelope(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    return dict(value) if isinstance(value, Mapping) else {}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> tuple[Any, ...]:
    return tuple(value) if isinstance(value, (list, tuple)) else ()


def _source_key(envelope: Mapping[str, Any]) -> tuple[str, str]:
    return (str(envelope.get("source_type") or "unknown"), str(envelope.get("source_system") or "unknown"))


def _subject_items(envelope: Mapping[str, Any]) -> list[tuple[str, str]]:
    subjects = _mapping(envelope.get("subjects"))
    items: list[tuple[str, str]] = []
    for kind in _SUBJECT_KINDS:
        value = subjects.get(kind)
        if isinstance(value, str) and value:
            items.append((kind, value))
    extra = _mapping(subjects.get("extra"))
    for kind, value in sorted(extra.items(), key=lambda item: str(item[0])):
        # Core/alias subjects are authoritative in their dedicated fields.
        # An arbitrary `extra` bag cannot replace them during projection.
        if str(kind) in _SUBJECT_KINDS:
            continue
        if isinstance(value, str) and value:
            items.append((str(kind), value))
    return items


def _subject_namespace_components(envelope: Mapping[str, Any], kind: str) -> dict[str, str]:
    """Return the issuer scope components for one subject identifier.

    Subject ids are not globally unique merely because their spelling matches.
    Client-generated ids are scoped to the client system, organization and
    project; ids from other domains additionally include the concrete source
    instance. Cross-source joins therefore require an explicit claimed link.
    """

    subjects = dict(_subject_items(envelope))
    source_type, source_system = _source_key(envelope)
    source_instance = str(envelope.get("source_instance") or "unknown")
    organization = subjects.get("organization") or "unscoped"
    project = subjects.get("project_id") or "unscoped"
    components = {
        "system": source_system,
        "organization": organization,
        "project": project,
    }
    source_home = subjects.get("source_namespace_fingerprint")
    if source_home:
        # A trusted local-log issuer scopes every synthetic subject, including
        # run ids. Otherwise two homes that reuse one raw session id still meet
        # through a shared run node even when their session nodes are separate.
        components["source_home"] = source_home
    if kind not in _CLIENT_ID_KINDS:
        components.update({"type": source_type, "instance": source_instance})
    return components


def _subject_namespace(envelope: Mapping[str, Any], kind: str) -> str:
    return json.dumps(
        _subject_namespace_components(envelope, kind),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _subject_node_id(namespace: str, kind: str, value: str) -> str:
    material = json.dumps(
        {"namespace": namespace, "kind": kind, "value": value},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "subject_" + hashlib.sha256(material).hexdigest()


def _subject_scope(envelope: Mapping[str, Any]) -> tuple[str, str, str] | None:
    subjects = dict(_subject_items(envelope))
    for kind in _ANCHOR_ORDER:
        value = subjects.get(kind)
        if value:
            return _subject_namespace(envelope, kind), kind, value
    return None


def _comparison_scope(envelope: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """Prefer the narrow correlation id used by the measured fact."""

    subjects = dict(_subject_items(envelope))
    for kind in (*_FACT_ID_ORDER, *_ANCHOR_ORDER):
        value = subjects.get(kind)
        if value:
            return _subject_namespace(envelope, kind), kind, value
    return None


def _measurement_fact(envelope: Mapping[str, Any], dimension: str) -> str | None:
    """Identify a single measured fact, not a whole evolving session/run.

    A request/span/etc is narrow enough to compare directly. Coarse scopes are
    only comparable for an explicit measurement window or the exact same
    event type and timestamp. This prevents cumulative usage and lifecycle
    transitions from being reported as contradictions.
    """

    subjects = dict(_subject_items(envelope))
    event_type = str(envelope.get("event_type") or "unknown")
    for kind in _FACT_ID_ORDER:
        value = subjects.get(kind)
        if value:
            if dimension in {"cost", "usage"}:
                return f"{kind}:{value}"
            return f"{event_type}|{kind}:{value}"
    window = _walk_key_values(envelope.get("payload"), set(_MEASUREMENT_WINDOW_KEYS))
    if window:
        return "window:" + json.dumps(window, sort_keys=True, ensure_ascii=False, default=str)
    timestamp = envelope.get("event_timestamp")
    if isinstance(timestamp, str) and timestamp:
        return f"{event_type}|at:{timestamp}"
    return None


def _walk_key_values(value: Any, wanted: set[str]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in wanted and child is not None and not isinstance(child, (Mapping, list, tuple)):
                found.setdefault(normalized, child)
            if isinstance(child, (Mapping, list, tuple)):
                for nested_key, nested_value in _walk_key_values(child, wanted).items():
                    found.setdefault(nested_key, nested_value)
    elif isinstance(value, (list, tuple)):
        for child in value:
            for nested_key, nested_value in _walk_key_values(child, wanted).items():
                found.setdefault(nested_key, nested_value)
    return found


def _dimension_values(envelope: Mapping[str, Any], dimension: str) -> dict[str, Any]:
    keys = _VALUE_KEYS.get(dimension, ())
    return _walk_key_values(envelope.get("payload"), set(keys)) if keys else {}


def _comparison_values(envelope: Mapping[str, Any], dimension: str) -> dict[str, Any]:
    """Canonical comparable fields; absence or shape differences are not conflicts."""

    values = _dimension_values(envelope, dimension)
    if dimension == "cost":
        for key in _VALUE_KEYS["cost"]:
            if key in values:
                return {"amount_usd": values[key]}
        return {}
    if dimension == "usage":
        comparable = dict(values)
        if "total_tokens" not in comparable:
            input_tokens = comparable.get("input_tokens")
            output_tokens = comparable.get("output_tokens")
            if (
                isinstance(input_tokens, (int, float))
                and not isinstance(input_tokens, bool)
                and isinstance(output_tokens, (int, float))
                and not isinstance(output_tokens, bool)
            ):
                comparable["total_tokens"] = input_tokens + output_tokens
        return comparable
    if dimension == "outcome":
        for key in ("result", "status", "outcome"):
            if key in values:
                return {"status": values[key]}
        return {}
    return values


def _value_fingerprint(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            normalized = Decimal(str(value)).normalize()
        except InvalidOperation:
            pass
        else:
            if normalized == 0:
                normalized = Decimal(0)
            return f"number:{normalized}"
    return "json:" + json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _basis_family(envelope: Mapping[str, Any], dimension: str) -> str:
    basis = str(_mapping(envelope.get("measurement_basis")).get(dimension) or "unknown").lower()
    if dimension != "cost":
        return "comparable"
    if "billed" in basis or "invoice" in basis:
        return "provider_billed"
    if "estimat" in basis:
        return "estimated"
    if "claim" in basis:
        return "claimed"
    if "report" in basis or "telemetry" in basis:
        return "reported"
    return basis


def _dimension_authority(envelope: Mapping[str, Any], dimension: str) -> tuple[str, str]:
    basis = str(_mapping(envelope.get("measurement_basis")).get(dimension) or "unknown")
    decision = DEFAULT_SOURCE_AUTHORITY_POLICY.evaluate_source(
        source_type=str(envelope.get("source_type") or "unknown"),
        source_system=str(envelope.get("source_system") or "unknown"),
        assertion=str(envelope.get("assertion") or "unknown"),
        dimension=dimension,
        measurement_basis=basis,
    )
    return decision.authority.value, decision.reason


def build_evidence_matrix(envelopes: Iterable[Any]) -> dict[str, Any]:
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    all_dimensions: set[str] = set()
    for raw in envelopes:
        envelope = _plain_envelope(raw)
        source_type, source_system = _source_key(envelope)
        assertion = str(envelope.get("assertion") or "unknown")
        completeness = str(_mapping(envelope.get("completeness")).get("status") or "unknown")
        for dimension_value in _sequence(envelope.get("dimensions")):
            dimension = str(dimension_value)
            all_dimensions.add(dimension)
            key = (source_type, source_system, dimension)
            row = rows.setdefault(
                key,
                {
                    "source_type": source_type,
                    "source_system": source_system,
                    "dimension": dimension,
                    "evidence_count": 0,
                    "observed_count": 0,
                    "claimed_count": 0,
                    "complete_count": 0,
                    "partial_count": 0,
                    "unknown_count": 0,
                    "measurement_bases": Counter(),
                    "authority_counts": Counter(),
                    "authority_reasons": Counter(),
                },
            )
            row["evidence_count"] += 1
            if assertion == "observed":
                row["observed_count"] += 1
            elif assertion == "claimed":
                row["claimed_count"] += 1
            completeness_key = f"{completeness}_count"
            row[completeness_key if completeness_key in row else "unknown_count"] += 1
            basis = str(_mapping(envelope.get("measurement_basis")).get(dimension) or "unknown")
            row["measurement_bases"][basis] += 1
            authority, reason = _dimension_authority(envelope, dimension)
            row["authority_counts"][authority] += 1
            row["authority_reasons"][reason] += 1

    serialized_rows: list[dict[str, Any]] = []
    for row in rows.values():
        serialized = dict(row)
        serialized["measurement_bases"] = dict(sorted(row["measurement_bases"].items()))
        serialized["authority_counts"] = dict(sorted(row["authority_counts"].items()))
        serialized["authority_reasons"] = dict(sorted(row["authority_reasons"].items()))
        serialized_rows.append(serialized)
    serialized_rows.sort(key=lambda row: (row["dimension"], row["source_type"], row["source_system"]))
    return {
        "dimensions": sorted(all_dimensions),
        "source_count": len({(row["source_type"], row["source_system"]) for row in serialized_rows}),
        "rows": serialized_rows,
    }


def _timestamp_entry(value: Any) -> tuple[float, str] | None:
    """Return a comparable instant while preserving the normalized source text."""

    if not isinstance(value, str) or not value:
        return None
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.timestamp(), value


def build_source_coverage(envelopes: Iterable[Any]) -> dict[str, Any]:
    """Summarize persisted source coverage without asserting connection health."""

    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw in envelopes:
        envelope = _plain_envelope(raw)
        source_type, source_system = _source_key(envelope)
        source_instance = str(envelope.get("source_instance") or "unknown")
        key = (source_type, source_system, source_instance)
        source = grouped.setdefault(
            key,
            {
                "source_type": source_type,
                "source_system": source_system,
                "source_instance": source_instance,
                "evidence_count": 0,
                "observed_count": 0,
                "claimed_count": 0,
                "complete_count": 0,
                "partial_count": 0,
                "unknown_count": 0,
                "dimensions": set(),
                "measurement_bases": Counter(),
                "measurement_bases_by_dimension": defaultdict(Counter),
                "_first_event": None,
                "_last_event": None,
                "_last_observed": None,
            },
        )
        source["evidence_count"] += 1
        assertion = str(envelope.get("assertion") or "unknown")
        if assertion == "observed":
            source["observed_count"] += 1
        elif assertion == "claimed":
            source["claimed_count"] += 1
        completeness = str(_mapping(envelope.get("completeness")).get("status") or "unknown")
        completeness_key = f"{completeness}_count"
        source[completeness_key if completeness_key in source else "unknown_count"] += 1
        for dimension_value in _sequence(envelope.get("dimensions")):
            dimension = str(dimension_value)
            source["dimensions"].add(dimension)
            basis = str(_mapping(envelope.get("measurement_basis")).get(dimension) or "unknown")
            source["measurement_bases"][basis] += 1
            source["measurement_bases_by_dimension"][dimension][basis] += 1

        event_entry = _timestamp_entry(envelope.get("event_timestamp"))
        if event_entry is not None:
            if source["_first_event"] is None or event_entry < source["_first_event"]:
                source["_first_event"] = event_entry
            if source["_last_event"] is None or event_entry > source["_last_event"]:
                source["_last_event"] = event_entry
        observed_entry = _timestamp_entry(envelope.get("observed_at"))
        if observed_entry is not None and (
            source["_last_observed"] is None or observed_entry > source["_last_observed"]
        ):
            source["_last_observed"] = observed_entry

    sources: list[dict[str, Any]] = []
    for source in grouped.values():
        serialized = {
            key: value
            for key, value in source.items()
            if key not in {"_first_event", "_last_event", "_last_observed"}
        }
        serialized["dimensions"] = sorted(source["dimensions"])
        serialized["measurement_bases"] = dict(sorted(source["measurement_bases"].items()))
        serialized["measurement_bases_by_dimension"] = {
            dimension: dict(sorted(bases.items()))
            for dimension, bases in sorted(source["measurement_bases_by_dimension"].items())
        }
        serialized["first_event_at"] = source["_first_event"][1] if source["_first_event"] else None
        serialized["last_event_at"] = source["_last_event"][1] if source["_last_event"] else None
        serialized["last_observed_at"] = source["_last_observed"][1] if source["_last_observed"] else None
        # Persisted records prove historical coverage only. They cannot prove
        # that a connector is currently installed, connected, or healthy.
        serialized["connection_state"] = "not_verified"
        sources.append(serialized)
    sources.sort(key=lambda source: (source["source_type"], source["source_system"], source["source_instance"]))
    return {"source_count": len(sources), "sources": sources}


def _work_evidence_scope(
    envelope: Mapping[str, Any],
) -> tuple[str, str, dict[str, str], dict[str, str]] | None:
    subjects = dict(_subject_items(envelope))
    if subjects.get("work_id"):
        kind = "work_id"
        match_fields = {"work_id": subjects[kind]}
    elif subjects.get("section_id") and subjects.get("client_session_id"):
        kind = "section_session"
        match_fields = {
            "section_id": subjects["section_id"],
            "client_session_id": subjects["client_session_id"],
        }
    elif subjects.get("run_id"):
        kind = "run_id"
        match_fields = {"run_id": subjects[kind]}
    elif subjects.get("client_session_id"):
        kind = "client_session_id"
        match_fields = {"client_session_id": subjects[kind]}
    else:
        return None
    namespace_kind = "section_id" if kind == "section_session" else kind
    namespace = _subject_namespace(envelope, namespace_kind)
    return kind, namespace, _subject_namespace_components(envelope, namespace_kind), match_fields


def _work_evidence_group_id(match_kind: str, namespace: str, match_fields: Mapping[str, str]) -> str:
    material = json.dumps(
        {"match_kind": match_kind, "namespace": namespace, "match_fields": match_fields},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "work_evidence_" + hashlib.sha256(material).hexdigest()


def build_work_evidence(envelopes: Iterable[Any]) -> dict[str, Any]:
    """Build bounded metadata-only evidence digests for conservative work matches."""

    grouped: dict[str, dict[str, Any]] = {}
    labels = {
        "work_id": "Work evidence",
        "section_session": "Section session evidence",
        "run_id": "Run evidence",
        "client_session_id": "Client session evidence",
    }
    for raw in envelopes:
        envelope = _plain_envelope(raw)
        scope = _work_evidence_scope(envelope)
        if scope is None:
            continue
        match_kind, namespace, source_scope, match_fields = scope
        group_id = _work_evidence_group_id(match_kind, namespace, match_fields)
        group = grouped.setdefault(
            group_id,
            {
                "group_id": group_id,
                "match_kind": match_kind,
                "display_label": labels[match_kind],
                "namespace": namespace,
                "source_scope": source_scope,
                "match_fields": match_fields,
                "summary": {
                    "evidence_count": 0,
                    "observed_count": 0,
                    "claimed_count": 0,
                    "complete_count": 0,
                    "partial_count": 0,
                    "unknown_count": 0,
                },
                "assertions": Counter(),
                "dimensions": set(),
                "_sources": Counter(),
                "records": [],
            },
        )
        summary = group["summary"]
        summary["evidence_count"] += 1
        assertion = str(envelope.get("assertion") or "unknown")
        group["assertions"][assertion] += 1
        if assertion == "observed":
            summary["observed_count"] += 1
        elif assertion == "claimed":
            summary["claimed_count"] += 1
        completeness = str(_mapping(envelope.get("completeness")).get("status") or "unknown")
        completeness_key = f"{completeness}_count"
        summary[completeness_key if completeness_key in summary else "unknown_count"] += 1

        source_type, source_system = _source_key(envelope)
        source_instance = str(envelope.get("source_instance") or "unknown")
        group["_sources"][(source_type, source_system, source_instance)] += 1
        dimensions = sorted({str(value) for value in _sequence(envelope.get("dimensions"))})
        group["dimensions"].update(dimensions)
        basis: dict[str, str] = {}
        authority: dict[str, str] = {}
        for dimension in dimensions:
            basis[dimension] = str(_mapping(envelope.get("measurement_basis")).get(dimension) or "unknown")
            authority[dimension] = _dimension_authority(envelope, dimension)[0]
        group["records"].append(
            {
                "evidence_id": str(envelope.get("evidence_id") or "unknown"),
                "source": {
                    "source_type": source_type,
                    "source_system": source_system,
                    "source_instance": source_instance,
                },
                "assertion": assertion,
                "event_type": str(envelope.get("event_type") or "unknown"),
                "time": envelope.get("event_timestamp"),
                "dimensions": dimensions,
                "basis": basis,
                "authority": authority,
                "completeness": completeness,
            }
        )

    items: list[dict[str, Any]] = []
    for group in grouped.values():
        records = sorted(
            group["records"],
            key=lambda record: (
                _timestamp_entry(record.get("time")) is not None,
                (_timestamp_entry(record.get("time")) or (0.0, ""))[0],
                str(record.get("time") or ""),
                str(record.get("evidence_id") or ""),
            ),
            reverse=True,
        )
        total = len(records)
        sources = [
            {
                "source_type": source_type,
                "source_system": source_system,
                "source_instance": source_instance,
                "evidence_count": count,
            }
            for (source_type, source_system, source_instance), count in sorted(group["_sources"].items())
        ]
        item = {
            key: value
            for key, value in group.items()
            if key not in {"_sources", "records"}
        }
        item["summary"] = dict(group["summary"])
        item["summary"]["sources"] = sorted({source["source_system"] for source in sources})
        item["summary"]["dimensions"] = sorted(group["dimensions"])
        item["assertions"] = dict(sorted(group["assertions"].items()))
        item["dimensions"] = sorted(group["dimensions"])
        item["sources"] = sources
        item["shown_record_count"] = min(total, WORK_EVIDENCE_RECORD_LIMIT)
        item["records_truncated"] = total > WORK_EVIDENCE_RECORD_LIMIT
        item["records"] = records[:WORK_EVIDENCE_RECORD_LIMIT]
        items.append(item)
    items.sort(key=lambda item: (item["match_kind"], item["namespace"], item["group_id"]))
    return {
        "item_count": len(items),
        "record_limit_per_item": WORK_EVIDENCE_RECORD_LIMIT,
        "items": items,
    }


def _plain_link(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if hasattr(value, "to_dict"):
        return dict(value.to_dict()), {}
    if not isinstance(value, Mapping):
        return {}, {}
    wrapper = dict(value)
    link = wrapper.get("link")
    if hasattr(link, "to_dict"):
        return dict(link.to_dict()), wrapper
    return (dict(link), wrapper) if isinstance(link, Mapping) else (wrapper, wrapper)


def build_work_graph(envelopes: Iterable[Any], *, claimed_links: Iterable[Any] = ()) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    evidence_anchors: dict[str, str] = {}
    evidence_without_subjects = 0
    for raw in envelopes:
        envelope = _plain_envelope(raw)
        evidence_id = str(envelope.get("evidence_id") or "unknown")
        subjects = _subject_items(envelope)
        if not subjects:
            evidence_without_subjects += 1
            continue
        for kind, value in subjects:
            namespace = _subject_namespace(envelope, kind)
            node_id = _subject_node_id(namespace, kind, value)
            node = nodes.setdefault(
                node_id,
                {
                    "node_id": node_id,
                    "namespace": namespace,
                    "kind": kind,
                    "value": value,
                    "evidence_count": 0,
                },
            )
            node["evidence_count"] += 1
        by_kind = dict(subjects)
        anchor = next(((kind, by_kind[kind]) for kind in _ANCHOR_ORDER if by_kind.get(kind)), subjects[0])
        anchor_namespace = _subject_namespace(envelope, anchor[0])
        anchor_id = _subject_node_id(anchor_namespace, anchor[0], anchor[1])
        evidence_anchors[evidence_id] = anchor_id
        for kind, value in subjects:
            target_namespace = _subject_namespace(envelope, kind)
            target_id = _subject_node_id(target_namespace, kind, value)
            if target_id == anchor_id:
                continue
            relation = f"has_{kind.removesuffix('_id')}"
            key = (anchor_id, target_id, relation, evidence_id)
            edges[key] = {
                "from": anchor_id,
                "to": target_id,
                "relation": relation,
                "assertion": envelope.get("assertion"),
                "link_confidence": "observed" if envelope.get("assertion") == "observed" else "claimed",
                "link_basis": "shared_subjects_in_evidence",
                "evidence_id": evidence_id,
                "dimensions": list(_sequence(envelope.get("dimensions"))),
            }
    claimed_link_count = 0
    unresolved_claimed_link_count = 0
    for raw_link in claimed_links:
        link, wrapper = _plain_link(raw_link)
        claimed_id = str(link.get("claimed_evidence_id") or "")
        observed_id = str(link.get("observed_evidence_id") or "")
        from_id = evidence_anchors.get(claimed_id)
        to_id = evidence_anchors.get(observed_id)
        if not from_id or not to_id:
            unresolved_claimed_link_count += 1
            continue
        claimed_link_count += 1
        link_id = str(link.get("link_id") or "unknown")
        validation_state = str(wrapper.get("validation_state") or "unknown")
        relationship = str(link.get("relationship") or "supports")
        key = (from_id, to_id, relationship, link_id)
        edges[key] = {
            "from": from_id,
            "to": to_id,
            "relation": relationship,
            "assertion": "claimed_link",
            "link_confidence": validation_state,
            "link_basis": str(link.get("rationale_code") or "explicit_claim_to_observation_link"),
            "link_id": link_id,
            "claimed_evidence_id": claimed_id,
            "observed_evidence_id": observed_id,
            "dimensions": list(_sequence(link.get("dimensions"))),
        }
    return {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "evidence_without_subjects": evidence_without_subjects,
        "claimed_link_count": claimed_link_count,
        "unresolved_claimed_link_count": unresolved_claimed_link_count,
        "nodes": sorted(nodes.values(), key=lambda node: (node["namespace"], node["kind"], node["value"])),
        "edges": sorted(
            edges.values(),
            key=lambda edge: (edge["from"], edge["to"], str(edge.get("evidence_id") or edge.get("link_id") or "")),
        ),
    }


_DISCREPANCY_GUIDANCE: dict[str, dict[str, str]] = {
    "source_identity_conflict": {
        "title": "A source event has conflicting versions",
        "explanation": (
            "The same deterministic source identity arrived with different normalized content, so agentacct preserved "
            "each version instead of silently replacing one."
        ),
        "recommended_next_step": (
            "Inspect the preserved versions and correct the source adapter identity or normalization rule before "
            "relying on it."
        ),
        "action_code": "inspect_source_identity_conflict",
    },
    "cost_value_conflict": {
        "title": "Cost values disagree",
        "explanation": "Comparable cost evidence for the same scoped fact and basis reports different values.",
        "recommended_next_step": (
            "Inspect the candidate records and resolve source normalization before using this cost."
        ),
        "action_code": "resolve_cost_value_conflict",
    },
    "usage_value_conflict": {
        "title": "Usage values disagree",
        "explanation": "Comparable usage evidence for the same scoped fact reports different values.",
        "recommended_next_step": "Inspect the candidate records and reconcile measurement windows and normalization.",
        "action_code": "resolve_usage_value_conflict",
    },
    "machine_check_value_conflict": {
        "title": "Machine-check results disagree",
        "explanation": "Comparable machine-check evidence for the same scoped fact reports different results.",
        "recommended_next_step": (
            "Inspect the check records and verify that they describe the same command and execution point."
        ),
        "action_code": "resolve_machine_check_value_conflict",
    },
    "outcome_value_conflict": {
        "title": "Outcome reports disagree",
        "explanation": "Comparable outcome evidence for the same scoped fact reports different states.",
        "recommended_next_step": "Inspect the outcome records and reconcile the source-of-truth lifecycle state.",
        "action_code": "resolve_outcome_value_conflict",
    },
    "cost_basis_variance": {
        "title": "Cost sources use different bases",
        "explanation": (
            "Cost evidence differs across measurement bases; this is preserved as variance rather than treated as a "
            "same-basis contradiction."
        ),
        "recommended_next_step": (
            "Compare the stated bases and use provider-billed evidence for invoicing when it is available."
        ),
        "action_code": "review_cost_basis_variance",
    },
    "semantic_context_missing": {
        "title": "Work meaning is missing",
        "explanation": "Observed client activity has no matching claimed task-semantic evidence in the current scope.",
        "recommended_next_step": (
            "Attach a Work Event or MCP semantic report when available; do not infer task meaning from activity alone."
        ),
        "action_code": "attach_semantic_context",
    },
}


def _discrepancy_guidance(kind: str) -> dict[str, str]:
    return dict(
        _DISCREPANCY_GUIDANCE.get(
            kind,
            {
                "title": "Evidence requires inspection",
                "explanation": "agentacct preserved evidence that it could not safely reconcile automatically.",
                "recommended_next_step": "Inspect the normalized evidence and its measurement basis before acting.",
                "action_code": "inspect_evidence_discrepancy",
            },
        )
    )


def build_discrepancies(envelopes: Iterable[Any], *, store_conflicts: Sequence[Any] = ()) -> dict[str, Any]:
    plain = [_plain_envelope(value) for value in envelopes]
    discrepancies: list[dict[str, Any]] = []
    for conflict in store_conflicts:
        data = dict(conflict) if isinstance(conflict, Mapping) else {"detail": str(conflict)}
        discrepancies.append({"kind": "source_identity_conflict", "severity": "high", **data})

    by_scope_dimension: dict[tuple[str, str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    cost_by_fact: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for envelope in plain:
        scope = _comparison_scope(envelope)
        if scope is None:
            continue
        for dimension_value in _sequence(envelope.get("dimensions")):
            dimension = str(dimension_value)
            if dimension in _VALUE_KEYS:
                fact = _measurement_fact(envelope, dimension)
                if fact is not None:
                    family = _basis_family(envelope, dimension)
                    by_scope_dimension[(scope[0], scope[1], scope[2], dimension, fact, family)].append(envelope)
                    if dimension == "cost":
                        cost_by_fact[(scope[0], scope[1], scope[2], fact)].append(envelope)

    for (namespace, scope_kind, scope_id, dimension, fact, family), candidates in sorted(by_scope_dimension.items()):
        values_by_field: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        display_values: dict[tuple[str, str], Any] = {}
        for envelope in candidates:
            payload = _comparison_values(envelope, dimension)
            if not payload:
                continue
            for field, value in payload.items():
                fingerprint = _value_fingerprint(value)
                values_by_field[field][fingerprint].append(str(envelope.get("evidence_id") or "unknown"))
                display_values.setdefault((field, fingerprint), value)
        conflicts = {
            field: variants
            for field, variants in values_by_field.items()
            if len(variants) > 1
        }
        if conflicts:
            discrepancies.append(
                {
                    "kind": f"{dimension}_value_conflict",
                    "severity": "high" if dimension in {"cost", "outcome", "machine_check"} else "medium",
                    "subject": {"namespace": namespace, "kind": scope_kind, "id": scope_id},
                    "fact": fact,
                    "dimension": dimension,
                    "basis_family": family,
                    "candidates": [
                        {"field": field, "value": display_values[(field, value)], "evidence_ids": evidence_ids}
                        for field, variants in sorted(conflicts.items())
                        for value, evidence_ids in sorted(variants.items())
                    ],
                }
            )

    for (namespace, scope_kind, scope_id, fact), candidates in sorted(cost_by_fact.items()):
        values: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        displayed: dict[tuple[str, str], Any] = {}
        bases: dict[str, set[str]] = defaultdict(set)
        for envelope in candidates:
            comparison = _comparison_values(envelope, "cost")
            if "amount_usd" not in comparison:
                continue
            family = _basis_family(envelope, "cost")
            value = comparison["amount_usd"]
            fingerprint = _value_fingerprint(value)
            evidence_id = str(envelope.get("evidence_id") or "unknown")
            values[family][fingerprint].append(evidence_id)
            displayed.setdefault((family, fingerprint), value)
            bases[family].add(str(_mapping(envelope.get("measurement_basis")).get("cost") or "unknown"))
        all_values = {fingerprint for variants in values.values() for fingerprint in variants}
        if len(values) > 1 and len(all_values) > 1:
            discrepancies.append(
                {
                    "kind": "cost_basis_variance",
                    "severity": "info",
                    "subject": {"namespace": namespace, "kind": scope_kind, "id": scope_id},
                    "fact": fact,
                    "dimension": "cost",
                    "candidates": [
                        {
                            "basis_family": family,
                            "measurement_bases": sorted(bases[family]),
                            "value": displayed[(family, fingerprint)],
                            "evidence_ids": evidence_ids,
                        }
                        for family, variants in sorted(values.items())
                        for fingerprint, evidence_ids in sorted(variants.items())
                    ],
                }
            )

    sessions_observed = {
        (scope[0], scope[2])
        for envelope in plain
        if envelope.get("assertion") == "observed"
        and (scope := _subject_scope(envelope)) is not None
        and scope[1] == "client_session_id"
    }
    sessions_with_semantics = {
        (
            _subject_namespace(envelope, "client_session_id"),
            str(_mapping(envelope.get("subjects")).get("client_session_id")),
        )
        for envelope in plain
        if envelope.get("assertion") == "claimed"
        and "task_semantics" in _sequence(envelope.get("dimensions"))
        and _mapping(envelope.get("subjects")).get("client_session_id")
    }
    for namespace, session_id in sorted(sessions_observed - sessions_with_semantics):
        discrepancies.append(
            {
                "kind": "semantic_context_missing",
                "severity": "info",
                "subject": {"namespace": namespace, "kind": "client_session_id", "id": session_id},
                "dimension": "task_semantics",
            }
        )

    discrepancies = [
        {**item, **_discrepancy_guidance(str(item.get("kind") or "unknown"))}
        for item in discrepancies
    ]
    return {
        "count": len(discrepancies),
        "by_kind": dict(sorted(Counter(item["kind"] for item in discrepancies).items())),
        "items": discrepancies,
    }


def build_cost_outcome_basis(envelopes: Iterable[Any]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for raw in envelopes:
        envelope = _plain_envelope(raw)
        for dimension_value in _sequence(envelope.get("dimensions")):
            dimension = str(dimension_value)
            if dimension not in {"cost", "usage", "machine_check", "outcome"}:
                continue
            authority, authority_reason = _dimension_authority(envelope, dimension)
            records.append(
                {
                    "evidence_id": envelope.get("evidence_id"),
                    "event_type": envelope.get("event_type"),
                    "dimension": dimension,
                    "assertion": envelope.get("assertion"),
                    "source_type": envelope.get("source_type"),
                    "source_system": envelope.get("source_system"),
                    "measurement_basis": _mapping(envelope.get("measurement_basis")).get(dimension),
                    "authority": authority,
                    "authority_reason": authority_reason,
                    "completeness": _mapping(envelope.get("completeness")).get("status"),
                    "subject": (
                        {
                            "namespace": scope[0],
                            "kind": scope[1],
                            "id": scope[2],
                        }
                        if (scope := _subject_scope(envelope)) is not None
                        else None
                    ),
                    "values": _dimension_values(envelope, dimension),
                    "occurred_at": envelope.get("event_timestamp"),
                }
            )
    records.sort(key=lambda row: (str(row["dimension"]), str(row["occurred_at"]), str(row["evidence_id"])))
    return {
        "record_count": len(records),
        "by_dimension": dict(sorted(Counter(str(row["dimension"]) for row in records).items())),
        "by_basis": dict(sorted(Counter(str(row["measurement_basis"] or "unknown") for row in records).items())),
        "records": records,
        "aggregation_policy": "claims_are_not_summed_without_non_overlapping_scope_evidence",
    }


def build_evidence_product(
    envelopes: Iterable[Any],
    *,
    store_conflicts: Sequence[Any] = (),
    claimed_links: Iterable[Any] = (),
) -> dict[str, Any]:
    plain = [_plain_envelope(value) for value in envelopes]
    matrix = build_evidence_matrix(plain)
    graph = build_work_graph(plain, claimed_links=claimed_links)
    discrepancies = build_discrepancies(plain, store_conflicts=store_conflicts)
    basis = build_cost_outcome_basis(plain)
    source_coverage = build_source_coverage(plain)
    work_evidence = build_work_evidence(plain)
    return {
        "schema_version": EVIDENCE_PRODUCT_SCHEMA_VERSION,
        "summary": {
            "evidence_count": len(plain),
            "observed_count": sum(1 for envelope in plain if envelope.get("assertion") == "observed"),
            "claimed_count": sum(1 for envelope in plain if envelope.get("assertion") == "claimed"),
            "source_count": matrix["source_count"],
            "discrepancy_count": discrepancies["count"],
        },
        "work_graph": graph,
        "evidence_matrix": matrix,
        "discrepancies": discrepancies,
        "cost_outcome_basis": basis,
        "source_coverage": source_coverage,
        "work_evidence": work_evidence,
    }


__all__ = [
    "EVIDENCE_PRODUCT_SCHEMA_VERSION",
    "WORK_EVIDENCE_RECORD_LIMIT",
    "build_cost_outcome_basis",
    "build_discrepancies",
    "build_evidence_matrix",
    "build_evidence_product",
    "build_source_coverage",
    "build_work_evidence",
    "build_work_graph",
]
