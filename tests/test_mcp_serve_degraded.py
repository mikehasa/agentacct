"""Degraded-but-connected `agentacct mcp serve` (fix/opencode-connection, Phase 1).

An MCP server's process exit is the only liveness signal the host has: exiting
at startup because the store could not be resolved reads to the client as a
CRASH (the most likely cause of the reported "it crashed my opencode"). These
tests lock the new contract:

- a store-less server stays connected — it answers `initialize` and
  `tools/list` and returns a legible JSON-RPC error on any recording tool call,
  never exits non-zero, and never creates or picks a store;
- an unwritable/invalid `--store-dir` surfaces as that same JSON-RPC error, not
  an uncaught startup exception;
- stdout carries only JSON-RPC frames (no banner/log) before the first response.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

from agent_chronicle.mcp import (
    DEGRADED_NO_STORE_MESSAGE,
    read_mcp_message_with_framing,
    serve_stdio,
)

_REQUESTS = (
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}}},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "sentinel_record_section",
            "arguments": {"source": "opencode", "section_id": "x", "section_status": "started"},
        },
    },
)


def _request_bytes() -> bytes:
    # Raw newline-delimited JSON — the framing Claude Code / Codex / opencode use
    # for local stdio MCP servers.
    return b"".join(json.dumps(message).encode("utf-8") + b"\n" for message in _REQUESTS)


def _drive_serve_stdio(**serve_kwargs) -> tuple[list[dict], bytes]:
    out = io.BytesIO()
    serve_stdio(stdin=io.BytesIO(_request_bytes()), stdout=out, **serve_kwargs)
    raw = out.getvalue()
    reader = io.BytesIO(raw)
    responses: list[dict] = []
    while True:
        framed = read_mcp_message_with_framing(reader)
        if framed is None:
            break
        responses.append(framed[0])
    return responses, raw


def _parse_responses(raw_stdout: bytes) -> list[dict]:
    reader = io.BytesIO(raw_stdout)
    responses: list[dict] = []
    while True:
        framed = read_mcp_message_with_framing(reader)
        if framed is None:
            break
        responses.append(framed[0])
    return responses


# --- In-process serve_stdio (fast, deterministic) ----------------------------


def test_serve_stdio_degraded_answers_handshake_and_errors_on_tool_call() -> None:
    responses, raw = _drive_serve_stdio(store_dir=None, degraded_reason=DEGRADED_NO_STORE_MESSAGE)

    assert len(responses) == 3
    init, tools, call = responses

    # initialize + tools/list answer exactly like the live server.
    assert init["result"]["serverInfo"]["name"] == "agent-chronicle"
    assert init["result"]["protocolVersion"] == "2024-11-05"
    assert "instructions" in init["result"]
    assert isinstance(tools["result"]["tools"], list) and tools["result"]["tools"]

    # The recording tool call returns a legible JSON-RPC error, not a crash.
    assert "result" not in call
    assert call["error"]["code"] == -32000
    assert "no store configured" in call["error"]["message"]

    # Stdout cleanliness: the very first byte is the JSON-RPC frame — no banner
    # or log precedes the first response.
    assert raw[:1] == b"{"


def test_serve_stdio_unwritable_store_dir_degrades_instead_of_raising(tmp_path: Path) -> None:
    blocker = tmp_path / "afile"
    blocker.write_text("not a directory", encoding="utf-8")
    bad_store = blocker / "state"  # mkdir(parents=True) must fail: parent is a file

    # Must NOT raise before the read loop.
    responses, _raw = _drive_serve_stdio(store_dir=bad_store)

    assert len(responses) == 3
    init, _tools, call = responses
    assert init["result"]["serverInfo"]["name"] == "agent-chronicle"
    assert call["error"]["code"] == -32000
    assert "not usable" in call["error"]["message"]
    assert str(bad_store) in call["error"]["message"]

    # Honesty rule: no stray store was created.
    assert not bad_store.exists()
    assert blocker.is_file()


# --- End-to-end subprocess (the EXACT registered command form) ----------------


def _serve_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    # Store-less scenario: strip the suite-wide store env (conftest sets it) and
    # both accepted names, and point HOME at a fresh dir with no global store.
    env.pop("AGENT_CHRONICLE_STORE_DIR", None)
    env.pop("AGENT_SENTINEL_STORE_DIR", None)
    env["HOME"] = str(home)
    return env


def _run_serve(args: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", "from agent_chronicle.cli import app; app()", *args],
        input=_request_bytes(),
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )


def test_storeless_mcp_serve_stays_connected_end_to_end(tmp_path: Path) -> None:
    """Launch the EXACT store-less registered form (`agentacct mcp serve`) from a
    store-less cwd and assert the process does NOT exit non-zero: it answers the
    handshake and returns a clear JSON-RPC error on a record tool call."""
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "storeless"
    cwd.mkdir()

    proc = _run_serve(["mcp", "serve"], cwd=cwd, env=_serve_env(home))

    # Never looks dead to the host.
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")

    responses = _parse_responses(proc.stdout)
    assert len(responses) == 3
    init, tools, call = responses
    assert init["result"]["serverInfo"]["name"] == "agent-chronicle"
    assert isinstance(tools["result"]["tools"], list) and tools["result"]["tools"]
    assert call["error"]["code"] == -32000
    assert "no store configured" in call["error"]["message"]

    # Stdout carries only JSON-RPC frames — no banner/log before the first one.
    assert proc.stdout[:1] == b"{"

    # The human-facing diagnostic goes to stderr, not stdout.
    stderr = proc.stderr.decode("utf-8", "replace")
    assert "No agentacct store found" in stderr
    assert "degraded" in stderr.lower()

    # Honesty rule: the store-less server created nothing.
    assert not (cwd / ".agent-sentinel").exists()
    assert not (home / ".agent-sentinel").exists()


def test_unwritable_store_dir_mcp_serve_does_not_crash_end_to_end(tmp_path: Path) -> None:
    """An unwritable absolute --store-dir must surface as a JSON-RPC error, not
    an uncaught startup exception (which would exit non-zero with a traceback)."""
    home = tmp_path / "home"
    home.mkdir()
    blocker = tmp_path / "afile"
    blocker.write_text("not a directory", encoding="utf-8")
    bad_store = blocker / "state"

    proc = _run_serve(["mcp", "serve", "--store-dir", str(bad_store)], cwd=tmp_path, env=_serve_env(home))

    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    responses = _parse_responses(proc.stdout)
    assert len(responses) == 3
    init, _tools, call = responses
    assert init["result"]["serverInfo"]["name"] == "agent-chronicle"
    assert call["error"]["code"] == -32000
    assert "not usable" in call["error"]["message"]
    assert not bad_store.exists()
