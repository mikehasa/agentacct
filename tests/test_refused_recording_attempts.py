"""Refused recording attempts — making agentacct's own rejections visible.

When the MCP server rejects a recording call it persists NOTHING: no event, no
counter, no log line. The only record is the client's own transcript, which the
importer already reads. These tests pin the honesty and privacy rules for the
figure derived from it:

- it counts calls **agentacct refused**, and nothing else. Client-side
  refusals, non-created-event payloads, and wire drift stay in the existing
  ``evidenced_outputs_skipped`` remainder instead of inflating it;
- every reason lands in a bounded vocabulary, and an unrecognised refusal
  becomes "other" rather than being dropped or guessed at;
- nothing derived from a refusal carries user content — the rejection path
  runs BEFORE the redactor, so the message, the rejected value, its length,
  and any path must never reach a stored row or a rendered page;
- it is retroactive by construction: nothing is persisted, so a scan of
  transcripts written long before this feature counts their refusals.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

import agentacct.client_usage as client_usage_module
import agentacct.log_evidence as log_evidence_module
import agentacct.mcp as mcp_module
import agentacct.work_ledger as work_ledger_module
from agentacct.api import (
    _REFUSED_RECORDING_REASON_LABELS,
    UsageDiscoveryConfig,
    _refused_recording_html,
    create_local_api_app,
)
from agentacct.client_usage import discover_claude_code_usage, discover_codex_usage
from agentacct.log_evidence import (
    AGENTACCT_MCP_ERROR_CODES,
    REFUSED_RECORDING_REASON_CODES,
    classify_refused_recording,
    refused_recording_rows,
    summarize_refused_recording_attempts,
)

SESSION = "019f2303-6ae1-7000-8000-0000000000c1"
CLAUDE_SESSION = "c037bd88-0000-4000-8000-0000000000c1"

# A real rejection carries the caller's own text back out. These stand in for
# the values a user would be furious to find in a world-readable file.
SECRET_PATH = "/Users/someone/private/clients/acme-contract.md"
SECRET_RUN = "run-with-a-customer-name-in-it"
SECRET_ARG = "internal_codename_bluebird"


# ---------------------------------------------------------------------------
# Codex rollout fixtures (wire shapes verified against real 0.14x rollouts)
# ---------------------------------------------------------------------------


def _mcp_err(tool: str, message: str, *, code: int = -32602) -> dict:
    """Codex mcp_tool_call_end failure branch, verbatim wire shape."""

    return {
        "Err": (
            f"tool call error: tool call failed for `agent-sentinel/{tool}`\n\n"
            f"Caused by:\n    Mcp error: -{abs(code)}: {message}"
        )
    }


def _mcp_ok_created(event_id: str) -> dict:
    payload = {"event": {"event_id": event_id, "event_type": "section_started"}}
    return {"Ok": {"content": [{"type": "text", "text": json.dumps(payload, indent=2, sort_keys=True)}]}}


def _mcp_tool_call_end(tool: str, call_id: str, result: dict) -> dict:
    return {
        "timestamp": "2026-07-05T10:00:01.000Z",
        "type": "event_msg",
        "payload": {
            "type": "mcp_tool_call_end",
            "call_id": call_id,
            "invocation": {"server": "agent-sentinel", "tool": tool, "arguments": {}},
            "duration": {"secs": 1, "nanos": 0},
            "result": result,
        },
    }


def _token_usage_line() -> dict:
    return {
        "type": "event_msg",
        "payload": {
            "info": {
                "total_token_usage": {
                    "input_tokens": 900,
                    "cached_input_tokens": 100,
                    "output_tokens": 80,
                    "reasoning_output_tokens": 0,
                }
            },
            "model": "gpt-5.5",
        },
    }


def _make_codex_home(root: Path, rollout_lines: list[dict], *, thread_id: str = SESSION) -> Path:
    codex_home = root / "codex-home"
    sessions_dir = codex_home / "sessions" / "2026" / "07" / "05"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    rollout = sessions_dir / f"rollout-2026-07-05T10-00-00-{thread_id}.jsonl"
    lines = [json.dumps({"type": "session_meta", "payload": {"id": thread_id}}), json.dumps(_token_usage_line())]
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
            (thread_id, str(rollout), 100, 300, "/work/project", "t", 980, "gpt-5.5", "0.test"),
        )
        con.execute("create table thread_spawn_edges (parent_thread_id text, child_thread_id text, status text)")
        con.commit()
    finally:
        con.close()
    return codex_home


def _claude_usage_line(session_id: str) -> dict:
    return {
        "type": "assistant",
        "sessionId": session_id,
        "cwd": "/work/project",
        "message": {
            "model": "claude-opus-4-8",
            "usage": {
                "input_tokens": 30,
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
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": [{"type": "text", "text": text}]}
            ],
        },
    }


def _rows_by_reason(summary: dict) -> dict[str, int]:
    return dict(summary["by_reason"])


# ---------------------------------------------------------------------------
# Classification: bounded vocabulary, unmatched text is kept as "other"
# ---------------------------------------------------------------------------


def test_every_observed_refusal_shape_maps_to_a_bounded_reason_and_field() -> None:
    cases = [
        ("MCP error -32602: summary must be <= 1200 characters", None, "summary", "narrative_over_limit"),
        ("MCP error -32602: files[0] must be project-relative", None, "files", "files_not_project_relative"),
        ("MCP error -32602: metadata must be <= 8192 bytes when JSON encoded", None, "metadata", "metadata_over_size"),
        ("MCP error -32602: unexpected argument(s): project_dir", None, "project_dir", "unknown_argument"),
        ("MCP error -32602: missing required argument: section_id", None, "section_id", "missing_argument"),
        ("MCP error -32602: kind must be one of: docs, other", None, "kind", "invalid_argument"),
        ("MCP error -32602: limit must be <= 200", None, "limit", "value_over_limit"),
        ("MCP error -32004: no runs found", None, None, "no_runs"),
        (f"MCP error -32004: unknown run_id: {SECRET_RUN}", None, "run_id", "unknown_run_id"),
        (
            "MCP error -32602: resolves_blocked_event_id, resolution_scope, and resolution_summary"
            " must be supplied together",
            None,
            "resolves_blocked_event_id",
            "incomplete_argument_group",
        ),
    ]
    for text, tool, expected_field, expected_reason in cases:
        result = classify_refused_recording(text, tool=tool)
        assert result is not None, text
        got_tool, got_field, got_reason = result
        assert got_reason in REFUSED_RECORDING_REASON_CODES
        assert (got_field, got_reason) == (expected_field, expected_reason), text


def test_unrecognised_refusal_text_lands_in_other_and_is_not_dropped() -> None:
    result = classify_refused_recording("MCP error -32602: a brand new message this build never saw")
    assert result is not None
    tool, field, reason = result
    assert reason == "other"
    assert field is None


def test_client_side_refusals_are_not_counted_as_agentacct_refusals() -> None:
    # codex's own approval layer never reached agentacct.
    assert (
        classify_refused_recording(
            '{"Err": "This action was rejected due to unacceptable risk.\\nReason: usage limit"}'
        )
        is None
    )
    # Claude Code's input validator, which echoes the payload it rejected.
    assert (
        classify_refused_recording(
            "<tool_use_error>InputValidationError: mcp__agentacct__sentinel_record_section was called"
            f" with input that could not be parsed as JSON.\nYou sent {SECRET_PATH}"
        )
        is None
    )


def test_a_quoted_agentacct_error_inside_a_payload_is_not_a_refusal() -> None:
    """Position, not presence: an error is a refusal only where the wire puts it.

    Agents narrate. "green after fixing MCP error -32602: ..." is a SUCCESSFUL
    recording whose own text quotes an old failure, and a scan of the whole
    output counts it as a call agentacct refused — the one direction this
    figure must never lie in.
    """

    quoted = (
        "green after fixing MCP error -32602: files[0] must be project-relative"
        f" on {SECRET_PATH}"
    )
    assert classify_refused_recording(quoted, tool="agentacct_record_machine_check") is None
    assert classify_refused_recording(json.dumps({"outcome": {"summary": quoted}})) is None
    # A quote sitting mid-payload is not rescued by being on its own line.
    assert classify_refused_recording(f"pytest passed\n{quoted}\nbuild ok") is None
    # ...while both real wire positions still classify.
    assert classify_refused_recording("MCP error -32602: files[0] must be project-relative") is not None
    assert (
        classify_refused_recording(
            "<tool_use_error>MCP error -32602: files[0] must be project-relative"
        )
        is not None
    )
    assert (
        classify_refused_recording(
            "tool call error: tool call failed for `agent-sentinel/sentinel_record_section`"
            "\n\nCaused by:\n    Mcp error: -32602: files[0] must be project-relative"
        )
        is not None
    )


def test_a_successful_call_quoting_an_error_is_not_counted_in_the_scan(tmp_path) -> None:
    """The same rule, end to end: three successes must count as zero refusals."""

    claude_home = tmp_path / "claude-home"
    project = claude_home / "projects" / "-work-project"
    project.mkdir(parents=True)
    # An outcome-only machine_check response: a real success shape that donates
    # no created-event id, so it reaches the refusal classifier as a skip.
    succeeded = json.dumps(
        {
            "outcome": {
                "machine_checks": {
                    "checks": [
                        {
                            "name": "pytest",
                            "after": {
                                "exit_code": 0,
                                "summary": "green after fixing MCP error -32602: files[0] must be"
                                f" project-relative on {SECRET_PATH}",
                            },
                        }
                    ]
                }
            }
        }
    )
    lines = [_claude_usage_line(CLAUDE_SESSION)]
    for index in range(3):
        lines.append(
            _claude_tool_use_line(
                CLAUDE_SESSION,
                tool_use_id=f"toolu_{index}",
                name="mcp__agentacct__agentacct_record_machine_check",
            )
        )
        lines.append(
            _claude_tool_result_line(CLAUDE_SESSION, tool_use_id=f"toolu_{index}", text=succeeded)
        )
    (project / f"{CLAUDE_SESSION}.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )

    events = discover_claude_code_usage(claude_home=claude_home, limit_sessions=10)
    summary = summarize_refused_recording_attempts(events)

    assert summary["refused_attempt_total"] == 0
    assert summary["rows"] == []
    assert summary["sessions_with_refusals"] == 0
    # They stay in the generic skip counter, disclosed as the remainder — the
    # two figures still reconcile, they just no longer accuse a success.
    assert summary["outputs_skipped"] == 3
    assert summary["unclassified_outputs_skipped"] == 3

    html = _refused_recording_html(summary, lambda value: str(value))
    assert "No refused recording calls were found" in html
    assert "refused by agentacct" not in html


def test_a_client_transport_failure_is_not_a_refusal() -> None:
    """WHOSE error it is, not only where it sits.

    A JSON-RPC error rendered in the anchor position can come from the client's
    own transport instead of this server: the call timed out, or the connection
    dropped. Nothing reached agentacct, so nothing was refused — reporting one
    as "agentacct refused this recording call" is the same overstatement as
    counting a quoted error, just arriving from the other side of the wire.
    """

    transport = [
        "MCP error -32001: Request timed out",
        "MCP error -32603: Internal error",
        "MCP error -32000: Connection closed",
        "MCP error -32601: Method not found",
        "MCP error -32700: Parse error",
        "MCP error -32600: Invalid Request",
        "<tool_use_error>MCP error -32001: Request timed out",
        "tool call error: tool call failed for `agent-sentinel/sentinel_record_section`"
        "\n\nCaused by:\n    Mcp error: -32001: Request timed out",
    ]
    for text in transport:
        assert classify_refused_recording(text, tool="agentacct_record_section") is None, text


def test_every_error_code_agentacct_itself_returns_still_classifies() -> None:
    """The mirror of the transport test: the fix must not silence the server.

    mcp.py answers a tools/call with -32602 (InvalidParams and ValueError,
    which is also how "Unknown tool" comes back), -32004 (FileNotFoundError)
    and -32000 (any other exception, plus the store-less degraded server that
    stays connected precisely so it can refuse). All three are agentacct
    refusing a recording call and all three must be counted.
    """

    assert AGENTACCT_MCP_ERROR_CODES == {-32000, -32004, -32602}
    for code in sorted(AGENTACCT_MCP_ERROR_CODES):
        result = classify_refused_recording(
            f"MCP error {code}: summary must be <= 1200 characters",
            tool="agentacct_record_section",
        )
        assert result == ("agentacct_record_section", "summary", "narrative_over_limit"), code
    # The degraded server's own refusal carries no argument, but it is still a
    # recording call this product turned down.
    degraded = classify_refused_recording(
        "MCP error -32000: agentacct is connected but has no usable store; restart the MCP"
        " server with a writable --store-dir <abs path>",
        tool="agentacct_record_section",
    )
    assert degraded == ("agentacct_record_section", None, "other")


def test_the_accepted_code_set_still_matches_what_the_server_emits() -> None:
    """The set is copied out of mcp.py, so it can drift out of it.

    A code the server starts returning that this list does not know about is a
    silent under-count; a code it stops returning is dead weight. Read the
    codes back off ``_error``'s call sites rather than off its wording, which
    is the whole point of keying on the code.

    ``-32601`` is the one deliberate exclusion: it answers an unknown METHOD,
    which no recording tool call can produce (an unknown TOOL comes back as a
    ValueError, so -32602).
    """

    tree = ast.parse(Path(mcp_module.__file__).read_text(encoding="utf-8"))
    emitted: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "_error":
            continue
        code = node.args[1]
        if isinstance(code, ast.UnaryOp) and isinstance(code.op, ast.USub):
            emitted.add(-code.operand.value)

    assert emitted, "no _error call sites found — the scan, not the server, is what broke"
    assert emitted - {-32601} == set(AGENTACCT_MCP_ERROR_CODES)


def test_a_transport_failure_and_a_refusal_land_in_different_buckets(tmp_path) -> None:
    """End to end on the real Claude Code channel, one session, two calls."""

    claude_home = tmp_path / "claude-home"
    project = claude_home / "projects" / "-work-project"
    project.mkdir(parents=True)
    lines = [_claude_usage_line(CLAUDE_SESSION)]
    for tool_use_id, text in (
        ("toolu_timeout", "MCP error -32001: Request timed out"),
        ("toolu_refused", f"MCP error -32602: files[0] must be project-relative: {SECRET_PATH}"),
    ):
        lines.append(
            _claude_tool_use_line(
                CLAUDE_SESSION,
                tool_use_id=tool_use_id,
                name="mcp__agentacct__agentacct_record_section",
            )
        )
        lines.append(_claude_tool_result_line(CLAUDE_SESSION, tool_use_id=tool_use_id, text=text))
    (project / f"{CLAUDE_SESSION}.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )

    summary = summarize_refused_recording_attempts(
        discover_claude_code_usage(claude_home=claude_home, limit_sessions=10)
    )

    assert summary["outputs_skipped"] == 2
    assert summary["refused_attempt_total"] == 1
    assert _rows_by_reason(summary) == {"files_not_project_relative": 1}
    # The timeout is not dropped — it is disclosed as the remainder, which is
    # what keeps the two figures reconciling instead of disagreeing.
    assert summary["unclassified_outputs_skipped"] == 1


def test_a_refusal_still_counts_behind_blank_and_wrapper_only_lines() -> None:
    """The anchor is a position, not a line number.

    Three real shapes put nothing on the first line: a tool_result that opens
    with a newline, Claude Code's ``<tool_use_error>`` written on a line of its
    own rather than inline, and codex's cause separated from its "Caused by:"
    header by a blank line. Each was a silent under-count.
    """

    message = "summary must be <= 1200 characters"
    shapes = [
        f"\nMCP error -32602: {message}",
        f"<tool_use_error>\nMCP error -32602: {message}\n</tool_use_error>",
        f"\n\n<tool_use_error>\n  MCP error -32602: {message}",
        "tool call error: tool call failed for `agent-sentinel/sentinel_record_section`"
        f"\n\nCaused by:\n\n    Mcp error: -32602: {message}",
    ]
    for text in shapes:
        result = classify_refused_recording(text, tool="agentacct_record_section")
        assert result == ("agentacct_record_section", "summary", "narrative_over_limit"), text

    # Widening WHAT is recognised must not widen WHERE it is looked for: a
    # payload that merely quotes an error is still not a refusal.
    assert (
        classify_refused_recording(
            f"\npytest passed\ngreen after fixing MCP error -32602: {message}\nbuild ok",
            tool="agentacct_record_machine_check",
        )
        is None
    )


def test_a_leading_caused_by_line_does_not_anchor_the_line_below_it() -> None:
    """"Caused by:" is a wrapper's header — it has to wrap something.

    With nothing above it, it is just the first line of some payload, and
    letting it nominate the second line lets any text opt itself in.
    """

    assert (
        classify_refused_recording(
            "Caused by:\n    Mcp error: -32602: summary must be <= 1200 characters",
            tool="agentacct_record_section",
        )
        is None
    )
    assert (
        classify_refused_recording(
            "\n\nCaused by:\n    Mcp error: -32602: summary must be <= 1200 characters",
            tool="agentacct_record_section",
        )
        is None
    )
    # codex's real shape always has its own wrapper line above the header.
    assert (
        classify_refused_recording(
            "tool call error: tool call failed for `agent-sentinel/sentinel_record_section`"
            "\n\nCaused by:\n    Mcp error: -32602: summary must be <= 1200 characters"
        )
        is not None
    )


def test_agent_invented_argument_names_never_become_a_field() -> None:
    result = classify_refused_recording(f"MCP error -32602: unexpected argument(s): {SECRET_ARG}")
    assert result == (None, None, "unknown_argument")


def test_minimum_bound_refusals_are_never_labelled_as_over_the_maximum() -> None:
    """A label that inverts the refusal is worse than no label at all.

    ``input_tokens=-5`` is rejected with "input_tokens must be >= 0"; reporting
    that as "Value above the allowed maximum" tells the user to shrink a number
    they need to raise.
    """

    cases = [
        ("input_tokens must be >= 0", "input_tokens", "value_under_limit"),
        ("turn_index must be >= 0", "turn_index", "value_under_limit"),
        ("budget_usd must be > 0", "budget_usd", "value_under_limit"),
        ("cost_usd must be a finite number >= 0", "cost_usd", "value_under_limit"),
        # The maximum bound keeps its own code, so the two stay distinguishable.
        ("limit must be <= 200", "limit", "value_over_limit"),
    ]
    for message, expected_field, expected_reason in cases:
        result = classify_refused_recording(f"MCP error -32602: {message}")
        assert result is not None, message
        _tool, field, reason = result
        assert reason in REFUSED_RECORDING_REASON_CODES
        assert (field, reason) == (expected_field, expected_reason), message

    rows = refused_recording_rows(
        {("agentacct_record_agent_usage_debug", "input_tokens", "value_under_limit"): 1}
    )
    html = _refused_recording_html(
        {"refused_attempt_total": 1, "sessions_with_refusals": 1, "rows": rows},
        lambda value: str(value),
    )
    assert "Value below the allowed minimum" in html
    assert "Value above the allowed maximum" not in html


def test_every_frozen_reason_code_has_plain_language_copy() -> None:
    """Guard against the drift that produced the inverted label.

    A code the table has no wording for still renders (as the bare code), so
    this is not about breakage — it is about the surface never again showing a
    sentence that describes a different refusal than the one that happened.
    """

    for reason_code in REFUSED_RECORDING_REASON_CODES:
        assert reason_code in _REFUSED_RECORDING_REASON_LABELS, reason_code


def test_minimum_bound_refusal_names_the_tool_that_was_refused(tmp_path) -> None:
    """Tool attribution survives this shape on both live client channels."""

    message = "input_tokens must be >= 0"
    tool = "agentacct_record_agent_usage_debug"

    claude_home = tmp_path / "claude-home"
    project = claude_home / "projects" / "-work-project"
    project.mkdir(parents=True)
    lines = [
        _claude_usage_line(CLAUDE_SESSION),
        _claude_tool_use_line(CLAUDE_SESSION, tool_use_id="toolu_a", name=f"mcp__agentacct__{tool}"),
        _claude_tool_result_line(CLAUDE_SESSION, tool_use_id="toolu_a", text=f"MCP error -32602: {message}"),
    ]
    (project / f"{CLAUDE_SESSION}.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )
    claude_summary = summarize_refused_recording_attempts(
        discover_claude_code_usage(claude_home=claude_home, limit_sessions=10)
    )

    codex_home = _make_codex_home(tmp_path, [_mcp_tool_call_end(tool, "call_a", _mcp_err(tool, message))])
    codex_summary = summarize_refused_recording_attempts(
        discover_codex_usage(codex_home=codex_home, limit_sessions=10)
    )

    expected = [{"tool": tool, "field": "input_tokens", "reason_code": "value_under_limit", "count": 1}]
    assert claude_summary["rows"] == expected
    assert codex_summary["rows"] == expected


def test_refused_recording_rows_revalidate_untrusted_input() -> None:
    rows = refused_recording_rows(
        {
            ("not_a_real_tool", SECRET_ARG, "made_up_reason"): 3,
            ("sentinel_record_section", "files", "files_not_project_relative"): 2,
        }
    )
    rendered = json.dumps(rows)
    assert SECRET_ARG not in rendered
    assert "not_a_real_tool" not in rendered
    assert {"tool": None, "field": None, "reason_code": "other", "count": 3} in rows


# ---------------------------------------------------------------------------
# End-to-end over a client log: counts, breakdown, reconciliation
# ---------------------------------------------------------------------------


def test_codex_scan_counts_each_refusal_reason_and_reconciles_with_skips(tmp_path) -> None:
    lines = [
        _mcp_tool_call_end("sentinel_record_section", "call_a", _mcp_err("sentinel_record_section", "files[0] must be project-relative")),
        _mcp_tool_call_end("sentinel_record_section", "call_b", _mcp_err("sentinel_record_section", "files[1] must be project-relative")),
        _mcp_tool_call_end("sentinel_record_machine_check", "call_c", _mcp_err("sentinel_record_machine_check", "no runs found", code=-32004)),
        _mcp_tool_call_end("sentinel_record_section", "call_d", _mcp_err("sentinel_record_section", "summary must be <= 1200 characters")),
        _mcp_tool_call_end("sentinel_record_section", "call_e", _mcp_err("sentinel_record_section", f"unexpected argument(s): {SECRET_ARG}")),
        _mcp_tool_call_end("sentinel_record_machine_check", "call_f", _mcp_err("sentinel_record_machine_check", f"unknown run_id: {SECRET_RUN}", code=-32004)),
        _mcp_tool_call_end("sentinel_record_event", "call_g", _mcp_err("sentinel_record_event", "an unmapped future message")),
        # A refusal the CLIENT made: a real skip, but agentacct never saw it.
        _mcp_tool_call_end(
            "sentinel_record_section",
            "call_h",
            {"Err": "This action was rejected due to unacceptable risk.\nReason: usage limit"},
        ),
        # A successful call: neither skipped nor refused.
        _mcp_tool_call_end("sentinel_record_section", "call_i", _mcp_ok_created("evt_aaa111bbb222")),
    ]
    codex_home = _make_codex_home(tmp_path, lines)

    events = discover_codex_usage(codex_home=codex_home, limit_sessions=10)
    assert len(events) == 1
    event = events[0]

    summary = summarize_refused_recording_attempts(events)
    assert summary["refused_attempt_total"] == 7
    assert summary["sessions_with_refusals"] == 1
    assert _rows_by_reason(summary) == {
        "files_not_project_relative": 2,
        "narrative_over_limit": 1,
        "no_runs": 1,
        "other": 1,
        "unknown_argument": 1,
        "unknown_run_id": 1,
    }
    # The existing generic counter saw all eight non-donating outputs; the one
    # it holds beyond the refusals is the client-side rejection, reported as a
    # remainder instead of being folded into "agentacct refused this".
    assert event.evidenced_outputs_skipped == 8
    assert summary["outputs_skipped"] == 8
    assert summary["unclassified_outputs_skipped"] == 1
    assert list(event.evidenced_event_ids) == ["evt_aaa111bbb222"]

    tools = {row["tool"] for row in summary["rows"]}
    assert tools == {"sentinel_record_section", "sentinel_record_machine_check", "sentinel_record_event"}


def test_claude_scan_names_the_refused_tool_and_field(tmp_path) -> None:
    claude_home = tmp_path / "claude-home"
    project = claude_home / "projects" / "-work-project"
    project.mkdir(parents=True)
    lines = [
        _claude_usage_line(CLAUDE_SESSION),
        _claude_tool_use_line(CLAUDE_SESSION, tool_use_id="toolu_a", name="mcp__agentacct__agentacct_record_section"),
        _claude_tool_result_line(
            CLAUDE_SESSION,
            tool_use_id="toolu_a",
            text="MCP error -32602: summary must be <= 1200 characters",
        ),
        _claude_tool_use_line(CLAUDE_SESSION, tool_use_id="toolu_b", name="mcp__agentacct__agentacct_record_machine_check"),
        _claude_tool_result_line(CLAUDE_SESSION, tool_use_id="toolu_b", text="MCP error -32004: no runs found"),
    ]
    (project / f"{CLAUDE_SESSION}.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )

    events = discover_claude_code_usage(claude_home=claude_home, limit_sessions=10)
    summary = summarize_refused_recording_attempts(events)

    assert summary["refused_attempt_total"] == 2
    assert {
        (row["tool"], row["field"], row["reason_code"]) for row in summary["rows"]
    } == {
        ("agentacct_record_section", "summary", "narrative_over_limit"),
        ("agentacct_record_machine_check", None, "no_runs"),
    }


def test_claude_model_lanes_do_not_multiply_one_session_of_refusals(tmp_path) -> None:
    """The same evidence triple rides every per-model row of one transcript."""

    claude_home = tmp_path / "claude-home"
    project = claude_home / "projects" / "-work-project"
    project.mkdir(parents=True)
    opus = _claude_usage_line(CLAUDE_SESSION)
    haiku = json.loads(json.dumps(opus))
    haiku["message"]["model"] = "claude-haiku-4-5"
    lines = [
        opus,
        haiku,
        _claude_tool_use_line(CLAUDE_SESSION, tool_use_id="toolu_a", name="mcp__agentacct__agentacct_record_section"),
        _claude_tool_result_line(
            CLAUDE_SESSION,
            tool_use_id="toolu_a",
            text="MCP error -32602: summary must be <= 1200 characters",
        ),
    ]
    (project / f"{CLAUDE_SESSION}.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )

    events = discover_claude_code_usage(claude_home=claude_home, limit_sessions=10)
    assert len({event.usage_row_lane for event in events}) == 2

    summary = summarize_refused_recording_attempts(events)
    assert summary["refused_attempt_total"] == 1
    assert summary["sessions_with_refusals"] == 1


# ---------------------------------------------------------------------------
# Privacy: the rejection path is pre-redaction, so nothing may leak
# ---------------------------------------------------------------------------


def test_no_user_content_reaches_the_stored_row_or_the_rendered_page(tmp_path) -> None:
    secrets = (SECRET_PATH, SECRET_RUN, SECRET_ARG)
    lines = [
        _mcp_tool_call_end(
            "sentinel_record_section",
            "call_a",
            _mcp_err("sentinel_record_section", f"files[0] must be project-relative: {SECRET_PATH}"),
        ),
        _mcp_tool_call_end(
            "sentinel_record_machine_check",
            "call_b",
            _mcp_err("sentinel_record_machine_check", f"unknown run_id: {SECRET_RUN}", code=-32004),
        ),
        _mcp_tool_call_end(
            "sentinel_record_section",
            "call_c",
            _mcp_err("sentinel_record_section", f"unexpected argument(s): {SECRET_ARG}"),
        ),
        _mcp_tool_call_end(
            "sentinel_record_section",
            "call_d",
            _mcp_err("sentinel_record_section", "summary must be <= 1200 characters (received 4211)"),
        ),
    ]
    codex_home = _make_codex_home(tmp_path, lines)
    events = discover_codex_usage(codex_home=codex_home, limit_sessions=10)
    event = events[0]

    # 1. Nothing about a refusal is persisted at all — not the message, and
    #    not even the bounded breakdown (it is re-derived on every scan).
    stored = json.dumps(event.to_sentinel_event())
    for secret in secrets:
        assert secret not in stored
    assert "refused_recording" not in stored

    # 2. The in-memory breakdown carries only the bounded triple.
    for row in event.refused_recording_attempts:
        assert set(row) == {"tool", "field", "reason_code", "count"}
    breakdown = json.dumps(list(event.refused_recording_attempts))
    for secret in secrets:
        assert secret not in breakdown
    # A "received N" length is user-derived measurement, never carried out.
    assert "4211" not in breakdown

    # 3. The rendered page carries only the bounded triple too.
    summary = summarize_refused_recording_attempts(events)
    html = _refused_recording_html(summary, lambda value: str(value))
    for secret in secrets:
        assert secret not in html
    assert "4211" not in html
    assert "Recording calls agentacct refused" in html


def test_rendered_copy_says_agentacct_refused_these_not_the_user(tmp_path) -> None:
    lines = [
        _mcp_tool_call_end(
            "sentinel_record_section",
            "call_a",
            _mcp_err("sentinel_record_section", "files[0] must be project-relative"),
        ),
        _mcp_tool_call_end(
            "sentinel_record_section",
            "call_b",
            {"Err": "This action was rejected due to unacceptable risk.\nReason: usage limit"},
        ),
    ]
    codex_home = _make_codex_home(tmp_path, lines)
    events = discover_codex_usage(codex_home=codex_home, limit_sessions=10)
    summary = summarize_refused_recording_attempts(events)

    html = _refused_recording_html(summary, lambda value: str(value))
    assert "refused by agentacct" in html
    assert "not work you failed to" in html
    # The remainder is disclosed rather than quietly added to the total.
    assert "1 scanned output(s) donated no recorded event but are NOT counted above" in html


def test_empty_scan_renders_an_honest_zero_state() -> None:
    html = _refused_recording_html(summarize_refused_recording_attempts([]), lambda value: str(value))
    assert "No refused recording calls were found" in html


# ---------------------------------------------------------------------------
# The surface itself: the section has to reach the page over HTTP
# ---------------------------------------------------------------------------


def _raw_page(store_root: Path, home: Path) -> str:
    client = TestClient(
        create_local_api_app(
            store_dir=store_root,
            usage_discovery=UsageDiscoveryConfig.from_home(home),
        )
    )
    response = client.get("/raw", headers={"Accept": "text/html"})
    assert response.status_code == 200
    return response.text


def test_raw_route_renders_the_refusal_section_with_its_rows(tmp_path) -> None:
    """Asserted over HTTP, not on the helper.

    Every other test here calls ``_refused_recording_html`` directly, so
    deleting the one line that injects it into /raw leaves the whole suite
    green while the feature is gone from the product.
    """

    home = tmp_path / "home"
    project = home / ".claude" / "projects" / "-work-project"
    project.mkdir(parents=True)
    lines = [
        _claude_usage_line(CLAUDE_SESSION),
        _claude_tool_use_line(
            CLAUDE_SESSION, tool_use_id="toolu_a", name="mcp__agentacct__agentacct_record_section"
        ),
        _claude_tool_result_line(
            CLAUDE_SESSION,
            tool_use_id="toolu_a",
            text=f"MCP error -32602: files[0] must be project-relative: {SECRET_PATH}",
        ),
    ]
    (project / f"{CLAUDE_SESSION}.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )

    page = _raw_page(tmp_path / "state", home)

    assert 'id="refused-recording-attempts"' in page
    assert "Recording calls agentacct refused" in page
    assert "1 recording call(s) across 1 client session(s) were refused by agentacct." in page
    assert "agentacct_record_section" in page
    assert "File path was not project-relative" in page
    # The page is a rendering of the same pre-redaction scan, so the privacy
    # rule has to hold at the route too, not only in the helper.
    assert SECRET_PATH not in page


def test_raw_route_renders_the_refusal_section_on_a_clean_machine(tmp_path) -> None:
    """No refusals is a state to report, not a reason to drop the section."""

    page = _raw_page(tmp_path / "state", tmp_path / "home")

    assert 'id="refused-recording-attempts"' in page
    assert "No refused recording calls were found" in page


# ---------------------------------------------------------------------------
# Cross-file copy: the next step points at a section that must exist
# ---------------------------------------------------------------------------


def _string_constants_containing(module, needle: str) -> list[str]:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and needle in node.value
    ]


def test_the_next_step_sentence_names_a_section_the_page_really_renders() -> None:
    """The sentence sends the user somewhere; that somewhere is in another file.

    ``work_ledger`` tells a user with unexplained usage to look under a named
    heading in Local logs, and ``api`` is what renders that heading. Rewording
    either alone leaves the ledger directing people at a section that is not
    there — a next step that cannot be followed is worse than none.
    """

    sentence = work_ledger_module._USAGE_WITHOUT_MCP_CONTEXT_NEXT_STEP
    quoted = re.findall(r'"([^"]+)"', sentence)
    assert quoted, sentence
    heading = quoted[0]

    # Rendered in both states, so the reader who follows the pointer lands on
    # the section whether or not they have refusals.
    esc = lambda value: str(value)  # noqa: E731 - the route's escaper, not under test here
    assert heading in _refused_recording_html({}, esc)
    assert heading in _refused_recording_html(
        {
            "refused_attempt_total": 1,
            "sessions_with_refusals": 1,
            "rows": refused_recording_rows({("agentacct_record_section", "summary", "narrative_over_limit"): 1}),
        },
        esc,
    )

    # One copy of the sentence, so the attention group and the blind spot
    # cannot be reworded apart from each other.
    assert work_ledger_module._ATTENTION_GROUP_NEXT_STEPS["usage_truth_without_mcp_context"] is sentence
    assert len(_string_constants_containing(work_ledger_module, heading)) == 1


# ---------------------------------------------------------------------------
# Retroactivity: the whole point
# ---------------------------------------------------------------------------


def test_refusals_recorded_before_this_feature_existed_are_counted(tmp_path) -> None:
    """Nothing is persisted, so there is nothing to backfill.

    The fixture is a pre-rename ``sentinel_*`` rollout with no agentacct store
    anywhere: exactly the state of a transcript written months before this
    surface existed. A plain scan still produces the full breakdown.
    """

    lines = [
        _mcp_tool_call_end(
            "sentinel_record_section",
            "call_old_a",
            _mcp_err("sentinel_record_section", "files[0] must be project-relative"),
        ),
        _mcp_tool_call_end(
            "sentinel_record_machine_check",
            "call_old_b",
            _mcp_err("sentinel_record_machine_check", "no runs found", code=-32004),
        ),
    ]
    codex_home = _make_codex_home(tmp_path, lines)

    # No store, no import, no prior agentacct state of any kind.
    assert not (tmp_path / "state").exists()

    summary = summarize_refused_recording_attempts(
        discover_codex_usage(codex_home=codex_home, limit_sessions=10)
    )
    assert summary["refused_attempt_total"] == 2
    assert _rows_by_reason(summary) == {"files_not_project_relative": 1, "no_runs": 1}


# ---------------------------------------------------------------------------
# Module hygiene: the repo runs no linter, so a shadowed import needs a test
# ---------------------------------------------------------------------------


def _rebound_module_level_imports(module) -> list[str]:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    seen: set[str] = set()
    rebound: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            if name in seen:
                rebound.append(name)
            seen.add(name)
    return rebound


def test_this_track_imports_no_name_twice() -> None:
    """A second import of the same name silently replaces the first.

    ``from typing import Mapping`` after ``from collections.abc import Mapping``
    changes what every annotation in the file means, and no CI job here would
    notice: the repo has no lint step.
    """

    for module in (client_usage_module, log_evidence_module):
        assert _rebound_module_level_imports(module) == [], module.__name__
