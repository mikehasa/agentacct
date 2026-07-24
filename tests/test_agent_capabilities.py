from __future__ import annotations

import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agent_chronicle.agent_capabilities import (
    CAPABILITY_NAMES,
    agent_capability_manifest,
    validate_agent_capability_manifest,
)
from agent_chronicle.api import create_local_api_app
from agent_chronicle.capture import DEFAULT_CAPTURE_REGISTRY
from agent_chronicle.cli import app
from agent_chronicle.client_usage import SUPPORTED_CLIENTS, USAGE_EVENT_CLIENTS
from agent_chronicle.evidence_html import render_agent_capability_manifest_body
from agent_chronicle.usage_cube import KNOWN_USAGE_CLIENTS


def _client_rows() -> dict[str, dict]:
    return {row["client"]: row for row in agent_capability_manifest()["clients"]}


def test_manifest_has_stable_independent_lanes_and_no_supported_badge() -> None:
    manifest = agent_capability_manifest()
    rows = manifest["clients"]

    assert [row["client"] for row in rows] == [
        "claude-code",
        "codex",
        "hermes",
        "opencode",
        "openclaw",
        "cursor",
        "gemini-cli",
        "github-copilot-cli",
        "cline",
        "windsurf",
        "aider",
    ]
    assert all(tuple(row["capabilities"]) == CAPABILITY_NAMES for row in rows)
    assert all("supported" not in row for row in rows)
    assert "supported" not in json.dumps(manifest).lower()


def test_manifest_matches_usage_and_capture_registries_without_promoting_cursor() -> None:
    rows = _client_rows()
    usage_clients = {
        client
        for client, row in rows.items()
        if row["capabilities"]["usage_import"]["state"] != "unavailable"
    }
    capture_clients = {
        client
        for client, row in rows.items()
        if row["capabilities"]["mechanical_capture"]["state"] != "unavailable"
    }

    assert usage_clients == set(USAGE_EVENT_CLIENTS) == set(KNOWN_USAGE_CLIENTS)
    assert set(SUPPORTED_CLIENTS) == {*USAGE_EVENT_CLIENTS, "cursor"}
    assert capture_clients == set(DEFAULT_CAPTURE_REGISTRY.vendors())
    assert rows["cursor"]["capabilities"]["usage_import"]["state"] == "unavailable"


def test_hermes_manifest_matches_observation_and_namespace_fixture_scope() -> None:
    hermes = _client_rows()["hermes"]

    assert hermes["zero_usage_observation"] == "verified"
    assert hermes["namespace_hardening"] == "namespaced_fail_closed"
    discovery = hermes["capabilities"]["session_discovery"]
    assert discovery["state"] == "experimental"
    assert discovery["verification"]["level"] == "synthetic_fixture"
    assert discovery["verification"]["evidence_refs"] == [
        "tests/test_client_usage.py::test_hermes_diagnostics_count_prelimit_rows_and_observe_zero_usage",
        "tests/test_client_usage.py::test_hermes_multiple_env_homes_fail_closed_until_explicit_selection",
    ]
    assert "fixture" in " ".join(discovery["limitations"]).lower()


def test_cursor_manifest_is_observation_only_and_single_version_bounded() -> None:
    cursor = _client_rows()["cursor"]

    assert cursor["zero_usage_observation"] == "verified"
    assert cursor["namespace_hardening"] == "namespaced_fail_closed"
    assert cursor["verified_stability"]["level"] == "single_machine_live_observation"
    assert cursor["verified_stability"]["client_versions"] == ["3.9.16"]
    discovery = cursor["capabilities"]["session_discovery"]
    assert discovery["state"] == "verified_partial"
    assert discovery["verification"]["level"] == "live_smoke"
    assert discovery["verification"]["client_versions"] == ["3.9.16"]
    assert cursor["capabilities"]["model_attribution"]["state"] == "verified_partial"
    for capability in ("usage_import", "cache_read", "cache_write"):
        row = cursor["capabilities"][capability]
        assert row["state"] == "unavailable"
        assert row["usage_basis"] == "unknown"
        assert row["cost_basis"] == "unknown"
    assert "multi-version stability is not claimed" in json.dumps(cursor)
    assert discovery["verification"]["evidence_refs"] == [
        "docs/adapter-capability-evidence.md#2026-07-17-cursor-3916-primary-state-observation"
    ]


def test_claude_one_command_install_is_scoped_to_onboard_and_fixture_only() -> None:
    rows = _client_rows()
    capability = rows["claude-code"]["capabilities"]["automatic_install"]

    assert capability["state"] == "experimental"
    assert capability["activation"] == "one_command_project"
    assert "agentacct onboard" in capability["scope"]
    assert capability["verification"]["level"] == "synthetic_fixture"
    assert capability["verification"]["evidence_refs"] == [
        "tests/test_activation_cli.py::test_fresh_claude_hook_is_verified_before_ready"
    ]
    assert any("init --write-mcp" in value for value in capability["limitations"])
    for client in (
        "hermes",
        "opencode",
        "openclaw",
        "cursor",
        "gemini-cli",
        "github-copilot-cli",
        "cline",
        "windsurf",
        "aider",
    ):
        assert rows[client]["capabilities"]["automatic_install"]["state"] == "unavailable"


def test_manifest_validator_rejects_overclaims() -> None:
    missing_evidence = agent_capability_manifest()
    capability = missing_evidence["clients"][0]["capabilities"]["session_discovery"]
    capability["verification"]["evidence_refs"] = []
    with pytest.raises(ValueError, match="dated evidence refs"):
        validate_agent_capability_manifest(missing_evidence)

    synthetic_verified = agent_capability_manifest()
    capability = synthetic_verified["clients"][0]["capabilities"]["session_discovery"]
    capability["verification"]["level"] = "synthetic_fixture"
    with pytest.raises(ValueError, match="requires real evidence"):
        validate_agent_capability_manifest(synthetic_verified)

    activated_unavailable = agent_capability_manifest()
    capability = activated_unavailable["clients"][-1]["capabilities"]["usage_import"]
    capability["activation"] = "manual_profile"
    with pytest.raises(ValueError, match="unavailable state"):
        validate_agent_capability_manifest(activated_unavailable)

    mcp_usage_claim = agent_capability_manifest()
    mcp_usage_claim["clients"][0]["capabilities"]["mcp_semantics"]["usage_basis"] = "client_reported"
    with pytest.raises(ValueError, match="cannot claim usage or cost basis"):
        validate_agent_capability_manifest(mcp_usage_claim)

    manual_automatic_install = agent_capability_manifest()
    capability = manual_automatic_install["clients"][0]["capabilities"]["automatic_install"]
    capability["activation"] = "manual_profile"
    with pytest.raises(ValueError, match="cannot label a manual path automatic"):
        validate_agent_capability_manifest(manual_automatic_install)


def test_manifest_returns_a_defensive_copy() -> None:
    first = agent_capability_manifest()
    first["clients"][0]["display_name"] = "MUTATED"

    assert agent_capability_manifest()["clients"][0]["display_name"] == "Claude Code"


def test_agent_capability_api_is_static_read_only_and_path_safe(tmp_path) -> None:
    state = tmp_path / "state"
    client = TestClient(create_local_api_app(store_dir=state))
    before = sorted(path.relative_to(state) for path in state.rglob("*")) if state.exists() else []

    response = client.get("/capabilities/agents")

    assert response.status_code == 200
    assert response.json() == agent_capability_manifest()
    after = sorted(path.relative_to(state) for path in state.rglob("*")) if state.exists() else []
    assert after == before
    assert str(tmp_path) not in response.text
    assert client.get("/usage/sources").json()["sources"] == []
    assert client.get("/capabilities/agents").json() == response.json()


def test_agent_capability_cli_emits_machine_readable_manifest() -> None:
    result = CliRunner().invoke(app, ["capabilities", "agents", "--json"])

    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed == agent_capability_manifest()
    validate_agent_capability_manifest(parsed)


def test_agent_capability_cli_human_output_keeps_states_distinct() -> None:
    result = CliRunner().invoke(app, ["capabilities", "agents"])

    assert result.exit_code == 0, result.output
    assert "verified" in result.output
    assert "limited" in result.output
    assert "experimental" in result.output
    assert "unavailable" in result.output


def test_advanced_dashboard_renders_manifest_and_keeps_runtime_wording_honest(tmp_path) -> None:
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))

    advanced = client.get("/advanced").text
    home = client.get("/").text
    section = advanced[advanced.index('id="agent-capability-coverage"') : advanced.index("Evidence projections")]
    raw = client.get("/raw").text

    assert "Agent capability coverage" in section
    assert "Limited verified path" in section
    assert "Experimental" in section
    assert "Unavailable" in section
    for name in ("Claude Code", "Codex", "Hermes", "OpenCode", "OpenClaw", "Cursor", "Gemini CLI"):
        assert name in section
    assert "support badge" in section
    assert "Limit:" in section
    assert "Evidence:" in section
    assert "Stability:" in section
    assert "Roadmap only" in section
    assert "Aider" in section
    assert "Connected" not in section
    assert "Known usage sources not detected" in raw
    assert "Supported but not detected" not in raw
    rendered_product_copy = "\n".join((home, advanced, raw)).lower()
    for whole_client_phrase in (
        "supported local agents",
        "supported local tools",
        "connect a supported coding agent",
    ):
        assert whole_client_phrase not in rendered_product_copy


def test_capability_renderer_escapes_scope_and_agent_labels() -> None:
    manifest = deepcopy(agent_capability_manifest())
    manifest["clients"][0]["display_name"] = '<script id="agent-canary">'
    manifest["clients"][0]["capabilities"]["usage_import"]["scope"] = '<img src=x onerror="scope-canary">'

    html = render_agent_capability_manifest_body(manifest)

    assert '<script id="agent-canary">' not in html
    assert '<img src=x onerror="scope-canary">' not in html
    assert "&lt;script id=&quot;agent-canary&quot;&gt;" in html
    assert "&lt;img src=x onerror=&quot;scope-canary&quot;&gt;" in html
