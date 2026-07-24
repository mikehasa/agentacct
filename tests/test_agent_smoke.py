from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent_chronicle.agent_smoke import AgentSmokeError, assert_live_agent_smoke_passed, build_live_agent_smoke_spec, run_live_agent_smoke


def test_live_agent_smoke_harness_verifies_sentinel_artifacts(tmp_path):
    marker = build_live_agent_smoke_spec("claude-code").marker
    store_dir = tmp_path / "state"
    work_dir = tmp_path / "work"
    command = [sys.executable, "-c", f"print({marker!r})"]

    summary = run_live_agent_smoke("claude-code", store_dir=store_dir, work_dir=work_dir, command_override=command)
    assert_live_agent_smoke_passed(summary)

    assert summary.agent == "claude-code"
    assert summary.marker_found is True
    assert summary.metadata_ok is True
    assert summary.status == "completed"
    assert summary.exit_code == 0
    assert Path(summary.stdout_log).read_text().strip() == marker
    metadata = json.loads(Path(summary.metadata_json).read_text())
    assert metadata["owned_by_sentinel"] is True
    assert metadata["cwd"] == str(work_dir)
    assert metadata["command"] == command
    assert Path(summary.report_md).exists()
    assert Path(summary.stderr_log).exists()


def test_live_agent_smoke_fails_when_marker_missing(tmp_path):
    command = [sys.executable, "-c", "print('wrong marker')"]

    summary = run_live_agent_smoke("codex", store_dir=tmp_path / "state", work_dir=tmp_path / "work", command_override=command)

    assert summary.marker_found is False
    with pytest.raises(AgentSmokeError, match="Expected marker not found"):
        assert_live_agent_smoke_passed(summary)


def test_live_agent_smoke_missing_binary_fails_before_run(tmp_path):
    with pytest.raises(AgentSmokeError, match="Missing required executable"):
        run_live_agent_smoke(
            "claude-code",
            store_dir=tmp_path / "state",
            work_dir=tmp_path / "work",
            command_override=["definitely-not-a-real-agent-chronicle-test-binary"],
        )


def test_live_agent_smoke_specs_use_portable_minimal_commands() -> None:
    claude = build_live_agent_smoke_spec("claude-code")
    codex = build_live_agent_smoke_spec("codex")

    assert claude.command == ["claude", "-p", "Reply with exactly: AGENT_CHRONICLE_CLAUDE_WRAP_OK"]
    assert "--max-turns" not in claude.command
    assert codex.command[:5] == ["codex", "exec", "--sandbox", "read-only", "--ephemeral"]
    assert "--skip-git-repo-check" in codex.command
    assert codex.marker == "AGENT_CHRONICLE_CODEX_WRAP_OK"
