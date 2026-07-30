"""Tests for the static capability manifest and its integrity validator.

The manifest is a table of "what agentacct can do with each coding agent, how
strongly, and what evidence proves it" (see ``agent_capabilities.py`` for the
full model and vocabulary).  These tests cover:

- manifest structure (all clients, all 8 lanes, no ``supported`` badge);
- cross-registry consistency (manifest matches the usage/capture client lists);
- per-client evidence (specific agents' states and evidence refs);
- the validator's rejection rules (every overclaim path pinned);
- the public surfaces (API endpoint, CLI command, dashboard HTML, XSS escape).

How to read the validator tests: each mutates ONE field of an otherwise-valid
manifest and asserts ``ValueError`` with the expected message substring.
"""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentacct.agent_capabilities import (
    CAPABILITY_NAMES,
    agent_capability_manifest,
    validate_agent_capability_manifest,
)
from agentacct.api import create_local_api_app
from agentacct.capture import DEFAULT_CAPTURE_REGISTRY
from agentacct.cli import app
from agentacct.client_usage import SUPPORTED_CLIENTS, USAGE_EVENT_CLIENTS
from agentacct.evidence_html import render_agent_capability_manifest_body
from agentacct.usage_cube import KNOWN_USAGE_CLIENTS


def _client_rows() -> dict[str, dict]:
    """Return ``{client_id: client_entry}`` from the canonical manifest."""
    return {row["client"]: row for row in agent_capability_manifest()["clients"]}


# ---------------------------------------------------------------------------
# Manifest structure: every client, every lane, no "supported" badge.
# ---------------------------------------------------------------------------


def test_manifest_has_stable_independent_lanes_and_no_supported_badge() -> None:
    """All clients appear in a stable order, each with the 8 canonical lanes, and no whole-client supported boolean."""
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
    """Clients with usage/capture must match the usage + capture registries exactly."""
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


# ---------------------------------------------------------------------------
# Per-client evidence: specific agents' states and verification refs.
# ---------------------------------------------------------------------------


def test_hermes_manifest_matches_observation_and_namespace_fixture_scope() -> None:
    """Hermes uses synthetic fixtures and a namespaced fail-closed observation path."""
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
    """Cursor is observation-only, single-version bounded, with unavailable usage/cache."""
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
    """Claude Code's auto-install is experimental (fixture), scoped to onboard, one command."""
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
    """Five core overclaim scenarios the validator must catch (verified-without-evidence, synthetic-verified, unavailable-activated, mcp-usage, manual-auto)."""
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


# ---------------------------------------------------------------------------
# Public surfaces: API, CLI, dashboard, security.
# ---------------------------------------------------------------------------


def test_manifest_returns_a_defensive_copy() -> None:
    """Mutating a returned manifest must not affect future calls (deep copy)."""
    first = agent_capability_manifest()
    first["clients"][0]["display_name"] = "MUTATED"

    assert agent_capability_manifest()["clients"][0]["display_name"] == "Claude Code"


def test_agent_capability_api_is_static_read_only_and_path_safe(tmp_path) -> None:
    """The /capabilities/agents endpoint serves the manifest without writing to disk or leaking paths."""
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
    """``capabilities agents --json`` emits the exact manifest, valid for machine parsing."""
    result = CliRunner().invoke(app, ["capabilities", "agents", "--json"])

    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed == agent_capability_manifest()
    validate_agent_capability_manifest(parsed)


def test_agent_capability_cli_human_output_keeps_states_distinct() -> None:
    """Human-readable CLI output shows all four state words distinctly."""
    result = CliRunner().invoke(app, ["capabilities", "agents"])

    assert result.exit_code == 0, result.output
    assert "verified" in result.output
    assert "limited" in result.output
    assert "experimental" in result.output
    assert "unavailable" in result.output


def test_advanced_dashboard_renders_manifest_and_keeps_runtime_wording_honest(tmp_path) -> None:
    """The dashboard renders the full matrix and never uses whole-client "supported" language."""
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
    """The HTML renderer must HTML-escape agent names and scope text (XSS defense)."""
    manifest = deepcopy(agent_capability_manifest())
    manifest["clients"][0]["display_name"] = '<script id="agent-canary">'
    manifest["clients"][0]["capabilities"]["usage_import"]["scope"] = '<img src=x onerror="scope-canary">'

    html = render_agent_capability_manifest_body(manifest)

    assert '<script id="agent-canary">' not in html
    assert '<img src=x onerror="scope-canary">' not in html
    assert "&lt;script id=&quot;agent-canary&quot;&gt;" in html
    assert "&lt;img src=x onerror=&quot;scope-canary&quot;&gt;" in html


# ---------------------------------------------------------------------------
# validate_agent_capability_manifest: every rejection rule pinned.
#
# The validator is the manifest's integrity guarantee -- it stops the published
# capability matrix from overclaiming. Each case mutates ONE field of an
# otherwise-valid manifest and asserts the validator rejects it for the right
# reason. A fresh deep copy is used per case so cases stay independent.
# ---------------------------------------------------------------------------


def _valid_manifest() -> dict:
    """A fresh deep copy of the canonical manifest; safe to mutate per case."""
    return agent_capability_manifest()


def test_validator_rejects_malformed_manifest_and_client_shape() -> None:
    """Top-level manifest shape and the per-client row contract."""
    bad_schema = _valid_manifest()
    bad_schema["schema_version"] = "agentacct.agent-capabilities.v999"
    with pytest.raises(ValueError, match="schema version"):
        validate_agent_capability_manifest(bad_schema)

    not_a_list = _valid_manifest()
    not_a_list["clients"] = {}
    with pytest.raises(ValueError, match="clients must be a list"):
        validate_agent_capability_manifest(not_a_list)

    row_not_object = _valid_manifest()
    row_not_object["clients"][0] = "not-a-mapping"
    with pytest.raises(ValueError, match="row must be an object"):
        validate_agent_capability_manifest(row_not_object)

    extra_client_field = _valid_manifest()
    extra_client_field["clients"][0]["unexpected"] = True
    with pytest.raises(ValueError, match="canonical fields"):
        validate_agent_capability_manifest(extra_client_field)

    duplicate_client_id = _valid_manifest()
    duplicate_client_id["clients"][1]["client"] = duplicate_client_id["clients"][0]["client"]
    with pytest.raises(ValueError, match="unique non-empty"):
        validate_agent_capability_manifest(duplicate_client_id)


def test_validator_rejects_bad_client_metadata() -> None:
    """Per-client metadata: roadmap phase, zero-usage, namespace, stability, capability set."""
    bad_phase = _valid_manifest()
    bad_phase["clients"][0]["roadmap_phase"] = "bogus"
    with pytest.raises(ValueError, match="roadmap phase"):
        validate_agent_capability_manifest(bad_phase)

    bad_zero_usage = _valid_manifest()
    bad_zero_usage["clients"][0]["zero_usage_observation"] = "bogus"
    with pytest.raises(ValueError, match="zero-usage observation"):
        validate_agent_capability_manifest(bad_zero_usage)

    bad_namespace = _valid_manifest()
    bad_namespace["clients"][0]["namespace_hardening"] = "bogus"
    with pytest.raises(ValueError, match="namespace hardening"):
        validate_agent_capability_manifest(bad_namespace)

    bad_stability = _valid_manifest()
    bad_stability["clients"][0]["verified_stability"]["level"] = "bogus"
    with pytest.raises(ValueError, match="verified stability"):
        validate_agent_capability_manifest(bad_stability)

    # A dated client-smoke stability claim must name at least one client version.
    smoke_no_versions = _valid_manifest()
    smoke_no_versions["clients"][0]["verified_stability"] = {
        "level": "dated_mcp_smoke_only",
        "verified_at": "2026-07-17",
        "client_versions": [],
        "evidence_refs": ["docs/adapter-capability-evidence.md"],
        "limitations": [],
    }
    with pytest.raises(ValueError, match="dated client smoke requires a client version"):
        validate_agent_capability_manifest(smoke_no_versions)

    wrong_capability_set = _valid_manifest()
    wrong_capability_set["clients"][0]["capabilities"]["extra_capability"] = {}
    with pytest.raises(ValueError, match="canonical capability set"):
        validate_agent_capability_manifest(wrong_capability_set)


def test_validator_rejects_bad_capability_fields() -> None:
    """Each capability must use the canonical fields with valid enum values."""
    capability_not_object = _valid_manifest()
    capability_not_object["clients"][0]["capabilities"]["session_discovery"] = "not-a-mapping"
    with pytest.raises(ValueError, match="must be an object"):
        validate_agent_capability_manifest(capability_not_object)

    extra_capability_field = _valid_manifest()
    extra_capability_field["clients"][0]["capabilities"]["session_discovery"]["extra"] = True
    with pytest.raises(ValueError, match="canonical capability fields"):
        validate_agent_capability_manifest(extra_capability_field)

    bad_state = _valid_manifest()
    bad_state["clients"][0]["capabilities"]["session_discovery"]["state"] = "bogus"
    with pytest.raises(ValueError, match="invalid state"):
        validate_agent_capability_manifest(bad_state)

    bad_activation = _valid_manifest()
    bad_activation["clients"][0]["capabilities"]["session_discovery"]["activation"] = "bogus"
    with pytest.raises(ValueError, match="invalid activation"):
        validate_agent_capability_manifest(bad_activation)

    empty_scope = _valid_manifest()
    empty_scope["clients"][0]["capabilities"]["session_discovery"]["scope"] = "  "
    with pytest.raises(ValueError, match="bounded scope"):
        validate_agent_capability_manifest(empty_scope)

    bad_verification_level = _valid_manifest()
    bad_verification_level["clients"][0]["capabilities"]["session_discovery"]["verification"]["level"] = "bogus"
    with pytest.raises(ValueError, match="invalid verification"):
        validate_agent_capability_manifest(bad_verification_level)

    bad_usage_basis = _valid_manifest()
    bad_usage_basis["clients"][0]["capabilities"]["session_discovery"]["usage_basis"] = "bogus"
    with pytest.raises(ValueError, match="invalid usage or cost basis"):
        validate_agent_capability_manifest(bad_usage_basis)


def test_validator_rejects_capability_specific_overclaims() -> None:
    """Per-capability honesty rules: basis claims must match the capability type."""
    # A synthetic-fixture claim must cite exact tests (refs containing "::test_").
    synthetic_without_test_ref = _valid_manifest()
    capability = synthetic_without_test_ref["clients"][0]["capabilities"]["mechanical_capture"]
    capability["state"] = "experimental"
    capability["verification"] = {
        "level": "synthetic_fixture",
        "verified_at": None,
        "client_versions": [],
        "evidence_refs": ["docs/no-test-name-here.md"],
    }
    with pytest.raises(ValueError, match="must name exact tests"):
        validate_agent_capability_manifest(synthetic_without_test_ref)

    # An unavailable capability cannot claim a usage/cost basis.
    unavailable_with_usage_basis = _valid_manifest()
    capability = unavailable_with_usage_basis["clients"][-1]["capabilities"]["mechanical_capture"]
    capability["state"] = "unavailable"
    capability["activation"] = "none"
    capability["verification"] = {"level": "none", "verified_at": None, "client_versions": [], "evidence_refs": []}
    capability["usage_basis"] = "client_reported"
    with pytest.raises(ValueError, match="unavailable state cannot claim usage"):
        validate_agent_capability_manifest(unavailable_with_usage_basis)

    # usage_import (when not unavailable) must be client_reported.
    usage_import_unknown_basis = _valid_manifest()
    usage_import_unknown_basis["clients"][0]["capabilities"]["usage_import"]["usage_basis"] = "unknown"
    with pytest.raises(ValueError, match="must declare client-reported usage basis"):
        validate_agent_capability_manifest(usage_import_unknown_basis)

    # cache_read / cache_write (when not unavailable) must be client_reported.
    cache_unknown_basis = _valid_manifest()
    cache_unknown_basis["clients"][0]["capabilities"]["cache_read"]["usage_basis"] = "unknown"
    with pytest.raises(ValueError, match="must declare the cache usage basis"):
        validate_agent_capability_manifest(cache_unknown_basis)


def test_validator_rejects_malformed_dated_evidence() -> None:
    """A verified claim's evidence must carry a real ISO date and relative refs."""
    bad_date = _valid_manifest()
    bad_date["clients"][0]["capabilities"]["session_discovery"]["verification"]["verified_at"] = "not-a-date"
    with pytest.raises(ValueError, match="requires dated evidence refs"):
        validate_agent_capability_manifest(bad_date)

    absolute_ref = _valid_manifest()
    absolute_ref["clients"][0]["capabilities"]["session_discovery"]["verification"]["evidence_refs"] = [
        "/absolute/path.md"
    ]
    with pytest.raises(ValueError, match="non-empty relative paths"):
        validate_agent_capability_manifest(absolute_ref)
