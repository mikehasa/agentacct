import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from agent_chronicle import cli as cli_module
from agent_chronicle.cli import app
from agent_chronicle.policy import DEFAULT_POLICY_FILE, load_policy, validate_policy


def test_init_creates_project_policy_without_overwriting(tmp_path):
    runner = CliRunner()

    result = runner.invoke(app, ["init", "--project-dir", str(tmp_path)])

    assert result.exit_code == 0
    policy_path = tmp_path / DEFAULT_POLICY_FILE
    assert policy_path.exists()
    assert "max_total_usd" in policy_path.read_text()
    assert ".agent-sentinel/policy.yaml" in result.output
    gitignore = tmp_path / ".gitignore"
    assert gitignore.exists()
    assert ".agent-sentinel/state/" in gitignore.read_text()
    assert ".env.local" in gitignore.read_text()

    policy_path.write_text("custom: true\n")
    second = runner.invoke(app, ["init", "--project-dir", str(tmp_path)])

    assert second.exit_code == 0
    assert "Policy already exists" in second.output
    assert policy_path.read_text() == "custom: true\n"
    assert (tmp_path / ".agent-sentinel" / "state").exists()


def test_init_can_add_agent_instructions_without_overwriting_existing_policy(tmp_path):
    runner = CliRunner()
    runner.invoke(app, ["init", "--project-dir", str(tmp_path)])
    policy_path = tmp_path / DEFAULT_POLICY_FILE
    policy_path.write_text("custom: true\n")

    result = runner.invoke(app, ["init", "--project-dir", str(tmp_path), "--agent", "codex"])

    assert result.exit_code == 0
    assert (tmp_path / "AGENTS.md").exists()
    assert policy_path.read_text() == "custom: true\n"
    assert "Policy already exists" in result.output


def test_init_can_add_agent_instructions_idempotently(tmp_path):
    runner = CliRunner()

    result = runner.invoke(app, ["init", "--project-dir", str(tmp_path), "--agent", "claude-code", "--agent", "codex", "--agent", "hermes", "--agent", "opencode", "--agent", "openclaw"])

    assert result.exit_code == 0
    claude = tmp_path / "CLAUDE.md"
    agents = tmp_path / "AGENTS.md"
    assert claude.exists()
    assert agents.exists()
    assert "agentacct" in claude.read_text()
    assert "agentacct run" in claude.read_text()
    assert "Do not connect provider API keys" in claude.read_text()
    assert "sentinel_record_event" in claude.read_text()
    assert "sentinel_attach_client_context" in claude.read_text()
    assert "sentinel_record_section" in claude.read_text()
    # Sections-only work contract: templates must not re-teach task_* events.
    assert "task_started" not in claude.read_text()
    assert "task_completed" not in claude.read_text()
    assert "task_blocked" not in claude.read_text()
    assert "section_status=started" in claude.read_text()
    assert "section_status=completed" in claude.read_text()
    assert "section_status=blocked" in claude.read_text()
    assert "agentacct" in agents.read_text()
    assert "agentacct run" in agents.read_text()
    assert "sentinel_record_event" in agents.read_text()
    assert "sentinel_attach_client_context" in agents.read_text()
    assert "sentinel_record_section" in agents.read_text()
    assert "task_started" not in agents.read_text()
    assert "task_completed" not in agents.read_text()
    assert "task_blocked" not in agents.read_text()
    assert "section_status=started" in agents.read_text()
    assert "section_status=completed" in agents.read_text()
    assert "client-reported token" in agents.read_text()

    second = runner.invoke(app, ["init", "--project-dir", str(tmp_path), "--agent", "claude-code", "--agent", "codex", "--agent", "hermes", "--agent", "opencode", "--agent", "openclaw"])

    assert second.exit_code == 0
    assert claude.read_text().count("# agentacct") == 1
    assert agents.read_text().count("## agentacct") == 1


def test_init_claude_code_respects_legacy_pre_rename_section(tmp_path):
    # Pre-rename "Agent Chronicle" headings are recognized forever: an existing
    # legacy section must keep suppressing a duplicate (Chronicle) append.
    runner = CliRunner()
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("# gstack\n\n## Agent Sentinel\n\nExisting guidance.\n", encoding="utf-8")

    result = runner.invoke(app, ["init", "--project-dir", str(tmp_path), "--agent", "claude-code"])

    assert result.exit_code == 0, result.output
    assert claude.read_text().count("Agent Sentinel") == 1
    assert "agentacct" not in claude.read_text()
    assert "Existing guidance" in claude.read_text()


def test_init_claude_code_inside_temp_worktree_initializes_owner(tmp_path):
    """A temporary Claude worktree belongs to its owner: init must not create a
    vanishing worktree-local store (the old behavior only printed a hint and
    still initialized the worktree — the stray-store bug)."""
    runner = CliRunner()
    project_root = tmp_path / "game"
    worktree = project_root / ".claude" / "worktrees" / "feature-123"
    worktree.mkdir(parents=True)

    result = runner.invoke(app, ["init", "--project-dir", str(worktree), "--agent", "claude-code", "--no-mcp"])

    assert result.exit_code == 0, result.output
    assert "Claude Code worktree detected" in result.output
    unwrapped = result.output.replace("\n", "")
    assert f"Initializing the owning project instead of this temporary worktree: {project_root}" in unwrapped
    assert (project_root / ".agent-sentinel" / "state").is_dir()
    assert (project_root / ".agent-sentinel" / "policy.yaml").is_file()
    assert not (worktree / ".agent-sentinel").exists()


def test_init_inside_worktree_with_its_own_store_keeps_it(tmp_path):
    runner = CliRunner()
    project_root = tmp_path / "game"
    worktree = project_root / ".claude" / "worktrees" / "feature-456"
    (worktree / ".agent-sentinel" / "state").mkdir(parents=True)

    result = runner.invoke(app, ["init", "--project-dir", str(worktree), "--no-mcp"])

    assert result.exit_code == 0, result.output
    assert "Initializing the owning project instead" not in result.output
    assert (worktree / ".agent-sentinel" / "policy.yaml").is_file()
    assert not (project_root / ".agent-sentinel" / "policy.yaml").exists()


def test_init_agent_mcp_preview_does_not_write_config_by_default(tmp_path):
    runner = CliRunner()

    result = runner.invoke(app, ["init", "--project-dir", str(tmp_path), "--agent", "codex"])

    assert result.exit_code == 0, result.output
    assert "Codex" in result.output
    assert "codex mcp add agentacct" in result.output
    assert "Preview only" in result.output
    assert "agentacct mcp doctor" in result.output
    assert "read-only diagnostics" in result.output
    assert "mcp doctor --store-dir" not in result.output
    assert (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / ".codex" / "config.toml").exists()


def test_setup_prompt_prints_the_short_one_liner_for_every_agent():
    from agent_chronicle import install_guide

    for agent in install_guide.PROMPT_AGENTS:
        result = CliRunner().invoke(app, ["setup", "prompt", "--agent", agent])

        assert result.exit_code == 0, result.output
        # The short prompt is ONE line, identical for every agent: the
        # per-agent branching lives in INSTALL.md, not in the paste block.
        assert result.output.strip() == install_guide.ONE_LINE_PROMPT
        # The public paste line installs from PyPI and points at onboarding.
        assert "pipx install agentacct" in result.output
        assert "agentacct onboard" in result.output
        assert "never store, request, or echo any API key" in result.output


def test_setup_prompt_rejects_unknown_agent():
    result = CliRunner().invoke(app, ["setup", "prompt", "--agent", "not-a-client"])

    assert result.exit_code != 0


def test_setup_prompt_full_is_self_contained_offline_for_claude_code():
    from agent_chronicle import install_guide

    result = CliRunner().invoke(app, ["setup", "prompt", "--agent", "claude-code", "--full"])

    assert result.exit_code == 0, result.output
    # Built from the same constants INSTALL.md embeds — cannot drift.
    assert install_guide.CLAUDE_CODE_BLOCK in result.output
    assert install_guide.CLI_INSTALL_PIPX_BLOCK in result.output
    assert install_guide.CLI_INSTALL_FALLBACK_BLOCK in result.output
    for line in install_guide.CAPABILITY_MATRIX:
        assert line in result.output
    # The hook bridge is the attribution keystone and must be installed.
    assert "agentacct hooks claude-code install" in result.output
    assert "hooks claude-code doctor" in result.output
    # Doctor stays read-only: no store-dir workaround from the old prompt era.
    assert "mcp doctor --store-dir" not in result.output
    assert "NEVER store, request, or echo provider API keys" in result.output
    assert "throwaway temp venv" in result.output


def test_setup_prompt_full_for_profile_agents_does_not_promise_silent_global_writes():
    result = CliRunner().invoke(app, ["setup", "prompt", "--agent", "hermes", "--full"])

    assert result.exit_code == 0, result.output
    assert "agentacct init --agent hermes" in result.output
    assert "agentacct setup mcp --agent hermes" in result.output
    assert "--write-mcp" not in result.output
    assert "ask before modifying global agent configuration" in result.output


def test_init_can_write_supported_project_local_mcp_configs(tmp_path):
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "init",
            "--project-dir",
            str(tmp_path),
            "--agent",
            "claude-code",
            "--agent",
            "codex",
            "--write-mcp",
        ],
    )

    assert result.exit_code == 0, result.output
    expected_store = str((tmp_path / ".agent-sentinel" / "state").resolve())
    claude_config = json.loads((tmp_path / ".mcp.json").read_text())
    # Absolute store path by default; --relative-store-path restores the
    # portable relative form for configs committed across machines.
    assert claude_config["mcpServers"]["agentacct"]["args"] == ["mcp", "serve", "--store-dir", expected_store]
    codex_config = (tmp_path / ".codex" / "config.toml").read_text()
    assert "[mcp_servers.agentacct]" in codex_config
    assert expected_store in codex_config
    assert "Wrote Claude Code MCP config" in result.output
    assert "Codex MCP config block" in result.output


def test_init_write_mcp_relative_store_path_flag_keeps_portable_form(tmp_path):
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "init",
            "--project-dir",
            str(tmp_path),
            "--agent",
            "claude-code",
            "--agent",
            "codex",
            "--write-mcp",
            "--relative-store-path",
        ],
    )

    assert result.exit_code == 0, result.output
    claude_config = json.loads((tmp_path / ".mcp.json").read_text())
    assert claude_config["mcpServers"]["agentacct"]["args"] == ["mcp", "serve", "--store-dir", ".agent-sentinel/state"]
    codex_config = (tmp_path / ".codex" / "config.toml").read_text()
    assert '".agent-sentinel/state"' in codex_config
    assert str(tmp_path / ".agent-sentinel" / "state") not in codex_config


def test_init_can_write_mcp_config_with_explicit_command_path(tmp_path):
    runner = CliRunner()
    command_path = tmp_path / "venv" / "bin" / "agent-chronicle"

    result = runner.invoke(
        app,
        [
            "init",
            "--project-dir",
            str(tmp_path),
            "--agent",
            "codex",
            "--agent",
            "claude-code",
            "--write-mcp",
            "--mcp-command",
            str(command_path),
        ],
    )

    assert result.exit_code == 0, result.output
    claude_config = json.loads((tmp_path / ".mcp.json").read_text())
    assert claude_config["mcpServers"]["agentacct"]["command"] == str(command_path)
    codex_config = (tmp_path / ".codex" / "config.toml").read_text()
    assert f'command = "{command_path}"' in codex_config


def test_init_auto_uses_current_executable_when_command_is_not_on_path(tmp_path, monkeypatch):
    runner = CliRunner()
    command_path = tmp_path / "durable-venv" / "bin" / "agent-chronicle"
    command_path.parent.mkdir(parents=True)
    command_path.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr(cli_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli_module, "_current_agent_chronicle_executable", lambda: str(command_path))

    result = runner.invoke(app, ["init", "--project-dir", str(tmp_path), "--agent", "codex", "--write-mcp"])

    assert result.exit_code == 0, result.output
    assert "MCP executable" in result.output
    codex_config = (tmp_path / ".codex" / "config.toml").read_text()
    assert f'command = "{command_path}"' in codex_config


def test_init_write_mcp_keeps_profile_agents_preview_only(tmp_path):
    runner = CliRunner()

    result = runner.invoke(app, ["init", "--project-dir", str(tmp_path), "--agent", "hermes", "--write-mcp"])

    assert result.exit_code == 0, result.output
    assert "Hermes" in result.output
    assert "hermes mcp add agentacct" in result.output
    assert "project-local MCP config write is not available" in result.output
    assert not (tmp_path / ".mcp.json").exists()


def test_init_write_mcp_requires_agent(tmp_path):
    result = CliRunner().invoke(app, ["init", "--project-dir", str(tmp_path), "--write-mcp"])

    assert result.exit_code != 0
    assert "--write-mcp requires at least one --agent" in result.output


def test_init_rejects_unknown_agent_target(tmp_path):
    result = CliRunner().invoke(app, ["init", "--project-dir", str(tmp_path), "--agent", "unknown-agent"])

    assert result.exit_code != 0
    assert not (tmp_path / "unknown-agent").exists()


def test_run_and_report_default_to_project_local_state_after_init(tmp_path, monkeypatch):
    runner = CliRunner()
    runner.invoke(app, ["init", "--project-dir", str(tmp_path)])
    monkeypatch.chdir(tmp_path)
    # Exercise real project walk-up: opt out of the suite-wide env isolation.
    monkeypatch.delenv("AGENT_CHRONICLE_STORE_DIR", raising=False)

    result = runner.invoke(app, ["run", "--", sys.executable, "-c", "print('tracked from project')"])

    assert result.exit_code == 0
    state_dir = tmp_path / ".agent-sentinel" / "state"
    runs_dir = state_dir / "runs"
    run_dirs = [path for path in runs_dir.iterdir() if path.is_dir()]
    assert len(run_dirs) == 1
    assert not (tmp_path / ".agent-sentinel" / "runs").exists()
    assert "Report:" in result.output
    assert ".agent-sentinel/state/runs" in result.output

    report = runner.invoke(app, ["report", "latest"])

    assert report.exit_code == 0
    assert "tracked from project" in report.output


def test_run_and_report_find_project_local_state_from_subdirectory(tmp_path, monkeypatch):
    runner = CliRunner()
    runner.invoke(app, ["init", "--project-dir", str(tmp_path)])
    subdir = tmp_path / "pkg" / "nested"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)
    # Exercise real project walk-up: opt out of the suite-wide env isolation.
    monkeypatch.delenv("AGENT_CHRONICLE_STORE_DIR", raising=False)

    result = runner.invoke(app, ["run", "--", sys.executable, "-c", "print('tracked from nested project dir')"])

    assert result.exit_code == 0
    runs_dir = tmp_path / ".agent-sentinel" / "state" / "runs"
    assert len([path for path in runs_dir.iterdir() if path.is_dir()]) == 1

    report = runner.invoke(app, ["report", "latest"])

    assert report.exit_code == 0
    assert "tracked from nested project dir" in report.output


def test_init_force_overwrites_project_policy(tmp_path):
    runner = CliRunner()
    policy_path = tmp_path / DEFAULT_POLICY_FILE
    policy_path.parent.mkdir(parents=True)
    policy_path.write_text("custom: true\n")

    result = runner.invoke(app, ["init", "--project-dir", str(tmp_path), "--force"])

    assert result.exit_code == 0
    assert "custom: true" not in policy_path.read_text()
    assert load_policy(policy_path).budgets.max_total_usd == 0.05


def test_policy_validate_accepts_default_policy(tmp_path):
    runner = CliRunner()
    runner.invoke(app, ["init", "--project-dir", str(tmp_path)])

    result = runner.invoke(app, ["policy", "validate", "--project-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert "Policy valid" in result.output


def test_policy_validate_rejects_invalid_budget(tmp_path):
    runner = CliRunner()
    policy_path = tmp_path / DEFAULT_POLICY_FILE
    policy_path.parent.mkdir(parents=True)
    policy_path.write_text("budgets:\n  max_total_usd: -1\n")

    result = runner.invoke(app, ["policy", "validate", "--project-dir", str(tmp_path)])

    assert result.exit_code != 0
    assert "max_total_usd must be positive" in result.output


def test_doctor_reports_project_readiness_without_printing_secrets(tmp_path, monkeypatch):
    runner = CliRunner()
    runner.invoke(app, ["init", "--project-dir", str(tmp_path)])
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text(".env.local\n.env\n")
    monkeypatch.setenv("AGENT_CHRONICLE_OPENROUTER_API_KEY", "OPENROUTER_TEST_KEY_PLACEHOLDER")

    result = runner.invoke(app, ["doctor", "--project-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert "policy file" in result.output
    assert "git repository" in result.output
    assert "secret files ignored" in result.output
    assert "OpenRouter key" in result.output
    assert "Next steps:" in result.output
    assert "Try a safe local run: agentacct run -- python --version" in result.output
    assert "Open the local dashboard: agentacct serve" in result.output
    assert "Initialize project-local config" not in result.output
    assert "sk-or" not in result.output
    assert "secret-value" not in result.output


def test_doctor_gives_actionable_setup_steps_for_uninitialized_project(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.delenv("AGENT_CHRONICLE_OPENROUTER_API_KEY", raising=False)

    result = runner.invoke(app, ["doctor", "--project-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert "policy file" in result.output
    assert "Next steps:" in result.output
    assert "Initialize project-local config: agentacct init --project-dir" in result.output
    assert "Run doctor from a git repository root" in result.output
    assert "Protect local secrets" in result.output
    # Phase 3 (item 5): the missing-key mention stays neutral and optional —
    # enforcement is secondary in the observe-only alpha.
    assert "AGENT_CHRONICLE_OPENROUTER_API_KEY" in result.output
    assert "observe-only" in result.output
    assert "Optional provider forwarding" not in result.output
    assert "agentacct serve" in result.output


def test_doctor_json_reports_readiness_without_printing_secret_values(tmp_path, monkeypatch):
    runner = CliRunner()
    runner.invoke(app, ["init", "--project-dir", str(tmp_path)])
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text(".env.local\n.env\n")
    monkeypatch.setenv("AGENT_CHRONICLE_OPENROUTER_API_KEY", "OPENROUTER_TEST_KEY_PLACEHOLDER")

    result = runner.invoke(app, ["doctor", "--project-dir", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["project_dir"] == str(tmp_path.resolve())
    assert payload["ready"] is True
    assert [check["name"] for check in payload["checks"]] == ["policy file", "git repository", "secret files ignored", "OpenRouter key"]
    assert all(check["ok"] is True for check in payload["checks"])
    assert "Try a safe local run: agentacct run -- python --version" in payload["next_steps"]
    assert "OPENROUTER_TEST_KEY_PLACEHOLDER" not in result.output


def test_doctor_json_reports_uninitialized_warnings(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.delenv("AGENT_CHRONICLE_OPENROUTER_API_KEY", raising=False)

    result = runner.invoke(app, ["doctor", "--project-dir", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ready"] is False
    warnings = {check["name"]: check for check in payload["checks"] if not check["ok"]}
    assert set(warnings) == {"policy file", "git repository", "secret files ignored", "OpenRouter key"}
    assert any(step.startswith("Initialize project-local config: agentacct init --project-dir") for step in payload["next_steps"])
    assert any("agentacct serve" in step for step in payload["next_steps"])


def test_doctor_json_treats_provider_key_as_optional_for_local_readiness(tmp_path, monkeypatch):
    runner = CliRunner()
    runner.invoke(app, ["init", "--project-dir", str(tmp_path)])
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text(".env.local\n.env\n")
    monkeypatch.delenv("AGENT_CHRONICLE_OPENROUTER_API_KEY", raising=False)

    result = runner.invoke(app, ["doctor", "--project-dir", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ready"] is True
    openrouter = next(check for check in payload["checks"] if check["name"] == "OpenRouter key")
    assert openrouter["ok"] is False
    assert openrouter["status"] == "warn"


def test_doctor_json_does_not_echo_malformed_policy_source_lines(tmp_path, monkeypatch):
    runner = CliRunner()
    policy_path = tmp_path / DEFAULT_POLICY_FILE
    policy_path.parent.mkdir(parents=True)
    policy_path.write_text("x: [sk-or-secret-like-placeholder-1234567890\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text(".env.local\n.env\n")
    monkeypatch.delenv("AGENT_CHRONICLE_OPENROUTER_API_KEY", raising=False)

    result = runner.invoke(app, ["doctor", "--project-dir", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ready"] is False
    policy = next(check for check in payload["checks"] if check["name"] == "policy file")
    assert policy["details"] == "policy file could not be parsed or loaded; run agentacct policy validate for details"
    assert "sk-or-secret-like-placeholder" not in result.output
    assert "x: [" not in result.output


def test_validate_policy_model_directly_reports_errors(tmp_path):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("checkpoints:\n  every_steps: 0\n")

    policy = load_policy(policy_path)
    errors = validate_policy(policy)

    assert "checkpoints.every_steps must be positive" in errors


def test_workflow_docs_and_hermes_skill_template_are_present() -> None:
    workflow = Path("docs/agent-chronicle-workflow-instructions.md").read_text()
    integrations = Path("docs/coding-agent-integrations.md").read_text()
    skill = Path("integrations/hermes/agent-chronicle-workflow/SKILL.md").read_text()

    assert "sentinel_record_event" in workflow
    assert "sentinel_record_section" in workflow
    # Sections-only work contract: docs must not re-teach task_* events.
    assert "task_started" not in workflow
    assert "task_completed" not in workflow
    assert "task_blocked" not in workflow
    assert "section_status: started" in workflow
    assert "section_status: completed" in workflow
    assert "section_status: blocked" in workflow
    assert "task_started" not in skill
    assert "task_completed" not in skill
    assert "task_blocked" not in skill
    assert "section_status=started" in skill
    assert "Keep these claims separate" in workflow
    assert "agent-chronicle-workflow/SKILL.md" in integrations
    assert skill.startswith("---\nname: agent-chronicle-workflow")
    assert "description:" in skill
    assert "sentinel_record_machine_check" in skill
    assert "Do not say agentacct hard-stopped" in skill
