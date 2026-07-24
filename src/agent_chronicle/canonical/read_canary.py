"""Side-effect-free HTTP canary for the canonical JSON read surfaces.

The canary deliberately probes only GET endpoints on the local agentacct
dashboard.  A successful HTTP response is not enough: every product payload
must explicitly prove that canonical reads are active, and the two surrounding
``/health`` snapshots must prove that the same live, current canonical store is
ready and that the expected per-surface canonical-read counters advanced
without any fallback or error counter advancing.

The running phase-4 service currently exposes ``attempts``/``served``/
``unavailable``.  The result normalizes those wire names to the phase-5
operator vocabulary ``total_reads``/``canonical_reads``/``fallback_reads``;
future health payloads that expose the normalized names directly are accepted
as well.
"""

from __future__ import annotations

import http.client
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol
from urllib.parse import quote

from .sqlite import MINIMAL_READ_MODEL_NAMES, SCHEMA_VERSION


READ_CANARY_SCHEMA_VERSION = "agent-chronicle.canonical-read-canary.v1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_TIMEOUT_SECONDS = 2.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

_EXPECTED_SERVICE = "agent-sentinel-local-api"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_BASE_PROBES = (
    ("/usage/summary?days=all", "usage_days"),
    ("/tasks", "task_list"),
    ("/sessions", "session_list"),
)
_COUNTER_ALIASES = {
    "total_reads": ("total_reads", "attempts"),
    "canonical_reads": ("canonical_reads", "served"),
    "fallback_reads": ("fallback_reads", "unavailable"),
    "errors": ("errors",),
    "unavailable": ("unavailable",),
}


@dataclass(frozen=True)
class ReadCanaryHttpResponse:
    """The small transport contract used by the verifier and its tests."""

    status_code: int
    body: bytes


class ReadCanaryTransport(Protocol):
    """Injectable transport; implementations must perform one GET only."""

    def get(
        self, endpoint: str, *, timeout_seconds: float
    ) -> ReadCanaryHttpResponse: ...


class StdlibReadCanaryTransport:
    """Direct HTTP transport that ignores proxy settings and redirects."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        self.host = host
        self.port = port

    def get(
        self, endpoint: str, *, timeout_seconds: float
    ) -> ReadCanaryHttpResponse:
        connection: http.client.HTTPConnection | None = None
        try:
            connection = http.client.HTTPConnection(
                self.host,
                self.port,
                timeout=timeout_seconds,
            )
            connection.request(
                "GET",
                endpoint,
                headers={"Accept": "application/json", "Connection": "close"},
            )
            response = connection.getresponse()
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(
                    f"response exceeded {MAX_RESPONSE_BYTES} byte canary limit"
                )
            return ReadCanaryHttpResponse(status_code=response.status, body=body)
        finally:
            if connection is not None:
                connection.close()


@dataclass(frozen=True)
class ReadCanaryBlocker:
    """One stable, machine-readable reason the cutover canary is not ready."""

    code: str
    message: str
    phase: str
    endpoint: str | None = None
    surface: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "phase": self.phase,
            "endpoint": self.endpoint,
            "surface": self.surface,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class ReadCanaryHealthObservation:
    """Bounded observation of one surrounding health request."""

    phase: str
    http_status: int | None
    passed: bool
    canonical_read_enabled: bool | None
    canonical_store_available: bool | None
    canonical_store_uuid: str | None
    canonical_store_schema_version: int | None
    canonical_store_ready: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "http_status": self.http_status,
            "passed": self.passed,
            "canonical_read_enabled": self.canonical_read_enabled,
            "canonical_store_available": self.canonical_store_available,
            "canonical_store_uuid": self.canonical_store_uuid,
            "canonical_store_schema_version": self.canonical_store_schema_version,
            "canonical_store_ready": self.canonical_store_ready,
        }


@dataclass(frozen=True)
class ReadCanaryProbe:
    """Contract result for one canonical product endpoint."""

    endpoint: str
    surface: str
    http_status: int | None
    passed: bool
    canonical_read_active: bool | None
    source: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "surface": self.surface,
            "http_status": self.http_status,
            "passed": self.passed,
            "canonical_read_active": self.canonical_read_active,
            "source": self.source,
        }


@dataclass(frozen=True)
class ReadCanarySkippedSurface:
    """A surface the verifier intentionally did not claim to probe."""

    surface: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"surface": self.surface, "reason": self.reason}


@dataclass(frozen=True)
class ReadCanaryCounterDelta:
    """Normalized before/after counters for one attempted read surface."""

    surface: str
    expected_minimum_reads: int
    before: Mapping[str, int]
    after: Mapping[str, int]
    delta: Mapping[str, int]
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "expected_minimum_reads": self.expected_minimum_reads,
            "before": dict(self.before),
            "after": dict(self.after),
            "delta": dict(self.delta),
            "passed": self.passed,
        }


@dataclass(frozen=True)
class ReadCanaryResult:
    """Typed phase-5 readiness result; ``ready`` is false on any blocker."""

    ready: bool
    target: str
    timeout_seconds: float
    health: tuple[ReadCanaryHealthObservation, ...]
    probes: tuple[ReadCanaryProbe, ...]
    probed_surfaces: tuple[str, ...]
    skipped_surfaces: tuple[ReadCanarySkippedSurface, ...]
    counter_deltas: tuple[ReadCanaryCounterDelta, ...]
    blockers: tuple[ReadCanaryBlocker, ...]
    task_detail_probed: bool
    task_detail_skipped_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": READ_CANARY_SCHEMA_VERSION,
            "ready": self.ready,
            "target": self.target,
            "timeout_seconds": self.timeout_seconds,
            "health": [item.to_dict() for item in self.health],
            "probes": [item.to_dict() for item in self.probes],
            "probed_surfaces": list(self.probed_surfaces),
            "skipped_surfaces": [item.to_dict() for item in self.skipped_surfaces],
            "counter_deltas": [item.to_dict() for item in self.counter_deltas],
            "task_detail_probed": self.task_detail_probed,
            "task_detail_skipped_reason": self.task_detail_skipped_reason,
            "blockers": [item.to_dict() for item in self.blockers],
        }


@dataclass(frozen=True)
class _FetchedJson:
    status_code: int | None
    payload: Mapping[str, Any] | None


@dataclass(frozen=True)
class _HealthState:
    enabled: bool | None
    surfaces: Mapping[str, Any] | None
    store_uuid: str | None


def _target(host: str, port: int) -> str:
    rendered_host = f"[{host}]" if ":" in host else host
    return f"http://{rendered_host}:{port}"


def _bounded(value: object, limit: int = 500) -> str:
    return str(value)[:limit]


def _configuration_blocker(
    *, host: object, port: object, timeout_seconds: object
) -> ReadCanaryBlocker | None:
    if not isinstance(host, str) or host not in _LOOPBACK_HOSTS:
        return ReadCanaryBlocker(
            code="invalid_target",
            message="canonical read canary permits loopback targets only",
            phase="configuration",
            details={"host": _bounded(host)},
        )
    if (
        not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65535
    ):
        return ReadCanaryBlocker(
            code="invalid_port",
            message="port must be an integer from 1 through 65535",
            phase="configuration",
            details={"port": _bounded(port)},
        )
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not math.isfinite(float(timeout_seconds))
        or float(timeout_seconds) <= 0
    ):
        return ReadCanaryBlocker(
            code="invalid_timeout",
            message="timeout_seconds must be finite and greater than zero",
            phase="configuration",
            details={"timeout_seconds": _bounded(timeout_seconds)},
        )
    return None


def _fetch_json(
    transport: ReadCanaryTransport,
    endpoint: str,
    *,
    timeout_seconds: float,
    phase: str,
    surface: str | None,
    blockers: list[ReadCanaryBlocker],
) -> _FetchedJson:
    try:
        response = transport.get(endpoint, timeout_seconds=timeout_seconds)
    except Exception as exc:  # noqa: BLE001 - every operational failure is a typed blocker.
        blockers.append(
            ReadCanaryBlocker(
                code="http_error",
                message="HTTP request failed",
                phase=phase,
                endpoint=endpoint,
                surface=surface,
                details={"error": f"{type(exc).__name__}: {_bounded(exc)}"},
            )
        )
        return _FetchedJson(status_code=None, payload=None)

    try:
        status = response.status_code
        body = response.body
    except Exception as exc:  # noqa: BLE001 - an injected transport can violate its protocol.
        blockers.append(
            ReadCanaryBlocker(
                code="transport_contract_invalid",
                message="transport response lacks the required status_code/body fields",
                phase=phase,
                endpoint=endpoint,
                surface=surface,
                details={"error": f"{type(exc).__name__}: {_bounded(exc)}"},
            )
        )
        return _FetchedJson(status_code=None, payload=None)
    if not isinstance(status, int) or isinstance(status, bool):
        blockers.append(
            ReadCanaryBlocker(
                code="transport_contract_invalid",
                message="transport returned a non-integer HTTP status",
                phase=phase,
                endpoint=endpoint,
                surface=surface,
            )
        )
        return _FetchedJson(status_code=None, payload=None)
    if status != 200:
        blockers.append(
            ReadCanaryBlocker(
                code="http_status",
                message="endpoint did not return HTTP 200",
                phase=phase,
                endpoint=endpoint,
                surface=surface,
                details={"status_code": status},
            )
        )
        return _FetchedJson(status_code=status, payload=None)
    if not isinstance(body, bytes):
        blockers.append(
            ReadCanaryBlocker(
                code="transport_contract_invalid",
                message="transport returned a non-bytes response body",
                phase=phase,
                endpoint=endpoint,
                surface=surface,
            )
        )
        return _FetchedJson(status_code=status, payload=None)
    try:
        decoded = body.decode("utf-8")
        payload = json.loads(decoded)
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        blockers.append(
            ReadCanaryBlocker(
                code="invalid_json",
                message="endpoint did not return valid UTF-8 JSON",
                phase=phase,
                endpoint=endpoint,
                surface=surface,
                details={"error": f"{type(exc).__name__}: {_bounded(exc)}"},
            )
        )
        return _FetchedJson(status_code=status, payload=None)
    if not isinstance(payload, Mapping):
        blockers.append(
            ReadCanaryBlocker(
                code="json_not_object",
                message="endpoint JSON must be an object",
                phase=phase,
                endpoint=endpoint,
                surface=surface,
            )
        )
        return _FetchedJson(status_code=status, payload=None)
    return _FetchedJson(status_code=status, payload=payload)


def _canonical_store_state(
    payload: Mapping[str, Any],
    *,
    phase: str,
    expected_store_uuid: str | None,
    blockers: list[ReadCanaryBlocker],
) -> tuple[bool | None, str | None, int | None]:
    canonical_store = payload.get("canonical_store")
    if not isinstance(canonical_store, Mapping):
        blockers.append(
            ReadCanaryBlocker(
                code="canonical_store_missing",
                message="health response lacks canonical_store readiness evidence",
                phase=phase,
                endpoint="/health",
            )
        )
        return None, None, None

    raw_available = canonical_store.get("available")
    available = raw_available if isinstance(raw_available, bool) else None
    if raw_available is not True:
        blockers.append(
            ReadCanaryBlocker(
                code="canonical_store_unavailable",
                message="canonical store is not available for cutover reads",
                phase=phase,
                endpoint="/health",
                details={
                    "available": raw_available,
                    "reason": _bounded(canonical_store.get("reason")),
                },
            )
        )
        return available, None, None

    store = canonical_store.get("store")
    if not isinstance(store, Mapping):
        blockers.append(
            ReadCanaryBlocker(
                code="canonical_store_invalid",
                message="canonical_store.store must be an object",
                phase=phase,
                endpoint="/health",
            )
        )
        return available, None, None

    raw_uuid = store.get("store_uuid")
    store_uuid = (
        raw_uuid
        if isinstance(raw_uuid, str)
        and bool(raw_uuid)
        and raw_uuid.strip() == raw_uuid
        else None
    )
    if store_uuid is None:
        blockers.append(
            ReadCanaryBlocker(
                code="canonical_store_uuid_invalid",
                message="canonical store UUID must be a non-empty normalized string",
                phase=phase,
                endpoint="/health",
                details={"store_uuid": _bounded(raw_uuid)},
            )
        )
    elif expected_store_uuid is not None and store_uuid != expected_store_uuid:
        blockers.append(
            ReadCanaryBlocker(
                code="canonical_store_uuid_changed",
                message="canonical store UUID changed between health snapshots",
                phase=phase,
                endpoint="/health",
                details={
                    "initial_store_uuid": expected_store_uuid,
                    "final_store_uuid": store_uuid,
                },
            )
        )

    if store.get("store_role") != "live":
        blockers.append(
            ReadCanaryBlocker(
                code="canonical_store_role_invalid",
                message="canonical store role is not live",
                phase=phase,
                endpoint="/health",
                details={"store_role": _bounded(store.get("store_role"))},
            )
        )

    raw_schema_version = store.get("schema_version")
    schema_version = (
        raw_schema_version
        if isinstance(raw_schema_version, int)
        and not isinstance(raw_schema_version, bool)
        else None
    )
    if schema_version != SCHEMA_VERSION:
        blockers.append(
            ReadCanaryBlocker(
                code="canonical_store_schema_mismatch",
                message="canonical store schema is not current",
                phase=phase,
                endpoint="/health",
                details={
                    "schema_version": raw_schema_version,
                    "expected_schema_version": SCHEMA_VERSION,
                },
            )
        )

    projections = canonical_store.get("projections")
    if not isinstance(projections, Mapping):
        blockers.append(
            ReadCanaryBlocker(
                code="canonical_projections_missing",
                message="canonical store readiness lacks projection evidence",
                phase=phase,
                endpoint="/health",
            )
        )
        return available, store_uuid, schema_version
    for projection_name in MINIMAL_READ_MODEL_NAMES:
        projection = projections.get(projection_name)
        if not isinstance(projection, Mapping):
            blockers.append(
                ReadCanaryBlocker(
                    code="canonical_projection_missing",
                    message="canonical store lacks a required minimal projection",
                    phase=phase,
                    endpoint="/health",
                    surface=projection_name,
                )
            )
            continue
        state = projection.get("state")
        stale = projection.get("stale")
        pending_writes = projection.get("pending_writes")
        if (
            state != "current"
            or stale is not False
            or not isinstance(pending_writes, int)
            or isinstance(pending_writes, bool)
            or pending_writes != 0
        ):
            blockers.append(
                ReadCanaryBlocker(
                    code="canonical_projection_not_ready",
                    message="canonical minimal projection is not current and fully caught up",
                    phase=phase,
                    endpoint="/health",
                    surface=projection_name,
                    details={
                        "state": _bounded(state),
                        "stale": stale,
                        "pending_writes": pending_writes,
                    },
                )
            )
    return available, store_uuid, schema_version


def _health_state(
    fetched: _FetchedJson,
    *,
    phase: str,
    expected_store_uuid: str | None,
    blockers: list[ReadCanaryBlocker],
) -> tuple[_HealthState, ReadCanaryHealthObservation]:
    before_count = len(blockers)
    payload = fetched.payload
    if payload is None:
        return (
            _HealthState(enabled=None, surfaces=None, store_uuid=None),
            ReadCanaryHealthObservation(
                phase=phase,
                http_status=fetched.status_code,
                passed=False,
                canonical_read_enabled=None,
                canonical_store_available=None,
                canonical_store_uuid=None,
                canonical_store_schema_version=None,
                canonical_store_ready=False,
            ),
        )
    if payload.get("ok") is not True:
        blockers.append(
            ReadCanaryBlocker(
                code="health_not_ok",
                message="dashboard health did not report ok=true",
                phase=phase,
                endpoint="/health",
            )
        )
    if payload.get("service") != _EXPECTED_SERVICE:
        blockers.append(
            ReadCanaryBlocker(
                code="unexpected_service",
                message="health response is not the agentacct local API",
                phase=phase,
                endpoint="/health",
                details={"service": _bounded(payload.get("service"))},
            )
        )
    read_status = payload.get("canonical_read")
    if not isinstance(read_status, Mapping):
        blockers.append(
            ReadCanaryBlocker(
                code="canonical_health_missing",
                message="health response lacks canonical_read status",
                phase=phase,
                endpoint="/health",
            )
        )
        enabled: bool | None = None
        surfaces: Mapping[str, Any] | None = None
    else:
        raw_enabled = read_status.get("enabled")
        enabled = raw_enabled if isinstance(raw_enabled, bool) else None
        if raw_enabled is not True:
            blockers.append(
                ReadCanaryBlocker(
                    code="canonical_read_disabled",
                    message="canonical read flag is not enabled in the dashboard process",
                    phase=phase,
                    endpoint="/health",
                    details={"enabled": raw_enabled},
                )
            )
        raw_surfaces = read_status.get("surfaces")
        surfaces = raw_surfaces if isinstance(raw_surfaces, Mapping) else None
        if surfaces is None:
            blockers.append(
                ReadCanaryBlocker(
                    code="surface_counters_missing",
                    message="health response lacks per-surface canonical counters",
                    phase=phase,
                    endpoint="/health",
                )
            )
    store_blocker_count = len(blockers)
    store_available, store_uuid, store_schema_version = _canonical_store_state(
        payload,
        phase=phase,
        expected_store_uuid=expected_store_uuid,
        blockers=blockers,
    )
    return (
        _HealthState(enabled=enabled, surfaces=surfaces, store_uuid=store_uuid),
        ReadCanaryHealthObservation(
            phase=phase,
            http_status=fetched.status_code,
            passed=len(blockers) == before_count,
            canonical_read_enabled=enabled,
            canonical_store_available=store_available,
            canonical_store_uuid=store_uuid,
            canonical_store_schema_version=store_schema_version,
            canonical_store_ready=len(blockers) == store_blocker_count,
        ),
    )


def _probe_contract(
    fetched: _FetchedJson,
    *,
    endpoint: str,
    surface: str,
    blockers: list[ReadCanaryBlocker],
) -> ReadCanaryProbe:
    before_count = len(blockers)
    label = fetched.payload.get("canonical_read") if fetched.payload is not None else None
    active: bool | None = None
    source: str | None = None
    if fetched.payload is not None:
        if not isinstance(label, Mapping):
            blockers.append(
                ReadCanaryBlocker(
                    code="canonical_label_missing",
                    message="product payload lacks a canonical_read object",
                    phase="probe",
                    endpoint=endpoint,
                    surface=surface,
                )
            )
        else:
            raw_active = label.get("active")
            active = raw_active if isinstance(raw_active, bool) else None
            raw_source = label.get("source")
            source = raw_source if isinstance(raw_source, str) else None
            if raw_active is not True:
                blockers.append(
                    ReadCanaryBlocker(
                        code="canonical_read_inactive",
                        message="product payload did not prove canonical_read.active=true",
                        phase="probe",
                        endpoint=endpoint,
                        surface=surface,
                        details={"active": raw_active},
                    )
                )
            if raw_source != "canonical":
                blockers.append(
                    ReadCanaryBlocker(
                        code="canonical_source_mismatch",
                        message="product payload source is not canonical",
                        phase="probe",
                        endpoint=endpoint,
                        surface=surface,
                        details={"source": raw_source},
                    )
                )
    return ReadCanaryProbe(
        endpoint=endpoint,
        surface=surface,
        http_status=fetched.status_code,
        passed=fetched.payload is not None and len(blockers) == before_count,
        canonical_read_active=active,
        source=source,
    )


def _task_id(
    payload: Mapping[str, Any] | None,
    *,
    blockers: list[ReadCanaryBlocker],
) -> str | None:
    if payload is None:
        return None
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        blockers.append(
            ReadCanaryBlocker(
                code="tasks_contract_invalid",
                message="canonical task-list payload must contain a tasks array",
                phase="probe",
                endpoint="/tasks",
                surface="task_list",
            )
        )
        return None
    for index, task in enumerate(tasks):
        if not isinstance(task, Mapping):
            blockers.append(
                ReadCanaryBlocker(
                    code="tasks_contract_invalid",
                    message="canonical tasks array contains a non-object entry",
                    phase="probe",
                    endpoint="/tasks",
                    surface="task_list",
                    details={"index": index},
                )
            )
            continue
        public_task_id = task.get("public_task_id")
        if not isinstance(public_task_id, str) or not public_task_id:
            blockers.append(
                ReadCanaryBlocker(
                    code="public_task_id_invalid",
                    message="canonical task entry lacks a non-empty public_task_id",
                    phase="probe",
                    endpoint="/tasks",
                    surface="task_list",
                    details={"index": index},
                )
            )
            continue
        if len(public_task_id) > 512:
            blockers.append(
                ReadCanaryBlocker(
                    code="public_task_id_invalid",
                    message="canonical public_task_id exceeds the canary path limit",
                    phase="probe",
                    endpoint="/tasks",
                    surface="task_list",
                    details={"index": index, "length": len(public_task_id)},
                )
            )
            continue
        return public_task_id
    return None


def _normalized_lane(
    lane: Mapping[str, Any],
    *,
    phase: str,
    surface: str,
    blockers: list[ReadCanaryBlocker],
) -> dict[str, int] | None:
    normalized: dict[str, int] = {}
    for semantic_name, aliases in _COUNTER_ALIASES.items():
        wire_name = next((name for name in aliases if name in lane), None)
        value = lane.get(wire_name) if wire_name is not None else None
        if (
            wire_name is None
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            blockers.append(
                ReadCanaryBlocker(
                    code="counter_invalid",
                    message="surface counter is missing or not a non-negative integer",
                    phase=phase,
                    endpoint="/health",
                    surface=surface,
                    details={"counter": semantic_name, "value": _bounded(value)},
                )
            )
            return None
        normalized[semantic_name] = value
    return normalized


def _counter_deltas(
    before_surfaces: Mapping[str, Any] | None,
    after_surfaces: Mapping[str, Any] | None,
    *,
    attempted_surfaces: tuple[str, ...],
    blockers: list[ReadCanaryBlocker],
) -> tuple[ReadCanaryCounterDelta, ...]:
    if before_surfaces is None or after_surfaces is None:
        return ()
    results: list[ReadCanaryCounterDelta] = []
    for surface in attempted_surfaces:
        before_raw = before_surfaces.get(surface)
        after_raw = after_surfaces.get(surface)
        if before_raw is None:
            before = {name: 0 for name in _COUNTER_ALIASES}
        elif isinstance(before_raw, Mapping):
            before = _normalized_lane(
                before_raw,
                phase="initial_health",
                surface=surface,
                blockers=blockers,
            )
        else:
            before = None
            blockers.append(
                ReadCanaryBlocker(
                    code="surface_counter_lane_invalid",
                    message="initial health surface counter lane is not an object",
                    phase="initial_health",
                    endpoint="/health",
                    surface=surface,
                )
            )
        if not isinstance(after_raw, Mapping):
            after = None
            blockers.append(
                ReadCanaryBlocker(
                    code="surface_counter_lane_missing",
                    message="final health lacks the attempted surface counter lane",
                    phase="final_health",
                    endpoint="/health",
                    surface=surface,
                )
            )
        else:
            after = _normalized_lane(
                after_raw,
                phase="final_health",
                surface=surface,
                blockers=blockers,
            )
        if before is None or after is None:
            continue
        delta = {name: after[name] - before[name] for name in _COUNTER_ALIASES}
        local_blockers = 0
        regressions = {name: value for name, value in delta.items() if value < 0}
        if regressions:
            local_blockers += 1
            blockers.append(
                ReadCanaryBlocker(
                    code="counter_regressed",
                    message="surface counters decreased between health snapshots",
                    phase="counter_verification",
                    endpoint="/health",
                    surface=surface,
                    details={"deltas": regressions},
                )
            )
        if delta["total_reads"] < 1:
            local_blockers += 1
            blockers.append(
                ReadCanaryBlocker(
                    code="total_read_not_counted",
                    message="attempted surface did not advance total_reads",
                    phase="counter_verification",
                    endpoint="/health",
                    surface=surface,
                    details={"delta": delta["total_reads"], "expected_minimum": 1},
                )
            )
        if delta["canonical_reads"] < 1:
            local_blockers += 1
            blockers.append(
                ReadCanaryBlocker(
                    code="canonical_read_not_counted",
                    message="attempted surface did not advance canonical_reads",
                    phase="counter_verification",
                    endpoint="/health",
                    surface=surface,
                    details={"delta": delta["canonical_reads"], "expected_minimum": 1},
                )
            )
        safety_increases = {
            name: delta[name]
            for name in ("fallback_reads", "errors", "unavailable")
            if delta[name] > 0
        }
        if safety_increases:
            local_blockers += 1
            blockers.append(
                ReadCanaryBlocker(
                    code="unsafe_counter_increase",
                    message="fallback, error, or unavailable counters advanced",
                    phase="counter_verification",
                    endpoint="/health",
                    surface=surface,
                    details={"deltas": safety_increases},
                )
            )
        results.append(
            ReadCanaryCounterDelta(
                surface=surface,
                expected_minimum_reads=1,
                before=before,
                after=after,
                delta=delta,
                passed=local_blockers == 0,
            )
        )
    return tuple(results)


def verify_read_canary(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    transport: ReadCanaryTransport | None = None,
) -> ReadCanaryResult:
    """Probe the canonical JSON cutover lane and return typed evidence.

    Operational failures never escape: HTTP, JSON, label, and counter failures
    become structured blockers.  Configuration is loopback-only so an
    operator cannot accidentally turn this local readiness check into a
    remote network probe.
    """

    blockers: list[ReadCanaryBlocker] = []
    health_observations: list[ReadCanaryHealthObservation] = []
    probes: list[ReadCanaryProbe] = []
    target = _target(host, port) if isinstance(host, str) else "invalid-target"
    configuration_error = _configuration_blocker(
        host=host,
        port=port,
        timeout_seconds=timeout_seconds,
    )
    if configuration_error is not None:
        blockers.append(configuration_error)
        return ReadCanaryResult(
            ready=False,
            target=target,
            timeout_seconds=(
                float(timeout_seconds)
                if isinstance(timeout_seconds, (int, float))
                and not isinstance(timeout_seconds, bool)
                else 0.0
            ),
            health=(),
            probes=(),
            probed_surfaces=(),
            skipped_surfaces=tuple(
                ReadCanarySkippedSurface(surface=surface, reason="invalid_configuration")
                for surface in ("usage_days", "task_list", "session_list", "task_detail")
            ),
            counter_deltas=(),
            blockers=tuple(blockers),
            task_detail_probed=False,
            task_detail_skipped_reason="invalid_configuration",
        )

    effective_timeout = float(timeout_seconds)
    effective_transport = transport or StdlibReadCanaryTransport(host, port)

    initial = _fetch_json(
        effective_transport,
        "/health",
        timeout_seconds=effective_timeout,
        phase="initial_health",
        surface=None,
        blockers=blockers,
    )
    initial_state, initial_observation = _health_state(
        initial,
        phase="initial_health",
        expected_store_uuid=None,
        blockers=blockers,
    )
    health_observations.append(initial_observation)

    task_payload: Mapping[str, Any] | None = None
    task_list_passed = False
    for endpoint, surface in _BASE_PROBES:
        fetched = _fetch_json(
            effective_transport,
            endpoint,
            timeout_seconds=effective_timeout,
            phase="probe",
            surface=surface,
            blockers=blockers,
        )
        probe = _probe_contract(
            fetched,
            endpoint=endpoint,
            surface=surface,
            blockers=blockers,
        )
        probes.append(probe)
        if surface == "task_list":
            task_payload = fetched.payload
            task_list_passed = probe.passed

    task_contract_blocker_count = len(blockers)
    public_task_id = _task_id(task_payload, blockers=blockers) if task_list_passed else None
    task_list_contract_invalid = len(blockers) > task_contract_blocker_count
    if task_list_contract_invalid:
        probes = [
            replace(probe, passed=False) if probe.surface == "task_list" else probe
            for probe in probes
        ]
    task_detail_probed = public_task_id is not None
    if task_detail_probed:
        task_detail_skipped_reason = None
    elif not task_list_passed:
        task_detail_skipped_reason = "task_list_probe_failed"
    elif task_list_contract_invalid:
        task_detail_skipped_reason = "task_list_contract_invalid"
    else:
        task_detail_skipped_reason = "no_public_task_id"
    if public_task_id is not None:
        endpoint = "/api/tasks/" + quote(public_task_id, safe="")
        fetched = _fetch_json(
            effective_transport,
            endpoint,
            timeout_seconds=effective_timeout,
            phase="probe",
            surface="task_detail",
            blockers=blockers,
        )
        probes.append(
            _probe_contract(
                fetched,
                endpoint=endpoint,
                surface="task_detail",
                blockers=blockers,
            )
        )

    final = _fetch_json(
        effective_transport,
        "/health",
        timeout_seconds=effective_timeout,
        phase="final_health",
        surface=None,
        blockers=blockers,
    )
    final_state, final_observation = _health_state(
        final,
        phase="final_health",
        expected_store_uuid=initial_state.store_uuid,
        blockers=blockers,
    )
    health_observations.append(final_observation)

    attempted_surfaces = tuple(probe.surface for probe in probes)
    counter_deltas = _counter_deltas(
        initial_state.surfaces,
        final_state.surfaces,
        attempted_surfaces=attempted_surfaces,
        blockers=blockers,
    )
    return ReadCanaryResult(
        ready=not blockers,
        target=target,
        timeout_seconds=effective_timeout,
        health=tuple(health_observations),
        probes=tuple(probes),
        probed_surfaces=attempted_surfaces,
        skipped_surfaces=(
            ()
            if task_detail_probed
            else (
                ReadCanarySkippedSurface(
                    surface="task_detail",
                    reason=task_detail_skipped_reason,
                ),
            )
        ),
        counter_deltas=counter_deltas,
        blockers=tuple(blockers),
        task_detail_probed=task_detail_probed,
        task_detail_skipped_reason=task_detail_skipped_reason,
    )


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_TIMEOUT_SECONDS",
    "READ_CANARY_SCHEMA_VERSION",
    "ReadCanaryBlocker",
    "ReadCanaryCounterDelta",
    "ReadCanaryHealthObservation",
    "ReadCanaryHttpResponse",
    "ReadCanaryProbe",
    "ReadCanaryResult",
    "ReadCanarySkippedSurface",
    "ReadCanaryTransport",
    "StdlibReadCanaryTransport",
    "verify_read_canary",
]
