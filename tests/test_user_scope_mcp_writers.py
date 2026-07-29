"""Unit tests for the native user-scope MCP writers used by default-global onboard.

Claude Code's ``~/.claude.json`` is the client's own global state; the writer
must merge (preserve every other key) and use ``type: "stdio"``. Codex's
``~/.codex/config.toml`` is an upsert that preserves other servers.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentacct import cli


def test_user_claude_mcp_fresh_file(tmp_path: Path) -> None:
    config = tmp_path / ".claude.json"
    path, action = cli._write_user_claude_mcp_config(config, "/global/store", command="agentacct")

    assert path == config
    assert action == "wrote"
    data = json.loads(config.read_text())
    assert data["mcpServers"]["agentacct"] == {
        "type": "stdio",
        "command": "agentacct",
        "args": ["mcp", "serve", "--store-dir", "/global/store"],
    }
    assert (config.stat().st_mode & 0o777) == 0o600


def test_user_claude_mcp_preserves_other_keys_and_extra_entry_keys(tmp_path: Path) -> None:
    config = tmp_path / ".claude.json"
    config.write_text(
        json.dumps(
            {
                "userID": "abc",
                "projects": {"/x": {}},
                "mcpServers": {
                    "gbrain": {"type": "stdio", "command": "g", "args": ["serve"]},
                    "agentacct": {
                        "type": "stdio",
                        "command": "old",
                        "args": ["mcp", "serve", "--store-dir", "/old"],
                        "alwaysLoad": True,
                    },
                },
            }
        )
    )
    _path, action = cli._write_user_claude_mcp_config(config, "/new/store")

    assert action == "updated"
    data = json.loads(config.read_text())
    # Claude Code's own top-level state is untouched
    assert data["userID"] == "abc"
    assert data["projects"] == {"/x": {}}
    # other MCP servers untouched
    assert data["mcpServers"]["gbrain"]["command"] == "g"
    entry = data["mcpServers"]["agentacct"]
    assert entry["args"][-1] == "/new/store"  # store path updated
    assert entry["alwaysLoad"] is True  # extra entry key preserved
    assert entry["type"] == "stdio"


def test_user_claude_mcp_migrates_prior_own_name_and_carries_env(tmp_path: Path) -> None:
    config = tmp_path / ".claude.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "agent-chronicle": {
                        "command": "agentacct",
                        "args": ["mcp", "serve", "--store-dir", "/old"],
                        "env": {"X": "1"},
                    }
                }
            }
        )
    )
    _path, _action = cli._write_user_claude_mcp_config(config, "/new")

    data = json.loads(config.read_text())
    assert "agent-chronicle" not in data["mcpServers"]  # collapsed
    entry = data["mcpServers"]["agentacct"]
    assert entry["env"] == {"X": "1"}  # env carried forward
    assert entry["args"][-1] == "/new"


def test_user_claude_mcp_leaves_custom_pre_rename_sentinel(tmp_path: Path) -> None:
    config = tmp_path / ".claude.json"
    config.write_text(
        json.dumps(
            {"mcpServers": {"agent-sentinel": {"type": "stdio", "command": "custom", "args": ["weird"]}}}
        )
    )
    _path, _action = cli._write_user_claude_mcp_config(config, "/new")

    data = json.loads(config.read_text())
    # user's custom pre-rename registration is never clobbered
    assert data["mcpServers"]["agent-sentinel"]["command"] == "custom"
    # but the new-name server is still registered
    assert data["mcpServers"]["agentacct"]["args"][-1] == "/new"


def test_user_claude_mcp_rejects_non_object(tmp_path: Path) -> None:
    config = tmp_path / ".claude.json"
    config.write_text("[]")
    import pytest

    with pytest.raises(Exception):
        cli._write_user_claude_mcp_config(config, "/new")


def test_user_codex_mcp_fresh_file(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    path, _action = cli._write_codex_mcp_config_at(config, "/global/store")

    text = config.read_text()
    assert path == config
    assert "[mcp_servers.agentacct]" in text
    assert '"--store-dir"' in text
    assert "/global/store" in text


def test_user_codex_mcp_preserves_other_servers(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[mcp_servers.other]\ncommand = "x"\nargs = ["y"]\n')
    cli._write_codex_mcp_config_at(config, "/global/store")

    text = config.read_text()
    assert "[mcp_servers.other]" in text  # untouched
    assert "[mcp_servers.agentacct]" in text
