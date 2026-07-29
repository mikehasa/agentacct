from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from agentacct.canonical.read_canary import (
    READ_CANARY_SCHEMA_VERSION,
    ReadCanaryHttpResponse,
    verify_read_canary,
)
from agentacct.canonical.sqlite import SCHEMA_VERSION


_TASK_ID = "task_0123456789abcdef0123456789abcdef"
_STORE_UUID = "store-uuid-a"


class _ScriptedTransport:
    def __init__(
        self,
        steps: list[tuple[str, ReadCanaryHttpResponse | Exception]],
    ) -> None:
        self.steps = list(steps)
        self.calls: list[tuple[str, float]] = []

    def get(self, endpoint: str, *, timeout_seconds: float) -> ReadCanaryHttpResponse:
        self.calls.append((endpoint, timeout_seconds))
        assert self.steps, f"unexpected request: {endpoint}"
        expected_endpoint, outcome = self.steps.pop(0)
        assert endpoint == expected_endpoint
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _response(payload: Mapping[str, Any], *, status: int = 200) -> ReadCanaryHttpResponse:
    return ReadCanaryHttpResponse(
        status_code=status,
        body=json.dumps(payload, sort_keys=True).encode("utf-8"),
    )


def _canonical_payload(**extra: Any) -> dict[str, Any]:
    return {
        "canonical_read": {"active": True, "source": "canonical"},
        **extra,
    }


def _fallback_payload(**extra: Any) -> dict[str, Any]:
    return {
        "canonical_read": {
            "active": False,
            "source": "v1_fallback",
            "reason": "store_absent",
        },
        **extra,
    }


def _lane(
    *,
    attempts: int = 0,
    served: int = 0,
    unavailable: int = 0,
    errors: int = 0,
) -> dict[str, int]:
    return {
        "attempts": attempts,
        "served": served,
        "unavailable": unavailable,
        "errors": errors,
    }


def _normalized_lane(
    *,
    total_reads: int = 0,
    canonical_reads: int = 0,
    fallback_reads: int = 0,
    unavailable: int = 0,
    errors: int = 0,
) -> dict[str, int]:
    return {
        "total_reads": total_reads,
        "canonical_reads": canonical_reads,
        "fallback_reads": fallback_reads,
        "unavailable": unavailable,
        "errors": errors,
    }


def _canonical_store(
    *,
    store_uuid: str = _STORE_UUID,
    schema_version: int = SCHEMA_VERSION,
) -> dict[str, Any]:
    return {
        "available": True,
        "reason": None,
        "detail": None,
        "store": {
            "store_uuid": store_uuid,
            "store_role": "live",
            "schema_version": schema_version,
            "canonical_sequence": 10,
        },
        "projections": {
            projection_name: {
                "projection_name": projection_name,
                "projection_version": 1,
                "built_through_sequence": 10,
                "state": "current",
                "stale": False,
                "pending_writes": 0,
            }
            for projection_name in ("rm_task_current", "rm_usage_day")
        },
        "sources": [],
    }


def _health(
    *,
    enabled: bool = True,
    surfaces: Mapping[str, Mapping[str, int]] | None = None,
    canonical_store: Mapping[str, Any] | None = None,
    include_canonical_store: bool = True,
) -> dict[str, Any]:
    payload = {
        "ok": True,
        "service": "agent-sentinel-local-api",
        "canonical_read": {
            "enabled": enabled,
            "surfaces": dict(surfaces or {}),
        },
    }
    if include_canonical_store:
        payload["canonical_store"] = dict(canonical_store or _canonical_store())
    return payload


def _successful_steps(*, include_task: bool) -> list[tuple[str, ReadCanaryHttpResponse]]:
    surfaces = ("usage_days", "task_list", "session_list")
    final_surfaces = {surface: _lane(attempts=1, served=1) for surface in surfaces}
    tasks = [{"public_task_id": _TASK_ID}] if include_task else []
    steps: list[tuple[str, ReadCanaryHttpResponse]] = [
        ("/health", _response(_health())),
        ("/usage/summary?days=all", _response(_canonical_payload(totals={}))),
        ("/tasks", _response(_canonical_payload(tasks=tasks))),
        ("/sessions", _response(_canonical_payload(sessions=[]))),
    ]
    if include_task:
        steps.append(
            (
                f"/api/tasks/{_TASK_ID}",
                _response(_canonical_payload(task={"public_task_id": _TASK_ID})),
            )
        )
        final_surfaces["task_detail"] = _lane(attempts=1, served=1)
    steps.append(("/health", _response(_health(surfaces=final_surfaces))))
    return steps


def _blocker_codes(result: Any) -> set[str]:
    return {blocker.code for blocker in result.blockers}


def test_success_probes_all_json_surfaces_and_normalizes_legacy_counters():
    transport = _ScriptedTransport(_successful_steps(include_task=True))

    result = verify_read_canary(transport=transport)

    assert result.ready is True
    assert result.blockers == ()
    assert result.probed_surfaces == (
        "usage_days",
        "task_list",
        "session_list",
        "task_detail",
    )
    assert result.skipped_surfaces == ()
    assert result.task_detail_probed is True
    assert result.task_detail_skipped_reason is None
    assert all(probe.passed for probe in result.probes)
    assert all(delta.passed for delta in result.counter_deltas)
    assert all(delta.delta["total_reads"] == 1 for delta in result.counter_deltas)
    assert all(delta.delta["canonical_reads"] == 1 for delta in result.counter_deltas)
    assert all(delta.delta["fallback_reads"] == 0 for delta in result.counter_deltas)
    assert [endpoint for endpoint, _timeout in transport.calls] == [
        "/health",
        "/usage/summary?days=all",
        "/tasks",
        "/sessions",
        f"/api/tasks/{_TASK_ID}",
        "/health",
    ]
    wire = result.to_dict()
    assert wire["schema_version"] == READ_CANARY_SCHEMA_VERSION
    assert wire["ready"] is True
    assert wire["probed_surfaces"][-1] == "task_detail"
    assert wire["skipped_surfaces"] == []
    assert [health["canonical_store_uuid"] for health in wire["health"]] == [
        _STORE_UUID,
        _STORE_UUID,
    ]
    assert all(health["canonical_store_ready"] for health in wire["health"])
    assert all(
        health["canonical_store_schema_version"] == SCHEMA_VERSION
        for health in wire["health"]
    )


@pytest.mark.parametrize(
    ("health_index", "expected_phase"),
    [(0, "initial_health"), (-1, "final_health")],
)
def test_missing_canonical_store_blocks_either_health_snapshot(
    health_index: int,
    expected_phase: str,
):
    steps = _successful_steps(include_task=False)
    endpoint, response = steps[health_index]
    payload = json.loads(response.body)
    payload.pop("canonical_store")
    steps[health_index] = (endpoint, _response(payload))

    result = verify_read_canary(transport=_ScriptedTransport(steps))

    assert result.ready is False
    blocker = next(
        item for item in result.blockers if item.code == "canonical_store_missing"
    )
    assert blocker.phase == expected_phase
    health = next(item for item in result.health if item.phase == expected_phase)
    assert health.canonical_store_ready is False


def test_missing_required_projection_blocks_readiness():
    steps = _successful_steps(include_task=False)
    endpoint, response = steps[-1]
    payload = json.loads(response.body)
    payload["canonical_store"]["projections"].pop("rm_usage_day")
    steps[-1] = (endpoint, _response(payload))

    result = verify_read_canary(transport=_ScriptedTransport(steps))

    assert result.ready is False
    blocker = next(
        item for item in result.blockers if item.code == "canonical_projection_missing"
    )
    assert blocker.phase == "final_health"
    assert blocker.surface == "rm_usage_day"


def test_unavailable_canonical_store_blocks_readiness():
    steps = _successful_steps(include_task=False)
    endpoint, response = steps[0]
    payload = json.loads(response.body)
    payload["canonical_store"] = {
        "available": False,
        "reason": "store_absent",
        "detail": None,
    }
    steps[0] = (endpoint, _response(payload))

    result = verify_read_canary(transport=_ScriptedTransport(steps))

    assert result.ready is False
    blocker = next(
        item for item in result.blockers if item.code == "canonical_store_unavailable"
    )
    assert blocker.phase == "initial_health"
    assert blocker.details["reason"] == "store_absent"


def test_non_live_canonical_store_role_blocks_readiness():
    steps = _successful_steps(include_task=False)
    endpoint, response = steps[-1]
    payload = json.loads(response.body)
    payload["canonical_store"]["store"]["store_role"] = "candidate"
    steps[-1] = (endpoint, _response(payload))

    result = verify_read_canary(transport=_ScriptedTransport(steps))

    assert result.ready is False
    blocker = next(
        item for item in result.blockers if item.code == "canonical_store_role_invalid"
    )
    assert blocker.phase == "final_health"
    assert blocker.details["store_role"] == "candidate"


def test_stale_or_pending_projection_blocks_readiness():
    steps = _successful_steps(include_task=False)
    endpoint, response = steps[-1]
    payload = json.loads(response.body)
    projection = payload["canonical_store"]["projections"]["rm_usage_day"]
    projection.update({"stale": True, "pending_writes": 1})
    steps[-1] = (endpoint, _response(payload))

    result = verify_read_canary(transport=_ScriptedTransport(steps))

    assert result.ready is False
    blocker = next(
        item for item in result.blockers if item.code == "canonical_projection_not_ready"
    )
    assert blocker.phase == "final_health"
    assert blocker.surface == "rm_usage_day"
    assert blocker.details["stale"] is True
    assert blocker.details["pending_writes"] == 1


def test_outdated_canonical_store_schema_blocks_readiness():
    steps = _successful_steps(include_task=False)
    endpoint, response = steps[0]
    payload = json.loads(response.body)
    payload["canonical_store"]["store"]["schema_version"] = SCHEMA_VERSION - 1
    steps[0] = (endpoint, _response(payload))

    result = verify_read_canary(transport=_ScriptedTransport(steps))

    assert result.ready is False
    blocker = next(
        item
        for item in result.blockers
        if item.code == "canonical_store_schema_mismatch"
    )
    assert blocker.phase == "initial_health"
    assert blocker.details["expected_schema_version"] == SCHEMA_VERSION


def test_canonical_store_uuid_change_between_snapshots_blocks_readiness():
    steps = _successful_steps(include_task=False)
    endpoint, response = steps[-1]
    payload = json.loads(response.body)
    payload["canonical_store"]["store"]["store_uuid"] = "store-uuid-b"
    steps[-1] = (endpoint, _response(payload))

    result = verify_read_canary(transport=_ScriptedTransport(steps))

    assert result.ready is False
    blocker = next(
        item for item in result.blockers if item.code == "canonical_store_uuid_changed"
    )
    assert blocker.phase == "final_health"
    assert blocker.details == {
        "initial_store_uuid": _STORE_UUID,
        "final_store_uuid": "store-uuid-b",
    }


def test_no_task_is_ready_but_task_detail_is_explicitly_skipped():
    transport = _ScriptedTransport(_successful_steps(include_task=False))

    result = verify_read_canary(transport=transport)

    assert result.ready is True
    assert result.probed_surfaces == ("usage_days", "task_list", "session_list")
    assert result.task_detail_probed is False
    assert result.task_detail_skipped_reason == "no_public_task_id"
    assert [item.to_dict() for item in result.skipped_surfaces] == [
        {"surface": "task_detail", "reason": "no_public_task_id"}
    ]
    assert len(result.counter_deltas) == 3


def test_flag_off_and_unlabeled_v1_payloads_are_blockers():
    transport = _ScriptedTransport(
        [
            ("/health", _response(_health(enabled=False))),
            ("/usage/summary?days=all", _response({"totals": {}})),
            ("/tasks", _response({"tasks": []})),
            ("/sessions", _response({"sessions": []})),
            ("/health", _response(_health(enabled=False))),
        ]
    )

    result = verify_read_canary(transport=transport)

    assert result.ready is False
    assert "canonical_read_disabled" in _blocker_codes(result)
    assert "canonical_label_missing" in _blocker_codes(result)
    assert result.task_detail_skipped_reason == "task_list_probe_failed"
    assert result.skipped_surfaces[0].surface == "task_detail"


def test_labeled_v1_fallback_and_counter_increase_are_blockers():
    fallback_surfaces = {
        surface: _lane(attempts=1, unavailable=1)
        for surface in ("usage_days", "task_list", "session_list")
    }
    transport = _ScriptedTransport(
        [
            ("/health", _response(_health())),
            ("/usage/summary?days=all", _response(_fallback_payload(totals={}))),
            ("/tasks", _response(_fallback_payload(tasks=[]))),
            ("/sessions", _response(_fallback_payload(sessions=[]))),
            ("/health", _response(_health(surfaces=fallback_surfaces))),
        ]
    )

    result = verify_read_canary(transport=transport)

    assert result.ready is False
    assert "canonical_read_inactive" in _blocker_codes(result)
    assert "canonical_source_mismatch" in _blocker_codes(result)
    assert "unsafe_counter_increase" in _blocker_codes(result)
    assert all(not delta.passed for delta in result.counter_deltas)
    assert all(delta.delta["fallback_reads"] == 1 for delta in result.counter_deltas)


def test_bad_json_is_a_structured_blocker():
    final_surfaces = {
        surface: _lane(attempts=1, served=1)
        for surface in ("usage_days", "task_list", "session_list")
    }
    transport = _ScriptedTransport(
        [
            ("/health", _response(_health())),
            ("/usage/summary?days=all", ReadCanaryHttpResponse(200, b"{")),
            ("/tasks", _response(_canonical_payload(tasks=[]))),
            ("/sessions", _response(_canonical_payload(sessions=[]))),
            ("/health", _response(_health(surfaces=final_surfaces))),
        ]
    )

    result = verify_read_canary(transport=transport)

    assert result.ready is False
    invalid = next(blocker for blocker in result.blockers if blocker.code == "invalid_json")
    assert invalid.endpoint == "/usage/summary?days=all"
    assert invalid.surface == "usage_days"
    assert next(probe for probe in result.probes if probe.surface == "usage_days").passed is False


def test_http_transport_failure_is_a_structured_blocker():
    final_surfaces = {
        "usage_days": _lane(attempts=1, served=1),
        "task_list": _lane(attempts=1, served=1),
    }
    transport = _ScriptedTransport(
        [
            ("/health", _response(_health())),
            ("/usage/summary?days=all", _response(_canonical_payload(totals={}))),
            ("/tasks", _response(_canonical_payload(tasks=[]))),
            ("/sessions", OSError("connection reset")),
            ("/health", _response(_health(surfaces=final_surfaces))),
        ]
    )

    result = verify_read_canary(transport=transport)

    assert result.ready is False
    failure = next(blocker for blocker in result.blockers if blocker.code == "http_error")
    assert failure.endpoint == "/sessions"
    assert failure.surface == "session_list"
    assert "OSError" in failure.details["error"]
    assert "surface_counter_lane_missing" in _blocker_codes(result)


def test_non_200_status_is_distinct_from_transport_failure():
    final_surfaces = {
        "usage_days": _lane(attempts=1, served=1),
        "task_list": _lane(attempts=1, served=1),
    }
    transport = _ScriptedTransport(
        [
            ("/health", _response(_health())),
            ("/usage/summary?days=all", _response(_canonical_payload(totals={}))),
            ("/tasks", _response(_canonical_payload(tasks=[]))),
            ("/sessions", ReadCanaryHttpResponse(503, b'{"detail":"down"}')),
            ("/health", _response(_health(surfaces=final_surfaces))),
        ]
    )

    result = verify_read_canary(transport=transport)

    blocker = next(item for item in result.blockers if item.code == "http_status")
    assert blocker.details["status_code"] == 503
    assert "http_error" not in _blocker_codes(result)


def test_counter_drift_blocks_even_when_payload_labels_are_canonical():
    final_surfaces = {
        "usage_days": _lane(attempts=1, served=1),
        "task_list": _lane(attempts=1, served=1, unavailable=1),
        "session_list": _lane(attempts=1, served=1),
    }
    transport = _ScriptedTransport(
        [
            ("/health", _response(_health())),
            ("/usage/summary?days=all", _response(_canonical_payload(totals={}))),
            ("/tasks", _response(_canonical_payload(tasks=[]))),
            ("/sessions", _response(_canonical_payload(sessions=[]))),
            ("/health", _response(_health(surfaces=final_surfaces))),
        ]
    )

    result = verify_read_canary(transport=transport)

    assert result.ready is False
    blocker = next(
        item
        for item in result.blockers
        if item.code == "unsafe_counter_increase" and item.surface == "task_list"
    )
    assert blocker.details["deltas"] == {"fallback_reads": 1, "unavailable": 1}
    assert all(probe.passed for probe in result.probes)


def test_normalized_phase5_counter_names_are_accepted_directly():
    final_surfaces = {
        surface: _normalized_lane(total_reads=1, canonical_reads=1)
        for surface in ("usage_days", "task_list", "session_list")
    }
    transport = _ScriptedTransport(
        [
            ("/health", _response(_health())),
            ("/usage/summary?days=all", _response(_canonical_payload(totals={}))),
            ("/tasks", _response(_canonical_payload(tasks=[]))),
            ("/sessions", _response(_canonical_payload(sessions=[]))),
            ("/health", _response(_health(surfaces=final_surfaces))),
        ]
    )

    result = verify_read_canary(transport=transport)

    assert result.ready is True
    assert all(delta.delta["canonical_reads"] == 1 for delta in result.counter_deltas)


def test_invalid_target_returns_blockers_without_calling_transport():
    transport = _ScriptedTransport([])

    result = verify_read_canary(host="example.com", transport=transport)

    assert result.ready is False
    assert _blocker_codes(result) == {"invalid_target"}
    assert result.probed_surfaces == ()
    assert {item.surface for item in result.skipped_surfaces} == {
        "usage_days",
        "task_list",
        "session_list",
        "task_detail",
    }
    assert transport.calls == []
