"""OpenCode observe-only tool-activity plugin: the capture subcommand, the plugin
renderer, and the installer.

OpenCode auto-loads named-export plugins from ``<config>/plugins/*.js``. Its
``tool.execute.before`` hook carries the tool name and the OpenCode ``session_id``
(``ses_...``), which is the ``session`` table id the usage importer keys on — so a
tick attributes straight to the OpenCode session with no log pairing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentacct.cli import _install_opencode_plugin, _onboard_global_opencode, app
from agentacct.hooks import (
    OPENCODE_PLUGIN_RELATIVE_PATH,
    capture_opencode_tool_check,
    capture_tool_activity,
    render_opencode_plugin,
)
from agentacct.mechanical_capture import drain_mechanical_check_spool
from agentacct.tool_activity import drain_tool_activity_spool


def _tool_event(session_id: str, tool: str) -> str:
    return json.dumps({"tool_name": tool, "session_id": session_id})


# ---------------------------------------------------------------------------
# capture subcommand
# ---------------------------------------------------------------------------


def test_capture_tool_activity_labels_opencode_client(tmp_path: Path) -> None:
    capture_tool_activity(_tool_event("ses_0097fe8daffeJ2Recg87jKR2rc", "bash"), store_dir=tmp_path, client="opencode")
    events = drain_tool_activity_spool(tmp_path)
    assert len(events) == 1
    meta = events[0]["metadata"]
    assert events[0]["source"] == "opencode"
    assert meta["client_session_id"] == "ses_0097fe8daffeJ2Recg87jKR2rc"
    assert {"name": "bash", "count": 1} in meta["tool_names"]


def test_tool_activity_subcommand_spools_tick(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["hooks", "opencode", "tool-activity", "--store-dir", str(tmp_path)],
        input=_tool_event("ses_abc", "write"),
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "{}"
    events = drain_tool_activity_spool(tmp_path)
    assert len(events) == 1
    assert events[0]["source"] == "opencode"
    assert events[0]["metadata"]["client_session_id"] == "ses_abc"


def test_tool_activity_subcommand_fails_open_on_garbage(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["hooks", "opencode", "tool-activity", "--store-dir", str(tmp_path)],
        input="}{not json",
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "{}"
    assert drain_tool_activity_spool(tmp_path) == []


# ---------------------------------------------------------------------------
# plugin renderer
# ---------------------------------------------------------------------------


def test_render_opencode_plugin_shape() -> None:
    src = render_opencode_plugin("/abs/agentacct", store_dir="/store/x")
    # named export (OpenCode requires a named-export plugin, not a default export)
    assert "export const AgentacctToolActivity" in src
    assert "export default" not in src
    # the tool-activity hook and the two fields it forwards
    assert '"tool.execute.before"' in src
    assert "input.tool" in src and "input.sessionID" in src
    # the agentacct executable + store are bound at install time
    assert "/abs/agentacct" in src
    assert "/store/x" in src
    assert '"hooks", "opencode", subcommand' in src  # invokes `agentacct hooks opencode <sub>`
    assert 'spawnCapture("tool-activity"' in src
    # fire-and-forget: it must not await the spawned CLI (never add tool-call latency)
    assert "await Bun.spawn" not in src
    assert "Bun.spawn" in src


def test_render_opencode_plugin_no_store_binding() -> None:
    src = render_opencode_plugin("/abs/agentacct", store_dir=None)
    assert "const STORE_DIR = null;" in src


def test_render_opencode_plugin_escapes_paths_with_spaces() -> None:
    src = render_opencode_plugin("/a b/agentacct", store_dir="/x y/store")
    # JSON-encoded, so a path with spaces round-trips as a valid JS string literal
    assert '"/a b/agentacct"' in src
    assert '"/x y/store"' in src


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available to syntax-check the generated plugin")
def test_generated_plugin_is_valid_javascript(tmp_path: Path) -> None:
    plugin = tmp_path / "agentacct.mjs"
    plugin.write_text(render_opencode_plugin("/abs/agentacct", store_dir="/store/x"), encoding="utf-8")
    result = subprocess.run(["node", "--check", str(plugin)], capture_output=True, text=True)
    assert result.returncode == 0, f"generated plugin is not valid JS:\n{result.stderr}"


# ---------------------------------------------------------------------------
# installer
# ---------------------------------------------------------------------------


def test_install_opencode_plugin_writes_and_is_idempotent(tmp_path: Path) -> None:
    config_dir = tmp_path / "opencode"
    store = tmp_path / "store"
    action, plugin_path = _install_opencode_plugin(config_dir, store, "/abs/agentacct")
    assert action == "wrote"
    assert plugin_path == config_dir / OPENCODE_PLUGIN_RELATIVE_PATH
    assert plugin_path.exists()
    assert plugin_path.parent.name == "plugins"
    body = plugin_path.read_text(encoding="utf-8")
    assert str(store) in body

    # second run with the same inputs is a byte-identical no-op
    before = plugin_path.read_bytes()
    action2, _ = _install_opencode_plugin(config_dir, store, "/abs/agentacct")
    assert action2 == "unchanged"
    assert plugin_path.read_bytes() == before


def test_install_opencode_plugin_updates_on_change(tmp_path: Path) -> None:
    config_dir = tmp_path / "opencode"
    store = tmp_path / "store"
    _install_opencode_plugin(config_dir, store, "/abs/agentacct")
    action, plugin_path = _install_opencode_plugin(config_dir, tmp_path / "store2", "/abs/agentacct")
    assert action == "updated"
    assert str(tmp_path / "store2") in plugin_path.read_text(encoding="utf-8")


def test_install_opencode_plugin_overwrites_non_utf8_file(tmp_path: Path) -> None:
    # A pre-existing non-UTF-8 agentacct.js must not crash the idempotency check
    # (a bytes compare, not read_text) — it is simply overwritten.
    config_dir = tmp_path / "opencode"
    plugin_path = config_dir / OPENCODE_PLUGIN_RELATIVE_PATH
    plugin_path.parent.mkdir(parents=True)
    plugin_path.write_bytes(b"\xff\xfe not utf-8 \x80\x81")
    action, _ = _install_opencode_plugin(config_dir, tmp_path / "store", "/abs/agentacct")
    assert action == "updated"
    body = plugin_path.read_text(encoding="utf-8")  # now valid UTF-8
    assert "AgentacctToolActivity" in body


def test_render_opencode_plugin_survives_sentinel_in_path(tmp_path: Path) -> None:
    # An agentacct path containing the literal template sentinel must not corrupt
    # the candidates array (single-pass substitution).
    weird = "/home/u/__STORE_DIR__/bin/agentacct"
    src = render_opencode_plugin(weird, store_dir="/store/x")
    assert json.dumps(weird) in src  # the literal path survives intact
    assert 'const STORE_DIR = "/store/x";' in src
    if shutil.which("node"):
        plugin = tmp_path / "p.mjs"
        plugin.write_text(src, encoding="utf-8")
        result = subprocess.run(["node", "--check", str(plugin)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_onboard_installs_plugin_even_when_mcp_config_is_jsonc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A JSONC config with comments makes the MCP write skip; the observe-only plugin
    # must still be installed (it is independent of that JSON file).
    xdg = tmp_path / "xdg"
    (xdg / "opencode").mkdir(parents=True)
    (xdg / "opencode" / "opencode.jsonc").write_text(
        '{\n  // a comment makes this un-writable strict JSON\n  "mcp": {}\n}\n', encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    ready = _onboard_global_opencode(tmp_path / "store", "/abs/agentacct")
    # MCP couldn't be rewritten (JSONC) -> not fully ready ...
    assert ready is False
    # ... but the tool-activity plugin WAS installed anyway.
    plugin_path = xdg / "opencode" / OPENCODE_PLUGIN_RELATIVE_PATH
    assert plugin_path.exists()
    assert "AgentacctToolActivity" in plugin_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# #3 exit-code mechanical check (tool.execute.after)
# ---------------------------------------------------------------------------


def _after(command, exit_code, *, tool="bash", session="ses_01") -> str:
    return json.dumps({"tool_name": tool, "session_id": session, "command": command, "exit_code": exit_code})


def _drain_checks(tmp_path: Path):
    return drain_mechanical_check_spool(tmp_path)


def test_opencode_tool_check_records_recognized_check(tmp_path: Path) -> None:
    capture_opencode_tool_check(_after("pytest -q", 0), store_dir=tmp_path)
    envs = _drain_checks(tmp_path)
    assert len(envs) == 1
    env = envs[0]
    assert env.source_system == "opencode"
    # Provenance names the opencode wire, not Claude Code's PostToolUse.
    assert env.source_schema == "opencode_tool_execute_after.v1"
    assert env.adapter == "opencode_tool_execute_after"
    attrs = env.payload["attributes"]
    assert attrs["check_kind"] == "test" and attrs["runner"] == "pytest" and attrs["exit_code"] == 0


def test_opencode_tool_check_records_failing_exit(tmp_path: Path) -> None:
    capture_opencode_tool_check(_after("npm run build", 2), store_dir=tmp_path)
    env = _drain_checks(tmp_path)[0]
    assert env.payload["attributes"]["exit_code"] == 2
    assert env.payload["attributes"]["passed"] is False


def test_opencode_tool_check_attributes_input_session(tmp_path: Path) -> None:
    capture_opencode_tool_check(_after("pytest -q", 0, session="ses_abc123"), store_dir=tmp_path)
    env = _drain_checks(tmp_path)[0]
    assert env.source_instance == "ses_abc123"


def test_opencode_tool_check_skips_non_check_command(tmp_path: Path) -> None:
    capture_opencode_tool_check(_after("echo hi", 0), store_dir=tmp_path)
    assert _drain_checks(tmp_path) == []


def test_opencode_tool_check_skips_non_bash_tool(tmp_path: Path) -> None:
    capture_opencode_tool_check(_after("pytest -q", 0, tool="read"), store_dir=tmp_path)
    assert _drain_checks(tmp_path) == []


def test_opencode_tool_check_skips_missing_or_nonint_exit(tmp_path: Path) -> None:
    capture_opencode_tool_check(json.dumps({"tool_name": "bash", "session_id": "ses_01", "command": "pytest -q"}), store_dir=tmp_path)
    capture_opencode_tool_check(_after("pytest -q", True), store_dir=tmp_path)  # bool is not a valid exit code
    capture_opencode_tool_check(_after("pytest -q", "0"), store_dir=tmp_path)  # string is not an int
    assert _drain_checks(tmp_path) == []


def test_opencode_tool_check_overlong_session_and_garbage(tmp_path: Path) -> None:
    capture_opencode_tool_check(_after("pytest -q", 0, session="x" * 5000), store_dir=tmp_path)
    capture_opencode_tool_check("not json", store_dir=tmp_path)
    capture_opencode_tool_check(json.dumps([1, 2]), store_dir=tmp_path)
    assert _drain_checks(tmp_path) == []


def test_opencode_plugin_has_after_hook_and_is_valid_js(tmp_path: Path) -> None:
    src = render_opencode_plugin("/abs/agentacct", store_dir=str(tmp_path / "store"))
    assert "tool.execute.after" in src
    assert "tool-check" in src and "spawnCapture" in src
    assert 'input.tool !== "bash"' in src  # gated on the shell tool
    if shutil.which("node"):
        plugin = tmp_path / "p.mjs"
        plugin.write_text(src, encoding="utf-8")
        result = subprocess.run(["node", "--check", str(plugin)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_tool_check_subcommand_spools_check(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["hooks", "opencode", "tool-check", "--store-dir", str(tmp_path)], input=_after("pytest -q", 0)
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "{}"
    assert len(drain_mechanical_check_spool(tmp_path)) == 1


def test_tool_check_subcommand_fail_open_on_garbage(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["hooks", "opencode", "tool-check", "--store-dir", str(tmp_path)], input="}{not json"
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "{}"


def test_before_and_after_hooks_survive_null_input(tmp_path: Path) -> None:
    # A null/undefined `input` from OpenCode must not make a hook reject (observe-only
    # must never throw into OpenCode's dispatch). The before-hook guards input the same
    # way the after-hook does.
    src = render_opencode_plugin("/abs/agentacct", store_dir=None)
    assert "if (!input) return;" in src  # before-hook null guard
    if shutil.which("node") is None:
        pytest.skip("node not available to run the plugin hooks")
    plugin = tmp_path / "p.mjs"
    plugin.write_text(src, encoding="utf-8")
    harness = tmp_path / "h.mjs"
    harness.write_text(
        "import { AgentacctToolActivity } from './p.mjs';\n"
        "const h = await AgentacctToolActivity();\n"
        "await h['tool.execute.before'](null);\n"
        "await h['tool.execute.before'](undefined);\n"
        "await h['tool.execute.after'](null, null);\n"
        "await h['tool.execute.after'](undefined, undefined);\n"
        "console.log('NO_THROW');\n",
        encoding="utf-8",
    )
    result = subprocess.run(["node", str(harness)], capture_output=True, text=True, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "NO_THROW" in result.stdout
