"""Default-global onboard: install once, machine-wide, ZERO files in the repo.

HOME is redirected to a tmp dir so this never touches the real ~/.claude*,
~/.codex, or ~/.local/state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_chronicle.cli import app


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


def test_onboard_global_writes_zero_repo_files_and_configures_user_scope(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(app, ["onboard", "--scope", "global", "--yes", "--no-start"])
    assert result.exit_code == 0, result.output

    # 1. The repo we ran from is untouched — the whole point of default-global.
    for leaked in (".agent-sentinel", ".mcp.json", ".codex", ".claude", "CLAUDE.md", "AGENTS.md"):
        assert not (repo / leaked).exists(), f"{leaked} leaked into the repo"

    # 2. Global store created at the XDG-shaped canonical location under HOME.
    store = isolated_home / ".local" / "state" / "agentacct" / "state"
    assert store.is_dir()

    # 3. User-level MCP registration for both native clients.
    claude_json = json.loads((isolated_home / ".claude.json").read_text())
    server = claude_json["mcpServers"]["agentacct"]
    assert server["type"] == "stdio"
    assert str(store) in server["args"]
    # GUI clients don't inherit shell PATH -> the command must be absolute.
    assert Path(server["command"]).is_absolute(), server["command"]
    assert "[mcp_servers.agentacct]" in (isolated_home / ".codex" / "config.toml").read_text()

    # 4. Standing instructions + hook wrapper OUTSIDE the store.
    assert (isolated_home / ".claude" / "CLAUDE.md").exists()
    assert (isolated_home / ".codex" / "AGENTS.md").exists()
    hook_wrapper = isolated_home / ".claude" / "hooks" / "claude_pre_tool_use.py"
    assert hook_wrapper.exists()
    assert store not in hook_wrapper.parents

    # 5. --yes merged the hook + the load-bearing env into user settings.json.
    settings = json.loads((isolated_home / ".claude" / "settings.json").read_text())
    assert "PreToolUse" in settings.get("hooks", {})
    assert settings.get("env", {}).get("ENABLE_TOOL_SEARCH") == "auto"


def test_onboard_global_without_yes_is_non_interactive_safe(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --yes and no TTY, the run must NOT block or touch settings.json,
    but must still register MCP + instructions (the recording primitives)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(app, ["onboard", "--scope", "global", "--no-start"])
    assert result.exit_code == 0, result.output

    # settings.json is user-owned: never written without explicit consent.
    assert not (isolated_home / ".claude" / "settings.json").exists()
    # but MCP registration still happened
    claude_json = json.loads((isolated_home / ".claude.json").read_text())
    assert "agentacct" in claude_json["mcpServers"]
