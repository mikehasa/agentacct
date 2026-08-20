import io
import json

from typer.testing import CliRunner

from agentacct.cli import app
from agentacct.mcp import SentinelMCPServer, read_mcp_message, run_mcp_event_workflow_smoke, serve_stdio, write_mcp_message
from agentacct.runner import RunOptions, start_guarded_run
from agentacct.storage import json_utf8_size
from agentacct.work_ledger import build_work_ledger


def _make_run(tmp_path):
    dummy = tmp_path / "mcp_dummy.py"
    dummy.write_text("print('mcp ok')\n", encoding="utf-8")
    store_root = tmp_path / "state"
    result = start_guarded_run(["python", str(dummy)], RunOptions(store_dir=store_root, poll_interval=0.05))
    return store_root, result


def _tool_payload(response):
    text = response["result"]["content"][0]["text"]
    return json.loads(text)


def test_mcp_initialize_and_tools_list(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    initialized = server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}}}
    )
    assert initialized["result"]["serverInfo"]["name"] == "agentacct"
    assert initialized["result"]["capabilities"] == {"tools": {}}
    assert initialized["result"]["protocolVersion"] == "2025-06-18"

    tools = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    names = {tool["name"] for tool in tools["result"]["tools"]}
    assert "agentacct_list_runs" in names
    assert "agentacct_get_report" in names
    assert "agentacct_record_machine_check" in names
    assert "agentacct_record_event" in names
    assert "agentacct_attach_client_context" in names
    assert "agentacct_record_section" in names
    assert "agentacct_list_events" in names
    assert "agentacct_get_event_summary" in names
    assert "sentinel_run_judge" not in names


def test_mcp_initialize_returns_directive_instructions(tmp_path):
    """Phase 2.9: the initialize result must carry an `instructions` string that
    the client shows to the agent AT THE TOOL LAYER — the one place a background
    CLAUDE.md instruction cannot reliably reach when the tools are deferred.

    The field is the MCP protocol's InitializeResult.instructions; it must be
    non-empty, directive, tool-aware, and honest, while the pre-existing keys
    (protocolVersion/serverInfo/capabilities) are unchanged.
    """
    from agentacct.install_guide import MCP_SERVER_INSTRUCTIONS

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    result = server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}}}
    )["result"]

    instructions = result["instructions"]
    assert isinstance(instructions, str) and instructions.strip()
    # Single source of truth: the exact string comes from install_guide.
    assert instructions == MCP_SERVER_INSTRUCTIONS

    # Directive to record work via sections + machine-check evidence.
    assert "agentacct_record_section" in instructions
    assert "agentacct_record_machine_check" in instructions
    # THE deferral-beating line: load the tools first if they are not available.
    assert "not directly available" in instructions
    assert "load them first" in instructions
    assert "deferred" in instructions
    # Honesty guardrail: token/cost figures are separate and never fabricated.
    assert "never fabricate" in instructions
    assert "token/cost" in instructions
    # SECTIONS-ONLY contract: no task lifecycle events.
    assert "task_started" not in instructions
    assert "task_completed" not in instructions

    # Pre-existing fields are unchanged by adding instructions.
    assert result["serverInfo"] == {"name": "agentacct", "version": "0.1.0"}
    assert result["capabilities"] == {"tools": {}}
    assert result["protocolVersion"] == "2025-06-18"


def test_workflow_smoke_initialize_carries_instructions(tmp_path):
    """The release-gate workflow smoke drives a real initialize; its returned
    initialize result must include the same directive instructions field."""
    from agentacct.install_guide import MCP_SERVER_INSTRUCTIONS

    result = run_mcp_event_workflow_smoke(store_dir=tmp_path / "state")
    assert result["ok"] is True
    assert result["initialize"]["instructions"] == MCP_SERVER_INSTRUCTIONS
    # protocolVersion still negotiated from the default when unspecified.
    assert result["initialize"]["protocolVersion"] == "2024-11-05"


def test_mcp_tools_call_list_runs_record_machine_check_and_get_report(tmp_path):
    store_root, result = _make_run(tmp_path)
    server = SentinelMCPServer(store_dir=store_root)

    runs_response = server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "agentacct_list_runs", "arguments": {"limit": 5}}}
    )
    assert _tool_payload(runs_response)["runs"][0]["run_id"] == result.run_id

    check_response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "agentacct_record_machine_check",
                "arguments": {
                    "run_id": result.run_id,
                    "name": "pytest",
                    "before_exit_code": 1,
                    "after_exit_code": 0,
                    "before_summary": "failed before",
                    "after_summary": "passed after",
                },
            },
        }
    )
    assert _tool_payload(check_response)["outcome"]["machine_checks"]["resolved_failures"] == 1

    report_response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "agentacct_get_report", "arguments": {"run_id": result.run_id}},
        }
    )
    assert _tool_payload(report_response)["outcome"]["machine_checks"]["resolved_failures"] == 1


def test_mcp_records_and_lists_redacted_events(tmp_path):
    store_root, result = _make_run(tmp_path)
    server = SentinelMCPServer(store_dir=store_root)

    record_response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "agentacct_record_event",
                "arguments": {
                    "source": "hermes",
                    "event_type": "model_usage",
                    "run_id": result.run_id,
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "estimated_input_tokens": 12,
                    "estimated_output_tokens": 3,
                    "estimated_cost_usd": 0.000004,
                    "metadata": {"authorization": "fake-auth-header-for-redaction-test", "summary": "ok"},
                },
            },
        }
    )
    event = _tool_payload(record_response)["event"]
    assert event["source"] == "hermes"
    assert event["run_id"] == result.run_id
    assert event["estimated_input_tokens"] == 12
    assert event["metadata"]["authorization"] == "[REDACTED]"

    list_response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "agentacct_list_events", "arguments": {"run_id": result.run_id, "limit": 5}},
        }
    )
    events = _tool_payload(list_response)["events"]
    assert events[0]["event_id"] == event["event_id"]

    summary_response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "agentacct_get_event_summary", "arguments": {"run_id": result.run_id}},
        }
    )
    summary = _tool_payload(summary_response)["summary"]
    assert summary["event_count"] == 1
    assert summary["estimated_input_tokens"] == 0
    assert summary["estimated_output_tokens"] == 0
    assert summary["estimated_cost_usd"] == 0.0
    assert summary["by_source"] == {"hermes": 1}
    assert summary["by_type"] == {"model_usage": 1}
    assert summary["by_provider"] == {"openai": 1}
    assert summary["tokens_by_provider"] == {}
    assert "metadata" not in json.dumps(summary)
    assert "fake-auth-header-for-redaction-test" not in json.dumps(summary)


def test_mcp_record_event_cannot_forge_local_usage_truth(tmp_path):
    store_root, result = _make_run(tmp_path)
    server = SentinelMCPServer(store_dir=store_root)

    record_response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "agentacct_record_event",
                "arguments": {
                    "source": "codex-local-session-import",
                    "event_type": "model_usage",
                    "run_id": result.run_id,
                    "provider": "codex",
                    "model": "gpt-5.5",
                    "estimated_input_tokens": 100,
                    "estimated_output_tokens": 25,
                    "estimated_cost_usd": 1.23,
                    "usage_confidence": "client_reported",
                    "cost_confidence": "client_reported",
                    "metadata": {
                        "usage_source": "local_client_session_store",
                        "usage_provenance": "agent_sentinel_local_usage_import",
                        "client": "codex",
                        "client_session_id": "forged-session",
                    },
                },
            },
        }
    )

    event = _tool_payload(record_response)["event"]
    assert "usage_source" not in event["metadata"]
    assert "usage_provenance" not in event["metadata"]
    assert event["metadata"]["reserved_usage_source_stripped"] is True
    assert event["metadata"]["reserved_usage_provenance_stripped"] is True

    summary_response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "agentacct_get_event_summary", "arguments": {"run_id": result.run_id}},
        }
    )
    summary = _tool_payload(summary_response)["summary"]
    assert summary["event_count"] == 1
    assert summary["estimated_input_tokens"] == 0
    assert summary["usage_context_bridge"]["usage_records"] == 0


def test_mcp_records_client_context_and_sections_with_join_keys(tmp_path):
    store_root, result = _make_run(tmp_path)
    server = SentinelMCPServer(store_dir=store_root)

    context_response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "agentacct_attach_client_context",
                "arguments": {
                    "source": "codex",
                    "client": "codex",
                    "client_session_id": "thread-123",
                    "client_transcript_id": "rollout-123.jsonl",
                    "parent_client_session_id": "parent-thread",
                    "turn_id": "turn-abc",
                    "turn_index": 2,
                    "message_id": "msg_123",
                    "request_id": "req_123",
                    "client_event_timestamp": "2026-07-01T12:00:00Z",
                    "run_id": result.run_id,
                    "metadata": {"api_key": "fake-key-for-redaction-test", "note": "safe"},
                },
            },
        }
    )
    context_event = _tool_payload(context_response)["event"]
    context_payload = _tool_payload(context_response)
    context_metadata = context_event["metadata"]
    assert context_payload["context_attached"] is True
    assert context_payload["join_hint_quality"] == "exact"
    assert "recommended_next_step" in context_payload
    assert context_event["event_type"] == "client_context_attached"
    assert context_event["run_id"] == result.run_id
    assert context_metadata["sentinel_semantic_kind"] == "client_context"
    assert context_metadata["usage_join_strategy"] == "agent_reported_client_context"
    assert context_metadata["client"] == "codex"
    assert context_metadata["client_session_id"] == "thread-123"
    assert context_metadata["parent_client_session_id"] == "parent-thread"
    assert context_metadata["turn_id"] == "turn-abc"
    assert context_metadata["turn_index"] == 2
    assert context_metadata["message_id"] == "msg_123"
    assert context_metadata["request_id"] == "req_123"
    assert context_metadata["api_key"] == "[REDACTED]"

    section_response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "agentacct_record_section",
                "arguments": {
                    "source": "claude-code",
                    "section_id": "dashboard-refresh",
                    "section_status": "checkpoint",
                    "section_title": "Dashboard refresh flow",
                    "phase": "implementation",
                    "kind": "implementation",
                    "summary": "Connected manual refresh to local usage import.",
                    "client": "claude-code",
                    "client_session_id": "claude-session",
                    "project_dir": "/tmp/project",
                    "request_id": "req_456",
                    "message_id": "msg_456",
                    "client_event_timestamp": "2026-07-01T12:10:00Z",
                    "files": ["src/agentacct/local_api.py"],
                    "next_step": "Run dashboard smoke.",
                    "run_id": result.run_id,
                },
            },
        }
    )
    section_event = _tool_payload(section_response)["event"]
    section_metadata = section_event["metadata"]
    assert section_event["event_type"] == "section_checkpoint"
    assert section_metadata["sentinel_semantic_kind"] == "section"
    assert section_metadata["usage_join_strategy"] == "agent_reported_section_context"
    assert section_metadata["section_id"] == "dashboard-refresh"
    assert section_metadata["section_status"] == "checkpoint"
    assert section_metadata["section_title"] == "Dashboard refresh flow"
    assert section_metadata["client_session_id"] == "claude-session"
    assert section_metadata["project_dir"] == "/tmp/project"
    assert section_metadata["kind"] == "implementation"
    assert section_metadata["files"] == ["src/agentacct/local_api.py"]
    assert section_metadata["next_step"] == "Run dashboard smoke."

    list_response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "agentacct_list_events", "arguments": {"run_id": result.run_id, "limit": 5}},
        }
    )
    event_types = {event["event_type"] for event in _tool_payload(list_response)["events"]}
    assert {"client_context_attached", "section_checkpoint"} <= event_types


def test_mcp_records_agent_usage_debug_without_counting_usage_totals(tmp_path):
    store_root, result = _make_run(tmp_path)
    server = SentinelMCPServer(store_dir=store_root)

    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "agentacct_record_agent_usage_debug",
                "arguments": {
                    "source": "codex",
                    "client": "codex",
                    "client_session_id": "codex-session-123",
                    "client_transcript_id": "rollout-123",
                    "turn_id": "turn-2",
                    "turn_index": 2,
                    "message_id": "msg-2",
                    "request_id": "req-2",
                    "provider": "openai",
                    "model": "gpt-5.5",
                    "reporting_basis": "visible_client_usage",
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 50,
                    "reasoning_output_tokens": 5,
                    "total_tokens": 175,
                    "cost_usd": 0.0123,
                    "cost_basis": "client_visible_session_estimate",
                    "summary": "Codex reported visible usage after message 2.",
                    "run_id": result.run_id,
                    "metadata": {"api_key": "fake-key-for-redaction-test", "note": "safe"},
                },
            },
        }
    )

    event = _tool_payload(response)["event"]
    metadata = event["metadata"]
    assert event["event_type"] == "agent_usage_debug_reported"
    assert event["provider"] == "openai"
    assert event["model"] == "gpt-5.5"
    assert event["estimated_input_tokens"] is None
    assert event["estimated_output_tokens"] is None
    assert event["estimated_cost_usd"] is None
    assert metadata["sentinel_semantic_kind"] == "agent_usage_debug"
    assert metadata["usage_join_strategy"] == "agent_reported_usage_debug"
    assert metadata["agent_reported_input_tokens"] == 100
    assert metadata["agent_reported_output_tokens"] == 20
    assert metadata["agent_reported_cache_read_input_tokens"] == 50
    assert metadata["agent_reported_reasoning_output_tokens"] == 5
    assert metadata["agent_reported_total_tokens"] == 175
    assert metadata["agent_reported_cost_usd"] == 0.0123
    assert metadata["agent_reported_cost_currency"] == "USD"
    assert metadata["api_key"] == "[REDACTED]"

    summary_response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "agentacct_get_event_summary", "arguments": {"run_id": result.run_id, "limit": 20}},
        }
    )
    summary = _tool_payload(summary_response)["summary"]
    assert summary["estimated_input_tokens"] == 0
    assert summary["estimated_output_tokens"] == 0
    assert summary["estimated_cost_usd"] == 0.0
    assert summary["by_type"] == {"agent_usage_debug_reported": 1}
    assert summary["by_provider"] == {"openai": 1}


def test_mcp_record_section_is_work_item_compatible_and_idempotent(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    args = {
        "source": "codex",
        "section_id": "mcp-v1",
        "section_status": "checkpoint",
        "section_title": "MCP v1 convergence",
        "kind": "implementation",
        "summary": "Converged section events into WorkEvent-compatible fields.",
        "client": "codex",
        "client_session_id": "codex-session",
        "project_dir": "/tmp/project",
        "files": ["src/agentacct/mcp.py"],
        "next_step": "Run pytest.",
        "idempotency_key": "section-mcp-v1-checkpoint",
    }

    first = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "agentacct_record_section", "arguments": args}})
    second = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "agentacct_record_section", "arguments": args}})

    first_event = _tool_payload(first)["event"]
    second_event = _tool_payload(second)["event"]
    assert first_event["event_id"] == second_event["event_id"]
    events = server.service.list_all_events()
    assert len(events) == 1
    ledger = build_work_ledger(events)
    assert ledger["work_items"][0]["work_id"] == "codex::codex-session::mcp-v1"
    assert ledger["work_items"][0]["section_id"] == "mcp-v1"
    assert ledger["work_items"][0]["latest_status"] == "checkpoint"
    assert ledger["work_items"][0]["kind"] == "implementation"
    assert ledger["work_items"][0]["files"] == ["src/agentacct/mcp.py"]
    assert ledger["work_items"][0]["next_step"] == "Run pytest."


def test_mcp_record_section_accepts_handed_off_and_reduces_to_clean_terminal(tmp_path):
    # DECISION 1 end-to-end: the choice-set validator accepts handed_off, the
    # ledger preserves it (not coerced to checkpoint), and the Task reduces to
    # the clean-stop terminal — never in_progress/verified.
    from agentacct.task_outcome import reduce_task_outcome

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "agentacct_record_section",
                "arguments": {
                    "source": "codex",
                    "section_id": "handoff-step",
                    "section_status": "handed_off",
                    "section_title": "Continue in a new session",
                    "client": "codex",
                    "client_session_id": "codex-session",
                },
            },
        }
    )

    assert "error" not in response
    event = _tool_payload(response)["event"]
    assert event["event_type"] == "section_handed_off"
    assert event["metadata"]["section_status"] == "handed_off"

    item = build_work_ledger(server.service.list_all_events())["work_items"][0]
    assert item["latest_status"] == "handed_off"

    task = {"work_items": [item], "task_evidence_events": [], "sessions": [], "usage": {"rows": 0}}
    outcome = reduce_task_outcome(task)
    assert outcome["key"] == "handed_off"
    assert outcome["key"] not in {"in_progress", "verified", "reported"}


def test_mcp_record_machine_check_creates_evidence_event_linked_to_section(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    section = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "agentacct_record_section",
                "arguments": {
                    "source": "codex",
                    "section_id": "mcp-v1",
                    "section_status": "completed",
                    "section_title": "MCP v1 convergence",
                    "client": "codex",
                    "client_session_id": "codex-session",
                },
            },
        }
    )
    evidence_args = {
        "source": "codex",
        "section_id": "mcp-v1",
        "evidence_type": "test",
        "result": "passed",
        "summary": "Targeted MCP tests passed.",
        "command": "pytest tests/test_mcp.py",
        "exit_code": 0,
        "files": ["tests/test_mcp.py"],
        "idempotency_key": "evidence-mcp-v1-tests",
    }
    evidence = server.handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "agentacct_record_machine_check", "arguments": evidence_args}}
    )
    duplicate_evidence = server.handle_message(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "agentacct_record_machine_check", "arguments": evidence_args}}
    )

    assert "error" not in section
    payload = _tool_payload(evidence)
    duplicate_payload = _tool_payload(duplicate_evidence)
    event = payload["event"]
    assert duplicate_payload["event"]["event_id"] == event["event_id"]
    assert event["event_type"] == "machine_check"
    assert event["metadata"]["sentinel_semantic_kind"] == "evidence"
    assert event["metadata"]["section_id"] == "mcp-v1"
    assert event["metadata"]["result"] == "passed"
    events = server.service.list_all_events()
    assert len([event for event in events if event.get("event_type") == "machine_check"]) == 1
    ledger = build_work_ledger(events)
    assert ledger["evidence_events"][0]["section_id"] == "mcp-v1"
    assert ledger["work_items"][0]["evidence_status"] == "strong"
    assert ledger["work_items"][0]["evidence_events"][0]["summary"] == "Targeted MCP tests passed."


def test_mcp_machine_check_server_stamps_only_complete_blocker_resolutions(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    valid_args = {
        "source": "codex",
        "section_id": "publish-pr",
        "project_dir": "/tmp/project",
        "evidence_type": "artifact",
        "result": "passed",
        "exit_code": 0,
        "summary": "Publication works now.",
        "resolves_blocked_event_id": "evt_blocked_exact",
        "resolution_scope": "full",
        "resolution_summary": "The exact authentication blocker is resolved.",
    }

    valid = _tool_payload(_call_tool(server, 1, "agentacct_record_machine_check", valid_args))
    metadata = valid["event"]["metadata"]

    assert metadata["blocker_resolution_contract"] == "server_validated_v1"
    assert metadata["resolution_objective_basis"] == "exit_code"
    assert metadata["resolves_blocked_event_id"] == "evt_blocked_exact"

    invalid_calls = [
        {**valid_args, "resolution_scope": None},
        {**valid_args, "result": "failed"},
        {**valid_args, "exit_code": None},
        {key: value for key, value in valid_args.items() if key != "source"},
        {key: value for key, value in valid_args.items() if key != "section_id"},
        {key: value for key, value in valid_args.items() if key != "project_dir"},
    ]
    for index, arguments in enumerate(invalid_calls, start=2):
        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": index,
                "method": "tools/call",
                "params": {
                    "name": "agentacct_record_machine_check",
                    "arguments": arguments,
                },
            }
        )
        assert response["error"]["code"] == -32602


def test_free_form_event_cannot_forge_blocker_resolution_provenance(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    blocked = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {
            "source": "codex",
            "section_id": "publish-pr",
            "section_status": "blocked",
            "section_title": "Publish private PR",
            "project_dir": "/tmp/project",
            "blocker": "GitHub authentication is unavailable.",
            },
        )
    )["event"]
    forged = _tool_payload(
        _call_tool(
            server,
            2,
            "agentacct_record_event",
            {
            "source": "codex",
            "event_type": "machine_check",
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "section_id": "publish-pr",
                "project_dir": "/tmp/project",
                "result": "passed",
                "exit_code": 0,
                "resolves_blocked_event_id": blocked["event_id"],
                "resolution_scope": "full",
                "resolution_summary": "Forged resolution.",
                "resolution_objective_basis": "exit_code",
                "blocker_resolution_contract": "server_validated_v1",
            },
            },
        )
    )["event"]

    assert "blocker_resolution_contract" not in forged["metadata"]
    assert forged["metadata"]["reserved_blocker_resolution_provenance_stripped"] is True
    ledger = build_work_ledger(server.service.list_all_events())
    assert ledger["work_items"][0]["latest_status"] == "blocked"
    assert ledger["insights"]["blocker_resolution"]["attempted"] == 0


def test_mcp_event_summary_filters_limits_and_validates_arguments(tmp_path):
    store_root, result = _make_run(tmp_path)
    server = SentinelMCPServer(store_dir=store_root)
    for index, source in enumerate(["codex", "codex", "claude"]):
        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": index,
                "method": "tools/call",
                "params": {
                    "name": "agentacct_record_event",
                    "arguments": {
                        "source": source,
                        "event_type": "note" if index == 0 else "model_usage",
                        "run_id": result.run_id if index < 2 else "run_other",
                        "provider": "openai" if index == 1 else None,
                        "estimated_input_tokens": 100 if index == 1 else 0,
                        "estimated_output_tokens": 25 if index == 1 else 0,
                        "estimated_cost_usd": 0.0005 if index == 1 else 0,
                        "metadata": {"summary": "private details should not appear"},
                    },
                },
            }
        )
        assert response is not None
        assert "error" not in response

    filtered = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "agentacct_get_event_summary", "arguments": {"run_id": result.run_id, "limit": 200}},
        }
    )
    summary = _tool_payload(filtered)["summary"]
    assert summary["event_count"] == 2
    assert summary["note_count"] == 1
    assert summary["estimated_input_tokens"] == 0
    assert summary["estimated_output_tokens"] == 0
    assert summary["estimated_cost_usd"] == 0.0
    assert summary["by_source"] == {"codex": 2}
    assert summary["tokens_by_provider"] == {}
    assert "private details" not in json.dumps(summary)

    limited = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {"name": "agentacct_get_event_summary", "arguments": {"limit": 1}},
        }
    )
    assert _tool_payload(limited)["summary"]["event_count"] == 1

    bad_limit = server.handle_message(
        {"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": {"name": "agentacct_get_event_summary", "arguments": {"limit": 0}}}
    )
    bad_run_id = server.handle_message(
        {"jsonrpc": "2.0", "id": 13, "method": "tools/call", "params": {"name": "agentacct_get_event_summary", "arguments": {"run_id": "../bad"}}}
    )
    unknown = server.handle_message(
        {"jsonrpc": "2.0", "id": 14, "method": "tools/call", "params": {"name": "agentacct_get_event_summary", "arguments": {"unexpected": True}}}
    )
    bad_falsy_arguments = [
        server.handle_message({"jsonrpc": "2.0", "id": 20, "method": "tools/call", "params": {"name": "agentacct_get_event_summary", "arguments": value}})
        for value in ([], "", 0, False)
    ]
    bad_params = server.handle_message({"jsonrpc": "2.0", "id": 21, "method": "tools/call", "params": []})
    assert bad_limit is not None
    assert bad_run_id is not None
    assert unknown is not None
    assert bad_limit["error"]["code"] == -32602
    assert bad_run_id["error"]["code"] == -32602
    assert unknown["error"]["code"] == -32602
    assert bad_params is not None
    assert bad_params["error"]["code"] == -32602
    assert [response["error"]["code"] for response in bad_falsy_arguments if response is not None] == [-32602, -32602, -32602, -32602]


def test_mcp_event_tools_validate_arguments(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    bad_run = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "agentacct_record_event", "arguments": {"source": "x", "event_type": "y", "run_id": "../bad"}},
        }
    )
    assert bad_run["error"]["code"] == -32602

    unknown = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "agentacct_record_event", "arguments": {"source": "x", "event_type": "y", "extra_blob": "x"}},
        }
    )
    assert unknown["error"]["code"] == -32602

    huge_metadata = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "agentacct_record_event", "arguments": {"source": "x", "event_type": "y", "metadata": {"blob": "x" * 9000}}},
        }
    )
    assert huge_metadata["error"]["code"] == -32602

    bad_filter = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "agentacct_list_events", "arguments": {"run_id": "../bad"}},
        }
    )
    assert bad_filter["error"]["code"] == -32602

    missing_both = server.handle_message(
        {"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "agentacct_record_event", "arguments": {}}}
    )
    missing_event_type = server.handle_message(
        {"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "agentacct_record_event", "arguments": {"source": "x"}}}
    )
    missing_source = server.handle_message(
        {"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": {"name": "agentacct_record_event", "arguments": {"event_type": "y"}}}
    )
    assert missing_both["error"]["code"] == -32602
    assert missing_event_type["error"]["code"] == -32602
    assert missing_source["error"]["code"] == -32602

    nan_cost = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "tools/call",
            "params": {"name": "agentacct_record_event", "arguments": {"source": "x", "event_type": "y", "estimated_cost_usd": float("nan")}},
        }
    )
    nan_metadata = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 14,
            "method": "tools/call",
            "params": {"name": "agentacct_record_event", "arguments": {"source": "x", "event_type": "y", "metadata": {"bad": float("nan")}}},
        }
    )
    assert nan_cost["error"]["code"] == -32602
    assert nan_metadata["error"]["code"] == -32602


def test_mcp_semantic_tools_validate_arguments(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    missing_context_session = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "agentacct_attach_client_context", "arguments": {"source": "codex", "client": "codex"}},
        }
    )
    unknown_context_key = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "agentacct_attach_client_context",
                "arguments": {"source": "codex", "client": "codex", "client_session_id": "session", "unexpected": True},
            },
        }
    )
    bad_turn_index = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "agentacct_attach_client_context",
                "arguments": {"source": "codex", "client": "codex", "client_session_id": "session", "turn_index": -1},
            },
        }
    )
    bad_section_status = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "agentacct_record_section", "arguments": {"source": "codex", "section_id": "s1", "section_status": "done"}},
        }
    )
    too_many_files = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "agentacct_record_section",
                "arguments": {"source": "codex", "section_id": "s1", "section_status": "started", "files": [f"file-{index}" for index in range(51)]},
            },
        }
    )
    bad_usage_basis = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "agentacct_record_agent_usage_debug",
                "arguments": {"source": "codex", "client": "codex", "reporting_basis": "billing_truth"},
            },
        }
    )
    bad_usage_tokens = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "agentacct_record_agent_usage_debug",
                "arguments": {"source": "codex", "client": "codex", "reporting_basis": "visible_client_usage", "input_tokens": -1},
            },
        }
    )

    assert missing_context_session["error"]["code"] == -32602
    assert unknown_context_key["error"]["code"] == -32602
    assert bad_turn_index["error"]["code"] == -32602
    assert bad_section_status["error"]["code"] == -32602
    assert too_many_files["error"]["code"] == -32602
    assert bad_usage_basis["error"]["code"] == -32602
    assert bad_usage_tokens["error"]["code"] == -32602


def test_mcp_event_tool_schema_documents_limits(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    tools = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert tools is not None
    tool_by_name = {tool["name"]: tool for tool in tools["result"]["tools"]}
    record_tool = tool_by_name["agentacct_record_event"]
    context_tool = tool_by_name["agentacct_attach_client_context"]
    machine_tool = tool_by_name["agentacct_record_machine_check"]
    section_tool = tool_by_name["agentacct_record_section"]
    usage_debug_tool = tool_by_name["agentacct_record_agent_usage_debug"]
    summary_tool = tool_by_name["agentacct_get_event_summary"]
    props = record_tool["inputSchema"]["properties"]

    assert record_tool["inputSchema"]["required"] == ["source", "event_type"]
    assert props["source"]["maxLength"] == 80
    assert props["event_type"]["maxLength"] == 80
    assert props["run_id"]["maxLength"] == 128
    assert props["provider"]["maxLength"] == 80
    assert props["model"]["maxLength"] == 120
    assert context_tool["inputSchema"]["required"] == ["source", "client"]
    assert context_tool["inputSchema"]["properties"]["project_dir"]["maxLength"] == 1000
    assert context_tool["inputSchema"]["properties"]["idempotency_key"]["maxLength"] == 240
    assert context_tool["inputSchema"]["properties"]["turn_index"]["minimum"] == 0
    assert section_tool["inputSchema"]["required"] == ["source", "section_id", "section_status"]
    # handed_off is an additive terminal status (DECISION 1): a clean stop when
    # the user hands work off / continues in a new session.
    assert section_tool["inputSchema"]["properties"]["section_status"]["enum"] == ["started", "checkpoint", "completed", "blocked", "handed_off"]
    assert section_tool["inputSchema"]["properties"]["kind"]["enum"][:3] == ["planning", "implementation", "debugging"]
    assert section_tool["inputSchema"]["properties"]["files"]["maxItems"] == 50
    assert section_tool["inputSchema"]["properties"]["blocker"]["maxLength"] == 1200
    assert machine_tool["inputSchema"]["properties"]["evidence_type"]["enum"][:3] == ["test", "build", "lint"]
    assert machine_tool["inputSchema"]["properties"]["result"]["enum"][:3] == ["passed", "failed", "skipped"]
    assert machine_tool["inputSchema"]["properties"]["resolution_scope"]["enum"] == ["full", "partial", None]
    assert machine_tool["inputSchema"]["properties"]["resolves_blocked_event_id"]["maxLength"] == 240
    assert usage_debug_tool["inputSchema"]["required"] == ["source", "client", "reporting_basis"]
    assert usage_debug_tool["inputSchema"]["properties"]["reporting_basis"]["enum"] == [
        "visible_client_usage",
        "estimated_by_agent",
        "unavailable",
    ]
    assert usage_debug_tool["inputSchema"]["properties"]["cost_usd"]["minimum"] == 0
    assert summary_tool["inputSchema"]["properties"]["limit"]["default"] == 200
    assert summary_tool["inputSchema"]["properties"]["limit"]["maximum"] == 200
    assert summary_tool["inputSchema"]["additionalProperties"] is False


def test_mcp_workflow_smoke_records_lists_and_summarizes_event(tmp_path):
    payload = run_mcp_event_workflow_smoke(store_dir=tmp_path / "state", run_id="mcp_workflow_test")

    assert payload["ok"] is True
    assert payload["event_round_tripped"] is True
    assert payload["summary_ok"] is True
    assert payload["metadata_redacted"] is True
    assert payload["event"]["source"] == "agent-sentinel-mcp-workflow-smoke"
    assert payload["event"]["metadata"]["api_key"] == "[REDACTED]"
    assert payload["summary"]["event_count"] == 1
    assert payload["summary"]["by_source"] == {"agent-sentinel-mcp-workflow-smoke": 1}
    assert "agentacct_record_event" in payload["tool_names"]
    assert "fake-key-for-redaction-test" not in json.dumps(payload)


def test_mcp_workflow_smoke_cli_json(tmp_path):
    result = CliRunner().invoke(app, ["mcp", "workflow-smoke", "--store-dir", str(tmp_path / "state"), "--run-id", "mcp_cli_workflow", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["run_id"] == "mcp_cli_workflow"
    assert payload["event_round_tripped"] is True
    assert payload["summary_ok"] is True
    assert payload["metadata_redacted"] is True
    assert "fake-key-for-redaction-test" not in result.output


def test_mcp_workflow_smoke_rejects_bad_run_id(tmp_path):
    result = CliRunner().invoke(app, ["mcp", "workflow-smoke", "--store-dir", str(tmp_path / "state"), "--run-id", "../bad"])

    assert result.exit_code != 0
    assert "invalid_run_id" not in result.output
    assert "invalid run_id" in result.output


def test_mcp_workflow_smoke_is_documented() -> None:
    # The MCP tool/workflow reference moved from the README to docs/reference.md
    # in the value-first README rewrite.
    reference = open("docs/reference.md", encoding="utf-8").read()
    checklist = open("docs/public-alpha-checklist.md", encoding="utf-8").read()

    # The reference uses the public agentacct CLI; the maintainer checklist
    # still uses the transition-alias name (a later doc-rebrand pass).
    assert "agentacct mcp workflow-smoke" in reference
    assert "agentacct mcp workflow-smoke" in checklist
    # docs/reference.md now names the live agentacct_* MCP tools (post-cutover).
    assert "agentacct_record_event" in reference
    assert "agentacct_get_event_summary" in reference
    assert "does not call Claude, Codex, or provider APIs" in reference


def test_mcp_content_length_framing_roundtrip():
    stream = io.BytesIO()
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

    write_mcp_message(stream, message)
    stream.seek(0)

    assert read_mcp_message(stream) == message


def test_mcp_raw_json_framing_roundtrip():
    stream = io.BytesIO(b'{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{"roots":{},"elicitation":{}}}}')

    assert read_mcp_message(stream) == {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "capabilities": {"roots": {}, "elicitation": {}}},
    }


def test_mcp_serve_stdio_replies_with_matching_raw_json_framing(tmp_path):
    request = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "capabilities": {"roots": {}, "elicitation": {}}},
    }
    stdin = io.BytesIO(json.dumps(request, separators=(",", ":")).encode("utf-8"))
    stdout = io.BytesIO()

    serve_stdio(store_dir=tmp_path / "state", stdin=stdin, stdout=stdout)

    raw = stdout.getvalue()
    assert raw.startswith(b'{')
    assert b"Content-Length" not in raw
    response = json.loads(raw)
    assert response["id"] == 0
    assert response["result"]["protocolVersion"] == "2025-11-25"


def test_mcp_serve_stdio_replies_with_matching_content_length_framing(tmp_path):
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    stdin = io.BytesIO()
    write_mcp_message(stdin, request)
    stdin.seek(0)
    stdout = io.BytesIO()

    serve_stdio(store_dir=tmp_path / "state", stdin=stdin, stdout=stdout)

    raw = stdout.getvalue()
    assert raw.startswith(b"Content-Length:")
    assert b"agentacct_get_event_summary" in raw


def test_mcp_rejects_invalid_tool_arguments(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    bad_limit = server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "agentacct_list_runs", "arguments": {"limit": 0}}}
    )
    assert bad_limit["error"]["code"] == -32602
    assert "limit" in bad_limit["error"]["message"]


def test_mcp_rejects_unknown_keys_and_bad_optional_types(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    unknown = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "agentacct_get_report", "arguments": {"run_id": "latest", "unexpected": True}},
        }
    )
    assert unknown["error"]["code"] == -32602
    assert "unexpected" in unknown["error"]["message"]

    bad_summary = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "agentacct_record_machine_check",
                "arguments": {"run_id": "latest", "before_summary": {"not": "a string"}},
            },
        }
    )
    assert bad_summary["error"]["code"] == -32602
    assert "before_summary" in bad_summary["error"]["message"]


def test_mcp_cli_help_lists_serve_command():
    result = CliRunner().invoke(app, ["mcp", "--help"])

    assert result.exit_code == 0
    assert "serve" in result.output


def _call_tool(server, msg_id, name, arguments):
    return server.handle_message(
        {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    )


def test_mcp_record_section_inherits_attached_client_context(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    attach_response = _call_tool(
        server,
        1,
        "agentacct_attach_client_context",
        {
            "source": "claude-code",
            "client": "claude-code",
            "client_session_id": "3778e5d9-aaaa-bbbb-cccc-1234567890ab",
            "client_transcript_id": "3778e5d9-aaaa-bbbb-cccc-1234567890ab",
            "project_dir": "/tmp/project",
        },
    )
    attach_payload = _tool_payload(attach_response)
    assert attach_payload["join_hint_quality"] == "exact"
    assert attach_payload["warnings"] == []
    assert attach_payload["event"]["metadata"]["join_hint_quality"] == "exact"

    section_response = _call_tool(
        server,
        2,
        "agentacct_record_section",
        {"source": "claude-code", "section_id": "join-fix", "section_status": "started", "section_title": "Join fix"},
    )
    section_payload = _tool_payload(section_response)
    metadata = section_payload["event"]["metadata"]
    assert metadata["client"] == "claude-code"
    assert metadata["client_session_id"] == "3778e5d9-aaaa-bbbb-cccc-1234567890ab"
    assert metadata["client_transcript_id"] == "3778e5d9-aaaa-bbbb-cccc-1234567890ab"
    assert metadata["project_dir"] == "/tmp/project"
    assert sorted(metadata["client_context_inherited_keys"]) == [
        "client",
        "client_session_id",
        "client_transcript_id",
        "project_dir",
    ]
    assert metadata["client_context_inherited_from_event_id"] == attach_payload["event"]["event_id"]
    # Inherited ids never claim exact: freshness across conversations is unproven.
    assert section_payload["join_hint_quality"] == "inherited"
    assert any("medium" in warning for warning in section_payload["warnings"])


def test_mcp_record_section_explicit_ids_override_inherited_context(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    _call_tool(
        server,
        1,
        "agentacct_attach_client_context",
        {
            "source": "codex",
            "client": "codex",
            "client_session_id": "attached-session",
            "client_transcript_id": "attached-transcript",
        },
    )
    section_response = _call_tool(
        server,
        2,
        "agentacct_record_section",
        {
            "source": "codex",
            "section_id": "explicit-ids",
            "section_status": "checkpoint",
            "client_session_id": "explicit-session",
            "client_transcript_id": "explicit-transcript",
        },
    )
    metadata = _tool_payload(section_response)["event"]["metadata"]
    assert metadata["client_session_id"] == "explicit-session"
    assert metadata["client_transcript_id"] == "explicit-transcript"
    # Only the fields the caller left unset were inherited.
    assert metadata["client_context_inherited_keys"] == ["client"]
    assert metadata["client"] == "codex"


def test_mcp_attach_client_context_without_join_keys_warns(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    attach_response = _call_tool(
        server,
        1,
        "agentacct_attach_client_context",
        {"source": "codex", "client": "codex", "project_dir": "/tmp/project"},
    )
    attach_payload = _tool_payload(attach_response)
    assert attach_payload["join_hint_quality"] == "weak"
    assert attach_payload["warnings"]
    assert "client_session_id" in attach_payload["warnings"][0]
    assert attach_payload["event"]["metadata"]["join_hint_quality"] == "weak"

    section_response = _call_tool(
        server,
        2,
        "agentacct_record_section",
        {"source": "codex", "section_id": "weak-context", "section_status": "started"},
    )
    section_payload = _tool_payload(section_response)
    assert section_payload["join_hint_quality"] == "weak"
    assert section_payload["warnings"]
    assert "client_session_id" in section_payload["warnings"][0]


def test_usage_import_row_joins_section_with_inherited_context(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    session_id = "8651a799-0000-1111-2222-333344445555"

    _call_tool(
        server,
        1,
        "agentacct_attach_client_context",
        {
            "source": "claude-code",
            "client": "claude-code",
            "client_session_id": session_id,
            "client_transcript_id": session_id,
        },
    )
    _call_tool(
        server,
        2,
        "agentacct_record_section",
        {
            "source": "claude-code",
            "section_id": "inherited-join",
            "section_status": "completed",
            "section_title": "Inherited join",
            "kind": "implementation",
        },
    )
    server.service.record_event(
        {
            "source": "claude-code-local-session-import",
            "event_type": "model_usage",
            "provider": "claude-code",
            "estimated_input_tokens": 1200,
            "estimated_output_tokens": 400,
            "metadata": {
                "client": "claude-code",
                "client_session_id": session_id,
                "client_transcript_id": session_id,
            },
        },
        trusted_usage_import=True,
    )

    ledger = build_work_ledger(server.service.list_all_events())
    assert len(ledger["usage_events"]) == 1
    attribution = ledger["attributions"][0]
    assert attribution["section_id"] == "inherited-join"
    # Inherited join keys attribute the usage, but at medium confidence with an
    # inherited_* strategy — never exact.
    assert attribution["join_confidence"] == "medium"
    assert attribution["join_strategy"] in {"inherited_client_session_id", "inherited_client_transcript_id"}
    assert ledger["overview"]["attributed_count"] == 1
    assert ledger["overview"]["usage_without_mcp_context_count"] == 0


def test_mcp_failed_attach_clears_inherited_context(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    _call_tool(
        server,
        1,
        "agentacct_attach_client_context",
        {"source": "claude-code", "client": "claude-code", "client_session_id": "old-session"},
    )
    failed = _call_tool(
        server,
        2,
        "agentacct_attach_client_context",
        {"source": "claude-code", "client": "claude-code"},
    )
    assert failed["error"]["code"] == -32602

    section_response = _call_tool(
        server,
        3,
        "agentacct_record_section",
        {"source": "claude-code", "section_id": "after-failed-attach", "section_status": "started"},
    )
    section_payload = _tool_payload(section_response)
    metadata = section_payload["event"]["metadata"]
    # Fail safe: a failed re-attach must yield missing attribution, never a
    # stale session id inherited from the previous attach.
    assert "client_session_id" not in metadata
    assert "client_context_inherited_keys" not in metadata
    assert section_payload["warnings"]


def test_mcp_idempotent_attach_reports_persisted_join_quality(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    first = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_attach_client_context",
            {"source": "codex", "client": "codex", "project_dir": "/tmp/project", "idempotency_key": "attach-ctx"},
        )
    )
    replay = _tool_payload(
        _call_tool(
            server,
            2,
            "agentacct_attach_client_context",
            {"source": "codex", "client": "codex", "client_session_id": "real-session", "idempotency_key": "attach-ctx"},
        )
    )
    # The replay returns the stored weak event; payload and inherited context
    # must describe what is persisted, not what this call asked for.
    assert replay["event"]["event_id"] == first["event"]["event_id"]
    assert replay["join_hint_quality"] == "weak"
    assert replay["warnings"]

    section_metadata = _tool_payload(
        _call_tool(server, 3, "agentacct_record_section", {"source": "codex", "section_id": "s1", "section_status": "started"})
    )["event"]["metadata"]
    assert "client_session_id" not in section_metadata
    assert section_metadata["project_dir"] == "/tmp/project"


def test_mcp_section_drops_inherited_context_instead_of_breaking_metadata_limit(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    _call_tool(
        server,
        1,
        "agentacct_attach_client_context",
        {
            "source": "claude-code",
            "client": "claude-code",
            "client_session_id": "5" * 200,
            "client_transcript_id": "6" * 200,
            "project_dir": "/tmp/" + "p" * 500,
        },
    )
    section_response = _call_tool(
        server,
        2,
        "agentacct_record_section",
        {
            "source": "claude-code",
            "section_id": "size-test",
            "section_status": "started",
            "metadata": {"notes": "x" * 7900},
        },
    )
    section_payload = _tool_payload(section_response)
    metadata = section_payload["event"]["metadata"]
    # The call fit the 8192-byte metadata limit before inheritance, so it must
    # still be recorded; only the inherited context is dropped, with a warning.
    assert metadata["section_id"] == "size-test"
    assert "client_session_id" not in metadata
    assert "client_context_inherited_keys" not in metadata
    assert any("8192" in warning for warning in section_payload["warnings"])


def test_mcp_section_provenance_keys_cannot_be_forged(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    section_response = _call_tool(
        server,
        1,
        "agentacct_record_section",
        {
            "source": "codex",
            "section_id": "forged",
            "section_status": "started",
            "client_session_id": "victim-session",
            "metadata": {
                "client_context_inherited_keys": ["client_session_id"],
                "client_context_inherited_from_event_id": "evt_forged000000",
            },
        },
    )
    metadata = _tool_payload(section_response)["event"]["metadata"]
    assert "client_context_inherited_keys" not in metadata
    assert "client_context_inherited_from_event_id" not in metadata
    assert metadata["client_session_id"] == "victim-session"


def _record_trusted_usage(server, *, session_id, transcript_id=None, tokens=1500):
    server.service.record_event(
        {
            "source": "claude-code-local-session-import",
            "event_type": "model_usage",
            "provider": "claude-code",
            "estimated_input_tokens": tokens,
            "estimated_output_tokens": 100,
            "metadata": {
                "client": "claude-code",
                "client_session_id": session_id,
                "client_transcript_id": transcript_id or session_id,
            },
        },
        trusted_usage_import=True,
    )


def test_stale_inherited_context_never_produces_exact_attribution(tmp_path):
    """Long-lived MCP server: conversation A attaches exact ids, then a new
    conversation records a section without a fresh attach. The stale inherited
    ids must never yield exact attribution."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    _call_tool(
        server,
        1,
        "agentacct_attach_client_context",
        {
            "source": "claude-code",
            "client": "claude-code",
            "client_session_id": "conversation-a-session",
            "client_transcript_id": "conversation-a-session",
        },
    )
    # Client starts a new conversation (e.g. /clear) on the same MCP process;
    # the new conversation records work without re-attaching.
    _call_tool(
        server,
        2,
        "agentacct_record_section",
        {"source": "claude-code", "section_id": "conversation-b-work", "section_status": "completed"},
    )
    _record_trusted_usage(server, session_id="conversation-a-session")

    ledger = build_work_ledger(server.service.list_all_events())
    attribution = ledger["attributions"][0]
    assert attribution["join_confidence"] != "exact"
    assert attribution["join_confidence"] == "medium"
    assert attribution["join_strategy"] in {"inherited_client_session_id", "inherited_client_transcript_id"}
    assert "freshness" in attribution["join_reason"]
    # The work item and its explanation carry the downgraded confidence too.
    item = next(item for item in ledger["work_items"] if item["section_id"] == "conversation-b-work")
    assert item["join_confidence"] == "medium"
    assert item["inherited_join_keys"] == ["client_session_id", "client_transcript_id"]
    assert item["join_explanation"]["join_confidence"] != "exact"


def test_explicit_section_ids_still_produce_exact_attribution(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    _call_tool(
        server,
        1,
        "agentacct_record_section",
        {
            "source": "claude-code",
            "section_id": "explicit-work",
            "section_status": "completed",
            "client": "claude-code",
            "client_session_id": "explicit-session",
            "client_transcript_id": "explicit-session",
        },
    )
    _record_trusted_usage(server, session_id="explicit-session")

    ledger = build_work_ledger(server.service.list_all_events())
    attribution = ledger["attributions"][0]
    assert attribution["section_id"] == "explicit-work"
    assert attribution["join_confidence"] == "exact"
    assert attribution["join_strategy"] in {"exact_client_session_id", "exact_client_transcript_id"}


def test_generic_record_event_cannot_forge_client_context_provenance(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    response = _call_tool(
        server,
        1,
        "agentacct_record_event",
        {
            "source": "codex",
            "event_type": "section_started",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "forged-section",
                "client": "codex",
                "client_session_id": "victim-session",
                "client_context_inherited_keys": ["client_session_id"],
                "client_context_inherited_from_event_id": "evt_forged000000",
            },
        },
    )
    metadata = _tool_payload(response)["event"]["metadata"]
    assert "client_context_inherited_keys" not in metadata
    assert "client_context_inherited_from_event_id" not in metadata
    assert metadata["reserved_client_context_provenance_stripped"] is True
    # The ledger sees the forged section without inheritance provenance.
    ledger = build_work_ledger(server.service.list_all_events())
    work_event = next(event for event in ledger["work_events"] if event["section_id"] == "forged-section")
    assert work_event["inherited_join_keys"] == []


def test_service_record_event_strips_provenance_unless_preserved(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    event = {
        "source": "any",
        "event_type": "note",
        "metadata": {"client_context_inherited_keys": ["client_session_id"], "client_context_inherited_from_event_id": "evt_x"},
    }

    stripped = server.service.record_event(dict(event))
    assert "client_context_inherited_keys" not in stripped["metadata"]
    assert stripped["metadata"]["reserved_client_context_provenance_stripped"] is True

    preserved = server.service.record_event(dict(event), preserve_client_context_provenance=True)
    assert preserved["metadata"]["client_context_inherited_keys"] == ["client_session_id"]


def test_context_bridge_does_not_upgrade_stale_inherited_section_to_exact(tmp_path):
    """The old attach event matches the usage explicitly, but the section that
    canonically won attribution inherited its ids; the advisory bridge must
    agree with the canonical work_ledger confidence, never exact."""
    from agentacct.context_bridge import build_usage_context_bridge

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _call_tool(
        server,
        1,
        "agentacct_attach_client_context",
        {
            "source": "claude-code",
            "client": "claude-code",
            "client_session_id": "conversation-a-session",
            "client_transcript_id": "conversation-a-session",
        },
    )
    # New conversation on the same MCP process records work without re-attaching.
    _call_tool(
        server,
        2,
        "agentacct_record_section",
        {"source": "claude-code", "section_id": "conversation-b-work", "section_status": "completed"},
    )
    _record_trusted_usage(server, session_id="conversation-a-session")

    events = server.service.list_all_events()
    ledger = build_work_ledger(events)
    attribution = ledger["attributions"][0]
    assert attribution["join_confidence"] == "medium"
    assert attribution["join_strategy"] in {"inherited_client_session_id", "inherited_client_transcript_id"}

    bridge = build_usage_context_bridge(events)
    assert bridge["usage_records"] == 1
    link = bridge["links"][0]
    assert link["join_confidence"] != "exact"
    assert link["join_confidence"] == "medium"
    assert link["join_strategy"] not in {"exact_client_session_id", "exact_client_transcript_id"}
    assert link["join_strategy"] == "inherited_client_context"
    assert "not exact" in link["join_reason"]
    # The attach context event still shows as matched context; it just cannot
    # upgrade the link above the canonical work_ledger confidence.
    assert link["client_context_count"] == 1
    assert link["section_count"] == 1
    assert link["attribution_status"] == "attributed"


def test_context_bridge_explicit_section_ids_still_exact(tmp_path):
    from agentacct.context_bridge import build_usage_context_bridge

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _call_tool(
        server,
        1,
        "agentacct_attach_client_context",
        {
            "source": "claude-code",
            "client": "claude-code",
            "client_session_id": "explicit-session",
            "client_transcript_id": "explicit-session",
        },
    )
    _call_tool(
        server,
        2,
        "agentacct_record_section",
        {
            "source": "claude-code",
            "section_id": "explicit-work",
            "section_status": "completed",
            "client": "claude-code",
            "client_session_id": "explicit-session",
            "client_transcript_id": "explicit-session",
        },
    )
    _record_trusted_usage(server, session_id="explicit-session")

    events = server.service.list_all_events()
    ledger = build_work_ledger(events)
    assert ledger["attributions"][0]["join_confidence"] == "exact"

    bridge = build_usage_context_bridge(events)
    link = bridge["links"][0]
    assert link["join_confidence"] == "exact"
    assert link["join_strategy"] in {"exact_client_session_id", "exact_client_transcript_id"}
    assert link["attribution_status"] == "attributed"


def _write_hook_context(store_root, *, session_id="hooked-session", transcript_id=None, now=None):
    import time as _time

    from agentacct.hooks import write_claude_code_hook_context

    write_claude_code_hook_context(
        store_root,
        {
            "schema_version": "agent-sentinel.client-context.v1",
            "client": "claude-code",
            "client_session_id": session_id,
            "client_transcript_id": transcript_id or session_id,
            "project_label": "project",
            "source": "claude_code_hook",
            "hook_event_name": "PreToolUse",
        },
        now=_time.time() if now is None else now,
    )


def _write_per_session_hook_context(store_root, *, session_id, transcript_id=None, now=None, hook_ancestor_pids=None):
    """Write ONLY a per-session context file (no legacy slot), as if another
    session's hook had since overwritten the shared slot."""
    import time as _time

    from agentacct.hooks import _hook_context_filename, claude_code_hook_context_dir

    context_dir = claude_code_hook_context_dir(store_root)
    context_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "agent-sentinel.client-context.v1",
        "client": "claude-code",
        "client_session_id": session_id,
        "client_transcript_id": transcript_id or session_id,
        "project_label": "project",
        "source": "claude_code_hook",
        "hook_event_name": "PreToolUse",
        "observed_at": _time.time() if now is None else now,
    }
    if hook_ancestor_pids is not None:
        payload["hook_ancestor_pids"] = hook_ancestor_pids
    path = context_dir / _hook_context_filename(session_id)
    path.write_text(json.dumps(payload))
    return path


def test_mcp_section_inherits_hook_client_context(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _write_hook_context(server.service.store.root)

    section_payload = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {"source": "claude-code", "section_id": "hook-join", "section_status": "completed"},
        )
    )
    metadata = section_payload["event"]["metadata"]
    assert metadata["client"] == "claude-code"
    assert metadata["client_session_id"] == "hooked-session"
    assert metadata["client_transcript_id"] == "hooked-session"
    assert metadata["client_context_source"] == "claude_code_hook"
    assert metadata["context_freshness"] == "client_derived"
    assert metadata["client_context_inherited_from"] == "client-context/claude-code.json"
    assert "client_session_id" in metadata["client_context_inherited_keys"]
    # Privacy: no raw project path is inherited from the hook context.
    assert "project_dir" not in metadata
    assert section_payload["join_hint_quality"] == "client_derived"
    assert section_payload["warnings"] == []
    assert section_payload["inherited_client_context"]["source"] == "claude_code_hook"


def test_usage_joins_hook_derived_section_at_high_confidence(tmp_path):
    from agentacct.context_bridge import build_usage_context_bridge

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _write_hook_context(server.service.store.root)
    _call_tool(
        server,
        1,
        "agentacct_record_section",
        {"source": "claude-code", "section_id": "hook-join", "section_status": "completed"},
    )
    _record_trusted_usage(server, session_id="hooked-session")

    events = server.service.list_all_events()
    ledger = build_work_ledger(events)
    attribution = ledger["attributions"][0]
    assert attribution["section_id"] == "hook-join"
    # Client-derived, TTL-fresh, but not session-bound: high, never exact.
    assert attribution["join_confidence"] == "high"
    assert attribution["join_strategy"] in {"client_derived_client_session_id", "client_derived_client_transcript_id"}
    assert ledger["overview"]["attributed_count"] == 1

    bridge = build_usage_context_bridge(events)
    link = bridge["links"][0]
    assert link["join_confidence"] == "high"
    assert link["join_strategy"] == "client_derived_client_context"
    assert link["attribution_status"] == "attributed"


def test_stale_hook_context_is_not_inherited(tmp_path):
    import time as _time

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _write_hook_context(server.service.store.root, session_id="old-hook-session", now=_time.time() - 7200)

    section_payload = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {"source": "claude-code", "section_id": "after-stale-hook", "section_status": "started"},
        )
    )
    metadata = section_payload["event"]["metadata"]
    assert "client_session_id" not in metadata
    assert "client_context_source" not in metadata
    assert section_payload["warnings"]
    _record_trusted_usage(server, session_id="old-hook-session")
    ledger = build_work_ledger(server.service.list_all_events())
    # The usage row stays unjoined instead of being wrongly attributed.
    assert ledger["attributions"][0]["join_strategy"] == "unjoined"


def test_hook_context_outranks_attach_context_for_ids(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _write_hook_context(server.service.store.root, session_id="hooked-session")
    _call_tool(
        server,
        1,
        "agentacct_attach_client_context",
        {"source": "claude-code", "client": "claude-code", "client_session_id": "agent-reported-session"},
    )

    metadata = _tool_payload(
        _call_tool(
            server,
            2,
            "agentacct_record_section",
            {"source": "claude-code", "section_id": "priority", "section_status": "started"},
        )
    )["event"]["metadata"]
    # Hook ids are client-derived and outrank agent-reported attach ids; the
    # id pair comes from a single source.
    assert metadata["client_session_id"] == "hooked-session"
    assert metadata["client_transcript_id"] == "hooked-session"
    assert metadata["client_context_source"] == "claude_code_hook"


def test_hook_context_not_applied_to_other_clients(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _write_hook_context(server.service.store.root, session_id="hooked-session")

    metadata = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {"source": "codex", "section_id": "codex-work", "section_status": "started", "client": "codex"},
        )
    )["event"]["metadata"]
    assert "client_session_id" not in metadata
    assert "client_context_source" not in metadata


def test_explicit_section_ids_override_hook_context(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _write_hook_context(server.service.store.root, session_id="hooked-session")

    metadata = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {
                "source": "claude-code",
                "section_id": "explicit-wins",
                "section_status": "started",
                "client_session_id": "explicit-session",
                "client_transcript_id": "explicit-session",
            },
        )
    )["event"]["metadata"]
    assert metadata["client_session_id"] == "explicit-session"
    # No id was inherited, so no hook provenance is stamped.
    assert "client_context_source" not in metadata


def test_generic_record_event_cannot_forge_hook_provenance(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    response = _call_tool(
        server,
        1,
        "agentacct_record_event",
        {
            "source": "codex",
            "event_type": "section_started",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "forged-hook",
                "client": "claude-code",
                "client_session_id": "victim-session",
                "client_context_source": "claude_code_hook",
                "context_freshness": "client_derived",
                "client_context_inherited_from": "client-context/claude-code.json",
                "client_context_inherited_keys": ["client_session_id"],
                "client_context_selection": "env_session_match",
                "client_context_inheritance_refused": "concurrent_claude_code_hook_contexts",
                "hook_context_fresh_count": 2,
            },
        },
    )
    metadata = _tool_payload(response)["event"]["metadata"]
    for key in (
        "client_context_source",
        "context_freshness",
        "client_context_inherited_from",
        "client_context_inherited_keys",
        "client_context_selection",
        "client_context_inheritance_refused",
        "hook_context_fresh_count",
    ):
        assert key not in metadata
    assert metadata["reserved_client_context_provenance_stripped"] is True


def test_section_cannot_forge_hook_provenance_without_inheritance(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    metadata = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {
                "source": "claude-code",
                "section_id": "forged-freshness",
                "section_status": "started",
                "client_session_id": "explicit-session",
                "metadata": {
                    "client_context_source": "claude_code_hook",
                    "context_freshness": "client_derived",
                    "client_context_selection": "single_fresh",
                    "client_context_inheritance_refused": "concurrent_claude_code_hook_contexts",
                    "hook_context_fresh_count": 3,
                },
            },
        )
    )["event"]["metadata"]
    assert "client_context_source" not in metadata
    assert "context_freshness" not in metadata
    assert "client_context_selection" not in metadata
    assert "client_context_inheritance_refused" not in metadata
    assert "hook_context_fresh_count" not in metadata


def test_hook_context_not_mixed_with_conflicting_explicit_id(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _write_hook_context(server.service.store.root, session_id="hooked-session")

    metadata = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {
                "source": "claude-code",
                "section_id": "conflicting-session",
                "section_status": "started",
                "client_session_id": "different-session",
            },
        )
    )["event"]["metadata"]
    # The caller described a different session than the hook context; the hook
    # transcript id must not be paired with the explicit session id.
    assert metadata["client_session_id"] == "different-session"
    assert "client_transcript_id" not in metadata
    assert "client_context_source" not in metadata


def test_explicit_session_id_with_hook_context_yields_exact(tmp_path):
    """The documented upgrade path must work: passing the session id
    explicitly gives exact attribution; the hook must not complete the pair
    with an inherited transcript that would downgrade the join to high."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _write_hook_context(server.service.store.root, session_id="hooked-session")

    section_payload = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {
                "source": "claude-code",
                "section_id": "explicit-upgrade",
                "section_status": "completed",
                "client_session_id": "hooked-session",
            },
        )
    )
    metadata = section_payload["event"]["metadata"]
    assert metadata["client_session_id"] == "hooked-session"
    assert "client_transcript_id" not in metadata
    assert section_payload["join_hint_quality"] == "exact"
    _record_trusted_usage(server, session_id="hooked-session")

    ledger = build_work_ledger(server.service.list_all_events())
    attribution = ledger["attributions"][0]
    assert attribution["section_id"] == "explicit-upgrade"
    assert attribution["join_confidence"] == "exact"
    assert attribution["join_strategy"] == "exact_client_session_id"


def test_attach_ids_not_mixed_with_explicit_id(tmp_path):
    """An explicitly passed id blocks attach id inheritance too: the pair
    must never mix a new conversation's session with a stale transcript."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _call_tool(
        server,
        1,
        "agentacct_attach_client_context",
        {
            "source": "claude-code",
            "client": "claude-code",
            "client_session_id": "old-session",
            "client_transcript_id": "old-transcript",
        },
    )

    metadata = _tool_payload(
        _call_tool(
            server,
            2,
            "agentacct_record_section",
            {
                "source": "claude-code",
                "section_id": "new-conversation",
                "section_status": "started",
                "client_session_id": "new-session",
            },
        )
    )["event"]["metadata"]
    assert metadata["client_session_id"] == "new-session"
    assert "client_transcript_id" not in metadata


def test_hook_context_requires_claude_source_when_client_unset(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _write_hook_context(server.service.store.root, session_id="hooked-session")

    metadata = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {"source": "codex", "section_id": "codex-no-client", "section_status": "started"},
        )
    )["event"]["metadata"]
    # A non-claude source with no client must not absorb claude-code hook ids.
    assert "client_session_id" not in metadata
    assert "client_context_source" not in metadata
    assert "client" not in metadata


def test_concurrent_hook_contexts_refuse_inheritance_end_to_end(tmp_path):
    """Two concurrently fresh hook contexts with no way to prove which one is
    this server's own: inherit NOTHING and record why. The usage row stays
    unattributed instead of being wrongly attributed (missing beats wrong)."""
    server = SentinelMCPServer(
        store_dir=tmp_path / "state", hook_env_session_id=None, hook_consumer_ancestor_pids=None
    )
    _write_hook_context(server.service.store.root, session_id="session-a")
    _write_hook_context(server.service.store.root, session_id="session-b")

    payload = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {"source": "claude-code", "section_id": "ambiguous-work", "section_status": "completed"},
        )
    )
    metadata = payload["event"]["metadata"]
    assert "client_session_id" not in metadata
    assert "client_transcript_id" not in metadata
    assert "client_context_source" not in metadata
    # Absolute refusal: not even the non-id client key is inherited.
    assert "client" not in metadata
    assert metadata["client_context_inheritance_refused"] == "concurrent_claude_code_hook_contexts"
    assert metadata["hook_context_fresh_count"] == 2
    assert payload["refused_client_context"]["reason"] == "concurrent_claude_code_hook_contexts"
    assert payload["refused_client_context"]["fresh_context_count"] == 2
    assert any("concurrent" in warning.lower() for warning in payload["warnings"])

    _record_trusted_usage(server, session_id="session-a")
    ledger = build_work_ledger(server.service.list_all_events())
    # Never the last writer's ids: the usage row stays unjoined.
    assert ledger["attributions"][0]["join_strategy"] == "unjoined"
    assert ledger["overview"]["attributed_count"] == 0


def test_concurrent_contexts_env_binding_selects_own_session(tmp_path):
    """CLAUDE_CODE_SESSION_ID binding: the env-matched candidate is inherited
    when it is also STRICTLY newest; ids stay client-derived, never exact."""
    import time as _time

    server = SentinelMCPServer(
        store_dir=tmp_path / "state", hook_env_session_id="session-a", hook_consumer_ancestor_pids=None
    )
    _write_hook_context(server.service.store.root, session_id="session-b", now=_time.time() - 30)
    _write_per_session_hook_context(server.service.store.root, session_id="session-a")

    payload = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {"source": "claude-code", "section_id": "env-bound", "section_status": "completed"},
        )
    )
    metadata = payload["event"]["metadata"]
    assert metadata["client_session_id"] == "session-a"
    assert metadata["client_transcript_id"] == "session-a"
    assert metadata["client_context_source"] == "claude_code_hook"
    assert metadata["context_freshness"] == "client_derived"
    assert metadata["client_context_selection"] == "env_session_match"
    # Provenance points at the actual per-session file that was selected.
    assert metadata["client_context_inherited_from"].startswith("client-context/claude-code/")
    assert "client_context_inheritance_refused" not in metadata

    _record_trusted_usage(server, session_id="session-a", tokens=1500)
    _record_trusted_usage(server, session_id="session-b", tokens=700)
    ledger = build_work_ledger(server.service.list_all_events())
    by_tokens = {attribution["usage_tokens"]: attribution for attribution in ledger["attributions"]}
    own = by_tokens[1600]
    assert own["section_id"] == "env-bound"
    assert own["join_confidence"] == "high"
    assert own["join_strategy"] in {"client_derived_client_session_id", "client_derived_client_transcript_id"}
    assert by_tokens[800]["join_strategy"] == "unjoined"


def test_concurrent_contexts_env_binding_requires_strict_recency_end_to_end(tmp_path):
    """A stale env binding (e.g. session id rotated by /clear after the MCP
    server spawned) must degrade to refusal, never pick the stale candidate."""
    import time as _time

    server = SentinelMCPServer(
        store_dir=tmp_path / "state", hook_env_session_id="session-a", hook_consumer_ancestor_pids=None
    )
    _write_per_session_hook_context(server.service.store.root, session_id="session-a", now=_time.time() - 60)
    _write_hook_context(server.service.store.root, session_id="session-b")

    metadata = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {"source": "claude-code", "section_id": "stale-env", "section_status": "completed"},
        )
    )["event"]["metadata"]
    assert "client_session_id" not in metadata
    assert metadata["client_context_inheritance_refused"] == "concurrent_claude_code_hook_contexts"


def test_concurrent_contexts_pid_lineage_selects_own_session(tmp_path):
    """Process-lineage binding: only the context written by a hook of THIS
    server's own claude process is inherited; sibling sessions under the same
    claude process tie and refuse."""
    server = SentinelMCPServer(
        store_dir=tmp_path / "state-distinct", hook_env_session_id=None, hook_consumer_ancestor_pids=[4242]
    )
    _write_per_session_hook_context(
        server.service.store.root, session_id="session-a", hook_ancestor_pids=[31337, 4242, 999]
    )
    _write_per_session_hook_context(
        server.service.store.root, session_id="session-b", hook_ancestor_pids=[41414, 5151, 999]
    )

    payload = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {"source": "claude-code", "section_id": "lineage-bound", "section_status": "completed"},
        )
    )
    metadata = payload["event"]["metadata"]
    assert metadata["client_session_id"] == "session-a"
    assert metadata["client_context_selection"] == "pid_lineage_match"
    assert metadata["client_context_source"] == "claude_code_hook"

    # Sibling sessions under the SAME claude process: both chains carry the
    # consumer's ancestor -> tie -> refusal metadata instead of a guess.
    sibling_server = SentinelMCPServer(
        store_dir=tmp_path / "state-sibling", hook_env_session_id=None, hook_consumer_ancestor_pids=[4242]
    )
    _write_per_session_hook_context(
        sibling_server.service.store.root, session_id="session-a", hook_ancestor_pids=[31337, 4242]
    )
    _write_per_session_hook_context(
        sibling_server.service.store.root, session_id="session-b", hook_ancestor_pids=[41414, 4242]
    )
    sibling_metadata = _tool_payload(
        _call_tool(
            sibling_server,
            1,
            "agentacct_record_section",
            {"source": "claude-code", "section_id": "sibling-work", "section_status": "completed"},
        )
    )["event"]["metadata"]
    assert "client_session_id" not in sibling_metadata
    assert sibling_metadata["client_context_inheritance_refused"] == "concurrent_claude_code_hook_contexts"
    assert sibling_metadata["hook_context_fresh_count"] == 2


def test_explicit_ids_suppress_refusal_stamp(tmp_path):
    """A caller that passes its own id never needed inheritance: no refusal
    stamp, and the explicit id keeps exact quality."""
    server = SentinelMCPServer(
        store_dir=tmp_path / "state", hook_env_session_id=None, hook_consumer_ancestor_pids=None
    )
    _write_hook_context(server.service.store.root, session_id="session-a")
    _write_hook_context(server.service.store.root, session_id="session-b")

    payload = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {
                "source": "claude-code",
                "section_id": "explicit-own-id",
                "section_status": "completed",
                "client_session_id": "my-own-session",
            },
        )
    )
    metadata = payload["event"]["metadata"]
    assert metadata["client_session_id"] == "my-own-session"
    assert "client_context_inheritance_refused" not in metadata
    assert "hook_context_fresh_count" not in metadata
    assert "refused_client_context" not in payload
    assert payload["join_hint_quality"] == "exact"


def test_refused_section_replay_payload_matches_persisted_event(tmp_path):
    """Idempotent replays describe what was PERSISTED: a replay under a
    later-appearing second context must not claim (or stamp) a refusal."""
    server = SentinelMCPServer(
        store_dir=tmp_path / "state", hook_env_session_id=None, hook_consumer_ancestor_pids=None
    )
    _write_hook_context(server.service.store.root, session_id="session-a")
    args = {
        "source": "claude-code",
        "section_id": "replay-vs-refusal",
        "section_status": "started",
        "idempotency_key": "replay-vs-refusal-1",
    }
    first = _tool_payload(_call_tool(server, 1, "agentacct_record_section", args))
    assert first["event"]["metadata"]["client_session_id"] == "session-a"

    _write_hook_context(server.service.store.root, session_id="session-b")
    replay = _tool_payload(_call_tool(server, 2, "agentacct_record_section", args))
    assert replay["event"]["event_id"] == first["event"]["event_id"]
    assert replay["event"]["metadata"]["client_session_id"] == "session-a"
    assert "client_context_inheritance_refused" not in replay["event"]["metadata"]
    assert "refused_client_context" not in replay
    assert replay["inherited_client_context"]["source"] == "claude_code_hook"


def test_metadata_overflow_keeps_refusal_marker(tmp_path):
    """The overflow fallback drops inherited attach context but keeps the
    tiny concurrent-session refusal marker that explains the missing ids."""
    server = SentinelMCPServer(
        store_dir=tmp_path / "state", hook_env_session_id=None, hook_consumer_ancestor_pids=None
    )
    _write_hook_context(server.service.store.root, session_id="session-a")
    _write_hook_context(server.service.store.root, session_id="session-b")
    _call_tool(
        server,
        1,
        "agentacct_attach_client_context",
        {
            "source": "claude-code",
            "client": "claude-code",
            "client_session_id": "attach-" + "s" * 230,
            "client_transcript_id": "attach-" + "t" * 230,
            "project_dir": "/tmp/" + "p" * 900,
        },
    )

    payload = _tool_payload(
        _call_tool(
            server,
            2,
            "agentacct_record_section",
            {
                "source": "claude-code",
                "section_id": "overflow-refusal",
                "section_status": "completed",
                "metadata": {"filler": "x" * 7000},
            },
        )
    )
    metadata = payload["event"]["metadata"]
    # Attach inheritance was dropped to fit the metadata ceiling...
    assert "client_session_id" not in metadata
    assert "client_context_inherited_keys" not in metadata
    assert any("dropped" in warning for warning in payload["warnings"])
    # ...but the refusal marker survives the reserved-key cleanup.
    assert metadata["client_context_inheritance_refused"] == "concurrent_claude_code_hook_contexts"
    assert metadata["hook_context_fresh_count"] == 2
    assert payload["refused_client_context"]["reason"] == "concurrent_claude_code_hook_contexts"


def test_section_spanning_sessions_keeps_per_session_snapshots(tmp_path):
    """Namespaced work identity keeps one snapshot per (client, session,
    section): a section that spans two hook sessions yields two work items,
    and usage from the first session joins ITS snapshot instead of vanishing
    behind the later session's snapshot. Ledger and bridge agree.

    Sessions here are SEQUENTIAL (conversation-a's context ages out before
    conversation-b starts); two concurrently fresh contexts refuse
    inheritance instead — covered by the concurrent-context tests."""
    import time as _time

    from agentacct.context_bridge import build_usage_context_bridge

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _write_hook_context(server.service.store.root, session_id="conversation-a")
    _call_tool(
        server,
        1,
        "agentacct_record_section",
        {"source": "claude-code", "section_id": "spanning-work", "section_status": "started"},
    )
    # Conversation A's context expires (idle gap), then a new conversation
    # takes over: the hook publishes B as the only fresh context.
    _write_hook_context(server.service.store.root, session_id="conversation-a", now=_time.time() - 7200)
    _write_hook_context(server.service.store.root, session_id="conversation-b")
    _call_tool(
        server,
        2,
        "agentacct_record_section",
        {"source": "claude-code", "section_id": "spanning-work", "section_status": "completed"},
    )
    _record_trusted_usage(server, session_id="conversation-a")

    events = server.service.list_all_events()
    ledger = build_work_ledger(events)
    attribution = ledger["attributions"][0]
    assert attribution["section_id"] == "spanning-work"
    # Hook-derived ids: client-derived and fresh, but never exact.
    assert attribution["join_confidence"] == "high"
    assert attribution["join_strategy"] in {"client_derived_client_session_id", "client_derived_client_transcript_id"}
    items_by_work_id = {item["work_id"]: item for item in ledger["work_items"]}
    assert len(items_by_work_id) == 2
    attributed_item = items_by_work_id[attribution["work_id"]]
    assert attributed_item["client_session_id"] == "conversation-a"
    assert attributed_item["usage_total"] > 0
    other_item = next(item for item in ledger["work_items"] if item["work_id"] != attribution["work_id"])
    assert other_item["client_session_id"] == "conversation-b"
    assert other_item["usage_total"] == 0

    bridge = build_usage_context_bridge(events)
    link = bridge["links"][0]
    assert link["join_confidence"] == "high"
    assert link["join_strategy"] == "client_derived_client_context"
    assert link["attribution_status"] == "attributed"


def test_item_mixed_inheritance_sources_stay_per_key(tmp_path):
    """A transcript inherited from attach must stay medium-tier even when a
    later event hook-inherits the session id for the same work item."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    _call_tool(
        server,
        1,
        "agentacct_attach_client_context",
        {"source": "claude-code", "client": "claude-code", "client_transcript_id": "attach-transcript"},
    )
    _call_tool(
        server,
        2,
        "agentacct_record_section",
        {"source": "claude-code", "section_id": "mixed-sources", "section_status": "started"},
    )
    # Hook context appears with a session id but no usable transcript.
    _write_hook_context(server.service.store.root, session_id="hook-session", transcript_id=None)
    hook_path = server.service.store.root / "client-context" / "claude-code.json"
    payload = json.loads(hook_path.read_text())
    payload["client_transcript_id"] = None
    hook_path.write_text(json.dumps(payload))
    _call_tool(
        server,
        3,
        "agentacct_record_section",
        {"source": "claude-code", "section_id": "mixed-sources", "section_status": "checkpoint"},
    )
    _record_trusted_usage(server, session_id="unrelated-session", transcript_id="attach-transcript")

    ledger = build_work_ledger(server.service.list_all_events())
    # Namespaced identity splits the two snapshots; the transcript-carrying
    # snapshot keeps its attach-tier provenance per key.
    item = next(item for item in ledger["work_items"] if item.get("client_transcript_id") == "attach-transcript")
    assert item["section_id"] == "mixed-sources"
    assert item["inherited_key_sources"].get("client_transcript_id") is None
    explanation = item["join_explanation"]
    # The attach-inherited transcript match must not be presented at the
    # hook (high) tier just because a later event was hook-sourced.
    assert explanation["join_confidence"] != "high"
    assert explanation["join_confidence"] != "exact"


def test_idempotent_section_replay_payload_matches_persisted_event(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    args = {
        "source": "claude-code",
        "section_id": "replayed",
        "section_status": "started",
        "idempotency_key": "section-replay",
    }
    first = _tool_payload(_call_tool(server, 1, "agentacct_record_section", args))
    assert "inherited_client_context" not in first

    # Hook context appears after the first call; the replay returns the stored
    # event and must not claim inheritance that was never persisted.
    _write_hook_context(server.service.store.root, session_id="hooked-session")
    replay = _tool_payload(_call_tool(server, 2, "agentacct_record_section", args))
    assert replay["event"]["event_id"] == first["event"]["event_id"]
    assert "inherited_client_context" not in replay
    assert "client_session_id" not in replay["event"]["metadata"]


def test_mcp_section_inherits_windows_hook_context_without_raw_paths(tmp_path):
    from agentacct.hooks import derive_claude_code_client_context, write_claude_code_hook_context

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    context = derive_claude_code_client_context(
        {
            "session_id": "3778e5d9-aaaa-bbbb-cccc-1234567890ab",
            "transcript_path": "C:\\Users\\alice\\.claude\\projects\\secret\\abc123.jsonl",
            "cwd": "C:\\Users\\alice\\code\\secret-project",
            "hook_event_name": "PreToolUse",
        }
    )
    write_claude_code_hook_context(server.service.store.root, context)

    metadata = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {"source": "claude-code", "section_id": "windows-host", "section_status": "started"},
        )
    )["event"]["metadata"]
    assert metadata["client_session_id"] == "3778e5d9-aaaa-bbbb-cccc-1234567890ab"
    assert metadata["client_transcript_id"] == "abc123"
    assert "project_dir" not in metadata
    text = json.dumps(metadata)
    assert "C:" not in text
    assert "alice" not in text


def test_section_free_form_metadata_cannot_smuggle_join_keys(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    response = _call_tool(
        server,
        1,
        "agentacct_record_section",
        {
            "source": "codex",
            "section_id": "smuggle",
            "section_status": "completed",
            "metadata": {"client_session_id": "victim-session", "client_transcript_id": "victim-session"},
        },
    )
    metadata = _tool_payload(response)["event"]["metadata"]
    assert "client_session_id" not in metadata
    assert "client_transcript_id" not in metadata
    assert "client_context_keys_authored" not in metadata
    assert "client_session_id" in metadata["reserved_context_keys_stripped"]
    assert "client_transcript_id" in metadata["reserved_context_keys_stripped"]

    _record_trusted_usage(server, session_id="victim-session")
    ledger = build_work_ledger(server.service.list_all_events())
    # The smuggled ids never reached the store, so the usage row stays honest.
    assert ledger["attributions"][0]["join_strategy"] == "unjoined"
    assert ledger["attributions"][0]["join_confidence"] == "unjoined"


def test_attach_debug_and_machine_check_metadata_cannot_smuggle_join_keys(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    attach_metadata = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_attach_client_context",
            {
                "source": "codex",
                "client": "codex",
                "project_dir": "/tmp/project",
                "metadata": {"client_session_id": "victim-session", "client_transcript_id": "victim-transcript"},
            },
        )
    )["event"]["metadata"]
    assert "client_session_id" not in attach_metadata
    assert "client_transcript_id" not in attach_metadata
    assert "client_session_id" in attach_metadata["reserved_context_keys_stripped"]

    debug_metadata = _tool_payload(
        _call_tool(
            server,
            2,
            "agentacct_record_agent_usage_debug",
            {
                "source": "codex",
                "client": "codex",
                "reporting_basis": "unavailable",
                "metadata": {"client_session_id": "victim-session", "client_transcript_id": "victim-transcript"},
            },
        )
    )["event"]["metadata"]
    assert "client_session_id" not in debug_metadata
    assert "client_transcript_id" not in debug_metadata
    assert "client_session_id" in debug_metadata["reserved_context_keys_stripped"]

    # agentacct_record_machine_check accepts no free-form metadata argument at
    # all, so this smuggling surface is rejected outright.
    check_response = _call_tool(
        server,
        3,
        "agentacct_record_machine_check",
        {
            "source": "codex",
            "result": "passed",
            "summary": "ok",
            "metadata": {"client_session_id": "victim-session", "client_transcript_id": "victim-transcript"},
        },
    )
    assert check_response["error"]["code"] == -32602
    assert "metadata" in check_response["error"]["message"]


def test_generic_record_event_section_ids_never_earn_exact(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    _call_tool(
        server,
        1,
        "agentacct_record_event",
        {
            "source": "codex",
            "event_type": "section_started",
            "metadata": {
                "sentinel_semantic_kind": "section",
                "section_id": "generic-section",
                "client": "claude-code",
                "client_session_id": "generic-session",
            },
        },
    )
    _record_trusted_usage(server, session_id="generic-session")

    ledger = build_work_ledger(server.service.list_all_events())
    attribution = ledger["attributions"][0]
    # Generic-path ids keep joining, but their provenance is unverifiable:
    # capped at high, never exact.
    assert attribution["section_id"] == "generic-section"
    assert attribution["join_strategy"] == "unverified_client_session_id"
    assert attribution["join_confidence"] == "high"
    assert "unverified" in attribution["join_reason"]


def test_authored_marker_cannot_be_forged(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    generic_metadata = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_event",
            {
                "source": "codex",
                "event_type": "section_started",
                "metadata": {
                    "sentinel_semantic_kind": "section",
                    "section_id": "forged-authored",
                    "client_session_id": "victim-session",
                    "client_context_keys_authored": ["client_session_id"],
                },
            },
        )
    )["event"]["metadata"]
    assert "client_context_keys_authored" not in generic_metadata
    assert generic_metadata["reserved_client_context_provenance_stripped"] is True

    section_metadata = _tool_payload(
        _call_tool(
            server,
            2,
            "agentacct_record_section",
            {
                "source": "codex",
                "section_id": "no-ids",
                "section_status": "started",
                "metadata": {"client_context_keys_authored": ["client_session_id"]},
            },
        )
    )["event"]["metadata"]
    assert "client_context_keys_authored" not in section_metadata


def test_explicit_section_ids_persist_authored_marker_and_stay_exact(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    metadata = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {
                "source": "claude-code",
                "section_id": "explicit-marker",
                "section_status": "completed",
                "client": "claude-code",
                "client_session_id": "explicit-session",
                "client_transcript_id": "explicit-session",
            },
        )
    )["event"]["metadata"]
    assert metadata["client_context_keys_authored"] == ["client_session_id", "client_transcript_id"]

    _record_trusted_usage(server, session_id="explicit-session")
    ledger = build_work_ledger(server.service.list_all_events())
    assert ledger["attributions"][0]["join_confidence"] == "exact"
    assert ledger["attributions"][0]["join_strategy"] in {"exact_client_session_id", "exact_client_transcript_id"}


def test_benign_metadata_display_fields_survive_and_are_not_labelled_smuggled(tmp_path):
    """Minor fix (Phase 1 review): only join-id/provenance collisions are
    stripped+labelled. Display fields passed via metadata without the
    corresponding argument (summary, files, ...) are honest agent data and
    must persist without a reserved_context_keys_stripped entry."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    metadata = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {
                "source": "codex",
                "section_id": "display-fields",
                "section_status": "completed",
                "metadata": {"summary": "important context", "files": ["a.py"], "custom": "kept"},
            },
        )
    )["event"]["metadata"]

    assert metadata["summary"] == "important context"
    assert metadata["files"] == ["a.py"]
    assert metadata["custom"] == "kept"
    assert "reserved_context_keys_stripped" not in metadata


def test_supplied_argument_overwrites_colliding_benign_metadata_without_label(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    metadata = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {
                "source": "codex",
                "section_id": "server-wins",
                "section_status": "completed",
                "summary": "validated summary",
                "metadata": {"summary": "caller summary", "custom": "kept"},
            },
        )
    )["event"]["metadata"]

    assert metadata["summary"] == "validated summary"
    assert metadata["custom"] == "kept"
    assert "reserved_context_keys_stripped" not in metadata


def test_forged_strip_label_in_metadata_is_discarded(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    metadata = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {
                "source": "codex",
                "section_id": "forged-label",
                "section_status": "completed",
                "metadata": {"reserved_context_keys_stripped": ["client_session_id"]},
            },
        )
    )["event"]["metadata"]

    assert "reserved_context_keys_stripped" not in metadata


def test_refusal_note_with_attach_inherited_ids_does_not_claim_unattributed(tmp_path):
    """Minor fix (Phase 1 review): when hook inheritance is refused but a
    prior attach still supplies ids, the payload must say hook inheritance was
    refused — not falsely claim the section stays unattributed."""
    server = SentinelMCPServer(
        store_dir=tmp_path / "state", hook_env_session_id=None, hook_consumer_ancestor_pids=None
    )
    _call_tool(
        server,
        1,
        "agentacct_attach_client_context",
        {
            "source": "claude-code",
            "client": "claude-code",
            "client_session_id": "attach-session",
            "client_transcript_id": "attach-session",
        },
    )
    _write_hook_context(server.service.store.root, session_id="session-a")
    _write_hook_context(server.service.store.root, session_id="session-b")

    payload = _tool_payload(
        _call_tool(
            server,
            2,
            "agentacct_record_section",
            {"source": "claude-code", "section_id": "attach-after-refusal", "section_status": "completed"},
        )
    )

    metadata = payload["event"]["metadata"]
    assert metadata["client_context_inheritance_refused"] == "concurrent_claude_code_hook_contexts"
    assert metadata["client_session_id"] == "attach-session"
    note = payload["refused_client_context"]["note"]
    assert "stays unattributed" not in note
    assert "hook-context id inheritance was refused" in note
    assert "agentacct_attach_client_context" in note
    assert any("attach-inherited ids were used instead" in warning for warning in payload["warnings"])
    # The pure-refusal wording is preserved when no attach ids exist.
    fresh = SentinelMCPServer(
        store_dir=tmp_path / "state2", hook_env_session_id=None, hook_consumer_ancestor_pids=None
    )
    _write_hook_context(fresh.service.store.root, session_id="session-a")
    _write_hook_context(fresh.service.store.root, session_id="session-b")
    bare = _tool_payload(
        _call_tool(
            fresh,
            1,
            "agentacct_record_section",
            {"source": "claude-code", "section_id": "bare-refusal", "section_status": "completed"},
        )
    )
    assert "stays unattributed" in bare["refused_client_context"]["note"]


# --- MCP input-surface hardening ---------------------------------------------


def test_section_accepts_title_alias_and_prefers_section_title(tmp_path):
    """The shipped instruction surfaces told agents to pass `title` and the
    schema rejected the whole call. The HTTP lane has always called this field
    `title` and work_events reads `section_title or title`, so MCP was the
    outlier: accept the alias, but let the canonical name win."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    aliased = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {"source": "codex", "section_id": "alias", "section_status": "started", "title": "add rate-limit to login"},
        )
    )
    assert aliased["event"]["metadata"]["section_title"] == "add rate-limit to login"

    both = _tool_payload(
        _call_tool(
            server,
            2,
            "agentacct_record_section",
            {
                "source": "codex",
                "section_id": "alias-both",
                "section_status": "started",
                "section_title": "canonical",
                "title": "alias",
            },
        )
    )
    assert both["event"]["metadata"]["section_title"] == "canonical"

    # The alias is one specific key, not a hole in the unknown-key guard.
    still_unknown = _call_tool(
        server,
        3,
        "agentacct_record_section",
        {"source": "codex", "section_id": "alias-bad", "section_status": "started", "titel": "typo"},
    )
    assert still_unknown["error"]["code"] == -32602
    assert "titel" in still_unknown["error"]["message"]

    # A malformed alias is rejected rather than silently ignored.
    bad_alias = _call_tool(
        server,
        4,
        "agentacct_record_section",
        {"source": "codex", "section_id": "alias-long", "section_status": "started", "title": "t" * 161},
    )
    assert bad_alias["error"]["code"] == -32602
    assert "title" in bad_alias["error"]["message"]


def test_shipped_instructions_name_the_argument_the_schema_accepts(tmp_path):
    """Every instruction surface is generated from one constant; the section tool
    must accept whatever field name that constant tells agents to set."""
    from agentacct.install_guide import MCP_SERVER_INSTRUCTIONS

    assert "set `section_title`" in MCP_SERVER_INSTRUCTIONS
    assert "set `title`" not in MCP_SERVER_INSTRUCTIONS

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    tools = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    section_props = {tool["name"]: tool for tool in tools["result"]["tools"]}["agentacct_record_section"]["inputSchema"]["properties"]
    assert "section_title" in section_props
    assert section_props["title"]["maxLength"] == 160
    assert "section_title" in section_props["title"]["description"]


def test_limit_errors_state_the_limit_and_what_was_received(tmp_path):
    """Blind shrinking cost five retries in the incident that prompted this:
    2039 -> 1904 -> 1664 -> 1614 -> 1477 -> 1172 accepted."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    long_summary = _call_tool(
        server,
        1,
        "agentacct_record_section",
        {"source": "codex", "section_id": "limits", "section_status": "started", "summary": "x" * 2039},
    )
    message = long_summary["error"]["message"]
    assert "1200" in message and "2039" in message

    long_file = _call_tool(
        server,
        2,
        "agentacct_record_section",
        {"source": "codex", "section_id": "limits", "section_status": "started", "files": ["a" * 300]},
    )
    file_message = long_file["error"]["message"]
    assert "files[0]" in file_message
    assert "240" in file_message and "300" in file_message

    too_many = _call_tool(
        server,
        3,
        "agentacct_record_section",
        {"source": "codex", "section_id": "limits", "section_status": "started", "files": [f"f{index}.py" for index in range(51)]},
    )
    count_message = too_many["error"]["message"]
    # Pinned exactly: "50" and "51" both appear in a message that says nothing
    # useful ("files: 50/51 problem"), so a substring pair does not prove the
    # limit and the received count were reported as such.
    assert count_message == "files must be <= 50 items (received 51)"


def test_metadata_budget_measures_real_utf8_bytes_for_cjk(tmp_path):
    """The old check json-encoded with ensure_ascii=True, so every CJK character
    cost 6 bytes: a summary of 1200 CJK chars plus a next_step of 200 measured
    8432 bytes and was rejected, i.e. about a third of the advertised budget."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    accepted = _call_tool(
        server,
        1,
        "agentacct_record_section",
        {
            "source": "codex",
            "section_id": "cjk",
            "section_status": "checkpoint",
            "summary": "记" * 1200,
            "next_step": "步" * 200,
        },
    )
    assert "error" not in accepted
    assert _tool_payload(accepted)["event"]["metadata"]["summary"] == "记" * 1200

    # Still bounded: genuinely oversized metadata fails, and the message names
    # the field to shrink instead of a parameter the caller never passed.
    oversized = _call_tool(
        server,
        2,
        "agentacct_record_section",
        {
            "source": "codex",
            "section_id": "cjk-big",
            "section_status": "checkpoint",
            "metadata": {"notes": "记" * 3000},
        },
    )
    message = oversized["error"]["message"]
    assert "8192" in message
    assert "notes" in message
    # 3000 CJK chars * 3 real UTF-8 bytes, plus the JSON envelope.
    assert "received 9013 bytes" in message


def test_files_absolute_under_project_dir_is_normalized_not_rejected(tmp_path):
    """Roughly 468 sessions hit "files[0] must be project-relative" while the
    harness instructed absolute paths and no schema published the rule."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    accepted = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {
                "source": "codex",
                "section_id": "paths",
                "section_status": "started",
                "project_dir": "/repo/agentacct",
                "files": ["/repo/agentacct/src/agentacct/mcp.py", "src/./a.py", "src//b.py"],
            },
        )
    )
    assert accepted["event"]["metadata"]["files"] == ["src/agentacct/mcp.py", "src/a.py", "src/b.py"]

    outside = _call_tool(
        server,
        2,
        "agentacct_record_section",
        {
            "source": "codex",
            "section_id": "paths",
            "section_status": "started",
            "project_dir": "/repo/agentacct",
            "files": ["/etc/passwd"],
        },
    )
    assert outside["error"]["code"] == -32602
    assert "project-relative" in outside["error"]["message"]

    # A sibling directory sharing the project_dir prefix is NOT inside it.
    sibling = _call_tool(
        server,
        3,
        "agentacct_record_section",
        {
            "source": "codex",
            "section_id": "paths",
            "section_status": "started",
            "project_dir": "/repo/agentacct",
            "files": ["/repo/agentacct-secrets/key.pem"],
        },
    )
    assert sibling["error"]["code"] == -32602

    # Lexical containment must not be fooled by a traversal back out.
    escape_via_root = _call_tool(
        server,
        4,
        "agentacct_record_section",
        {
            "source": "codex",
            "section_id": "paths",
            "section_status": "started",
            "project_dir": "/repo/agentacct",
            "files": ["/repo/agentacct/../../etc/passwd"],
        },
    )
    assert escape_via_root["error"]["code"] == -32602
    assert "escape" in escape_via_root["error"]["message"]

    # A redundant separator is the same POSIX path, and must not rebuild an
    # absolute-looking value out of the relativized remainder.
    redundant = _tool_payload(
        _call_tool(
            server,
            90,
            "agentacct_record_section",
            {
                "source": "codex",
                "section_id": "paths",
                "section_status": "started",
                "project_dir": "/repo/agentacct",
                "files": ["/repo/agentacct//etc/passwd"],
            },
        )
    )
    assert redundant["event"]["metadata"]["files"] == ["etc/passwd"]

    for index, bad in enumerate(
        (
            {"files": ["../outside.py"]},
            {"files": ["~/secrets.env"]},
            {"files": ["/repo/agentacct/src/a.py"]},  # absolute, but no project_dir on the call
            # "." is NOT in this list: it names the project root, is dropped
            # rather than rejected, and must never fail the call. Covered by
            # test_files_entry_naming_the_project_root_is_dropped_not_fatal.
        ),
        start=5,
    ):
        response = _call_tool(
            server,
            index,
            "agentacct_record_section",
            {"source": "codex", "section_id": "paths", "section_status": "started", **bad},
        )
        assert response["error"]["code"] == -32602, bad


def test_files_rule_is_published_in_every_schema_that_takes_files(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    tools = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    tool_by_name = {tool["name"]: tool for tool in tools["result"]["tools"]}

    for tool_name in ("agentacct_record_section", "agentacct_record_machine_check"):
        description = tool_by_name[tool_name]["inputSchema"]["properties"]["files"]["description"]
        assert "Project-relative" in description
        assert "forward slashes" in description
        assert "'..'" in description
        assert "project_dir" in description
    # A machine check's name carries no private cap: the shared metadata budget
    # is the only ceiling, and a cap here would reject names the CLI and HTTP
    # lanes accept. See test_machine_check_name_has_no_private_length_cap.
    assert "maxLength" not in tool_by_name["agentacct_record_machine_check"]["inputSchema"]["properties"]["name"]


def test_machine_check_normalizes_absolute_files_under_project_dir(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    payload = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_machine_check",
            {
                "source": "codex",
                "section_id": "s1",
                "result": "passed",
                "project_dir": "/repo/agentacct",
                "files": ["/repo/agentacct/tests/test_mcp.py"],
            },
        )
    )
    assert payload["event"]["metadata"]["files"] == ["tests/test_mcp.py"]


def test_mangled_tool_call_is_warned_about_never_repaired(tmp_path):
    """A mangled tool call absorbs parameters into a narrative value as literal
    text. Warn and stamp; never reject, never repair — repair would fabricate
    fields the agent never wrote."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    payload = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {
                "source": "codex",
                "section_id": "mangled",
                "section_status": "completed",
                "summary": "Fixed the validator.</summary>\n<files>src/agentacct/mcp.py</files>",
            },
        )
    )
    metadata = payload["event"]["metadata"]
    assert metadata["mangled_tool_call_suspected_fields"] == ["files"]
    # Never repaired: no files were invented from the narrative text.
    assert "files" not in metadata
    assert metadata["summary"].endswith("</files>")
    assert any("</files>" in warning and "did NOT" in warning for warning in payload["warnings"])

    # The same detector guards the other narrative write path.
    check = _tool_payload(
        _call_tool(
            server,
            2,
            "agentacct_record_machine_check",
            {
                "source": "codex",
                "section_id": "mangled",
                "result": "passed",
                "summary": "Suite green.</summary>\n<command>pytest -q</command>",
            },
        )
    )
    assert check["event"]["metadata"]["mangled_tool_call_suspected_fields"] == ["command"]
    assert "command" not in check["event"]["metadata"]
    assert any("</command>" in warning for warning in check["warnings"])


def test_mangle_detector_ignores_prose_that_merely_mentions_fields(tmp_path):
    """A bare-word detector fires 277 times on "source" and 192 on "files" in the
    real ledger, and an opening-tag detector has a real false positive on prose
    about MCP config. Closing tags only, and only for unsupplied properties."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    ordinary = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {
                "source": "codex",
                "section_id": "prose",
                "section_status": "completed",
                "summary": "Reviewed the files and the source list; <files> and <summary> tags are discussed in the MCP config docs.",
                "blocker": "Waiting on the next_step from review.",
            },
        )
    )
    assert "mangled_tool_call_suspected_fields" not in ordinary["event"]["metadata"]
    assert not any("mangled tool call" in warning for warning in ordinary["warnings"])

    # A closing tag for an argument the call DID supply is not evidence of a
    # mangled call — the parameter arrived where it belongs.
    supplied = _tool_payload(
        _call_tool(
            server,
            2,
            "agentacct_record_section",
            {
                "source": "codex",
                "section_id": "prose-2",
                "section_status": "completed",
                "summary": "Documented the </files> closing tag.",
                "files": ["docs/mcp.md"],
            },
        )
    )
    assert "mangled_tool_call_suspected_fields" not in supplied["event"]["metadata"]


def test_mangle_marker_is_server_authored_and_cannot_be_forged(tmp_path):
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    payload = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {
                "source": "codex",
                "section_id": "forge",
                "section_status": "started",
                "metadata": {"mangled_tool_call_suspected_fields": ["client_session_id"]},
            },
        )
    )
    metadata = payload["event"]["metadata"]
    assert "mangled_tool_call_suspected_fields" not in metadata
    assert "mangled_tool_call_suspected_fields" in metadata["reserved_context_keys_stripped"]


def test_machine_check_name_has_no_private_length_cap(tmp_path):
    """A 240-character cap turned a 241-4036 band that recorded fine on main
    into hard rejections. The band is real: an agent recording the full pytest
    invocation it ran routinely writes a ~300-character name. The only ceiling
    is the shared metadata budget, whose error now reports a field and a byte
    count instead of a bare "metadata must be <= 8192 bytes".

    4036, not the 8000 first claimed: see
    test_machine_check_name_band_is_the_measured_one for the boundary."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    long_name = ".venv/bin/pytest -q " + "tests/test_mcp.py::test_a " * 12
    assert 240 < len(long_name) <= 4036
    accepted = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_machine_check",
            {"source": "codex", "name": long_name, "result": "passed"},
        )
    )
    # Never truncated: the name feeds the check-identity hash.
    assert accepted["event"]["metadata"]["name"] == long_name

    # Still bounded, by the shared metadata budget: a name large enough to blow
    # the whole budget is rejected as a SIZE problem that reports what arrived,
    # not by a private cap of its own.
    oversized = _call_tool(
        server,
        2,
        "agentacct_record_machine_check",
        {"source": "codex", "name": "n" * 9000, "result": "passed"},
    )
    message = oversized["error"]["message"]
    assert message.startswith("metadata must be <= 8192 bytes when JSON encoded (received ")
    assert "name must be <=" not in message
    # Measured, not assumed: at this size the largest field is the `summary`
    # the server synthesizes as "<name>: <result>", so the blame is still
    # indirect in this extreme case. Recorded here so the next reader sees it.
    assert "largest field is summary" in message


def test_machine_check_name_band_is_the_measured_one(tmp_path):
    """The band this validator restored was documented as "241-8000". Binary
    searching a {source, name, result} call puts the real edge at 4036 accepted
    / 4037 rejected -- identical on the 0.5.2 release this branched from, so the
    number was wrong when it was written, not changed by the branch. It is half
    of 8192 because `name` lands in the budget twice: once as itself, once
    inside the "<name>: <result>" summary the server synthesizes."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    def accepted(msg_id, length):
        response = _call_tool(
            server,
            msg_id,
            "agentacct_record_machine_check",
            {"source": "codex", "name": "x" * length, "result": "passed"},
        )
        return "error" not in response, response

    ok, response = accepted(1, 4036)
    assert ok
    assert response["result"]["content"][0]["text"]

    ok, response = accepted(2, 4037)
    assert not ok
    # One character over the edge already blames the synthesized summary, so the
    # indirection the comment warns about is the normal case, not an extreme.
    assert "largest field is summary at 4060 bytes" in response["error"]["message"]


def test_metadata_budget_decision_is_identical_on_all_three_write_surfaces(tmp_path):
    """Measuring real UTF-8 bytes on MCP alone broke the symmetry the shared
    budget exists for: 2000 CJK characters are 6013 real bytes but 12013
    escaped, so MCP accepted a payload the HTTP lane rejected. Same payload,
    same verdict, whichever lane the agent writes through."""
    from agentacct.api import EventRecordRequest
    from agentacct.cli import _parse_metadata_json
    from agentacct.mcp import _validate_metadata_size

    def verdicts(payload):
        results = []
        for attempt in (
            lambda: _validate_metadata_size(payload),
            lambda: EventRecordRequest(source="codex", event_type="probe", metadata=payload),
            lambda: _parse_metadata_json(json.dumps(payload)),
        ):
            try:
                attempt()
                results.append("accepted")
            except Exception:
                results.append("rejected")
        return results

    under = {"notes": "记" * 2000}
    assert json_utf8_size(under) == 6013
    assert len(json.dumps(under, sort_keys=True).encode("utf-8")) == 12013
    assert verdicts(under) == ["accepted", "accepted", "accepted"]

    over = {"notes": "记" * 3000}
    assert json_utf8_size(over) == 9013
    assert verdicts(over) == ["rejected", "rejected", "rejected"]


def test_metadata_size_survives_a_lone_surrogate_on_every_surface(tmp_path):
    """json.loads accepts a lone surrogate but it has no UTF-8 form, so a
    real-bytes measure raises UnicodeEncodeError mid-validation and fails the
    call with a crash instead of a verdict."""
    from agentacct.api import EventRecordRequest
    from agentacct.mcp import _validate_metadata_size

    lone_surrogate = json.loads('"\\ud800"')
    assert _validate_metadata_size({"notes": lone_surrogate}) is None
    assert EventRecordRequest(source="codex", event_type="probe", metadata={"notes": lone_surrogate})
    assert json_utf8_size({"notes": lone_surrogate}) > 0

    server = SentinelMCPServer(store_dir=tmp_path / "state")
    recorded = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {"source": "codex", "section_id": "surrogate", "section_status": "started", "summary": lone_surrogate},
        )
    )
    assert recorded["event"]["metadata"]["summary"] == lone_surrogate

    # Oversized-and-unencodable still fails as SIZE, never as an encoding crash.
    oversized = _call_tool(
        server,
        2,
        "agentacct_record_section",
        {
            "source": "codex",
            "section_id": "surrogate",
            "section_status": "started",
            "metadata": {"notes": lone_surrogate * 3000},
        },
    )
    assert "bytes" in oversized["error"]["message"]


def test_files_entry_naming_the_project_root_is_dropped_not_fatal(tmp_path):
    """A whole-repo section with files=["."] recorded fine on main. Killing the
    whole record to reject one cosmetic path entry is exactly the behaviour
    this validator exists to remove."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    for msg_id, entry in enumerate((".", "./"), start=1):
        payload = _tool_payload(
            _call_tool(
                server,
                msg_id,
                "agentacct_record_section",
                {"source": "codex", "section_id": "whole-repo", "section_status": "started", "files": [entry]},
            )
        )
        # Accepted, and no row is stored for a value that names no file.
        assert "error" not in payload, entry
        assert payload["event"]["metadata"].get("files", []) == [], entry

    mixed = _tool_payload(
        _call_tool(
            server,
            3,
            "agentacct_record_section",
            {"source": "codex", "section_id": "whole-repo", "section_status": "started", "files": [".", "src/a.py"]},
        )
    )
    assert mixed["event"]["metadata"]["files"] == ["src/a.py"]

    # An absolute path equal to the declared project root says the same thing,
    # with or without the trailing slash: one must not be fatal while the other
    # is cosmetic.
    for msg_id, entry in enumerate(("/repo/agentacct/", "/repo/agentacct"), start=4):
        at_root = _tool_payload(
            _call_tool(
                server,
                msg_id,
                "agentacct_record_machine_check",
                {
                    "source": "codex",
                    "result": "passed",
                    "project_dir": "/repo/agentacct",
                    "files": [entry],
                },
            )
        )
        assert "error" not in at_root, entry
        assert at_root["event"]["metadata"].get("files", []) == [], entry


def test_files_backslash_is_folded_for_validation_but_never_for_storage(tmp_path):
    """On macOS/Linux `weird\\name.py` is a legal filename. main folded
    backslashes for VALIDATION and stored the original; storing the folded form
    silently merges that file with a genuinely different `weird/name.py`."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    stored = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {
                "source": "codex",
                "section_id": "backslash",
                "section_status": "started",
                "files": ["weird\\name.py", "weird/name.py"],
            },
        )
    )
    # Two distinct real paths must stay two distinct rows.
    assert stored["event"]["metadata"]["files"] == ["weird\\name.py", "weird/name.py"]

    # Folding still happens for validation, so a Windows-style traversal is
    # caught rather than waved through as one opaque segment.
    escaped = _call_tool(
        server,
        2,
        "agentacct_record_section",
        {
            "source": "codex",
            "section_id": "backslash",
            "section_status": "started",
            "files": ["src\\..\\..\\etc\\passwd"],
        },
    )
    assert escaped["error"]["code"] == -32602
    assert "escape" in escaped["error"]["message"]


def test_relativized_remainder_is_revalidated_against_the_published_rule(tmp_path):
    """The remainder was only checked for '..', so project_dir="/repo" with
    files=["/repo/~/x"] stored "~/x" -- a value the schema description this
    server publishes says is impossible."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    for msg_id, entry in enumerate(("/repo/~/x.py", "/repo/C:/x.py"), start=1):
        response = _call_tool(
            server,
            msg_id,
            "agentacct_record_section",
            {
                "source": "codex",
                "section_id": "remainder",
                "section_status": "started",
                "project_dir": "/repo",
                "files": [entry],
            },
        )
        assert response["error"]["code"] == -32602, entry
        assert "project-relative" in response["error"]["message"], entry


def test_windows_drive_letter_is_absolute_for_validation(tmp_path):
    """Backslashes are folded before the absolute check, so PurePosixPath alone
    reads "C:/repo/a.py" as a relative path and stores it as one.

    Absolute for validation means it is relativized against a drive-letter root
    and still escape-checked -- NOT that it is rejected without one. See
    test_windows_absolute_path_is_never_fatal for why."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    # Not filed as a relative path, and not rejected either: stored as sent.
    stored = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {"source": "codex", "section_id": "drive", "section_status": "started", "files": ["C:\\repo\\a.py"]},
        )
    )
    assert stored["event"]["metadata"]["files"] == ["C:/repo/a.py"]

    # With the drive-letter root declared on the call it relativizes normally.
    accepted = _tool_payload(
        _call_tool(
            server,
            2,
            "agentacct_record_section",
            {
                "source": "codex",
                "section_id": "drive",
                "section_status": "started",
                "project_dir": "C:\\repo",
                "files": ["C:\\repo\\src\\a.py"],
            },
        )
    )
    assert accepted["event"]["metadata"]["files"] == ["src/a.py"]


def test_windows_absolute_path_is_never_fatal(tmp_path):
    """Recognizing drive letters as absolute was right; pairing it with a
    rejection was not. Measured on the 0.5.2 release this branched from, both
    shapes below RECORD, storing the backslashes they arrived with; on the first
    cut of this branch both killed the whole call -- over a path-style detail,
    which is the anti-pattern the validator exists to remove. It is the one
    absolute form with no POSIX reading, so keeping it invents nothing. The
    stored value differs from 0.5.2 only in separator: on a Windows path a
    backslash IS a separator, so it is normalized like any other."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    def files_for(args):
        payload = _tool_payload(
            _call_tool(
                server,
                files_for.msg_id,
                "agentacct_record_section",
                {"source": "codex", "section_id": "win", "section_status": "started", **args},
            )
        )
        files_for.msg_id += 1
        return payload["event"]["metadata"]["files"]

    files_for.msg_id = 1

    # The shape agent harnesses actually hand out on Windows: no project_dir.
    assert files_for({"files": ["C:\\repo\\src\\a.py"]}) == ["C:/repo/src/a.py"]
    # And with a project_dir that cannot possibly contain it.
    assert files_for({"files": ["C:\\repo\\src\\a.py"], "project_dir": "/other"}) == ["C:/repo/src/a.py"]

    # A POSIX absolute path with no usable project_dir stays a rejection: it is
    # not a regression, it is what the release before this branch also did.
    posix = _call_tool(
        server,
        90,
        "agentacct_record_section",
        {"source": "codex", "section_id": "win", "section_status": "started", "files": ["/repo/src/a.py"]},
    )
    assert posix["error"]["code"] == -32602

    # Non-fatal is not unchecked: the escape guard still rejects.
    escaping = _call_tool(
        server,
        91,
        "agentacct_record_section",
        {"source": "codex", "section_id": "win", "section_status": "started", "files": ["C:\\repo\\..\\..\\secrets"]},
    )
    assert escaping["error"]["code"] == -32602
    assert "escape" in escaping["error"]["message"]


def test_backslash_is_preserved_on_the_relativized_branch_too(tmp_path):
    """On POSIX "weird\\name.py" is a legal filename and a genuinely different
    file from "weird/name.py". The relative branch kept it; the relativized
    branch folded it, silently merging the two. Same rule on both branches."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    relative = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {"source": "codex", "section_id": "bs", "section_status": "started", "files": ["weird\\name.py"]},
        )
    )
    assert relative["event"]["metadata"]["files"] == ["weird\\name.py"]

    relativized = _tool_payload(
        _call_tool(
            server,
            2,
            "agentacct_record_section",
            {
                "source": "codex",
                "section_id": "bs",
                "section_status": "started",
                "project_dir": "/repo",
                "files": ["/repo/weird\\name.py"],
            },
        )
    )
    # Same file, same stored value, whichever way the caller spelled the path.
    assert relativized["event"]["metadata"]["files"] == ["weird\\name.py"]

    # The Windows exception, where a backslash really is a separator: folding is
    # correct there, so this must NOT fragment against a forward-slash "src/a.py".
    windows = _tool_payload(
        _call_tool(
            server,
            3,
            "agentacct_record_section",
            {
                "source": "codex",
                "section_id": "bs",
                "section_status": "started",
                "project_dir": "C:\\repo",
                "files": ["C:\\repo\\src\\a.py"],
            },
        )
    )
    assert windows["event"]["metadata"]["files"] == ["src/a.py"]


def test_tilde_project_dir_never_anchors_a_relativization(tmp_path):
    """"~" is a shell shortcut, not a root: two machines expand it to different
    directories, so containment under it is not provable and must not be
    claimed."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    response = _call_tool(
        server,
        1,
        "agentacct_record_section",
        {
            "source": "codex",
            "section_id": "tilde",
            "section_status": "started",
            "project_dir": "~/repo",
            "files": ["~/repo/src/a.py"],
        },
    )
    assert response["error"]["code"] == -32602
    assert "project-relative" in response["error"]["message"]

    # A relative project_dir is equally unusable as an anchor.
    relative_root = _call_tool(
        server,
        2,
        "agentacct_record_section",
        {
            "source": "codex",
            "section_id": "tilde",
            "section_status": "started",
            "project_dir": "repo",
            "files": ["/repo/src/a.py"],
        },
    )
    assert relative_root["error"]["code"] == -32602


def test_mangle_detector_does_not_fire_on_a_closing_title_tag(tmp_path):
    """Adding `title` as a record_section property widened the detector onto
    `</title>`, the most common closing tag in HTML prose. The 1-true-positive
    /0-false-positive calibration was measured WITHOUT `title` in the property
    set, so leaving it in would quote a rate for a detector nobody measured."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")

    ordinary = _tool_payload(
        _call_tool(
            server,
            1,
            "agentacct_record_section",
            {
                "source": "codex",
                "section_id": "html",
                "section_status": "completed",
                "summary": "The page head has <title>Report</title> in it.",
            },
        )
    )
    assert "mangled_tool_call_suspected_fields" not in ordinary["event"]["metadata"]
    assert not [warning for warning in ordinary["warnings"] if "mangled" in warning]

    # The detector is still armed for every other unsupplied property.
    mangled = _tool_payload(
        _call_tool(
            server,
            2,
            "agentacct_record_section",
            {
                "source": "codex",
                "section_id": "html",
                "section_status": "completed",
                "summary": "The page head has <title>Report</title> in it.</next_step>",
            },
        )
    )
    assert mangled["event"]["metadata"]["mangled_tool_call_suspected_fields"] == ["next_step"]


def test_mangle_detector_ineligible_set_is_calibrated_not_hand_picked(tmp_path):
    """Excluding `title` alone was inconsistent: it removed one HTML/SVG element
    name on the grounds that agents write about markup, and left `summary` and
    `metadata` -- element names by the same reasoning -- armed to stamp a
    server-authored accusation into the stored record of ordinary prose.

    The ineligible set is now every property name in those two vocabularies.
    Measured READ-ONLY against the real 6261-event ledger, that costs nothing:
    across the 3535 section+evidence events `</title>`, `</metadata>` and
    `</source>` never occur, and `</summary>` occurs only inside the two genuine
    mangled calls, which both supplied `summary` so it could never have fired."""
    from agentacct.mcp import MANGLE_DETECTOR_INELIGIBLE_PROPERTIES

    server = SentinelMCPServer(store_dir=tmp_path / "state")

    def suspected(msg_id, summary):
        payload = _tool_payload(
            _call_tool(
                server,
                msg_id,
                "agentacct_record_section",
                {"source": "codex", "section_id": "svg", "section_status": "completed", "summary": summary},
            )
        )
        metadata = payload["event"]["metadata"]
        # The stamp in the stored record and the warning to the caller always
        # agree; other warnings (join hints) are not this test's business.
        mangle_warnings = [warning for warning in payload["warnings"] if "mangled tool call" in warning]
        assert bool(mangle_warnings) == ("mangled_tool_call_suspected_fields" in metadata)
        return metadata.get("mangled_tool_call_suspected_fields")

    # Ordinary prose about markup: accused before, silent now. Each of these is
    # a real thing an agent writes while doing web work.
    assert suspected(1, "Stripped the <metadata> block from the exported SVG.</metadata>") is None
    assert suspected(2, "The <details> disclosure needs its </summary> closing tag.") is None
    assert suspected(3, "Removed the stale </source> tag from the <picture> element.") is None

    # Still armed on every name that is not a markup element: these are the
    # names the two real mangled calls in the ledger actually absorbed.
    assert suspected(4, "Done.</next_step>") == ["next_step"]
    assert suspected(5, "Done.</files>") == ["files"]
    assert suspected(6, "Done.</project_dir>") == ["project_dir"]

    # Pinned last, so a regression shows up as the behaviour above rather than
    # as a bare constant mismatch.
    assert MANGLE_DETECTOR_INELIGIBLE_PROPERTIES == {"title", "summary", "metadata", "source"}
