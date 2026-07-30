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

import json
import sqlite3
from pathlib import Path

from agentacct.api import _refused_recording_html
from agentacct.client_usage import discover_claude_code_usage, discover_codex_usage
from agentacct.log_evidence import (
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


def test_agent_invented_argument_names_never_become_a_field() -> None:
    result = classify_refused_recording(f"MCP error -32602: unexpected argument(s): {SECRET_ARG}")
    assert result == (None, None, "unknown_argument")


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
