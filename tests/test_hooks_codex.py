"""Codex observe-only hooks: client-parameterized capture, the hooks.json
writer, the wrapper, and the CLI subcommands.

Codex's PreToolUse/SessionEnd hook STDIN uses the SAME snake_case shape as
Claude Code (hook_event_name/session_id/tool_name), and its session_id equals
the rollout id the usage importer keys on — verified on a real Codex 0.146
build — so the tool ticks and session-end facts attribute straight to the Codex
session with no log pairing.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from typer.testing import CliRunner

from agentacct import cli
from agentacct.cli import app
from agentacct.hooks import (
    CODEX_HOOK_RELATIVE_PATH,
    CODEX_HOOKS_JSON_RELATIVE_PATH,
    capture_session_end,
    capture_tool_activity,
    codex_hooks_json_block,
    render_codex_hook_wrapper,
)
from agentacct.session_lifecycle import drain_session_end_spool
from agentacct.tool_activity import drain_tool_activity_spool


def _codex_pre_tool_event(session_id: str, tool_name: str) -> str:
    return json.dumps({"hook_event_name": "PreToolUse", "session_id": session_id, "tool_name": tool_name, "cwd": "/x"})


def test_capture_tool_activity_labels_codex_client(tmp_path: Path) -> None:
    capture_tool_activity(_codex_pre_tool_event("019ff6ee-a", "Bash"), store_dir=tmp_path, client="codex")
    capture_tool_activity(
        _codex_pre_tool_event("019ff6ee-a", "mcp__agentacct__agentacct_record_section"),
        store_dir=tmp_path, client="codex",
    )
    [event] = drain_tool_activity_spool(tmp_path, now=100.0, token="t")
    md = event["metadata"]
    assert md["client"] == "codex"
    assert md["client_session_id"] == "019ff6ee-a"
    assert {row["name"]: row["count"] for row in md["tool_names"]} == {
        "Bash": 1,
        "mcp__agentacct__agentacct_record_section": 1,
    }
    assert md["tool_category_counts"] == {"execute": 1, "mcp": 1}


def test_capture_session_end_labels_codex_client(tmp_path: Path) -> None:
    capture_session_end(
        json.dumps({"hook_event_name": "SessionEnd", "session_id": "019ff6ee-a", "reason": "other"}),
        store_dir=tmp_path, client="codex",
    )
    [event] = drain_session_end_spool(tmp_path, now=100.0, token="t")
    assert event["source"] == "codex"
    assert event["metadata"]["client"] == "codex"
    assert event["metadata"]["client_session_id"] == "019ff6ee-a"


def test_codex_wrapper_renders_valid_python_shelling_to_hooks_codex(tmp_path: Path) -> None:
    src = render_codex_hook_wrapper("/abs/agentacct", store_dir="/global/store")
    ast.parse(src)  # must be valid python
    assert '"hooks", "codex"' in src or "'hooks', 'codex'" in src
    assert "/abs/agentacct" in src
    assert "/global/store" in src  # store bound on the command line
    # Never makes an allow/block decision for codex: fail-open is an empty {}.
    assert "sys.stdout.write" in src


def test_codex_hooks_json_writer_fresh_idempotent_and_merge(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    store = tmp_path / "store"
    action, wrapper = cli._install_codex_hook(home, store, "/abs/agentacct")
    assert action == "wrote"
    assert wrapper.exists()
    assert (wrapper.stat().st_mode & 0o777) == 0o755
    hooks = json.loads((home / CODEX_HOOKS_JSON_RELATIVE_PATH).read_text())
    assert set(hooks["hooks"].keys()) == {"PreToolUse", "SessionEnd"}
    assert CODEX_HOOK_RELATIVE_PATH.name in hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

    before = (home / CODEX_HOOKS_JSON_RELATIVE_PATH).read_text()
    action2, _ = cli._install_codex_hook(home, store, "/abs/agentacct")
    assert action2 == "unchanged"
    assert (home / CODEX_HOOKS_JSON_RELATIVE_PATH).read_text() == before  # byte-identical


def test_codex_hooks_json_writer_preserves_user_hook(tmp_path: Path) -> None:
    home = tmp_path / "home"
    hj = home / CODEX_HOOKS_JSON_RELATIVE_PATH
    hj.parent.mkdir(parents=True)
    hj.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/bin/user-hook"}]}
    ]}}))
    cli._install_codex_hook(home, tmp_path / "store", "/abs/agentacct")
    merged = json.loads(hj.read_text())
    commands = [h["command"] for row in merged["hooks"]["PreToolUse"] for h in row["hooks"]]
    assert "/usr/bin/user-hook" in commands  # user's own hook survives
    assert any(CODEX_HOOK_RELATIVE_PATH.name in c for c in commands)  # ours added


def test_codex_hooks_json_writer_refuses_non_object(tmp_path: Path) -> None:
    hj = tmp_path / "hooks.json"
    hj.write_text("[]")
    _path, action = cli._write_codex_hooks_json_at(
        hj, codex_hooks_json_block("/w/wrap.py"), wrapper_basename="wrap.py"
    )
    assert action == "skipped-unparsed"
    assert hj.read_text() == "[]"  # never clobbered


def test_codex_pre_tool_use_subcommand_spools_codex_tick(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["hooks", "codex", "pre-tool-use", "--store-dir", str(tmp_path)],
        input=_codex_pre_tool_event("019ff6ee-b", "Bash"),
    )
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "{}"  # observe-only no-op response
    [event] = drain_tool_activity_spool(tmp_path, now=100.0, token="t")
    assert event["metadata"]["client"] == "codex"
    assert event["metadata"]["client_session_id"] == "019ff6ee-b"


# --- Regression tests for adversarial-review findings --------------------------


def test_codex_hooks_json_preserves_co_located_user_command(tmp_path: Path) -> None:
    # FINDING #1: a user command co-located in the SAME matcher row as ours must
    # survive re-install; ours must not accrue a duplicate.
    home = tmp_path / "home"
    hj = home / CODEX_HOOKS_JSON_RELATIVE_PATH
    hj.parent.mkdir(parents=True)
    wrapper_cmd = f"python3 {home / CODEX_HOOK_RELATIVE_PATH}"
    hj.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "*", "hooks": [
            {"type": "command", "command": wrapper_cmd},
            {"type": "command", "command": "/usr/bin/user-audit"},
        ]}
    ]}}))
    cli._install_codex_hook(home, tmp_path / "store", str(home / "bin/agentacct"))
    merged = json.loads(hj.read_text())
    commands = [h["command"] for row in merged["hooks"]["PreToolUse"] for h in row["hooks"]]
    assert "/usr/bin/user-audit" in commands  # co-located user command survives
    assert sum(CODEX_HOOK_RELATIVE_PATH.name in c for c in commands) == 1  # no duplicate

    before = hj.read_text()
    cli._install_codex_hook(home, tmp_path / "store", str(home / "bin/agentacct"))
    assert hj.read_text() == before  # idempotent on the co-located shape


def test_codex_hooks_install_command_fails_honestly_on_non_object(tmp_path: Path) -> None:
    # FINDING #2: the standalone install must NOT print "trust it and it fires"
    # when the writer skipped a non-object hooks.json.
    home = tmp_path / "home"
    hj = home / CODEX_HOOKS_JSON_RELATIVE_PATH
    hj.parent.mkdir(parents=True)
    hj.write_text("[]")
    result = CliRunner().invoke(
        app, ["hooks", "codex", "install", "--home", str(home), "--store-dir", str(tmp_path / "store")]
    )
    assert result.exit_code == 1
    # Normalize: the rich console soft-wraps at ~80 cols with no TTY (CI), which
    # would split these multi-word phrases across a newline.
    normalized = " ".join(result.output.split())
    assert "LEFT UNCHANGED" in normalized
    assert "No hook was wired" in normalized
    assert "trust" not in normalized.lower()  # no false "approve it" line
    assert hj.read_text() == "[]"  # never clobbered


def test_codex_wrapper_bounds_subprocess_with_timeout() -> None:
    # FINDING #4: a hung CLI must not block a Codex tool call forever.
    src = render_codex_hook_wrapper("/abs/agentacct", store_dir="/store")
    assert "timeout=5" in src
    assert "TimeoutExpired" in src
