from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import agentacct.cli as cli_module
import agentacct.client_usage as client_usage_module
from agentacct.api import UsageDiscoveryConfig, _task_title, create_local_api_app
from agentacct.cli import app
from agentacct.client_usage import (
    ClientSessionObservation,
    ClientUsageDiscoveryResult,
    ClientUsageEvent,
    apply_pricing_estimate_to_event,
    describe_scanned_client_homes,
    discover_claude_code_usage,
    discover_client_usage_with_diagnostics,
    discover_codex_usage,
    discover_hermes_usage,
    discover_opencode_usage,
    discover_openclaw_usage,
    plan_local_usage_import,
    usage_less_session_observations,
)
from agentacct.pricing_catalog import default_pricing_catalog_snapshot_path
from agentacct.refreshable_usage import refreshable_usage_source_order
from agentacct.service import SentinelService
from agentacct.source_discovery import discover_usage_sources
from agentacct.store_resolution import ENV_STORE_DIR, LEGACY_ENV_STORE_DIR
from agentacct.usage_truth import (
    CODEX_LINEAGE_DELTA_SEMANTICS,
    CODEX_REPLAY_QUARANTINE_STATE,
    is_local_session_observation_event,
    mark_trusted_local_usage_import_event,
)


def _make_codex_home(root: Path, *, model: str = "gpt-5.5") -> Path:
    codex_home = root / "codex-home"
    sessions = codex_home / "sessions" / "2026" / "06" / "27"
    sessions.mkdir(parents=True)
    rollout = sessions / "rollout-2026-06-27T00-00-00-session-abc.jsonl"
    rollout.write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": "session-abc"}}),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 1500,
                                    "cached_input_tokens": 400,
                                    "output_tokens": 75,
                                    "reasoning_output_tokens": 11,
                                    "total_tokens": 1575,
                                }
                            }
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 2500,
                                    "cached_input_tokens": 900,
                                    "output_tokens": 125,
                                    "reasoning_output_tokens": 22,
                                    "total_tokens": 2625,
                                },
                                "last_token_usage": {
                                    "input_tokens": 1000,
                                    "cached_input_tokens": 500,
                                    "output_tokens": 50,
                                    "reasoning_output_tokens": 11,
                                    "total_tokens": 1050,
                                },
                            },
                            "model": model,
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    db = codex_home / "state_5.sqlite"
    con = sqlite3.connect(db)
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
            (
                "session-abc",
                str(rollout),
                100,
                200,
                "/work/project",
                "PRIVATE FIRST PROMPT MUST NEVER BECOME A TITLE",
                2625,
                model,
                "0.test",
            ),
        )
        con.commit()
    finally:
        con.close()
    (codex_home / "session_index.jsonl").write_text(
        json.dumps(
            {
                "id": "session-abc",
                "thread_name": "A useful Codex session",
                "updated_at": "2026-06-27T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return codex_home


def _add_codex_thread(
    codex_home: Path,
    *,
    session_id: str,
    updated_at: int,
    model: str,
    parent_session_id: str | None = None,
) -> None:
    sessions = codex_home / "sessions" / "2026" / "06" / "27"
    rollout = sessions / f"rollout-2026-06-27T00-00-00-{session_id}.jsonl"
    session_meta: dict[str, object] = {"id": session_id}
    if parent_session_id is not None:
        session_meta["parent_thread_id"] = parent_session_id
    rollout.write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": session_meta}),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 300,
                                    "cached_input_tokens": 100,
                                    "output_tokens": 20,
                                    "reasoning_output_tokens": 5,
                                }
                            },
                            "model": model,
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    con = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        con.execute(
            """
            insert into threads (
                id, rollout_path, created_at, updated_at, cwd, title,
                tokens_used, model, cli_version
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                str(rollout),
                updated_at - 10,
                updated_at,
                "/work/project",
                "PRIVATE FIRST PROMPT",
                320,
                model,
                "0.test",
            ),
        )
        con.commit()
    finally:
        con.close()


def _codex_counters(
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_output_tokens: int,
) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _codex_token_count(
    timestamp: str,
    total: dict[str, int],
    *,
    last: dict[str, int] | None = None,
    model: str = "gpt-5.5",
) -> dict[str, object]:
    info: dict[str, object] = {"total_token_usage": total}
    if last is not None:
        info["last_token_usage"] = last
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "model": model,
            "info": info,
        },
    }


def _rewrite_codex_rollout(
    codex_home: Path,
    session_id: str,
    rows: list[dict[str, object]],
) -> None:
    rollout = next((codex_home / "sessions").rglob(f"*{session_id}.jsonl"))
    rollout.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def _make_claude_home(root: Path) -> Path:
    claude_home = root / "claude-home"
    project = claude_home / "projects" / "-tmp-project"
    project.mkdir(parents=True)
    session = project / "claude-session.jsonl"
    rows = [
        {"type": "ai-title", "sessionId": "claude-session", "aiTitle": "A useful Claude session"},
        {
            "type": "assistant",
            "sessionId": "claude-session",
            "cwd": "/work/project",
            "timestamp": "2026-06-20T10:00:00Z",
            "message": {
                "model": "claude-opus-4-8",
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 30,
                    "cache_read_input_tokens": 40,
                    "output_tokens": 5,
                },
            },
        },
        {
            "type": "assistant",
            "sessionId": "claude-session",
            "cwd": "/work/project",
            "timestamp": "2026-06-21T10:00:00Z",
            "message": {
                "model": "claude-opus-4-8",
                "usage": {
                    "input_tokens": 20,
                    "cache_creation_input_tokens": 3,
                    "cache_read_input_tokens": 4,
                    "output_tokens": 7,
                },
            },
        },
    ]
    session.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return claude_home


def _make_opencode_home(root: Path) -> Path:
    opencode_home = root / "opencode-home"
    session_dir = opencode_home / "sessions"
    session_dir.mkdir(parents=True)
    stream = session_dir / "ses_example.jsonl"
    rows = [
        {"type": "step_start", "sessionID": "ses_example"},
        {
            "type": "step_finish",
            "sessionID": "ses_example",
            "part": {
                "type": "step-finish",
                "tokens": {"total": 8678, "input": 8449, "output": 229, "reasoning": 0, "cache": {"write": 0, "read": 0}},
                "cost": 0.00124698,
            },
        },
        {
            "type": "step_finish",
            "sessionID": "ses_example",
            "part": {
                "type": "step-finish",
                "tokens": {"total": 14947, "input": 216, "output": 11, "reasoning": 0, "cache": {"write": 0, "read": 14720}},
                "cost": 0.000074536,
            },
        },
    ]
    stream.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return opencode_home


_OPENCODE_SESSION_COLUMNS = (
    "id",
    "project_id",
    "parent_id",
    "directory",
    "title",
    "cost",
    "tokens_input",
    "tokens_output",
    "tokens_reasoning",
    "tokens_cache_read",
    "tokens_cache_write",
    "model",
    "time_created",
    "time_updated",
)


def _make_opencode_db_home(
    root: Path,
    *,
    rows: list[dict[str, object]] | None = None,
    columns: tuple[str, ...] = _OPENCODE_SESSION_COLUMNS,
    db_name: str = "opencode.db",
    dir_name: str = "opencode-db-home",
) -> Path:
    """Build an OpenCode data home whose native ``session`` rollup is populated."""

    opencode_home = root / dir_name
    opencode_home.mkdir(parents=True)
    if rows is None:
        rows = [
            {
                "id": "ses_alpha",
                "project_id": "global",
                "parent_id": None,
                "directory": "/Users/dev/project",
                "title": "session alpha",
                "cost": 0.0,
                "tokens_input": 19589,
                "tokens_output": 516,
                "tokens_reasoning": 526,
                "tokens_cache_read": 63488,
                "tokens_cache_write": 0,
                "model": json.dumps(
                    {"id": "gpt-4o-mini", "providerID": "openai", "variant": "high"}
                ),
                "time_created": 1_785_248_922_730,
                "time_updated": 1_785_249_081_561,
            }
        ]
    column_defs = {
        "id": "id text primary key",
        "project_id": "project_id text not null",
        "parent_id": "parent_id text",
        "directory": "directory text not null",
        "title": "title text not null",
        "cost": "cost real default 0 not null",
        "tokens_input": "tokens_input integer default 0 not null",
        "tokens_output": "tokens_output integer default 0 not null",
        "tokens_reasoning": "tokens_reasoning integer default 0 not null",
        "tokens_cache_read": "tokens_cache_read integer default 0 not null",
        "tokens_cache_write": "tokens_cache_write integer default 0 not null",
        "model": "model text",
        "time_created": "time_created integer not null",
        "time_updated": "time_updated integer not null",
    }
    con = sqlite3.connect(opencode_home / db_name)
    try:
        con.execute(
            f"create table session ({', '.join(column_defs[name] for name in columns)})"
        )
        placeholders = ", ".join("?" for _ in columns)
        con.executemany(
            f"insert into session ({', '.join(columns)}) values ({placeholders})",
            [tuple(row.get(name) for name in columns) for row in rows],
        )
        con.commit()
    finally:
        con.close()
    return opencode_home


def test_discover_opencode_usage_reads_native_session_db(tmp_path):
    opencode_home = _make_opencode_db_home(tmp_path)

    events = discover_opencode_usage(opencode_home=opencode_home, limit_sessions=10)

    assert len(events) == 1
    event = events[0]
    assert event.client == "opencode"
    assert event.client_session_id == "ses_alpha"
    assert event.model == "gpt-4o-mini"
    assert event.provider == "openai"
    # Fresh input is stored verbatim (cache reads are a separate additive bucket
    # in OpenCode, not a subset of input).
    assert event.input_tokens == 19589
    assert event.output_tokens == 516
    assert event.reasoning_output_tokens == 526
    assert event.cache_read_input_tokens == 63488
    assert event.cache_creation_input_tokens == 0
    assert event.cached_input_tokens == 63488
    assert event.cwd == "/Users/dev/project"
    assert event.usage_update_semantics == "opencode_session_rollup"
    assert event.source_revision_at == 1_785_249_081_561 * 1000
    assert event.source_revision_basis == "opencode_session_time_updated_us"

    payload = event.to_sentinel_event()
    assert payload["provider"] == "openai"
    assert payload["metadata"]["usage_update_semantics"] == "opencode_session_rollup"


def _agentacct_record_output(event_id: str, *, event_type: str = "section_started") -> str:
    """The JSON string OpenCode stores as an agentacct tool ``state.output``."""
    return json.dumps(
        {"event": {"created_at": 1.0, "event_id": event_id, "event_type": event_type,
                   "metadata": {"section_id": "s1"}}}
    )


def _add_opencode_parts(opencode_home: Path, parts: list[dict[str, object]], *, db_name: str = "opencode.db") -> None:
    """Add a ``part`` table (OpenCode's tool-call log) to an existing db home.

    Each entry is ``{"session_id": ..., "tool": ..., "output": <str|None>,
    "status": ...}``; the row's ``data`` is assembled into OpenCode's real
    ``{"type":"tool","tool":...,"state":{...}}`` shape.
    """
    con = sqlite3.connect(opencode_home / db_name)
    try:
        con.execute(
            "create table part (id text primary key, message_id text, session_id text, "
            "time_created integer, time_updated integer, data text)"
        )
        for index, part in enumerate(parts):
            state: dict[str, object] = {"status": part.get("status", "completed")}
            if "output" in part:
                state["output"] = part["output"]
            data = {"type": part.get("type", "tool"), "tool": part.get("tool"), "state": state}
            con.execute(
                "insert into part (id, message_id, session_id, time_created, time_updated, data) "
                "values (?, ?, ?, ?, ?, ?)",
                (f"prt_{index}", f"msg_{index}", part.get("session_id"), index, index, json.dumps(data)),
            )
        con.commit()
    finally:
        con.close()


def test_discover_opencode_usage_pairs_recorded_section_to_session(tmp_path):
    # An agentacct record_section call in the opencode part log donates its
    # created event id to the session that made it (the observed->reported fix).
    opencode_home = _make_opencode_db_home(tmp_path)
    _add_opencode_parts(
        opencode_home,
        [
            {"session_id": "ses_alpha", "tool": "agentacct_agentacct_record_section",
             "output": _agentacct_record_output("evt_aaaaaaaaaaaa")},
            {"session_id": "ses_alpha", "tool": "agentacct_agentacct_record_machine_check",
             "output": _agentacct_record_output("evt_bbbbbbbbbbbb", event_type="machine_check")},
        ],
    )

    events = discover_opencode_usage(opencode_home=opencode_home, limit_sessions=10)

    assert len(events) == 1
    event = events[0]
    assert event.client_session_id == "ses_alpha"
    assert set(event.evidenced_event_ids) == {"evt_aaaaaaaaaaaa", "evt_bbbbbbbbbbbb"}
    assert event.evidenced_event_id_total == 2
    # And it rides into the emitted event metadata for the reducer to consume.
    payload = event.to_sentinel_event()
    assert set(payload["metadata"]["evidenced_event_ids"]) == {"evt_aaaaaaaaaaaa", "evt_bbbbbbbbbbbb"}


def test_discover_opencode_usage_read_tool_and_inflight_donate_nothing(tmp_path):
    # A READ tool echoes OTHER sessions' ids and must never donate; an in-flight
    # call (no output) donates nothing and is counted as an honest skip.
    opencode_home = _make_opencode_db_home(tmp_path)
    _add_opencode_parts(
        opencode_home,
        [
            {"session_id": "ses_alpha", "tool": "agentacct_agentacct_list_events",
             "output": json.dumps({"events": [{"event_id": "evt_ffffffffffff"}]})},
            {"session_id": "ses_alpha", "tool": "agentacct_agentacct_record_section",
             "status": "running"},  # no output key at all
        ],
    )

    events = discover_opencode_usage(opencode_home=opencode_home, limit_sessions=10)

    assert len(events) == 1
    event = events[0]
    assert event.evidenced_event_ids == ()  # read-tool echo excluded, in-flight skipped
    assert "evt_ffffffffffff" not in event.evidenced_event_ids
    assert event.evidenced_outputs_skipped >= 1
    # No evidence -> metadata stays byte-clean (no evidenced_event_ids key).
    assert "evidenced_event_ids" not in event.to_sentinel_event()["metadata"]


def test_discover_opencode_usage_ignores_non_agentacct_tool_payloads(tmp_path):
    # A foreign tool's payload (bash output, file reads) that never names the
    # agentacct server is excluded by the SQL prefilter — it is not SELECTed,
    # not parsed, and cannot donate an id even if its text happens to contain an
    # evt_-shaped string. It is not even counted as a skip (never reaches Python).
    opencode_home = _make_opencode_db_home(tmp_path)
    _add_opencode_parts(
        opencode_home,
        [
            {"session_id": "ses_alpha", "tool": "bash",
             "output": "ran a command; log line evt_deadbeefcafe here"},
            {"session_id": "ses_alpha", "type": "text",
             "output": "user prompt mentioning nothing relevant"},
        ],
    )

    events = discover_opencode_usage(opencode_home=opencode_home, limit_sessions=10)

    assert len(events) == 1
    event = events[0]
    assert event.evidenced_event_ids == ()
    assert event.evidenced_outputs_skipped == 0  # foreign rows never reached the scanner


def test_discover_opencode_usage_recomputes_cost_from_tokens_when_stored_zero(tmp_path):
    opencode_home = _make_opencode_db_home(tmp_path)

    event = discover_opencode_usage(opencode_home=opencode_home, limit_sessions=10)[0]
    assert event.client_reported_cost_usd is None

    payload = event.to_sentinel_event()
    applied = apply_pricing_estimate_to_event(payload)

    assert applied is True
    assert payload["cost_confidence"] == "estimated_from_tokens"
    assert payload["estimated_cost_usd"] > 0


def test_opencode_model_id_strips_fast_routing_suffix_for_pricing():
    from agentacct.client_usage import (
        _normalize_opencode_model_id,
        _opencode_model_fields,
    )

    # OpenCode reports "<base>-fast"; only the base model is in the price table,
    # so the suffixed form must normalize to the base or cost stays unknown.
    assert _normalize_opencode_model_id("gpt-5.6-sol-fast") == "gpt-5.6-sol"
    assert _normalize_opencode_model_id("gpt-5.6-luna-fast") == "gpt-5.6-luna"
    assert _normalize_opencode_model_id("gpt-5.6-sol") == "gpt-5.6-sol"
    assert _normalize_opencode_model_id("-fast") == "-fast"  # only-suffix untouched
    assert _normalize_opencode_model_id(None) is None

    # End-to-end from the session.model cell (dict form and JSON-string form).
    assert _opencode_model_fields(
        {"id": "gpt-5.6-sol-fast", "providerID": "openai", "variant": "high"}
    ) == ("gpt-5.6-sol", "openai")
    assert _opencode_model_fields(
        json.dumps({"id": "gpt-5.6-luna-fast", "providerID": "openai"})
    ) == ("gpt-5.6-luna", "openai")


def test_discover_opencode_usage_keeps_nonzero_stored_cost_as_reported(tmp_path):
    opencode_home = _make_opencode_db_home(
        tmp_path,
        rows=[
            {
                "id": "ses_paid",
                "project_id": "global",
                "parent_id": None,
                "directory": "/tmp/paid",
                "title": "paid session",
                "cost": 1.25,
                "tokens_input": 100,
                "tokens_output": 20,
                "tokens_reasoning": 0,
                "tokens_cache_read": 0,
                "tokens_cache_write": 0,
                "model": json.dumps({"id": "gpt-4o-mini", "providerID": "openai"}),
                "time_created": 1_785_248_922_730,
                "time_updated": 1_785_249_081_561,
            }
        ],
    )

    event = discover_opencode_usage(opencode_home=opencode_home, limit_sessions=10)[0]

    assert event.client_reported_cost_usd == 1.25
    assert event.client_cost_source == "opencode_session_cost"
    payload = event.to_sentinel_event()
    assert payload["cost_confidence"] == "client_reported"
    # A real reported cost is not overwritten by the pricing estimate.
    assert apply_pricing_estimate_to_event(payload) is False
    assert payload["estimated_cost_usd"] == 1.25


def test_discover_opencode_usage_presence_flags_track_schema_columns(tmp_path):
    # Full schema: reasoning column present but its measured value is zero.
    full_home = _make_opencode_db_home(tmp_path, dir_name="full")
    full_event = discover_opencode_usage(opencode_home=full_home, limit_sessions=10)[0]
    assert full_event.reasoning_output_tokens_reported is True
    assert full_event.cache_read_tokens_reported is True

    # Degraded schema: no reasoning / cache columns at all -> absent, not zero.
    lean_columns = (
        "id",
        "project_id",
        "directory",
        "title",
        "cost",
        "tokens_input",
        "tokens_output",
        "model",
        "time_created",
        "time_updated",
    )
    lean_home = _make_opencode_db_home(
        tmp_path,
        dir_name="lean",
        columns=lean_columns,
        rows=[
            {
                "id": "ses_lean",
                "project_id": "global",
                "directory": "/tmp/lean",
                "title": "lean",
                "cost": 0.0,
                "tokens_input": 800,
                "tokens_output": 40,
                "model": json.dumps({"id": "gpt-4o-mini", "providerID": "openai"}),
                "time_created": 1_785_248_922_730,
                "time_updated": 1_785_249_081_561,
            }
        ],
    )
    lean_event = discover_opencode_usage(opencode_home=lean_home, limit_sessions=10)[0]
    assert lean_event.input_tokens == 800
    assert lean_event.reasoning_output_tokens_reported is False
    assert lean_event.cache_read_tokens_reported is False
    assert lean_event.cache_creation_tokens_reported is False


def test_discover_opencode_usage_db_takes_precedence_over_json(tmp_path):
    # A home carrying BOTH a native db and JSON export streams imports the db.
    opencode_home = _make_opencode_db_home(tmp_path, dir_name="both")
    session_dir = opencode_home / "sessions"
    session_dir.mkdir()
    (session_dir / "ses_json.jsonl").write_text(
        json.dumps(
            {
                "type": "step_finish",
                "sessionID": "ses_json",
                "part": {
                    "type": "step-finish",
                    "tokens": {"input": 5, "output": 5, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                    "cost": 0.0,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    events = discover_opencode_usage(opencode_home=opencode_home, limit_sessions=10)

    assert {event.client_session_id for event in events} == {"ses_alpha"}
    assert all(event.usage_update_semantics == "opencode_session_rollup" for event in events)


def test_discover_opencode_usage_corrupt_db_fails_closed(tmp_path):
    opencode_home = tmp_path / "corrupt-home"
    opencode_home.mkdir()
    (opencode_home / "opencode.db").write_bytes(b"this is not a sqlite database")

    result = discover_client_usage_with_diagnostics(
        client="opencode",
        codex_home=tmp_path / "missing-codex",
        claude_home=tmp_path / "missing-claude",
        opencode_home=opencode_home,
        hermes_home=tmp_path / "missing-hermes",
        openclaw_home=tmp_path / "missing-openclaw",
        cursor_home=tmp_path / "missing-cursor",
        limit_sessions=10,
    )

    opencode_events = [event for event in result.events if event.client == "opencode"]
    assert opencode_events == []
    diagnostics = result.diagnostics.get("opencode", {})
    assert diagnostics.get("error_count", 0) >= 1
    assert "sqlite_read_failed" in (diagnostics.get("error_codes") or [])


def _make_openclaw_home(root: Path) -> Path:
    openclaw_home = root / "openclaw-home"
    session_dir = openclaw_home / "agents" / "default" / "sessions"
    session_dir.mkdir(parents=True)
    stream = session_dir / "openclaw-session.jsonl"
    rows = [
        {"type": "model_change", "provider": "openai", "modelId": "gpt-5.2"},
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "timestamp": 1_769_753_935_279,
                "usage": {
                    "input": 1660,
                    "output": 55,
                    "cacheRead": 108928,
                    "cacheWrite": 10,
                    "totalTokens": 110653,
                    "cost": {"total": 0.02},
                },
            },
        },
    ]
    stream.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return openclaw_home


def _make_hermes_home(root: Path) -> Path:
    hermes_home = root / "hermes-home"
    hermes_home.mkdir()
    con = sqlite3.connect(hermes_home / "state.db")
    try:
        con.execute(
            """
            create table sessions (
                id text primary key,
                source text not null,
                model text,
                started_at real not null,
                message_count integer default 0,
                input_tokens integer default 0,
                output_tokens integer default 0,
                cache_read_tokens integer default 0,
                cache_write_tokens integer default 0,
                reasoning_tokens integer default 0,
                billing_provider text,
                estimated_cost_usd real,
                actual_cost_usd real
            )
            """
        )
        con.execute(
            """
            insert into sessions (
                id, source, model, started_at, message_count,
                input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens,
                billing_provider, estimated_cost_usd, actual_cost_usd
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "hermes-session-1",
                "cli",
                "claude-sonnet-4-20250514",
                1_750_000_000.25,
                4,
                1200,
                300,
                50,
                20,
                10,
                "anthropic",
                0.12,
                0.34,
            ),
        )
        con.commit()
    finally:
        con.close()
    return hermes_home


def test_discover_codex_usage_reads_non_cached_input_and_cache_metadata(tmp_path):
    codex_home = _make_codex_home(tmp_path)

    events = discover_codex_usage(codex_home=codex_home, limit_sessions=10)

    assert len(events) == 1
    event = events[0]
    assert event.client == "codex"
    assert event.input_tokens == 1600
    assert event.cached_input_tokens == 900
    assert event.output_tokens == 125
    assert event.reasoning_output_tokens == 22
    assert event.input_tokens_reported is True
    assert event.output_tokens_reported is True
    assert event.reasoning_output_tokens_reported is True
    assert event.total_tokens == 2625
    assert event.total_tokens_reported is True
    assert event.usage_representation is None
    assert event.usage_precedence_role is None
    assert event.turn_count == 2
    payload = event.to_sentinel_event()
    assert payload["usage_confidence"] == "client_reported"
    assert payload["cost_confidence"] == "unknown"
    assert payload["cost_basis"] == "local_client_session"
    assert payload["metadata"]["usage_source"] == "local_client_session_store"
    assert payload["metadata"]["usage_update_semantics"] == "codex_rollout_token_count_events"
    assert payload["metadata"]["client_session_kind"] == "root"
    assert payload["metadata"]["parent_client_session_id"] is None
    assert payload["metadata"]["client_transcript_id"] == "session-abc"
    assert payload["metadata"]["title_redacted"] is False
    assert payload["metadata"]["client_session_title_source"] == "explicit_client_title_field"
    assert payload["metadata"]["client_session_title_sanitized"] is True
    assert payload["metadata"]["client_session_title"] == "A useful Codex session"
    assert payload["metadata"]["cache_creation_tokens_reported"] is False
    assert payload["metadata"]["cache_read_tokens_reported"] is True
    assert payload["metadata"]["input_tokens_reported"] is True
    assert payload["metadata"]["output_tokens_reported"] is True
    assert payload["metadata"]["reasoning_output_tokens_reported"] is True
    assert payload["metadata"]["total_tokens"] == 2625
    assert payload["metadata"]["total_tokens_reported"] is True
    assert "usage_representation" not in payload["metadata"]
    assert "precedence_role" not in payload["metadata"]
    assert payload["metadata"]["source_file"].startswith("rollout-")


def test_discover_codex_usage_preserves_missing_vs_explicit_zero_counter_presence(
    tmp_path,
):
    codex_home = _make_codex_home(tmp_path)
    rollout_path = next((codex_home / "sessions").rglob("rollout-*.jsonl"))
    rows = [json.loads(line) for line in rollout_path.read_text(encoding="utf-8").splitlines()]
    rows[-2]["payload"]["info"]["total_token_usage"] = {
        "input_tokens": 1500,
        "cached_input_tokens": 400,
        "total_tokens": 1500,
    }
    rows[-1]["payload"]["info"]["total_token_usage"] = {
        "input_tokens": 2500,
        "cached_input_tokens": 900,
        "total_tokens": 2500,
    }
    rows[-1]["payload"]["info"]["last_token_usage"] = {
        "input_tokens": 1000,
        "cached_input_tokens": 500,
        "total_tokens": 1000,
    }
    rollout_path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    missing = discover_codex_usage(codex_home=codex_home, limit_sessions=10)[0]

    assert missing.input_tokens == 1600
    assert missing.output_tokens == 0
    assert missing.reasoning_output_tokens == 0
    assert missing.input_tokens_reported is True
    assert missing.output_tokens_reported is False
    assert missing.reasoning_output_tokens_reported is False
    assert missing.total_tokens == 2500
    assert missing.total_tokens_reported is True
    missing_metadata = missing.to_sentinel_event()["metadata"]
    assert missing_metadata["input_tokens_reported"] is True
    assert missing_metadata["output_tokens_reported"] is False
    assert missing_metadata["reasoning_output_tokens_reported"] is False

    rows[-1]["payload"]["info"]["total_token_usage"].update(
        {"output_tokens": 0, "reasoning_output_tokens": 0}
    )
    rows[-1]["payload"]["info"]["last_token_usage"].update(
        {"output_tokens": 0, "reasoning_output_tokens": 0}
    )
    rows[-2]["payload"]["info"]["total_token_usage"].update(
        {"output_tokens": 0, "reasoning_output_tokens": 0}
    )
    rollout_path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    explicit_zero = discover_codex_usage(codex_home=codex_home, limit_sessions=10)[0]

    assert explicit_zero.output_tokens == 0
    assert explicit_zero.reasoning_output_tokens == 0
    assert explicit_zero.output_tokens_reported is True
    assert explicit_zero.reasoning_output_tokens_reported is True
    explicit_metadata = explicit_zero.to_sentinel_event()["metadata"]
    assert explicit_metadata["output_tokens_reported"] is True
    assert explicit_metadata["reasoning_output_tokens_reported"] is True


def test_codex_normalization_requires_presence_across_every_retained_delta(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    for missing_first in (True, False):
        first = {"input_tokens": 100, "total_tokens": 100}
        second = {"input_tokens": 200, "total_tokens": 200}
        last = {"input_tokens": 100, "total_tokens": 100}
        if not missing_first:
            first["output_tokens"] = 0
        if missing_first:
            second["output_tokens"] = 0
            last["output_tokens"] = 0
        _rewrite_codex_rollout(
            codex_home,
            "session-abc",
            [
                {"type": "session_meta", "payload": {"id": "session-abc"}},
                _codex_token_count("2026-06-27T00:01:00.000Z", first),
                _codex_token_count(
                    "2026-06-27T00:02:00.000Z",
                    second,
                    last=last,
                ),
            ],
        )

        event = discover_codex_usage(codex_home=codex_home, limit_sessions=10)[0]

        assert event.output_tokens == 0
        assert event.output_tokens_reported is False


def test_codex_cache_split_presence_distinguishes_missing_zero_and_invalid(tmp_path):
    cases: tuple[tuple[str, object, object, bool, bool, bool], ...] = (
        ("missing-cache-read", None, None, False, False, False),
        ("cache-write-not-applicable", 0, None, True, False, True),
        ("explicit-zero", 0, 0, True, True, True),
        ("invalid-cache-read", True, None, False, False, False),
        ("invalid-cache-write", 0, "0", True, False, False),
        ("invalid-negative", -1, -1, False, False, False),
    )
    for (
        name,
        cache_read,
        cache_write,
        expected_read_reported,
        expected_write_reported,
        expected_input_reported,
    ) in cases:
        codex_home = _make_codex_home(tmp_path / name)
        rollout_path = next((codex_home / "sessions").rglob("rollout-*.jsonl"))
        rows = [
            json.loads(line)
            for line in rollout_path.read_text(encoding="utf-8").splitlines()
        ]
        for row in rows:
            payload = row.get("payload")
            info = payload.get("info") if isinstance(payload, dict) else None
            if not isinstance(info, dict):
                continue
            for counter_name in ("total_token_usage", "last_token_usage"):
                counters = info.get(counter_name)
                if not isinstance(counters, dict):
                    continue
                counters.pop("cached_input_tokens", None)
                counters.pop("cache_write_tokens", None)
                if cache_read is not None:
                    counters["cached_input_tokens"] = cache_read
                if cache_write is not None:
                    counters["cache_write_tokens"] = cache_write
        rollout_path.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )

        event = discover_codex_usage(codex_home=codex_home, limit_sessions=10)[0]

        assert event.cache_read_input_tokens == 0
        assert event.cache_creation_input_tokens == 0
        assert event.cache_read_tokens_reported is expected_read_reported
        assert event.cache_creation_tokens_reported is expected_write_reported
        assert event.input_tokens_reported is expected_input_reported
        metadata = event.to_sentinel_event()["metadata"]
        assert metadata["input_tokens_reported"] is expected_input_reported
        assert metadata["cache_read_tokens_reported"] is expected_read_reported
        assert (
            metadata["cache_creation_tokens_reported"]
            is expected_write_reported
        )

        normalized = client_usage_module._read_codex_rollout_usage(rollout_path)
        assert isinstance(normalized, dict)
        client_usage_module._normalize_codex_rollout_usage_cohorts(
            [{"id": "session-abc", "created_at": 100, "model": "gpt-5.5"}],
            parent_by_session={"session-abc": None},
            usage_by_session={"session-abc": normalized},
            parse_complete_by_session={"session-abc": True},
        )
        assert (
            normalized["_cached_input_tokens_reported"]
            is expected_read_reported
        )
        assert (
            normalized["_cache_write_tokens_reported"]
            is expected_write_reported
        )
        assert normalized["_cache_write_tokens_applicable"] is (
            cache_write is not None
        )
        if not expected_write_reported:
            assert "cache_write_tokens" not in normalized


def test_codex_impossible_last_counter_is_schema_drift_not_amplified_or_fallback(
    tmp_path,
):
    codex_home = _make_codex_home(tmp_path)
    rollout_path = next((codex_home / "sessions").rglob("rollout-*.jsonl"))
    rows = [
        json.loads(line)
        for line in rollout_path.read_text(encoding="utf-8").splitlines()
    ]
    rows[-1]["payload"]["info"]["last_token_usage"].update(
        {"input_tokens": 9_999, "total_tokens": 9_999}
    )
    rollout_path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    stats: dict[str, object] = {}
    observations = []

    events = client_usage_module._discover_codex_usage_from_home(
        codex_home=codex_home,
        limit_sessions=10,
        _discovery_stats=stats,
        _session_observations=observations,
    )

    assert len(events) == 1
    event = events[0]
    assert event.input_tokens == 1_600
    assert event.total_tokens == 2_625
    assert event.source_parse_complete is False
    assert event.usage_representation is None
    assert event.usage_precedence_role is None
    metadata = event.to_sentinel_event()["metadata"]
    assert metadata["usage_update_semantics"] == "codex_rollout_token_count_events"
    assert "usage_representation" not in metadata
    assert "precedence_role" not in metadata
    assert stats["error_count"] == 1
    assert stats["error_codes"] == ["codex_rollout_schema_drift"]
    assert observations[0].source_parse_complete is False
    plan = plan_local_usage_import(events, [])
    assert plan.new_candidates == []
    assert plan.incomplete_source_candidates == events


def test_codex_dedupe_signature_distinguishes_missing_from_explicit_zero(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    missing = {"input_tokens": 0, "total_tokens": 0}
    explicit_zero = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    _rewrite_codex_rollout(
        codex_home,
        "session-abc",
        [
            {"type": "session_meta", "payload": {"id": "session-abc"}},
            _codex_token_count(
                "2026-06-27T00:01:00.000Z",
                missing,
                last=missing,
            ),
            _codex_token_count(
                "2026-06-27T00:01:00.000Z",
                explicit_zero,
                last=explicit_zero,
            ),
        ],
    )

    event = discover_codex_usage(codex_home=codex_home, limit_sessions=10)[0]

    assert event.output_tokens == 0
    assert event.output_tokens_reported is False
    assert event.turn_count == 2
    assert event.deduplicated_usage_rows == 0


def test_codex_rollout_survives_deeply_nested_json_line(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    rollout_path = next((codex_home / "sessions").rglob("rollout-*.jsonl"))
    deeply_nested_line = (
        '{"type":"event_msg","payload":{"nested":'
        + "[" * 10_000
        + "0"
        + "]" * 10_000
        + "}}"
    )
    rollout_path.write_text(
        rollout_path.read_text(encoding="utf-8") + deeply_nested_line + "\n",
        encoding="utf-8",
    )
    parse_stats: dict[str, int] = {}

    usage = client_usage_module._read_codex_rollout_usage(
        rollout_path,
        _parse_stats=parse_stats,
    )

    assert usage is not None
    assert usage["total_tokens"] == 2_625
    assert parse_stats["unparseable_rollouts"] == 1


def test_codex_partial_or_invalid_rollout_never_uses_sqlite_input_fallback(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    partial = {
        "output_tokens": 5,
        "reasoning_output_tokens": 2,
        "total_tokens": 5,
    }
    _rewrite_codex_rollout(
        codex_home,
        "session-abc",
        [
            {"type": "session_meta", "payload": {"id": "session-abc"}},
            _codex_token_count("2026-06-27T00:01:00.000Z", partial, last=partial),
        ],
    )

    event = discover_codex_usage(codex_home=codex_home, limit_sessions=10)[0]

    assert event.input_tokens == 0
    assert event.input_tokens_reported is False
    assert event.output_tokens == 5
    assert event.output_tokens_reported is True
    assert event.reasoning_output_tokens == 2
    assert event.reasoning_output_tokens_reported is True

    invalid: dict[str, object] = {
        "input_tokens": True,
        "output_tokens": "0",
        "reasoning_output_tokens": -1,
        "total_tokens": 0,
    }
    _rewrite_codex_rollout(
        codex_home,
        "session-abc",
        [
            {"type": "session_meta", "payload": {"id": "session-abc"}},
            _codex_token_count(  # type: ignore[arg-type]
                "2026-06-27T00:02:00.000Z",
                invalid,
                last=invalid,
            ),
        ],
    )

    invalid_event = discover_codex_usage(codex_home=codex_home, limit_sessions=10)[0]

    assert invalid_event.input_tokens == 0
    assert invalid_event.output_tokens == 0
    assert invalid_event.reasoning_output_tokens == 0
    assert invalid_event.input_tokens_reported is False
    assert invalid_event.output_tokens_reported is False
    assert invalid_event.reasoning_output_tokens_reported is False


def test_codex_invalid_total_token_usage_carrier_is_incomplete_and_never_falls_back(
    tmp_path,
):
    codex_home = _make_codex_home(tmp_path)
    rollout_path = next((codex_home / "sessions").rglob("rollout-*.jsonl"))
    rows = [
        json.loads(line)
        for line in rollout_path.read_text(encoding="utf-8").splitlines()
    ]
    for row in rows:
        payload = row.get("payload")
        info = payload.get("info") if isinstance(payload, dict) else None
        if isinstance(info, dict) and "total_token_usage" in info:
            info["total_token_usage"] = []
    rollout_path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    stats: dict[str, object] = {}
    observations = []

    events = client_usage_module._discover_codex_usage_from_home(
        codex_home=codex_home,
        limit_sessions=10,
        _discovery_stats=stats,
        _session_observations=observations,
    )

    assert len(events) == 1
    event = events[0]
    assert event.input_tokens == 0
    assert event.input_tokens_reported is False
    assert event.total_tokens is None
    assert event.total_tokens_reported is False
    assert event.source_parse_complete is False
    assert event.usage_representation is None
    assert event.usage_precedence_role is None
    assert stats["error_count"] == 1
    assert stats["error_codes"] == ["codex_rollout_schema_drift"]
    assert observations[0].source_parse_complete is False


def test_codex_difference_without_last_field_preserves_proven_presence(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    first = {"input_tokens": 100, "output_tokens": 5, "total_tokens": 105}
    second = {"input_tokens": 200, "output_tokens": 10, "total_tokens": 210}
    last_without_output = {"input_tokens": 100, "total_tokens": 100}
    _rewrite_codex_rollout(
        codex_home,
        "session-abc",
        [
            {"type": "session_meta", "payload": {"id": "session-abc"}},
            _codex_token_count("2026-06-27T00:01:00.000Z", first),
            _codex_token_count(
                "2026-06-27T00:02:00.000Z",
                second,
                last=last_without_output,
            ),
        ],
    )

    event = discover_codex_usage(codex_home=codex_home, limit_sessions=10)[0]

    assert event.output_tokens == 10
    assert event.output_tokens_reported is True


def test_codex_missing_counter_does_not_reset_other_cumulative_deltas():
    previous = {
        "input_tokens": 100,
        "output_tokens": 10,
        "total_tokens": 110,
    }
    current = {
        "input_tokens": 150,
        # Output disappeared from this wire row. It is missing, not a reset to
        # zero and not evidence that the input counter restarted.
        "total_tokens": 150,
    }

    delta = client_usage_module._codex_token_delta(
        current_value=current,
        last_value=None,
        previous_value=previous,
    )
    presence = client_usage_module._codex_token_delta_presence(
        current_value=current,
        last_value=None,
        previous_value=previous,
    )

    assert delta[0] == 50
    assert delta[3] == 0
    assert presence[0] is True
    assert presence[3] is False


def test_codex_invalid_present_last_counter_is_unreported_not_fallback():
    current = {
        "input_tokens": 100,
        "output_tokens": 5,
        "total_tokens": 105,
    }
    last: dict[str, object] = {
        "input_tokens": "bad",
        "output_tokens": 0,
        "total_tokens": 0,
    }

    delta = client_usage_module._codex_token_delta(
        current_value=current,
        last_value=last,
        previous_value=None,
    )
    presence = client_usage_module._codex_token_delta_presence(
        current_value=current,
        last_value=last,
        previous_value=None,
    )

    assert delta[0] == 0
    assert presence[0] is False
    assert delta[3] == 0
    assert presence[3] is True


def test_codex_explicit_zero_total_is_not_replaced_by_a_derived_total():
    explicit_zero = {
        "input_tokens": 4,
        "output_tokens": 1,
        "total_tokens": 0,
    }
    missing = {
        "input_tokens": 4,
        "output_tokens": 1,
    }

    assert client_usage_module._codex_counter_vector(explicit_zero)[5] == 0
    assert client_usage_module._codex_counter_presence(explicit_zero)[5] is True
    assert client_usage_module._codex_counter_vector(missing)[5] == 5
    assert client_usage_module._codex_counter_presence(missing)[5] is False


def test_discover_codex_usage_never_falls_back_to_sqlite_prompt_title(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    (codex_home / "session_index.jsonl").unlink()

    event = discover_codex_usage(codex_home=codex_home, limit_sessions=10)[0]
    payload = event.to_sentinel_event()

    assert event.title is None
    assert "client_session_title" not in payload["metadata"]
    assert "client_session_title_source" not in payload["metadata"]
    assert "PRIVATE FIRST PROMPT" not in json.dumps(payload)


def test_discover_codex_usage_uses_latest_matching_session_index_title(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    (codex_home / "session_index.jsonl").write_text(
        "\n".join(
            [
                "not-json",
                json.dumps({"id": "another-session", "thread_name": "Wrong session"}),
                json.dumps({"id": "session-abc", "thread_name": "Old sidebar name"}),
                json.dumps({"id": "session-abc", "thread_name": "Renamed sidebar task"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    event = discover_codex_usage(codex_home=codex_home, limit_sessions=10)[0]

    assert event.title == "Renamed sidebar task"
    assert event.to_sentinel_event()["metadata"]["client_session_title"] == "Renamed sidebar task"


def test_discover_codex_usage_uses_row_level_cache_write_capability(tmp_path):
    codex_home = _make_codex_home(tmp_path, model="gpt-5.6-sol")
    rollout_path = next((codex_home / "sessions").rglob("rollout-*.jsonl"))
    rows = [json.loads(line) for line in rollout_path.read_text(encoding="utf-8").splitlines()]
    rows[-2]["payload"]["info"]["total_token_usage"]["cache_write_tokens"] = 100
    rows[-1]["payload"]["info"]["total_token_usage"]["cache_write_tokens"] = 300
    rows[-1]["payload"]["info"]["last_token_usage"]["cache_write_tokens"] = 200
    rollout_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    event = discover_codex_usage(codex_home=codex_home, limit_sessions=10)[0]

    # input_tokens is inclusive in Codex's rollout. Chronicle normalizes its
    # detail buckets without changing the original input+output total.
    assert event.input_tokens == 1300
    assert event.cache_creation_input_tokens == 300
    assert event.cache_read_input_tokens == 900
    assert event.cached_input_tokens == 1200
    assert event.input_tokens + event.output_tokens + event.cached_input_tokens == 2625
    payload = event.to_sentinel_event()
    assert payload["metadata"]["cache_creation_tokens_reported"] is True
    assert payload["metadata"]["cache_read_tokens_reported"] is True


def test_discover_codex_sqlite_fallback_marks_cache_splits_unreported(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    rollout_path = next((codex_home / "sessions").rglob("rollout-*.jsonl"))
    rollout_path.write_text("", encoding="utf-8")

    event = discover_codex_usage(codex_home=codex_home, limit_sessions=10)[0]

    assert event.input_tokens == 2625
    assert event.input_tokens_reported is False
    assert event.output_tokens_reported is False
    assert event.reasoning_output_tokens_reported is False
    assert event.total_tokens == 2625
    assert event.total_tokens_reported is True
    assert event.cache_creation_tokens_reported is False
    assert event.cache_read_tokens_reported is False
    assert event.usage_representation == "codex-sqlite-tokens-used-fallback-v1"
    assert event.usage_precedence_role == "fallback"
    assert event.usage_update_semantics == "codex_sqlite_tokens_used_fallback"
    payload = event.to_sentinel_event()
    assert payload["metadata"]["input_tokens_reported"] is False
    assert payload["metadata"]["output_tokens_reported"] is False
    assert payload["metadata"]["reasoning_output_tokens_reported"] is False
    assert payload["metadata"]["total_tokens"] == 2625
    assert payload["metadata"]["total_tokens_reported"] is True
    assert payload["metadata"]["cache_creation_tokens_reported"] is False
    assert payload["metadata"]["cache_read_tokens_reported"] is False
    assert (
        payload["metadata"]["usage_representation"]
        == "codex-sqlite-tokens-used-fallback-v1"
    )
    assert payload["metadata"]["precedence_role"] == "fallback"
    assert (
        payload["metadata"]["usage_update_semantics"]
        == "codex_sqlite_tokens_used_fallback"
    )


def test_client_usage_event_rejects_invalid_usage_precedence_role(tmp_path):
    with pytest.raises(ValueError, match="usage_precedence_role is invalid"):
        ClientUsageEvent(
            client="codex",
            client_session_id="invalid-precedence",
            source_path=tmp_path / "rollout.jsonl",
            title=None,
            cwd=None,
            model="gpt-5.5",
            input_tokens=0,
            output_tokens=0,
            usage_precedence_role="primary",  # type: ignore[arg-type]
        )


def test_discover_codex_usage_records_thread_spawn_parent_when_present(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    con = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        con.execute(
            """
            create table thread_spawn_edges (
                parent_thread_id text not null,
                child_thread_id text not null,
                status text not null
            )
            """
        )
        con.execute("insert into thread_spawn_edges values (?, ?, ?)", ("parent-session", "session-abc", "completed"))
        con.commit()
    finally:
        con.close()

    events = discover_codex_usage(codex_home=codex_home, limit_sessions=10)

    assert len(events) == 1
    payload = events[0].to_sentinel_event()
    assert payload["metadata"]["client_session_kind"] == "child"
    assert payload["metadata"]["parent_client_session_id"] == "parent-session"
    assert payload["metadata"]["client_transcript_id"] == "session-abc"
    assert payload["metadata"]["client_spawn_status"] == "completed"


def test_discover_codex_usage_marks_subagent_thread_without_spawn_parent(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    con = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        con.execute("alter table threads add column source text")
        con.execute("alter table threads add column thread_source text")
        con.execute(
            "update threads set source = ?, thread_source = ? where id = ?",
            ('{"subagent":{"name":"worker"}}', "subagent", "session-abc"),
        )
        con.commit()
    finally:
        con.close()

    events = discover_codex_usage(codex_home=codex_home, limit_sessions=10)

    assert len(events) == 1
    payload = events[0].to_sentinel_event()
    assert payload["metadata"]["client_session_kind"] == "child"
    assert payload["metadata"]["parent_client_session_id"] is None
    assert payload["metadata"]["client_thread_source"] == "subagent"


def test_codex_session_meta_spawn_source_excludes_fork_and_resume():
    # The narrow spawn detector recognizes ONLY a concurrent-spawn carrier.
    spawn = {"source": {"subagent": {"thread_spawn": {"parent_thread_id": "p"}}}}
    fork = {"source": {"forked_from_id": "p"}}
    resume = {"source": {"resumed": True}}
    assert client_usage_module._codex_session_meta_has_spawn_source(spawn) is True
    assert client_usage_module._codex_session_meta_has_spawn_source(fork) is False
    assert client_usage_module._codex_session_meta_has_spawn_source(resume) is False
    # The broad replay detector still flags the fork (it drives token-replay
    # accounting, not Task grouping) — proving the two axes are distinct.
    assert client_usage_module._codex_session_meta_has_replay_source(spawn) is True
    assert client_usage_module._codex_session_meta_has_replay_source(fork) is True


def test_discover_codex_usage_splits_bare_lineage_parent_into_own_root(tmp_path):
    # A fork / resume / compaction rollout carries the seed thread's id as a
    # bare session_meta.parent_thread_id, with no spawn edge and no subagent
    # marker: the SAME conversation continued, not a spawned child. It must
    # become its own root Task instead of transitively merging under the seed.
    codex_home = _make_codex_home(tmp_path)
    _add_codex_thread(
        codex_home,
        session_id="resumed-conversation",
        updated_at=300,
        model="gpt-5.5",
        parent_session_id="session-abc",
    )

    observations: list[ClientSessionObservation] = []
    events = discover_codex_usage(
        codex_home=codex_home,
        limit_sessions=10,
        _session_observations=observations,
    )

    # Task grouping (observation): the bare lineage edge is dropped, so the
    # continued conversation is its own clean root, never nested under the seed.
    resumed_obs = {obs.client_session_id: obs for obs in observations}[
        "resumed-conversation"
    ]
    assert resumed_obs.parent_client_session_id is None
    assert resumed_obs.client_session_kind == "root"

    # The usage event mirrors the dropped grouping parent — the session rollup
    # unions parent pointers across events AND observations, so both must drop
    # for the row to become its own Task.
    resumed_event = {event.client_session_id: event for event in events}[
        "resumed-conversation"
    ]
    payload = resumed_event.to_sentinel_event()
    assert payload["metadata"]["parent_client_session_id"] is None
    assert payload["metadata"]["client_session_kind"] == "root"

    # Token lineage rides a SEPARATE axis: the exact recorded parent still
    # governs additivity, so a resumed row whose raw counter may replay the
    # parent prefix stays quarantined rather than double-counting the tokens.
    assert resumed_event.token_lineage_parent_client_session_id == "session-abc"
    assert payload["metadata"]["usage_additive"] is False

    # The live preview / dashboard additivity lane must agree with the stored
    # lane: both quarantine the split fork so its replayed cumulative counter is
    # never double-counted in /usage/preview totals or dashboard local records.
    from agentacct.usage_view import _record_from_client_usage

    preview_record = _record_from_client_usage(resumed_event)
    assert preview_record.usage_additive is False
    assert preview_record.input_tokens == 0
    assert preview_record.output_tokens == 0


def test_discover_codex_usage_keeps_real_spawn_child_nested_under_root(tmp_path):
    # Contrast with the bare-lineage split: a rollout that recorded a genuine
    # source.subagent.thread_spawn is a distinct concurrent child and keeps its
    # grouping parent, nesting under the spawning root exactly as before.
    codex_home = _make_codex_home(tmp_path)
    _add_codex_thread(
        codex_home,
        session_id="spawned-child",
        updated_at=300,
        model="gpt-5.5",
        parent_session_id="session-abc",
    )
    _rewrite_codex_rollout(
        codex_home,
        "spawned-child",
        [
            {
                "timestamp": "2026-06-27T00:01:00.000Z",
                "type": "session_meta",
                "payload": {
                    "id": "spawned-child",
                    "parent_thread_id": "session-abc",
                    "source": {
                        "subagent": {
                            "thread_spawn": {"parent_thread_id": "session-abc"}
                        }
                    },
                },
            },
            _codex_token_count(
                "2026-06-27T00:02:00.000Z",
                _codex_counters(100, 40, 10, 2),
            ),
        ],
    )

    observations: list[ClientSessionObservation] = []
    discover_codex_usage(
        codex_home=codex_home,
        limit_sessions=10,
        _session_observations=observations,
    )

    child_obs = {obs.client_session_id: obs for obs in observations}["spawned-child"]
    assert child_obs.parent_client_session_id == "session-abc"
    assert child_obs.client_session_kind == "child"


def test_discover_codex_usage_counts_only_child_delta_after_replayed_parent_prefix(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    _add_codex_thread(
        codex_home,
        session_id="child-session",
        updated_at=300,
        model="gpt-5.5",
        parent_session_id="session-abc",
    )
    _rewrite_codex_rollout(
        codex_home,
        "child-session",
        [
            {
                "timestamp": "2026-06-27T00:01:00.000Z",
                "type": "session_meta",
                "payload": {
                    "id": "child-session",
                    "parent_thread_id": "session-abc",
                    "source": {
                        "subagent": {
                            "thread_spawn": {"parent_thread_id": "session-abc"}
                        }
                    },
                },
            },
            _codex_token_count(
                "2026-06-27T00:01:00.100Z",
                _codex_counters(1500, 400, 75, 11),
            ),
            _codex_token_count(
                "2026-06-27T00:01:00.900Z",
                _codex_counters(2500, 900, 125, 22),
                last=_codex_counters(1000, 500, 50, 11),
            ),
            _codex_token_count(
                "2026-06-27T00:01:00.950Z",
                _codex_counters(2700, 950, 130, 23),
                last=_codex_counters(200, 50, 5, 1),
            ),
            _codex_token_count(
                "2026-06-27T00:02:00.000Z",
                _codex_counters(3000, 1050, 150, 28),
            ),
        ],
    )

    events = discover_codex_usage(codex_home=codex_home, limit_sessions=10)

    by_session = {event.client_session_id: event for event in events}
    root = by_session["session-abc"]
    child = by_session["child-session"]
    assert (root.input_tokens, root.cached_input_tokens, root.output_tokens) == (
        1600,
        900,
        125,
    )
    assert (child.input_tokens, child.cached_input_tokens, child.output_tokens) == (
        200,
        100,
        20,
    )
    assert child.reasoning_output_tokens == 5
    assert child.turn_count == 1
    payload = child.to_sentinel_event()
    assert payload["metadata"]["usage_update_semantics"] == CODEX_LINEAGE_DELTA_SEMANTICS
    assert payload["metadata"]["usage_additive"] is True
    assert payload["metadata"]["replay_prefix_token_events"] == 3


def test_discover_codex_usage_deduplicates_identical_delta_inside_root_cohort(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    duplicate_delta = _codex_counters(100, 40, 10, 2)
    _rewrite_codex_rollout(
        codex_home,
        "session-abc",
        [
            {
                "timestamp": "2026-06-27T00:03:00.000Z",
                "type": "session_meta",
                "payload": {"id": "session-abc"},
            },
            _codex_token_count(
                "2026-06-27T00:04:00.000Z",
                duplicate_delta,
                last=duplicate_delta,
            ),
            _codex_token_count(
                "2026-06-27T00:04:00.000Z",
                duplicate_delta,
                last=duplicate_delta,
            ),
            _codex_token_count(
                "2026-06-27T00:05:00.000Z",
                _codex_counters(150, 60, 15, 3),
                last=_codex_counters(50, 20, 5, 1),
            ),
        ],
    )

    events = discover_codex_usage(codex_home=codex_home, limit_sessions=10)

    root = next(event for event in events if event.client_session_id == "session-abc")
    assert (root.input_tokens, root.cached_input_tokens, root.output_tokens) == (
        90,
        60,
        15,
    )
    assert root.reasoning_output_tokens == 3
    assert root.turn_count == 2
    assert root.raw_usage_rows == 3
    assert root.deduplicated_usage_rows == 1


def test_discover_codex_usage_accepts_zero_start_child_without_replay_prefix(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    _add_codex_thread(
        codex_home,
        session_id="independent-child",
        updated_at=300,
        model="gpt-5.5",
        parent_session_id="session-abc",
    )
    first_delta = _codex_counters(100, 40, 10, 2)
    _rewrite_codex_rollout(
        codex_home,
        "independent-child",
        [
            {
                "timestamp": "2026-06-27T00:01:00.000Z",
                "type": "session_meta",
                "payload": {
                    "id": "independent-child",
                    "parent_thread_id": "session-abc",
                    "source": {
                        "subagent": {
                            "thread_spawn": {"parent_thread_id": "session-abc"}
                        }
                    },
                },
            },
            _codex_token_count(
                "2026-06-27T00:02:00.000Z",
                first_delta,
                last=first_delta,
            ),
        ],
    )

    events = discover_codex_usage(codex_home=codex_home, limit_sessions=10)

    child = next(
        event for event in events if event.client_session_id == "independent-child"
    )
    assert (child.input_tokens, child.cached_input_tokens, child.output_tokens) == (
        60,
        40,
        10,
    )
    payload = child.to_sentinel_event()
    assert payload["metadata"]["usage_update_semantics"] == CODEX_LINEAGE_DELTA_SEMANTICS
    assert payload["metadata"]["usage_additive"] is True
    assert payload["metadata"].get("replay_prefix_token_events", 0) == 0


def test_discover_codex_usage_counts_single_creation_second_delta(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    _add_codex_thread(
        codex_home,
        session_id="single-fast-turn-child",
        updated_at=300,
        model="gpt-5.5",
        parent_session_id="session-abc",
    )
    first_delta = _codex_counters(100, 40, 10, 2)
    second_delta = _codex_counters(50, 20, 5, 1)
    _rewrite_codex_rollout(
        codex_home,
        "single-fast-turn-child",
        [
            {
                "timestamp": "2026-06-27T00:01:00.000Z",
                "type": "session_meta",
                "payload": {
                    "id": "single-fast-turn-child",
                    "parent_thread_id": "session-abc",
                    "source": {
                        "subagent": {
                            "thread_spawn": {"parent_thread_id": "session-abc"}
                        }
                    },
                },
            },
            _codex_token_count(
                "2026-06-27T00:01:00.100Z",
                _codex_counters(2600, 940, 135, 24),
                last=first_delta,
            ),
            _codex_token_count(
                "2026-06-27T00:02:00.000Z",
                _codex_counters(2650, 960, 140, 25),
                last=second_delta,
            ),
        ],
    )

    events = discover_codex_usage(codex_home=codex_home, limit_sessions=10)

    child = next(
        event
        for event in events
        if event.client_session_id == "single-fast-turn-child"
    )
    assert (child.input_tokens, child.cached_input_tokens, child.output_tokens) == (
        90,
        60,
        15,
    )
    payload = child.to_sentinel_event()
    assert payload["metadata"]["usage_update_semantics"] == CODEX_LINEAGE_DELTA_SEMANTICS
    assert payload["metadata"]["usage_additive"] is True
    assert payload["metadata"].get("replay_prefix_token_events", 0) == 0


def test_discover_codex_usage_keeps_missing_or_malformed_parent_held(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    _add_codex_thread(
        codex_home,
        session_id="missing-parent-child",
        updated_at=300,
        model="gpt-5.5",
        parent_session_id="missing-parent",
    )
    _add_codex_thread(
        codex_home,
        session_id="malformed-parent-child",
        updated_at=400,
        model="gpt-5.5",
    )
    con = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        con.execute("alter table threads add column source text")
        con.execute("alter table threads add column thread_source text")
        con.execute(
            "update threads set source = ?, thread_source = ? where id = ?",
            ('{"subagent":{"name":"worker"}}', "subagent", "malformed-parent-child"),
        )
        con.commit()
    finally:
        con.close()
    _rewrite_codex_rollout(
        codex_home,
        "malformed-parent-child",
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "malformed-parent-child",
                    "parent_thread_id": {"unexpected": "shape"},
                },
            },
            _codex_token_count(
                "2026-06-27T00:06:00.000Z",
                _codex_counters(100, 40, 10, 2),
                last=_codex_counters(100, 40, 10, 2),
            ),
        ],
    )

    events = discover_codex_usage(codex_home=codex_home, limit_sessions=10)

    by_session = {event.client_session_id: event for event in events}
    for session_id in ("missing-parent-child", "malformed-parent-child"):
        payload = by_session[session_id].to_sentinel_event()
        assert payload["metadata"]["usage_additive"] is False
        assert (
            payload["metadata"]["usage_normalization_state"]
            == CODEX_REPLAY_QUARANTINE_STATE
        )
        assert payload["metadata"]["usage_update_semantics"] != CODEX_LINEAGE_DELTA_SEMANTICS


def test_discover_codex_usage_holds_mixed_malformed_child_source(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    _add_codex_thread(
        codex_home,
        session_id="partially-readable-child",
        updated_at=300,
        model="gpt-5.5",
        parent_session_id="session-abc",
    )
    rollout = next(
        (codex_home / "sessions").rglob("*partially-readable-child.jsonl")
    )
    meta = {
        "timestamp": "2026-06-27T00:01:00.000Z",
        "type": "session_meta",
        "payload": {
            "id": "partially-readable-child",
            "parent_thread_id": "session-abc",
            "source": {
                "subagent": {"thread_spawn": {"parent_thread_id": "session-abc"}}
            },
        },
    }
    usage = _codex_token_count(
        "2026-06-27T00:02:00.000Z",
        _codex_counters(100, 40, 10, 2),
        last=_codex_counters(100, 40, 10, 2),
    )
    rollout.write_text(
        json.dumps(meta) + "\n{malformed\n" + json.dumps(usage) + "\n",
        encoding="utf-8",
    )

    result = discover_client_usage_with_diagnostics(
        client="codex",
        codex_home=codex_home,
        limit_sessions=10,
    )

    child = next(
        event
        for event in result.events
        if event.client_session_id == "partially-readable-child"
    )
    payload = child.to_sentinel_event()
    assert child.source_parse_complete is False
    assert payload["metadata"]["usage_additive"] is False
    assert payload["metadata"]["usage_update_semantics"] != CODEX_LINEAGE_DELTA_SEMANTICS
    assert result.diagnostics["codex"]["error_count"] == 1
    assert result.diagnostics["codex"]["error_codes"] == [
        "codex_rollout_unparseable"
    ]


def test_discover_codex_usage_holds_conflicting_parent_carriers(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    _add_codex_thread(
        codex_home,
        session_id="other-parent",
        updated_at=250,
        model="gpt-5.5",
    )
    _add_codex_thread(
        codex_home,
        session_id="conflicted-child",
        updated_at=300,
        model="gpt-5.5",
        parent_session_id="session-abc",
    )
    con = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        con.execute(
            """
            create table thread_spawn_edges (
                parent_thread_id text not null,
                child_thread_id text not null primary key,
                status text not null
            )
            """
        )
        con.execute(
            "insert into thread_spawn_edges values (?, ?, ?)",
            ("other-parent", "conflicted-child", "closed"),
        )
        con.commit()
    finally:
        con.close()
    first_delta = _codex_counters(100, 40, 10, 2)
    _rewrite_codex_rollout(
        codex_home,
        "conflicted-child",
        [
            {
                "timestamp": "2026-06-27T00:01:00.000Z",
                "type": "session_meta",
                "payload": {
                    "id": "conflicted-child",
                    "parent_thread_id": "session-abc",
                    "source": {
                        "subagent": {
                            "thread_spawn": {"parent_thread_id": "session-abc"}
                        }
                    },
                },
            },
            _codex_token_count(
                "2026-06-27T00:02:00.000Z",
                first_delta,
                last=first_delta,
            ),
        ],
    )

    events = discover_codex_usage(codex_home=codex_home, limit_sessions=10)

    child = next(
        event for event in events if event.client_session_id == "conflicted-child"
    )
    payload = child.to_sentinel_event()
    assert child.parent_client_session_id == "other-parent"
    assert payload["metadata"]["usage_additive"] is False
    assert payload["metadata"]["usage_update_semantics"] != CODEX_LINEAGE_DELTA_SEMANTICS


def test_discover_codex_usage_holds_rollout_only_replay_without_exact_parent(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    rollout = (
        codex_home
        / "sessions"
        / "2026"
        / "06"
        / "27"
        / "rollout-2026-06-27T00-00-00-unproven-replay-child.jsonl"
    )
    rollout.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {
                    "timestamp": "2026-06-27T00:01:00.000Z",
                    "type": "session_meta",
                    "payload": {
                        "id": "unproven-replay-child",
                        "source": {
                            "subagent": {
                                "thread_spawn": {"parent_thread_id": "missing-parent"}
                            }
                        },
                    },
                },
                _codex_token_count(
                    "2026-06-27T00:01:00.100Z",
                    _codex_counters(100, 40, 10, 2),
                    last=_codex_counters(100, 40, 10, 2),
                ),
                _codex_token_count(
                    "2026-06-27T00:01:00.200Z",
                    _codex_counters(150, 60, 15, 3),
                    last=_codex_counters(50, 20, 5, 1),
                ),
                _codex_token_count(
                    "2026-06-27T00:02:00.000Z",
                    _codex_counters(200, 80, 20, 4),
                    last=_codex_counters(50, 20, 5, 1),
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    events = discover_codex_usage(codex_home=codex_home, limit_sessions=10)

    child = next(
        event
        for event in events
        if event.client_session_id == "unproven-replay-child"
    )
    payload = child.to_sentinel_event()
    assert child.client_session_kind == "child"
    assert child.parent_client_session_id is None
    assert payload["metadata"]["usage_additive"] is False
    assert (
        payload["metadata"]["usage_normalization_state"]
        == CODEX_REPLAY_QUARANTINE_STATE
    )


def test_discover_codex_usage_holds_parent_cycle(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    _add_codex_thread(
        codex_home,
        session_id="cycle-a",
        updated_at=300,
        model="gpt-5.5",
        parent_session_id="cycle-b",
    )
    _add_codex_thread(
        codex_home,
        session_id="cycle-b",
        updated_at=310,
        model="gpt-5.5",
        parent_session_id="cycle-a",
    )
    first_delta = _codex_counters(100, 40, 10, 2)
    for session_id, parent_id in (("cycle-a", "cycle-b"), ("cycle-b", "cycle-a")):
        _rewrite_codex_rollout(
            codex_home,
            session_id,
            [
                {
                    "timestamp": "2026-06-27T00:01:00.000Z",
                    "type": "session_meta",
                    "payload": {
                        "id": session_id,
                        "parent_thread_id": parent_id,
                        "source": {
                            "subagent": {
                                "thread_spawn": {"parent_thread_id": parent_id}
                            }
                        },
                    },
                },
                _codex_token_count(
                    "2026-06-27T00:02:00.000Z",
                    first_delta,
                    last=first_delta,
                ),
            ],
        )

    events = discover_codex_usage(codex_home=codex_home, limit_sessions=10)

    by_session = {event.client_session_id: event for event in events}
    for session_id in ("cycle-a", "cycle-b"):
        payload = by_session[session_id].to_sentinel_event()
        assert payload["metadata"]["usage_additive"] is False
        assert payload["metadata"]["usage_update_semantics"] != CODEX_LINEAGE_DELTA_SEMANTICS


def test_discover_codex_usage_does_not_treat_internal_review_as_model(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    rollout_path = next((codex_home / "sessions").rglob("rollout-*.jsonl"))
    rollout_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 300,
                                    "cached_input_tokens": 100,
                                    "output_tokens": 20,
                                    "reasoning_output_tokens": 5,
                                }
                            },
                            "model": "codex-auto-review",
                        },
                    }
                )
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    con = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        con.execute("update threads set model = ? where id = ?", ("codex-auto-review", "session-abc"))
        con.commit()
    finally:
        con.close()

    events = discover_codex_usage(codex_home=codex_home, limit_sessions=10)

    assert len(events) == 1
    assert events[0].client_session_kind == "internal"
    assert events[0].model is None
    assert events[0].to_sentinel_event()["model"] is None


def test_discover_codex_usage_never_assigns_scan_wide_model_to_orphan_internal_thread(tmp_path):
    codex_home = _make_codex_home(tmp_path, model="gpt-5.6-sol")
    _add_codex_thread(
        codex_home,
        session_id="orphan-review",
        updated_at=300,
        model="codex-auto-review",
    )

    events = discover_codex_usage(codex_home=codex_home, limit_sessions=10)

    review = next(event for event in events if event.client_session_id == "orphan-review")
    payload = review.to_sentinel_event()
    assert review.client_session_kind == "internal"
    assert review.model is None
    assert payload["model"] is None
    assert "client_model_source" not in payload["metadata"]
    assert "client_model_inherited_from_session_id" not in payload["metadata"]


def test_discover_codex_usage_inherits_internal_model_only_from_exact_parent_with_provenance(tmp_path):
    codex_home = _make_codex_home(tmp_path, model="gpt-5.6-sol")
    _add_codex_thread(
        codex_home,
        session_id="linked-review",
        updated_at=300,
        model="codex-auto-review",
        parent_session_id="session-abc",
    )

    events = discover_codex_usage(codex_home=codex_home, limit_sessions=10)

    review = next(event for event in events if event.client_session_id == "linked-review")
    metadata = review.to_sentinel_event()["metadata"]
    assert review.client_session_kind == "internal"
    assert review.parent_client_session_id == "session-abc"
    assert review.model == "gpt-5.6-sol"
    assert metadata["client_model_source"] == "inherited_exact_parent_session"
    assert metadata["client_model_inherited_from_session_id"] == "session-abc"


def test_discover_codex_usage_limit_selects_complete_recent_root_group(tmp_path):
    codex_home = _make_codex_home(tmp_path, model="gpt-5.6-sol")
    _add_codex_thread(
        codex_home,
        session_id="linked-review-newest",
        updated_at=500,
        model="codex-auto-review",
        parent_session_id="session-abc",
    )
    _add_codex_thread(
        codex_home,
        session_id="linked-review-older",
        updated_at=400,
        model="codex-auto-review",
        parent_session_id="session-abc",
    )
    _add_codex_thread(
        codex_home,
        session_id="unrelated-root",
        updated_at=450,
        model="gpt-5.7",
    )

    events = discover_codex_usage(codex_home=codex_home, limit_sessions=1)

    assert {event.client_session_id for event in events} == {
        "session-abc",
        "linked-review-newest",
        "linked-review-older",
    }
    assert next(event for event in events if event.client_session_id == "session-abc").client_session_kind == "root"
    assert all(
        event.parent_client_session_id == "session-abc"
        for event in events
        if event.client_session_id.startswith("linked-review-")
    )


def test_discover_codex_usage_limit_full_parses_only_selected_root_groups(tmp_path, monkeypatch):
    codex_home = _make_codex_home(tmp_path, model="gpt-5.6-sol")
    _add_codex_thread(
        codex_home,
        session_id="selected-child",
        updated_at=500,
        model="codex-auto-review",
        parent_session_id="session-abc",
    )
    _add_codex_thread(
        codex_home,
        session_id="excluded-root",
        updated_at=300,
        model="gpt-5.7",
    )
    parsed_paths: list[str] = []
    original = client_usage_module._read_codex_rollout_usage

    def tracked(path, **kwargs):
        parsed_paths.append(path.name)
        return original(path, **kwargs)

    monkeypatch.setattr(client_usage_module, "_read_codex_rollout_usage", tracked)

    events = discover_codex_usage(codex_home=codex_home, limit_sessions=1)

    assert {event.client_session_id for event in events} == {"session-abc", "selected-child"}
    assert len(parsed_paths) == 2
    assert all("excluded-root" not in path for path in parsed_paths)


def test_discover_client_usage_with_diagnostics_explains_root_group_limit(tmp_path):
    codex_home = _make_codex_home(tmp_path, model="gpt-5.6-sol")
    _add_codex_thread(
        codex_home,
        session_id="linked-review-newest",
        updated_at=500,
        model="codex-auto-review",
        parent_session_id="session-abc",
    )
    _add_codex_thread(
        codex_home,
        session_id="linked-review-older",
        updated_at=400,
        model="codex-auto-review",
        parent_session_id="session-abc",
    )
    _add_codex_thread(
        codex_home,
        session_id="unrelated-root",
        updated_at=450,
        model="gpt-5.7",
    )

    result = discover_client_usage_with_diagnostics(
        client="codex",
        codex_home=codex_home,
        limit_sessions=1,
    )

    assert len(result.events) == 3
    assert result.diagnostics["codex"] == {
        "client": "codex",
        "discovered": 4,
        "parsed": 3,
        "skipped": 0,
        "error_count": 0,
        "error_codes": [],
        "watermark": 500,
        "limit_unit": "root_groups",
        "selected_root_groups": 1,
        "returned_root_groups": 1,
        "returned_rows": 3,
        "excluded_by_limit": 1,
        "ignored_non_transcript_files": 0,
        "unresolved_identity_files": 0,
        "excluded_by_source_namespace": 0,
        "unparsed_selected_rows": 0,
        "observed_sessions": 3,
        "usage_sessions": 3,
        "sessions_without_usage": 0,
        "source_present": True,
    }


def test_discover_claude_usage_limit_selects_complete_recent_root_group(
    tmp_path,
    monkeypatch,
):
    claude_home = _make_claude_home(tmp_path)
    project = claude_home / "projects" / "-tmp-project"
    root = project / "claude-session.jsonl"

    def write_transcript(path: Path, *, session_id: str, input_tokens: int) -> None:
        path.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "sessionId": session_id,
                    "timestamp": "2026-07-01T00:00:00Z",
                    "message": {
                        "model": "claude-opus-4-8",
                        "usage": {
                            "input_tokens": input_tokens,
                            "output_tokens": 1,
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

    newest_child = project / "agent-newest.jsonl"
    older_child = project / "agent-older.jsonl"
    unrelated_root = project / "unrelated-root.jsonl"
    write_transcript(newest_child, session_id="claude-session", input_tokens=3)
    write_transcript(older_child, session_id="claude-session", input_tokens=2)
    write_transcript(unrelated_root, session_id="unrelated-root", input_tokens=9)
    os.utime(root, (100, 100))
    os.utime(older_child, (400, 400))
    os.utime(unrelated_root, (450, 450))
    os.utime(newest_child, (500, 500))

    fully_parsed: list[str] = []
    original = client_usage_module._read_claude_project_usages

    def tracked(path, **kwargs):
        fully_parsed.append(path.name)
        return original(path, **kwargs)

    monkeypatch.setattr(client_usage_module, "_read_claude_project_usages", tracked)

    result = discover_client_usage_with_diagnostics(
        client="claude-code",
        claude_home=claude_home,
        limit_sessions=1,
    )

    assert {event.client_session_id for event in result.events} == {
        "claude-session",
        "claude-session:agent-newest",
        "claude-session:agent-older",
    }
    assert set(fully_parsed) == {
        "claude-session.jsonl",
        "agent-newest.jsonl",
        "agent-older.jsonl",
    }
    assert result.diagnostics["claude-code"] == {
        "client": "claude-code",
        "discovered": 4,
        "parsed": 3,
        "skipped": 0,
        "error_count": 0,
        "error_codes": [],
        "watermark": 1_782_864_000,
        "limit_unit": "root_groups",
        "selected_root_groups": 1,
        "returned_root_groups": 1,
        "returned_rows": 3,
        "excluded_by_limit": 1,
        "ignored_non_transcript_files": 0,
        "unresolved_identity_files": 0,
        "excluded_by_source_namespace": 0,
        "unparsed_selected_rows": 0,
        "observed_sessions": 3,
        "usage_sessions": 3,
        "sessions_without_usage": 0,
        "source_present": True,
        "skipped_unsafe_paths": 0,
    }


def test_claude_usage_less_root_keeps_all_usage_children_in_one_global_group(
    tmp_path,
):
    claude_home = tmp_path / "claude-home"
    project = claude_home / "projects" / "-tmp-project"
    project.mkdir(parents=True)
    root = project / "root-session.jsonl"
    root.write_text(
        json.dumps({"type": "system", "sessionId": "root-session"}) + "\n",
        encoding="utf-8",
    )

    def write_usage(path: Path, *, session_id: str, message_id: str, tokens: int) -> None:
        path.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": message_id,
                    "sessionId": session_id,
                    "timestamp": "2026-07-01T00:00:00Z",
                    "message": {
                        "model": "claude-opus-4-8",
                        "usage": {
                            "input_tokens": tokens,
                            "output_tokens": 1,
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

    child_a = project / "child-a.jsonl"
    child_b = project / "child-b.jsonl"
    unrelated = project / "unrelated.jsonl"
    write_usage(
        child_a,
        session_id="root-session",
        message_id="message-a",
        tokens=3,
    )
    write_usage(
        child_b,
        session_id="root-session",
        message_id="message-b",
        tokens=4,
    )
    write_usage(
        unrelated,
        session_id="unrelated",
        message_id="message-unrelated",
        tokens=9,
    )
    os.utime(root, (100, 100))
    os.utime(child_b, (400, 400))
    os.utime(unrelated, (450, 450))
    os.utime(child_a, (500, 500))

    result = discover_client_usage_with_diagnostics(
        client="claude-code",
        claude_home=claude_home,
        limit_sessions=1,
    )

    assert {event.client_session_id for event in result.events} == {
        "root-session:child-a",
        "root-session:child-b",
    }
    assert all(
        event.parent_client_session_id == "root-session"
        for event in result.events
    )
    diagnostic = result.diagnostics["claude-code"]
    assert diagnostic["selected_root_groups"] == 1
    assert diagnostic["returned_root_groups"] == 1
    assert diagnostic["returned_rows"] == 2
    assert diagnostic["excluded_by_limit"] == 1
    assert diagnostic["error_count"] == 0
    assert diagnostic["observed_sessions"] == 3
    assert diagnostic["usage_sessions"] == 2
    assert diagnostic["sessions_without_usage"] == 1
    assert {
        observation.client_session_id
        for observation in result.session_observations
    } == {
        "root-session",
        "root-session:child-a",
        "root-session:child-b",
    }


def test_codex_zero_token_thread_is_observed_without_fabricating_usage(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    rollout = next((codex_home / "sessions").rglob("rollout-*.jsonl"))
    rollout.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "timestamp": "2026-07-01T00:00:00Z",
                        "payload": {
                            "id": "session-abc",
                            "cwd": "/work/project",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "timestamp": "2026-07-01T00:00:01Z",
                        "payload": {"type": "task_started"},
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "timestamp": "2026-07-01T00:00:02Z",
                        "payload": {"type": "task_complete"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    connection = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        connection.execute(
            "update threads set tokens_used = 0 where id = ?",
            ("session-abc",),
        )
        connection.commit()
    finally:
        connection.close()

    result = discover_client_usage_with_diagnostics(
        client="codex",
        codex_home=codex_home,
    )

    assert result.events == []
    assert [row.client_session_id for row in result.session_observations] == [
        "session-abc"
    ]
    assert result.diagnostics["codex"]["observed_sessions"] == 1
    assert result.diagnostics["codex"]["usage_sessions"] == 0
    assert result.diagnostics["codex"]["sessions_without_usage"] == 1
    candidates = usage_less_session_observations(result)
    assert len(candidates) == 1
    assert candidates[0].source_revision_at == rollout.stat().st_mtime_ns
    assert candidates[0].source_revision_basis == "file_mtime_ns"
    event = candidates[0].to_sentinel_event()
    assert event["event_type"] == "session_observed"
    assert not {
        "provider",
        "model",
        "estimated_input_tokens",
        "estimated_output_tokens",
        "estimated_cost_usd",
    }.intersection(event)


def test_codex_rollout_only_session_is_observed_without_sqlite(tmp_path):
    codex_home = tmp_path / "codex-home"
    sessions = codex_home / "sessions" / "2026" / "07" / "01"
    sessions.mkdir(parents=True)
    rollout = sessions / "rollout-rollout-only.jsonl"
    rollout.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "timestamp": "2026-07-01T00:00:00Z",
                        "payload": {
                            "id": "rollout-only-session",
                            "cwd": "/work/rollout-only",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "timestamp": "2026-07-01T00:00:01Z",
                        "payload": {"type": "task_complete"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = discover_client_usage_with_diagnostics(
        client="codex",
        codex_home=codex_home,
    )

    assert result.events == []
    assert [row.client_session_id for row in result.session_observations] == [
        "rollout-only-session"
    ]
    observation = result.session_observations[0]
    assert observation.cwd == "/work/rollout-only"
    assert observation.observation_basis == "codex_rollout_identity"
    assert observation.source_revision_at == rollout.stat().st_mtime_ns
    assert observation.source_revision_basis == "file_mtime_ns"
    assert result.diagnostics["codex"]["discovered"] == 1
    assert result.diagnostics["codex"]["sessions_without_usage"] == 1
    assert result.diagnostics["codex"]["source_present"] is True


def test_codex_unindexed_rollout_without_session_meta_is_fail_visible(tmp_path):
    codex_home = tmp_path / "codex-home"
    sessions = codex_home / "sessions" / "2026" / "07" / "01"
    sessions.mkdir(parents=True)
    valid = sessions / "rollout-valid.jsonl"
    valid.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "timestamp": "2026-07-01T00:00:00Z",
                "payload": {"id": "valid-rollout-only"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    malformed = sessions / "rollout-missing-session-meta.jsonl"
    malformed.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "task_complete"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = discover_client_usage_with_diagnostics(
        client="codex",
        codex_home=codex_home,
        limit_sessions=10,
    )

    assert [row.client_session_id for row in result.session_observations] == [
        "valid-rollout-only"
    ]
    diagnostic = result.diagnostics["codex"]
    assert diagnostic["source_present"] is True
    assert diagnostic["unresolved_identity_files"] == 1
    assert diagnostic["error_count"] == 1
    assert diagnostic["error_codes"] == ["codex_rollout_identity_unresolved"]


def test_codex_unindexed_archived_rollout_is_included_in_inventory(tmp_path):
    codex_home = tmp_path / "codex-home"
    archived = codex_home / "archived_sessions"
    archived.mkdir(parents=True)
    rollout = archived / "rollout-archived.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "timestamp": "2026-07-01T00:00:00Z",
                "payload": {"id": "archived-rollout-only"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = discover_client_usage_with_diagnostics(
        client="codex",
        codex_home=codex_home,
        limit_sessions=10,
    )

    assert [row.client_session_id for row in result.session_observations] == [
        "archived-rollout-only"
    ]
    assert result.diagnostics["codex"]["source_present"] is True
    assert result.diagnostics["codex"]["error_count"] == 0


@pytest.mark.parametrize(
    ("client", "home_argument"),
    [
        ("codex", "codex_home"),
        ("claude-code", "claude_home"),
        ("hermes", "hermes_home"),
    ],
)
def test_core_usage_diagnostics_do_not_infer_source_presence_from_junk_home(
    tmp_path,
    client,
    home_argument,
):
    junk_home = tmp_path / f"{client}-junk-home"
    junk_home.mkdir()
    (junk_home / "unrelated.txt").write_text("not an authority carrier\n", encoding="utf-8")

    result = discover_client_usage_with_diagnostics(
        client=client,
        **{home_argument: junk_home},
    )

    assert result.events == []
    assert result.diagnostics[client]["source_present"] is False


def test_codex_nested_rollout_directory_symlink_is_fail_visible(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    foreign = tmp_path / "foreign-rollouts"
    foreign.mkdir()
    (codex_home / "sessions" / "linked-history").symlink_to(
        foreign,
        target_is_directory=True,
    )

    result = discover_client_usage_with_diagnostics(
        client="codex",
        codex_home=codex_home,
        limit_sessions=10,
    )

    diagnostic = result.diagnostics["codex"]
    assert diagnostic["source_present"] is True
    assert diagnostic["error_count"] == 1
    assert diagnostic["error_codes"] == ["codex_rollout_inventory_failed"]


def test_codex_rollout_traversal_error_is_not_silently_omitted(
    tmp_path,
    monkeypatch,
):
    codex_home = _make_codex_home(tmp_path)
    real_scandir = client_usage_module.os.scandir

    def fail_descriptor_scan(path):
        if isinstance(path, int):
            raise PermissionError("synthetic traversal denial")
        return real_scandir(path)

    monkeypatch.setattr(client_usage_module.os, "scandir", fail_descriptor_scan)

    result = discover_client_usage_with_diagnostics(
        client="codex",
        codex_home=codex_home,
        limit_sessions=10,
    )

    diagnostic = result.diagnostics["codex"]
    assert diagnostic["source_present"] is True
    assert diagnostic["error_count"] == 1
    assert diagnostic["error_codes"] == ["codex_rollout_inventory_failed"]


def test_claude_projects_traversal_error_is_not_silently_omitted(
    tmp_path,
    monkeypatch,
):
    claude_home = _make_claude_home(tmp_path)
    real_scandir = client_usage_module.os.scandir

    def fail_descriptor_scan(path):
        if isinstance(path, int):
            raise PermissionError("synthetic traversal denial")
        return real_scandir(path)

    monkeypatch.setattr(client_usage_module.os, "scandir", fail_descriptor_scan)

    result = discover_client_usage_with_diagnostics(
        client="claude-code",
        claude_home=claude_home,
        limit_sessions=10,
    )

    assert result.events == []
    diagnostic = result.diagnostics["claude-code"]
    assert diagnostic["source_present"] is True
    assert diagnostic["error_count"] == 1
    assert diagnostic["error_codes"] == ["claude_transcript_discovery_failed"]


def test_claude_projects_tree_race_is_fail_visible(
    tmp_path,
    monkeypatch,
):
    claude_home = _make_claude_home(tmp_path)
    transcript = (
        claude_home / "projects" / "-tmp-project" / "claude-session.jsonl"
    )
    real_stat = client_usage_module.os.stat
    mutated = False

    def race_after_inventory_stat(path, *args, **kwargs):
        nonlocal mutated
        observed = real_stat(path, *args, **kwargs)
        if (
            not mutated
            and path == transcript.name
            and kwargs.get("dir_fd") is not None
            and kwargs.get("follow_symlinks") is False
        ):
            mutated = True
            transcript.write_bytes(transcript.read_bytes() + b"\n")
        return observed

    monkeypatch.setattr(client_usage_module.os, "stat", race_after_inventory_stat)

    result = discover_client_usage_with_diagnostics(
        client="claude-code",
        claude_home=claude_home,
        limit_sessions=10,
    )

    assert mutated is True
    assert result.events == []
    diagnostic = result.diagnostics["claude-code"]
    assert diagnostic["error_count"] == 1
    assert diagnostic["error_codes"] == [
        "claude_transcript_changed_during_scan"
    ]


def test_existing_unsafe_hermes_state_db_carrier_is_fail_visible(tmp_path):
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    foreign_home = _make_hermes_home(foreign_root)
    (hermes_home / "state.db").symlink_to(foreign_home / "state.db")

    result = discover_client_usage_with_diagnostics(
        client="hermes",
        hermes_home=hermes_home,
        limit_sessions=10,
    )

    assert result.events == []
    diagnostic = result.diagnostics["hermes"]
    assert diagnostic["source_present"] is False
    assert diagnostic["error_count"] == 1
    assert diagnostic["error_codes"] == [
        "hermes_state_db_carrier_unreadable"
    ]


def test_claude_explicit_all_zero_usage_is_observation_only(tmp_path):
    claude_home = tmp_path / "claude-home"
    project = claude_home / "projects" / "-tmp-project"
    project.mkdir(parents=True)
    transcript = project / "zero-session.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "zero-session",
                "cwd": "/work/project",
                "timestamp": "2026-07-01T00:00:00Z",
                "message": {
                    "model": "<synthetic>",
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = discover_client_usage_with_diagnostics(
        client="claude-code",
        claude_home=claude_home,
    )

    assert result.events == []
    assert [row.client_session_id for row in result.session_observations] == [
        "zero-session"
    ]
    assert result.session_observations[0].source_revision_at == transcript.stat().st_mtime_ns
    assert result.session_observations[0].source_revision_basis == "file_mtime_ns"
    assert result.diagnostics["claude-code"]["parsed"] == 1
    assert result.diagnostics["claude-code"]["skipped"] == 0
    assert result.diagnostics["claude-code"]["sessions_without_usage"] == 1


def test_usage_less_observations_are_scoped_by_client_home(tmp_path):
    home_a = "sha256:" + "a" * 64
    home_b = "sha256:" + "b" * 64
    usage = ClientUsageEvent(
        client="codex",
        client_session_id="shared-session",
        source_path=tmp_path / "home-a.jsonl",
        title=None,
        cwd="/work/a",
        model="gpt-test",
        input_tokens=1,
        output_tokens=1,
        source_namespace_fingerprint=home_a,
    )
    observation_a = ClientSessionObservation(
        client="codex",
        client_session_id="shared-session",
        source_path=tmp_path / "home-a.jsonl",
        updated_at=100,
        source_namespace_fingerprint=home_a,
    )
    observation_b = ClientSessionObservation(
        client="codex",
        client_session_id="shared-session",
        source_path=tmp_path / "home-b.jsonl",
        updated_at=200,
        source_namespace_fingerprint=home_b,
    )
    discovery = ClientUsageDiscoveryResult(
        events=[usage],
        diagnostics={"codex": {"error_count": 0}},
        session_observations=[observation_a, observation_b],
    )

    candidates = usage_less_session_observations(discovery)

    assert candidates == [observation_b]


def test_two_usage_less_observations_with_same_raw_id_keep_both_homes(tmp_path):
    observations = [
        ClientSessionObservation(
            client="codex",
            client_session_id="shared-session",
            source_path=tmp_path / f"home-{suffix}.jsonl",
            updated_at=updated_at,
            source_namespace_fingerprint="sha256:" + suffix * 64,
        )
        for suffix, updated_at in (("a", 100), ("b", 200))
    ]
    discovery = ClientUsageDiscoveryResult(
        events=[],
        diagnostics={"codex": {"error_count": 0}},
        session_observations=observations,
    )

    candidates = usage_less_session_observations(discovery)

    assert candidates == observations

    merged_stats: dict[str, object] = {}
    client_usage_module._merge_multi_home_discovery_stats(
        merged_stats,
        [],
        events=[],
        limit_unit="sessions",
        selected_root_groups=None,
        extra_error_codes=[],
        extra_error_count=0,
        extra_excluded_by_limit=0,
        excluded_by_namespace=0,
        observations=observations,
    )
    assert merged_stats["observed_sessions"] == 2
    assert merged_stats["usage_sessions"] == 0
    assert merged_stats["sessions_without_usage"] == 2


def test_valid_usage_less_observation_survives_unrelated_client_parse_error(tmp_path):
    valid = ClientSessionObservation(
        client="claude-code",
        client_session_id="valid-session",
        source_path=tmp_path / "valid.jsonl",
        updated_at=100,
        source_namespace_fingerprint="sha256:" + "a" * 64,
        source_parse_complete=True,
    )
    incomplete = ClientSessionObservation(
        client="claude-code",
        client_session_id="incomplete-session",
        source_path=tmp_path / "incomplete.jsonl",
        updated_at=200,
        source_namespace_fingerprint="sha256:" + "a" * 64,
        source_parse_complete=False,
    )
    discovery = ClientUsageDiscoveryResult(
        events=[],
        diagnostics={"claude-code": {"error_count": 1}},
        session_observations=[valid, incomplete],
    )

    assert usage_less_session_observations(discovery) == [valid]


def test_claude_root_identity_scan_is_prefix_bounded(tmp_path):
    transcript = tmp_path / "oversized-prefix.jsonl"
    transcript.write_bytes(
        b"x" * (client_usage_module._CLAUDE_IDENTITY_SCAN_MAX_BYTES + 1_024)
    )

    session_id, complete = client_usage_module._peek_claude_session_id(transcript)

    assert session_id is None
    assert complete is False


def test_claude_workflow_journals_do_not_enter_transcript_selection(
    tmp_path,
    monkeypatch,
):
    claude_home = _make_claude_home(tmp_path)
    project = claude_home / "projects" / "-tmp-project"
    workflow_root = (
        project
        / "claude-session"
        / "subagents"
        / "workflows"
    )
    small_journal = workflow_root / "wf_small" / "journal.jsonl"
    large_journal = workflow_root / "wf_large" / "journal.jsonl"
    small_journal.parent.mkdir(parents=True)
    large_journal.parent.mkdir(parents=True)
    small_journal.write_text(
        json.dumps({"agentId": "agent-a", "key": "state", "type": "started"})
        + "\n",
        encoding="utf-8",
    )
    large_journal.write_text(
        json.dumps(
            {
                "agentId": "agent-b",
                "key": "state",
                "result": "x"
                * (client_usage_module._CLAUDE_IDENTITY_SCAN_MAX_BYTES + 1_024),
                "type": "result",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    os.utime(project / "claude-session.jsonl", (100, 100))
    os.utime(small_journal, (500, 500))
    os.utime(large_journal, (600, 600))

    fully_parsed: list[Path] = []
    original = client_usage_module._read_claude_project_usages

    def tracked(path, **kwargs):
        fully_parsed.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(client_usage_module, "_read_claude_project_usages", tracked)

    result = discover_client_usage_with_diagnostics(
        client="claude-code",
        claude_home=claude_home,
        limit_sessions=1,
    )

    assert [event.client_session_id for event in result.events] == ["claude-session"]
    assert fully_parsed == [project / "claude-session.jsonl"]
    diagnostic = result.diagnostics["claude-code"]
    assert diagnostic["discovered"] == 3
    assert diagnostic["ignored_non_transcript_files"] == 2
    assert diagnostic["selected_root_groups"] == 1
    assert diagnostic["returned_root_groups"] == 1
    assert diagnostic["error_count"] == 0
    assert diagnostic["error_codes"] == []
    assert result.events[0].source_parse_complete is True


def test_claude_workflow_journal_schema_drift_fails_closed(tmp_path):
    claude_home = _make_claude_home(tmp_path)
    project = claude_home / "projects" / "-tmp-project"
    journal = (
        project
        / "claude-session"
        / "subagents"
        / "workflows"
        / "wf_drift"
        / "journal.jsonl"
    )
    journal.parent.mkdir(parents=True)
    journal.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "claude-session",
                "message": {
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 999, "output_tokens": 99},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = discover_client_usage_with_diagnostics(
        client="claude-code",
        claude_home=claude_home,
        limit_sessions=10,
    )

    assert result.events == []
    assert result.diagnostics["claude-code"]["discovered"] == 2
    assert result.diagnostics["claude-code"]["error_codes"] == [
        "claude_workflow_journal_schema_drift"
    ]


def test_claude_workflow_journal_mutation_after_validation_fails_closed(
    tmp_path,
    monkeypatch,
):
    claude_home = _make_claude_home(tmp_path)
    project = claude_home / "projects" / "-tmp-project"
    journal = (
        project
        / "claude-session"
        / "subagents"
        / "workflows"
        / "wf_mutating"
        / "journal.jsonl"
    )
    journal.parent.mkdir(parents=True)
    journal.write_text(
        json.dumps({"agentId": "agent-a", "key": "state", "type": "started"})
        + "\n",
        encoding="utf-8",
    )
    original = client_usage_module._read_claude_project_usages
    mutated = False

    def mutate_journal(path, **kwargs):
        nonlocal mutated
        if not mutated:
            with journal.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "agentId": "agent-a",
                            "key": "state",
                            "result": "done",
                            "type": "result",
                        }
                    )
                    + "\n"
                )
            mutated = True
        return original(path, **kwargs)

    monkeypatch.setattr(
        client_usage_module,
        "_read_claude_project_usages",
        mutate_journal,
    )

    result = discover_client_usage_with_diagnostics(
        client="claude-code",
        claude_home=claude_home,
        limit_sessions=10,
    )

    assert [event.client_session_id for event in result.events] == [
        "claude-session"
    ]
    # issue #53 follow-up: the journal change is still detected and flagged
    # (error_codes below), but a changed workflow journal — which carries no
    # usage — no longer withholds the real session's usage.
    assert result.events[0].source_parse_complete is True
    assert result.diagnostics["claude-code"]["error_codes"] == [
        "claude_transcript_changed_during_scan"
    ]
    assert plan_local_usage_import(result.events, []).new_candidates == result.events


def test_claude_projects_root_symlink_is_rejected_as_unsafe(tmp_path):
    foreign_home = _make_claude_home(tmp_path / "foreign")
    claude_home = tmp_path / "linked-home"
    claude_home.mkdir()
    (claude_home / "projects").symlink_to(foreign_home / "projects")

    result = discover_client_usage_with_diagnostics(
        client="claude-code",
        claude_home=claude_home,
        limit_sessions=10,
    )

    assert result.events == []
    assert result.diagnostics["claude-code"]["error_codes"] == [
        "claude_transcript_unsafe_path"
    ]


def test_claude_descendant_directory_symlink_is_skipped_not_fatal(tmp_path):
    # issue #84: a descendant directory symlink is never FOLLOWED (that could
    # smuggle a foreign subtree into a "complete" scan), but it must not abort
    # the whole home. The link is skipped and surfaced; every legitimate
    # sibling transcript still imports instead of the home returning zero.
    claude_home = _make_claude_home(tmp_path)
    projects_root = claude_home / "projects"
    foreign_home = _make_claude_home(tmp_path / "foreign")
    # Plant a distinctly-named transcript behind the symlink so the test proves
    # no-follow: if the link were traversed, this id would appear in the import.
    (foreign_home / "projects" / "-tmp-project" / "foreign-only.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "foreign-secret",
                "message": {
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 99, "output_tokens": 9},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (projects_root / "linked-project").symlink_to(
        foreign_home / "projects" / "-tmp-project"
    )

    result = discover_client_usage_with_diagnostics(
        client="claude-code",
        claude_home=claude_home,
        limit_sessions=10,
    )

    imported_ids = [event.client_session_id for event in result.events]
    assert imported_ids == ["claude-session"]
    # The foreign session behind the symlink is NOT imported (no-follow holds).
    assert "foreign-secret" not in imported_ids
    assert result.events[0].source_parse_complete is True
    diagnostic = result.diagnostics["claude-code"]
    assert diagnostic["error_codes"] == ["claude_transcript_unsafe_path"]
    assert diagnostic["error_count"] == 1
    assert diagnostic["skipped_unsafe_paths"] == 1


def test_claude_non_carrier_file_symlinks_do_not_block_discovery(tmp_path):
    claude_home = _make_claude_home(tmp_path)
    project = claude_home / "projects" / "-tmp-project"
    # A "latest"-style convenience pointer at a regular file and a dangling
    # sibling are side files, not transcript carriers: neither may condemn
    # the walk or enter the imported cohort.
    (project / "latest").symlink_to(project / "claude-session.jsonl")
    (project / "stale-pointer").symlink_to(project / "does-not-exist")

    result = discover_client_usage_with_diagnostics(
        client="claude-code",
        claude_home=claude_home,
        limit_sessions=10,
    )

    assert [event.client_session_id for event in result.events] == [
        "claude-session"
    ]
    assert result.events[0].source_parse_complete is True
    diagnostic = result.diagnostics["claude-code"]
    assert diagnostic["error_count"] == 0
    assert diagnostic["error_codes"] == []


def test_claude_non_carrier_symlink_to_directory_is_skipped_not_fatal(tmp_path):
    # issue #84: a directory symlink nested beside real transcripts (e.g. a
    # "current"-style convenience pointer) is skipped and surfaced, but the
    # sibling transcript in the same project still imports.
    claude_home = _make_claude_home(tmp_path)
    project = claude_home / "projects" / "-tmp-project"
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (project / "current").symlink_to(outside_dir)

    result = discover_client_usage_with_diagnostics(
        client="claude-code",
        claude_home=claude_home,
        limit_sessions=10,
    )

    assert [event.client_session_id for event in result.events] == [
        "claude-session"
    ]
    assert result.events[0].source_parse_complete is True
    diagnostic = result.diagnostics["claude-code"]
    assert diagnostic["error_codes"] == ["claude_transcript_unsafe_path"]
    assert diagnostic["error_count"] == 1
    assert diagnostic["skipped_unsafe_paths"] == 1


def test_claude_shared_memory_symlink_does_not_zero_the_home(tmp_path):
    # issue #84 reproduction: a shared memory directory symlinked into a project
    # dir (a common cross-machine sync pattern) used to raise on the first
    # symlinked component of the O_NOFOLLOW walk, and the caller discarded every
    # transcript in the home. Now the link is skipped and the real sessions
    # import. Two projects, each with its own session, plus one shared-memory
    # symlink apiece.
    claude_home = tmp_path / "claude-home"
    projects_root = claude_home / "projects"
    shared_memory = tmp_path / "shared-memory"
    shared_memory.mkdir(parents=True)
    (shared_memory / "MEMORY.md").write_text("shared notes\n", encoding="utf-8")

    for index, project_name in enumerate(("-Users-me", "-Users-me-Downloads")):
        project = projects_root / project_name
        project.mkdir(parents=True)
        session = project / f"session-{index}.jsonl"
        session.write_text(
            "\n".join(
                json.dumps(row)
                for row in (
                    {
                        "type": "ai-title",
                        "sessionId": f"session-{index}",
                        "aiTitle": "Real work",
                    },
                    {
                        "type": "assistant",
                        "sessionId": f"session-{index}",
                        "message": {
                            "model": "claude-opus-4-8",
                            "usage": {
                                "input_tokens": 11,
                                "output_tokens": 3,
                            },
                        },
                    },
                )
            )
            + "\n",
            encoding="utf-8",
        )
        (project / "memory").symlink_to(shared_memory, target_is_directory=True)

    result = discover_client_usage_with_diagnostics(
        client="claude-code",
        claude_home=claude_home,
        limit_sessions=10,
    )

    assert {event.client_session_id for event in result.events} == {
        "session-0",
        "session-1",
    }
    assert all(event.source_parse_complete is True for event in result.events)
    diagnostic = result.diagnostics["claude-code"]
    assert diagnostic["error_codes"] == ["claude_transcript_unsafe_path"]
    # One skipped memory symlink per project.
    assert diagnostic["skipped_unsafe_paths"] == 2
    assert diagnostic["error_count"] == 2


def test_claude_non_regular_jsonl_is_rejected_without_blocking(tmp_path):
    claude_home = _make_claude_home(tmp_path)
    fifo = claude_home / "projects" / "-tmp-project" / "blocked.jsonl"
    os.mkfifo(fifo)

    result = discover_client_usage_with_diagnostics(
        client="claude-code",
        claude_home=claude_home,
        limit_sessions=10,
    )

    # issue #53: the FIFO is still rejected (unsafe path, surfaced in
    # error_codes) but no longer blocks the readable sibling's usage.
    assert [event.client_session_id for event in result.events] == [
        "claude-session"
    ]
    assert result.events[0].source_parse_complete is True
    assert result.diagnostics["claude-code"]["error_codes"] == [
        "claude_transcript_unsafe_path"
    ]


def test_unknown_truncated_claude_identity_is_non_writable_and_does_not_consume_limit(
    tmp_path,
):
    claude_home = _make_claude_home(tmp_path)
    project = claude_home / "projects" / "-tmp-project"
    unresolved = project / "late-child.jsonl"
    unresolved.write_bytes(
        b"x" * (client_usage_module._CLAUDE_IDENTITY_SCAN_MAX_BYTES + 1_024)
        + b"\n"
        + json.dumps(
            {
                "type": "assistant",
                "sessionId": "claude-session",
                "message": {
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                },
            }
        ).encode("utf-8")
        + b"\n"
    )
    os.utime(project / "claude-session.jsonl", (100, 100))
    os.utime(unresolved, (600, 600))

    result = discover_client_usage_with_diagnostics(
        client="claude-code",
        claude_home=claude_home,
        limit_sessions=1,
    )

    # issue #53: the truncated file is still excluded (non-writable, does not
    # consume the limit slot) and surfaced in error_codes, but the readable
    # session now imports instead of being withheld along with it.
    assert [event.client_session_id for event in result.events] == ["claude-session"]
    assert result.events[0].source_parse_complete is True
    diagnostic = result.diagnostics["claude-code"]
    assert diagnostic["selected_root_groups"] == 1
    assert diagnostic["error_codes"] == [
        "claude_transcript_identity_scan_truncated"
    ]
    plan = plan_local_usage_import(result.events, [])
    assert plan.new_candidates == result.events
    assert plan.refresh_candidates == []
    assert plan.migration_candidates == []
    assert plan.incomplete_source_candidates == []


def test_claude_descendant_file_symlink_is_rejected_without_importing_foreign_data(
    tmp_path,
):
    claude_home = _make_claude_home(tmp_path)
    project = claude_home / "projects" / "-tmp-project"
    foreign = tmp_path / "foreign-private.jsonl"
    foreign.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "foreign-session",
                "customTitle": "FOREIGN PRIVATE TITLE",
                "cwd": "/foreign/private/project",
                "message": {
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 999, "output_tokens": 99},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (project / "linked.jsonl").symlink_to(foreign)

    result = discover_client_usage_with_diagnostics(
        client="claude-code",
        claude_home=claude_home,
        limit_sessions=10,
    )

    # issue #53: the symlinked foreign file is still excluded (unsafe path) and
    # its data never enters an event; the readable sibling now imports normally.
    assert [event.client_session_id for event in result.events] == ["claude-session"]
    assert result.events[0].source_parse_complete is True
    assert result.diagnostics["claude-code"]["error_codes"] == [
        "claude_transcript_unsafe_path"
    ]
    serialized = json.dumps(
        [event.to_sentinel_event() for event in result.events],
        sort_keys=True,
    )
    assert "FOREIGN PRIVATE TITLE" not in serialized
    assert "/foreign/private/project" not in serialized
    assert plan_local_usage_import(result.events, []).new_candidates == result.events


def test_claude_transcript_replacement_between_identity_and_usage_reads_fails_closed(
    tmp_path,
    monkeypatch,
):
    claude_home = _make_claude_home(tmp_path)
    transcript = (
        claude_home / "projects" / "-tmp-project" / "claude-session.jsonl"
    )
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "foreign-session",
                "customTitle": "REPLACED PRIVATE TITLE",
                "message": {
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 999, "output_tokens": 99},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    original = client_usage_module._read_claude_project_usages
    replaced = False

    def retarget(path, **kwargs):
        nonlocal replaced
        if path == transcript and not replaced:
            os.replace(replacement, transcript)
            replaced = True
        return original(path, **kwargs)

    monkeypatch.setattr(client_usage_module, "_read_claude_project_usages", retarget)

    result = discover_client_usage_with_diagnostics(
        client="claude-code",
        claude_home=claude_home,
        limit_sessions=10,
    )

    assert result.events == []
    assert result.diagnostics["claude-code"]["error_codes"] == [
        "claude_transcript_changed_during_scan"
    ]


def test_claude_in_place_append_between_identity_and_usage_reads_fails_closed(
    tmp_path,
    monkeypatch,
):
    claude_home = _make_claude_home(tmp_path)
    project = claude_home / "projects" / "-tmp-project"
    transcript = project / "late-child.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    os.utime(project / "claude-session.jsonl", (100, 100))
    os.utime(transcript, (500, 500))
    original = client_usage_module._read_claude_project_usages
    appended = False

    def append_identity(path, **kwargs):
        nonlocal appended
        if path == transcript and not appended:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "assistant",
                            "sessionId": "claude-session",
                            "message": {
                                "model": "claude-opus-4-8",
                                "usage": {
                                    "input_tokens": 999,
                                    "output_tokens": 99,
                                },
                            },
                        }
                    )
                    + "\n"
                )
            appended = True
        return original(path, **kwargs)

    monkeypatch.setattr(
        client_usage_module,
        "_read_claude_project_usages",
        append_identity,
    )

    result = discover_client_usage_with_diagnostics(
        client="claude-code",
        claude_home=claude_home,
        limit_sessions=1,
    )

    assert result.events == []
    assert result.diagnostics["claude-code"]["error_codes"] == [
        "claude_transcript_changed_during_scan"
    ]


def test_transcript_changed_mid_read_keeps_siblings_and_does_not_undercount_replay(
    tmp_path,
    monkeypatch,
):
    # A parent transcript rewritten mid-read raises changed_during_scan and is
    # skipped — but (a) other clean sessions still import (the cohort is no longer
    # withheld for a transient race), and (b) dedup is transactional so the parent
    # commits NO keys before raising: a sidechain child replaying the parent's rows
    # is COUNTED, not silently deduped to zero (issue #53 follow-up).
    claude_home = _make_claude_home(tmp_path)  # a clean "claude-session" session
    project = claude_home / "projects" / "-tmp-project"

    def _u(session_id: str, msg_id: str, out: int) -> str:
        return json.dumps({
            "type": "assistant", "sessionId": session_id,
            "timestamp": "2026-08-01T00:00:00Z",
            "message": {"id": msg_id, "role": "assistant", "model": "claude-opus-4-8",
                        "usage": {"input_tokens": 100, "output_tokens": out}},
        }) + "\n"

    parent = project / "run.jsonl"          # root: stem "run" == sessionId "run"
    child = project / "agent-sub.jsonl"     # child of "run" (stem != sessionId), replays m1
    parent.write_text(_u("run", "m1", 200), encoding="utf-8")
    child.write_text(_u("run", "m1", 200), encoding="utf-8")
    os.utime(parent, (500, 500))
    os.utime(child, (450, 450))

    # Append to the PARENT during its usage read (after it has staged m1) so the
    # END-fingerprint check fails -> changed_during_scan. _claude_usage_dedupe_key
    # is invoked per usage row inside the read loop.
    original = client_usage_module._claude_usage_dedupe_key
    state = {"done": False}

    def mutate(path, obj, message, usage):
        if path == parent and not state["done"]:
            with parent.open("a", encoding="utf-8") as fh:
                fh.write(_u("run", "extra", 1))
            state["done"] = True
        return original(path, obj, message, usage)

    monkeypatch.setattr(client_usage_module, "_claude_usage_dedupe_key", mutate)

    result = discover_client_usage_with_diagnostics(
        client="claude-code", claude_home=claude_home, limit_sessions=200,
    )

    by_id = {str(e.client_session_id): e for e in result.events}
    # The clean pre-existing session still imports; nothing is withheld.
    assert "claude-session" in by_id
    assert all(e.source_parse_complete for e in result.events)
    # The racing parent was skipped, so the "run" lineage's usage is the child's
    # replay — COUNTED (200 out), not deduped to zero by a ghost key.
    run_events = [e for e in result.events if str(e.client_session_id).startswith("run")]
    assert run_events, "the child's replay must be imported, not lost to a ghost dedup key"
    assert sum(e.output_tokens for e in run_events) == 200
    assert (
        "claude_transcript_changed_during_scan"
        in result.diagnostics["claude-code"]["error_codes"]
    )


def test_claude_source_namespace_stays_bound_to_held_root_during_retarget(
    tmp_path,
    monkeypatch,
):
    claude_home = _make_claude_home(tmp_path / "source-a")
    foreign_home = _make_claude_home(tmp_path / "source-b")
    expected_namespace = client_usage_module._source_home_namespace(
        claude_home.resolve(),
        already_canonical=True,
    )
    original = client_usage_module._discover_claude_code_usage_from_home
    moved_home = claude_home.with_name("claude-home-held")
    retargeted = False

    def retarget_source(**kwargs):
        nonlocal retargeted
        if not retargeted:
            claude_home.rename(moved_home)
            claude_home.symlink_to(foreign_home, target_is_directory=True)
            retargeted = True
        return original(**kwargs)

    monkeypatch.setattr(
        client_usage_module,
        "_discover_claude_code_usage_from_home",
        retarget_source,
    )

    result = discover_client_usage_with_diagnostics(
        client="claude-code",
        claude_home=claude_home,
        limit_sessions=10,
    )

    assert [event.client_session_id for event in result.events] == [
        "claude-session"
    ]
    assert result.events[0].source_namespace_fingerprint == expected_namespace


def test_discover_client_usage_with_diagnostics_surfaces_corrupt_codex_sqlite(tmp_path):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "state_5.sqlite").write_text("not a sqlite database", encoding="utf-8")

    result = discover_client_usage_with_diagnostics(client="codex", codex_home=codex_home)

    assert result.events == []
    diagnostic = result.diagnostics["codex"]
    assert diagnostic["error_count"] == 1
    assert diagnostic["error_codes"] == ["sqlite_thread_scan_failed"]
    assert str(tmp_path) not in json.dumps(diagnostic)


def test_discover_client_usage_with_diagnostics_surfaces_unparseable_codex_rollout(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    rollout_path = next((codex_home / "sessions").rglob("rollout-*.jsonl"))
    rollout_path.write_text("{not-json}\n", encoding="utf-8")

    result = discover_client_usage_with_diagnostics(client="codex", codex_home=codex_home)

    # SQLite usage remains a useful degraded fallback, but the product must
    # not present that scan as perfectly healthy.
    assert len(result.events) == 1
    diagnostic = result.diagnostics["codex"]
    assert diagnostic["parsed"] == 1
    assert diagnostic["error_count"] == 1
    assert diagnostic["error_codes"] == ["codex_rollout_unparseable"]


def test_discover_client_usage_with_diagnostics_counts_malformed_claude_transcript(tmp_path):
    claude_home = _make_claude_home(tmp_path)
    malformed = claude_home / "projects" / "-tmp-project" / "malformed.jsonl"
    malformed.write_text("{not-json}\n", encoding="utf-8")

    result = discover_client_usage_with_diagnostics(
        client="claude-code",
        claude_home=claude_home,
        limit_sessions=10,
    )

    assert len(result.events) == 1
    diagnostic = result.diagnostics["claude-code"]
    assert diagnostic["discovered"] == 2
    assert diagnostic["parsed"] == 1
    assert diagnostic["skipped"] == 1
    assert diagnostic["unresolved_identity_files"] == 0
    assert diagnostic["returned_rows"] == 1
    assert diagnostic["limit_unit"] == "root_groups"
    assert diagnostic["selected_root_groups"] == 2
    assert diagnostic["returned_root_groups"] == 1
    assert diagnostic["error_count"] == 1
    assert diagnostic["error_codes"] == ["claude_transcript_unparseable"]


def test_discover_client_usage_with_diagnostics_isolates_unreadable_claude_transcript(
    tmp_path,
    monkeypatch,
):
    claude_home = _make_claude_home(tmp_path)
    unreadable = claude_home / "projects" / "-tmp-project" / "unreadable.jsonl"
    unreadable.write_text("{}\n", encoding="utf-8")
    original_open = client_usage_module._open_claude_transcript_fd
    unreadable_open_count = 0

    def guarded_open(path: Path, **kwargs):
        nonlocal unreadable_open_count
        if path == unreadable:
            unreadable_open_count += 1
        if path == unreadable and unreadable_open_count >= 2:
            raise PermissionError("simulated unreadable transcript")
        return original_open(path, **kwargs)

    monkeypatch.setattr(
        client_usage_module,
        "_open_claude_transcript_fd",
        guarded_open,
    )

    result = discover_client_usage_with_diagnostics(
        client="claude-code",
        claude_home=claude_home,
        limit_sessions=10,
    )

    # A bad sibling must be visible in health diagnostics without discarding
    # usage from the readable transcript.
    assert len(result.events) == 1
    diagnostic = result.diagnostics["claude-code"]
    assert diagnostic["discovered"] == 2
    assert diagnostic["parsed"] == 1
    assert diagnostic["skipped"] == 0
    assert diagnostic["unresolved_identity_files"] == 1
    assert diagnostic["returned_rows"] == 1
    assert diagnostic["error_count"] == 1
    assert diagnostic["error_codes"] == ["claude_transcript_read_failed"]
    # issue #53: the readable transcript's usage is retained (see this test's
    # own contract above: a bad sibling is visible in diagnostics WITHOUT
    # discarding the readable transcript's usage).
    assert result.events[0].source_parse_complete is True
    assert str(tmp_path) not in json.dumps(diagnostic)


def test_discover_claude_code_usage_sums_assistant_usage_without_transcript(tmp_path):
    claude_home = _make_claude_home(tmp_path)

    events = discover_claude_code_usage(claude_home=claude_home, limit_sessions=10)

    assert len(events) == 1
    event = events[0]
    assert event.client == "claude-code"
    assert event.input_tokens == 30
    assert event.output_tokens == 12
    assert event.cached_input_tokens == 77
    assert event.cache_creation_input_tokens == 33
    assert event.cache_read_input_tokens == 44
    assert event.started_at == 1_781_949_600
    assert event.updated_at == 1_782_036_000
    assert event.model == "claude-opus-4-8"
    payload = event.to_sentinel_event()
    assert payload["provider"] == "claude-code"
    assert payload["metadata"]["client_session_id"] == "claude-session"
    assert payload["metadata"]["usage_update_semantics"] == "claude_assistant_message_usage_rows"
    assert payload["metadata"]["cache_creation_input_tokens"] == 33
    assert payload["metadata"]["cache_read_input_tokens"] == 44
    assert payload["metadata"]["client_session_kind"] == "root"
    assert payload["metadata"]["parent_client_session_id"] is None
    assert payload["metadata"]["client_transcript_id"] == "claude-session"
    assert payload["metadata"]["title_redacted"] is False
    assert payload["metadata"]["client_session_title_source"] == "explicit_client_title_field"
    assert payload["metadata"]["client_session_title_sanitized"] is True
    assert payload["metadata"]["client_session_title"] == "A useful Claude session"
    assert payload["metadata"]["cache_creation_tokens_reported"] is True
    assert payload["metadata"]["cache_read_tokens_reported"] is True
    assert "content" not in json.dumps(payload).lower()


def test_claude_usage_event_emits_transcript_mtime_us_revision_watermark(tmp_path):
    claude_home = _make_claude_home(tmp_path)
    transcript = claude_home / "projects" / "-tmp-project" / "claude-session.jsonl"

    events = discover_claude_code_usage(claude_home=claude_home, limit_sessions=10)

    assert len(events) == 1
    event = events[0]
    expected_revision_us = transcript.stat().st_mtime_ns // 1000
    assert event.source_revision_at == expected_revision_us
    assert event.source_revision_basis == "transcript_file_mtime_us"
    payload = event.to_sentinel_event()
    assert payload["metadata"]["source_revision_at"] == expected_revision_us
    assert payload["metadata"]["source_revision_basis"] == "transcript_file_mtime_us"
    stored = mark_trusted_local_usage_import_event(payload)
    # The refreshable lane must order by the exact microsecond watermark,
    # never falling back to the whole-second updated_at clock.
    assert refreshable_usage_source_order(stored) == expected_revision_us


def test_claude_code_title_prefers_custom_title_without_reading_user_prompt(tmp_path):
    claude_home = _make_claude_home(tmp_path)
    session = claude_home / "projects" / "-tmp-project" / "claude-session.jsonl"
    rows = [
        {
            "type": "user",
            "sessionId": "claude-session",
            "message": {"role": "user", "content": "PRIVATE raw prompt must never become a title"},
        },
        {"type": "ai-title", "sessionId": "claude-session", "aiTitle": "Generated session title"},
        {
            "type": "assistant",
            "sessionId": "claude-session",
            "cwd": "/work/project",
            "message": {
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
        {"type": "custom-title", "sessionId": "claude-session", "customTitle": "Chosen session title"},
    ]
    session.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    events = discover_claude_code_usage(claude_home=claude_home, limit_sessions=10)

    assert len(events) == 1
    payload = events[0].to_sentinel_event()
    assert payload["metadata"]["client_session_title"] == "Chosen session title"
    assert payload["metadata"]["title_redacted"] is False
    assert "Generated session title" not in json.dumps(payload)
    assert "PRIVATE raw prompt" not in json.dumps(payload)


def test_claude_code_title_removes_controls_and_enforces_length_bound(tmp_path):
    claude_home = _make_claude_home(tmp_path)
    session = claude_home / "projects" / "-tmp-project" / "claude-session.jsonl"
    unsafe_title = "\x00  Safe\tTitle\n" + ("x" * 300) + "\u202e"
    with session.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"type": "custom-title", "sessionId": "claude-session", "customTitle": unsafe_title}
            )
            + "\n"
        )

    events = discover_claude_code_usage(claude_home=claude_home, limit_sessions=10)

    assert len(events) == 1
    payload = events[0].to_sentinel_event()
    title = payload["metadata"]["client_session_title"]
    assert title.startswith("Safe Title ")
    assert len(title) == 240
    assert title.endswith("…")
    assert not any(control in title for control in ("\x00", "\t", "\n", "\u202e"))


def test_discover_claude_code_usage_splits_mixed_model_transcript(tmp_path):
    claude_home = _make_claude_home(tmp_path)
    session = claude_home / "projects" / "-tmp-project" / "claude-session.jsonl"
    with session.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "assistant",
                    "sessionId": "claude-session",
                    "cwd": "/work/project",
                    "message": {
                        "model": "claude-haiku-4-5-20251001",
                        "usage": {
                            "input_tokens": 4,
                            "cache_creation_input_tokens": 1,
                            "cache_read_input_tokens": 2,
                            "output_tokens": 3,
                        },
                    },
                }
            )
            + "\n"
        )

    events = discover_claude_code_usage(claude_home=claude_home, limit_sessions=10)

    by_model = {event.model: event for event in events}
    assert set(by_model) == {"claude-opus-4-8", "claude-haiku-4-5-20251001"}
    assert by_model["claude-opus-4-8"].input_tokens == 30
    assert by_model["claude-haiku-4-5-20251001"].input_tokens == 4
    assert by_model["claude-haiku-4-5-20251001"].cached_input_tokens == 3
    assert by_model["claude-haiku-4-5-20251001"].cache_creation_input_tokens == 1
    assert by_model["claude-haiku-4-5-20251001"].cache_read_input_tokens == 2
    # One client session keeps ONE stable session key across a model switch;
    # the per-model breakdown lives in usage_row_lane, never in the key.
    assert by_model["claude-opus-4-8"].client_session_id == "claude-session"
    assert by_model["claude-haiku-4-5-20251001"].client_session_id == "claude-session"
    assert by_model["claude-opus-4-8"].client_transcript_id == "claude-session"
    assert by_model["claude-haiku-4-5-20251001"].client_transcript_id == "claude-session"
    assert by_model["claude-opus-4-8"].usage_row_lane == "model:claude-opus-4-8"
    assert by_model["claude-haiku-4-5-20251001"].usage_row_lane == "model:claude-haiku-4-5-20251001"
    assert all(":model:" not in event.client_session_id for event in events)
    haiku_payload = by_model["claude-haiku-4-5-20251001"].to_sentinel_event()
    assert haiku_payload["metadata"]["usage_row_lane"] == "model:claude-haiku-4-5-20251001"


def test_discover_claude_code_usage_deduplicates_sidechain_replayed_messages(tmp_path):
    claude_home = _make_claude_home(tmp_path)
    project = claude_home / "projects" / "-tmp-project"
    parent = project / "claude-session.jsonl"
    rows = [json.loads(line) for line in parent.read_text(encoding="utf-8").splitlines()]
    rows[1]["message"]["id"] = "msg_shared"
    rows[1]["requestId"] = "req_shared"
    rows[1]["message"]["usage"]["cache_creation"] = {
        "ephemeral_5m_input_tokens": 10,
        "ephemeral_1h_input_tokens": 20,
    }
    parent.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    sidechain = project / "agent-replay.jsonl"
    replayed = dict(rows[1])
    replayed["isSidechain"] = True
    replayed["uuid"] = "sidechain-row"
    sidechain.write_text(json.dumps(replayed) + "\n", encoding="utf-8")
    parent.touch()

    events = discover_claude_code_usage(claude_home=claude_home, limit_sessions=10)

    assert len(events) == 1
    event = events[0]
    assert event.input_tokens == 30
    assert event.output_tokens == 12
    assert event.cache_creation_input_tokens == 33
    assert event.cache_creation_5m_input_tokens == 10
    assert event.cache_creation_1h_input_tokens == 20
    assert event.cache_read_input_tokens == 44
    assert event.raw_usage_rows == 2
    assert event.deduplicated_usage_rows == 0


def test_discover_claude_code_usage_keeps_sidechain_output_delta(tmp_path):
    claude_home = _make_claude_home(tmp_path)
    project = claude_home / "projects" / "-tmp-project"
    parent = project / "claude-session.jsonl"
    rows = [json.loads(line) for line in parent.read_text(encoding="utf-8").splitlines()]
    rows[1]["message"]["id"] = "msg_shared"
    rows[1]["requestId"] = "req_shared"
    parent.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    sidechain = project / "agent-output-delta.jsonl"
    replayed = dict(rows[1])
    replayed["isSidechain"] = True
    replayed["uuid"] = "sidechain-row"
    replayed["message"] = dict(rows[1]["message"])
    replayed["message"]["usage"] = dict(rows[1]["message"]["usage"])
    replayed["message"]["usage"]["output_tokens"] = 9
    sidechain.write_text(json.dumps(replayed) + "\n", encoding="utf-8")
    parent.touch()

    events = discover_claude_code_usage(claude_home=claude_home, limit_sessions=10)

    by_session = {event.client_session_id: event for event in events}
    assert by_session["claude-session"].output_tokens == 12
    child = by_session["claude-session:agent-output-delta"]
    assert child.input_tokens == 0
    assert child.cache_creation_input_tokens == 0
    assert child.cache_read_input_tokens == 0
    assert child.output_tokens == 4
    assert child.deduplicated_usage_rows == 1


def test_discover_claude_code_usage_distinguishes_agent_transcript_files(tmp_path):
    claude_home = _make_claude_home(tmp_path)
    project = claude_home / "projects" / "-tmp-project"
    agent_file = project / "agent-a123.jsonl"
    rows = [
        {
            "type": "assistant",
            "sessionId": "claude-session",
            "cwd": "/work/project",
            "message": {
                "model": "claude-opus-4-8",
                "usage": {
                    "input_tokens": 3,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 4,
                    "output_tokens": 2,
                },
            },
        }
    ]
    agent_file.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    events = discover_claude_code_usage(claude_home=claude_home, limit_sessions=10)

    session_ids = {event.client_session_id for event in events}
    assert "claude-session" in session_ids
    assert "claude-session:agent-a123" in session_ids
    assert len(session_ids) == 2
    child_event = next(event for event in events if event.client_session_id == "claude-session:agent-a123")
    child_payload = child_event.to_sentinel_event()
    assert child_payload["metadata"]["client_session_kind"] == "child"
    assert child_payload["metadata"]["parent_client_session_id"] == "claude-session"
    assert child_payload["metadata"]["client_transcript_id"] == "agent-a123"


def test_discover_opencode_usage_reads_json_event_stream_tokens_and_cost(tmp_path):
    opencode_home = _make_opencode_home(tmp_path)

    events = discover_opencode_usage(opencode_home=opencode_home, limit_sessions=10)

    assert len(events) == 1
    event = events[0]
    assert event.client == "opencode"
    assert event.client_session_id == "ses_example"
    assert event.input_tokens == 8665
    assert event.output_tokens == 240
    assert event.cached_input_tokens == 14720
    assert event.cache_creation_input_tokens == 0
    assert event.cache_read_input_tokens == 14720
    assert event.cache_creation_tokens_reported is True
    assert event.cache_read_tokens_reported is True
    assert event.reasoning_output_tokens == 0
    assert event.turn_count == 2
    payload = event.to_sentinel_event()
    assert payload["provider"] == "opencode"
    assert payload["cost_confidence"] == "client_reported"
    assert payload["estimated_cost_usd"] == 0.001321516
    assert payload["metadata"]["client_reported_cost_usd"] == 0.001321516
    assert payload["metadata"]["usage_update_semantics"] == "opencode_step_finish_events"
    assert "OPENCODE_DEEPSEEK_REAL_TASK_OK" not in json.dumps(payload)


def test_discover_opencode_usage_skips_json_arrays(tmp_path):
    opencode_home = _make_opencode_home(tmp_path)
    stream = opencode_home / "sessions" / "non-event.json"
    stream.write_text(json.dumps([{"sessionID": "not-an-event"}]), encoding="utf-8")

    events = discover_opencode_usage(opencode_home=opencode_home, limit_sessions=10)

    assert len(events) == 1
    assert events[0].client_session_id == "ses_example"


def test_discover_openclaw_usage_reads_jsonl_tokens_and_cost(tmp_path):
    openclaw_home = _make_openclaw_home(tmp_path)

    events = discover_openclaw_usage(openclaw_home=openclaw_home, limit_sessions=10)

    assert len(events) == 1
    event = events[0]
    assert event.client == "openclaw"
    assert event.client_session_id == "openclaw-session"
    assert event.provider == "openai"
    assert event.model == "gpt-5.2"
    assert event.input_tokens == 1660
    assert event.output_tokens == 55
    assert event.cached_input_tokens == 108938
    assert event.cache_creation_input_tokens == 10
    assert event.cache_read_input_tokens == 108928
    assert event.cache_creation_tokens_reported is True
    assert event.cache_read_tokens_reported is True
    assert event.turn_count == 1
    payload = event.to_sentinel_event()
    assert payload["provider"] == "openai"
    assert payload["cost_confidence"] == "client_reported"
    assert payload["estimated_cost_usd"] == 0.02
    assert payload["metadata"]["usage_update_semantics"] == "openclaw_assistant_usage_rows"
    assert "content" not in json.dumps(payload).lower()


def test_discover_hermes_usage_reads_state_db_sessions_and_client_cost(tmp_path):
    hermes_home = _make_hermes_home(tmp_path)

    events = discover_hermes_usage(hermes_home=hermes_home, limit_sessions=10)

    assert len(events) == 1
    event = events[0]
    assert event.client == "hermes"
    assert event.client_session_id == "hermes-session-1"
    assert event.provider == "anthropic"
    assert event.model == "claude-sonnet-4-20250514"
    assert event.input_tokens == 1200
    assert event.output_tokens == 300
    assert event.cached_input_tokens == 70
    assert event.cache_creation_input_tokens == 20
    assert event.cache_read_input_tokens == 50
    assert event.cache_creation_tokens_reported is True
    assert event.cache_read_tokens_reported is True
    assert event.reasoning_output_tokens == 10
    assert event.turn_count == 4
    assert event.source_namespace_fingerprint
    payload = event.to_sentinel_event()
    assert payload["provider"] == "anthropic"
    assert payload["cost_confidence"] == "client_reported"
    assert payload["estimated_cost_usd"] == 0.34
    assert payload["metadata"]["client"] == "hermes"
    assert payload["metadata"]["usage_update_semantics"] == "hermes_state_db_session_rows"
    assert payload["metadata"]["client_cost_source"] == "hermes_actual_cost_usd"
    assert payload["metadata"]["title_redacted"] is False
    assert "client_session_title" not in payload["metadata"]
    assert "client_session_title_source" not in payload["metadata"]
    assert "client_session_title_sanitized" not in payload["metadata"]
    assert "prompt" not in json.dumps(payload).lower()


def test_hermes_usage_event_updated_at_uses_row_started_at_not_db_mtime(tmp_path):
    hermes_home = _make_hermes_home(tmp_path)
    db_path = hermes_home / "state.db"
    # The shared state.db mtime advances whenever ANY hermes session writes;
    # it must never become this row's activity clock.
    os.utime(db_path, (1_800_000_000, 1_800_000_000))

    events = discover_hermes_usage(hermes_home=hermes_home, limit_sessions=10)

    assert len(events) == 1
    event = events[0]
    assert event.started_at == 1_750_000_000
    assert event.updated_at == 1_750_000_000
    assert event.updated_at != int(db_path.stat().st_mtime)
    payload = event.to_sentinel_event()
    assert payload["metadata"]["updated_at"] == 1_750_000_000
    # The shared container clock stays out of the per-row usage watermark.
    assert "source_revision_at" not in payload["metadata"]


def test_hermes_diagnostics_count_prelimit_rows_and_observe_zero_usage(
    tmp_path,
):
    hermes_home = _make_hermes_home(tmp_path)
    con = sqlite3.connect(hermes_home / "state.db")
    try:
        con.execute("alter table sessions add column cwd text")
        con.execute("alter table sessions add column title text")
        con.execute(
            """
            insert into sessions (
                id, source, model, started_at, message_count,
                input_tokens, output_tokens, cache_read_tokens,
                cache_write_tokens, reasoning_tokens, billing_provider,
                estimated_cost_usd, actual_cost_usd, cwd, title
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "hermes-zero-session",
                "cli",
                "gpt-5.5",
                1_760_000_000.0,
                1,
                0,
                0,
                0,
                0,
                0,
                "openai",
                0,
                0,
                "/work/private-project",
                "secret transcript-derived title",
            ),
        )
        con.commit()
    finally:
        con.close()

    result = discover_client_usage_with_diagnostics(
        client="hermes",
        hermes_home=hermes_home,
        limit_sessions=2,
    )

    assert [event.client_session_id for event in result.events] == [
        "hermes-session-1"
    ]
    assert {
        observation.client_session_id
        for observation in result.session_observations
    } == {"hermes-session-1", "hermes-zero-session"}
    diagnostic = result.diagnostics["hermes"]
    assert diagnostic["discovered"] == 2
    assert diagnostic["parsed"] == 2
    assert diagnostic["returned_rows"] == 1
    assert diagnostic["excluded_by_limit"] == 0
    assert diagnostic["observed_sessions"] == 2
    assert diagnostic["usage_sessions"] == 1
    assert diagnostic["sessions_without_usage"] == 1
    zero_observation = next(
        observation
        for observation in result.session_observations
        if observation.client_session_id == "hermes-zero-session"
    )
    assert zero_observation.title is None
    assert zero_observation.cwd == "/work/private-project"
    assert zero_observation.updated_at == 1_760_000_000
    assert zero_observation.activity_time_basis == "client_started_at"
    assert zero_observation.source_revision_at == (
        hermes_home / "state.db"
    ).stat().st_mtime_ns
    assert zero_observation.source_namespace_fingerprint
    assert zero_observation.source_session_identity != (
        "",
        "hermes",
        "hermes-zero-session",
    )
    observation_event = usage_less_session_observations(result)[0].to_sentinel_event()
    assert observation_event["event_type"] == "session_observed"
    assert "secret transcript-derived title" not in json.dumps(observation_event)
    assert not {
        "provider",
        "model",
        "estimated_input_tokens",
        "estimated_output_tokens",
        "estimated_cost_usd",
    }.intersection(observation_event)


def test_hermes_diagnostics_report_rows_excluded_before_usage_filter(tmp_path):
    hermes_home = _make_hermes_home(tmp_path)
    con = sqlite3.connect(hermes_home / "state.db")
    try:
        con.execute(
            """
            insert into sessions (
                id, source, model, started_at, message_count,
                input_tokens, output_tokens, cache_read_tokens,
                cache_write_tokens, reasoning_tokens, billing_provider,
                estimated_cost_usd, actual_cost_usd
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "newest-zero-session",
                "cli",
                "gpt-5.5",
                1_760_000_000.0,
                1,
                0,
                0,
                0,
                0,
                0,
                "openai",
                0,
                0,
            ),
        )
        con.commit()
    finally:
        con.close()

    result = discover_client_usage_with_diagnostics(
        client="hermes",
        hermes_home=hermes_home,
        limit_sessions=1,
    )

    assert result.events == []
    assert [
        observation.client_session_id
        for observation in result.session_observations
    ] == ["newest-zero-session"]
    assert result.diagnostics["hermes"] == {
        "client": "hermes",
        "discovered": 2,
        "parsed": 1,
        "skipped": 0,
        "error_count": 0,
        "error_codes": [],
        "watermark": 1_760_000_000,
        "limit_unit": "rows",
        "selected_root_groups": None,
        "returned_root_groups": None,
        "returned_rows": 0,
        "excluded_by_limit": 1,
        "ignored_non_transcript_files": 0,
        "unresolved_identity_files": 0,
        "excluded_by_source_namespace": 0,
        "unparsed_selected_rows": 0,
        "observed_sessions": 1,
        "usage_sessions": 0,
        "sessions_without_usage": 1,
        "source_present": True,
    }


def test_hermes_multiple_env_homes_fail_closed_until_explicit_selection(
    tmp_path,
    monkeypatch,
):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    home_a = _make_hermes_home(root_a)
    home_b = _make_hermes_home(root_b)
    monkeypatch.setenv("HERMES_HOME", f"{home_a},{home_b}")

    ambiguous = discover_client_usage_with_diagnostics(
        client="hermes",
        limit_sessions=10,
    )
    source = next(
        row
        for row in discover_usage_sources(
            codex_home=tmp_path / "missing-codex",
            claude_home=tmp_path / "missing-claude",
            opencode_home=tmp_path / "missing-opencode",
            openclaw_home=tmp_path / "missing-openclaw",
        )
        if row.client == "hermes"
    )

    assert ambiguous.events == []
    assert ambiguous.session_observations == []
    assert ambiguous.diagnostics["hermes"]["error_count"] == 1
    assert ambiguous.diagnostics["hermes"]["error_codes"] == [
        "hermes_multiple_source_homes_require_explicit_selection"
    ]
    assert source.status == "found"
    assert source.importer is None
    assert "select one explicitly" in " ".join(source.notes)
    assert describe_scanned_client_homes(client="hermes") == [
        f"hermes: {home_a}",
        f"hermes: {home_b}",
    ]
    assert discover_hermes_usage(limit_sessions=10) == []

    selected_a = discover_client_usage_with_diagnostics(
        client="hermes",
        hermes_home=home_a,
        limit_sessions=10,
    )
    selected_b = discover_client_usage_with_diagnostics(
        client="hermes",
        hermes_home=home_b,
        limit_sessions=10,
    )

    assert [event.client_session_id for event in selected_a.events] == [
        "hermes-session-1"
    ]
    assert [event.client_session_id for event in selected_b.events] == [
        "hermes-session-1"
    ]
    assert selected_a.events[0].source_session_identity != (
        selected_b.events[0].source_session_identity
    )
    assert selected_a.session_observations[0].source_session_identity != (
        selected_b.session_observations[0].source_session_identity
    )
    stored_a = selected_a.events[0].to_sentinel_event()
    stored_a["event_id"] = "stored-home-a"
    cross_home_plan = plan_local_usage_import(selected_b.events, [stored_a])
    assert cross_home_plan.new_candidates == []
    assert cross_home_plan.refresh_candidates == []
    assert cross_home_plan.namespace_conflict_candidates == selected_b.events


def test_describe_scanned_hermes_home_handles_unset_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HERMES_HOME", raising=False)

    assert describe_scanned_client_homes(client="hermes") == [
        "hermes: ~/.hermes"
    ]


def test_hermes_multiple_env_aliases_to_one_inode_are_deduplicated(
    tmp_path,
    monkeypatch,
):
    canonical_root = tmp_path / "canonical"
    canonical_root.mkdir()
    canonical_home = _make_hermes_home(canonical_root)
    alias_home = tmp_path / "alias" / "hermes-home"
    alias_home.mkdir(parents=True)
    os.link(canonical_home / "state.db", alias_home / "state.db")
    monkeypatch.setenv(
        "HERMES_HOME",
        f"{canonical_home}, {alias_home}",
    )

    result = discover_client_usage_with_diagnostics(
        client="hermes",
        limit_sessions=10,
    )
    source = next(
        row
        for row in discover_usage_sources(
            codex_home=tmp_path / "missing-codex",
            claude_home=tmp_path / "missing-claude",
            opencode_home=tmp_path / "missing-opencode",
            openclaw_home=tmp_path / "missing-openclaw",
        )
        if row.client == "hermes"
    )

    assert [event.client_session_id for event in result.events] == [
        "hermes-session-1"
    ]
    assert [
        observation.client_session_id
        for observation in result.session_observations
    ] == ["hermes-session-1"]
    assert result.diagnostics["hermes"]["error_count"] == 0
    assert result.diagnostics["hermes"]["discovered"] == 1
    assert result.diagnostics["hermes"]["returned_rows"] == 1
    assert source.importer == "agentacct usage import-local --client hermes"

    monkeypatch.setenv(
        "HERMES_HOME",
        f"{alias_home},{canonical_home}",
    )
    reversed_result = discover_client_usage_with_diagnostics(
        client="hermes",
        limit_sessions=10,
    )
    reversed_source = next(
        row
        for row in discover_usage_sources(
            codex_home=tmp_path / "missing-codex",
            claude_home=tmp_path / "missing-claude",
            opencode_home=tmp_path / "missing-opencode",
            openclaw_home=tmp_path / "missing-openclaw",
        )
        if row.client == "hermes"
    )

    assert reversed_result.events[0].source_session_identity == (
        result.events[0].source_session_identity
    )
    assert reversed_result.session_observations[0].source_session_identity == (
        result.session_observations[0].source_session_identity
    )
    assert reversed_source.importer == source.importer


def test_hermes_root_symlink_is_rejected_by_importer_and_source_discovery(
    tmp_path,
):
    foreign_root = tmp_path / "foreign-root"
    foreign_root.mkdir()
    foreign_home = _make_hermes_home(foreign_root)
    linked_home = tmp_path / "linked-hermes"
    linked_home.symlink_to(foreign_home, target_is_directory=True)

    result = discover_client_usage_with_diagnostics(
        client="hermes",
        hermes_home=linked_home,
        limit_sessions=10,
    )
    source = next(
        row
        for row in discover_usage_sources(
            codex_home=tmp_path / "missing-codex",
            claude_home=tmp_path / "missing-claude",
            opencode_home=tmp_path / "missing-opencode",
            hermes_home=linked_home,
            openclaw_home=tmp_path / "missing-openclaw",
        )
        if row.client == "hermes"
    )

    assert result.events == []
    assert result.session_observations == []
    assert source.status == "missing"
    assert source.file_count == 0
    assert source.session_count is None
    assert source.importer is None


def test_codex_state_db_symlink_does_not_import_foreign_session(tmp_path):
    local_home = _make_codex_home(tmp_path / "local")
    foreign_home = _make_codex_home(tmp_path / "foreign")
    _add_codex_thread(
        foreign_home,
        session_id="foreign-db-session",
        updated_at=999,
        model="gpt-foreign",
    )
    (local_home / "state_5.sqlite").unlink()
    (local_home / "state_5.sqlite").symlink_to(
        foreign_home / "state_5.sqlite"
    )

    events = discover_codex_usage(codex_home=local_home, limit_sessions=10)

    assert {event.client_session_id for event in events} == {"session-abc"}
    assert all("foreign" not in str(event.source_path) for event in events)


def test_existing_unsafe_codex_state_db_carrier_is_fail_visible(tmp_path):
    local_home = _make_codex_home(tmp_path / "local")
    foreign_home = _make_codex_home(tmp_path / "foreign")
    _add_codex_thread(
        foreign_home,
        session_id="foreign-db-session",
        updated_at=999,
        model="gpt-foreign",
    )
    (local_home / "state_5.sqlite").unlink()
    (local_home / "state_5.sqlite").symlink_to(
        foreign_home / "state_5.sqlite"
    )

    result = discover_client_usage_with_diagnostics(
        client="codex",
        codex_home=local_home,
        limit_sessions=10,
    )

    diagnostic = result.diagnostics["codex"]
    assert diagnostic["error_count"] == 1
    assert "codex_state_db_carrier_unreadable" in diagnostic["error_codes"]
    # The unsafe sqlite carrier is skipped fail-visibly, but the local
    # rollout files remain an authoritative usage source of their own.
    assert {event.client_session_id for event in result.events} == {
        "session-abc"
    }
    assert all(
        "foreign" not in str(event.source_path) for event in result.events
    )


def test_codex_rollout_only_identity_replacement_fails_closed(
    tmp_path,
    monkeypatch,
):
    codex_home = tmp_path / "codex-home"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    rollout = sessions / "rollout-local.jsonl"
    rollout.write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"id": "local-session"}}
        )
        + "\n",
        encoding="utf-8",
    )
    foreign = tmp_path / "foreign.jsonl"
    foreign.write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"id": "foreign-session"}}
        )
        + "\n",
        encoding="utf-8",
    )
    original = client_usage_module._read_codex_rollout_identity
    replaced = False

    def replace_before_read(source):
        nonlocal replaced
        if not replaced:
            replaced = True
            source.path.unlink()
            source.path.symlink_to(foreign)
        return original(source)

    monkeypatch.setattr(
        client_usage_module,
        "_read_codex_rollout_identity",
        replace_before_read,
    )
    observations: list[ClientSessionObservation] = []

    events = discover_codex_usage(
        codex_home=codex_home,
        limit_sessions=10,
        _session_observations=observations,
    )

    assert events == []
    assert observations == []


def test_codex_db_rollout_path_outside_home_uses_db_only(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    foreign_rollout = tmp_path / "foreign-rollout.jsonl"
    foreign_rollout.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": "foreign-rollout-session"},
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 999_999,
                                    "cached_input_tokens": 888_888,
                                    "output_tokens": 777_777,
                                }
                            },
                            "model": "gpt-foreign",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    con = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        con.execute(
            "update threads set rollout_path = ? where id = ?",
            (str(foreign_rollout), "session-abc"),
        )
        con.commit()
    finally:
        con.close()

    event = next(
        event
        for event in discover_codex_usage(
            codex_home=codex_home,
            limit_sessions=10,
        )
        if event.client_session_id == "session-abc"
    )

    assert event.input_tokens == 2625
    assert event.output_tokens == 0
    assert event.model != "gpt-foreign"
    assert event.source_path == codex_home / "state_5.sqlite"


def test_codex_db_rollout_symlink_uses_db_only(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    local_rollout = next((codex_home / "sessions").rglob("rollout-*.jsonl"))
    foreign_rollout = tmp_path / "foreign-rollout.jsonl"
    foreign_rollout.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 999_999,
                            "output_tokens": 777_777,
                        }
                    },
                    "model": "gpt-foreign",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    local_rollout.unlink()
    local_rollout.symlink_to(foreign_rollout)

    event = discover_codex_usage(codex_home=codex_home, limit_sessions=10)[0]

    assert event.input_tokens == 2625
    assert event.output_tokens == 0
    assert event.model != "gpt-foreign"
    assert event.source_path == codex_home / "state_5.sqlite"


def test_hermes_state_db_symlink_is_ignored(tmp_path):
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    foreign_home = _make_hermes_home(foreign_root)
    local_home = tmp_path / "local-hermes"
    local_home.mkdir()
    (local_home / "state.db").symlink_to(foreign_home / "state.db")

    assert discover_hermes_usage(hermes_home=local_home, limit_sessions=10) == []


def test_opencode_event_symlink_is_ignored(tmp_path):
    foreign_home = _make_opencode_home(tmp_path / "foreign")
    foreign_stream = next(foreign_home.rglob("*.jsonl"))
    local_home = tmp_path / "local-opencode"
    (local_home / "sessions").mkdir(parents=True)
    (local_home / "sessions" / "linked.jsonl").symlink_to(foreign_stream)

    assert discover_opencode_usage(opencode_home=local_home, limit_sessions=10) == []


def test_opencode_root_symlink_is_ignored(tmp_path):
    foreign_home = _make_opencode_home(tmp_path / "foreign")
    linked_home = tmp_path / "linked-opencode"
    linked_home.symlink_to(foreign_home, target_is_directory=True)

    assert discover_opencode_usage(opencode_home=linked_home, limit_sessions=10) == []


def test_opencode_env_home_matches_source_discovery_and_importer(
    tmp_path,
    monkeypatch,
):
    opencode_home = _make_opencode_home(tmp_path)
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(opencode_home))

    source = next(
        row
        for row in discover_usage_sources(
            codex_home=tmp_path / "missing-codex",
            claude_home=tmp_path / "missing-claude",
            hermes_home=tmp_path / "missing-hermes",
            openclaw_home=tmp_path / "missing-openclaw",
        )
        if row.client == "opencode"
    )
    events = discover_opencode_usage(limit_sessions=10)

    assert source.status == "found"
    assert source.importer == "agentacct usage import-local --client opencode"
    assert [event.client_session_id for event in events] == ["ses_example"]
    assert events[0].source_namespace_fingerprint is not None


def test_opencode_multi_home_same_session_id_fails_closed(
    tmp_path,
    monkeypatch,
):
    first_home = _make_opencode_home(tmp_path / "first")
    second_home = _make_opencode_home(tmp_path / "second")
    monkeypatch.setenv(
        "OPENCODE_DATA_DIR",
        f"{first_home},{second_home}",
    )

    source = next(
        row
        for row in discover_usage_sources(
            codex_home=tmp_path / "missing-codex",
            claude_home=tmp_path / "missing-claude",
            hermes_home=tmp_path / "missing-hermes",
            openclaw_home=tmp_path / "missing-openclaw",
        )
        if row.client == "opencode"
    )
    events = discover_opencode_usage(limit_sessions=1)

    assert source.status == "found"
    assert source.importer is None
    assert "select one explicitly" in " ".join(source.notes)
    assert events == []


def test_openclaw_session_symlink_is_ignored(tmp_path):
    foreign_home = _make_openclaw_home(tmp_path / "foreign")
    foreign_stream = next(foreign_home.rglob("*.jsonl"))
    local_home = tmp_path / "local-openclaw"
    local_home.mkdir()
    (local_home / "linked.jsonl").symlink_to(foreign_stream)

    assert discover_openclaw_usage(openclaw_home=local_home, limit_sessions=10) == []


def test_openclaw_root_symlink_is_ignored(tmp_path):
    foreign_home = _make_openclaw_home(tmp_path / "foreign")
    linked_home = tmp_path / "linked-openclaw"
    linked_home.symlink_to(foreign_home, target_is_directory=True)

    assert discover_openclaw_usage(openclaw_home=linked_home, limit_sessions=10) == []


def test_title_sanitized_to_empty_is_marked_redacted_and_never_persisted(tmp_path):
    payload = ClientUsageEvent(
        client="claude-code",
        client_session_id="blank-title",
        source_path=tmp_path / "blank.jsonl",
        title="\x00\u202e",
        cwd=None,
        model="claude-opus",
        input_tokens=1,
        output_tokens=1,
    ).to_sentinel_event()

    assert payload["metadata"]["title_redacted"] is True
    assert "client_session_title" not in payload["metadata"]
    assert "client_session_title_source" not in payload["metadata"]
    assert "client_session_title_sanitized" not in payload["metadata"]


def test_pricing_helper_never_prices_held_codex_descendant_usage(tmp_path):
    event = ClientUsageEvent(
        client="codex",
        client_session_id="child-with-replayed-parent-history",
        source_path=tmp_path / "child.jsonl",
        title=None,
        cwd="/work/project",
        model="gpt-5.5",
        input_tokens=9_000_000_000,
        output_tokens=2_000_000_000,
        cache_read_input_tokens=70_000_000_000,
        cached_input_tokens=70_000_000_000,
        client_session_kind="child",
        parent_client_session_id="root-session",
    ).to_sentinel_event()

    assert event["metadata"]["usage_additive"] is False
    assert apply_pricing_estimate_to_event(event) is False
    assert event["estimated_cost_usd"] is None
    assert event["cost_confidence"] == "unknown"
    assert "pricing_source" not in event["metadata"]


def test_discover_hermes_usage_treats_zero_cost_as_unknown(tmp_path):
    hermes_home = _make_hermes_home(tmp_path)
    con = sqlite3.connect(hermes_home / "state.db")
    try:
        con.execute("update sessions set estimated_cost_usd = 0, actual_cost_usd = 0")
        con.commit()
    finally:
        con.close()

    events = discover_hermes_usage(hermes_home=hermes_home, limit_sessions=10)

    assert len(events) == 1
    payload = events[0].to_sentinel_event()
    assert payload["estimated_cost_usd"] is None
    assert payload["cost_confidence"] == "unknown"
    assert payload["metadata"]["client_cost_source"] is None


def test_hermes_zero_usage_session_is_detected_without_claiming_usage(tmp_path):
    hermes_home = _make_hermes_home(tmp_path)
    con = sqlite3.connect(hermes_home / "state.db")
    try:
        con.execute(
            """
            update sessions
            set input_tokens = 0,
                output_tokens = 0,
                cache_read_tokens = 0,
                cache_write_tokens = 0,
                reasoning_tokens = 0,
                estimated_cost_usd = 0,
                actual_cost_usd = 0
            """
        )
        con.commit()
    finally:
        con.close()

    source = next(
        row
        for row in discover_usage_sources(
            codex_home=tmp_path / "missing-codex",
            claude_home=tmp_path / "missing-claude",
            opencode_home=tmp_path / "missing-opencode",
            hermes_home=hermes_home,
            openclaw_home=tmp_path / "missing-openclaw",
        )
        if row.client == "hermes"
    )

    assert source.status == "found"
    assert source.usage_confidence == "unknown"
    assert source.importer == "agentacct usage import-local --client hermes"
    assert "no positive Hermes token fields detected" in source.notes
    assert discover_hermes_usage(hermes_home=hermes_home, limit_sessions=10) == []


def test_usage_import_local_estimates_hermes_openai_gpt55_when_cost_missing(tmp_path):
    hermes_home = _make_hermes_home(tmp_path)
    con = sqlite3.connect(hermes_home / "state.db")
    try:
        con.execute(
            "update sessions set model = ?, billing_provider = ?, estimated_cost_usd = 0, actual_cost_usd = 0",
            ("gpt-5.5", "openai"),
        )
        con.commit()
    finally:
        con.close()
    store_dir = tmp_path / "sentinel-state"

    result = CliRunner().invoke(
        app,
        [
            "usage",
            "import-local",
            "--client",
            "hermes",
            "--store-dir",
            str(store_dir),
            "--hermes-home",
            str(hermes_home),
            "--estimate-costs",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["priced_events"] == 1
    assert payload["cost_confidence"] == "estimated_from_tokens"
    assert payload["events"][0]["provider"] == "openai"
    assert payload["events"][0]["model"] == "gpt-5.5"
    assert payload["events"][0]["estimated_cost_usd"] == 0.015125


def test_usage_import_local_estimates_from_store_pricing_snapshot(tmp_path):
    hermes_home = _make_hermes_home(tmp_path)
    con = sqlite3.connect(hermes_home / "state.db")
    try:
        con.execute(
            "update sessions set model = ?, billing_provider = ?, estimated_cost_usd = 0, actual_cost_usd = 0",
            ("gpt-auto-price", "openai"),
        )
        con.commit()
    finally:
        con.close()
    store_dir = tmp_path / "sentinel-state"
    catalog_path = default_pricing_catalog_snapshot_path(store_dir)
    assert catalog_path is not None
    catalog_path.parent.mkdir(parents=True)
    catalog_path.write_text(
        json.dumps(
            {
                "gpt-auto-price": {
                    "litellm_provider": "openai",
                    "input_cost_per_token": 0.000001,
                    "output_cost_per_token": 0.000002,
                    "cache_read_input_token_cost": 0.0000001,
                }
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "usage",
            "import-local",
            "--client",
            "hermes",
            "--store-dir",
            str(store_dir),
            "--hermes-home",
            str(hermes_home),
            "--estimate-costs",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["pricing_catalog_path"] == str(catalog_path)
    assert payload["priced_events"] == 1
    assert payload["events"][0]["metadata"]["pricing_source"] == "litellm_model_cost_map"
    assert payload["events"][0]["estimated_cost_usd"] == 0.001825


def test_usage_import_local_imports_opencode_usage_with_client_reported_cost(tmp_path):
    opencode_home = _make_opencode_home(tmp_path)
    store_dir = tmp_path / "sentinel-state"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "usage",
            "import-local",
            "--client",
            "opencode",
            "--store-dir",
            str(store_dir),
            "--opencode-home",
            str(opencode_home),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scanned_sessions"] == 1
    assert payload["imported_events"] == 1
    assert payload["usage_totals"]["input_tokens"] == 8665
    assert payload["usage_totals"]["output_tokens"] == 240
    assert payload["usage_totals"]["cached_input_tokens"] == 14720
    assert payload["usage_totals"]["estimated_cost_usd"] == 0.001321516
    assert payload["cost_confidence"] == "client_reported"
    assert payload["cost_confidence_counts"] == {"client_reported": 1}
    assert payload["events"][0]["provider"] == "opencode"
    assert payload["events"][0]["cost_confidence"] == "client_reported"


def test_usage_import_local_imports_openclaw_usage_with_client_reported_cost(tmp_path):
    openclaw_home = _make_openclaw_home(tmp_path)
    store_dir = tmp_path / "sentinel-state"

    result = CliRunner().invoke(
        app,
        [
            "usage",
            "import-local",
            "--client",
            "openclaw",
            "--store-dir",
            str(store_dir),
            "--openclaw-home",
            str(openclaw_home),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scanned_sessions"] == 1
    assert payload["imported_events"] == 1
    assert payload["usage_totals"]["input_tokens"] == 1660
    assert payload["usage_totals"]["output_tokens"] == 55
    assert payload["usage_totals"]["cached_input_tokens"] == 108938
    assert payload["usage_totals"]["estimated_cost_usd"] == 0.02
    assert payload["cost_confidence"] == "client_reported"
    assert payload["events"][0]["provider"] == "openai"
    assert payload["events"][0]["metadata"]["client"] == "openclaw"


def test_usage_import_local_imports_hermes_usage_with_client_reported_cost(tmp_path):
    hermes_home = _make_hermes_home(tmp_path)
    store_dir = tmp_path / "sentinel-state"

    result = CliRunner().invoke(
        app,
        [
            "usage",
            "import-local",
            "--client",
            "hermes",
            "--store-dir",
            str(store_dir),
            "--hermes-home",
            str(hermes_home),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scanned_sessions"] == 1
    assert payload["imported_events"] == 1
    assert payload["usage_totals"]["input_tokens"] == 1200
    assert payload["usage_totals"]["output_tokens"] == 300
    assert payload["usage_totals"]["cached_input_tokens"] == 70
    assert payload["usage_totals"]["estimated_cost_usd"] == 0.34
    assert payload["cost_confidence"] == "client_reported"
    assert payload["cost_confidence_counts"] == {"client_reported": 1}
    assert payload["events"][0]["provider"] == "anthropic"
    assert payload["events"][0]["metadata"]["client"] == "hermes"
    assert payload["events"][0]["cost_confidence"] == "client_reported"


def test_usage_import_local_persists_zero_token_session_without_usage(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    rollout = next((codex_home / "sessions").rglob("rollout-*.jsonl"))
    rollout.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "timestamp": "2026-07-01T00:00:00Z",
                        "payload": {"id": "session-abc", "cwd": "/work/project"},
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "timestamp": "2026-07-01T00:00:01Z",
                        "payload": {"type": "task_complete"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    connection = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        connection.execute("update threads set tokens_used = 0")
        connection.commit()
    finally:
        connection.close()
    store_dir = tmp_path / "sentinel-state"
    runner = CliRunner()
    args = [
        "usage",
        "import-local",
        "--client",
        "codex",
        "--store-dir",
        str(store_dir),
        "--codex-home",
        str(codex_home),
        "--json",
    ]

    first = runner.invoke(app, args)

    assert first.exit_code == 0, first.output
    payload = json.loads(first.output)
    assert payload["scanned_sessions"] == 1
    assert payload["observed_sessions"] == 1
    assert payload["usage_sessions"] == 0
    assert payload["sessions_without_usage"] == 1
    assert payload["imported_events"] == 0
    assert payload["imported_session_observations"] == 1
    stored = SentinelService(store_dir).list_all_events()
    assert len(stored) == 1
    assert is_local_session_observation_event(stored[0])
    assert stored[0]["event_type"] == "session_observed"
    assert not {
        "provider",
        "model",
        "estimated_input_tokens",
        "estimated_output_tokens",
        "estimated_cost_usd",
        "usage_confidence",
        "cost_confidence",
    }.intersection(stored[0])
    ledger_lines = SentinelService(store_dir).event_log.read_lines()

    second = runner.invoke(app, args)

    assert second.exit_code == 0, second.output
    second_payload = json.loads(second.output)
    assert second_payload["imported_events"] == 0
    assert second_payload["imported_session_observations"] == 0
    assert SentinelService(store_dir).event_log.read_lines() == ledger_lines


def test_usage_import_local_persists_hermes_zero_token_observation_only(
    tmp_path,
):
    hermes_home = _make_hermes_home(tmp_path)
    connection = sqlite3.connect(hermes_home / "state.db")
    try:
        connection.execute(
            """
            update sessions
            set input_tokens = 0,
                output_tokens = 0,
                cache_read_tokens = 0,
                cache_write_tokens = 0,
                reasoning_tokens = 0,
                estimated_cost_usd = 0,
                actual_cost_usd = 0
            """
        )
        connection.commit()
    finally:
        connection.close()
    store_dir = tmp_path / "sentinel-state"

    result = CliRunner().invoke(
        app,
        [
            "usage",
            "import-local",
            "--client",
            "hermes",
            "--store-dir",
            str(store_dir),
            "--hermes-home",
            str(hermes_home),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scanned_sessions"] == 1
    assert payload["observed_sessions"] == 1
    assert payload["usage_sessions"] == 0
    assert payload["sessions_without_usage"] == 1
    assert payload["imported_events"] == 0
    assert payload["imported_session_observations"] == 1
    stored = SentinelService(store_dir).list_all_events()
    assert len(stored) == 1
    assert is_local_session_observation_event(stored[0])
    assert stored[0]["event_type"] == "session_observed"
    assert stored[0]["metadata"]["client"] == "hermes"
    assert stored[0]["metadata"]["source_namespace_fingerprint"]
    assert "client_session_title" not in stored[0]["metadata"]
    assert not {
        "provider",
        "model",
        "estimated_input_tokens",
        "estimated_output_tokens",
        "estimated_cost_usd",
        "usage_confidence",
        "cost_confidence",
    }.intersection(stored[0])


def test_import_local_projects_observation_only_session_and_task(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    rollout = next((codex_home / "sessions").rglob("rollout-*.jsonl"))
    rollout.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "timestamp": "2026-07-01T00:00:00Z",
                        "payload": {"id": "session-abc", "cwd": "/work/project"},
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "timestamp": "2026-07-01T00:00:01Z",
                        "payload": {"type": "task_complete"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    connection = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        connection.execute("update threads set tokens_used = 0")
        connection.commit()
    finally:
        connection.close()
    store_dir = tmp_path / "sentinel-state"

    result = CliRunner().invoke(
        app,
        [
            "usage",
            "import-local",
            "--client",
            "codex",
            "--store-dir",
            str(store_dir),
            "--codex-home",
            str(codex_home),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["sessions_without_usage"] == 1
    assert payload["imported_session_observations"] == 1
    assert payload["preserved_session_observations"] == 1
    client = TestClient(create_local_api_app(store_dir=store_dir))
    sessions_payload = client.get(
        "/sessions",
        headers={"accept": "application/json"},
    ).json()
    assert sessions_payload["total_sessions"] == 1
    session = sessions_payload["sessions"][0]
    assert session["usage"]["rows"] == 0
    assert session["usage"]["estimated_cost_usd"] is None
    assert "not a zero-cost claim" in session["usage_note"]
    assert session["local_client_observation"]["measurement_basis"] == (
        "local_client_log_observed"
    )
    summary = sessions_payload["summary"]
    assert summary["sessions_with_usage"] == 0
    assert summary["attributed_sessions"] == 0
    assert summary["sessions_with_local_client_observation"] == 1
    # Unavailable usage must stay null (not a zero-cost claim), even though
    # the additive token counters legitimately read 0.
    assert summary["totals"]["estimated_cost_usd"] is None
    assert summary["totals"]["fresh_tokens"] == 0
    assert summary["totals"]["total_tokens"] == 0
    tasks_payload = client.get("/tasks").json()
    assert tasks_payload["summary"]["task_count"] == 1
    assert tasks_payload["tasks"][0]["usage"]["usage_availability"] == "unknown"


def test_usage_import_local_imports_codex_and_claude_once(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    claude_home = _make_claude_home(tmp_path)
    store_dir = tmp_path / "sentinel-state"
    runner = CliRunner()

    first = runner.invoke(
        app,
        [
            "usage",
            "import-local",
            "--store-dir",
            str(store_dir),
            "--codex-home",
            str(codex_home),
            "--claude-home",
            str(claude_home),
            "--opencode-home",
            str(tmp_path / "missing-opencode"),
            "--hermes-home",
            str(tmp_path / "missing-hermes"),
            "--openclaw-home",
            str(tmp_path / "missing-openclaw"),
            "--cursor-home",
            str(tmp_path / "missing-cursor"),
            "--json",
        ],
    )

    assert first.exit_code == 0, first.output
    payload = json.loads(first.output)
    assert payload["scanned_sessions"] == 2
    assert payload["imported_events"] == 2
    assert payload["usage_totals"]["input_tokens"] == 1630
    assert payload["usage_totals"]["output_tokens"] == 137
    assert payload["usage_totals"]["cached_input_tokens"] == 977
    assert payload["usage_totals"]["cache_creation_input_tokens"] == 33
    assert payload["usage_totals"]["cache_read_input_tokens"] == 944
    assert payload["usage_totals"]["estimated_cost_usd"] == 0.0

    summary = runner.invoke(app, ["event", "summary", "--store-dir", str(store_dir), "--json"])
    assert summary.exit_code == 0, summary.output
    summary_payload = json.loads(summary.output)["summary"]
    assert summary_payload["event_count"] == 2
    assert summary_payload["estimated_input_tokens"] == 1630
    assert summary_payload["estimated_output_tokens"] == 137
    assert summary_payload["estimated_total_tokens"] == 1767
    assert summary_payload["cached_input_tokens"] == 977
    assert summary_payload["cache_creation_input_tokens"] == 33
    assert summary_payload["cache_read_input_tokens"] == 944
    assert summary_payload["reasoning_output_tokens"] == 22
    assert summary_payload["total_tokens_including_cached"] == 2744
    assert summary_payload["by_provider"] == {"claude-code": 1, "codex": 1}
    assert summary_payload["tokens_by_provider"]["codex"] == {
        "event_count": 1,
        "estimated_cost_usd": 0.0,
        "estimated_input_tokens": 1600,
        "estimated_output_tokens": 125,
        "estimated_total_tokens": 1725,
        "cached_input_tokens": 900,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 900,
        "reasoning_output_tokens": 22,
        "total_tokens_including_cached": 2625,
    }
    assert summary_payload["tokens_by_provider"]["claude-code"] == {
        "event_count": 1,
        "estimated_cost_usd": 0.0,
        "estimated_input_tokens": 30,
        "estimated_output_tokens": 12,
        "estimated_total_tokens": 42,
        "cached_input_tokens": 77,
        "cache_creation_input_tokens": 33,
        "cache_read_input_tokens": 44,
        "reasoning_output_tokens": 0,
        "total_tokens_including_cached": 119,
    }

    second = runner.invoke(
        app,
        [
            "usage",
            "import-local",
            "--store-dir",
            str(store_dir),
            "--codex-home",
            str(codex_home),
            "--claude-home",
            str(claude_home),
            "--opencode-home",
            str(tmp_path / "missing-opencode"),
            "--hermes-home",
            str(tmp_path / "missing-hermes"),
            "--openclaw-home",
            str(tmp_path / "missing-openclaw"),
            "--json",
        ],
    )
    assert second.exit_code == 0, second.output
    assert json.loads(second.output)["imported_events"] == 0


def test_usage_import_local_can_estimate_equivalent_costs_from_known_model_prices(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    claude_home = _make_claude_home(tmp_path)
    store_dir = tmp_path / "sentinel-state"

    result = CliRunner().invoke(
        app,
        [
            "usage",
            "import-local",
            "--store-dir",
            str(store_dir),
            "--codex-home",
            str(codex_home),
            "--claude-home",
            str(claude_home),
            "--opencode-home",
            str(tmp_path / "missing-opencode"),
            "--hermes-home",
            str(tmp_path / "missing-hermes"),
            "--openclaw-home",
            str(tmp_path / "missing-openclaw"),
            "--cursor-home",
            str(tmp_path / "missing-cursor"),
            "--estimate-costs",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["priced_events"] == 2
    assert payload["cost_confidence"] == "estimated_from_tokens"
    assert payload["usage_totals"]["estimated_cost_usd"] > 0

    summary = CliRunner().invoke(app, ["event", "summary", "--store-dir", str(store_dir), "--json"])
    assert summary.exit_code == 0, summary.output
    summary_payload = json.loads(summary.output)["summary"]
    assert summary_payload["estimated_cost_usd"] > 0
    assert summary_payload["by_cost_confidence"] == {"estimated_from_tokens": 2}
    events = CliRunner().invoke(app, ["event", "list", "--store-dir", str(store_dir), "--json"])
    assert events.exit_code == 0, events.output
    event_payloads = json.loads(events.output)["events"]
    assert {event["cost_basis"] for event in event_payloads} == {"pricing_table"}
    by_provider = {event["provider"]: event for event in event_payloads}
    assert by_provider["codex"]["estimated_cost_usd"] == 0.0305
    assert by_provider["claude-code"]["estimated_cost_usd"] == 0.00067825


def test_usage_import_local_dry_run_does_not_write(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    store_dir = tmp_path / "sentinel-state"

    result = CliRunner().invoke(
        app,
        [
            "usage",
            "import-local",
            "--client",
            "codex",
            "--store-dir",
            str(store_dir),
            "--codex-home",
            str(codex_home),
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["importable_sessions"] == 1
    assert payload["imported_events"] == 0
    assert not (store_dir / "events.jsonl").exists()


def test_usage_watch_once_imports_and_exits(tmp_path):
    openclaw_home = _make_openclaw_home(tmp_path)
    store_dir = tmp_path / "sentinel-state"

    result = CliRunner().invoke(
        app,
        [
            "usage",
            "watch",
            "--client",
            "openclaw",
            "--store-dir",
            str(store_dir),
            "--openclaw-home",
            str(openclaw_home),
            "--once",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["imported_events"] == 1
    assert payload["usage_totals"]["cached_input_tokens"] == 108938
    assert any(
        event.get("event_type") == "model_usage"
        for event in SentinelService(store_dir).list_all_events()
    )


def test_usage_import_local_skips_incompatible_codex_schema(tmp_path):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    con = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        con.execute("create table not_threads (id text)")
        con.commit()
    finally:
        con.close()

    result = CliRunner().invoke(
        app,
        [
            "usage",
            "import-local",
            "--client",
            "codex",
            "--store-dir",
            str(tmp_path / "state"),
            "--codex-home",
            str(codex_home),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scanned_sessions"] == 0
    assert payload["imported_events"] == 0


def test_usage_import_local_dedupe_scans_beyond_latest_event_limit(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    store_dir = tmp_path / "sentinel-state"
    store_dir.mkdir(parents=True)
    events_path = store_dir / "events.jsonl"
    imported = {
        "event_id": "evt_old_import",
        "created_at": 1,
        "source": "codex-local-session-import",
        "event_type": "model_usage",
        "metadata": {
            "usage_source": "local_client_session_store",
            "usage_provenance": "agent_sentinel_local_usage_import",
            "client": "codex",
            "client_session_id": "session-abc",
        },
    }
    with events_path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(imported) + "\n")
        for index in range(10050):
            handle.write(
                json.dumps(
                    {
                        "event_id": f"evt_newer_{index}",
                        "created_at": 100 + index,
                        "source": "other",
                        "event_type": "note",
                        "metadata": {"summary": "noise"},
                    }
                )
                + "\n"
            )

    result = CliRunner().invoke(
        app,
        [
            "usage",
            "import-local",
            "--client",
            "codex",
            "--store-dir",
            str(store_dir),
            "--codex-home",
            str(codex_home),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scanned_sessions"] == 1
    assert payload["importable_sessions"] == 0
    assert payload["imported_events"] == 0


def test_usage_import_local_dedupe_recognizes_legacy_pre_provenance_rows(tmp_path):
    codex_home = _make_codex_home(tmp_path)
    store_dir = tmp_path / "sentinel-state"
    store_dir.mkdir(parents=True)
    (store_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "event_id": "evt_legacy_import",
                "created_at": 1,
                "source": "codex-local-session-import",
                "event_type": "model_usage",
                "metadata": {
                    "usage_source": "local_client_session_store",
                    "client": "codex",
                    "client_session_id": "session-abc",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "usage",
            "import-local",
            "--client",
            "codex",
            "--store-dir",
            str(store_dir),
            "--codex-home",
            str(codex_home),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scanned_sessions"] == 1
    assert payload["importable_sessions"] == 0
    assert payload["imported_events"] == 0

    summary = CliRunner().invoke(app, ["event", "summary", "--store-dir", str(store_dir), "--json"])
    assert summary.exit_code == 0, summary.output
    summary_payload = json.loads(summary.output)["summary"]
    assert summary_payload["event_count"] == 1
    assert summary_payload["estimated_input_tokens"] == 0
    assert summary_payload["tokens_by_provider"] == {}


def _append_claude_model_switch_row(claude_home: Path) -> None:
    session = claude_home / "projects" / "-tmp-project" / "claude-session.jsonl"
    with session.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "assistant",
                    "sessionId": "claude-session",
                    "cwd": "/work/project",
                    "message": {
                        "model": "claude-haiku-4-5-20251001",
                        "usage": {
                            "input_tokens": 4,
                            "cache_creation_input_tokens": 1,
                            "cache_read_input_tokens": 2,
                            "output_tokens": 3,
                        },
                    },
                }
            )
            + "\n"
        )


def _append_claude_same_model_growth_row(claude_home: Path) -> None:
    """Grow the fixture session in place: same model, so same row identity/lane."""

    session = claude_home / "projects" / "-tmp-project" / "claude-session.jsonl"
    with session.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "assistant",
                    "sessionId": "claude-session",
                    "cwd": "/work/project",
                    "message": {
                        "model": "claude-opus-4-8",
                        "usage": {
                            "input_tokens": 100,
                            "cache_creation_input_tokens": 5,
                            "cache_read_input_tokens": 6,
                            "output_tokens": 50,
                        },
                    },
                }
            )
            + "\n"
        )


def _stored_claude_import_row(event_id: str, client_session_id: str, *, model: str | None = None, lane: str | None = None) -> dict:
    row = {
        "event_id": event_id,
        "created_at": 1,
        "source": "claude-code-local-session-import",
        "event_type": "model_usage",
        "provider": "claude-code",
        "model": model,
        "estimated_input_tokens": 1,
        "estimated_output_tokens": 1,
        "usage_confidence": "client_reported",
        "metadata": {
            "usage_source": "local_client_session_store",
            "usage_provenance": "agent_sentinel_local_usage_import",
            "client": "claude-code",
            "client_session_id": client_session_id,
            "client_transcript_id": "claude-session",
            "cached_input_tokens": 0,
        },
    }
    if lane is not None:
        row["metadata"]["usage_row_lane"] = lane
    return row


def _claude_import_args(store_dir: Path, claude_home: Path, *extra: str) -> list[str]:
    return [
        "usage",
        "import-local",
        "--client",
        "claude-code",
        "--store-dir",
        str(store_dir),
        "--claude-home",
        str(claude_home),
        *extra,
        "--json",
    ]


def test_claude_model_switch_keeps_session_key_stable_across_discovery(tmp_path):
    claude_home = _make_claude_home(tmp_path)

    first = discover_claude_code_usage(claude_home=claude_home, limit_sessions=10)

    assert len(first) == 1
    assert first[0].client_session_id == "claude-session"
    assert first[0].usage_row_lane == "model:claude-opus-4-8"

    _append_claude_model_switch_row(claude_home)
    second = discover_claude_code_usage(claude_home=claude_home, limit_sessions=10)

    assert [event.client_session_id for event in second] == ["claude-session", "claude-session"]
    assert {event.usage_row_identity for event in second} == {
        ("claude-code", "claude-session", "model:claude-opus-4-8"),
        ("claude-code", "claude-session", "model:claude-haiku-4-5-20251001"),
    }


def test_usage_import_local_model_switch_does_not_double_count(tmp_path):
    claude_home = _make_claude_home(tmp_path)
    store_dir = tmp_path / "sentinel-state"
    runner = CliRunner()

    first = runner.invoke(app, _claude_import_args(store_dir, claude_home))
    assert first.exit_code == 0, first.output
    assert json.loads(first.output)["imported_events"] == 1

    _append_claude_model_switch_row(claude_home)
    second = runner.invoke(app, _claude_import_args(store_dir, claude_home))

    assert second.exit_code == 0, second.output
    payload = json.loads(second.output)
    assert payload["imported_events"] == 1
    assert payload["migrated_events"] == 0
    assert payload["superseded_legacy_rows"] == 0
    stored = SentinelService(store_dir).list_all_events()
    usage_rows = [event for event in stored if event["event_type"] == "model_usage"]
    assert len(usage_rows) == 2
    assert {row["metadata"]["client_session_id"] for row in usage_rows} == {"claude-session"}
    assert all(":model:" not in row["metadata"]["client_session_id"] for row in usage_rows)
    assert {row["metadata"]["usage_row_lane"] for row in usage_rows} == {
        "model:claude-opus-4-8",
        "model:claude-haiku-4-5-20251001",
    }

    summary = runner.invoke(app, ["event", "summary", "--store-dir", str(store_dir), "--json"])
    assert summary.exit_code == 0, summary.output
    summary_payload = json.loads(summary.output)["summary"]
    assert summary_payload["event_count"] == 2
    assert summary_payload["estimated_input_tokens"] == 34
    assert summary_payload["estimated_output_tokens"] == 15
    assert summary_payload["cache_creation_input_tokens"] == 34
    assert summary_payload["cache_read_input_tokens"] == 46


def test_usage_import_local_migrates_legacy_model_suffixed_rows(tmp_path):
    claude_home = _make_claude_home(tmp_path)
    _append_claude_model_switch_row(claude_home)
    store_dir = tmp_path / "sentinel-state"
    store_dir.mkdir(parents=True)
    legacy_keys = [
        "claude-session",
        "claude-session:model:claude-haiku-4-5-20251001",
        "claude-session:model:claude-opus-4-8",
    ]
    with (store_dir / "events.jsonl").open("w", encoding="utf-8") as handle:
        for index, key in enumerate(legacy_keys):
            handle.write(json.dumps(_stored_claude_import_row(f"evt_legacy_{index}", key)) + "\n")

    result = CliRunner().invoke(app, _claude_import_args(store_dir, claude_home))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["importable_sessions"] == 2
    assert payload["imported_events"] == 2
    assert payload["migrated_events"] == 2
    assert payload["superseded_legacy_rows"] == 3
    stored = SentinelService(store_dir).list_all_events()
    usage_rows = [event for event in stored if event["event_type"] == "model_usage"]
    assert len(usage_rows) == 2
    assert not any(str(event["event_id"]).startswith("evt_legacy_") for event in stored)
    assert {row["metadata"]["client_session_id"] for row in usage_rows} == {"claude-session"}
    assert {row["metadata"]["usage_row_lane"] for row in usage_rows} == {
        "model:claude-opus-4-8",
        "model:claude-haiku-4-5-20251001",
    }
    for row in usage_rows:
        assert row["metadata"]["migrated_from_client_session_ids"] == sorted(legacy_keys)
        assert row["metadata"]["usage_key_migration_reason"] == "claude_code_session_key_unification"

    summary = CliRunner().invoke(app, ["event", "summary", "--store-dir", str(store_dir), "--json"])
    assert summary.exit_code == 0, summary.output
    summary_payload = json.loads(summary.output)["summary"]
    assert summary_payload["event_count"] == 2
    assert summary_payload["estimated_input_tokens"] == 34
    assert summary_payload["estimated_output_tokens"] == 15
    assert summary_payload["cache_creation_input_tokens"] == 34
    assert summary_payload["cache_read_input_tokens"] == 46


def test_usage_import_local_never_refreshes_lane_tagged_rows_but_binds_source_once(tmp_path):
    claude_home = _make_claude_home(tmp_path)
    store_dir = tmp_path / "sentinel-state"
    store_dir.mkdir(parents=True)
    stale = _stored_claude_import_row("evt_stale_lane", "claude-session", model="claude-opus-4-8", lane="model:claude-opus-4-8")
    (store_dir / "events.jsonl").write_text(json.dumps(stale) + "\n", encoding="utf-8")
    before = SentinelService(store_dir).event_log.read_lines()

    result = CliRunner().invoke(app, _claude_import_args(store_dir, claude_home))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["importable_sessions"] == 0
    assert payload["imported_events"] == 0
    assert payload["migrated_events"] == 0
    assert payload["source_namespace_adoptions"] == 1
    after = SentinelService(store_dir).event_log.read_lines()
    assert after != before
    stored = SentinelService(store_dir).list_all_events()[0]
    assert stored["event_id"] == "evt_stale_lane"
    assert stored["created_at"] == 1
    assert stored["metadata"]["source_namespace_binding"] == "tofu_explicit_scan_v1"


def test_usage_import_local_skips_untagged_pre_lane_row_but_binds_source_once(tmp_path):
    claude_home = _make_claude_home(tmp_path)
    store_dir = tmp_path / "sentinel-state"
    store_dir.mkdir(parents=True)
    # Pre-fix stored shape: base key, no usage_row_lane metadata; the lane is
    # derived from the row's model so re-import must classify it as refresh.
    stale = _stored_claude_import_row("evt_pre_lane", "claude-session", model="claude-opus-4-8")
    (store_dir / "events.jsonl").write_text(json.dumps(stale) + "\n", encoding="utf-8")
    before = SentinelService(store_dir).event_log.read_lines()

    result = CliRunner().invoke(app, _claude_import_args(store_dir, claude_home))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["importable_sessions"] == 0
    assert payload["imported_events"] == 0
    assert payload["migrated_events"] == 0
    assert payload["source_namespace_adoptions"] == 1
    after = SentinelService(store_dir).event_log.read_lines()
    assert after != before
    stored = SentinelService(store_dir).list_all_events()[0]
    assert stored["event_id"] == "evt_pre_lane"
    assert stored["created_at"] == 1
    assert stored["metadata"]["source_namespace_binding"] == "tofu_explicit_scan_v1"


def test_usage_import_local_dry_run_reports_migration_without_writing(tmp_path):
    claude_home = _make_claude_home(tmp_path)
    store_dir = tmp_path / "sentinel-state"
    store_dir.mkdir(parents=True)
    suffixed = _stored_claude_import_row("evt_legacy_suffixed", "claude-session:model:claude-opus-4-8")
    (store_dir / "events.jsonl").write_text(json.dumps(suffixed) + "\n", encoding="utf-8")
    before = (store_dir / "events.jsonl").read_bytes()

    result = CliRunner().invoke(app, _claude_import_args(store_dir, claude_home, "--dry-run"))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["migrated_events"] == 1
    assert payload["superseded_legacy_rows"] == 1
    assert payload["imported_events"] == 0
    assert (store_dir / "events.jsonl").read_bytes() == before


def test_usage_import_local_keeps_raced_in_duplicate_new_row_without_duplicating(tmp_path, monkeypatch):
    # Plan-then-write is not atomic across processes: another importer (a watch
    # daemon or the dashboard button) can commit the same brand-new session
    # between this import's plan and its write. Under the never-refresh default
    # a NEW row is insert-if-absent: the write must NOT append a second row with
    # the same identity, and — because default never replaces an existing row —
    # it leaves the raced-in row in place (writing nothing) rather than
    # superseding it with a possibly-staler snapshot.
    import agentacct.cli as cli_module
    from agentacct.service import SentinelService

    claude_home = _make_claude_home(tmp_path)
    store_dir = tmp_path / "sentinel-state"
    real_plan = cli_module.plan_local_usage_import

    def racing_plan(candidates, existing_events):
        plan = real_plan(candidates, existing_events)
        racer = SentinelService(store_dir)
        for candidate in plan.new_candidates:
            racer.record_event(candidate.to_sentinel_event(), trusted_usage_import=True)
        return plan

    monkeypatch.setattr(cli_module, "plan_local_usage_import", racing_plan)

    result = CliRunner().invoke(app, _claude_import_args(store_dir, claude_home))

    assert result.exit_code == 0, result.output
    # The raced-in row already exists, so the never-refresh default writes nothing.
    assert json.loads(result.output)["imported_events"] == 0
    stored = SentinelService(store_dir).list_all_events()
    usage_rows = [event for event in stored if event["event_type"] == "model_usage"]
    # Exactly one row (no duplicate), carrying the correct totals.
    assert len(usage_rows) == 1
    row = usage_rows[0]
    assert row["metadata"]["client_session_id"] == "claude-session"
    assert row["estimated_input_tokens"] == 30
    assert row["estimated_output_tokens"] == 12


def test_usage_import_local_text_reports_actual_count_after_raced_new_row(
    tmp_path,
    monkeypatch,
):
    import agentacct.cli as cli_module
    from agentacct.service import SentinelService

    claude_home = _make_claude_home(tmp_path)
    store_dir = tmp_path / "sentinel-state"
    real_plan = cli_module.plan_local_usage_import

    def racing_plan(candidates, existing_events):
        plan = real_plan(candidates, existing_events)
        racer = SentinelService(store_dir)
        for candidate in plan.new_candidates:
            racer.record_event(candidate.to_sentinel_event(), trusted_usage_import=True)
        return plan

    monkeypatch.setattr(cli_module, "plan_local_usage_import", racing_plan)
    args = _claude_import_args(store_dir, claude_home)
    args.remove("--json")

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 0, result.output
    assert "Imported 0 local client usage session(s)." in result.output
    assert "Imported 1 local client usage session(s)." not in result.output


def test_usage_import_local_guard_abort_reports_only_actual_results(
    tmp_path,
    monkeypatch,
):
    import agentacct.cli as cli_module
    from agentacct.service import SentinelService

    claude_home = _make_claude_home(tmp_path)
    _append_claude_model_switch_row(claude_home)
    store_dir = tmp_path / "sentinel-state"
    store_dir.mkdir(parents=True)
    legacy_keys = [
        "claude-session",
        "claude-session:model:claude-haiku-4-5-20251001",
        "claude-session:model:claude-opus-4-8",
    ]
    with (store_dir / "events.jsonl").open("w", encoding="utf-8") as handle:
        for index, key in enumerate(legacy_keys):
            handle.write(json.dumps(_stored_claude_import_row(f"evt_guard_{index}", key)) + "\n")
    real_plan = cli_module.plan_local_usage_import
    raced = False

    def racing_plan(candidates, existing_events):
        nonlocal raced
        plan = real_plan(candidates, existing_events)
        if not raced and plan.migration_candidates:
            raced = True
            service = SentinelService(store_dir)
            current = service.list_all_events()[0]
            current_event_id = current["event_id"]
            service.replace_events(
                lambda event: event.get("event_id") == current_event_id,
                [current],
                trusted_usage_import=True,
            )
        return plan

    monkeypatch.setattr(cli_module, "plan_local_usage_import", racing_plan)

    result = CliRunner().invoke(
        app,
        _claude_import_args(store_dir, claude_home, "--estimate-costs"),
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["importable_sessions"] == 2
    assert payload["imported_events"] == 0
    assert payload["migrated_events"] == 0
    assert payload["superseded_legacy_rows"] == 0
    assert payload["refreshed_events"] == 0
    assert payload["repriced_events"] == 0
    assert payload["priced_events"] == 0
    assert payload["usage_totals"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "estimated_cost_usd": 0,
    }
    stored = SentinelService(store_dir).list_all_events()
    assert len(stored) == 3
    assert {
        event["metadata"]["client_session_id"] for event in stored
    } == set(legacy_keys)


def test_usage_import_local_refresh_replaces_grown_session_totals(tmp_path):
    from agentacct.service import SentinelService

    claude_home = _make_claude_home(tmp_path)
    store_dir = tmp_path / "sentinel-state"
    runner = CliRunner()

    first = runner.invoke(app, _claude_import_args(store_dir, claude_home))
    assert first.exit_code == 0, first.output
    assert json.loads(first.output)["imported_events"] == 1

    note = SentinelService(store_dir).record_event(
        {"source": "test", "event_type": "note", "metadata": {"summary": "keep me"}}
    )
    _append_claude_same_model_growth_row(claude_home)

    # The never-refresh default holds: the grown session writes nothing.
    stale = runner.invoke(app, _claude_import_args(store_dir, claude_home))
    assert stale.exit_code == 0, stale.output
    stale_payload = json.loads(stale.output)
    assert stale_payload["imported_events"] == 0
    assert stale_payload["refreshed_events"] == 0

    refreshed = runner.invoke(app, _claude_import_args(store_dir, claude_home, "--refresh"))
    assert refreshed.exit_code == 0, refreshed.output
    payload = json.loads(refreshed.output)
    assert payload["refresh"] is True
    assert payload["imported_events"] == 1
    assert payload["refreshed_events"] == 1
    assert payload["usage_totals"]["input_tokens"] == 130
    assert payload["usage_totals"]["output_tokens"] == 62

    stored = SentinelService(store_dir).list_all_events()
    usage_rows = [event for event in stored if event["event_type"] == "model_usage"]
    assert len(usage_rows) == 1
    row = usage_rows[0]
    assert row["estimated_input_tokens"] == 130
    assert row["estimated_output_tokens"] == 62
    assert row["usage_confidence"] == "client_reported"
    assert row["metadata"]["usage_source"] == "local_client_session_store"
    assert row["metadata"]["usage_provenance"] == "agent_sentinel_local_usage_import"
    assert row["metadata"]["usage_row_lane"] == "model:claude-opus-4-8"
    # Non-usage events are never touched by the refresh predicate.
    assert any(event.get("event_id") == note["event_id"] for event in stored)


def test_usage_watch_refresh_replaces_grown_session_totals(tmp_path):
    claude_home = _make_claude_home(tmp_path)
    store_dir = tmp_path / "sentinel-state"
    runner = CliRunner()
    watch_args = [
        "usage",
        "watch",
        "--client",
        "claude-code",
        "--store-dir",
        str(store_dir),
        "--claude-home",
        str(claude_home),
        "--once",
        "--json",
    ]

    first = runner.invoke(app, watch_args)
    assert first.exit_code == 0, first.output
    assert json.loads(first.output)["imported_events"] == 1

    _append_claude_same_model_growth_row(claude_home)

    second = runner.invoke(app, [*watch_args, "--refresh"])
    assert second.exit_code == 0, second.output
    payload = json.loads(second.output)
    assert payload["refresh"] is True
    assert payload["imported_events"] == 1
    assert payload["refreshed_events"] == 1
    stored = SentinelService(store_dir).list_all_events()
    usage_rows = [event for event in stored if event["event_type"] == "model_usage"]
    assert len(usage_rows) == 1
    assert usage_rows[0]["estimated_input_tokens"] == 130
    assert usage_rows[0]["estimated_output_tokens"] == 62


def test_usage_watch_refresh_skips_unchanged_rows_without_reissuing_event_id(tmp_path):
    # D5: a --refresh scan over an idle (unchanged) session must be a no-op —
    # no ledger rewrite and no reissued event_id/created_at on unchanged rows.
    from agentacct.service import SentinelService

    claude_home = _make_claude_home(tmp_path)
    store_dir = tmp_path / "sentinel-state"
    runner = CliRunner()

    first = runner.invoke(app, _claude_import_args(store_dir, claude_home))
    assert first.exit_code == 0, first.output
    before_lines = SentinelService(store_dir).event_log.read_lines()
    before_ids = [e["event_id"] for e in SentinelService(store_dir).list_all_events() if e["event_type"] == "model_usage"]

    refreshed = runner.invoke(app, _claude_import_args(store_dir, claude_home, "--refresh"))
    assert refreshed.exit_code == 0, refreshed.output
    payload = json.loads(refreshed.output)
    assert payload["imported_events"] == 0
    assert payload["refreshed_events"] == 0
    assert SentinelService(store_dir).event_log.read_lines() == before_lines
    after_ids = [e["event_id"] for e in SentinelService(store_dir).list_all_events() if e["event_type"] == "model_usage"]
    assert after_ids == before_ids


def test_usage_reconcile_failure_is_fail_open_degraded_then_noop_self_heals(
    tmp_path,
    monkeypatch,
):
    from agentacct.ingestion_health import (
        EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE,
    )
    from agentacct.service import SentinelService

    claude_home = _make_claude_home(tmp_path)
    store_dir = tmp_path / "sentinel-state"
    runner = CliRunner()
    outcomes: list[object] = [
        RuntimeError("private projection detail"),
        {
            "enabled": True,
            "complete_requested": True,
            "complete_applied": True,
            "errors": [],
            "conflicts": 0,
        },
        {
            "enabled": True,
            "complete_requested": True,
            "complete_applied": True,
            "errors": [],
            "conflicts": 2,
        },
    ]

    def reconcile(_service, *, complete=True, transport="internal"):
        assert complete is True
        assert transport == "internal"
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(
        SentinelService,
        "reconcile_evidence_refreshable_usage_snapshot",
        reconcile,
    )

    first = runner.invoke(app, _claude_import_args(store_dir, claude_home))
    assert first.exit_code == 0, first.output
    failed_payload = json.loads(first.output)
    assert failed_payload["imported_events"] == 1
    assert failed_payload["evidence_refreshable_usage"]["errors"] == [
        EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE
    ]
    assert failed_payload["ingestion_health"]["state"] == "degraded"
    assert failed_payload["ingestion_health"]["issues"][0]["code"] == (
        EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE
    )
    assert "private projection detail" not in json.dumps(
        failed_payload["ingestion_health"]
    )
    assert len(SentinelService(store_dir).list_all_events()) == 1

    # The next persisted scan is a ledger no-op, but it still runs the full
    # Evidence reconcile and replaces the failed durable health receipt.
    second = runner.invoke(app, _claude_import_args(store_dir, claude_home))
    assert second.exit_code == 0, second.output
    healed_payload = json.loads(second.output)
    assert healed_payload["imported_events"] == 0
    assert healed_payload["evidence_refreshable_usage"]["complete_applied"] is True
    assert healed_payload["ingestion_health"]["issues"] == []
    assert healed_payload["ingestion_health"]["sources"][0]["state"] == "healthy"
    assert len(SentinelService(store_dir).list_all_events()) == 1

    watch = runner.invoke(
        app,
        [
            "usage",
            "watch",
            "--client",
            "claude-code",
            "--store-dir",
            str(store_dir),
            "--claude-home",
            str(claude_home),
            "--once",
        ],
    )
    assert watch.exit_code == 0, watch.output
    assert "errors=0 conflicts=2" in watch.output
    assert "Evidence v2 current-usage reconciliation is not healthy" in watch.output


def test_evidence_refreshable_usage_warning_surfaces_existing_conflicts(capsys):
    # existing_conflicts alone can make the reconcile unhealthy; the warning must
    # print that count, not just conflicts (which is 0 here), so the degraded
    # cause is not hidden.
    from agentacct.cli import _print_evidence_refreshable_usage_warning

    _print_evidence_refreshable_usage_warning(
        {
            "evidence_refreshable_usage": {
                "complete_requested": True,
                "complete_applied": True,
                "errors": [],
                "conflicts": 0,
                "existing_conflicts": 2,
            }
        }
    )
    err = capsys.readouterr().err
    assert "current-usage reconciliation is not healthy" in err
    assert "conflicts=0" in err
    assert "existing_conflicts=2" in err


def test_usage_import_refresh_preserves_prior_estimate_on_unchanged_session(tmp_path):
    # D1: a prior --estimate-costs run leaves an estimated_from_tokens cost; a
    # later --refresh WITHOUT --estimate-costs over the unchanged session must
    # NOT strip that estimate (the unchanged row is skipped, so it survives).
    from agentacct.service import SentinelService

    claude_home = _make_claude_home(tmp_path)
    store_dir = tmp_path / "sentinel-state"
    runner = CliRunner()

    first = runner.invoke(app, _claude_import_args(store_dir, claude_home, "--estimate-costs"))
    assert first.exit_code == 0, first.output
    priced = [e for e in SentinelService(store_dir).list_all_events() if e["event_type"] == "model_usage"][0]
    assert priced["cost_confidence"] == "estimated_from_tokens"
    assert priced["estimated_cost_usd"] is not None

    refreshed = runner.invoke(app, _claude_import_args(store_dir, claude_home, "--refresh"))
    assert refreshed.exit_code == 0, refreshed.output
    assert json.loads(refreshed.output)["refreshed_events"] == 0
    after = [e for e in SentinelService(store_dir).list_all_events() if e["event_type"] == "model_usage"][0]
    assert after["estimated_cost_usd"] == priced["estimated_cost_usd"]
    assert after["cost_confidence"] == "estimated_from_tokens"
    assert after["event_id"] == priced["event_id"]


def test_usage_import_refresh_carries_forward_migration_provenance(tmp_path):
    # D2: refreshing a previously-migrated row with grown totals must keep the
    # migration audit trail (migrated_from_client_session_ids) — never drop it.
    claude_home = _make_claude_home(tmp_path)
    store_dir = tmp_path / "sentinel-state"
    store_dir.mkdir(parents=True)
    migrated = _stored_claude_import_row("evt_migrated", "claude-session", model="claude-opus-4-8", lane="model:claude-opus-4-8")
    migrated["metadata"]["migrated_from_client_session_ids"] = ["claude-session:model:claude-haiku-4-5-20251001"]
    migrated["metadata"]["usage_key_migration_reason"] = "claude_code_session_key_unification"
    (store_dir / "events.jsonl").write_text(json.dumps(migrated) + "\n", encoding="utf-8")

    result = CliRunner().invoke(app, _claude_import_args(store_dir, claude_home, "--refresh"))

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["refreshed_events"] == 1
    usage_rows = _stored_usage_rows(store_dir)
    assert len(usage_rows) == 1
    row = usage_rows[0]
    assert row["estimated_input_tokens"] == 30  # refreshed to fresh totals
    assert row["metadata"]["migrated_from_client_session_ids"] == ["claude-session:model:claude-haiku-4-5-20251001"]
    assert row["metadata"]["usage_key_migration_reason"] == "claude_code_session_key_unification"


# ---------------------------------------------------------------------------
# Pre-merge triage fix 1: LiteLLM TTL auto-refresh + unknown→priced reprice
# ---------------------------------------------------------------------------


def _codex_import_args(store_dir: Path, codex_home: Path, *extra: str) -> list[str]:
    return [
        "usage",
        "import-local",
        "--client",
        "codex",
        "--store-dir",
        str(store_dir),
        "--codex-home",
        str(codex_home),
        "--json",
        *extra,
    ]


def _stored_usage_rows(store_dir: Path) -> list[dict]:
    return [
        event
        for event in SentinelService(store_dir).list_all_events()
        if event.get("event_type") == "model_usage"
    ]


def _write_gpt56_snapshot(store_dir: Path, *, input_cost: float = 0.000005) -> Path:
    catalog_path = default_pricing_catalog_snapshot_path(store_dir)
    assert catalog_path is not None
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(
        json.dumps(
            {
                "gpt-5.6-sol": {
                    "litellm_provider": "openai",
                    "input_cost_per_token": input_cost,
                    "output_cost_per_token": 0.00003,
                    "cache_read_input_token_cost": 0.0000005,
                }
            }
        ),
        encoding="utf-8",
    )
    return catalog_path


def test_usage_refresh_reprices_unknown_cost_row_once_when_catalog_resolves(tmp_path):
    """The live gap end-to-end: a codex gpt-5.6-sol session imports with
    cost_confidence unknown (no catalog entry anywhere); once the LiteLLM
    snapshot carries the model (openai key + codex provider alias), a
    --refresh --estimate-costs scan replaces the row with a priced one —
    provenance stamped, event id reissued exactly once."""

    codex_home = _make_codex_home(tmp_path, model="gpt-5.6-sol")
    store_dir = tmp_path / "sentinel-state"
    runner = CliRunner()

    first = runner.invoke(app, _codex_import_args(store_dir, codex_home, "--estimate-costs"))
    assert first.exit_code == 0, first.output
    first_payload = json.loads(first.output)
    assert first_payload["imported_events"] == 1
    assert first_payload["repriced_events"] == 0
    unknown_row = _stored_usage_rows(store_dir)[0]
    assert unknown_row["cost_confidence"] == "unknown"
    assert unknown_row["estimated_cost_usd"] is None
    unknown_event_id = unknown_row["event_id"]

    # Refresh while the model still resolves nowhere: the unknown row stays
    # untouched (no reissue).
    still_unknown = runner.invoke(app, _codex_import_args(store_dir, codex_home, "--estimate-costs", "--refresh"))
    assert still_unknown.exit_code == 0, still_unknown.output
    assert json.loads(still_unknown.output)["repriced_events"] == 0
    assert _stored_usage_rows(store_dir)[0]["event_id"] == unknown_event_id

    _write_gpt56_snapshot(store_dir)

    repriced = runner.invoke(app, _codex_import_args(store_dir, codex_home, "--estimate-costs", "--refresh"))
    assert repriced.exit_code == 0, repriced.output
    repriced_payload = json.loads(repriced.output)
    assert repriced_payload["repriced_events"] == 1
    assert repriced_payload["refreshed_events"] == 0  # totals unchanged; this is the reprice path
    row = _stored_usage_rows(store_dir)[0]
    assert row["cost_confidence"] == "estimated_from_tokens"
    assert row["estimated_cost_usd"] is not None and row["estimated_cost_usd"] > 0
    assert row["metadata"]["pricing_source"] == "litellm_model_cost_map"
    assert row["metadata"]["pricing_source_provider"] == "openai"
    assert row["metadata"]["pricing_source_model"] == "gpt-5.6-sol"
    assert row["event_id"] != unknown_event_id  # replaced (reissued) once
    repriced_event_id = row["event_id"]

    # Now priced: a further refresh scan is a no-op — reissued ONCE, ever.
    settled = runner.invoke(app, _codex_import_args(store_dir, codex_home, "--estimate-costs", "--refresh"))
    assert settled.exit_code == 0, settled.output
    settled_payload = json.loads(settled.output)
    assert settled_payload["repriced_events"] == 0
    assert settled_payload["imported_events"] == 0
    assert _stored_usage_rows(store_dir)[0]["event_id"] == repriced_event_id


def test_unknown_cost_reprice_requires_both_refresh_and_estimate_costs(tmp_path):
    codex_home = _make_codex_home(tmp_path, model="gpt-5.6-sol")
    store_dir = tmp_path / "sentinel-state"
    runner = CliRunner()

    first = runner.invoke(app, _codex_import_args(store_dir, codex_home))
    assert first.exit_code == 0, first.output
    unknown_event_id = _stored_usage_rows(store_dir)[0]["event_id"]
    _write_gpt56_snapshot(store_dir)

    # --estimate-costs WITHOUT --refresh: never rewrites stored rows.
    no_refresh = runner.invoke(app, _codex_import_args(store_dir, codex_home, "--estimate-costs"))
    assert no_refresh.exit_code == 0, no_refresh.output
    assert json.loads(no_refresh.output)["repriced_events"] == 0
    # --refresh WITHOUT --estimate-costs: pricing paths stay opt-in on the CLI.
    no_estimate = runner.invoke(app, _codex_import_args(store_dir, codex_home, "--refresh"))
    assert no_estimate.exit_code == 0, no_estimate.output
    assert json.loads(no_estimate.output)["repriced_events"] == 0

    row = _stored_usage_rows(store_dir)[0]
    assert row["event_id"] == unknown_event_id
    assert row["cost_confidence"] == "unknown"


def test_priced_row_catalog_price_drift_never_rewrites(tmp_path):
    """Phase-3 stability rule pinned against the new reprice path: once a row
    is priced, later catalog price changes never rewrite it — only the
    unknown→priced transition is refresh-worthy."""

    codex_home = _make_codex_home(tmp_path, model="gpt-5.6-sol")
    store_dir = tmp_path / "sentinel-state"
    runner = CliRunner()
    _write_gpt56_snapshot(store_dir, input_cost=0.000005)

    first = runner.invoke(app, _codex_import_args(store_dir, codex_home, "--estimate-costs"))
    assert first.exit_code == 0, first.output
    priced = _stored_usage_rows(store_dir)[0]
    assert priced["cost_confidence"] == "estimated_from_tokens"

    # The catalog price drifts (2x): the stored row must NOT be rewritten.
    _write_gpt56_snapshot(store_dir, input_cost=0.00001)
    drifted = runner.invoke(app, _codex_import_args(store_dir, codex_home, "--estimate-costs", "--refresh"))
    assert drifted.exit_code == 0, drifted.output
    drift_payload = json.loads(drifted.output)
    assert drift_payload["repriced_events"] == 0
    assert drift_payload["refreshed_events"] == 0
    after = _stored_usage_rows(store_dir)[0]
    assert after["event_id"] == priced["event_id"]
    assert after["estimated_cost_usd"] == priced["estimated_cost_usd"]


def test_estimate_costs_auto_refreshes_stale_snapshot_from_litellm(tmp_path, monkeypatch):
    """TTL auto-refresh wired into the import payload (import-local AND every
    watch tick): with no snapshot on disk, an --estimate-costs import fetches
    the LiteLLM table (mocked) and prices the session from it in the same run."""

    import httpx

    monkeypatch.setenv("AGENT_CHRONICLE_PRICING_AUTO_REFRESH", "1")
    calls: list[str] = []

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "gpt-5.6-sol": {
                    "litellm_provider": "openai",
                    "input_cost_per_token": 0.000005,
                    "output_cost_per_token": 0.00003,
                }
            }

    def _fake_get(url, **kwargs):
        calls.append(url)
        return _FakeResponse()

    monkeypatch.setattr(httpx, "get", _fake_get)
    codex_home = _make_codex_home(tmp_path, model="gpt-5.6-sol")
    store_dir = tmp_path / "sentinel-state"

    result = CliRunner().invoke(app, _codex_import_args(store_dir, codex_home, "--estimate-costs"))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["pricing_auto_refresh"]["refreshed"] is True
    assert len(calls) == 1
    assert payload["priced_events"] == 1
    row = _stored_usage_rows(store_dir)[0]
    assert row["cost_confidence"] == "estimated_from_tokens"
    assert row["metadata"]["pricing_source"] == "litellm_model_cost_map"
    catalog_path = default_pricing_catalog_snapshot_path(store_dir)
    assert catalog_path.exists()

    # Second run inside the TTL: no second fetch (cheap stat per watch tick).
    again = CliRunner().invoke(app, _codex_import_args(store_dir, codex_home, "--estimate-costs"))
    assert again.exit_code == 0, again.output
    assert json.loads(again.output)["pricing_auto_refresh"] == {"refreshed": False, "reason": "fresh"}
    assert len(calls) == 1


def test_estimate_costs_import_proceeds_when_auto_refresh_fetch_fails(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setenv("AGENT_CHRONICLE_PRICING_AUTO_REFRESH", "1")

    def _failing_get(url, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(httpx, "get", _failing_get)
    codex_home = _make_codex_home(tmp_path)  # builtin-priced gpt-5.5
    store_dir = tmp_path / "sentinel-state"

    result = CliRunner().invoke(app, _codex_import_args(store_dir, codex_home, "--estimate-costs"))

    # The failed fetch NEVER blocks or fails the import; the builtin catalog
    # still prices the session and the failure is recorded in the sidecar.
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["pricing_auto_refresh"]["reason"] == "error"
    assert payload["imported_events"] == 1
    assert payload["priced_events"] == 1
    from agentacct.pricing_catalog import read_pricing_snapshot_metadata

    catalog_path = default_pricing_catalog_snapshot_path(store_dir)
    metadata = read_pricing_snapshot_metadata(catalog_path)
    assert "network down" in metadata["last_refresh_error"]
    assert metadata["last_refresh_attempt_at"] > 0


def test_usage_import_preserves_unparseable_ledger_lines(tmp_path):
    # D4: the whole-ledger rewrite must carry corrupt-but-recoverable bytes
    # through verbatim (a torn append from a killed writer is forensic evidence).
    claude_home = _make_claude_home(tmp_path)
    store_dir = tmp_path / "sentinel-state"
    store_dir.mkdir(parents=True)
    torn = '{"source": "old-writer", "event_type": "model_usage", "TRUNCATED'
    (store_dir / "events.jsonl").write_text(torn + "\n", encoding="utf-8")

    result = CliRunner().invoke(app, _claude_import_args(store_dir, claude_home))

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["imported_events"] == 1
    lines = (store_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert torn in lines


def test_claude_child_transcript_model_switch_keeps_child_key(tmp_path):
    claude_home = _make_claude_home(tmp_path)
    project = claude_home / "projects" / "-tmp-project"
    agent_file = project / "agent-a123.jsonl"
    rows = [
        {
            "type": "assistant",
            "sessionId": "claude-session",
            "cwd": "/work/project",
            "message": {
                "id": "msg_child_opus",
                "model": "claude-opus-4-8",
                "usage": {
                    "input_tokens": 3,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 4,
                    "output_tokens": 2,
                },
            },
        },
        {
            "type": "assistant",
            "sessionId": "claude-session",
            "cwd": "/work/project",
            "message": {
                "id": "msg_child_haiku",
                "model": "claude-haiku-4-5-20251001",
                "usage": {
                    "input_tokens": 5,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 6,
                    "output_tokens": 1,
                },
            },
        },
    ]
    agent_file.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    events = discover_claude_code_usage(claude_home=claude_home, limit_sessions=10)

    child_events = [event for event in events if event.client_transcript_id == "agent-a123"]
    assert len(child_events) == 2
    assert {event.client_session_id for event in child_events} == {"claude-session:agent-a123"}
    assert {event.client_session_kind for event in child_events} == {"child"}
    assert {event.parent_client_session_id for event in child_events} == {"claude-session"}
    assert {event.usage_row_lane for event in child_events} == {
        "model:claude-opus-4-8",
        "model:claude-haiku-4-5-20251001",
    }


def test_codex_identity_ignores_model_label_changes(tmp_path):
    stored = {
        "event_id": "evt_codex_row",
        "created_at": 1,
        "source": "codex-local-session-import",
        "event_type": "model_usage",
        "model": None,
        "metadata": {
            "usage_source": "local_client_session_store",
            "usage_provenance": "agent_sentinel_local_usage_import",
            "client": "codex",
            "client_session_id": "session-abc",
        },
    }
    candidate = ClientUsageEvent(
        client="codex",
        client_session_id="session-abc",
        source_path=tmp_path / "rollout.jsonl",
        title=None,
        cwd=None,
        model="gpt-5.5",
        input_tokens=10,
        output_tokens=2,
    )

    plan = plan_local_usage_import([candidate], [stored])

    assert plan.refresh_candidates == [candidate]
    assert plan.new_candidates == []
    assert plan.migration_candidates == []
    assert plan.replaced_alias_keys_by_base == {}


def test_usage_row_compare_ignores_revision_watermark_but_not_usage_changes(
    tmp_path,
):
    candidate = ClientUsageEvent(
        client="claude-code",
        client_session_id="claude-session",
        source_path=tmp_path / "claude-session.jsonl",
        title=None,
        cwd="/work/project",
        model="claude-opus-4-8",
        input_tokens=30,
        output_tokens=12,
        updated_at=1_782_036_000,
        usage_row_lane="model:claude-opus-4-8",
        source_revision_at=1_782_036_000_123_456,
        source_revision_basis="transcript_file_mtime_us",
    ).to_sentinel_event()

    # A transcript mtime advance without any usage change must not force a
    # churn rewrite of the stored row.
    revision_only = json.loads(json.dumps(candidate))
    revision_only["metadata"]["source_revision_at"] = 1_782_036_005_654_321
    revision_only["metadata"]["source_revision_basis"] = "state_db_mtime_ns"
    assert (
        client_usage_module._local_usage_candidate_matches_stored_row(
            candidate, revision_only
        )
        is True
    )

    # Pre-watermark stored rows lack both fields entirely; the candidate's
    # new watermark alone is still not a content change.
    watermark_missing = json.loads(json.dumps(candidate))
    del watermark_missing["metadata"]["source_revision_at"]
    del watermark_missing["metadata"]["source_revision_basis"]
    assert (
        client_usage_module._local_usage_candidate_matches_stored_row(
            candidate, watermark_missing
        )
        is True
    )

    # A real metadata difference still forces the refresh.
    usage_changed = json.loads(json.dumps(candidate))
    usage_changed["metadata"]["updated_at"] = 1_782_036_999
    assert (
        client_usage_module._local_usage_candidate_matches_stored_row(
            candidate, usage_changed
        )
        is False
    )


def test_product_dashboard_prefers_global_store_and_warns_about_project_mcp_shadow(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_STORE_DIR, raising=False)
    monkeypatch.delenv(LEGACY_ENV_STORE_DIR, raising=False)
    fake_home = tmp_path / "home"
    global_store = fake_home / ".agent-sentinel-global" / "state"
    global_store.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))

    project = tmp_path / "project"
    (project / ".agent-sentinel" / "state").mkdir(parents=True)
    (project / ".codex").mkdir()
    (project / ".codex" / "config.toml").write_text(
        "[mcp_servers.agent-sentinel]\n"
        'command = "agent-sentinel"\n'
        'args = ["mcp", "serve", "--store-dir", ".agent-sentinel/state"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(project)

    served: list[Path] = []
    real_create = cli_module.create_local_api_app

    def _capture(*, store_dir, **kwargs):
        served.append(Path(store_dir))
        return real_create(store_dir=store_dir, **kwargs)

    monkeypatch.setattr(cli_module, "create_local_api_app", _capture)
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)

    result = CliRunner().invoke(app, ["serve"])

    assert result.exit_code == 0, result.output
    assert served == [global_store]
    assert "All projects" in result.output
    assert "project MCP" in result.output
    assert "new agent session" in result.output


def test_task_title_prefers_trusted_primary_chat_title_over_work_step():
    task = {
        "title_override": None,
        "primary_root": {"client": "codex", "client_session_id": "prediction-root"},
        "sessions": [
            {
                "client": "codex",
                "client_session_id": "prediction-root",
                "client_session_title": "Build prediction market MVP",
                "project": "prediction-market",
            }
        ],
        "work_items": [
            {
                "client": "codex",
                "client_session_id": "prediction-root",
                "kind": "implementation",
                "title": "Rebrand prediction market as Eden",
                "started_at": 1,
            }
        ],
    }

    assert _task_title(task) == "Build prediction market MVP"
