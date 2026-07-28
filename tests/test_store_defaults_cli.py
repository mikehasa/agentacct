"""CLI-level regression tests for the one-store-dir default (Batch 4, 4a).

Tests that exercise the resolver's own precedence opt out of the suite-wide
``AGENT_CHRONICLE_STORE_DIR`` isolation (see tests/conftest.py) with
``monkeypatch.delenv`` and chdir into a private tmp tree.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from typer.testing import CliRunner

from agent_chronicle import cli as cli_module
from agent_chronicle.cli import app
from agent_chronicle.storage import RunStore
from agent_chronicle.store_resolution import ENV_STORE_DIR

runner = CliRunner()


def _isolate(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    """Bare cwd + fake HOME + no env override: pure resolver behavior."""
    monkeypatch.delenv(ENV_STORE_DIR, raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    cwd = tmp_path / "cwd"
    cwd.mkdir(exist_ok=True)
    monkeypatch.chdir(cwd)
    return cwd, fake_home


def _init_project(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    cwd, fake_home = _isolate(monkeypatch, tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    assert runner.invoke(app, ["init", "--project-dir", str(project)]).exit_code == 0
    monkeypatch.chdir(project)
    return project, fake_home


def test_report_outside_project_fails_with_guidance(tmp_path, monkeypatch) -> None:
    _cwd, fake_home = _isolate(monkeypatch, tmp_path)

    result = runner.invoke(app, ["report", "latest"])

    assert result.exit_code == 2, result.output
    assert "No agentacct store found" in result.output
    assert "agentacct init" in result.output
    assert "--store-dir" in result.output
    assert ENV_STORE_DIR in result.output
    assert "~/.agent-sentinel" in result.output
    assert not (fake_home / ".agent-sentinel").exists()


def test_outcome_and_value_default_to_project_store(tmp_path, monkeypatch) -> None:
    """Regression for the report-vs-outcome split-brain: `run`, `outcome`, and
    `value` now resolve to the SAME project store without --store-dir."""
    project, fake_home = _init_project(monkeypatch, tmp_path)

    run = runner.invoke(app, ["run", "--", sys.executable, "-c", "print('joined store')"])
    assert run.exit_code == 0, run.output

    outcome = runner.invoke(
        app,
        ["outcome", "record-machine-check", "latest", "--name", "pytest", "--before-exit-code", "1", "--after-exit-code", "0"],
    )
    assert outcome.exit_code == 0, outcome.output

    value = runner.invoke(app, ["value", "compute", "latest", "--json"])
    assert value.exit_code == 0, value.output

    state = project / ".agent-sentinel" / "state"
    run_dirs = [path for path in (state / "runs").iterdir() if path.is_dir()]
    assert len(run_dirs) == 1
    assert (run_dirs[0] / "outcome.json").exists()
    assert not (fake_home / ".agent-sentinel").exists()


def test_subscription_set_and_estimate_share_project_store(tmp_path, monkeypatch) -> None:
    """README subscription flow regression: set + estimate see the same plan."""
    project, fake_home = _init_project(monkeypatch, tmp_path)

    saved = runner.invoke(
        app,
        ["cost", "subscription-set", "--name", "claude-code-pro", "--provider", "claude-code", "--monthly-price-usd", "200", "--period-run-budget", "100"],
    )
    assert saved.exit_code == 0, saved.output

    estimate = runner.invoke(app, ["cost", "subscription-estimate", "--subscription-name", "claude-code-pro", "--json"])

    assert estimate.exit_code == 0, estimate.output
    assert "unknown subscription plan" not in estimate.output
    assert (project / ".agent-sentinel" / "state" / "subscriptions.json").exists()
    assert not (fake_home / ".agent-sentinel").exists()


def test_demo_without_project_uses_temp_store_and_says_so(tmp_path, monkeypatch) -> None:
    _cwd, fake_home = _isolate(monkeypatch, tmp_path)

    result = runner.invoke(app, ["demo", "--json"])

    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    store_dir = Path(summary["store_dir"])
    try:
        assert summary["store_is_temporary"] is True
        assert store_dir.name.startswith("agent-chronicle-demo-")
        assert not str(store_dir).startswith(str(tmp_path / "cwd"))
        assert not (fake_home / ".agent-sentinel").exists()

        human = runner.invoke(app, ["demo"])
        assert human.exit_code == 0, human.output
        assert "Demo store is temporary" in human.output
        assert "agentacct init" in human.output
    finally:
        shutil.rmtree(store_dir, ignore_errors=True)
        for path in Path(tempfile.gettempdir()).glob("agent-chronicle-demo-*"):
            shutil.rmtree(path, ignore_errors=True)


def test_demo_env_store_dir_is_honored_and_not_temporary(tmp_path, monkeypatch) -> None:
    _cwd, _fake_home = _isolate(monkeypatch, tmp_path)
    env_store = tmp_path / "env-store"
    monkeypatch.setenv(ENV_STORE_DIR, str(env_store))

    result = runner.invoke(app, ["demo", "--json"])

    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["store_is_temporary"] is False
    assert Path(summary["store_dir"]) == env_store


def test_workflow_smoke_defaults_to_throwaway_temp_store(tmp_path, monkeypatch) -> None:
    _cwd, fake_home = _isolate(monkeypatch, tmp_path)

    result = runner.invoke(app, ["mcp", "workflow-smoke", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    store_dir = Path(payload["store_dir"])
    try:
        assert payload["ok"] is True
        assert payload["store_is_temporary"] is True
        assert store_dir.name.startswith("agent-chronicle-workflow-smoke-")
        assert not (fake_home / ".agent-sentinel").exists()
        assert not (tmp_path / "cwd" / ".agent-sentinel").exists()

        human = runner.invoke(app, ["mcp", "workflow-smoke"])
        assert human.exit_code == 0, human.output
        assert "throwaway temporary store" in human.output
    finally:
        shutil.rmtree(store_dir, ignore_errors=True)
        for path in Path(tempfile.gettempdir()).glob("agent-sentinel-workflow-smoke-*"):
            shutil.rmtree(path, ignore_errors=True)


def test_serve_resolves_store_before_uvicorn(tmp_path, monkeypatch) -> None:
    project, _fake_home = _init_project(monkeypatch, tmp_path)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: calls.append(kwargs))
    captured: list[Path] = []
    real_create = cli_module.create_local_api_app

    def _capture(*, store_dir, **kwargs):
        captured.append(Path(store_dir))
        return real_create(store_dir=store_dir, **kwargs)

    monkeypatch.setattr(cli_module, "create_local_api_app", _capture)

    inside = runner.invoke(app, ["serve"])
    assert inside.exit_code == 0, inside.output
    assert captured == [(project / ".agent-sentinel" / "state").resolve()]
    assert len(calls) == 1

    monkeypatch.chdir(tmp_path / "cwd")
    outside = runner.invoke(app, ["serve"])
    assert outside.exit_code == 2, outside.output
    assert "No agentacct store found" in outside.output
    assert len(calls) == 1  # uvicorn.run was never reached


def test_api_serve_outside_project_fails_before_uvicorn(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    calls: list[object] = []
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: calls.append(args))

    result = runner.invoke(app, ["api", "serve"])

    assert result.exit_code == 2, result.output
    assert "No agentacct store found" in result.output
    assert calls == []


def test_mcp_serve_relative_store_dir_resolves_against_project_root(tmp_path, monkeypatch) -> None:
    project, _fake_home = _init_project(monkeypatch, tmp_path)
    subdir = project / "pkg" / "nested"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)
    served: list[Path] = []
    monkeypatch.setattr(cli_module, "serve_stdio", lambda *, store_dir: served.append(Path(store_dir)))

    result = runner.invoke(app, ["mcp", "serve", "--store-dir", ".agent-sentinel/state"])

    assert result.exit_code == 0, result.output
    assert served == [(project / ".agent-sentinel" / "state").resolve()]
    assert "agentacct mcp serve: store=" in result.output
    # The legacy relative value must never be anchored to the raw client cwd.
    assert not (subdir / ".agent-sentinel").exists()


def test_mcp_serve_relative_store_dir_without_project_starts_degraded(tmp_path, monkeypatch) -> None:
    """A legacy relative --store-dir with no project anchor must NOT exit — a
    non-zero exit reads to the MCP host as a crashed server. It starts a
    degraded-but-connected session and still never creates a stray store."""
    cwd, fake_home = _isolate(monkeypatch, tmp_path)
    calls: list[dict] = []
    monkeypatch.setattr(
        cli_module,
        "serve_stdio",
        lambda *, store_dir=None, degraded_reason=None: calls.append({"store_dir": store_dir, "degraded_reason": degraded_reason}),
    )

    result = runner.invoke(app, ["mcp", "serve", "--store-dir", ".agent-sentinel/state"])

    assert result.exit_code == 0, result.output
    assert "legacy config form" in result.output
    assert "setup mcp --agent" in result.output
    assert "degraded" in result.output.lower()
    assert len(calls) == 1
    assert calls[0]["store_dir"] is None
    assert calls[0]["degraded_reason"] is not None
    assert not (cwd / ".agent-sentinel").exists()
    assert not (fake_home / ".agent-sentinel").exists()


def test_mcp_serve_without_store_dir_starts_degraded_outside_project(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    calls: list[dict] = []
    monkeypatch.setattr(
        cli_module,
        "serve_stdio",
        lambda *, store_dir=None, degraded_reason=None: calls.append({"store_dir": store_dir, "degraded_reason": degraded_reason}),
    )

    outside = runner.invoke(app, ["mcp", "serve"])
    assert outside.exit_code == 0, outside.output
    assert "No agentacct store found" in outside.output
    assert len(calls) == 1
    assert calls[0]["store_dir"] is None
    assert calls[0]["degraded_reason"] is not None

    project = tmp_path / "project"
    project.mkdir()
    assert runner.invoke(app, ["init", "--project-dir", str(project)]).exit_code == 0
    monkeypatch.chdir(project)
    inside = runner.invoke(app, ["mcp", "serve"])
    assert inside.exit_code == 0, inside.output
    assert calls[-1]["store_dir"] == (project / ".agent-sentinel" / "state").resolve()
    assert calls[-1]["degraded_reason"] is None


def test_mcp_serve_honors_env_store_dir(tmp_path, monkeypatch) -> None:
    """Pins the isolation the whole test suite relies on (tests/conftest.py)."""
    _isolate(monkeypatch, tmp_path)
    env_store = tmp_path / "env-store"
    monkeypatch.setenv(ENV_STORE_DIR, str(env_store))
    served: list[Path] = []
    monkeypatch.setattr(cli_module, "serve_stdio", lambda *, store_dir: served.append(Path(store_dir)))

    result = runner.invoke(app, ["mcp", "serve"])

    assert result.exit_code == 0, result.output
    assert served == [env_store]


def test_report_from_claude_worktree_resolves_owner_store_with_notice(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(ENV_STORE_DIR, raising=False)
    owner = tmp_path / "owner"
    owner.mkdir()
    assert runner.invoke(app, ["init", "--project-dir", str(owner)]).exit_code == 0
    (owner / ".git").mkdir()
    run_dir = owner / ".agent-sentinel" / "state" / "runs" / "run_owner_demo"
    run_dir.mkdir(parents=True)
    (run_dir / "report.md").write_text("owner store report", encoding="utf-8")
    worktree = owner / ".claude" / "worktrees" / "feature-x"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    monkeypatch.chdir(worktree)

    result = runner.invoke(app, ["report", "latest"])

    assert result.exit_code == 0, result.output
    assert "owner store report" in result.output
    assert "Claude worktree detected" in result.output


def test_cli_source_has_no_home_default_help_text() -> None:
    source = Path(cli_module.__file__).read_text(encoding="utf-8")

    assert "Defaults to ~/.agent-sentinel" not in source
    assert "otherwise ~/.agent-sentinel" not in source
    assert "_default_project_store_dir" not in source


def test_runstore_requires_root_and_create_false_skips_mkdir(tmp_path) -> None:
    try:
        RunStore(None)  # type: ignore[arg-type]
    except ValueError as exc:
        assert "resolve_store_dir" in str(exc)
    else:  # pragma: no cover - defense in depth
        raise AssertionError("RunStore(None) must raise ValueError")

    read_only_root = tmp_path / "existing"
    read_only_root.mkdir()
    RunStore(read_only_root, create=False)
    assert not (read_only_root / "runs").exists()

    RunStore(read_only_root)
    assert (read_only_root / "runs").is_dir()


def test_mcp_serve_relative_store_dir_without_project_honors_env_store(tmp_path, monkeypatch) -> None:
    """Minor fix (Phase 1 review): when a legacy relative --store-dir cannot be
    anchored to any project root, a valid absolute AGENT_CHRONICLE_STORE_DIR
    wins outright — the user explicitly named the store."""
    cwd, fake_home = _isolate(monkeypatch, tmp_path)
    env_store = tmp_path / "env-store"
    monkeypatch.setenv(ENV_STORE_DIR, str(env_store))
    served: list[Path] = []
    monkeypatch.setattr(cli_module, "serve_stdio", lambda *, store_dir: served.append(Path(store_dir)))

    result = runner.invoke(app, ["mcp", "serve", "--store-dir", ".agent-sentinel/state"])

    assert result.exit_code == 0, result.output
    assert served == [env_store]
    assert ENV_STORE_DIR in result.output
    # The legacy relative value must never be anchored to the raw client cwd.
    assert not (cwd / ".agent-sentinel").exists()
    assert not (fake_home / ".agent-sentinel").exists()


def test_mcp_serve_relative_store_dir_refusal_mentions_env_option(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    calls: list[dict] = []
    monkeypatch.setattr(
        cli_module,
        "serve_stdio",
        lambda *, store_dir=None, degraded_reason=None: calls.append({"store_dir": store_dir, "degraded_reason": degraded_reason}),
    )

    result = runner.invoke(app, ["mcp", "serve", "--store-dir", ".agent-sentinel/state"])

    assert result.exit_code == 0, result.output
    assert "legacy config form" in result.output
    assert "setup mcp --agent" in result.output
    assert f"set {ENV_STORE_DIR}=<absolute path>" in result.output
    assert calls[0]["degraded_reason"] is not None
    assert calls[0]["store_dir"] is None


def test_mcp_serve_relative_store_dir_refusal_flags_relative_env_value(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv(ENV_STORE_DIR, "relative/env-store")
    calls: list[dict] = []
    monkeypatch.setattr(
        cli_module,
        "serve_stdio",
        lambda *, store_dir=None, degraded_reason=None: calls.append({"store_dir": store_dir, "degraded_reason": degraded_reason}),
    )

    result = runner.invoke(app, ["mcp", "serve", "--store-dir", ".agent-sentinel/state"])

    assert result.exit_code == 0, result.output
    assert "not an absolute path" in result.output
    assert calls[0]["degraded_reason"] is not None
    assert calls[0]["store_dir"] is None


def test_version_flag_prints_installed_version_and_exits() -> None:
    """F5 (clean-machine finding): `agentacct --version` prints the installed
    version and exits 0 without needing a subcommand — a new user's instinct to
    check the version must not fall through to the help text."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("agentacct "), result.output
    token = result.output.split("agentacct ", 1)[1].strip()
    assert token, "version token is empty"


def test_version_flag_does_not_change_help_or_break_subcommands() -> None:
    """The added --version callback must not change the app-level help text or
    stop `agentacct <subcommand>` from resolving."""
    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0, help_result.output
    assert "Local-first Agent Work Intelligence" in help_result.output
    # A known subcommand still resolves (its own help renders, exit 0).
    sub = runner.invoke(app, ["serve", "--help"])
    assert sub.exit_code == 0, sub.output
