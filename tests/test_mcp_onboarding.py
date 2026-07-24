from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

from typer.testing import CliRunner

from agent_chronicle.cli import app

runner = CliRunner()


def test_setup_mcp_claude_code_preview_is_copy_paste_friendly_and_does_not_write(tmp_path: Path) -> None:
    result = runner.invoke(app, ["setup", "mcp", "--agent", "claude-code", "--project-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Claude Code" in result.output
    assert "agentacct mcp serve" in result.output
    assert "claude mcp add" in result.output
    assert "Preview only" in result.output
    assert "--write" in result.output
    assert "Source: PyPI (pipx install agentacct)" in result.output
    assert not (tmp_path / ".mcp.json").exists()


def test_setup_mcp_claude_code_preview_quotes_paths_with_spaces(tmp_path: Path) -> None:
    project_dir = tmp_path / "project with spaces"
    project_dir.mkdir()

    result = runner.invoke(app, ["setup", "mcp", "--agent", "claude-code", "--project-dir", str(project_dir)])

    assert result.exit_code == 0, result.output
    assert ".agent-sentinel/state" in result.output


def test_setup_mcp_claude_code_write_creates_project_mcp_json(tmp_path: Path) -> None:
    result = runner.invoke(app, ["setup", "mcp", "--agent", "claude-code", "--project-dir", str(tmp_path), "--write"])

    assert result.exit_code == 0, result.output
    config = json.loads((tmp_path / ".mcp.json").read_text())
    assert config["mcpServers"]["agentacct"]["command"] == "agentacct"
    # Absolute store path by default: a relative path is resolved against the
    # MCP client's launch cwd and used to silently create stray stores.
    expected_store = str((tmp_path / ".agent-sentinel" / "state").resolve())
    assert config["mcpServers"]["agentacct"]["args"] == ["mcp", "serve", "--store-dir", expected_store]
    assert ".mcp.json" in result.output


def test_setup_mcp_claude_code_worktree_defaults_to_owning_repo_store(tmp_path: Path) -> None:
    """Config written from a temp worktree must never embed the worktree's
    vanishing store path (it would merge to main and re-create phantom stores
    after cleanup). The default now remaps to the owning repo's store."""
    project_root = tmp_path / "game"
    worktree = project_root / ".claude" / "worktrees" / "feature-123"
    worktree.mkdir(parents=True)

    result = runner.invoke(app, ["setup", "mcp", "--agent", "claude-code", "--project-dir", str(worktree)])

    assert result.exit_code == 0, result.output
    assert "Claude Code worktree detected" in result.output
    assert "owning project's store" in result.output
    assert str((project_root / ".agent-sentinel" / "state").resolve()) in result.output.replace("\n", "")


def test_setup_mcp_claude_code_write_from_worktree_embeds_owning_repo_store(tmp_path: Path) -> None:
    project_root = tmp_path / "game"
    worktree = project_root / ".claude" / "worktrees" / "feature-123"
    worktree.mkdir(parents=True)

    result = runner.invoke(app, ["setup", "mcp", "--agent", "claude-code", "--project-dir", str(worktree), "--write"])

    assert result.exit_code == 0, result.output
    config = json.loads((worktree / ".mcp.json").read_text())
    expected_store = str((project_root / ".agent-sentinel" / "state").resolve())
    assert config["mcpServers"]["agentacct"]["args"] == ["mcp", "serve", "--store-dir", expected_store]


def test_setup_mcp_codex_write_from_worktree_embeds_owning_repo_store(tmp_path: Path) -> None:
    project_root = tmp_path / "game"
    worktree = project_root / ".claude" / "worktrees" / "feature-777"
    worktree.mkdir(parents=True)

    result = runner.invoke(app, ["setup", "mcp", "--agent", "codex", "--project-dir", str(worktree), "--write"])

    assert result.exit_code == 0, result.output
    payload = tomllib.loads((worktree / ".codex" / "config.toml").read_text())
    args = payload["mcp_servers"]["agentacct"]["args"]
    expected_store = str((project_root / ".agent-sentinel" / "state").resolve())
    assert args == ["mcp", "serve", "--store-dir", expected_store]


def test_setup_mcp_write_from_worktree_with_own_store_keeps_it(tmp_path: Path) -> None:
    project_root = tmp_path / "game"
    worktree = project_root / ".claude" / "worktrees" / "feature-own"
    (worktree / ".agent-sentinel" / "state").mkdir(parents=True)

    result = runner.invoke(app, ["setup", "mcp", "--agent", "claude-code", "--project-dir", str(worktree), "--write"])

    assert result.exit_code == 0, result.output
    config = json.loads((worktree / ".mcp.json").read_text())
    expected_store = str((worktree / ".agent-sentinel" / "state").resolve())
    assert config["mcpServers"]["agentacct"]["args"] == ["mcp", "serve", "--store-dir", expected_store]
    # The advisory owner-store hint still prints for an own-store worktree.
    assert "Claude Code worktree detected" in result.output
    assert "stable ledger" in result.output


def test_setup_mcp_claude_code_default_config_embeds_absolute_store_path(tmp_path: Path) -> None:
    result = runner.invoke(app, ["setup", "mcp", "--agent", "claude-code", "--project-dir", str(tmp_path), "--write"])

    assert result.exit_code == 0, result.output
    text = (tmp_path / ".mcp.json").read_text()
    assert str((tmp_path / ".agent-sentinel" / "state").resolve()) in text
    assert '".agent-sentinel/state"' not in text


def test_setup_mcp_codex_default_project_config_embeds_absolute_store_path(tmp_path: Path) -> None:
    result = runner.invoke(app, ["setup", "mcp", "--agent", "codex", "--project-dir", str(tmp_path), "--write"])

    assert result.exit_code == 0, result.output
    text = (tmp_path / ".codex" / "config.toml").read_text()
    assert str((tmp_path / ".agent-sentinel" / "state").resolve()) in text
    assert '".agent-sentinel/state"' not in text


def test_setup_mcp_relative_store_path_flag_writes_relative(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["setup", "mcp", "--agent", "claude-code", "--project-dir", str(tmp_path), "--write", "--relative-store-path"],
    )

    assert result.exit_code == 0, result.output
    config = json.loads((tmp_path / ".mcp.json").read_text())
    assert config["mcpServers"]["agentacct"]["args"] == ["mcp", "serve", "--store-dir", ".agent-sentinel/state"]
    assert str(tmp_path) not in (tmp_path / ".mcp.json").read_text()


def test_setup_mcp_relative_store_path_flag_conflicts_with_explicit_store_dir(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "setup",
            "mcp",
            "--agent",
            "claude-code",
            "--project-dir",
            str(tmp_path),
            "--store-dir",
            str(tmp_path / "explicit"),
            "--relative-store-path",
        ],
    )

    assert result.exit_code != 0
    assert "cannot be combined" in result.output


def test_setup_mcp_accepts_explicit_command_path_for_local_venv(tmp_path: Path) -> None:
    command_path = tmp_path / "durable-venv" / "bin" / "agent-chronicle"

    result = runner.invoke(
        app,
        ["setup", "mcp", "--agent", "codex", "--project-dir", str(tmp_path), "--mcp-command", str(command_path), "--write"],
    )

    assert result.exit_code == 0, result.output
    text = (tmp_path / ".codex" / "config.toml").read_text()
    assert f'command = "{command_path}"' in text
    assert str((tmp_path / ".agent-sentinel" / "state").resolve()) in text


def test_setup_mcp_expands_explicit_store_dir_tilde(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    result = runner.invoke(app, ["setup", "mcp", "--agent", "claude-code", "--project-dir", str(tmp_path), "--store-dir", "~/sentinel-state", "--write"])

    assert result.exit_code == 0, result.output
    config = json.loads((tmp_path / ".mcp.json").read_text())
    assert config["mcpServers"]["agentacct"]["args"] == ["mcp", "serve", "--store-dir", str(fake_home / "sentinel-state")]


def test_setup_mcp_codex_preview_prints_toml_without_writing_project_config(tmp_path: Path) -> None:
    result = runner.invoke(app, ["setup", "mcp", "--agent", "codex", "--project-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Codex" in result.output
    assert "codex mcp add agentacct -- agentacct mcp serve" in result.output
    assert "[mcp_servers.agentacct]" in result.output
    assert 'command = "agentacct"' in result.output
    assert "Preview only" in result.output
    assert not (tmp_path / ".codex" / "config.toml").exists()


def test_setup_mcp_codex_write_appends_safe_project_config_block(tmp_path: Path) -> None:
    result = runner.invoke(app, ["setup", "mcp", "--agent", "codex", "--project-dir", str(tmp_path), "--write"])

    assert result.exit_code == 0, result.output
    config_path = tmp_path / ".codex" / "config.toml"
    text = config_path.read_text()
    assert "# agentacct MCP" in text
    assert "[mcp_servers.agentacct]" in text
    assert 'command = "agentacct"' in text
    assert '"--store-dir"' in text
    assert str((tmp_path / ".agent-sentinel" / "state").resolve()) in text


_CUSTOM_SETTINGS_NOTE = "custom settings found; it keeps working as-is"


def _computed_store(tmp_path: Path) -> str:
    return str((tmp_path / ".agent-sentinel" / "state").resolve())


def test_setup_mcp_codex_write_migrates_standard_pre_rename_block(tmp_path: Path, monkeypatch) -> None:
    # A pre-rename [mcp_servers.agent-sentinel] block whose settings match the
    # standard install (ignoring the binary name itself) is REPLACED by the new
    # agent-chronicle block in place — never left behind as a duplicate
    # registration.
    config_dir = tmp_path / ".codex"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.toml"
    config_path.write_text(
        "model = \"gpt-5\"\n"
        "# Agent Sentinel MCP\n"
        "[mcp_servers.agent-sentinel]\n"
        "command = \"agent-sentinel\"\n"
        f"args = [\"mcp\", \"serve\", \"--store-dir\", \"{_computed_store(tmp_path)}\"]\n"
        "[mcp_servers.other]\n"
        "command = \"other\"\n"
    )
    result = runner.invoke(app, ["setup", "mcp", "--agent", "codex", "--project-dir", str(tmp_path), "--write"])

    assert result.exit_code == 0, result.output
    text = config_path.read_text()
    assert ".agent-sentinel/state" in text
    assert "# Agent Sentinel MCP" not in text
    parsed = tomllib.loads(text)
    assert set(parsed["mcp_servers"]) == {"agentacct", "other"}
    assert parsed["mcp_servers"]["agentacct"]["args"] == ["mcp", "serve", "--store-dir", _computed_store(tmp_path)]


def test_setup_mcp_codex_write_leaves_custom_pre_rename_block_untouched_with_note(tmp_path: Path, monkeypatch) -> None:
    # A pre-rename block whose command/args/store-dir differ from what the
    # writer would generate is the USER'S configuration: never silently
    # replaced (the old registration keeps working via the alias binary), and
    # nothing is written over it — one note explains how to migrate.
    config_dir = tmp_path / ".codex"
    config_dir.mkdir(parents=True)
    old_store = tmp_path / "old" / "state"
    config_path = config_dir / "config.toml"
    original = (
        "model = \"gpt-5\"\n"
        "# Agent Sentinel MCP\n"
        "[mcp_servers.agent-sentinel]\n"
        "command = \"/custom/venv/bin/agent-sentinel\"\n"
        f"args = [\"mcp\", \"serve\", \"--store-dir\", \"{old_store}\"]\n"
        "[mcp_servers.other]\n"
        "command = \"other\"\n"
    )
    config_path.write_text(original)
    result = runner.invoke(app, ["setup", "mcp", "--agent", "codex", "--project-dir", str(tmp_path), "--write"])

    assert result.exit_code == 0, result.output
    assert _CUSTOM_SETTINGS_NOTE in " ".join(result.output.split())
    assert config_path.read_text() == original


def test_setup_mcp_codex_write_collapses_both_generation_blocks_old_first(tmp_path: Path, monkeypatch) -> None:
    # Both a pre-rename `agent-sentinel` block AND an `agent-chronicle` block
    # present (mid-migration state): the writer must collapse them into ONE
    # valid new block — never a duplicate [mcp_servers.agent-chronicle] table
    # (invalid TOML, codex refuses the whole config) and never a silently
    # surviving old registration (split ledger).
    config_dir = tmp_path / ".codex"
    config_dir.mkdir(parents=True)
    stale_store = tmp_path / "stale" / "state"
    config_path = config_dir / "config.toml"
    config_path.write_text(
        "model = \"gpt-5\"\n"
        "# Agent Sentinel MCP\n"
        "[mcp_servers.agent-sentinel]\n"
        "command = \"agent-sentinel\"\n"
        f"args = [\"mcp\", \"serve\", \"--store-dir\", \"{_computed_store(tmp_path)}\"]\n"
        "# Agent Chronicle MCP\n"
        "[mcp_servers.agent-chronicle]\n"
        "command = \"agent-chronicle\"\n"
        f"args = [\"mcp\", \"serve\", \"--store-dir\", \"{stale_store}\"]\n"
        "[mcp_servers.other]\n"
        "command = \"other\"\n"
    )
    result = runner.invoke(app, ["setup", "mcp", "--agent", "codex", "--project-dir", str(tmp_path), "--write"])

    assert result.exit_code == 0, result.output
    text = config_path.read_text()
    parsed = tomllib.loads(text)  # duplicate tables would raise here
    assert set(parsed["mcp_servers"]) == {"agentacct", "other"}
    assert parsed["mcp_servers"]["agentacct"]["args"] == ["mcp", "serve", "--store-dir", _computed_store(tmp_path)]
    assert str(stale_store) not in text


def test_setup_mcp_codex_write_collapses_both_generation_blocks_new_first(tmp_path: Path, monkeypatch) -> None:
    # Reverse order (new block above the old one): the old-name block must not
    # silently survive as a second live registration.
    config_dir = tmp_path / ".codex"
    config_dir.mkdir(parents=True)
    stale_store = tmp_path / "stale" / "state"
    config_path = config_dir / "config.toml"
    config_path.write_text(
        "# Agent Chronicle MCP\n"
        "[mcp_servers.agent-chronicle]\n"
        "command = \"agent-chronicle\"\n"
        f"args = [\"mcp\", \"serve\", \"--store-dir\", \"{stale_store}\"]\n"
        "# Agent Sentinel MCP\n"
        "[mcp_servers.agent-sentinel]\n"
        "command = \"agent-sentinel\"\n"
        f"args = [\"mcp\", \"serve\", \"--store-dir\", \"{_computed_store(tmp_path)}\"]\n"
        "[mcp_servers.other]\n"
        "command = \"other\"\n"
    )
    result = runner.invoke(app, ["setup", "mcp", "--agent", "codex", "--project-dir", str(tmp_path), "--write"])

    assert result.exit_code == 0, result.output
    text = config_path.read_text()
    parsed = tomllib.loads(text)
    assert set(parsed["mcp_servers"]) == {"agentacct", "other"}
    assert parsed["mcp_servers"]["agentacct"]["args"] == ["mcp", "serve", "--store-dir", _computed_store(tmp_path)]
    assert "# Agent Sentinel MCP" not in text


def test_setup_mcp_codex_write_migrates_standard_block_with_env_subtable(tmp_path: Path, monkeypatch) -> None:
    # A standard-shape pre-rename block carrying its env as a TOML sub-table
    # migrates cleanly: the env is carried into the new block and no ghost
    # [mcp_servers.agent-sentinel.env] (env-without-command server) survives.
    config_dir = tmp_path / ".codex"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.toml"
    config_path.write_text(
        "# Agent Sentinel MCP\n"
        "[mcp_servers.agent-sentinel]\n"
        "command = \"agent-sentinel\"\n"
        f"args = [\"mcp\", \"serve\", \"--store-dir\", \"{_computed_store(tmp_path)}\"]\n"
        "[mcp_servers.agent-sentinel.env]\n"
        "AGENT_SENTINEL_PRICING_CATALOG_PATH = \"/pinned/catalog.json\"\n"
        "[mcp_servers.other]\n"
        "command = \"other\"\n"
    )
    result = runner.invoke(app, ["setup", "mcp", "--agent", "codex", "--project-dir", str(tmp_path), "--write"])

    assert result.exit_code == 0, result.output
    parsed = tomllib.loads(config_path.read_text())
    assert set(parsed["mcp_servers"]) == {"agentacct", "other"}
    server = parsed["mcp_servers"]["agentacct"]
    assert server["args"] == ["mcp", "serve", "--store-dir", _computed_store(tmp_path)]
    assert server["env"] == {"AGENT_SENTINEL_PRICING_CATALOG_PATH": "/pinned/catalog.json"}


def test_setup_mcp_claude_write_leaves_custom_pre_rename_registration_untouched_with_note(tmp_path: Path) -> None:
    # .mcp.json parallel of the codex custom-settings rule: a pre-rename
    # registration with a custom command/store-dir/env is left byte-identical
    # (no silent re-point of recording to a different store) plus one note.
    config_path = tmp_path / ".mcp.json"
    original = json.dumps(
        {
            "mcpServers": {
                "agent-sentinel": {
                    "command": "/custom/venv/bin/agent-sentinel",
                    "args": ["mcp", "serve", "--store-dir", "/custom/global/state"],
                    "env": {"FOO": "bar"},
                }
            }
        },
        indent=2,
    ) + "\n"
    config_path.write_text(original)
    result = runner.invoke(app, ["setup", "mcp", "--agent", "claude-code", "--project-dir", str(tmp_path), "--write"])

    assert result.exit_code == 0, result.output
    assert _CUSTOM_SETTINGS_NOTE in " ".join(result.output.split())
    assert config_path.read_text() == original
    assert "Wrote Claude Code MCP config" not in result.output


def test_setup_mcp_claude_write_migrates_standard_pre_rename_registration_preserving_env(tmp_path: Path) -> None:
    # A standard-shape pre-rename registration (matching what the writer would
    # generate, ignoring the binary name) migrates in place and carries its
    # env block into the new registration.
    config_path = tmp_path / ".mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "agent-sentinel": {
                        "command": "agent-sentinel",
                        "args": ["mcp", "serve", "--store-dir", _computed_store(tmp_path)],
                        "env": {"AGENT_SENTINEL_PRICING_CATALOG_PATH": "/pinned/catalog.json"},
                    },
                    "other": {"command": "other"},
                }
            }
        )
        + "\n"
    )
    result = runner.invoke(app, ["setup", "mcp", "--agent", "claude-code", "--project-dir", str(tmp_path), "--write"])

    assert result.exit_code == 0, result.output
    config = json.loads(config_path.read_text())
    assert set(config["mcpServers"]) == {"agentacct", "other"}
    server = config["mcpServers"]["agentacct"]
    assert server["args"] == ["mcp", "serve", "--store-dir", _computed_store(tmp_path)]
    assert server["env"] == {"AGENT_SENTINEL_PRICING_CATALOG_PATH": "/pinned/catalog.json"}


def test_setup_mcp_codex_write_updates_quoted_existing_block(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / ".codex"
    config_dir.mkdir(parents=True)
    old_store = tmp_path / "old" / "state"
    config_path = config_dir / "config.toml"
    config_path.write_text(
        "[mcp_servers.\"agent-chronicle\"]\n"
        "command = \"agent-chronicle\"\n"
        f"args = [\"mcp\", \"serve\", \"--store-dir\", \"{old_store}\"]\n"
    )
    result = runner.invoke(app, ["setup", "mcp", "--agent", "codex", "--project-dir", str(tmp_path), "--write"])

    assert result.exit_code == 0, result.output
    parsed = tomllib.loads(config_path.read_text())
    assert parsed["mcp_servers"]["agentacct"]["args"] == [
        "mcp",
        "serve",
        "--store-dir",
        str((tmp_path / ".agent-sentinel" / "state").resolve()),
    ]


def test_setup_mcp_codex_write_escapes_toml_special_characters(tmp_path: Path) -> None:
    tricky_store = tmp_path / 'store "quoted"' / "state"

    result = runner.invoke(
        app,
        ["setup", "mcp", "--agent", "codex", "--project-dir", str(tmp_path), "--store-dir", str(tricky_store), "--write"],
    )

    assert result.exit_code == 0, result.output
    text = (tmp_path / ".codex" / "config.toml").read_text()
    assert '\\"quoted\\"' in text
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["agentacct"]["args"] == ["mcp", "serve", "--store-dir", str(tricky_store)]


def test_public_docs_recommend_read_only_mcp_doctor() -> None:
    # The manual-setup block moved from the README to docs/reference.md in the
    # value-first README rewrite; the doctor recommendation lives there now.
    text = Path("docs/reference.md").read_text()
    # The resolver finds the project store from the project root; the old
    # explicit-relative form is retired everywhere except the Claude Code
    # prompt, which passes an explicit absolute "$SENTINEL_STORE".
    assert "agentacct mcp doctor --store-dir .agent-sentinel/state" not in text
    assert "agentacct mcp doctor" in text
    # The README must not resurrect the retired explicit-relative form either.
    assert "agentacct mcp doctor --store-dir .agent-sentinel/state" not in Path("README.md").read_text()


def test_docs_no_longer_claim_doctor_writes() -> None:
    checklist = Path("docs/public-alpha-checklist.md").read_text()
    workflow = Path("docs/agent-chronicle-workflow-instructions.md").read_text()

    assert "`mcp doctor` is read-only by default" in checklist
    assert "throwaway temp store" in checklist
    assert "should write/read a safe local test event" not in checklist
    assert "read-only; it never writes to the ledger" in workflow


def test_readme_leads_with_the_one_line_install_prompt() -> None:
    text = Path("README.md").read_text()
    # The install section leads with the public paste prompt pointing at
    # INSTALL.md. The README wraps that prompt across lines for readability (a
    # single ~500-char line overflows on GitHub); `setup prompt` still emits the
    # canonical single line (test_one_liner_is_a_single_public_install_line), so
    # here we assert the prompt's substance is present, not the exact one line.
    assert "Install and set up agentacct in this repo" in text
    assert "pipx install agentacct" in text
    assert "agentacct onboard" in text
    assert "never store, request, or echo any API key" in text
    assert "INSTALL.md" in text
    assert "Install Agent Chronicle for this Claude Code workspace" not in text
    assert 'SENTINEL_STORE="$PROJECT_ROOT/.agent-sentinel/state"' not in text


def test_readme_leads_with_work_intelligence_not_retired_framings() -> None:
    text = Path("README.md").read_text()
    # Phase 3 positioning: the join story leads; the retired circuit-breaker /
    # ledger / flight-recorder framings are gone from the public first screen.
    assert "Agent Work Intelligence" in text
    assert "circuit breaker" not in text.lower()
    assert "circuit-breaker" not in text.lower()
    assert "flight recorder" not in text
    for word in ("join", "attribution", "hook bridge", "confidence"):
        assert word in text, f"README must tell the join story; missing: {word}"


def test_retired_framings_gone_from_all_public_docs_the_checklist_claims() -> None:
    # docs/public-alpha-checklist.md ticks "the retired 'black box recorder /
    # circuit breaker' framing is gone from public docs". That evidence-cited
    # checkbox is only true if the framing is gone from EVERY first-touch public
    # doc a reader reaches from the README — not just the README first screen.
    retired = ("circuit breaker", "circuit-breaker", "black box", "black-box", "flight recorder", "electric meter")
    # The checklist itself NAMES the retired phrases when it claims they are
    # gone; every OTHER public doc must be free of them.
    public_docs = [
        Path("README.md"),
        Path("CONTRIBUTING.md"),
        Path("docs/architecture.md"),
        Path("docs/prompt-budget-optimizer.md"),
    ]
    offenders: list[str] = []
    for doc in public_docs:
        text = doc.read_text(encoding="utf-8").lower()
        offenders.extend(f"{doc}: {phrase!r}" for phrase in retired if phrase in text)
    assert not offenders, "retired framing still present in public docs: " + "; ".join(offenders)


def test_setup_mcp_codex_write_ignores_commented_sample_section(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / ".codex"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.toml"
    config_path.write_text(
        "# Example only:\n"
        "# [mcp_servers.agent-sentinel]\n"
        "# command = \"agent-chronicle\"\n"
    )
    result = runner.invoke(app, ["setup", "mcp", "--agent", "codex", "--project-dir", str(tmp_path), "--write"])

    assert result.exit_code == 0, result.output
    text = config_path.read_text()
    assert "# [mcp_servers.agent-sentinel]" in text
    assert "\n[mcp_servers.agentacct]\n" in text


def test_setup_mcp_hermes_preview_does_not_write_profile_config(tmp_path: Path) -> None:
    result = runner.invoke(app, ["setup", "mcp", "--agent", "hermes", "--project-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Hermes" in result.output
    assert "hermes mcp add agentacct --command agentacct --args mcp serve" in result.output
    assert "Preview only" in result.output
    assert not (tmp_path / ".mcp.json").exists()


def test_setup_mcp_opencode_preview_uses_verified_separator_syntax(tmp_path: Path) -> None:
    result = runner.invoke(app, ["setup", "mcp", "--agent", "opencode", "--project-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "OpenCode" in result.output
    assert "opencode mcp add agentacct -- agentacct mcp serve" in result.output
    assert "Preview only" in result.output
    assert not (tmp_path / ".config" / "opencode" / "opencode.jsonc").exists()


def test_setup_mcp_openclaw_preview_uses_repeatable_arg_syntax(tmp_path: Path) -> None:
    result = runner.invoke(app, ["setup", "mcp", "--agent", "openclaw", "--project-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "OpenClaw" in result.output
    assert "openclaw mcp add agentacct --command agentacct" in result.output
    expected_store = str((tmp_path / ".agent-sentinel" / "state").resolve())
    assert f"--arg mcp --arg serve --arg --store-dir --arg {expected_store}" in result.output
    assert "Preview only" in result.output


def test_setup_mcp_generic_preview_prints_portable_json(tmp_path: Path) -> None:
    result = runner.invoke(app, ["setup", "mcp", "--agent", "generic", "--project-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Generic MCP-capable agent" in result.output
    assert '"mcpServers"' in result.output
    # Absolute store path in the portable definition too: the pasted config's
    # launch cwd is the MCP client's, so a relative path is a stray-store trap.
    assert str((tmp_path / ".agent-sentinel" / "state").resolve()) in result.output


def test_setup_mcp_profile_global_agents_reject_write(tmp_path: Path) -> None:
    for agent in ["generic", "hermes", "opencode", "openclaw"]:
        result = runner.invoke(app, ["setup", "mcp", "--agent", agent, "--project-dir", str(tmp_path), "--write"])

        assert result.exit_code != 0, agent
        assert "--write is not available" in result.output


def test_readme_links_coding_agent_integration_guide() -> None:
    readme = Path("README.md").read_text()
    guide = Path("docs/coding-agent-integrations.md").read_text()

    assert "docs/coding-agent-integrations.md" in readme
    assert "Integration tiers" in guide
    assert "agentacct setup mcp --agent hermes" in guide
    assert "agentacct setup mcp --agent opencode" in guide
    assert "agentacct setup mcp --agent openclaw" in guide
    assert "MCP setup does not automatically monitor all agent sessions" in guide


def test_mcp_doctor_is_read_only_by_default(tmp_path: Path) -> None:
    from agent_chronicle.mcp import SentinelMCPServer

    server = SentinelMCPServer(store_dir=tmp_path)
    server.call_tool("sentinel_record_event", {"source": "codex", "event_type": "note", "metadata": {"summary": "seed"}})
    events_path = tmp_path / "events.jsonl"
    before_bytes = events_path.read_bytes()

    result = runner.invoke(app, ["mcp", "doctor", "--store-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert events_path.read_bytes() == before_bytes
    normalized = " ".join(result.output.split())
    assert "sentinel_record_event" in normalized
    assert "sentinel_list_events" in normalized
    assert "Read-only diagnostics: no events were written." in normalized
    assert "--write-probe" in normalized
    assert "mcp_doctor_test" not in events_path.read_text(encoding="utf-8")


def test_mcp_doctor_read_only_does_not_create_store_dirs(tmp_path: Path) -> None:
    empty_store = tmp_path / "empty-store"
    empty_store.mkdir()

    result = runner.invoke(app, ["mcp", "doctor", "--store-dir", str(empty_store)])

    assert result.exit_code == 0, result.output
    assert list(empty_store.iterdir()) == []  # no runs/, no events.jsonl
    assert "store file absent" in " ".join(result.output.split())

    missing_store = tmp_path / "missing-store"
    missing = runner.invoke(app, ["mcp", "doctor", "--store-dir", str(missing_store)])

    assert missing.exit_code == 0, missing.output
    assert not missing_store.exists()
    assert "store not initialized" in " ".join(missing.output.split())


def test_mcp_doctor_write_probe_round_trips_in_temp_store_only(tmp_path: Path) -> None:
    (tmp_path / "events.jsonl").write_text("", encoding="utf-8")
    before_bytes = (tmp_path / "events.jsonl").read_bytes()

    result = runner.invoke(app, ["mcp", "doctor", "--store-dir", str(tmp_path), "--write-probe"])

    assert result.exit_code == 0, result.output
    normalized = " ".join(result.output.split())
    assert "round-trip ok (throwaway temp store, removed)" in normalized
    # The production/resolved store never receives the probe event.
    assert (tmp_path / "events.jsonl").read_bytes() == before_bytes
    import tempfile as _tempfile

    assert not list(Path(_tempfile.gettempdir()).glob("agent-sentinel-doctor-probe-*"))


def test_mcp_doctor_write_probe_failure_exits_1(tmp_path: Path, monkeypatch) -> None:
    from agent_chronicle import cli as cli_module

    class _BrokenServer:
        def __init__(self, *, store_dir) -> None:
            self.store_dir = store_dir

        def call_tool(self, *args, **kwargs):
            raise RuntimeError("write path broken")

    monkeypatch.setattr(cli_module, "SentinelMCPServer", _BrokenServer)

    result = runner.invoke(app, ["mcp", "doctor", "--store-dir", str(tmp_path), "--write-probe"])

    assert result.exit_code == 1, result.output
    assert "write probe failed" in " ".join(result.output.split())


def test_mcp_doctor_json_shape(tmp_path: Path) -> None:
    from agent_chronicle.mcp import SentinelMCPServer

    server = SentinelMCPServer(store_dir=tmp_path)
    server.call_tool("sentinel_record_event", {"source": "codex", "event_type": "note", "metadata": {"summary": "seed"}})

    result = runner.invoke(app, ["mcp", "doctor", "--store-dir", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["read_only"] is True
    assert payload["store"]["path"] == str(tmp_path)
    assert payload["store"]["source"] == "flag"
    assert payload["store"]["exists"] is True
    assert payload["store"]["event_count"] == 1
    assert payload["store"]["parse_errors"] == 0
    assert {check["name"] for check in payload["checks"]} >= {"store resolution", "store readability", "store writability", "mcp tools"}
    assert all(check["status"] in {"ok", "warn", "fail"} for check in payload["checks"])
    assert payload["join_health"]["context_events"] == 0
    assert payload["hook_context"]["status"] == "absent"
    assert payload["write_probe"] is None


def test_mcp_doctor_write_probe_json_reports_temp_store(tmp_path: Path) -> None:
    result = runner.invoke(app, ["mcp", "doctor", "--store-dir", str(tmp_path), "--write-probe", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["write_probe"] == {"round_trip_ok": True, "temp_store": True}


def test_mcp_doctor_warns_on_relative_store_path_in_mcp_config(tmp_path: Path, monkeypatch) -> None:
    from agent_chronicle.store_resolution import ENV_STORE_DIR

    monkeypatch.delenv(ENV_STORE_DIR, raising=False)
    project = tmp_path / "project"
    (project / ".agent-sentinel" / "state").mkdir(parents=True)
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"agent-chronicle": {"command": "agent-chronicle", "args": ["mcp", "serve", "--store-dir", ".agent-sentinel/state"]}}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["mcp", "doctor", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["store"]["source"] == "project"
    config_check = next(check for check in payload["checks"] if check["name"] == "mcp config (.mcp.json [agent-chronicle])")
    assert config_check["status"] == "warn"
    assert "legacy relative store path" in config_check["details"]
    assert "setup mcp --agent claude-code --write" in config_check["details"]


def test_mcp_doctor_reports_resolution_failure_as_failing_check(tmp_path: Path, monkeypatch) -> None:
    from agent_chronicle.store_resolution import ENV_STORE_DIR

    monkeypatch.delenv(ENV_STORE_DIR, raising=False)
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)

    result = runner.invoke(app, ["mcp", "doctor", "--json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    resolution_check = next(check for check in payload["checks"] if check["name"] == "store resolution")
    assert resolution_check["status"] == "fail"
    assert "agentacct init" in resolution_check["details"]
    assert payload["store"]["path"] is None


def test_full_demo_docs_list_event_mcp_tools() -> None:
    text = Path("docs/full-demo.md").read_text()
    assert "sentinel_record_event" in text
    assert "sentinel_list_events" in text


def test_readme_links_public_alpha_checklist() -> None:
    readme = Path("README.md").read_text()
    checklist = Path("docs/public-alpha-checklist.md").read_text()

    assert "docs/public-alpha-checklist.md" in readme
    assert "Public Alpha Checklist" in checklist
    assert "not include private strategy" in checklist


def test_public_docs_include_live_agent_smoke_gate() -> None:
    # The smoke-gate commands moved from the README to docs/reference.md in the
    # value-first README rewrite.
    reference = Path("docs/reference.md").read_text()
    checklist = Path("docs/public-alpha-checklist.md").read_text()
    guide = Path("docs/live-agent-smoke.md").read_text()
    results = Path("docs/live-smoke-results.md").read_text()

    # The reference, the maintainer checklist, and the smoke guide are all
    # rebranded to the public agentacct CLI for current recommendations.
    assert "agentacct smoke claude-code" in reference
    assert "agentacct smoke codex" in reference
    assert "agentacct smoke all --json" in reference
    for text in (checklist, guide):
        assert "agentacct smoke claude-code" in text
        assert "agentacct smoke codex" in text
        assert "agentacct smoke all --json" in text
    for text in (reference, checklist, guide):
        assert "automatically monitors" in text
    # The results page is preserved dated evidence: its 2026-06-27 run used
    # the pre-rename binary, and dated evidence keeps the name it was
    # observed under (see the page's own historical note).
    assert "agent-sentinel smoke claude-code" in results
    assert "agent-sentinel smoke codex" in results
    assert "agent-sentinel smoke all --json" in results
    assert "automatically monitors" in results

    assert "live-agent-smoke.md" in reference
    assert "docs/live-agent-smoke.md" in checklist
    assert "live-smoke-results.md" in reference
    assert "docs/live-smoke-results.md" in checklist
    assert "docs/live-smoke-results.md" in guide
    assert "claude -p 'Reply with exactly: AGENT_CHRONICLE_CLAUDE_WRAP_OK'" in guide
    assert "--max-turns" not in guide
    assert "codex exec --sandbox read-only --ephemeral --skip-git-repo-check" in guide
    assert "stdout.log" in guide
    assert "metadata.json" in guide
    assert "marker_found" in guide
    assert "metadata_ok" in guide
    assert "Claude Code: passed" in results
    assert "Codex: passed" in results
    # Dated-evidence honesty: the recorded 2026-06-27 markers predate the
    # rename, so the preserved observations MUST keep the AGENT_SENTINEL_*
    # spelling (the AGENT_CHRONICLE_* markers did not exist yet).
    assert "AGENT_SENTINEL_CLAUDE_WRAP_OK" in results
    assert "AGENT_SENTINEL_CODEX_WRAP_OK" in results
    assert "AGENT_CHRONICLE_CLAUDE_WRAP_OK" not in results
    assert "AGENT_CHRONICLE_CODEX_WRAP_OK" not in results
    assert "automatically monitors Claude Code or Codex sessions launched outside Chronicle" in results
    assert "/tmp/agent-" not in results
    assert "session id" not in results.lower()


def test_public_docs_include_explicit_command_wrappers_without_overclaiming() -> None:
    # The wrapper-command reference moved from the README to docs/reference.md
    # in the value-first README rewrite.
    reference = Path("docs/reference.md").read_text()
    checklist = Path("docs/public-alpha-checklist.md").read_text()

    for text in (reference, checklist):
        assert "agentacct-claude" in text
        assert "agentacct-codex" in text
    assert "explicit wrapper commands" in reference
    assert "They do not automatically monitor Claude Code/Codex sessions started outside these wrapper commands" in reference
    assert "do not provide exact subscription billing by themselves" in reference
    assert "explicit opt-in wrappers" in checklist


def test_public_docs_record_mcp_client_smoke_success_without_overclaiming() -> None:
    # The smoke-evidence pointers moved from the README to docs/reference.md in
    # the value-first README rewrite (which links the results page relative to
    # docs/, without the "docs/" prefix).
    reference = Path("docs/reference.md").read_text()
    checklist = Path("docs/public-alpha-checklist.md").read_text()
    results = Path("docs/live-mcp-client-smoke-results.md").read_text()

    for text in (reference, checklist):
        assert "live-mcp-client-smoke-results.md" in text
        assert "successfully called Sentinel MCP tools" in text or "smoke-tested as an MCP tool" in text
    assert "sentinel_record_event" in checklist
    assert "sentinel_attach_client_context" in checklist
    assert "sentinel_record_section" in checklist
    assert "sentinel_record_agent_usage_debug" in checklist
    assert "sentinel_get_event_summary" in checklist
    assert "local dashboard must aggregate those usage/context/debug events" in checklist

    assert "Live MCP Client Smoke Results" in results
    assert "Claude Code interactive MCP client: passed" in results
    assert "Codex interactive MCP client: passed" in results
    assert "2026-07-02 semantic context dogfood result" in results
    assert "Codex semantic MCP transport: passed" in results
    assert "Claude Code semantic MCP transport: passed" in results
    assert "client_context_attached" in results
    assert "section_checkpoint" in results
    assert "raw JSON-RPC objects over stdio" in results
    assert "Content-Length framed JSON-RPC" in results
    assert "Agent Chronicle has been smoke-tested as an MCP tool inside real interactive Claude Code and Codex sessions" in results
    assert "Agent Chronicle automatically tracks every Claude Code/Codex session without configuration" in results
    assert "reads exact Claude Code/Codex/ChatGPT subscription billing" in results
    assert "/tmp/agent-" not in results
    assert "session id" not in results.lower()
    assert "session IDs" not in results


def test_mcp_doctor_warns_when_client_context_is_not_joinable(tmp_path: Path) -> None:
    from agent_chronicle.mcp import SentinelMCPServer

    server = SentinelMCPServer(store_dir=tmp_path)
    server.call_tool(
        "sentinel_attach_client_context",
        {"source": "codex", "client": "codex", "project_dir": "/tmp/project"},
    )
    server.call_tool(
        "sentinel_record_section",
        {"source": "codex", "section_id": "weak-section", "section_status": "started"},
    )

    result = runner.invoke(app, ["mcp", "doctor", "--store-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Join health:" in result.output
    assert "client_context events: 1 (0 joinable)" in result.output
    assert "section events: 1 (0 joinable)" in result.output
    assert "Warning:" in result.output
    assert "client_session_id" in result.output


def test_mcp_doctor_reports_joinable_context_without_warnings(tmp_path: Path) -> None:
    from agent_chronicle.mcp import SentinelMCPServer

    server = SentinelMCPServer(store_dir=tmp_path)
    server.call_tool(
        "sentinel_attach_client_context",
        {"source": "claude-code", "client": "claude-code", "client_session_id": "session-1"},
    )
    server.call_tool(
        "sentinel_record_section",
        {"source": "claude-code", "section_id": "joined-section", "section_status": "started"},
    )

    result = runner.invoke(app, ["mcp", "doctor", "--store-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "client_context events: 1 (1 joinable)" in result.output
    assert "section events: 1 (1 joinable)" in result.output
    assert "Warning:" not in result.output


def test_mcp_doctor_reports_hook_context_status(tmp_path: Path) -> None:
    result = runner.invoke(app, ["mcp", "doctor", "--store-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    normalized = " ".join(result.output.split())
    assert "claude-code hook context: absent" in normalized
    assert "hooks claude-code install" in normalized

    from agent_chronicle.hooks import write_claude_code_hook_context

    write_claude_code_hook_context(
        tmp_path,
        {
            "schema_version": "agent-sentinel.client-context.v1",
            "client": "claude-code",
            "client_session_id": "3778e5d9-aaaa-bbbb-cccc-1234567890ab",
            "client_transcript_id": "3778e5d9-aaaa-bbbb-cccc-1234567890ab",
            "project_dir": "/tmp/project",
            "source": "claude_code_hook",
            "hook_event_name": "PreToolUse",
        },
    )
    fresh_result = runner.invoke(app, ["mcp", "doctor", "--store-dir", str(tmp_path)])

    assert fresh_result.exit_code == 0, fresh_result.output
    assert "claude-code hook context: fresh" in fresh_result.output
    assert "3778e5d9" in fresh_result.output


def test_mcp_doctor_notes_concurrent_hook_contexts(tmp_path: Path) -> None:
    from agent_chronicle.hooks import write_claude_code_hook_context

    for session_id in ("3778e5d9-aaaa-bbbb-cccc-1234567890ab", "9b2c1a00-dddd-eeee-ffff-0987654321fe"):
        write_claude_code_hook_context(
            tmp_path,
            {
                "schema_version": "agent-sentinel.client-context.v1",
                "client": "claude-code",
                "client_session_id": session_id,
                "client_transcript_id": session_id,
                "project_label": "project",
                "source": "claude_code_hook",
                "hook_event_name": "PreToolUse",
            },
        )

    result = runner.invoke(app, ["mcp", "doctor", "--store-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    normalized = " ".join(result.output.split())
    assert "2 concurrent session contexts" in normalized
    assert "refuse id inheritance" in normalized


def test_mcp_doctor_reports_unreadable_events_file_as_failing_check(tmp_path: Path) -> None:
    """Minor fix (Phase 1 review): an unreadable events.jsonl is exactly what a
    doctor exists to diagnose — clean failing check, no traceback."""
    if os.geteuid() == 0:
        return  # chmod 000 does not block root
    store = tmp_path / "state"
    store.mkdir()
    events = store / "events.jsonl"
    events.write_text('{"event_type": "note"}\n', encoding="utf-8")
    events.chmod(0)
    try:
        result = runner.invoke(app, ["mcp", "doctor", "--store-dir", str(store)])
    finally:
        events.chmod(0o644)

    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert "PermissionError" not in result.output
    assert "fail: store readability" in result.output
    assert "not readable" in result.output


def test_mcp_doctor_json_stays_valid_when_events_file_is_unreadable(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        return  # chmod 000 does not block root
    store = tmp_path / "state"
    store.mkdir()
    events = store / "events.jsonl"
    events.write_text('{"event_type": "note"}\n', encoding="utf-8")
    events.chmod(0)
    try:
        result = runner.invoke(app, ["mcp", "doctor", "--store-dir", str(store), "--json"])
    finally:
        events.chmod(0o644)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["read_only"] is True
    readability = [check for check in payload["checks"] if check["name"] == "store readability"]
    assert readability and readability[0]["status"] == "fail"
    assert "not readable" in readability[0]["details"]
