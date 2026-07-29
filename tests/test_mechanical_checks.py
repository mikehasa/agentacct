from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from agentacct.api import _latest_machine_check_events, _task_product_state
from agentacct.mechanical_checks import build_mechanical_check_events
from agentacct.task_intelligence import build_task_intelligence


def _check_envelope(
    *,
    evidence_id: str = "ev-check-1",
    session_id: str = "session-1",
    event_timestamp: str = "2026-07-16T08:00:00Z",
    check_kind: str = "test",
    runner: str = "pytest",
    command_digest: str | None = None,
    exit_code: int = 0,
    passed: bool = True,
) -> dict[str, Any]:
    return {
        "schema_version": "agent-chronicle.evidence-envelope.v2",
        "evidence_id": evidence_id,
        "assertion": "observed",
        "source_type": "client_hook",
        "source_system": "codex",
        "source_event_id": f"source-{evidence_id}",
        "event_type": "machine_check_observed",
        "event_timestamp": event_timestamp,
        "observed_at": event_timestamp,
        "dimensions": ["activity", "machine_check"],
        "measurement_basis": {"machine_check": "hook_exit_code"},
        "tags": ["mechanical_capture"],
        "subjects": {"client_session_id": session_id},
        "payload": {
            "capture_basis": "host_hook",
            "host_event": "PostToolUse",
            "attributes": {
                "check_kind": check_kind,
                "runner": runner,
                "command_digest": command_digest or f"sha256:{'a' * 64}",
                "exit_code": exit_code,
                "passed": passed,
            },
        },
    }


@pytest.mark.parametrize(
    ("exit_code", "passed", "expected_result"),
    [(0, True, "passed"), (17, False, "failed")],
)
def test_valid_hook_exit_metadata_translates_to_task_check(
    exit_code: int,
    passed: bool,
    expected_result: str,
) -> None:
    envelope = _check_envelope(exit_code=exit_code, passed=passed)

    events = build_mechanical_check_events([envelope])

    assert len(events) == 1
    event = events[0]
    assert event["event_id"] == "evidence:ev-check-1"
    assert event["client"] == "codex"
    assert event["client_session_id"] == "session-1"
    assert event["evidence_type"] == "test"
    assert event["result"] == expected_result
    assert event["exit_code"] == exit_code
    assert event["measurement_basis"] == "hook_exit_code"


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("check_kind", "deploy"),
        ("runner", "pytest -q"),
        ("command_digest", "not-a-digest"),
    ],
)
def test_invalid_machine_metadata_is_not_translated(field: str, invalid_value: str) -> None:
    envelope = _check_envelope()
    envelope["payload"]["attributes"][field] = invalid_value

    assert build_mechanical_check_events([envelope]) == []


def test_machine_check_requires_consistent_exit_code_and_passed_flag() -> None:
    false_success = _check_envelope(exit_code=0, passed=False)
    false_failure = _check_envelope(exit_code=1, passed=True)

    assert build_mechanical_check_events([false_success, false_failure]) == []


def test_check_identity_is_stable_for_the_same_mechanical_check() -> None:
    first = _check_envelope(
        evidence_id="ev-first",
        session_id="session-a",
        event_timestamp="2026-07-16T08:00:00Z",
    )
    retried_elsewhere = _check_envelope(
        evidence_id="ev-second",
        session_id="session-b",
        event_timestamp="2026-07-16T09:00:00Z",
    )
    different_command = _check_envelope(
        evidence_id="ev-third",
        command_digest=f"sha256:{'b' * 64}",
    )

    first_identity = build_mechanical_check_events([first])[0]["check_identity"]
    retry_identity = build_mechanical_check_events([retried_elsewhere])[0]["check_identity"]
    different_identity = build_mechanical_check_events([different_command])[0]["check_identity"]

    assert first_identity.startswith("client-hook:")
    assert first_identity == retry_identity
    assert first_identity != different_identity


def test_mechanical_check_projection_never_copies_command_or_output() -> None:
    canary = "CANARY_CHECK_COMMAND_MUST_NOT_LEAK"
    envelope = _check_envelope()
    envelope["command"] = canary
    envelope["payload"]["command"] = canary
    envelope["payload"]["prompt"] = canary
    envelope["payload"]["response"] = canary
    envelope["payload"]["attributes"].update(
        {"command": canary, "stdout": canary, "stderr": canary}
    )

    events = build_mechanical_check_events([deepcopy(envelope)])
    rendered = json.dumps(events, sort_keys=True)

    assert len(events) == 1
    assert events[0]["command"] is None
    assert events[0]["command_redacted"] is True
    assert canary not in rendered


@pytest.mark.parametrize(
    ("newer_exit_code", "newer_passed", "expected_result", "expected_state"),
    [
        (0, True, "passed", "verified"),
        (2, False, "failed", "open_finding"),
    ],
)
def test_arrival_sequence_breaks_same_timestamp_rerun_ties_consistently(
    newer_exit_code: int,
    newer_passed: bool,
    expected_result: str,
    expected_state: str,
) -> None:
    older_exit_code = 2 if newer_exit_code == 0 else 0
    older_passed = older_exit_code == 0
    older = SimpleNamespace(
        envelope=_check_envelope(
            evidence_id="ev-older",
            event_timestamp="2026-07-16T08:00:00Z",
            exit_code=older_exit_code,
            passed=older_passed,
        ),
        first_receipt_sequence=10,
    )
    newer = SimpleNamespace(
        envelope=_check_envelope(
            evidence_id="ev-newer",
            event_timestamp="2026-07-16T08:00:00Z",
            exit_code=newer_exit_code,
            passed=newer_passed,
        ),
        first_receipt_sequence=11,
    )
    events = build_mechanical_check_events([newer, older])
    latest = _latest_machine_check_events({"evidence_events": events})

    assert len(latest) == 1
    assert latest[0][0]["result"] == expected_result

    task = {
        "work_items": [{"latest_status": "completed", "evidence_events": []}],
        "task_evidence_events": events,
        "sessions": [],
        "usage": {"rows": 0},
    }
    home_state, _carrier = _task_product_state(task)
    detail = build_task_intelligence(task, public_task_id="task_test", title="Test task")
    expected_detail = "verified" if expected_state == "verified" else "finding"
    assert home_state["key"] == expected_state
    assert detail["states"]["outcome"]["key"] == expected_detail
