"""Phase 2.7 — client-log-evidenced context linking.

Pins the core invariants:

- links are extracted ONLY from the five creation tools' outputs (allowlist
  by tool name, pairing strictly by call id, strict single-created-event
  shape); read-tool outputs and free-text event-id echoes never donate;
- markers are derived at read time and never trusted from stored metadata;
- conflicts veto BOTH directions, multi-session evidence refuses, caps and
  skips are counted;
- every link flows through the one unified matcher: tier ``log_evidenced``
  caps at high, exact stays unreachable, and hook/explicit provenance is
  never downgraded by corroboration.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentacct.api import create_local_api_app
from agentacct.cli import app
from agentacct.client_usage import ClientUsageEvent, discover_claude_code_usage, discover_codex_usage
from agentacct.context_bridge import build_usage_context_bridge
from agentacct.join_rules import (
    TIER_CONFIDENCE,
    TIER_STRATEGY_PREFIX,
    decide_attribution,
    id_key_tier,
    pair_confidence,
    pair_match,
)
from agentacct.log_evidence import (
    CANONICAL_EVENT_IDENTITY_CONFLICT_REASON,
    CODEX_REPLAY_CLOCK_SKEW_SECONDS,
    CODEX_REPLAY_REJECTION_REASON,
    CODEX_REPLAY_TIME_UNPROVEN_REASON,
    EVIDENCED_EVENT_IDS_CAP,
    SOURCE_NAMESPACE_CONFLICT_REASON,
    apply_log_evidence_to_snapshots,
    build_log_evidence_index,
    extract_created_event_id,
)
from agentacct.service import SentinelService
from agentacct.work_ledger import build_work_ledger

SESSION = "019f2303-6ae1-7000-8000-000000000001"
OTHER_SESSION = "019f272c-bb18-7000-8000-000000000002"


# ---------------------------------------------------------------------------
# Codex rollout fixtures (wire shapes verified against Codex 0.142.5 rollouts)
# ---------------------------------------------------------------------------


def _creation_payload(event_id: str, **extra) -> dict:
    return {"event": {"event_id": event_id, "event_type": "section_started"}, **extra}


def _mcp_text_blocks(payload: dict) -> str:
    return json.dumps([{"type": "text", "text": json.dumps(payload, indent=2, sort_keys=True)}])


def _wrapped_output(payload: dict) -> str:
    return "Wall time: 2.3735 seconds\nOutput:\n" + _mcp_text_blocks(payload)


def _function_call(name: str, call_id: str, *, namespace: str | None = "mcp__agent_sentinel", omit_namespace: bool = False) -> dict:
    payload = {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "name": name,
        "arguments": "{}",
        "call_id": call_id,
    }
    if not omit_namespace and namespace is not None:
        payload["namespace"] = namespace
    return {"type": "response_item", "payload": payload}


def _function_output(call_id: str, output) -> dict:
    return {"type": "response_item", "payload": {"type": "function_call_output", "call_id": call_id, "output": output}}


def _mcp_ok_result(payload: dict) -> dict:
    return {"Ok": {"content": [{"type": "text", "text": json.dumps(payload, indent=2, sort_keys=True)}]}}


def _mcp_tool_call_end(server, tool: str, call_id: str, result, *, omit_server: bool = False) -> dict:
    """Codex 0.144.0-alpha.4 wire shape: MCP calls exist ONLY as event_msg/
    mcp_tool_call_end lines (the duplicate function_call records are gone).
    Fully synthetic redacted fixture modeled on the documented shape."""

    invocation: dict = {"tool": tool, "arguments": {}}
    if not omit_server:
        invocation["server"] = server
    return {
        "timestamp": "2026-07-05T10:00:01.000Z",
        "type": "event_msg",
        "payload": {
            "type": "mcp_tool_call_end",
            "call_id": call_id,
            "invocation": invocation,
            "duration": {"secs": 2, "nanos": 0},
            "result": result,
        },
    }


def _token_usage_line(input_tokens: int = 900, output_tokens: int = 80) -> dict:
    return {
        "type": "event_msg",
        "payload": {
            "info": {
                "total_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": 100,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": 0,
                }
            },
            "model": "gpt-5.5",
        },
    }


def _make_codex_home(
    root: Path,
    rollout_lines: list[dict],
    *,
    thread_id: str = SESSION,
    tokens_used: int = 980,
    include_usage_line: bool = True,
) -> Path:
    codex_home = root / "codex-home"
    sessions_dir = codex_home / "sessions" / "2026" / "07" / "05"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    rollout = sessions_dir / f"rollout-2026-07-05T10-00-00-{thread_id}.jsonl"
    lines = [json.dumps({"type": "session_meta", "payload": {"id": thread_id}})]
    if include_usage_line:
        lines.append(json.dumps(_token_usage_line()))
    lines.extend(json.dumps(line) for line in rollout_lines)
    rollout.write_text("\n".join(lines) + "\n", encoding="utf-8")
    con = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        con.execute(
            """
            create table threads (
                id text primary key,
                rollout_path text not null,
                created_at integer not null,
                updated_at integer not null,
                cwd text not null,
                title text not null,
                tokens_used integer not null,
                model text,
                cli_version text
            )
            """
        )
        con.execute(
            "insert into threads values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (thread_id, str(rollout), 100, 300, "/work/project", "t", tokens_used, "gpt-5.5", "0.test"),
        )
        con.execute("create table thread_spawn_edges (parent_thread_id text, child_thread_id text, status text)")
        con.commit()
    finally:
        con.close()
    return codex_home


def _single_codex_event(codex_home: Path) -> ClientUsageEvent:
    events = discover_codex_usage(codex_home=codex_home, limit_sessions=10)
    assert len(events) == 1
    return events[0]


# ---------------------------------------------------------------------------
# Importer (codex)
# ---------------------------------------------------------------------------


def test_codex_all_five_creation_tools_extract_and_pair_by_call_id(tmp_path) -> None:
    tools_and_ids = [
        ("sentinel_record_event", "evt_aaa001aaa001"),
        ("sentinel_attach_client_context", "evt_bbb002bbb002"),
        ("sentinel_record_section", "evt_ccc003ccc003"),
        ("sentinel_record_agent_usage_debug", "evt_ddd004ddd004"),
        ("sentinel_record_machine_check", "evt_eee005eee005"),
    ]
    lines: list[dict] = []
    for index, (tool, event_id) in enumerate(tools_and_ids):
        call_id = f"call_{index}"
        lines.append(_function_call(tool, call_id))
        payload = _creation_payload(event_id)
        if tool == "sentinel_attach_client_context":
            payload["context_attached"] = True
            # bare JSON output form (no human wrapper)
            lines.append(_function_output(call_id, _mcp_text_blocks(payload)))
        else:
            lines.append(_function_output(call_id, _wrapped_output(payload)))
    # Decoy: output keyed by a response-item `id` (fc_call_0), never by
    # call_id — must NOT pair, its event id must not leak in.
    lines.append(_function_output("fc_call_0", _wrapped_output(_creation_payload("evt_fff006fff006"))))

    event = _single_codex_event(_make_codex_home(tmp_path, lines))

    assert list(event.evidenced_event_ids) == [event_id for _tool, event_id in tools_and_ids]
    assert event.evidenced_event_id_total == 5
    assert event.evidenced_outputs_skipped == 0
    payload = event.to_sentinel_event()
    assert payload["metadata"]["evidenced_event_ids"] == [event_id for _tool, event_id in tools_and_ids]
    assert payload["metadata"]["evidenced_event_id_total"] == 5
    assert "evidenced_outputs_skipped" not in payload["metadata"]


def test_codex_read_tool_outputs_never_donate(tmp_path) -> None:
    lines = [
        _function_call("sentinel_list_events", "call_list"),
        _function_output(
            "call_list",
            _wrapped_output({"events": [{"event_id": "evt_deadbeef0001"}, {"event_id": "evt_deadbeef0002"}]}),
        ),
        # A read tool whose text happens to BE a creation-shaped payload:
        # the allowlist means it is never even examined.
        _function_call("sentinel_get_event_summary", "call_summary"),
        _function_output("call_summary", _wrapped_output(_creation_payload("evt_facefeed0003"))),
    ]

    event = _single_codex_event(_make_codex_home(tmp_path, lines))

    assert event.evidenced_event_ids == ()
    assert event.evidenced_event_id_total == 0
    # Read tools are not creation calls: not extracted AND not counted as skips.
    assert event.evidenced_outputs_skipped == 0
    payload = event.to_sentinel_event()
    assert "evidenced_event_ids" not in payload["metadata"]
    assert "evidenced_event_id_total" not in payload["metadata"]


def test_codex_malformed_creation_outputs_skipped_and_counted(tmp_path) -> None:
    cases = [
        _wrapped_output({"events": [{"event_id": "evt_deadbeef0004"}]}),  # read-shaped under a creation name
        _wrapped_output({"event": {"event_id": "evt_deadbeef0005"}, "events": [1]}),  # belt-and-braces reject
        "tool call error: tool call failed for `agent-chronicle/sentinel_record_section` (-32602)",
        "Wall time: 1.0 seconds\nOutput:\n[{\"type\": \"text\", \"text\": \"{\\\"event\\\": {\\\"event_id\\\": \\\"evt_tr",  # truncated
        _wrapped_output({}),  # outcome-only machine_check
        _wrapped_output({"outcome": {"status": "pass"}}),
        42,  # non-str/non-dict output
        _wrapped_output({"event": {"event_id": "EVT_UPPER"}}),  # malformed id
    ]
    lines: list[dict] = []
    for index, output in enumerate(cases):
        call_id = f"call_{index}"
        lines.append(_function_call("sentinel_record_section", call_id))
        lines.append(_function_output(call_id, output))

    event = _single_codex_event(_make_codex_home(tmp_path, lines))

    assert event.evidenced_event_ids == ()
    assert event.evidenced_event_id_total == 0
    assert event.evidenced_outputs_skipped == len(cases)
    payload = event.to_sentinel_event()
    assert payload["metadata"]["evidenced_outputs_skipped"] == len(cases)
    assert "evidenced_event_ids" not in payload["metadata"]


def test_codex_namespace_gating(tmp_path) -> None:
    accepted = [
        ("mcp__agent_sentinel", "evt_a1a1a1a1a1a1"),
        ("mcp__agent-chronicle", "evt_b2b2b2b2b2b2"),
        (None, "evt_c3c3c3c3c3c3"),  # namespace absent
    ]
    rejected = [
        ("mcp__other_server", "evt_d4d4d4d4d4d4"),
        ("mcp__not_agent_sentinel_fake", "evt_e5e5e5e5e5e5"),
    ]
    lines: list[dict] = []
    for index, (namespace, event_id) in enumerate(accepted + rejected):
        call_id = f"call_{index}"
        lines.append(
            _function_call(
                "sentinel_record_section",
                call_id,
                namespace=namespace,
                omit_namespace=namespace is None,
            )
        )
        lines.append(_function_output(call_id, _wrapped_output(_creation_payload(event_id))))

    event = _single_codex_event(_make_codex_home(tmp_path, lines))

    assert list(event.evidenced_event_ids) == [event_id for _ns, event_id in accepted]
    # Namespace-rejected creation-name calls are skipped-but-counted so a
    # custom registration name is visible in counters, never guessed.
    assert event.evidenced_outputs_skipped == len(rejected)


def test_codex_cap_dedup_and_rollout_order(tmp_path) -> None:
    lines: list[dict] = []
    total = EVIDENCED_EVENT_IDS_CAP + 1
    ids = [f"evt_{index:012x}" for index in range(total)]
    for index, event_id in enumerate(ids):
        call_id = f"call_{index}"
        lines.append(_function_call("sentinel_record_section", call_id))
        lines.append(_function_output(call_id, _wrapped_output(_creation_payload(event_id))))
    # Repeat of the FIRST id: deduped, order preserved, not double counted.
    lines.append(_function_call("sentinel_record_section", "call_repeat"))
    lines.append(_function_output("call_repeat", _wrapped_output(_creation_payload(ids[0]))))

    event = _single_codex_event(_make_codex_home(tmp_path, lines))

    assert len(event.evidenced_event_ids) == EVIDENCED_EVENT_IDS_CAP
    assert list(event.evidenced_event_ids) == ids[:EVIDENCED_EVENT_IDS_CAP]
    assert event.evidenced_event_id_total == total  # honest overflow
    assert event.evidenced_outputs_skipped == 0


def test_no_evidence_event_metadata_is_byte_identical_to_pre_change_shape(tmp_path) -> None:
    event = _single_codex_event(_make_codex_home(tmp_path, []))

    payload = event.to_sentinel_event()
    for key in ("evidenced_event_ids", "evidenced_event_id_total", "evidenced_outputs_skipped"):
        assert key not in payload["metadata"]
    assert "evidenced" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# Importer (codex 0.144+ mcp_tool_call_end channel)
# ---------------------------------------------------------------------------


def test_codex_144_mcp_tool_call_end_only_rollout_pairs_all_five_creation_tools(tmp_path) -> None:
    """Codex 0.144.0-alpha.4 dropped the duplicate function_call records for
    MCP tools; the mcp_tool_call_end event_msg is now the ONLY channel."""

    tools_and_ids = [
        ("sentinel_record_event", "evt_aaa001aaa001"),
        ("sentinel_attach_client_context", "evt_bbb002bbb002"),
        ("sentinel_record_section", "evt_ccc003ccc003"),
        ("sentinel_record_agent_usage_debug", "evt_ddd004ddd004"),
        ("sentinel_record_machine_check", "evt_eee005eee005"),
    ]
    lines = [
        _mcp_tool_call_end("agent-sentinel", tool, f"call_{index}", _mcp_ok_result(_creation_payload(event_id)))
        for index, (tool, event_id) in enumerate(tools_and_ids)
    ]

    event = _single_codex_event(_make_codex_home(tmp_path, lines))

    assert list(event.evidenced_event_ids) == [event_id for _tool, event_id in tools_and_ids]
    assert event.evidenced_event_id_total == 5
    assert event.evidenced_outputs_skipped == 0


def test_codex_144_read_tool_mcp_end_never_donates_and_is_not_counted(tmp_path) -> None:
    lines = [
        _mcp_tool_call_end(
            "agent-sentinel",
            "sentinel_get_event_summary",
            "call_summary",
            # A read tool whose text happens to BE a creation-shaped payload:
            # the allowlist means it is never even examined.
            _mcp_ok_result(_creation_payload("evt_facefeed0003")),
        ),
        _mcp_tool_call_end(
            "agent-sentinel",
            "sentinel_list_events",
            "call_list",
            _mcp_ok_result({"events": [{"event_id": "evt_deadbeef0001"}]}),
        ),
    ]

    event = _single_codex_event(_make_codex_home(tmp_path, lines))

    assert event.evidenced_event_ids == ()
    assert event.evidenced_event_id_total == 0
    assert event.evidenced_outputs_skipped == 0


def test_codex_144_foreign_or_absent_server_rejected_and_skip_counted(tmp_path) -> None:
    """The channel-drift alarm covers this channel: a creation-tool
    mcp_tool_call_end under a foreign server — or with the server field
    missing entirely (the field is always present in this shape; absence is
    drift) — is skipped-but-counted, never guessed."""

    lines = [
        _mcp_tool_call_end("other-server", "sentinel_record_section", "call_0", _mcp_ok_result(_creation_payload("evt_d4d4d4d4d4d4"))),
        _mcp_tool_call_end("mcp__not_agent_sentinel_fake", "sentinel_record_section", "call_1", _mcp_ok_result(_creation_payload("evt_e5e5e5e5e5e5"))),
        _mcp_tool_call_end(None, "sentinel_record_section", "call_2", _mcp_ok_result(_creation_payload("evt_f6f6f6f6f6f6")), omit_server=True),
        # Accepted namespaces: exact match modulo underscore normalization
        # and the optional mcp__ prefix, same rule as the function_call path.
        _mcp_tool_call_end("mcp__agentacct", "sentinel_record_section", "call_3", _mcp_ok_result(_creation_payload("evt_a7a7a7a7a7a7"))),
    ]

    event = _single_codex_event(_make_codex_home(tmp_path, lines))

    assert list(event.evidenced_event_ids) == ["evt_a7a7a7a7a7a7"]
    assert event.evidenced_outputs_skipped == 3
    payload = event.to_sentinel_event()
    assert "evt_d4d4d4d4d4d4" not in json.dumps(payload)


def test_codex_144_err_result_is_skip_counted(tmp_path) -> None:
    lines = [
        _mcp_tool_call_end("agent-sentinel", "sentinel_record_section", "call_ok", _mcp_ok_result(_creation_payload("evt_0c0c0c0c0c0c"))),
        _mcp_tool_call_end("agent-sentinel", "sentinel_record_section", "call_err", {"Err": "tool call failed (-32602)"}),
        _mcp_tool_call_end("agent-sentinel", "sentinel_record_section", "call_other", "not-a-dict-result"),
    ]

    event = _single_codex_event(_make_codex_home(tmp_path, lines))

    assert list(event.evidenced_event_ids) == ["evt_0c0c0c0c0c0c"]
    assert event.evidenced_event_id_total == 1
    assert event.evidenced_outputs_skipped == 2


def test_codex_144_malformed_ok_content_is_skip_counted(tmp_path) -> None:
    cases = [
        {"Ok": {"content": [{"type": "text", "text": "{\"event\": {\"event_id\": \"evt_tr"}]}},  # truncated JSON
        {"Ok": {"content": [{"type": "text", "text": json.dumps({"events": [{"event_id": "evt_deadbeef0004"}]})}]}},  # read-shaped
        {"Ok": {"content": []}},  # no text block
        {"Ok": {"content": "not-a-list"}},
        {"Ok": {}},  # content absent
        {"Ok": {"content": [{"type": "text", "text": json.dumps({"event": {"event_id": "EVT_UPPER"}})}]}},  # malformed id
    ]
    lines = [
        _mcp_tool_call_end("agent-sentinel", "sentinel_record_section", f"call_{index}", result)
        for index, result in enumerate(cases)
    ]

    event = _single_codex_event(_make_codex_home(tmp_path, lines))

    assert event.evidenced_event_ids == ()
    assert event.evidenced_event_id_total == 0
    assert event.evidenced_outputs_skipped == len(cases)


def test_codex_dual_shape_rollout_donates_each_call_id_exactly_once(tmp_path) -> None:
    """Pre-0.144 rollouts carry every MCP call under BOTH shapes with an
    identical call_id (verified 10/10 overlap): each call must donate — or
    skip-count — exactly once, regardless of file order."""

    lines = [
        # Accepted call, function_call/output pair FIRST, mcp_end after.
        _function_call("sentinel_record_section", "call_dup"),
        _function_output("call_dup", _wrapped_output(_creation_payload("evt_d0d0d0d0d0d0"))),
        _mcp_tool_call_end("agent-sentinel", "sentinel_record_section", "call_dup", _mcp_ok_result(_creation_payload("evt_d0d0d0d0d0d0"))),
        # Accepted call, mcp_end FIRST (file order must not matter).
        _mcp_tool_call_end("agent-sentinel", "sentinel_record_section", "call_rev", _mcp_ok_result(_creation_payload("evt_e1e1e1e1e1e1"))),
        _function_call("sentinel_record_section", "call_rev"),
        _function_output("call_rev", _wrapped_output(_creation_payload("evt_e1e1e1e1e1e1"))),
        # Rejected (foreign server/namespace) under both shapes: the skip is
        # counted ONCE — the pre-fix expectation for this rollout was
        # evidenced_outputs_skipped == 1 and it must not double.
        _function_call("sentinel_record_section", "call_rej", namespace="mcp__other_server"),
        _function_output("call_rej", _wrapped_output(_creation_payload("evt_f2f2f2f2f2f2"))),
        _mcp_tool_call_end("other-server", "sentinel_record_section", "call_rej", _mcp_ok_result(_creation_payload("evt_f2f2f2f2f2f2"))),
    ]

    event = _single_codex_event(_make_codex_home(tmp_path, lines))

    assert list(event.evidenced_event_ids) == ["evt_d0d0d0d0d0d0", "evt_e1e1e1e1e1e1"]
    assert event.evidenced_event_id_total == 2
    assert event.evidenced_outputs_skipped == 1


def test_codex_err_end_never_blocks_valid_fco_donation_for_same_call(tmp_path) -> None:
    """Divergence corner: only a DONATING channel consumes the call_id. An
    accepted-but-unwrap-failing (Err) mcp_tool_call_end is skip-counted, but
    the paired function_call_output's valid payload still donates."""

    lines = [
        _function_call("sentinel_record_section", "call_div"),
        _mcp_tool_call_end("agent-sentinel", "sentinel_record_section", "call_div", {"Err": "tool call failed"}),
        _function_output("call_div", _wrapped_output(_creation_payload("evt_abcdef123456"))),
    ]

    event = _single_codex_event(_make_codex_home(tmp_path, lines))

    assert list(event.evidenced_event_ids) == ["evt_abcdef123456"]
    assert event.evidenced_event_id_total == 1
    assert event.evidenced_outputs_skipped == 1  # the failed end stays counted


def test_codex_rejected_end_preceding_accepted_pair_never_blocks_donation(tmp_path) -> None:
    """A foreign-server (rejected) mcp_tool_call_end arriving FIRST must not
    consume the call_id: the later accepted fc/fco pair still donates."""

    lines = [
        _mcp_tool_call_end("other-server", "sentinel_record_section", "call_hostile", _mcp_ok_result(_creation_payload("evt_badbadbadbad"))),
        _function_call("sentinel_record_section", "call_hostile"),
        _function_output("call_hostile", _wrapped_output(_creation_payload("evt_00ddcc112233"))),
    ]

    event = _single_codex_event(_make_codex_home(tmp_path, lines))

    assert list(event.evidenced_event_ids) == ["evt_00ddcc112233"]
    assert "evt_badbadbadbad" not in event.evidenced_event_ids
    assert event.evidenced_event_id_total == 1
    assert event.evidenced_outputs_skipped == 1  # the rejected end stays counted


def test_codex_144_absent_or_malformed_invocation_block_is_skip_counted(tmp_path) -> None:
    """Channel-drift alarm one level down: an mcp_tool_call_end whose
    invocation block is missing or non-dict cannot be classified at all —
    that state is itself drift for this wire shape and must be counted, not
    silently invisible."""

    missing = _mcp_tool_call_end("agent-sentinel", "sentinel_record_section", "call_m0", _mcp_ok_result(_creation_payload("evt_0f0f0f0f0f0f")))
    del missing["payload"]["invocation"]
    malformed = _mcp_tool_call_end("agent-sentinel", "sentinel_record_section", "call_m1", _mcp_ok_result(_creation_payload("evt_1f1f1f1f1f1f")))
    malformed["payload"]["invocation"] = "not-a-dict"

    event = _single_codex_event(_make_codex_home(tmp_path, [missing, malformed]))

    assert event.evidenced_event_ids == ()
    assert event.evidenced_event_id_total == 0
    assert event.evidenced_outputs_skipped == 2


def test_codex_model_label_never_poisoned_by_tool_call_payloads(tmp_path) -> None:
    """Agent-chosen tool ARGUMENTS (and tool results) must never become the
    session model label: the model scan skips tool-call channel payloads."""

    from agentacct.client_usage import _read_codex_rollout_usage

    poisoned_end = _mcp_tool_call_end(
        "agent-sentinel", "sentinel_record_section", "call_p", _mcp_ok_result(_creation_payload("evt_0011aabbccdd"))
    )
    poisoned_end["payload"]["invocation"]["arguments"] = {"model": "gpt-poison"}
    token_line_no_model = {
        "type": "event_msg",
        "payload": {
            "info": {
                "total_token_usage": {
                    "input_tokens": 5,
                    "cached_input_tokens": 0,
                    "output_tokens": 2,
                    "reasoning_output_tokens": 0,
                }
            }
        },
    }
    rollout = tmp_path / "rollout-poison.jsonl"
    rollout.write_text(
        "\n".join(json.dumps(line) for line in (poisoned_end, token_line_no_model)) + "\n", encoding="utf-8"
    )
    usage = _read_codex_rollout_usage(rollout)
    assert usage is not None
    assert usage.get("model") is None

    # A foreign-server end (rejected, never unwrapped) must not donate a
    # label from its result payload either.
    foreign_end = _mcp_tool_call_end(
        "mcp__evil",
        "sentinel_record_section",
        "call_q",
        {"Ok": {"content": [{"type": "text", "text": "x"}], "model": "gpt-evil"}},
    )
    rollout_foreign = tmp_path / "rollout-foreign.jsonl"
    rollout_foreign.write_text(
        "\n".join(json.dumps(line) for line in (foreign_end, token_line_no_model)) + "\n", encoding="utf-8"
    )
    usage_foreign = _read_codex_rollout_usage(rollout_foreign)
    assert usage_foreign is not None
    assert usage_foreign.get("model") is None

    # The legitimate carrier still labels the session — even when it appears
    # AFTER the tool line (the poisoned line no longer wins the first-scan).
    rollout_legit = tmp_path / "rollout-legit.jsonl"
    rollout_legit.write_text(
        "\n".join(json.dumps(line) for line in (poisoned_end, _token_usage_line())) + "\n", encoding="utf-8"
    )
    usage_legit = _read_codex_rollout_usage(rollout_legit)
    assert usage_legit is not None
    assert usage_legit.get("model") == "gpt-5.5"


def test_classify_codex_mcp_invocation_and_unwrap_codex_mcp_result_units() -> None:
    from agentacct.log_evidence import classify_codex_mcp_invocation, unwrap_codex_mcp_result

    assert classify_codex_mcp_invocation("agent-sentinel", "sentinel_record_section") == "accepted"
    assert classify_codex_mcp_invocation("agent-chronicle", "sentinel_record_event") == "accepted"
    assert classify_codex_mcp_invocation("mcp__agent_sentinel", "sentinel_record_section") == "accepted"
    assert classify_codex_mcp_invocation("other", "sentinel_record_section") == "rejected"
    # None/absent server is REJECTED here (the field is always present in
    # this shape; absence is drift, count it) — unlike the function_call
    # namespace rule where absence is accepted.
    assert classify_codex_mcp_invocation(None, "sentinel_record_section") == "rejected"
    assert classify_codex_mcp_invocation(42, "sentinel_record_section") == "rejected"
    assert classify_codex_mcp_invocation("agent-sentinel", "sentinel_get_event_summary") is None
    assert classify_codex_mcp_invocation("agent-sentinel", None) is None
    assert classify_codex_mcp_invocation("agent-sentinel", 42) is None

    payload_text = json.dumps({"event": {"event_id": "evt_abc123abc123"}})
    assert unwrap_codex_mcp_result({"Ok": {"content": [{"type": "text", "text": payload_text}]}}) == payload_text
    assert unwrap_codex_mcp_result({"Err": "boom"}) is None
    assert unwrap_codex_mcp_result({"Ok": {"content": []}}) is None
    assert unwrap_codex_mcp_result({"Ok": {}}) is None
    assert unwrap_codex_mcp_result({"Ok": "text"}) is None
    assert unwrap_codex_mcp_result("nope") is None
    assert unwrap_codex_mcp_result(None) is None


def test_codex_evidence_only_rollout_rides_sqlite_fallback_row(tmp_path) -> None:
    lines = [
        _function_call("sentinel_record_section", "call_0"),
        _function_output("call_0", _wrapped_output(_creation_payload("evt_0123456789ab"))),
    ]
    codex_home = _make_codex_home(tmp_path, lines, tokens_used=500, include_usage_line=False)

    event = _single_codex_event(codex_home)

    assert event.input_tokens == 500  # sqlite tokens_used fallback still fires
    assert list(event.evidenced_event_ids) == ["evt_0123456789ab"]
    assert event.evidenced_event_id_total == 1


# ---------------------------------------------------------------------------
# Importer (claude code)
# ---------------------------------------------------------------------------


def _claude_usage_line(session_id: str, *, model: str = "claude-opus-4-8", input_tokens: int = 30) -> dict:
    return {
        "type": "assistant",
        "sessionId": session_id,
        "cwd": "/work/project",
        "message": {
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 5,
            },
        },
    }


def _claude_tool_use_line(session_id: str, *, tool_use_id: str, name: str) -> dict:
    return {
        "type": "assistant",
        "sessionId": session_id,
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_use_id, "name": name, "input": {}}],
        },
    }


def _claude_tool_result_line(session_id: str, *, tool_use_id: str, text: str) -> dict:
    return {
        "type": "user",
        "sessionId": session_id,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": [{"type": "text", "text": text}]}],
        },
    }


def test_claude_pairing_rejections_and_free_text_hazard(tmp_path) -> None:
    claude_home = tmp_path / "claude-home"
    project = claude_home / "projects" / "-work-project"
    project.mkdir(parents=True)
    session_id = "c037bd88-0000-4000-8000-000000000001"
    creation_text = json.dumps({"context_attached": True, "event": {"event_id": "evt_910a2536a928"}}, indent=2)
    lines = [
        _claude_usage_line(session_id),
        _claude_tool_use_line(session_id, tool_use_id="toolu_ok", name="mcp__agent-chronicle__sentinel_attach_client_context"),
        _claude_tool_result_line(session_id, tool_use_id="toolu_ok", text=creation_text),
        # Wrong server segment under a creation tool name: rejected + counted.
        _claude_tool_use_line(session_id, tool_use_id="toolu_bad", name="mcp__other__sentinel_record_event"),
        _claude_tool_result_line(session_id, tool_use_id="toolu_bad", text=json.dumps(_creation_payload("evt_deadbeef1111"))),
        # The observed real hazard: an evt id inside a Task-prompt echo
        # (line-level toolUseResult) and in free text — never linked.
        {
            "type": "user",
            "sessionId": session_id,
            "toolUseResult": {"prompt": "verify evt_79987826dace and evt_56b60678c26e"},
            "message": {"role": "user", "content": "please check evt_79987826dace"},
        },
        # tool_result for an unknown tool_use_id with a perfect creation
        # shape: no pairing, no extraction.
        _claude_tool_result_line(session_id, tool_use_id="toolu_unknown", text=json.dumps(_creation_payload("evt_deadbeef2222"))),
    ]
    (project / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )

    events = discover_claude_code_usage(claude_home=claude_home, limit_sessions=10)

    assert len(events) == 1
    event = events[0]
    assert list(event.evidenced_event_ids) == ["evt_910a2536a928"]
    assert event.evidenced_event_id_total == 1
    assert event.evidenced_outputs_skipped == 1  # the wrong-server call only
    payload = event.to_sentinel_event()
    assert "evt_79987826dace" not in json.dumps(payload)
    assert "evt_deadbeef1111" not in json.dumps(payload)


def test_claude_subagent_evidence_lands_on_child_row_and_lanes_dedupe_to_one_donor(tmp_path) -> None:
    claude_home = tmp_path / "claude-home"
    project = claude_home / "projects" / "-work-project"
    project.mkdir(parents=True)
    session_id = "c037bd88-0000-4000-8000-000000000002"
    parent_lines = [
        _claude_usage_line(session_id, model="claude-opus-4-8"),
        _claude_usage_line(session_id, model="claude-haiku-4-5"),
        _claude_tool_use_line(session_id, tool_use_id="toolu_p", name="mcp__agent-chronicle__sentinel_record_section"),
        _claude_tool_result_line(session_id, tool_use_id="toolu_p", text=json.dumps(_creation_payload("evt_aaaa11112222"))),
    ]
    (project / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(line) for line in parent_lines) + "\n", encoding="utf-8"
    )
    child_lines = [
        _claude_usage_line(session_id, model="claude-opus-4-8", input_tokens=7),
        _claude_tool_use_line(session_id, tool_use_id="toolu_c", name="mcp__agent-chronicle__sentinel_record_section"),
        _claude_tool_result_line(session_id, tool_use_id="toolu_c", text=json.dumps(_creation_payload("evt_bbbb33334444"))),
    ]
    (project / "agent-child.jsonl").write_text(
        "\n".join(json.dumps(line) for line in child_lines) + "\n", encoding="utf-8"
    )

    events = discover_claude_code_usage(claude_home=claude_home, limit_sessions=10)

    parent_rows = [event for event in events if event.client_session_id == session_id]
    child_rows = [event for event in events if event.client_session_id == f"{session_id}:agent-child"]
    assert len(parent_rows) == 2  # two model lanes share ONE evidence triple
    for row in parent_rows:
        assert list(row.evidenced_event_ids) == ["evt_aaaa11112222"]
    assert len(child_rows) == 1
    assert list(child_rows[0].evidenced_event_ids) == ["evt_bbbb33334444"]

    from agentacct.usage_truth import mark_trusted_local_usage_import_event

    index = build_log_evidence_index(
        [
            mark_trusted_local_usage_import_event(event.to_sentinel_event() | {"event_id": f"evt_row_{i}"})
            for i, event in enumerate(events)
        ]
    )
    # Lane rows dedupe to ONE donor for the parent id; the child id has its
    # own (child-suffixed) donor session.
    assert len(index["evt_aaaa11112222"]) == 1
    assert index["evt_aaaa11112222"][0]["client_session_id"] == session_id
    assert len(index["evt_bbbb33334444"]) == 1
    assert index["evt_bbbb33334444"][0]["client_session_id"] == f"{session_id}:agent-child"


# ---------------------------------------------------------------------------
# Ledger / join_rules
# ---------------------------------------------------------------------------


def _usage_row(
    *,
    event_id: str,
    session: str = SESSION,
    client: str = "codex",
    transcript: str | None = None,
    evidenced: list[str] | None = None,
    evidenced_total: int | None = None,
    skipped: int = 0,
    project_dir: str | None = "/tmp/projA",
    created_at: float = 100.0,
    started_at: float | None = None,
    session_kind: str = "root",
    parent_session: str | None = None,
    source_namespace: str | None = None,
) -> dict:
    metadata = {
        "usage_source": "local_client_session_store",
        "usage_provenance": "agent_sentinel_local_usage_import",
        "client": client,
        "client_session_id": session,
        "client_transcript_id": transcript,
        "client_session_kind": session_kind,
        "parent_client_session_id": parent_session,
        "cached_input_tokens": 0,
        "project_dir": project_dir,
    }
    if started_at is not None:
        metadata["started_at"] = started_at
    if source_namespace is not None:
        metadata["source_namespace_fingerprint"] = source_namespace
    if evidenced:
        metadata["evidenced_event_ids"] = list(evidenced)
        metadata["evidenced_event_id_total"] = evidenced_total if evidenced_total is not None else len(evidenced)
    if skipped:
        metadata["evidenced_outputs_skipped"] = skipped
    return {
        "event_id": event_id,
        "created_at": created_at,
        "source": f"{client}-local-session-import",
        "event_type": "model_usage",
        "provider": client,
        "model": "gpt-5.5",
        "estimated_input_tokens": 100,
        "estimated_output_tokens": 20,
        "estimated_cost_usd": 0.1,
        "usage_confidence": "client_reported",
        "cost_confidence": "estimated_from_tokens",
        "metadata": metadata,
    }


def _section_event(
    *,
    event_id: str,
    section_id: str = "sec-1",
    client: str | None = "codex",
    session: str | None = None,
    transcript: str | None = None,
    authored: list[str] | None = None,
    inherited: list[str] | None = None,
    context_source: str | None = None,
    extra_metadata: dict | None = None,
    created_at: float = 100.0,
) -> dict:
    metadata = {
        "sentinel_semantic_kind": "section",
        "client": client,
        "client_session_id": session,
        "client_transcript_id": transcript,
        "section_id": section_id,
        "section_status": "completed",
        "section_title": f"Section {section_id}",
        "kind": "implementation",
        "project_dir": "/tmp/projA",
    }
    if authored:
        metadata["client_context_keys_authored"] = list(authored)
    if inherited:
        metadata["client_context_inherited_keys"] = list(inherited)
        metadata["client_context_source"] = context_source or "attach"
    if extra_metadata:
        metadata.update(extra_metadata)
    return {
        "event_id": event_id,
        "created_at": created_at,
        "source": client or "codex",
        "event_type": "section_completed",
        "metadata": metadata,
    }


def _attach_event(
    *,
    event_id: str,
    client: str | None = "codex",
    session: str | None = None,
    transcript: str | None = None,
    authored: list[str] | None = None,
    project_dir: str | None = "/tmp/projA",
    created_at: float = 100.0,
) -> dict:
    metadata = {
        "sentinel_semantic_kind": "client_context",
        "client": client,
        "client_session_id": session,
        "client_transcript_id": transcript,
        "project_dir": project_dir,
    }
    if authored:
        metadata["client_context_keys_authored"] = list(authored)
    return {
        "event_id": event_id,
        "created_at": created_at,
        "source": client or "codex",
        "event_type": "client_context_attached",
        "metadata": metadata,
    }


def _entry(ledger: dict, client: str, session: str) -> dict:
    for entry in ledger["session_rollup"]["sessions"]:
        if entry["client"] == client and entry["client_session_id"] == session:
            return entry
    raise AssertionError(f"no rollup entry for {client}::{session}")


def test_codex_fork_replay_before_child_start_is_not_a_donor() -> None:
    target_event_id = "evt_5ec000000001"
    events = [
        _usage_row(
            event_id="evt_usage_root",
            session=SESSION,
            evidenced=[target_event_id],
            started_at=90.0,
        ),
        _usage_row(
            event_id="evt_usage_child",
            session=OTHER_SESSION,
            evidenced=[target_event_id],
            started_at=200.0,
            created_at=210.0,
            session_kind="child",
            parent_session=SESSION,
        ),
        _section_event(
            event_id=target_event_id,
            section_id="sec-replayed",
            session=None,
            created_at=100.0,
        ),
    ]

    index = build_log_evidence_index(events)

    assert [donor["client_session_id"] for donor in index[target_event_id]] == [SESSION]
    assert index.diagnostics == {
        "rejected_donor_links": 1,
        "rejected_usage_rows": 1,
        "rejection_reasons": {CODEX_REPLAY_REJECTION_REASON: 1},
        "clock_skew_tolerance_seconds": CODEX_REPLAY_CLOCK_SKEW_SECONDS,
    }
    # Filtering is derived-only. The imported child row remains intact for
    # raw/forensic views and future reconciliation changes.
    assert events[1]["metadata"]["evidenced_event_ids"] == [target_event_id]

    ledger = build_work_ledger(events)
    item = ledger["work_items"][0]
    assert item["client_session_id"] == SESSION
    assert item.get("log_evidence_ambiguous") is None


def test_codex_descendant_replay_filter_fails_closed_when_either_timestamp_is_missing() -> None:
    target_event_id = "evt_5ec000000001"
    no_canonical_event_time = [
        _usage_row(
            event_id="evt_usage_child",
            session=OTHER_SESSION,
            evidenced=[target_event_id],
            started_at=200.0,
            session_kind="child",
            parent_session=SESSION,
        )
    ]
    no_session_start = [
        _usage_row(
            event_id="evt_usage_child",
            session=OTHER_SESSION,
            evidenced=[target_event_id],
            session_kind="child",
            parent_session=SESSION,
        ),
        _section_event(event_id=target_event_id, session=None, created_at=100.0),
    ]

    for events in (no_canonical_event_time, no_session_start):
        index = build_log_evidence_index(events)
        assert target_event_id not in index
        assert index.diagnostics["rejected_donor_links"] == 1
        assert index.diagnostics["rejection_reasons"] == {
            CODEX_REPLAY_TIME_UNPROVEN_REASON: 1
        }


def test_codex_root_keeps_legacy_donor_when_timestamp_is_missing() -> None:
    target_event_id = "evt_5ec000000001"
    index = build_log_evidence_index(
        [_usage_row(event_id="evt_usage_root", evidenced=[target_event_id])]
    )

    assert index[target_event_id][0]["client_session_id"] == SESSION
    assert index.diagnostics["rejected_donor_links"] == 0


def test_codex_root_with_impossible_event_time_is_rejected() -> None:
    target_event_id = "evt_5ec000000001"
    index = build_log_evidence_index(
        [
            _usage_row(
                event_id="evt_usage_future_root",
                evidenced=[target_event_id],
                started_at=200.0,
            ),
            _section_event(event_id=target_event_id, session=None, created_at=100.0),
        ]
    )

    assert target_event_id not in index
    assert index.diagnostics["rejection_reasons"] == {
        CODEX_REPLAY_REJECTION_REASON: 1
    }


def test_replay_filter_is_codex_only_and_allows_clock_skew_boundary() -> None:
    target_event_id = "evt_5ec000000001"
    boundary_start = 100.0 + CODEX_REPLAY_CLOCK_SKEW_SECONDS
    events = [
        _usage_row(
            event_id="evt_usage_codex",
            session=SESSION,
            evidenced=[target_event_id],
            started_at=boundary_start,
            session_kind="child",
            parent_session="codex-parent",
        ),
        _usage_row(
            event_id="evt_usage_claude",
            client="claude-code",
            session="claude-child",
            evidenced=[target_event_id],
            started_at=200.0,
            created_at=210.0,
        ),
        _section_event(event_id=target_event_id, session=None, created_at=100.0),
    ]

    index = build_log_evidence_index(events)

    assert {(donor["client"], donor["client_session_id"]) for donor in index[target_event_id]} == {
        ("codex", SESSION),
        ("claude-code", "claude-child"),
    }
    assert index.diagnostics["rejected_donor_links"] == 0


def test_conflicting_canonical_event_timestamps_reject_donor() -> None:
    target_event_id = "evt_5ec000000001"
    events = [
        _usage_row(
            event_id="evt_usage_child",
            session=OTHER_SESSION,
            evidenced=[target_event_id],
            started_at=200.0,
        ),
        _section_event(event_id=target_event_id, session=None, created_at=100.0),
        _section_event(event_id=target_event_id, section_id="sec-duplicate", session=None, created_at=110.0),
    ]

    index = build_log_evidence_index(events)

    assert target_event_id not in index
    assert index.diagnostics["rejected_donor_links"] == 1
    assert index.diagnostics["rejection_reasons"] == {
        CANONICAL_EVENT_IDENTITY_CONFLICT_REASON: 1
    }


def test_donor_source_namespace_conflict_rejects_every_candidate() -> None:
    target_event_id = "evt_5ec000000001"
    events = [
        _usage_row(
            event_id="evt_usage_home_a",
            session=SESSION,
            evidenced=[target_event_id],
            source_namespace="sha256:home-a",
        ),
        _usage_row(
            event_id="evt_usage_home_b",
            session=OTHER_SESSION,
            evidenced=[target_event_id],
            source_namespace="sha256:home-b",
        ),
        _section_event(event_id=target_event_id, session=None, created_at=100.0),
    ]

    index = build_log_evidence_index(events)

    assert target_event_id not in index
    assert index.diagnostics["rejected_donor_links"] == 2
    assert index.diagnostics["rejected_usage_rows"] == 2
    assert index.diagnostics["rejection_reasons"] == {
        SOURCE_NAMESPACE_CONFLICT_REASON: 2
    }
    assert all(event["metadata"].get("evidenced_event_ids") == [target_event_id] for event in events[:2])


def test_explicit_and_missing_donor_source_namespace_fail_closed() -> None:
    target_event_id = "evt_5ec000000001"
    index = build_log_evidence_index(
        [
            _usage_row(
                event_id="evt_usage_scoped",
                session=SESSION,
                evidenced=[target_event_id],
                source_namespace="sha256:home-a",
            ),
            _usage_row(
                event_id="evt_usage_legacy",
                session=OTHER_SESSION,
                evidenced=[target_event_id],
            ),
            _section_event(event_id=target_event_id, session=None, created_at=100.0),
        ]
    )

    assert target_event_id not in index
    assert index.diagnostics["rejection_reasons"] == {
        SOURCE_NAMESPACE_CONFLICT_REASON: 2
    }


def test_canonical_event_namespace_collision_rejects_donor() -> None:
    target_event_id = "evt_5ec000000001"
    events = [
        _usage_row(event_id="evt_usage_root", evidenced=[target_event_id]),
        _section_event(
            event_id=target_event_id,
            section_id="sec-a",
            session=None,
            created_at=100.0,
            extra_metadata={
                "session_namespace_fingerprint": "sha256:project-a",
                "identity_scope_state": "explicit",
            },
        ),
        _section_event(
            event_id=target_event_id,
            section_id="sec-b",
            session=None,
            created_at=100.0,
            extra_metadata={
                "session_namespace_fingerprint": "sha256:project-b",
                "identity_scope_state": "explicit",
            },
        ),
    ]

    index = build_log_evidence_index(events)

    assert target_event_id not in index
    assert index.diagnostics["rejection_reasons"] == {
        CANONICAL_EVENT_IDENTITY_CONFLICT_REASON: 1
    }


def test_unique_donor_single_section_allocates_high_via_the_one_matcher() -> None:
    events = [
        _usage_row(event_id="evt_usage_s", evidenced=["evt_5ec000000001"]),
        _section_event(event_id="evt_5ec000000001", section_id="sec-1", session=None),
    ]

    ledger = build_work_ledger(events)

    attr = ledger["attributions"][0]
    # work_id derivation UNCHANGED (id stability): the id-less section keeps
    # its historical session-less identity even though the link is evidenced.
    assert attr["work_id"] == "codex::-::sec-1"
    assert attr["join_strategy"] == "client_log_evidenced_client_session_id"
    assert attr["join_confidence"] == "high"
    assert "client-log evidenced" in attr["join_reason"]
    assert "exact" not in attr["join_confidence"]
    entry = _entry(ledger, "codex", SESSION)
    assert entry["join"]["state"] == "attributed"
    assert entry["work"]["counts"]["total"] == 1
    evidence = ledger["insights"]["client_log_evidence"]
    assert evidence["donor_usage_rows"] == 1
    assert evidence["implied_session_keys"] == 1
    assert evidence["conflicts"] == 0
    assert evidence["evidenced_events_in_store"] == 1


def test_two_evidenced_sections_refuse_with_codex_specific_copy() -> None:
    events = [
        _usage_row(event_id="evt_usage_s", evidenced=["evt_5ec000000001", "evt_5ec000000002"]),
        _section_event(event_id="evt_5ec000000001", section_id="sec-1", session=None, created_at=100.0),
        _section_event(event_id="evt_5ec000000002", section_id="sec-2", session=None, created_at=110.0),
    ]

    ledger = build_work_ledger(events)

    attr = ledger["attributions"][0]
    assert attr["work_id"] is None  # refusal, no allocation
    assert attr["join_strategy"] == "exact_client_session_id_ambiguous_sections"
    assert attr["join_confidence"] == "medium"
    assert attr["join_reason"] == (
        "this session recorded 2 sections (client-log evidenced); "
        "session token totals are not divided among sections"
    )
    assert SESSION not in attr["join_reason"]  # id-free
    entry = _entry(ledger, "codex", SESSION)
    assert entry["join"]["state"] == "ambiguous"
    # Display win: the sections group under the session row.
    assert entry["work"]["counts"]["total"] == 2


def test_non_codex_ambiguous_copy_unchanged() -> None:
    events = [
        _usage_row(event_id="evt_usage_s", client="claude-code", session="cs-1", evidenced=["evt_5ec000000001", "evt_5ec000000002"]),
        _section_event(event_id="evt_5ec000000001", section_id="sec-1", client="claude-code", session=None, created_at=100.0),
        _section_event(event_id="evt_5ec000000002", section_id="sec-2", client="claude-code", session=None, created_at=110.0),
    ]

    ledger = build_work_ledger(events)

    attr = ledger["attributions"][0]
    assert attr["join_strategy"] == "exact_client_session_id_ambiguous_sections"
    assert attr["join_reason"] == "usage shares client_session_id with 2 sections; not allocating to a section"


def test_fabricated_id_conflict_vetoes_both_directions() -> None:
    fake = "codex-chat-zhe-li-2026-07-05"
    events = [
        _usage_row(event_id="evt_usage_real", session=SESSION, transcript=SESSION, evidenced=["evt_5ec000000001"]),
        _usage_row(event_id="evt_usage_fake", session=fake, transcript=fake, created_at=105.0),
        _section_event(
            event_id="evt_5ec000000001",
            section_id="sec-x",
            session=fake,
            transcript=fake,
            authored=["client_session_id", "client_transcript_id"],
        ),
    ]

    ledger = build_work_ledger(events)

    by_usage = {attr["usage_event_id"]: attr for attr in ledger["attributions"]}
    real = by_usage["evt_usage_real"]
    # The evidenced key never allocates from a conflicted snapshot.
    assert real["work_id"] is None
    assert real["join_strategy"] == "unjoined"
    assert "log_evidenced_session_id_conflict" in real["join_vetoes"]
    assert fake not in str(real["join_reason"])  # id-free veto reason
    assert "evidenced by the client's own log" in real["join_reason"]
    # The fabricated id joins nothing even though a usage row carries it.
    fake_attr = by_usage["evt_usage_fake"]
    assert fake_attr["work_id"] is None
    assert fake_attr["join_strategy"] == "unjoined"

    item = ledger["work_items"][0]
    # Evidenced grouping wins for display; the claim is preserved, not smoothed.
    assert item["client_session_id"] == SESSION
    assert item["claimed_client_session_id"] == fake
    assert item["log_evidence_conflict"]
    entry = _entry(ledger, "codex", SESSION)
    assert entry["join"]["state"] == "unjoined"
    assert entry["join"]["vetoed_rows"] == 1
    assert entry["work"]["counts"]["total"] == 1
    assert entry["join"]["client_log_evidence"]["conflicts"] == 1
    assert entry["join"]["project_level_context_only"] is False
    listed = entry["work"]["items"][0]
    assert listed["log_evidence_conflict"] is True
    evidence = ledger["insights"]["client_log_evidence"]
    assert evidence["conflicts"] == 1
    assert evidence["implied_session_keys"] == 0


def test_multi_session_evidence_refuses_with_counter() -> None:
    events = [
        _usage_row(event_id="evt_usage_a", session=SESSION, evidenced=["evt_5ec000000001"]),
        _usage_row(event_id="evt_usage_b", session=OTHER_SESSION, evidenced=["evt_5ec000000001"], created_at=105.0),
        _section_event(event_id="evt_5ec000000001", section_id="sec-1", session=None),
    ]

    ledger = build_work_ledger(events)

    for attr in ledger["attributions"]:
        assert attr["work_id"] is None
        assert attr["join_strategy"] == "unjoined"
    item = ledger["work_items"][0]
    assert item["client_session_id"] is None  # no implied key
    assert item["log_evidence_ambiguous"] == {"session_count": 2}
    assert item["log_evidence_candidate_sessions"] == [
        {"client": "codex", "client_session_id": SESSION},
        {"client": "codex", "client_session_id": OTHER_SESSION},
    ]
    assert ledger["insights"]["client_log_evidence"]["ambiguous_multi_session"] == 1
    # The section stays honestly unassigned in the rollup.
    assert ledger["session_rollup"]["summary"]["unassigned_work_items"] == 1


def test_opencode_evidenced_section_attaches_to_its_session() -> None:
    # End-to-end: an opencode section recorded via MCP carries no session id of
    # its own (session=None), but the opencode usage row evidences its event id
    # from the part log, so it attaches to that session — the flip from a task
    # with NO items (observed) to a task with a real step (reported).
    target = "evt_0bc0de000001"
    events = [
        _usage_row(event_id="evt_usage_oc", session="ses_oc_a", client="opencode", evidenced=[target]),
        _section_event(event_id=target, section_id="notes", client="opencode", session=None),
    ]

    ledger = build_work_ledger(events)

    item = ledger["work_items"][0]
    assert item["client_session_id"] == "ses_oc_a"
    assert item.get("log_evidence_ambiguous") is None


def test_opencode_multi_session_evidence_refuses() -> None:
    # The generic >1-donor guard reaches opencode too: one event evidenced by two
    # opencode sessions links to neither (no guessed session).
    target = "evt_0bc0de000002"
    events = [
        _usage_row(event_id="evt_usage_a", session="ses_oc_a", client="opencode", evidenced=[target]),
        _usage_row(event_id="evt_usage_b", session="ses_oc_b", client="opencode", evidenced=[target], created_at=105.0),
        _section_event(event_id=target, section_id="sec-1", client="opencode", session=None),
    ]

    ledger = build_work_ledger(events)

    item = ledger["work_items"][0]
    assert item["client_session_id"] is None  # >1 donor -> links to none
    assert item["log_evidence_ambiguous"] == {"session_count": 2}


def test_cross_session_idless_section_id_collision_item_level_refusal() -> None:
    events = [
        _usage_row(event_id="evt_usage_a", session=SESSION, evidenced=["evt_5a4ed000000a"]),
        _usage_row(event_id="evt_usage_b", session=OTHER_SESSION, evidenced=["evt_5a4ed000000b"], created_at=105.0),
        _section_event(event_id="evt_5a4ed000000a", section_id="shared", session=None, created_at=100.0),
        _section_event(event_id="evt_5a4ed000000b", section_id="shared", session=None, created_at=110.0),
    ]

    ledger = build_work_ledger(events)

    # One merged work item (unchanged identity), NO implied keys, no allocation.
    assert len(ledger["work_items"]) == 1
    item = ledger["work_items"][0]
    assert item["work_id"] == "codex::-::shared"
    assert item["client_session_id"] is None
    assert item["log_evidence_candidate_sessions"] == [
        {"client": "codex", "client_session_id": SESSION},
        {"client": "codex", "client_session_id": OTHER_SESSION},
    ]
    for attr in ledger["attributions"]:
        assert attr["work_id"] is None
        assert attr["join_strategy"] == "unjoined"
    evidence = ledger["insights"]["client_log_evidence"]
    assert evidence["item_conflicts"] == 1
    assert evidence["ambiguous_multi_session"] == 2
    assert evidence["implied_session_keys"] == 0


def test_corroboration_never_downgrades_explicit_or_hook() -> None:
    explicit_events = [
        _usage_row(event_id="evt_usage_s", evidenced=["evt_5ec000000001"]),
        _section_event(
            event_id="evt_5ec000000001",
            section_id="sec-1",
            session=SESSION,
            authored=["client_session_id"],
        ),
    ]
    ledger = build_work_ledger(explicit_events)
    attr = ledger["attributions"][0]
    assert attr["join_strategy"] == "exact_client_session_id"
    assert attr["join_confidence"] == "exact"
    assert ledger["insights"]["client_log_evidence"]["corroborated"] == 1

    hook_events = [
        _usage_row(event_id="evt_usage_h", client="claude-code", session="cs-1", evidenced=["evt_5ec000000002"]),
        _section_event(
            event_id="evt_5ec000000002",
            section_id="sec-2",
            client="claude-code",
            session="cs-1",
            inherited=["client_session_id"],
            context_source="claude_code_hook",
        ),
    ]
    hook_ledger = build_work_ledger(hook_events)
    hook_attr = hook_ledger["attributions"][0]
    assert hook_attr["join_strategy"] == "client_derived_client_session_id"
    assert hook_attr["join_confidence"] == "high"


def test_forged_markers_in_stored_metadata_are_ignored_everywhere() -> None:
    events = [
        # Untrusted usage-shaped event: has usage_source but NOT the trust
        # provenance marker (service.record_event strips it from MCP/HTTP
        # writers), so it can never mint a donor.
        {
            "event_id": "evt_forged_donor",
            "created_at": 100.0,
            "source": "hostile",
            "event_type": "model_usage",
            "estimated_input_tokens": 1,
            "estimated_output_tokens": 1,
            "metadata": {
                "usage_source": "local_client_session_store",
                "client": "codex",
                "client_session_id": SESSION,
                "evidenced_event_ids": ["evt_5ec000000001"],
                "evidenced_event_id_total": 1,
            },
        },
        # Section forging derived-only markers directly in metadata: the
        # snapshot is built fresh and never copies them.
        _section_event(
            event_id="evt_5ec000000001",
            section_id="sec-1",
            session=SESSION,
            extra_metadata={
                "log_evidenced_join_keys": ["client_session_id"],
                "log_evidence_conflict": {"claimed_client_session_id": "x"},
                "log_evidence_corroborated": True,
            },
        ),
        _usage_row(event_id="evt_usage_s", session=SESSION),
    ]

    ledger = build_work_ledger(events)

    assert build_log_evidence_index(events) == {}
    evidence = ledger["insights"]["client_log_evidence"]
    assert evidence["donor_usage_rows"] == 0
    assert evidence["evidenced_events_in_store"] == 0
    attr = next(a for a in ledger["attributions"] if a["usage_event_id"] == "evt_usage_s")
    # Tier stays unverified (id present, no verifiable provenance): the
    # forged log_evidenced marker neither upgrades the tier nor vetoes.
    assert attr["join_strategy"] == "unverified_client_session_id"
    assert attr["join_confidence"] == "high"
    assert attr["join_vetoes"] == []


def test_log_evidenced_tier_can_never_reach_exact() -> None:
    assert TIER_CONFIDENCE["log_evidenced"] == "high"
    assert TIER_STRATEGY_PREFIX["log_evidenced"] == "client_log_evidenced"
    assert [tier for tier, confidence in TIER_CONFIDENCE.items() if confidence == "exact"] == ["explicit"]

    snapshot = {"client_session_id": SESSION, "log_evidenced_join_keys": ["client_session_id"]}
    assert id_key_tier(snapshot, "client_session_id") == "log_evidenced"
    usage = {"client_session_id": SESSION}
    match = pair_match(usage, snapshot)
    assert match["join_keys"] == ["client_session_id_log_evidenced"]
    assert pair_confidence(match) == "high"
    decision = decide_attribution(usage, [dict(snapshot, work_id="w1")])
    assert decision["confidence"] == "high"
    assert decision["strategy"] == "client_log_evidenced_client_session_id"

    # The applier never mints authored/explicit provenance.
    idless = {"event_id": "evt_5ec000000001", "client": "codex", "client_session_id": None, "client_transcript_id": None}
    index = {"evt_5ec000000001": [{"client": "codex", "client_session_id": SESSION, "client_transcript_id": None, "usage_event_id": "evt_u"}]}
    apply_log_evidence_to_snapshots([idless], index)
    assert idless.get("authored_join_keys") is None
    assert id_key_tier(idless, "client_session_id") == "log_evidenced"


def test_extract_created_event_id_strict_shape() -> None:
    assert extract_created_event_id(json.dumps({"event": {"event_id": "evt_abc123abc123"}})) == "evt_abc123abc123"
    for bad in (
        "not json",
        json.dumps(["evt_abc123abc123"]),
        json.dumps({"events": [{"event_id": "evt_abc123abc123"}]}),
        json.dumps({"event": {"event_id": "evt_abc123abc123"}, "runs": []}),
        json.dumps({"event": {"event_id": "not-an-evt-id"}}),
        json.dumps({"event": "evt_abc123abc123"}),
        json.dumps({}),
    ):
        assert extract_created_event_id(bad) is None


def test_bridge_groups_evidenced_attach_but_caps_at_medium() -> None:
    events = [
        _usage_row(event_id="evt_usage_s", evidenced=["evt_a77000000001"]),
        _attach_event(event_id="evt_a77000000001", session=None),
    ]

    bridge = build_usage_context_bridge(events)

    link = next(l for l in bridge["links"] if l["event_id"] == "evt_usage_s")
    assert link["context_event_count"] == 1
    assert link["join_strategy"] == "context_only_client_context"
    assert link["join_confidence"] == "medium"  # never high without a section
    assert link["attribution_status"] == "context_matched_unallocated"
    assert bridge["unlinked_context_events"] == 0


# ---------------------------------------------------------------------------
# Rollup join block: evidence block precedence + residual flag + JSON
# ---------------------------------------------------------------------------


def test_evidenced_context_block_carries_counts_and_conflicts() -> None:
    fake = "codex-chat-zhe-li-2026-07-05"
    events = [
        _usage_row(event_id="evt_usage_s", evidenced=["evt_a77000000001", "evt_91a100000001"]),
        _attach_event(event_id="evt_a77000000001", session=fake, transcript=fake, authored=["client_session_id"]),
        {
            "event_id": "evt_91a100000001",
            "created_at": 101.0,
            "source": "codex-chat-sanity-check",
            "event_type": "sentinel_install_check",
            "metadata": {"client_session_id": fake},
        },
    ]

    ledger = build_work_ledger(events)
    entry = _entry(ledger, "codex", SESSION)

    block = entry["join"]["client_log_evidence"]
    assert block["evidenced_event_count"] == 2
    assert block["conflicts"] == 2
    assert block["by_event_type"] == {"client_context_attached": 1, "sentinel_install_check": 1}
    assert entry["join"]["state"] == "unjoined"


def test_project_level_context_only_conditions_falsified_in_turn() -> None:
    def _ledger_for(*, client: str = "codex", attach_project: str = "/tmp/projA", with_evidence: bool = False, with_veto: bool = False):
        usage = _usage_row(
            event_id="evt_usage_s",
            client=client,
            transcript="trans-1",
            evidenced=["evt_a77000000001"] if with_evidence else None,
        )
        events = [usage, _attach_event(event_id="evt_a77000000001", client=client, session=None, project_dir=attach_project)]
        if with_veto:
            events.append(
                _section_event(
                    event_id="evt_sec_veto0001",
                    section_id="sec-veto",
                    client=client,
                    session="another-session",
                    transcript="trans-1",
                    authored=["client_session_id", "client_transcript_id"],
                )
            )
        return build_work_ledger(events)

    # All conditions hold: codex, unjoined, no vetoes, no evidence, label match.
    ledger = _ledger_for()
    entry = _entry(ledger, "codex", SESSION)
    assert entry["join"]["project_level_context_only"] is True

    # Codex-only by decision: any other client never gets the residual flag.
    claude_entry = _entry(_ledger_for(client="claude-code"), "claude-code", SESSION)
    assert claude_entry["join"]["project_level_context_only"] is False

    # Label mismatch: context from another project proves nothing here.
    other_label_entry = _entry(_ledger_for(attach_project="/tmp/otherproj"), "codex", SESSION)
    assert other_label_entry["join"]["project_level_context_only"] is False

    # Evidence wins: the stronger evidenced-context block takes precedence.
    evidenced_entry = _entry(_ledger_for(with_evidence=True), "codex", SESSION)
    assert evidenced_entry["join"]["project_level_context_only"] is False
    assert evidenced_entry["join"]["client_log_evidence"]["evidenced_event_count"] == 1

    # A vetoed session has context HERE; the veto is the honest state.
    vetoed_entry = _entry(_ledger_for(with_veto=True), "codex", SESSION)
    assert vetoed_entry["join"]["vetoed_rows"] >= 1
    assert vetoed_entry["join"]["project_level_context_only"] is False

    # Bare fallback when no condition matches at all: no evidence block either.
    bare_ledger = build_work_ledger([_usage_row(event_id="evt_usage_s", project_dir="/tmp/projB")])
    bare_entry = _entry(bare_ledger, "codex", SESSION)
    assert bare_entry["join"]["project_level_context_only"] is False
    assert bare_entry["join"]["client_log_evidence"] is None


def test_evidenced_session_never_claims_other_project_scope() -> None:
    events = [
        _usage_row(event_id="evt_usage_s", project_dir="/tmp/otherproj", evidenced=["evt_a77000000001"]),
        _attach_event(event_id="evt_a77000000001", session=None, project_dir="/tmp/otherproj"),
    ]

    ledger = build_work_ledger(events, store_project_label="projA", store_scope="project")
    entry = _entry(ledger, "codex", SESSION)

    # Evidenced context lives HERE: the cross-store claim would be false.
    assert entry["join"]["context_scope"] == "this_store"


def test_sessions_json_carries_evidence_block_and_residual_flag(tmp_path) -> None:
    store = tmp_path / "store"
    service = SentinelService(store)
    # The service assigns event ids itself (identity cannot be spoofed):
    # record the attach first, then reference the REAL stored id.
    attached = service.record_event(_attach_event(event_id="ignored", session=None))
    service.record_event(_usage_row(event_id="evt_usage_s", evidenced=[attached["event_id"]]), trusted_usage_import=True)

    client = TestClient(create_local_api_app(store_dir=store))
    payload = client.get("/sessions").json()

    sessions = payload["sessions"]
    entry = next(s for s in sessions if s["client_session_id"] == SESSION)
    assert entry["join"]["client_log_evidence"]["evidenced_event_count"] == 1
    assert entry["join"]["project_level_context_only"] is False
    assert "log_evidence_conflict" in (entry["work"]["items"] or [{}])[0] if entry["work"]["items"] else True


# ---------------------------------------------------------------------------
# CLI surfaces: usage import-local --json and mcp doctor
# ---------------------------------------------------------------------------


def test_usage_import_local_json_reports_evidenced_link_counts(tmp_path) -> None:
    lines = [
        _function_call("sentinel_record_section", "call_0"),
        _function_output("call_0", _wrapped_output(_creation_payload("evt_0123456789ab"))),
        _function_call("sentinel_record_section", "call_1"),
        _function_output("call_1", "tool call error: failed"),
    ]
    codex_home = _make_codex_home(tmp_path, lines)
    store = tmp_path / "store"

    result = CliRunner().invoke(
        app,
        [
            "usage",
            "import-local",
            "--store-dir",
            str(store),
            "--client",
            "codex",
            "--codex-home",
            str(codex_home),
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["client_log_evidence"] == {
        "sessions_with_evidenced_events": 1,
        "evidenced_event_ids_total": 1,
        "evidenced_outputs_skipped": 1,
    }

    human = CliRunner().invoke(
        app,
        [
            "usage",
            "import-local",
            "--store-dir",
            str(store),
            "--client",
            "codex",
            "--codex-home",
            str(codex_home),
            "--dry-run",
        ],
    )
    assert human.exit_code == 0, human.output
    assert "Client-log evidenced links: 1 scanned session(s) carry 1 recorded-event id(s)" in human.output


def test_mcp_doctor_reports_evidenced_link_counts(tmp_path) -> None:
    store = tmp_path / "store"
    service = SentinelService(store)
    attached = service.record_event(_attach_event(event_id="ignored", session=None))
    service.record_event(
        _usage_row(event_id="evt_usage_s", evidenced=[attached["event_id"]], skipped=2), trusted_usage_import=True
    )

    result = CliRunner().invoke(app, ["mcp", "doctor", "--store-dir", str(store), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["read_only"] is True
    assert payload["client_log_evidence"] == {
        "donor_usage_rows": 1,
        "evidenced_event_ids_total": 1,
        "evidenced_events_in_store": 1,
        "outputs_skipped": 2,
    }

    human = CliRunner().invoke(app, ["mcp", "doctor", "--store-dir", str(store)])
    assert human.exit_code == 0, human.output
    assert "client-log evidence: 1 usage row(s) carry 1 evidenced event id(s)" in human.output


# ---------------------------------------------------------------------------
# Adversarial-review fix round (Phase 2.7)
# ---------------------------------------------------------------------------


def test_fix1_donor_cohort_refuses_even_when_a_sibling_section_is_conflict_vetoed() -> None:
    """FIX 1 (the important one): a co-session conflict veto must NOT let an
    ambiguous donor become a confident single-section allocation.

    Donor D's log evidence links TWO distinct sections. An agent FABRICATES a
    session id on one of them, so that section is conflict-vetoed and drops out
    of the surviving candidates. Pre-fix, the OTHER section became a unique
    survivor and D allocated 100% of its tokens to it at high — an honest
    ambiguous refusal converted into a confident allocation via untrusted
    input. The decision must instead refuse: the session genuinely spans two
    sections and its tokens cannot be divided.
    """

    fake = "fabricated-other-session"
    events = [
        _usage_row(event_id="evt_usage_d", evidenced=["evt_5ec00e0a0001", "evt_5ec00e0b0002"]),
        _section_event(event_id="evt_5ec00e0a0001", section_id="sec-good", session=None, created_at=100.0),
        _section_event(
            event_id="evt_5ec00e0b0002",
            section_id="sec-bad",
            session=fake,
            authored=["client_session_id"],
            created_at=110.0,
        ),
    ]

    ledger = build_work_ledger(events)

    attr = {a["usage_event_id"]: a for a in ledger["attributions"]}["evt_usage_d"]
    # THE fix: no allocation. Pre-fix this allocated to sec-good at high.
    assert attr["work_id"] is None
    assert attr["join_strategy"] == "client_log_evidenced_client_session_id_ambiguous_sections"
    assert attr["join_confidence"] == "medium"
    # Codex copy + id-free.
    assert "this session recorded 2 sections (client-log evidenced)" in attr["join_reason"]
    assert SESSION not in attr["join_reason"] and fake not in attr["join_reason"]
    # Display win preserved: both sections still group under the session.
    entry = _entry(ledger, "codex", SESSION)
    assert entry["join"]["state"] == "ambiguous"
    assert entry["work"]["counts"]["total"] == 2
    # The fabricated claim still joins nothing structurally.
    assert entry["join"]["client_log_evidence"]["conflicts"] == 1


def test_fix1_two_idless_sections_one_donor_still_refuse_and_group() -> None:
    """FIX 1 (a): two id-less sections, one donor -> refuse; sections group."""

    events = [
        _usage_row(event_id="evt_usage_d", evidenced=["evt_5ec00e0d0001", "evt_5ec00e0d0002"]),
        _section_event(event_id="evt_5ec00e0d0001", section_id="sec-a1", session=None, created_at=100.0),
        _section_event(event_id="evt_5ec00e0d0002", section_id="sec-a2", session=None, created_at=110.0),
    ]

    ledger = build_work_ledger(events)

    attr = ledger["attributions"][0]
    assert attr["work_id"] is None
    assert "ambiguous" in attr["join_strategy"]
    entry = _entry(ledger, "codex", SESSION)
    section_ids = {item["section_id"] for item in entry["work"]["items"]}
    assert section_ids == {"sec-a1", "sec-a2"}


def test_fix1_single_idless_section_one_donor_still_allocates_high() -> None:
    """FIX 1 (c): the PRIMARY honest case must not regress — a donor evidencing
    exactly ONE section still allocates to it at high (the zhe-li-style win)."""

    events = [
        _usage_row(event_id="evt_usage_d", evidenced=["evt_5ec00e0c0001"]),
        _section_event(event_id="evt_5ec00e0c0001", section_id="sec-only", session=None),
    ]

    ledger = build_work_ledger(events)

    attr = ledger["attributions"][0]
    assert attr["work_id"] == "codex::-::sec-only"
    assert attr["join_strategy"] == "client_log_evidenced_client_session_id"
    assert attr["join_confidence"] == "high"


def test_fix1_donor_cohort_ambiguous_variant_non_codex_copy() -> None:
    """FIX 1: the cohort refusal produces an id-free non-codex reason too."""

    fake = "fabricated-other-session"
    events = [
        _usage_row(
            event_id="evt_usage_c",
            client="claude-code",
            session="cs-1",
            evidenced=["evt_5ec00e0e0001", "evt_5ec00e0e0002"],
        ),
        _section_event(event_id="evt_5ec00e0e0001", section_id="sec-good", client="claude-code", session=None, created_at=100.0),
        _section_event(
            event_id="evt_5ec00e0e0002",
            section_id="sec-bad",
            client="claude-code",
            session=fake,
            authored=["client_session_id"],
            created_at=110.0,
        ),
    ]

    ledger = build_work_ledger(events)

    attr = ledger["attributions"][0]
    assert attr["work_id"] is None
    assert attr["join_strategy"] == "client_log_evidenced_client_session_id_ambiguous_sections"
    assert attr["join_reason"] == "this session's log evidence links 2 sections; not allocating to a section"


def test_fix2_transcript_only_conflict_is_labelled_by_the_transcript_key() -> None:
    """FIX 2: a transcript-only conflict (session agrees) must NOT be recorded
    as an equal session-id pair, and the veto reason must blame the transcript,
    not the session."""

    events = [
        _usage_row(event_id="evt_usage_d", transcript="tx-real", evidenced=["evt_5ec0ab000001"]),
        _section_event(
            event_id="evt_5ec0ab000001",
            section_id="sec-1",
            session=SESSION,  # AGREES with the donor session
            transcript="tx-fake",  # differs from the donor transcript
            authored=["client_session_id", "client_transcript_id"],
        ),
    ]

    ledger = build_work_ledger(events)

    item = ledger["work_items"][0]
    conflict = item["log_evidence_conflict"]
    assert conflict["conflicting_keys"] == ["client_transcript_id"]
    # No equal claimed==evidenced session pair is written.
    assert "claimed_client_session_id" not in conflict
    assert "evidenced_client_session_id" not in conflict
    assert conflict["claimed_client_transcript_id"] == "tx-fake"
    assert conflict["evidenced_client_transcript_id"] == "tx-real"
    assert item["claimed_client_session_id"] is None  # session never overwritten

    attr = ledger["attributions"][0]
    assert attr["work_id"] is None
    assert "log_evidenced_transcript_id_conflict" in attr["join_vetoes"]
    # Reason blames the TRANSCRIPT, not the session; id-free.
    assert "agent-claimed transcript id conflicts" in attr["join_reason"]
    assert "session id conflicts" not in attr["join_reason"]
    assert SESSION not in attr["join_reason"] and "tx-fake" not in attr["join_reason"]


def test_fix2_session_conflict_still_labelled_by_the_session_key() -> None:
    """FIX 2: a session-id conflict keeps the session-worded veto reason."""

    fake = "codex-chat-fabricated"
    events = [
        _usage_row(event_id="evt_usage_d", evidenced=["evt_5ec0ab000002"]),
        _section_event(
            event_id="evt_5ec0ab000002",
            section_id="sec-1",
            session=fake,
            authored=["client_session_id"],
        ),
    ]

    ledger = build_work_ledger(events)

    item = ledger["work_items"][0]
    conflict = item["log_evidence_conflict"]
    assert conflict["conflicting_keys"] == ["client_session_id"]
    assert conflict["claimed_client_session_id"] == fake
    assert conflict["evidenced_client_session_id"] == SESSION
    attr = ledger["attributions"][0]
    assert "log_evidenced_session_id_conflict" in attr["join_vetoes"]
    assert "agent-claimed session id conflicts" in attr["join_reason"]


def test_fix4_trailing_whitespace_evt_ids_rejected() -> None:
    """FIX 4: the evt-id regex uses \\Z, so a trailing newline/whitespace is
    rejected at every .match() call site (extract + index + donor summary)."""

    import re

    from agentacct.log_evidence import EVIDENCED_EVENT_ID_RE

    for bad in ("evt_deadbeef\n", "evt_deadbeef\r\n", "evt_deadbeef\t", "evt_deadbeef ", "evt_deadbeef\n\n"):
        assert EVIDENCED_EVENT_ID_RE.match(bad) is None, f"must reject {bad!r}"
    assert EVIDENCED_EVENT_ID_RE.match("evt_deadbeef") is not None

    # extract_created_event_id (call site 1): a newline-suffixed id is rejected.
    assert extract_created_event_id(json.dumps({"event": {"event_id": "evt_deadbeef\n"}})) is None
    assert extract_created_event_id(json.dumps({"event": {"event_id": "evt_deadbeef"}})) == "evt_deadbeef"

    # build_log_evidence_index (call site 2): a donor row carrying a
    # newline-suffixed id in metadata contributes no index entry.
    row = _usage_row(event_id="evt_usage_s", evidenced=["evt_deadbeef\n"])
    index = build_log_evidence_index([row])
    assert index == {}


def test_client_less_codex_section_gains_client_and_session_from_log_evidence() -> None:
    """Phase 2.10 pin of the real-store shape: codex agents commonly pass only
    `source` (no client key, no session id) — 24 such section events sat in the
    owner's global store. Log evidence implies BOTH the session id and the
    client from the donor usage row, so the section groups under its codex
    session row instead of dangling client-less. Documented as the INTENDED
    codex path (explicit `client` params are the fragile alternative: real
    agents typo'd three spellings and earned conflict vetoes)."""

    events = [
        _usage_row(event_id="evt_usage_noclient", evidenced=["evt_5ec00e0c0099"]),
        _section_event(event_id="evt_5ec00e0c0099", section_id="sec-clientless", session=None, client=None),
    ]

    ledger = build_work_ledger(events)

    attr = ledger["attributions"][0]
    assert attr["join_strategy"] == "client_log_evidenced_client_session_id"
    assert attr["join_confidence"] == "high"
    entry = _entry(ledger, "codex", SESSION)
    assert entry["join"]["state"] == "attributed"
    assert entry["work"]["counts"]["total"] == 1
    # The implied client places the item under the codex session — nothing
    # falls to the unassigned bucket.
    assert not ledger["session_rollup"]["summary"].get("unassigned_sessions")
    items = [item for item in ledger["work_items"] if item.get("section_id") == "sec-clientless"]
    assert len(items) == 1
    assert items[0]["client"] == "codex"
