"""The "usage but no work" trap: a project store that user-scope clients do not
record into. These tests pin the read-back of user-scope MCP registrations and
the notices the CLI prints when it reads from, or creates, such a store.

HOME is redirected to a tmp dir so nothing here reads the real ~/.claude.json
or ~/.codex/config.toml.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentacct.cli import app
from agentacct.registration_stores import (
    read_store_shadow_notice,
    user_scope_registered_stores,
)
from agentacct.store_resolution import StoreResolution


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


def _write_claude_registration(home: Path, store: Path, *, key: str = "agentacct") -> None:
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "numStartups": 3,
                "mcpServers": {key: {"type": "stdio", "command": "/opt/agentacct", "args": ["mcp", "serve", "--store-dir", str(store)]}},
            }
        )
    )


def _write_codex_registration(home: Path, store: Path) -> None:
    (home / ".codex").mkdir(exist_ok=True)
    (home / ".codex" / "config.toml").write_text(
        "[mcp_servers.agentacct]\n"
        'command = "/opt/agentacct"\n'
        f'args = ["mcp", "serve", "--store-dir", {json.dumps(str(store))}]\n'
    )


def _global_store(home: Path) -> Path:
    store = home / ".local" / "state" / "agentacct" / "state"
    store.mkdir(parents=True)
    return store


def _project_resolution(project: Path) -> StoreResolution:
    return StoreResolution(
        path=project / ".agent-sentinel" / "state", source="project", project_root=project, worktree_remapped=False
    )


# --- read-back -------------------------------------------------------------


def test_reads_claude_and_codex_user_registrations(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    store = _global_store(home)
    _write_claude_registration(home, store)
    _write_codex_registration(home, store)

    rows = user_scope_registered_stores(home=home)

    assert [(row.client, row.store_dir) for row in rows] == [("claude-code", store), ("codex", store)]
    assert rows[0].config_path == home / ".claude.json"
    assert rows[1].config_path == home / ".codex" / "config.toml"
    # A pre-rename registration key still counts.
    _write_claude_registration(home, store, key="agent-sentinel")
    assert [row.client for row in user_scope_registered_stores(home=home)] == ["claude-code", "codex"]


def test_missing_malformed_and_relative_registrations_contribute_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    # No files at all.
    assert user_scope_registered_stores(home=home) == []
    # Malformed JSON / TOML never raise.
    (home / ".claude.json").write_text("{not json")
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text("[mcp_servers.agentacct\n")
    assert user_scope_registered_stores(home=home) == []
    # A relative --store-dir in user scope depends on the client's cwd: unknown, not guessed.
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"agentacct": {"args": ["mcp", "serve", "--store-dir", ".agent-sentinel/state"]}}})
    )
    (home / ".codex" / "config.toml").write_text('[mcp_servers.agentacct]\nargs = ["mcp", "serve"]\n')
    assert user_scope_registered_stores(home=home) == []


# --- notices ----------------------------------------------------------------


def test_shadow_notice_names_the_recording_store_and_the_flag(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    store = _global_store(home)
    _write_claude_registration(home, store)
    project = tmp_path / "repo"

    notice = read_store_shadow_notice(_project_resolution(project), command="tui", home=home)

    assert notice is not None
    assert f"Reading project store {project / '.agent-sentinel' / 'state'}" in notice
    assert f"Your sessions record to {store}" in notice
    assert f"agentacct tui --store-dir {store}" in notice


def test_shadow_notice_is_silent_when_registrations_match_or_are_absent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "repo"
    resolution = _project_resolution(project)
    # No registrations at all: nothing to say.
    assert read_store_shadow_notice(resolution, command="tui", home=home) is None
    # Registered against the very project store being read: nothing to say.
    _write_claude_registration(home, resolution.path)
    assert read_store_shadow_notice(resolution, command="tui", home=home) is None


def test_shadow_notice_only_applies_to_project_walk_up(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _write_claude_registration(home, _global_store(home))
    # An explicit --store-dir is the user's own choice: never second-guessed.
    resolution = StoreResolution(path=tmp_path / "chosen", source="flag", project_root=None, worktree_remapped=False)
    assert read_store_shadow_notice(resolution, command="now", home=home) is None


# --- CLI wiring -------------------------------------------------------------


def _seed_project(root: Path) -> Path:
    root.mkdir()
    (root / ".git").mkdir()
    state = root / ".agent-sentinel" / "state"
    state.mkdir(parents=True)
    return state


def test_now_prints_notice_on_stderr_and_keeps_json_stdout_clean(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _global_store(isolated_home)
    _write_claude_registration(isolated_home, store)
    project = tmp_path / "repo"
    _seed_project(project)
    monkeypatch.delenv("AGENTACCT_STORE_DIR", raising=False)
    monkeypatch.delenv("AGENT_CHRONICLE_STORE_DIR", raising=False)
    monkeypatch.chdir(project)

    result = CliRunner().invoke(app, ["now", "--json"])

    assert result.exit_code == 0, result.output
    json.loads(result.stdout)  # stdout is still machine-parseable
    assert "Your sessions record to" in result.stderr
    assert f"agentacct now --store-dir {store}" in result.stderr


def test_init_warns_before_creating_a_project_store_on_a_global_install(
    tmp_path: Path, isolated_home: Path
) -> None:
    store = _global_store(isolated_home)
    _write_claude_registration(isolated_home, store)
    project = tmp_path / "repo"
    project.mkdir()

    result = CliRunner().invoke(app, ["init", "--project-dir", str(project)])

    assert result.exit_code == 0, result.output
    assert (project / ".agent-sentinel" / "state").is_dir()
    # rich folds long paths at the console width; compare with newlines removed.
    unwrapped = result.output.replace("\n", "")
    assert "already records to" in unwrapped and str(store) in unwrapped

    # Second run: the store exists already, so there is nothing new to warn about.
    again = CliRunner().invoke(app, ["init", "--project-dir", str(project)])
    assert again.exit_code == 0, again.output
    assert "already records to" not in again.output


def test_tui_passes_notice_through_to_the_app(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("textual")
    from agentacct import cli as cli_module
    from agentacct import tui as tui_module

    store = _global_store(isolated_home)
    _write_claude_registration(isolated_home, store)
    project = tmp_path / "repo"
    _seed_project(project)
    monkeypatch.delenv("AGENTACCT_STORE_DIR", raising=False)
    monkeypatch.delenv("AGENT_CHRONICLE_STORE_DIR", raising=False)
    monkeypatch.chdir(project)
    seen: dict[str, object] = {}

    class _FakeApp:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

        def run(self) -> None:
            return None

    monkeypatch.setattr(tui_module, "AgentAcctTUI", _FakeApp)
    # The command refuses to run without a terminal, and CliRunner swaps in
    # pipes at invoke time, so call the command function directly with the tty
    # check answered the way a real terminal would.
    monkeypatch.setattr(cli_module.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli_module.sys.stdout, "isatty", lambda: True)

    cli_module.tui(store_dir=None, window="7d", client=None, refresh=5.0)

    assert seen["store_dir"] == project / ".agent-sentinel" / "state"
    assert isinstance(seen["notice"], str) and str(store) in seen["notice"]
    assert "Your sessions record to" in capsys.readouterr().err


