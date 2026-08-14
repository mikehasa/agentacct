"""Default-global onboard: install once, machine-wide, ZERO files in the repo.

HOME is redirected to a tmp dir so this never touches the real ~/.claude*,
~/.codex, or ~/.local/state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentacct.cli import app


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


# --- upgrade path: an existing global store must not be silently abandoned ----
# The regression this covers: global onboard used to ALWAYS target the new
# canonical (XDG) store, so an upgrading user whose ledger lived in the
# pre-rename ~/.agent-sentinel-global got every client repointed at a new empty
# store — history apparently gone, and clients split across two ledgers.


def _seed_store(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "events.jsonl").write_text('{"event_id": "evt_seed"}\n', encoding="utf-8")
    return path


def test_onboard_global_reuses_an_existing_populated_legacy_store(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = _seed_store(isolated_home / ".agent-sentinel-global" / "state")
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(app, ["onboard", "--scope", "global", "--yes", "--no-start"])
    assert result.exit_code == 0, result.output

    # Every client registration points at the EXISTING ledger, not a new one.
    claude_json = json.loads((isolated_home / ".claude.json").read_text())
    assert str(legacy) in claude_json["mcpServers"]["agentacct"]["args"]
    assert str(legacy) in (isolated_home / ".codex" / "config.toml").read_text()
    # ...and the empty canonical store was not adopted as the target.
    assert str(isolated_home / ".local" / "state" / "agentacct" / "state") not in claude_json["mcpServers"][
        "agentacct"
    ]["args"]
    assert "reusing it" in result.output


def test_onboard_global_uses_canonical_store_on_a_fresh_machine(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(app, ["onboard", "--scope", "global", "--yes", "--no-start"])
    assert result.exit_code == 0, result.output

    canonical = isolated_home / ".local" / "state" / "agentacct" / "state"
    claude_json = json.loads((isolated_home / ".claude.json").read_text())
    assert str(canonical) in claude_json["mcpServers"]["agentacct"]["args"]
    assert "reusing it" not in result.output


def test_onboard_global_ignores_an_existing_but_EMPTY_legacy_store(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty legacy dir carries no history, so the canonical store still wins."""
    (isolated_home / ".agent-sentinel-global" / "state").mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(app, ["onboard", "--scope", "global", "--yes", "--no-start"])
    assert result.exit_code == 0, result.output

    canonical = isolated_home / ".local" / "state" / "agentacct" / "state"
    claude_json = json.loads((isolated_home / ".claude.json").read_text())
    assert str(canonical) in claude_json["mcpServers"]["agentacct"]["args"]


def test_onboard_global_warns_when_opencode_points_at_another_store(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = _seed_store(tmp_path / "elsewhere" / "state")
    config = isolated_home / ".config" / "opencode" / "opencode.jsonc"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"mcp": {"agentacct": {"type": "local", "command": ["agentacct", "mcp", "serve", "--store-dir", str(other)]}}}),
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(app, ["onboard", "--scope", "global", "--yes", "--no-start"])
    assert result.exit_code == 0, result.output
    assert "OpenCode is registered against a DIFFERENT store" in result.output
    assert "opencode mcp add agentacct" in result.output


def test_onboard_global_warns_when_hook_wrapper_lives_outside_claude_hooks(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = isolated_home / ".agent-sentinel-global" / ".agent-sentinel" / "hooks" / "claude_pre_tool_use.py"
    stale.parent.mkdir(parents=True)
    stale.write_text("# stale wrapper\n", encoding="utf-8")
    settings = isolated_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": f"python3 {stale}"}]}]}}),
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(app, ["onboard", "--scope", "global", "--yes", "--no-start"])
    assert result.exit_code == 0, result.output
    assert "outside ~/.claude/hooks/" in result.output


def test_onboard_global_agent_opencode_writes_mcp_and_global_rules(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(app, ["onboard", "--scope", "global", "--agent", "opencode", "--no-start"])
    assert result.exit_code == 0, result.output

    store = isolated_home / ".local" / "state" / "agentacct" / "state"
    # MCP registered in the real OpenCode config shape (command is an argv array).
    opencode_cfg = json.loads((isolated_home / ".config" / "opencode" / "opencode.jsonc").read_text())
    entry = opencode_cfg["mcp"]["agentacct"]
    assert entry["type"] == "local"
    assert entry["enabled"] is True
    assert str(store) in entry["command"]
    assert Path(entry["command"][0]).is_absolute()
    # Standing 'record your work' rules land in OpenCode's GLOBAL rules file.
    rules = isolated_home / ".config" / "opencode" / "AGENTS.md"
    assert rules.exists()
    assert "agentacct" in rules.read_text()
    # Zero files leaked into the repo.
    for leaked in ("AGENTS.md", ".mcp.json", ".agent-sentinel"):
        assert not (repo / leaked).exists()


def test_onboard_global_agent_hermes_installs_record_your_work_hook(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(app, ["onboard", "--scope", "global", "--agent", "hermes", "--no-start"])
    assert result.exit_code == 0, result.output

    store = isolated_home / ".local" / "state" / "agentacct" / "state"
    hermes_cfg = yaml.safe_load((isolated_home / ".hermes" / "config.yaml").read_text())
    assert hermes_cfg["mcp_servers"]["agentacct"]["args"][-1] == str(store)
    # Hermes has no reliable global instruction slot, so the standing directive is not
    # a FILE — it rides the pre_llm_call hook (injected on each session's first turn).
    assert not (isolated_home / "AGENTS.md").exists()
    assert "pre_llm_call" in hermes_cfg["hooks"]  # the record-your-work nudge hook is wired
    normalized = " ".join(result.output.split())
    assert "record-your-work" in normalized  # hermes now records, not tools-only
    assert "tools registered" not in normalized


def test_onboard_global_hermes_reported_tools_only_when_hooks_block_uneditable(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    # A pre-existing inline (flow-style) hooks block agentacct cannot safely edit: the
    # MCP server is still added, but the record-your-work nudge hook is NOT wired.
    hermes_dir = isolated_home / ".hermes"
    hermes_dir.mkdir(parents=True)
    (hermes_dir / "config.yaml").write_text("hooks: {pre_tool_call: []}\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["onboard", "--scope", "global", "--agent", "hermes", "--no-start"])
    assert result.exit_code == 0, result.output

    cfg = yaml.safe_load((hermes_dir / "config.yaml").read_text())
    assert "agentacct" in cfg["mcp_servers"]  # MCP got configured
    assert "pre_llm_call" not in (cfg.get("hooks") or {})  # but the nudge was NOT wired
    normalized = " ".join(result.output.split())
    # Honest: NOT reported as recording — it says the hooks were not wired and records
    # nothing, and the tools-only caveat is shown instead of a false "recording" claim.
    assert "records nothing" in normalized
    assert "MCP tools registered" in normalized


def test_onboard_global_agent_codex_installs_tool_activity_hooks(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(app, ["onboard", "--scope", "global", "--agent", "codex", "--no-start"])
    assert result.exit_code == 0, result.output

    store = isolated_home / ".local" / "state" / "agentacct" / "state"
    # MCP block still written (the semantic layer).
    assert "[mcp_servers.agentacct]" in (isolated_home / ".codex" / "config.toml").read_text()
    # Automatic hook layer: wrapper + hooks.json wiring PreToolUse + SessionEnd.
    wrapper = isolated_home / ".codex" / "hooks" / "agentacct_codex_hook.py"
    assert wrapper.exists()
    assert str(store) in wrapper.read_text()  # store bound on the command line
    hooks = json.loads((isolated_home / ".codex" / "hooks.json").read_text())
    assert set(hooks["hooks"].keys()) == {"PreToolUse", "SessionEnd"}
    assert "agentacct_codex_hook.py" in hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    # The one-time trust step is surfaced.
    assert "trust" in result.output.lower()
    # Zero files leaked into the repo.
    for leaked in ("hooks.json", ".codex", "AGENTS.md"):
        assert not (repo / leaked).exists()


def test_onboard_global_agent_codex_is_honest_when_hooks_skip(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # When the hook install skips (e.g. a non-object ~/.codex/hooks.json), the
    # onboard summary must NOT claim the session loads hooks that were never
    # wired. Forcing the skip return keeps this deterministic (independent of the
    # global-store resolution and the real hooks.json path).
    import agentacct.cli as cli_module

    monkeypatch.setattr(
        cli_module,
        "_install_codex_hook",
        lambda *args, **kwargs: ("skipped-unparsed", isolated_home / ".codex" / "hooks" / "agentacct_codex_hook.py"),
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(app, ["onboard", "--scope", "global", "--agent", "codex", "--no-start"])
    assert result.exit_code == 0, result.output
    # Normalize whitespace: the rich console soft-wraps at ~80 cols with no TTY
    # (CI), which would split a multi-word phrase across a newline.
    normalized = " ".join(result.output.split())
    assert "NOT wired" in normalized
    assert "loads the server + hooks" not in normalized  # no false "+ hooks" claim
