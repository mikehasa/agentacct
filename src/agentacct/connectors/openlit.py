"""Metadata-only OpenLIT/OTLP HTTP JSON trace adapter.

Only explicitly allowlisted resource and span attributes cross this boundary.
Prompt/response content, thoughts, tool arguments/results, commands, paths,
exception messages, and arbitrary vendor attributes are never copied.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .base import ConnectorError, ConnectorRecord, ReadOnlyConnector, canonical_json, load_json_document, stable_digest


OPENLIT_UPSTREAM_SHA = "8adf21c8f952c0768fd5ff85d853798bb3c028f3"
OPENLIT_LICENSE = "Apache-2.0"

_HEX_ID = re.compile(r"^[0-9a-fA-F]{8,64}$")
_MAX_OTLP_UNIX_NANOS = (1 << 64) - 1

_RESOURCE_ATTRIBUTES = {
    "service.name": "service_name",
    "service.version": "service_version",
    "deployment.environment": "deployment_environment",
    "deployment.environment.name": "deployment_environment",
    "telemetry.sdk.name": "telemetry_sdk_name",
    "telemetry.sdk.version": "telemetry_sdk_version",
}

# Exact-key allowlist.  It intentionally contains no text/body/path fields.
_SPAN_ATTRIBUTES = {
    "coding_agent.session.id": "session_id",
    "gen_ai.conversation.id": "conversation_id",
    "session.id": "session_id",
    "coding_agent.client": "client_name",
    "coding_agent.client.name": "client_name",
    "coding_agent.client.version": "client_version",
    "coding_agent.schema.version": "coding_agent_schema_version",
    "coding_agent.signal.source": "signal_source",
    "coding_agent.capture.level": "capture_level_claim",
    "coding_agent.session.outcome": "session_outcome",
    "coding_agent.session.duration_ms": "session_duration_ms",
    "coding_agent.session.tool_count": "session_tool_count",
    "coding_agent.session.subagent_count": "session_subagent_count",
    "coding_agent.turn.id": "turn_id",
    "coding_agent.turn.kind": "turn_kind",
    "coding_agent.turn.attachment_count": "attachment_count",
    "coding_agent.turn.thought_duration_ms": "thought_duration_ms",
    "coding_agent.tool.group": "tool_group",
    "coding_agent.tool.iteration": "tool_iteration",
    "coding_agent.tool.triggering_request_id": "triggering_request_id",
    "coding_agent.tool.sandboxed": "sandboxed",
    "coding_agent.tool.error": "tool_error",
    "coding_agent.tool.interrupted": "tool_interrupted",
    "coding_agent.tool.duration_ms": "tool_duration_ms",
    "coding_agent.mcp.server": "mcp_server",
    "coding_agent.mcp.scope": "mcp_scope",
    "coding_agent.mcp.transport": "mcp_transport",
    "coding_agent.lines.added": "lines_added",
    "coding_agent.lines.removed": "lines_removed",
    "coding_agent.files.changed": "files_changed",
    "coding_agent.commits.created": "commits_created",
    "coding_agent.pull_requests.created": "pull_requests_created",
    "agent_chronicle.completeness": "completeness_claim",
    "gen_ai.operation.name": "operation_name",
    "gen_ai.provider.name": "provider_name",
    "gen_ai.system": "provider_name",
    "gen_ai.request.model": "request_model",
    "gen_ai.response.model": "response_model",
    "gen_ai.agent.name": "agent_name",
    "gen_ai.tool.name": "tool_name",
    "gen_ai.tool.call.id": "tool_call_id",
    "gen_ai.usage.input_tokens": "input_tokens",
    "gen_ai.usage.output_tokens": "output_tokens",
    "gen_ai.usage.total_tokens": "total_tokens",
    "gen_ai.usage.cache_read_tokens": "cache_read_tokens",
    "gen_ai.usage.cache_write_tokens": "cache_write_tokens",
    "gen_ai.usage.cost": "cost_usd",
    "gen_ai.usage.cost_usd": "cost_usd",
    "llm.token_count.prompt": "input_tokens",
    "llm.token_count.completion": "output_tokens",
    "llm.token_count.total": "total_tokens",
    "openlit.cost": "cost_usd",
}

_NUMERIC_FIELDS = frozenset(
    {
        "session_duration_ms",
        "session_tool_count",
        "session_subagent_count",
        "attachment_count",
        "thought_duration_ms",
        "tool_iteration",
        "tool_duration_ms",
        "lines_added",
        "lines_removed",
        "files_changed",
        "commits_created",
        "pull_requests_created",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cost_usd",
    }
)
_USAGE_FIELDS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cost_usd",
    }
)


def _otlp_value(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value if isinstance(value, (str, bool, int, float)) else None
    if "stringValue" in value:
        return value["stringValue"] if isinstance(value["stringValue"], str) else None
    if "boolValue" in value:
        return value["boolValue"] if isinstance(value["boolValue"], bool) else None
    if "intValue" in value:
        raw = value["intValue"]
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    if "doubleValue" in value:
        raw = value["doubleValue"]
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None
    if "arrayValue" in value and isinstance(value["arrayValue"], Mapping):
        values = value["arrayValue"].get("values", [])
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
            return [_otlp_value(item) for item in values]
    return None


def _attributes(value: Any) -> list[tuple[str, Any]]:
    if isinstance(value, Mapping):
        return [(str(key), item) for key, item in value.items()]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    output: list[tuple[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping) or not isinstance(item.get("key"), str):
            continue
        output.append((item["key"], _otlp_value(item.get("value"))))
    return output


def _safe_value(field: str, value: Any) -> Any:
    if field in _NUMERIC_FIELDS:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or number < 0:
            return None
        if field != "cost_usd" and number.is_integer():
            return int(number)
        return number
    if isinstance(value, str):
        if not value.strip() or len(value) > 256 or "\n" in value or "\r" in value:
            return None
        return value
    if isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _allowlisted(value: Any, allowlist: Mapping[str, str]) -> tuple[dict[str, Any], int]:
    safe: dict[str, Any] = {}
    dropped = 0
    for key, raw in _attributes(value):
        canonical = allowlist.get(key)
        if canonical is None:
            dropped += 1
            continue
        parsed = _safe_value(canonical, raw)
        if parsed is None:
            dropped += 1
            continue
        if canonical not in safe:
            safe[canonical] = parsed
        elif safe[canonical] != parsed:
            # Conflicting aliases do not overwrite the first canonical value.
            dropped += 1
    return safe, dropped


def _valid_id(value: Any) -> str | None:
    if isinstance(value, str) and _HEX_ID.fullmatch(value):
        return value.lower()
    return None


def _parse_otlp_nanos(value: Any, *, field: str) -> int | None:
    """Parse OTLP's uint64 nanosecond timestamps without leaking raw input."""

    if value is None:
        return None
    if isinstance(value, bool):
        raise ConnectorError(f"OTLP {field} must be an unsigned nanosecond timestamp")
    try:
        nanos = int(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ConnectorError(f"OTLP {field} must be an unsigned nanosecond timestamp") from exc
    if nanos < 0 or nanos > _MAX_OTLP_UNIX_NANOS:
        raise ConnectorError(f"OTLP {field} is outside the supported uint64 range")
    return nanos


def _iso_from_nanos(value: Any, *, field: str) -> str | None:
    nanos = _parse_otlp_nanos(value, field=field)
    if nanos is None:
        return None
    seconds, remainder = divmod(nanos, 1_000_000_000)
    try:
        timestamp = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(
            microsecond=remainder // 1_000
        )
    except (OverflowError, OSError, ValueError) as exc:
        raise ConnectorError(f"OTLP {field} is outside the supported datetime range") from exc
    return timestamp.isoformat().replace("+00:00", "Z")


def _duration_ms(start: Any, end: Any) -> float | None:
    start_nanos = _parse_otlp_nanos(start, field="startTimeUnixNano")
    end_nanos = _parse_otlp_nanos(end, field="endTimeUnixNano")
    if start_nanos is None or end_nanos is None:
        return None
    difference = end_nanos - start_nanos
    if difference < 0:
        return None
    return difference / 1_000_000


def _candidate_content_key(candidate: Mapping[str, Any]) -> str:
    """Identity of retained OTLP content, excluding denied/raw attributes.

    OpenTelemetry attributes are often re-exported with a different set of
    body fields that this metadata-only adapter deliberately drops.  Those are
    equivalent here.  Any difference in retained attributes, resource,
    timing, status, classification, or trace ancestry is a distinct version
    and must reach Evidence Store under the same source identity.
    """

    return canonical_json(
        {
            key: value
            for key, value in candidate.items()
            if key not in {"dedupe_key", "dropped", "raw_digest"}
        }
    )


def _classify_span(span: Mapping[str, Any], safe: Mapping[str, Any]) -> str:
    name = span.get("name")
    operation = str(safe.get("operation_name", "")).lower()
    exact_name = name.lower() if isinstance(name, str) else ""
    combined = {exact_name, operation}
    if combined & {"coding_agent.session", "session", "agent.session"}:
        return "coding_agent.session.observed"
    if combined & {"coding_agent.llm.turn", "coding_agent.turn", "chat", "generate_content"}:
        return "coding_agent.turn.observed"
    if combined & {"coding_agent.tool_call", "coding_agent.tool.call", "tool", "execute_tool"}:
        return "coding_agent.tool.observed"
    if safe.get("tool_call_id") or safe.get("tool_name"):
        return "coding_agent.tool.observed"
    if safe.get("turn_id"):
        return "coding_agent.turn.observed"
    if safe.get("session_id"):
        return "coding_agent.session.observed"
    return "coding_agent.span.observed"


class OpenLITOTLPConnector(ReadOnlyConnector):
    name = "openlit"
    source_type = "otlp_http_json"
    upstream_sha = OPENLIT_UPSTREAM_SHA
    license_id = OPENLIT_LICENSE

    def read(self, source: Any = None) -> tuple[ConnectorRecord, ...]:
        document = load_json_document(source)
        if not isinstance(document, Mapping):
            raise ConnectorError("OTLP JSON root must be an object")
        resource_spans = document.get("resourceSpans", [])
        if not isinstance(resource_spans, Sequence) or isinstance(resource_spans, (str, bytes, bytearray)):
            raise ConnectorError("resourceSpans must be an array")

        candidates: dict[str, list[dict[str, Any]]] = {}
        for resource_group in resource_spans:
            if not isinstance(resource_group, Mapping):
                continue
            resource = resource_group.get("resource", {})
            raw_resource_attrs = resource.get("attributes", []) if isinstance(resource, Mapping) else []
            resource_attrs, resource_dropped = _allowlisted(raw_resource_attrs, _RESOURCE_ATTRIBUTES)
            scope_groups = resource_group.get("scopeSpans", resource_group.get("instrumentationLibrarySpans", []))
            if not isinstance(scope_groups, Sequence) or isinstance(scope_groups, (str, bytes, bytearray)):
                continue
            for scope_group in scope_groups:
                if not isinstance(scope_group, Mapping):
                    continue
                spans = scope_group.get("spans", [])
                if not isinstance(spans, Sequence) or isinstance(spans, (str, bytes, bytearray)):
                    continue
                for span in spans:
                    if not isinstance(span, Mapping):
                        continue
                    safe, dropped = _allowlisted(span.get("attributes", []), _SPAN_ATTRIBUTES)
                    start_nanos = _parse_otlp_nanos(
                        span.get("startTimeUnixNano"),
                        field="startTimeUnixNano",
                    )
                    end_nanos = _parse_otlp_nanos(
                        span.get("endTimeUnixNano"),
                        field="endTimeUnixNano",
                    )
                    trace_id = _valid_id(span.get("traceId"))
                    span_id = _valid_id(span.get("spanId"))
                    parent_span_id = _valid_id(span.get("parentSpanId"))
                    if trace_id and span_id:
                        dedupe_key = f"{trace_id}:{span_id}"
                    else:
                        dedupe_key = "digest:" + stable_digest(
                            {
                                "attributes": safe,
                                "end": end_nanos,
                                "resource": resource_attrs,
                                "start": start_nanos,
                            }
                        )
                    candidate = {
                        "dedupe_key": dedupe_key,
                        "trace_id": trace_id,
                        "span_id": span_id,
                        "parent_span_id": parent_span_id,
                        "safe": safe,
                        "resource": resource_attrs,
                        "dropped": dropped + resource_dropped,
                        "start": start_nanos,
                        "end": end_nanos,
                        "status_code": (
                            span.get("status", {}).get("code")
                            if isinstance(span.get("status"), Mapping)
                            else None
                        ),
                        "kind": _classify_span(span, safe),
                        "raw_digest": stable_digest(span),
                    }
                    candidates.setdefault(dedupe_key, []).append(candidate)

        records: list[ConnectorRecord] = []
        for dedupe_key in sorted(candidates):
            versions: dict[str, dict[str, Any]] = {}
            for candidate in candidates[dedupe_key]:
                content_key = _candidate_content_key(candidate)
                current = versions.get(content_key)
                if current is None or (
                    int(candidate["dropped"]), str(candidate["raw_digest"])
                ) > (
                    int(current["dropped"]), str(current["raw_digest"])
                ):
                    # Equivalent retained content remains one deterministic
                    # replay version. Differences in retained content are not
                    # collapsed and therefore become Evidence Store conflicts.
                    versions[content_key] = candidate
            for content_key in sorted(versions):
                records.extend(self._records_for_candidate(versions[content_key]))
        return tuple(
            sorted(
                records,
                key=lambda record: (record.record_id, canonical_json(record.to_dict())),
            )
        )

    def _records_for_candidate(self, candidate: Mapping[str, Any]) -> list[ConnectorRecord]:
        safe = dict(candidate["safe"])
        resource = dict(candidate["resource"])
        source_event_id = str(candidate["dedupe_key"])
        occurred_at = _iso_from_nanos(
            candidate.get("start"),
            field="startTimeUnixNano",
        )
        # Although the connector does not currently project an end timestamp,
        # validate it at the trust boundary so malformed input cannot surface
        # later as an unstructured overflow/500.
        _iso_from_nanos(candidate.get("end"), field="endTimeUnixNano")
        duration = _duration_ms(candidate.get("start"), candidate.get("end"))
        completeness = safe.pop("completeness_claim", "unknown")
        if completeness not in {"complete", "partial", "unknown"}:
            completeness = "unknown"

        # Usage is emitted exactly once in the dedicated usage observation.
        # Activity spans retain model/tool/lifecycle metadata but not counters
        # or cost, preventing downstream projections from double counting.
        attributes: dict[str, Any] = {
            key: value for key, value in safe.items() if key not in _USAGE_FIELDS
        }
        if resource:
            attributes["resource"] = resource
        if candidate.get("dropped"):
            attributes["dropped_attribute_count"] = int(candidate["dropped"])
        if duration is not None:
            attributes["span_duration_ms"] = duration
        status_code = candidate.get("status_code")
        if isinstance(status_code, (str, int)):
            attributes["status_code"] = status_code

        trace_id = candidate.get("trace_id")
        span_id = candidate.get("span_id")
        parent_span_id = candidate.get("parent_span_id")
        subjects = {
            "client_session": str(safe.get("session_id", "")),
            "turn": str(safe.get("turn_id", "")),
            "tool_call": str(safe.get("tool_call_id", "")),
            "trace": str(trace_id or ""),
            "span": str(span_id or ""),
            "parent_span": str(parent_span_id or ""),
            "principal": str(safe.get("agent_name", "")),
        }
        usage_present = any(field in safe for field in _USAGE_FIELDS - {"cost_usd"})
        cost_present = "cost_usd" in safe
        source_instance_id = str(resource.get("service_name", "local"))
        common = dict(
            connector=self.name,
            source_type=self.source_type,
            source_instance_id=source_instance_id,
            evidence_type="observation",
            occurred_at=occurred_at,
            observed_at=occurred_at,
            measurement_basis="telemetry_reported",
            completeness=completeness,
            subjects=subjects,
            capture_level="metadata_only",
            attribution="direct",
            raw_digest=str(candidate["raw_digest"]),
            upstream_sha=self.upstream_sha,
            license_id=self.license_id,
        )
        records = [
            ConnectorRecord(
                source_event_id=source_event_id,
                event_kind=str(candidate["kind"]),
                attributes=attributes,
                usage_confidence="unknown",
                cost_confidence="unknown",
                **common,
            )
        ]
        if usage_present or cost_present:
            usage_attributes = {field: safe[field] for field in sorted(_USAGE_FIELDS) if field in safe}
            for field in ("provider_name", "request_model", "response_model"):
                if field in safe:
                    usage_attributes[field] = safe[field]
            records.append(
                ConnectorRecord(
                    source_event_id=f"{source_event_id}:usage",
                    event_kind="coding_agent.usage.observed",
                    attributes=usage_attributes,
                    usage_confidence="telemetry_reported" if usage_present else "unknown",
                    cost_confidence="telemetry_reported" if cost_present else "unknown",
                    **common,
                )
            )
        return records
