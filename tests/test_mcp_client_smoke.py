from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_chronicle.cli import app
from agent_chronicle.mcp import SentinelMCPServer
from agent_chronicle.mcp_client_smoke import MCPClientSmokeError, assert_deepseek_mcp_client_smoke_passed, run_deepseek_mcp_client_smoke


def test_deepseek_mcp_client_smoke_requires_acknowledgement(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "DEEPSEEK_TEST_KEY_PLACEHOLDER")

    with pytest.raises(MCPClientSmokeError, match="real client smoke is disabled"):
        run_deepseek_mcp_client_smoke("opencode", repo_dir=tmp_path, acknowledge_real_api=False)


def test_deepseek_mcp_client_smoke_requires_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(MCPClientSmokeError, match="missing required environment variable"):
        run_deepseek_mcp_client_smoke("opencode", repo_dir=tmp_path, acknowledge_real_api=True)


def test_mcp_client_smoke_cli_is_disabled_by_default(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "DEEPSEEK_TEST_KEY_PLACEHOLDER")

    result = CliRunner().invoke(app, ["smoke", "mcp-client", "--client", "opencode", "--json"])

    assert result.exit_code != 0
    assert "real client smoke is disabled" in result.output


def test_mcp_client_smoke_command_is_documented() -> None:
    readme = Path("README.md").read_text()
    guide = Path("docs/coding-agent-integrations.md").read_text()

    # README and the integrations guide both use the public agentacct CLI for
    # current recommendations (the guide keeps its dated `agent-sentinel` evidence).
    assert "agentacct smoke mcp-client --client hermes" in readme
    assert "agentacct smoke mcp-client --client hermes" in guide
    for text in (readme, guide):
        assert "--i-understand-this-uses-real-api" in text


def test_opencode_deepseek_mcp_client_smoke_with_fake_runner_records_events(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "DEEPSEEK_TEST_KEY_PLACEHOLDER")
    calls: list[list[str]] = []
    store_holder: dict[str, Path] = {}

    def fake_runner(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["opencode", "mcp", "add"]:
            store_holder["store"] = Path(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, stdout="server connected", stderr="")
        if cmd and cmd[0] == "opencode" and cmd[1] == "run":
            store = store_holder["store"]
            server = SentinelMCPServer(store_dir=store)
            for section_status in ["started", "completed"]:
                server.handle_message(
                    {
                        "jsonrpc": "2.0",
                        "id": section_status,
                        "method": "tools/call",
                        "params": {
                            "name": "sentinel_record_section",
                            "arguments": {
                                "source": "opencode-deepseek-smoke",
                                "section_id": "mcp-client-smoke",
                                "section_status": section_status,
                                "run_id": "fake-opencode-smoke",
                                "summary": "MCP workflow ledger works for opencode.",
                            },
                        },
                    }
                )
            stdout = "\n".join(
                [
                    json.dumps({"type": "step_finish", "part": {"type": "step-finish", "tokens": {"input": 10, "output": 2}, "cost": 0.00001}}),
                    "AGENT_CHRONICLE_OPENCODE_DEEPSEEK_MCP_OK",
                ]
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected command")

    result = run_deepseek_mcp_client_smoke(
        "opencode",
        repo_dir=tmp_path,
        store_dir=tmp_path / "state",
        acknowledge_real_api=True,
        runner=fake_runner,
    )

    assert_deepseek_mcp_client_smoke_passed(result)
    assert result.client == "opencode"
    assert result.status == "passed"
    assert result.marker_found is True
    assert result.event_count == 2
    assert set(result.events) == {"section_started", "section_completed"}
    assert result.by_source == {"opencode-deepseek-smoke": 2}
    assert result.token_cost_observed is True
    assert any(call[:3] == ["opencode", "mcp", "add"] for call in calls)
    assert any(call[:2] == ["opencode", "run"] and "--format" in call and "json" in call for call in calls)


def test_openclaw_smoke_uses_isolated_profile_and_custom_deepseek_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "DEEPSEEK_TEST_KEY_PLACEHOLDER")
    calls: list[list[str]] = []
    store_holder: dict[str, Path] = {}

    def fake_runner(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[0] == "openclaw" and "mcp" in cmd and "add" in cmd:
            store_holder["store"] = Path(cmd[-2]) if cmd[-1] == "--no-probe" else Path(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, stdout="saved", stderr="")
        if cmd[0] == "openclaw" and "config" in cmd and "models.providers.deepseek" in cmd:
            provider = json.loads(cmd[-2])
            assert provider["baseUrl"] == "https://api.deepseek.com/v1"
            assert provider["apiKey"]["id"] == "DEEPSEEK_API_KEY"
            return subprocess.CompletedProcess(cmd, 0, stdout="updated", stderr="")
        if cmd[0] == "openclaw" and "agent" in cmd:
            server = SentinelMCPServer(store_dir=store_holder["store"])
            for section_status in ["started", "completed"]:
                server.handle_message(
                    {
                        "jsonrpc": "2.0",
                        "id": section_status,
                        "method": "tools/call",
                        "params": {
                            "name": "sentinel_record_section",
                            "arguments": {
                                "source": "openclaw-deepseek-smoke",
                                "section_id": "mcp-client-smoke",
                                "section_status": section_status,
                                "run_id": "fake-openclaw-smoke",
                            },
                        },
                    }
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="AGENT_CHRONICLE_OPENCLAW_DEEPSEEK_MCP_OK", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected command")

    result = run_deepseek_mcp_client_smoke(
        "openclaw",
        repo_dir=tmp_path,
        store_dir=tmp_path / "state",
        acknowledge_real_api=True,
        runner=fake_runner,
    )

    assert_deepseek_mcp_client_smoke_passed(result)
    assert result.isolated_profile is not None
    assert set(result.events) == {"section_started", "section_completed"}
    assert result.by_source == {"openclaw-deepseek-smoke": 2}
    assert any(call[0] == "openclaw" and "models.providers.deepseek" in call for call in calls)
