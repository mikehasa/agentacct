from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from agentacct.cli import app
from agentacct.usage_truth import (
    CODEX_LINEAGE_DELTA_SEMANTICS,
    CODEX_REPLAY_QUARANTINE_STATE,
    local_usage_base_session_id,
    local_usage_additivity,
    local_usage_row_identity,
    local_usage_row_lane,
    usage_truth_table,
)


def test_usage_truth_table_has_expected_integrations() -> None:
    rows = {row["integration"]: row for row in usage_truth_table()}

    assert "codex local usage import" in rows
    assert "claude-code local usage import" in rows
    assert "opencode JSON event stream import" in rows
    assert "hermes local state import" in rows
    assert "openclaw JSONL local usage import" in rows
    assert "agent MCP workflow events" in rows
    assert "native coding-agent hook capture" in rows
    assert "sentinel-owned process wrapper" in rows

    assert rows["codex local usage import"]["usage_confidence"] == "client_reported"
    assert "token_count" in rows["codex local usage import"]["update_timing"]
    assert "provider invoices" in " ".join(rows["codex local usage import"]["limitations"])
    assert rows["claude-code local usage import"]["cost_confidence"].startswith("unknown")
    assert "assistant message rows" in rows["claude-code local usage import"]["update_timing"]
    assert rows["opencode JSON event stream import"]["cost_confidence"] == "client_reported"
    assert "step-finish" in rows["opencode JSON event stream import"]["update_timing"]
    assert rows["hermes local state import"]["cost_confidence"] == "client_reported"
    assert "provider invoices" in " ".join(rows["hermes local state import"]["limitations"])
    assert rows["openclaw JSONL local usage import"]["usage_confidence"] == "client_reported"
    assert "usage.cost.total" in rows["openclaw JSONL local usage import"]["cost_confidence"]
    assert rows["agent MCP workflow events"]["usage_confidence"] == "unknown"
    assert rows["native coding-agent hook capture"]["usage_confidence"] == "unknown"
    assert "optional fallback" in " ".join(rows["sentinel-owned process wrapper"]["limitations"])


def test_usage_truth_table_cli_json() -> None:
    result = CliRunner().invoke(app, ["usage", "truth-table", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    integrations = payload["integrations"]
    assert len(integrations) >= 6
    assert "subscription billing" in json.dumps(payload)


def test_usage_truth_table_cli_human_output() -> None:
    result = CliRunner().invoke(app, ["usage", "truth-table"])

    assert result.exit_code == 0, result.output
    assert "agentacct usage/cost truth table" in result.output
    assert "codex local usage import" in result.output
    assert "Freshness" in result.output
    assert "agent MCP workflow events" in result.output


def test_usage_truth_table_is_documented() -> None:
    text = Path("docs/usage-truth-table.md").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "Codex" in text
    assert "Claude Code" in text
    assert "OpenCode" in text
    assert "Hermes" in text
    assert "OpenClaw" in text
    assert "MCP" in text
    assert "provider-billed" in text
    assert "Observed local timing" in text
    assert "docs/usage-truth-table.md" in readme


def _import_row(client: str, session_id: str, *, model: str | None = None, lane: str | None = None) -> dict:
    metadata = {
        "usage_source": "local_client_session_store",
        "usage_provenance": "agent_sentinel_local_usage_import",
        "client": client,
        "client_session_id": session_id,
    }
    if lane is not None:
        metadata["usage_row_lane"] = lane
    return {
        "event_type": "model_usage",
        "source": f"{client}-local-session-import",
        "model": model,
        "metadata": metadata,
    }


def test_local_usage_row_identity_variants() -> None:
    # Explicit lane metadata wins over anything derivable.
    explicit = _import_row("claude-code", "S", model="other-model", lane="model:claude-opus-4-8")
    assert local_usage_row_lane(explicit) == "model:claude-opus-4-8"
    assert local_usage_row_identity(explicit) == ("claude-code", "S", "model:claude-opus-4-8")

    # Legacy ':model:'-suffixed key parses back to (client, base, lane).
    legacy = _import_row("claude-code", "S:model:claude-haiku-4-5-20251001")
    assert local_usage_row_identity(legacy) == ("claude-code", "S", "model:claude-haiku-4-5-20251001")

    # Child ':stem' suffixes are preserved; only the ':model:' artifact is stripped.
    legacy_child = _import_row("claude-code", "S:agent-a123:model:m")
    assert local_usage_row_identity(legacy_child) == ("claude-code", "S:agent-a123", "model:m")
    assert local_usage_base_session_id("S:agent-a123") == "S:agent-a123"
    assert local_usage_base_session_id("S:agent-a123:model:m") == "S:agent-a123"

    # Untagged claude row derives its lane from the top-level model, sanitized.
    untagged = _import_row("claude-code", "S", model="claude-3.5")
    assert local_usage_row_identity(untagged) == ("claude-code", "S", "model:claude-3_5")
    unknown_model = _import_row("claude-code", "S")
    assert local_usage_row_identity(unknown_model) == ("claude-code", "S", "model:unknown")

    # Non-claude clients never get a model lane: their model labels can drift
    # between imports so model must not enter row identity.
    codex = _import_row("codex", "session-abc", model="gpt-5.5")
    assert local_usage_row_lane(codex) is None
    assert local_usage_row_identity(codex) == ("codex", "session-abc", "")
    hermes = _import_row("hermes", "hermes-session")
    assert local_usage_row_identity(hermes) == ("hermes", "hermes-session", "")

    # ':model:' interpretation is claude-only: a non-claude session id that
    # happens to contain the marker is user data and stays untouched.
    weird_codex = _import_row("codex", "x:model:y")
    assert local_usage_row_identity(weird_codex) == ("codex", "x:model:y", "")

    # Rows without a usable key have no identity.
    assert local_usage_row_identity({"event_type": "model_usage", "metadata": None}) is None
    assert local_usage_row_lane({"event_type": "model_usage", "metadata": None}) is None


def test_codex_descendant_usage_additivity_fails_closed_without_affecting_other_clients() -> None:
    assert local_usage_additivity(client="codex", session_kind="root") == (True, None)
    assert local_usage_additivity(client="codex", session_kind="child") == (
        False,
        CODEX_REPLAY_QUARANTINE_STATE,
    )
    assert local_usage_additivity(client="codex", session_kind="internal") == (
        False,
        CODEX_REPLAY_QUARANTINE_STATE,
    )
    # Parent identity is authoritative even if an older importer mislabeled
    # the row as root.
    assert local_usage_additivity(
        client="codex",
        session_kind="root",
        parent_client_session_id="parent-session",
    ) == (False, CODEX_REPLAY_QUARANTINE_STATE)

    # The Codex replay rule must not quarantine child rows from importers with
    # independent/additive usage semantics.
    for client in ("claude-code", "hermes", "opencode", "openclaw"):
        assert local_usage_additivity(
            client=client,
            session_kind="child",
            parent_client_session_id="parent-session",
        ) == (True, None)


def test_codex_lineage_delta_requires_exact_parent_and_same_namespace() -> None:
    proven = {
        "client": "codex",
        "session_kind": "child",
        "usage_update_semantics": CODEX_LINEAGE_DELTA_SEMANTICS,
        "source_namespace_fingerprint": "codex-home-a",
        "parent_source_namespace_fingerprint": "codex-home-a",
    }
    assert local_usage_additivity(
        **proven,
        parent_client_session_id="parent-session",
    ) == (True, None)

    for missing_or_malformed_parent in (None, "", "   ", {"unexpected": "shape"}):
        assert local_usage_additivity(
            **proven,
            parent_client_session_id=missing_or_malformed_parent,
        ) == (False, CODEX_REPLAY_QUARANTINE_STATE)

    assert local_usage_additivity(
        **{
            **proven,
            "parent_source_namespace_fingerprint": "codex-home-b",
        },
        parent_client_session_id="parent-session",
    ) == (False, CODEX_REPLAY_QUARANTINE_STATE)
    assert local_usage_additivity(
        client="codex",
        session_kind="child",
        parent_client_session_id="parent-session",
        usage_update_semantics="codex_rollout_token_count_events",
        source_namespace_fingerprint="codex-home-a",
        parent_source_namespace_fingerprint="codex-home-a",
    ) == (False, CODEX_REPLAY_QUARANTINE_STATE)


# ---------------------------------------------------------------------------
# Phase 2 Batch A. 2e: diagnostic tool event predicate — matches event_type OR
# source; NEVER run_id-only (a user collision must not hide real work).
# ---------------------------------------------------------------------------


def test_is_diagnostic_event_matches_type_or_source_never_run_id_only() -> None:
    from agentacct.usage_truth import (
        DIAGNOSTIC_EVENT_SOURCES,
        DIAGNOSTIC_EVENT_TYPES,
        DIAGNOSTIC_RUN_IDS,
        is_diagnostic_event,
    )

    assert DIAGNOSTIC_EVENT_TYPES == frozenset({"mcp_doctor_test", "workflow_smoke"})
    assert DIAGNOSTIC_EVENT_SOURCES == frozenset({"agent-sentinel-mcp-doctor", "agent-sentinel-mcp-workflow-smoke"})
    assert DIAGNOSTIC_RUN_IDS == frozenset({"mcp_doctor_test", "mcp_workflow_smoke"})

    # event_type alone matches.
    assert is_diagnostic_event({"event_type": "mcp_doctor_test", "source": "anything"})
    assert is_diagnostic_event({"event_type": "workflow_smoke"})
    # source alone matches (e.g. a smoke event recording model_usage).
    assert is_diagnostic_event({"event_type": "model_usage", "source": "agent-sentinel-mcp-workflow-smoke"})
    assert is_diagnostic_event({"event_type": "section_started", "source": "agent-sentinel-mcp-doctor"})
    # run_id alone NEVER matches: run_id is user-overridable free text.
    assert not is_diagnostic_event(
        {"event_type": "model_usage", "source": "codex-local-session-import", "run_id": "mcp_doctor_test"}
    )
    assert not is_diagnostic_event({"event_type": "model_usage", "source": "codex-local-session-import"})
    assert not is_diagnostic_event({"event_type": "section_started", "source": "codex"})
    assert not is_diagnostic_event({})


def test_split_diagnostic_events_preserves_order_and_content() -> None:
    from agentacct.usage_truth import split_diagnostic_events

    events = [
        {"event_id": "a", "event_type": "model_usage", "source": "codex-local-session-import"},
        {"event_id": "b", "event_type": "mcp_doctor_test", "source": "agent-sentinel-mcp-doctor"},
        {"event_id": "c", "event_type": "section_started", "source": "codex"},
        {"event_id": "d", "event_type": "workflow_smoke", "source": "agent-sentinel-mcp-workflow-smoke"},
    ]
    user_facing, diagnostic = split_diagnostic_events(events)

    assert [event["event_id"] for event in user_facing] == ["a", "c"]
    assert [event["event_id"] for event in diagnostic] == ["b", "d"]
    # No mutation, no loss.
    assert len(user_facing) + len(diagnostic) == len(events)
