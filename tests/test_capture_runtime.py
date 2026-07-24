from __future__ import annotations

import json

from agent_chronicle.capture import CaptureContext
from agent_chronicle.capture_runtime import capture_hook_payload
from agent_chronicle.evidence_runtime import EvidenceRuntime


def _payload() -> dict[str, object]:
    return {
        "hook_event_name": "PostToolUse",
        "timestamp": "2026-07-13T01:02:03Z",
        "session_id": "claude-session-001",
        "turn_id": "claude-turn-001",
        "tool_name": "Bash",
        "tool_use_id": "tool-use-001",
        "tool_input": {"command": "pytest -q", "authorization": "SECRET_CANARY"},
        "tool_response": {"exit_code": 0, "stdout": "RESULT_CANARY"},
        "prompt": "PROMPT_CANARY",
    }


def test_capture_runtime_writes_metadata_only_and_deduplicates(tmp_path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    context = CaptureContext(
        host_event="PostToolUse",
        workspace_roots=(str(tmp_path),),
    )

    first = capture_hook_payload(
        runtime,
        vendor="claude-code",
        host_event="PostToolUse",
        payload=_payload(),
        context=context,
    )
    second = capture_hook_payload(
        runtime,
        vendor="claude-code",
        host_event="PostToolUse",
        payload=_payload(),
        context=context,
    )

    assert first["ok"] is True
    assert first["stored_count"] == 2
    assert second["stored_count"] == 2
    assert runtime.status()["stats"]["logical_events"] == 2
    assert runtime.status()["stats"]["duplicate_receipts"] == 2
    encoded = json.dumps(first) + json.dumps([item.to_dict() for item in runtime.envelopes()])
    for canary in ("SECRET_CANARY", "RESULT_CANARY", "PROMPT_CANARY"):
        assert canary not in encoded


def test_capture_runtime_disabled_is_noop_without_store(tmp_path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=False)

    result = capture_hook_payload(
        runtime,
        vendor="codex",
        host_event="Stop",
        payload={"session_id": "s1"},
    )

    assert result["ok"] is True
    assert result["ignored_reason"] == "evidence_v2_disabled"
    assert result["fail_open"] is True
    assert not (tmp_path / "evidence-v2").exists()


def test_capture_runtime_malformed_input_is_structured_and_fail_open(tmp_path) -> None:
    result = capture_hook_payload(
        EvidenceRuntime(tmp_path, enabled=True),
        vendor="cursor",
        host_event="sessionStart",
        payload=b"not-json SECRET_CANARY",
    )

    assert result["ok"] is False
    assert result["ignored_reason"] == "malformed_json"
    assert result["fail_open"] is True
    assert "SECRET_CANARY" not in json.dumps(result)
