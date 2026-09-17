from __future__ import annotations

from agentacct.client_usage import SUPPORTED_CLIENTS
from agentacct.connections import agent_kind, build_connections


def _snapshot(sources=(), issues=(), watcher_running=True):
    # Model a live watcher covering every source by default (scope "watched"),
    # so a healthy source legitimately reads as recording/reading. Individual
    # tests override scope or flip watcher_running to exercise the liveness gate.
    watcher = {"state": "running"} if watcher_running else {"state": "stopped"}
    rows = []
    for source in sources:
        row = dict(source)
        row.setdefault("scope", "watched")
        rows.append(row)
    return {"state": "healthy", "sources": rows, "watcher": watcher, "issues": list(issues)}


def _by_id(rows):
    return {row["id"]: row for row in rows}


def test_agent_kind_classifies_active_semi_passive():
    for c in ("claude-code", "codex", "opencode", "hermes", "dsh"):
        assert agent_kind(c) == "active"
    assert agent_kind("openclaw") == "semi"
    assert agent_kind("cursor") == "passive"


def test_active_agent_recording_when_configured_and_healthy():
    rows = build_connections(
        supported_clients=["codex"],
        configured_clients=["codex"],
        ingestion_snapshot=_snapshot(sources=[{"source": "codex", "state": "healthy", "last_success_at": 10.0}]),
    )
    row = rows[0]
    assert row["kind"] == "active"
    assert row["configured"] is True
    assert row["status"] == "recording"
    assert row["primary_action"] is None


def test_active_agent_healthy_but_watcher_stopped_is_idle_not_recording():
    # Honesty (liveness gate): a healthy source under a STOPPED watcher reads as
    # healthy off a stale success, but nothing is being recorded right now, so it
    # must not show green "recording".
    rows = build_connections(
        supported_clients=["codex"],
        configured_clients=["codex"],
        ingestion_snapshot=_snapshot(
            sources=[{"source": "codex", "state": "healthy", "last_success_at": 10.0}],
            watcher_running=False,
        ),
    )
    assert rows[0]["status"] == "connected_idle"
    assert rows[0]["primary_action"] is None


def test_active_agent_healthy_but_unwatched_scope_is_idle_not_recording():
    # A source known only from a historical/manual import (scope != "watched")
    # is not covered by the live watcher, so healthy must read as idle.
    rows = build_connections(
        supported_clients=["codex"],
        configured_clients=["codex"],
        ingestion_snapshot=_snapshot(
            sources=[{"source": "codex", "state": "healthy", "scope": "manual"}],
        ),
    )
    assert rows[0]["status"] == "connected_idle"


def test_active_agent_not_connected_when_absent_from_activation_and_no_data():
    # Honesty: never claim connected/recording without evidence.
    rows = build_connections(
        supported_clients=["hermes"],
        configured_clients=[],
        ingestion_snapshot=_snapshot(),
    )
    assert rows[0]["status"] == "not_connected"
    assert rows[0]["primary_action"] == "connect"


def test_active_agent_not_connected_even_with_healthy_ingestion_when_unconfigured():
    # The activation-evidence half of the honesty rule: an agent recording data
    # but absent from the activation record is still "not_connected" — a guard
    # reorder that let ingestion evidence alone claim "recording" would be caught
    # here (the ingestion half is pinned by the passive/configured cases).
    rows = build_connections(
        supported_clients=["codex"],
        configured_clients=[],
        ingestion_snapshot=_snapshot(sources=[{"source": "codex", "state": "healthy"}]),
    )
    assert rows[0]["status"] == "not_connected"
    assert rows[0]["primary_action"] == "connect"


def test_active_agent_degraded_offers_point_to_point_resync():
    rows = build_connections(
        supported_clients=["codex"],
        configured_clients=["codex"],
        ingestion_snapshot=_snapshot(
            sources=[{"source": "codex", "state": "degraded"}],
            issues=[{"code": "source_scan_failed", "source": "codex", "action": "Refresh", "severity": "error"}],
        ),
    )
    assert rows[0]["status"] == "needs_attention"
    assert rows[0]["primary_action"] == "resync"


def test_store_wide_issue_reaches_every_affected_connection():
    rows = build_connections(
        supported_clients=["codex", "claude-code"],
        configured_clients=["codex", "claude-code"],
        ingestion_snapshot=_snapshot(
            sources=[{"source": "codex", "state": "degraded"}, {"source": "claude-code", "state": "degraded"}],
            issues=[{
                "code": "evidence_refreshable_usage_failed",
                "source": None,
                "affected_sources": ["codex", "claude-code"],
                "action": "Refresh usage",
                "severity": "error",
            }],
        ),
    )
    by_id = {row["id"]: row for row in rows}
    for client in ("codex", "claude-code"):
        assert by_id[client]["status"] == "needs_attention"
        assert [issue["code"] for issue in by_id[client]["issues"]] == ["evidence_refreshable_usage_failed"]
        assert by_id[client]["issues"][0]["source"] == client


def test_configured_but_no_data_yet_reads_as_connected_idle_not_recording():
    rows = build_connections(
        supported_clients=["opencode"],
        configured_clients=["opencode"],
        ingestion_snapshot=_snapshot(sources=[{"source": "opencode", "state": "pending"}]),
    )
    assert rows[0]["status"] == "connected_idle"
    assert rows[0]["primary_action"] is None


def test_semi_agent_openclaw_is_never_offered_a_one_click_connect():
    rows = build_connections(
        supported_clients=["openclaw"],
        configured_clients=[],
        ingestion_snapshot=_snapshot(),
    )
    row = rows[0]
    assert row["kind"] == "semi"
    assert row["primary_action"] == "connect_manual"  # guidance, not auto-connect
    assert row["status"] == "read_only"  # no evidence yet, never "recording"


def test_semi_agent_openclaw_reading_from_ingestion_evidence_not_a_phantom_flag():
    # openclaw has no onboarding writer, so its activation flag is never set. A
    # genuinely importing openclaw must read as "reading" from ingestion
    # evidence, not be pinned to "not_connected" by a flag we never write.
    rows = build_connections(
        supported_clients=["openclaw"],
        configured_clients=[],
        ingestion_snapshot=_snapshot(sources=[{"source": "openclaw", "state": "healthy"}]),
    )
    row = rows[0]
    assert row["kind"] == "semi"
    assert row["status"] == "reading"
    assert row["primary_action"] == "connect_manual"  # manual MCP step still offered


def test_semi_agent_openclaw_degraded_surfaces_resolve():
    rows = build_connections(
        supported_clients=["openclaw"],
        configured_clients=[],
        ingestion_snapshot=_snapshot(
            sources=[{"source": "openclaw", "state": "degraded"}],
            issues=[{"code": "source_scan_failed", "source": "openclaw", "action": "Refresh", "severity": "error"}],
        ),
    )
    assert rows[0]["status"] == "needs_attention"
    assert rows[0]["primary_action"] == "resolve"


def test_passive_cursor_is_read_only_and_never_offered_connect():
    healthy = build_connections(
        supported_clients=["cursor"],
        configured_clients=[],
        ingestion_snapshot=_snapshot(sources=[{"source": "cursor", "state": "healthy"}]),
    )[0]
    assert healthy["kind"] == "passive"
    assert healthy["status"] == "reading"
    assert healthy["primary_action"] is None

    degraded = build_connections(
        supported_clients=["cursor"],
        configured_clients=[],
        ingestion_snapshot=_snapshot(
            sources=[{"source": "cursor", "state": "degraded"}],
            issues=[{"code": "source_read_permission_required", "source": "cursor", "action": "Grant read", "severity": "error"}],
        ),
    )[0]
    assert degraded["status"] == "needs_attention"
    assert degraded["primary_action"] == "resolve"  # never "connect"


def test_passive_cursor_healthy_but_watcher_stopped_is_read_only_not_reading():
    # Same liveness gate as active: a healthy cursor under a stopped watcher is
    # not actively being read, so it must not show green "reading".
    row = build_connections(
        supported_clients=["cursor"],
        configured_clients=[],
        ingestion_snapshot=_snapshot(
            sources=[{"source": "cursor", "state": "healthy"}],
            watcher_running=False,
        ),
    )[0]
    assert row["status"] == "read_only"
    assert row["primary_action"] is None


def test_all_supported_agents_render_active_first_then_semi_then_passive():
    rows = build_connections(
        supported_clients=SUPPORTED_CLIENTS,
        configured_clients=[],
        ingestion_snapshot=_snapshot(),
    )
    assert {r["id"] for r in rows} == set(SUPPORTED_CLIENTS)
    kinds = [r["kind"] for r in rows]
    # active block, then semi, then passive — monotonic rank.
    rank = {"active": 0, "semi": 1, "passive": 2}
    assert kinds == sorted(kinds, key=lambda k: rank[k])
    assert rows[-1]["id"] == "cursor"


def test_advisory_only_issue_does_not_flip_configured_agent_to_needs_attention():
    # A cosmetic advisory (e.g. version mismatch, source=None) is not a per-agent
    # error, so a healthy configured agent stays "recording".
    rows = build_connections(
        supported_clients=["codex"],
        configured_clients=["codex"],
        ingestion_snapshot=_snapshot(
            sources=[{"source": "codex", "state": "healthy"}],
            issues=[{"code": "watcher_version_mismatch", "source": None, "action": "Restart", "severity": "advisory"}],
        ),
    )
    assert rows[0]["status"] == "recording"


# --- route wiring (bearer-gated /v1/connections) ---


def test_v1_connections_fails_closed_and_rejects_bad_bearer(tmp_path):
    from fastapi.testclient import TestClient

    from agentacct.api import create_local_api_app

    open_client = TestClient(create_local_api_app(store_dir=tmp_path))
    assert open_client.get("/v1/connections").status_code == 503
    auth_client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token="test-v1-token"))
    assert auth_client.get("/v1/connections").status_code == 401
    assert (
        auth_client.get("/v1/connections", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )


def test_v1_connections_returns_every_supported_agent_honestly(tmp_path):
    from fastapi.testclient import TestClient

    from agentacct.api import create_local_api_app
    from agentacct.connections import CONNECTIONS_SCHEMA_VERSION

    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token="test-v1-token"))
    response = client.get("/v1/connections", headers={"Authorization": "Bearer test-v1-token"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == CONNECTIONS_SCHEMA_VERSION
    assert {c["id"] for c in payload["connections"]} == set(SUPPORTED_CLIENTS)
    # Fresh store: nothing configured, nothing recording -> active agents are
    # honestly "not_connected", never "recording".
    codex = next(c for c in payload["connections"] if c["id"] == "codex")
    assert codex["status"] == "not_connected"
