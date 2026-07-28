from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_chronicle import cli as cli_module
from agent_chronicle.activation import ActivationStateStore, RuntimeManagerError
from agent_chronicle.cli import _merge_claude_project_settings, app
from agent_chronicle.hooks import claude_code_hook_paths
from agent_chronicle.ingestion_health import IngestionHealthStore
from agent_chronicle.source_discovery import UsageSourceDiscovery


class _RuntimeProbe:
    def __init__(self) -> None:
        self.external_decisions: list[bool] = []

    @staticmethod
    def _payload() -> dict[str, object]:
        return {
            "state": "running",
            "store_dir": "/test/store",
            "dashboard_url": "http://127.0.0.1:8765/",
            "dashboard_health": "healthy",
            "watcher": "running",
            "processes": [],
            "issues": [],
        }

    def start(self, *, external_watcher_running: bool) -> dict[str, object]:
        self.external_decisions.append(external_watcher_running)
        return self._payload()

    def status(self, *, external_watcher_running: bool) -> dict[str, object]:
        self.external_decisions.append(external_watcher_running)
        return self._payload()


def _record_dead_watcher(store_dir: Path, *, lease_id: str) -> None:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=5)
    IngestionHealthStore(store_dir).acquire_watcher(
        lease_id=lease_id,
        pid=process.pid,
        importer_version="stale-test-build",
        interval_seconds=60.0,
        scan_limit=20,
        sources=("codex",),
    )


def _source(
    client: str = "codex",
    *,
    importer: str | None = "test",
) -> UsageSourceDiscovery:
    return UsageSourceDiscovery(
        client=client,
        display_name="Codex",
        status="found",
        evidence="test",
        paths=["local-test-source"],
        file_count=1,
        session_count=1,
        latest_updated_at=1,
        usage_confidence="client_reported",
        cost_confidence="unknown",
        importer=importer,
        notes=[],
    )


def test_onboard_auto_skips_detected_but_unimportable_opencode_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        cli_module,
        "discover_usage_sources",
        lambda: [_source("opencode", importer=None)],
    )
    monkeypatch.setattr(
        cli_module,
        "init_project",
        lambda **_kwargs: calls.append("init"),
    )

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--project-dir",
            str(tmp_path),
            "--agent",
            "auto",
            "--no-start",
        ],
    )

    assert result.exit_code == 1
    assert calls == []
    assert "no importable usage path is available" in result.output
    assert "OpenCode was detected but no single importable store resolved" in result.output
    assert not (tmp_path / ".agent-sentinel").exists()


def test_onboard_uses_one_absolute_store_and_requires_a_new_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, object] = {}
    monkeypatch.setattr(cli_module, "discover_usage_sources", lambda: [_source()])

    def fake_init_project(**kwargs: object) -> dict[str, str]:
        calls["init"] = kwargs
        return {"codex": "wrote"}

    def fake_import(**kwargs: object) -> dict[str, int]:
        calls["import"] = kwargs
        return {"imported_events": 1, "refreshed_events": 1}

    monkeypatch.setattr(cli_module, "init_project", fake_init_project)
    monkeypatch.setattr(cli_module, "_local_usage_import_payload", fake_import)

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--project-dir",
            str(tmp_path),
            "--agent",
            "codex",
            "--no-start",
        ],
    )

    assert result.exit_code == 0, result.output
    expected_store = (tmp_path / ".agent-sentinel" / "state").resolve()
    assert calls["init"]["project_dir"] == tmp_path.resolve()  # type: ignore[index]
    assert calls["import"]["store_dir"] == expected_store  # type: ignore[index]
    activation = ActivationStateStore(expected_store).snapshot()
    assert activation is not None
    assert activation["project_dir"] == str(tmp_path.resolve())
    assert activation["clients"] == ["codex"]
    assert "open a NEW agent session" in result.output
    assert "no provider keys" in result.output


def test_onboard_from_claude_worktree_uses_one_owner_project_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = tmp_path / "owner"
    worktree = owner / ".claude" / "worktrees" / "feature"
    worktree.mkdir(parents=True)
    calls: dict[str, object] = {}
    monkeypatch.setattr(cli_module, "discover_usage_sources", lambda: [_source()])

    def fake_init_project(**kwargs: object) -> dict[str, str]:
        calls["init"] = kwargs
        return {"codex": "wrote"}

    def fake_import(**kwargs: object) -> dict[str, int]:
        calls["import"] = kwargs
        return {"imported_events": 0, "refreshed_events": 0}

    monkeypatch.setattr(cli_module, "init_project", fake_init_project)
    monkeypatch.setattr(cli_module, "_local_usage_import_payload", fake_import)

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--project-dir",
            str(worktree),
            "--agent",
            "codex",
            "--no-start",
        ],
    )

    assert result.exit_code == 0, result.output
    expected_project = owner.resolve()
    expected_store = expected_project / ".agent-sentinel" / "state"
    assert calls["init"]["project_dir"] == expected_project  # type: ignore[index]
    assert calls["import"]["store_dir"] == expected_store  # type: ignore[index]
    activation = ActivationStateStore(expected_store).snapshot()
    assert activation is not None
    assert activation["project_dir"] == str(expected_project)
    assert not (worktree / ".agent-sentinel").exists()
    assert "share one stable store" in result.output


def test_onboard_runtime_failure_keeps_configuration_and_gives_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "discover_usage_sources", lambda: [_source()])
    monkeypatch.setattr(cli_module, "init_project", lambda **_kwargs: {"codex": "wrote"})
    monkeypatch.setattr(
        cli_module,
        "_local_usage_import_payload",
        lambda **_kwargs: {"imported_events": 0, "refreshed_events": 0},
    )
    monkeypatch.setattr(
        cli_module,
        "_managed_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeManagerError("port is occupied")),
    )

    result = CliRunner().invoke(
        app,
        ["onboard", "--project-dir", str(tmp_path), "--agent", "codex"],
    )

    assert result.exit_code == 1
    assert "managed runtime did not" in result.output
    assert "start." in result.output
    assert "port is occupied" in result.output
    assert "agentacct repair" in result.output
    assert "Traceback" not in result.output
    assert ActivationStateStore(tmp_path / ".agent-sentinel" / "state").snapshot() is not None


def test_onboard_retires_dead_external_watcher_before_starting_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_dir = tmp_path / ".agent-sentinel" / "state"
    _record_dead_watcher(store_dir, lease_id="dead-onboard")
    runtime = _RuntimeProbe()
    monkeypatch.setattr(cli_module, "discover_usage_sources", lambda: [_source()])
    monkeypatch.setattr(cli_module, "init_project", lambda **_kwargs: {"codex": "wrote"})
    monkeypatch.setattr(
        cli_module,
        "_local_usage_import_payload",
        lambda **_kwargs: {"imported_events": 0, "refreshed_events": 0},
    )
    monkeypatch.setattr(cli_module, "_managed_runtime", lambda *_args, **_kwargs: runtime)

    result = CliRunner().invoke(
        app,
        ["onboard", "--project-dir", str(tmp_path), "--agent", "codex"],
    )

    assert result.exit_code == 0, result.output
    assert runtime.external_decisions == [False]
    assert IngestionHealthStore(store_dir).snapshot()["watcher"]["state"] == "stopped"


@pytest.mark.parametrize("command", ["start", "status"])
def test_runtime_commands_reconcile_dead_external_watcher(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_dir = tmp_path / "state"
    _record_dead_watcher(store_dir, lease_id=f"dead-{command}")
    runtime = _RuntimeProbe()
    monkeypatch.setattr(cli_module, "_managed_runtime", lambda *_args, **_kwargs: runtime)

    result = CliRunner().invoke(
        app,
        [command, "--store-dir", str(store_dir), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert runtime.external_decisions == [False]
    assert IngestionHealthStore(store_dir).snapshot()["watcher"]["state"] == "stopped"
    if command == "status":
        assert json.loads(result.output)["ingestion_health"]["watcher"]["state"] == "stopped"


def test_codex_no_mcp_does_not_claim_work_recording_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "discover_usage_sources", lambda: [_source()])
    monkeypatch.setattr(cli_module, "init_project", lambda **_kwargs: {})
    monkeypatch.setattr(
        cli_module,
        "_local_usage_import_payload",
        lambda **_kwargs: {"imported_events": 0, "refreshed_events": 0},
    )

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--project-dir",
            str(tmp_path),
            "--agent",
            "codex",
            "--no-mcp",
            "--no-start",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "semantic work recording is not configured" in result.output
    assert "agentacct onboard --mcp" in result.output
    assert ActivationStateStore(tmp_path / ".agent-sentinel" / "state").snapshot() is None


def test_claude_no_mcp_does_not_claim_work_recording_is_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_module,
        "discover_usage_sources",
        lambda: [_source("claude-code")],
    )
    monkeypatch.setattr(
        cli_module,
        "_local_usage_import_payload",
        lambda **_kwargs: {"imported_events": 0, "refreshed_events": 0},
    )

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--project-dir",
            str(tmp_path),
            "--agent",
            "claude-code",
            "--no-mcp",
            "--no-start",
        ],
    )

    assert result.exit_code == 0, result.output
    normalized_output = " ".join(result.output.split())
    assert "semantic work recording requires the project MCP server" in normalized_output
    assert "semantic work recording is not configured" in result.output
    assert "Ready for a real Task." not in result.output
    assert ActivationStateStore(tmp_path / ".agent-sentinel" / "state").snapshot() is None


def test_client_without_auto_recording_adapter_never_claims_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "discover_usage_sources", lambda: [_source("hermes")])
    monkeypatch.setattr(cli_module, "init_project", lambda **_kwargs: None)
    monkeypatch.setattr(
        cli_module,
        "_local_usage_import_payload",
        lambda **_kwargs: {"imported_events": 0, "refreshed_events": 0},
    )

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--project-dir",
            str(tmp_path),
            "--agent",
            "hermes",
            "--no-start",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "One-command work recording is not available for: hermes" in result.output
    assert "semantic work recording is not configured" in result.output
    assert "Ready for a real Task." not in result.output
    assert ActivationStateStore(tmp_path / ".agent-sentinel" / "state").snapshot() is None


def test_codex_conflicting_existing_registration_never_claims_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "discover_usage_sources", lambda: [_source()])
    monkeypatch.setattr(
        cli_module,
        "_local_usage_import_payload",
        lambda **_kwargs: {"imported_events": 0, "refreshed_events": 0},
    )
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[mcp_servers.agent-sentinel]\ncommand = "custom-recorder"\nargs = ["serve-elsewhere"]\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--project-dir",
            str(tmp_path),
            "--agent",
            "codex",
            "--no-start",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "preserved a conflicting existing MCP registration" in result.output.replace("\n", "")
    assert "semantic work recording is not configured" in result.output
    assert "Ready for a real Task." not in result.output
    assert ActivationStateStore(tmp_path / ".agent-sentinel" / "state").snapshot() is None
    assert "custom-recorder" in config.read_text(encoding="utf-8")


def test_existing_but_invalid_claude_hook_never_claims_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "discover_usage_sources", lambda: [_source("claude-code")])
    monkeypatch.setattr(
        cli_module,
        "_local_usage_import_payload",
        lambda **_kwargs: {"imported_events": 0, "refreshed_events": 0},
    )
    hook, example = claude_code_hook_paths(tmp_path)
    # hook (.claude/hooks/) and example (.claude/) now share the .claude/ parent,
    # so both mkdirs must tolerate it already existing.
    hook.parent.mkdir(parents=True, exist_ok=True)
    example.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("", encoding="utf-8")
    example.write_text(
        json.dumps({"env": {"ENABLE_TOOL_SEARCH": "auto"}, "hooks": {}}),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--project-dir",
            str(tmp_path),
            "--agent",
            "claude-code",
            "--no-start",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "recording adapter could not be verified" in result.output
    assert "semantic work recording is not configured" in result.output
    assert "Ready for a real Task." not in result.output
    assert ActivationStateStore(tmp_path / ".agent-sentinel" / "state").snapshot() is None


def test_fresh_claude_hook_is_verified_before_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "discover_usage_sources", lambda: [_source("claude-code")])
    monkeypatch.setattr(
        cli_module,
        "_local_usage_import_payload",
        lambda **_kwargs: {"imported_events": 0, "refreshed_events": 0},
    )

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--project-dir",
            str(tmp_path),
            "--agent",
            "claude-code",
            "--no-start",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Ready for a real Task." in result.output
    activation = ActivationStateStore(tmp_path / ".agent-sentinel" / "state").snapshot()
    assert activation is not None
    assert activation["clients"] == ["claude-code"]


def test_claude_settings_merge_preserves_existing_keys_and_is_idempotent(tmp_path: Path) -> None:
    _hook, example = claude_code_hook_paths(tmp_path)
    example.parent.mkdir(parents=True)
    generated = {
        "env": {"AGENT_CHRONICLE_STORE_DIR": "/tmp/store"},
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "chronicle-hook"}]}
            ]
        },
    }
    example.write_text(json.dumps(generated), encoding="utf-8")
    target = tmp_path / ".claude" / "settings.local.json"
    target.write_text(
        json.dumps(
            {
                "env": {"USER_SETTING": "preserved"},
                "hooks": {
                    "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "user-hook"}]}]
                },
                "permissions": {"allow": ["Read"]},
            }
        ),
        encoding="utf-8",
    )

    first_path, first_action = _merge_claude_project_settings(tmp_path)
    second_path, second_action = _merge_claude_project_settings(tmp_path)
    merged = json.loads(target.read_text(encoding="utf-8"))

    assert first_path == second_path == target
    assert first_action == "updated"
    assert second_action == "unchanged"
    assert merged["env"]["USER_SETTING"] == "preserved"
    assert merged["env"]["AGENT_CHRONICLE_STORE_DIR"] == "/tmp/store"
    assert merged["permissions"] == {"allow": ["Read"]}
    assert len(merged["hooks"]["PreToolUse"]) == 1
    assert len(merged["hooks"]["PostToolUse"]) == 1
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_claude_settings_conflict_fails_without_overwriting(tmp_path: Path) -> None:
    _hook, example = claude_code_hook_paths(tmp_path)
    example.parent.mkdir(parents=True)
    example.write_text(
        json.dumps({"env": {"AGENT_CHRONICLE_STORE_DIR": "/expected"}, "hooks": {}}),
        encoding="utf-8",
    )
    target = tmp_path / ".claude" / "settings.local.json"
    target.write_text(
        json.dumps({"env": {"AGENT_CHRONICLE_STORE_DIR": "/different"}, "hooks": {}}),
        encoding="utf-8",
    )
    before = target.read_bytes()

    with pytest.raises(RuntimeManagerError, match="left it unchanged"):
        _merge_claude_project_settings(tmp_path)

    assert target.read_bytes() == before
