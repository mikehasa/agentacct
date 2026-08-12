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


# --- OpenCode (opencode.jsonc) -------------------------------------------------


def test_opencode_mcp_fresh_file(tmp_path: Path) -> None:
    config = tmp_path / "opencode.jsonc"
    path, action = cli._write_opencode_mcp_config_at(config, "/global/store", command="/bin/agentacct")

    assert path == config
    assert action == "wrote"
    data = json.loads(config.read_text())
    assert data["mcp"]["agentacct"] == {
        "type": "local",
        "command": ["/bin/agentacct", "mcp", "serve", "--store-dir", "/global/store"],
        "enabled": True,
    }
    assert data["$schema"] == "https://opencode.ai/config.json"


def test_opencode_mcp_idempotent_no_write(tmp_path: Path) -> None:
    config = tmp_path / "opencode.jsonc"
    cli._write_opencode_mcp_config_at(config, "/store", command="/bin/agentacct")
    before = config.read_text()
    _path, action = cli._write_opencode_mcp_config_at(config, "/store", command="/bin/agentacct")
    assert action == "unchanged"
    assert config.read_text() == before  # byte-identical, no rewrite


def test_opencode_mcp_preserves_other_keys_and_servers(tmp_path: Path) -> None:
    config = tmp_path / "opencode.jsonc"
    config.write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "model": "anthropic/claude",
                "mcp": {
                    "other": {"type": "local", "command": ["x"], "enabled": True},
                    "agentacct": {
                        "type": "local",
                        "command": ["/bin/agentacct", "mcp", "serve", "--store-dir", "/old"],
                        "enabled": True,
                    },
                },
            },
            indent=2,
        )
        + "\n"
    )
    _path, action = cli._write_opencode_mcp_config_at(config, "/new/store", command="/bin/agentacct")

    assert action == "updated"
    data = json.loads(config.read_text())
    assert data["model"] == "anthropic/claude"  # unrelated key preserved
    assert data["mcp"]["other"]["command"] == ["x"]  # other server untouched
    assert data["mcp"]["agentacct"]["command"][-1] == "/new/store"  # store updated


def test_opencode_mcp_refuses_to_rewrite_jsonc_with_comments(tmp_path: Path) -> None:
    config = tmp_path / "opencode.jsonc"
    original = '{\n  // user comment\n  "mcp": {}\n}\n'
    config.write_text(original)
    _path, action = cli._write_opencode_mcp_config_at(config, "/store", command="/bin/agentacct")

    assert action == "skipped-unparsed"
    assert config.read_text() == original  # never clobbered


def test_opencode_mcp_collapses_prior_own_name(tmp_path: Path) -> None:
    config = tmp_path / "opencode.jsonc"
    config.write_text(
        json.dumps(
            {
                "mcp": {
                    "agent-chronicle": {
                        "type": "local",
                        "command": ["/bin/agent-chronicle", "mcp", "serve", "--store-dir", "/old"],
                        "enabled": True,
                    }
                }
            }
        )
    )
    _path, _action = cli._write_opencode_mcp_config_at(config, "/new", command="/bin/agentacct")

    data = json.loads(config.read_text())
    assert "agent-chronicle" not in data["mcp"]  # collapsed into new name
    assert data["mcp"]["agentacct"]["command"][-1] == "/new"


def test_opencode_mcp_leaves_custom_same_named_server(tmp_path: Path) -> None:
    # A pre-rename-named server whose argv is NOT an agentacct install (it is the
    # user's own tool) must never be dropped.
    config = tmp_path / "opencode.jsonc"
    config.write_text(
        json.dumps(
            {"mcp": {"agent-sentinel": {"type": "local", "command": ["mytool"], "enabled": True}}}
        )
    )
    _path, _action = cli._write_opencode_mcp_config_at(config, "/new", command="/bin/agentacct")

    data = json.loads(config.read_text())
    assert data["mcp"]["agent-sentinel"]["command"] == ["mytool"]  # untouched
    assert data["mcp"]["agentacct"]["command"][-1] == "/new"


# --- Hermes (config.yaml) ------------------------------------------------------


def test_hermes_mcp_appends_block_when_absent_preserving_comments(tmp_path: Path) -> None:
    import yaml

    config = tmp_path / "config.yaml"
    config.write_text(
        "# user config\n"
        "model:\n"
        "  provider: auto  # keep me\n"
        "hooks:\n"
        "  post_llm_call:\n"
        "  - command: /x/lark.py\n"
        "    timeout: 30\n"
    )
    path, action = cli._write_hermes_mcp_config_at(config, "/global/store", command="/bin/agentacct")

    assert path == config
    assert action == "wrote"
    text = config.read_text()
    assert "# keep me" in text  # comment preserved
    assert "/x/lark.py" in text  # existing hooks preserved
    data = yaml.safe_load(text)
    assert data["mcp_servers"]["agentacct"]["args"] == ["mcp", "serve", "--store-dir", "/global/store"]
    assert data["mcp_servers"]["agentacct"]["command"] == "/bin/agentacct"


def test_hermes_mcp_idempotent_no_write(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("model:\n  provider: auto\n")
    cli._write_hermes_mcp_config_at(config, "/store", command="/bin/agentacct")
    before = config.read_text()
    _path, action = cli._write_hermes_mcp_config_at(config, "/store", command="/bin/agentacct")
    assert action == "unchanged"
    assert config.read_text() == before


def test_hermes_mcp_updates_only_agentacct_child_keeping_siblings(tmp_path: Path) -> None:
    import yaml

    config = tmp_path / "config.yaml"
    config.write_text(
        "mcp_servers:\n"
        "  linear:  # existing server\n"
        "    command: linear-mcp\n"
        "    args:\n"
        "    - serve\n"
        "after_top: 1\n"
    )
    _path, action = cli._write_hermes_mcp_config_at(config, "/new/store", command="/bin/agentacct")

    assert action == "updated"
    text = config.read_text()
    assert "# existing server" in text  # sibling comment preserved
    data = yaml.safe_load(text)
    assert data["mcp_servers"]["linear"]["command"] == "linear-mcp"  # sibling kept
    assert data["mcp_servers"]["agentacct"]["args"][-1] == "/new/store"  # child inserted
    assert data["after_top"] == 1  # following top-level key preserved


def test_hermes_mcp_replaces_stale_agentacct_child(tmp_path: Path) -> None:
    import yaml

    config = tmp_path / "config.yaml"
    config.write_text(
        "mcp_servers:\n"
        "  agentacct:\n"
        "    command: old\n"
        "    args:\n"
        "    - mcp\n"
        "    - serve\n"
        "    - --store-dir\n"
        "    - /old\n"
        "trailing: keep\n"
    )
    _path, action = cli._write_hermes_mcp_config_at(config, "/new", command="/bin/agentacct")

    assert action == "updated"
    data = yaml.safe_load(config.read_text())
    assert data["mcp_servers"]["agentacct"]["command"] == "/bin/agentacct"
    assert data["mcp_servers"]["agentacct"]["args"][-1] == "/new"
    assert data["trailing"] == "keep"  # key after the block preserved


def test_hermes_mcp_skips_inline_flow_mapping(tmp_path: Path) -> None:
    # An inline/flow mcp_servers mapping cannot be edited safely by line surgery.
    config = tmp_path / "config.yaml"
    original = "mcp_servers: {}\nother: 1\n"
    config.write_text(original)
    _path, action = cli._write_hermes_mcp_config_at(config, "/store", command="/bin/agentacct")

    assert action == "skipped-unparsed"
    assert config.read_text() == original  # never clobbered


def test_hermes_mcp_paths_with_spaces_are_quoted(tmp_path: Path) -> None:
    import yaml

    config = tmp_path / "config.yaml"
    config.write_text("model:\n  provider: auto\n")
    store = "/Users/My Name/.local/state/agentacct/state"
    cli._write_hermes_mcp_config_at(config, store, command="/bin/agentacct")
    data = yaml.safe_load(config.read_text())  # must still parse
    assert data["mcp_servers"]["agentacct"]["args"][-1] == store


# --- Regression tests for adversarial-review findings --------------------------


def test_opencode_config_dir_honors_xdg_config_home(monkeypatch, tmp_path: Path) -> None:
    # FINDING #1: OpenCode is XDG-compliant; with XDG_CONFIG_HOME set it reads
    # $XDG_CONFIG_HOME/opencode, NOT ~/.config/opencode. Writing to the wrong dir
    # captures nothing while onboarding reports success.
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert cli._opencode_config_dir() == xdg / "opencode"
    assert cli._resolve_opencode_config_path() == xdg / "opencode" / "opencode.jsonc"
    assert cli._instruction_target_path("opencode", user=True, path=None) == xdg / "opencode" / "AGENTS.md"

    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert cli._opencode_config_dir() == tmp_path / "home" / ".config" / "opencode"


def test_opencode_drops_stale_old_name_even_when_agentacct_already_correct(tmp_path: Path) -> None:
    # FINDING #3: a correct agentacct entry alongside a stale agent-chronicle must
    # NOT short-circuit to "unchanged" — the dead ENOENT server has to be removed.
    config = tmp_path / "opencode.jsonc"
    good = {"type": "local", "command": ["/bin/agentacct", "mcp", "serve", "--store-dir", "/store"], "enabled": True}
    stale = {"type": "local", "command": ["/bin/agent-chronicle", "mcp", "serve", "--store-dir", "/store"], "enabled": True}
    config.write_text(json.dumps({"mcp": {"agentacct": good, "agent-chronicle": stale}}))
    _path, action = cli._write_opencode_mcp_config_at(config, "/store", command="/bin/agentacct")

    assert action == "updated"  # not "unchanged" — the write must happen
    data = json.loads(config.read_text())
    assert "agent-chronicle" not in data["mcp"]  # dead server dropped
    assert data["mcp"]["agentacct"]["command"][-1] == "/store"


def test_opencode_idempotent_with_custom_same_named_server(tmp_path: Path) -> None:
    # FINDING #5: a user's own (non-agentacct) server whose key is a pre-rename
    # name must not defeat idempotency — re-onboard is a no-op, not a rewrite.
    config = tmp_path / "opencode.jsonc"
    good = {"type": "local", "command": ["/bin/agentacct", "mcp", "serve", "--store-dir", "/store"], "enabled": True}
    custom = {"type": "local", "command": ["mytool"], "enabled": True}
    config.write_text(json.dumps({"mcp": {"agentacct": good, "agent-sentinel": custom}}, indent=2) + "\n")
    before = config.read_text()
    _path, action = cli._write_opencode_mcp_config_at(config, "/store", command="/bin/agentacct")

    assert action == "unchanged"
    assert config.read_text() == before  # byte-identical, custom server untouched


def test_opencode_non_object_mcp_reports_distinct_reason(tmp_path: Path) -> None:
    # FINDING #7: a strictly-valid JSON whose `mcp` is a non-object is not
    # "not strict JSON" — the skip reason must be distinct and accurate.
    config = tmp_path / "opencode.jsonc"
    config.write_text(json.dumps({"model": "x", "mcp": []}))
    _path, action = cli._write_opencode_mcp_config_at(config, "/store", command="/bin/agentacct")
    assert action == "skipped-mcp-not-object"


def test_hermes_update_preserves_trailing_comment_for_next_server(tmp_path: Path) -> None:
    # FINDING #2: a comment documenting the NEXT server (at sibling indent, after
    # the agentacct block) must not be swallowed by the replaced span.
    import yaml

    config = tmp_path / "config.yaml"
    config.write_text(
        "mcp_servers:\n"
        "  agentacct:\n"
        "    command: old\n"
        "    args:\n"
        "    - mcp\n"
        "    - serve\n"
        "    - --store-dir\n"
        "    - /old\n"
        "  # linear is a remote MCP server\n"
        "  linear:\n"
        "    command: linear-mcp\n"
    )
    _path, action = cli._write_hermes_mcp_config_at(config, "/new", command="/bin/agentacct")

    assert action == "updated"
    text = config.read_text()
    assert "# linear is a remote MCP server" in text  # trailing comment survives
    data = yaml.safe_load(text)
    assert data["mcp_servers"]["agentacct"]["args"][-1] == "/new"
    assert data["mcp_servers"]["linear"]["command"] == "linear-mcp"


def test_hermes_update_preserves_crlf_line_endings(tmp_path: Path) -> None:
    # FINDING #4: the writer promises never to reflow; a CRLF file must stay CRLF.
    config = tmp_path / "config.yaml"
    config.write_bytes(
        b"version: 1\r\n"
        b"mcp_servers:\r\n"
        b"  agentacct:\r\n"
        b"    command: old\r\n"
        b"    args:\r\n"
        b"    - mcp\r\n"
        b"    - serve\r\n"
        b"    - --store-dir\r\n"
        b"    - /old\r\n"
    )
    _path, action = cli._write_hermes_mcp_config_at(config, "/new", command="/bin/agentacct")

    assert action == "updated"
    raw = config.read_bytes()
    assert b"\r\n" in raw
    assert b"version: 1\r\n" in raw  # untouched line keeps its CR
    assert b"\n" not in raw.replace(b"\r\n", b"")  # no bare LF introduced


def test_hermes_refuses_tab_indented_config(tmp_path: Path) -> None:
    # FINDING #6: tab-indented children would push agentacct to column 0 (outside
    # mcp_servers). Refuse rather than emit a config Hermes silently ignores.
    config = tmp_path / "config.yaml"
    original = "mcp_servers:\n\tlinear:\n\t\tcommand: linear-mcp\n"
    config.write_text(original)
    _path, action = cli._write_hermes_mcp_config_at(config, "/store", command="/bin/agentacct")

    assert action == "skipped-unparsed"
    assert config.read_text() == original  # never clobbered
