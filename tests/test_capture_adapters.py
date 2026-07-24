from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from agent_chronicle.capture import (
    CaptureContext,
    CaptureService,
    build_default_registry,
    merge_cursor_hooks,
    merge_hook_manifest,
    render_hook_manifest,
)
from agent_chronicle.capture.base import safe_relative_path


FIXTURES = Path(__file__).parent / "fixtures" / "capture" / "v1"


def _fixture(name: str) -> dict:
    value = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert value["schema_version"] == "capture-fixture-v1"
    return value


def _context(value: dict) -> CaptureContext:
    context = value.get("context") or {}
    observed = context.get("observed_at")
    return CaptureContext(
        observed_at=datetime.fromisoformat(observed.replace("Z", "+00:00")) if observed else None,
        environment=context.get("environment") or {},
        workspace_roots=tuple(context.get("workspace_roots") or ()),
    )


@pytest.mark.parametrize(
    ("fixture_name", "expected_types"),
    [
        ("claude_code_post_tool_use.json", {"tool_observed", "machine_check_observed"}),
        ("codex_stop.json", {"turn_observed", "session_link_observed"}),
        ("cursor_after_shell_execution.json", {"tool_observed", "machine_check_observed"}),
        ("cursor_subagent_start.json", {"session_link_observed"}),
    ],
)
def test_versioned_fixtures_normalize_to_expected_canonical_events(
    fixture_name: str,
    expected_types: set[str],
) -> None:
    fixture = _fixture(fixture_name)
    registry = build_default_registry()

    result = registry.normalize(
        fixture["vendor"],
        fixture["payload"],
        context=_context(fixture),
        host_event=fixture["event"],
    )

    assert not result.ignored
    assert {item.event_type for item in result.observations} == expected_types
    assert all(item.source_event_id.startswith(f"capture:{fixture['vendor']}:") for item in result.observations)
    assert all(item.raw_digest.startswith("sha256:") for item in result.observations)


@pytest.mark.parametrize(
    "fixture_name",
    [
        "claude_code_post_tool_use.json",
        "codex_stop.json",
        "cursor_after_shell_execution.json",
        "cursor_subagent_start.json",
    ],
)
def test_metadata_only_projection_never_persists_fixture_canaries(fixture_name: str) -> None:
    fixture = _fixture(fixture_name)
    result = build_default_registry().normalize(
        fixture["vendor"],
        fixture["payload"],
        context=_context(fixture),
        host_event=fixture["event"],
    )

    rendered = json.dumps([item.to_dict() for item in result.observations], sort_keys=True)
    for canary in (
        "CANARY_CLAUDE_PROMPT",
        "CANARY_CLAUDE_RESPONSE",
        "CANARY_CLAUDE_THOUGHT",
        "CANARY_CLAUDE_SECRET",
        "CANARY_CLAUDE_TOOL_RESULT",
        "CANARY_CLAUDE_STDERR",
        "CANARY_CODEX_PROMPT",
        "CANARY_CODEX_RESPONSE",
        "CANARY_CODEX_THOUGHT",
        "CANARY_CODEX_TOOL_RESULT",
        "CANARY_CURSOR_PROMPT",
        "CANARY_CURSOR_RESPONSE",
        "CANARY_CURSOR_THOUGHT",
        "CANARY_CURSOR_SECRET",
        "CANARY_CURSOR_TOOL_RESULT",
        "CANARY_CURSOR_SUBAGENT_TASK",
        "CANARY_CURSOR_SUBAGENT_SUMMARY",
    ):
        assert canary not in rendered


def test_machine_check_requires_recognized_command_and_objective_exit_status() -> None:
    registry = build_default_registry()
    base = {
        "hook_event_name": "PostToolUse",
        "timestamp": "2026-07-13T00:00:00Z",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "tool_name": "Bash",
        "tool_use_id": "tool-1",
        "tool_input": {"command": "pytest -q"},
        "tool_response": {"stdout": "tests passed"},
    }

    no_exit = registry.normalize("claude-code", base)
    assert [item.event_type for item in no_exit.observations] == ["tool_observed"]

    input_claim_is_not_result = registry.normalize(
        "claude-code",
        {**base, "tool_input": {"command": "pytest -q", "exit_code": 0}},
    )
    assert [item.event_type for item in input_claim_is_not_result.observations] == ["tool_observed"]

    stdout_json_is_not_result_metadata = registry.normalize(
        "codex",
        {
            **base,
            "tool_output": "{\"exit_code\": 0}",
        },
    )
    assert [item.event_type for item in stdout_json_is_not_result_metadata.observations] == ["tool_observed"]

    unrecognized = registry.normalize(
        "claude-code",
        {**base, "tool_response": {"exit_code": 0}, "tool_input": {"command": "echo tests passed"}},
    )
    assert [item.event_type for item in unrecognized.observations] == ["tool_observed"]

    compound = registry.normalize(
        "claude-code",
        {**base, "tool_response": {"exit_code": 0}, "tool_input": {"command": "pytest -q && deploy"}},
    )
    assert [item.event_type for item in compound.observations] == ["tool_observed"]

    objective = registry.normalize(
        "claude-code",
        {**base, "tool_response": {"exit_code": 1, "stdout": "secret"}},
    )
    check = next(item for item in objective.observations if item.event_type == "machine_check_observed")
    assert check.attributes["check_kind"] == "test"
    assert check.attributes["exit_code"] == 1
    assert check.attributes["passed"] is False
    assert check.attributes["runner"] == "pytest"
    assert check.attributes["command_digest"].startswith("sha256:")


def test_machine_categories_cover_test_build_lint_and_typecheck() -> None:
    registry = build_default_registry()
    commands = {
        "pytest": "test",
        "npm run build": "build",
        "ruff check .": "lint",
        "cargo check": "typecheck",
    }
    for index, (command, expected) in enumerate(commands.items()):
        result = registry.normalize(
            "codex",
            {
                "hook_event_name": "PostToolUse",
                "timestamp": f"2026-07-13T00:00:0{index}Z",
                "session_id": "session-1",
                "turn_id": f"turn-{index}",
                "tool_name": "shell",
                "tool_use_id": f"tool-{index}",
                "tool_input": {"command": command},
                "tool_response": {"exit_code": 0},
            },
        )
        check = next(item for item in result.observations if item.event_type == "machine_check_observed")
        assert check.attributes["check_kind"] == expected


def test_source_ids_are_deterministic_and_missing_timestamp_is_explicitly_partial() -> None:
    fixture = _fixture("cursor_subagent_start.json")
    registry = build_default_registry()
    first_context = _context(fixture)
    second_context = CaptureContext(
        observed_at=datetime.fromisoformat("2026-07-14T04:05:06+00:00"),
        environment=first_context.environment,
        workspace_roots=first_context.workspace_roots,
    )
    first = registry.normalize(
        fixture["vendor"], fixture["payload"], context=first_context, host_event=fixture["event"]
    )
    second = registry.normalize(
        fixture["vendor"], fixture["payload"], context=second_context, host_event=fixture["event"]
    )

    assert [item.source_event_id for item in first.observations] == [
        item.source_event_id for item in second.observations
    ]
    assert first.warnings == ("host_timestamp_missing",)
    assert first.observations[0].completeness.status == "partial"
    assert "host_timestamp" in first.observations[0].completeness.missing_fields
    assert first.observations[0].observed_at != second.observations[0].observed_at
    assert first.observations[0].attributes["identity_basis"] == "payload_fingerprint"


def test_cursor_prefers_conversation_identity_and_only_keeps_allowed_paths() -> None:
    fixture = _fixture("cursor_subagent_start.json")
    result = build_default_registry().normalize(
        fixture["vendor"], fixture["payload"], context=_context(fixture), host_event=fixture["event"]
    )
    observation = result.observations[0]

    assert observation.session_id == "cursor-parent-conversation-001"
    assert observation.parent_session_id == "cursor-parent-conversation-001"
    assert observation.linked_session_id == "cursor-subagent-001"
    assert observation.attributes["files"] == ["src/safe.py"]
    serialized = json.dumps(observation.to_dict())
    assert "/workspace/repo" not in serialized
    assert "/outside" not in serialized
    assert "../escape" not in serialized


def test_safe_relative_path_handles_unix_and_windows_roots_without_leaking_absolute_paths() -> None:
    assert safe_relative_path("/repo/src/a.py", roots=("/repo",)) == "src/a.py"
    assert safe_relative_path("/outside/a.py", roots=("/repo",)) == ""
    assert safe_relative_path("/private/a.py", roots=("/",)) == ""
    assert safe_relative_path("C:\\repo\\src\\a.py", roots=("C:\\repo",)) == "src/a.py"
    assert safe_relative_path("C:\\private\\a.py", roots=("C:\\repo",)) == ""
    assert safe_relative_path("../private", roots=("/repo",)) == ""


def test_claude_adapter_rejects_cursor_process_masquerading_as_claude() -> None:
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "not-claude",
        "timestamp": "2026-07-13T00:00:00Z",
    }
    result = build_default_registry().normalize(
        "claude-code",
        payload,
        context=CaptureContext(environment={"CURSOR_TRACE_ID": "cursor-1"}),
    )
    assert result.ignored_reason == "host_mismatch_cursor_environment"

    real_claude = build_default_registry().normalize(
        "claude-code",
        payload,
        context=CaptureContext(environment={"CURSOR_TRACE_ID": "cursor-1", "CLAUDECODE": "1"}),
    )
    assert not real_claude.ignored


def test_capability_registry_is_explicit_and_bounded() -> None:
    registry = build_default_registry(max_payload_bytes=128)
    assert registry.vendors() == ("claude-code", "codex", "cursor")
    assert registry.get("cc").vendor == "claude-code"
    assert registry.get("openai_codex").vendor == "codex"

    capabilities = registry.capabilities("cursor")
    assert capabilities.session_lifecycle is True
    assert capabilities.subagents is True
    assert capabilities.usage is False
    assert capabilities.cost is False
    assert capabilities.machine_results is True
    assert capabilities.stable_event_identity == "derived"

    assert registry.normalize("unknown", {}).ignored_reason == "unsupported_vendor"
    assert registry.normalize("cursor", "not-json").ignored_reason == "malformed_json"
    assert registry.normalize("cursor", '{"exit_code":NaN}').ignored_reason == "malformed_json"
    assert registry.normalize("cursor", {"text": "x" * 500}).ignored_reason == "payload_too_large"


def test_manifest_rendering_is_deterministic_and_cursor_merge_is_idempotent() -> None:
    for vendor in ("claude-code", "codex", "cursor"):
        first = render_hook_manifest(vendor)
        second = render_hook_manifest(vendor)
        assert first.to_json() == second.to_json()
        assert "agent-chronicle capture hook" in first.to_json()
        assert first.relative_path.startswith(".")

    existing = {
        "version": 1,
        "custom": {"preserve": True},
        "hooks": {
            "sessionStart": [{"command": "foreign-observer", "timeout": 3000}],
            "customEvent": [{"command": "foreign-custom", "timeout": 3000}],
        },
    }
    rendered = render_hook_manifest("cursor")
    once = merge_cursor_hooks(existing, rendered)
    twice = merge_cursor_hooks(once, rendered)

    assert once == twice
    assert once["custom"] == {"preserve": True}
    assert once["hooks"]["customEvent"] == [{"command": "foreign-custom", "timeout": 3000}]
    commands = [entry["command"] for entry in once["hooks"]["sessionStart"]]
    assert commands[0] == "foreign-observer"
    assert sum("agent-chronicle capture hook" in command for command in commands) == 1

    for vendor in ("claude-code", "codex"):
        nested_rendered = render_hook_manifest(vendor)
        nested_existing = {
            "preserve": True,
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "foreign-observer", "timeout": 2}]},
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"agent-chronicle capture hook --vendor {vendor} --event SessionStart",
                                "timeout": 5,
                            }
                        ]
                    },
                ]
            },
        }
        nested_once = merge_hook_manifest(nested_existing, nested_rendered)
        nested_twice = merge_hook_manifest(nested_once, nested_rendered)
        assert nested_once == nested_twice
        assert nested_once["preserve"] is True
        serialized = json.dumps(nested_once["hooks"]["SessionStart"])
        assert serialized.count("foreign-observer") == 1
        assert serialized.count("agent-chronicle capture hook") == 1


class _MemorySink:
    def __init__(self) -> None:
        self.items = []

    def append_many(self, envelopes) -> None:
        self.items.extend(envelopes)


class _PartialDurableSink:
    def __init__(self) -> None:
        self.items = []

    def append(self, envelope) -> None:
        if self.items:
            raise OSError("CANARY_SECOND_WRITE_SECRET")
        self.items.append(envelope)


class _BrokenSink:
    def append_many(self, envelopes) -> None:
        raise OSError("CANARY_SINK_SECRET")


def test_capture_service_writes_through_narrow_sink_and_builds_evidence_envelopes() -> None:
    from agent_chronicle.source_policy import Authority, EvidenceDimension, default_source_authority_policy

    fixture = _fixture("claude_code_post_tool_use.json")
    sink = _MemorySink()
    service = CaptureService(sink)

    result = service.capture(
        fixture["vendor"],
        fixture["payload"],
        context=_context(fixture),
        host_event=fixture["event"],
    )

    assert result.ok
    assert result.attempted_count == 2
    assert result.stored_count == 2
    assert len(sink.items) == 2
    assert {item.event_type for item in sink.items} == {"tool_observed", "machine_check_observed"}
    assert all(item.source_type == "client_hook" for item in sink.items)
    assert all(item.privacy.content_capture == "metadata_only" for item in sink.items)
    machine = next(item for item in sink.items if item.event_type == "machine_check_observed")
    assert machine.measurement_basis["machine_check"] == "hook_exit_code"
    assert (
        default_source_authority_policy().evaluate(machine, EvidenceDimension.MACHINE_CHECK).authority
        is Authority.AUTHORITATIVE
    )
    serialized = json.dumps([item.to_dict() for item in sink.items])
    assert "CANARY_CLAUDE_PROMPT" not in serialized
    assert "CANARY_CLAUDE_TOOL_RESULT" not in serialized


def test_capture_service_is_fail_open_and_does_not_leak_sink_errors() -> None:
    fixture = _fixture("cursor_after_shell_execution.json")
    service = CaptureService(_BrokenSink(), envelope_factory=lambda item: item.to_dict())

    result = service.capture(
        fixture["vendor"],
        fixture["payload"],
        context=_context(fixture),
        host_event=fixture["event"],
    )

    assert result.fail_open is True
    assert result.stored_count == 0
    assert result.write_errors == ("sink_error:OSError",)
    assert "CANARY_SINK_SECRET" not in json.dumps(result.write_errors)


def test_capture_service_reports_a_durable_prefix_when_a_later_append_fails() -> None:
    fixture = _fixture("claude_code_post_tool_use.json")
    sink = _PartialDurableSink()
    service = CaptureService(sink, envelope_factory=lambda item: item.to_dict())

    result = service.capture(
        fixture["vendor"],
        fixture["payload"],
        context=_context(fixture),
        host_event=fixture["event"],
    )

    assert result.attempted_count == 2
    assert result.stored_count == 1
    assert len(sink.items) == 1
    assert result.write_errors == ("sink_error:OSError",)
    assert "CANARY_SECOND_WRITE_SECRET" not in json.dumps(result.write_errors)


def test_capture_service_appends_to_real_v2_spool_idempotently(tmp_path: Path) -> None:
    from agent_chronicle.evidence_store import EvidenceStore

    fixture = _fixture("cursor_after_shell_execution.json")
    store = EvidenceStore(tmp_path)
    service = CaptureService(store)

    for _ in range(2):
        result = service.capture(
            fixture["vendor"],
            fixture["payload"],
            context=_context(fixture),
            host_event=fixture["event"],
        )
        assert result.stored_count == 2
        assert result.write_errors == ()

    stats = store.stats()
    assert stats.logical_events == 2
    assert stats.evidence_versions == 2
    assert stats.receipts == 4
    assert stats.duplicate_receipts == 2


def test_unknown_fields_and_events_are_denied_by_default() -> None:
    registry = build_default_registry()
    unknown = registry.normalize(
        "cursor",
        {
            "hook_event_name": "futureSecretEvent",
            "conversation_id": "cursor-1",
            "future_body": "CANARY_FUTURE_BODY",
        },
    )
    assert unknown.ignored_reason == "unsupported_host_event"
    assert unknown.observations == ()
