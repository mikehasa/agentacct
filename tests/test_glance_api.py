"""Tests for the /v1 native-shell lane: discovery file, bearer gate, glance payload.

The glance is what a menu bar app / SwiftBar plugin polls. Contract under test:
fail-closed auth (no token config -> 503, bad token -> 401), the 0600 pid-gated
discovery file, a fingerprint cache that never rebuilds an unchanged payload,
and glance numbers that agree with the usage_snapshot functions every other
surface renders.
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

import agentacct.glance as glance_module
from agentacct.api import create_local_api_app
from agentacct.cli import app as cli_app
from agentacct.client_usage import ClientUsageEvent
from agentacct.glance import (
    DISCOVERY_SCHEMA_VERSION,
    GLANCE_SCHEMA_VERSION,
    discovery_file_path,
    read_discovery_file,
    remove_discovery_file,
    write_discovery_file,
)
from agentacct.service import SentinelService

TOKEN = "test-v1-token"


def _record_usage(
    service: SentinelService,
    *,
    client: str,
    model: str,
    session_id: str,
    input_tokens: int,
    output_tokens: int,
    updated_at: float,
    estimated_cost_usd: float | None = None,
) -> None:
    event = ClientUsageEvent(
        client=client,
        client_session_id=session_id,
        source_path=Path(f"/tmp/{client}/{session_id}.jsonl"),
        title=None,
        cwd="/tmp/project",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_tokens_reported=True,
        cache_read_tokens_reported=True,
        reasoning_output_tokens=0,
        provider_name=client,
        started_at=updated_at,
        updated_at=updated_at,
        turn_count=1,
        usage_row_lane=f"model:{model}",
        source_namespace_fingerprint=f"sha256:{client}",
        input_tokens_reported=True,
        output_tokens_reported=True,
        reasoning_output_tokens_reported=True,
        total_tokens=input_tokens + output_tokens,
        total_tokens_reported=True,
    ).to_sentinel_event()
    if estimated_cost_usd is not None:
        event["estimated_cost_usd"] = estimated_cost_usd
        event["cost_confidence"] = "estimated_from_tokens"
    service.record_event(event, trusted_usage_import=True)


def _record_7d_limit(service: SentinelService, *, captured: float, pct: float, client: str = "claude-code", index: int = 0) -> None:
    service.record_event({
        "event_id": f"evt_rl_{client}_{index}",
        "created_at": captured,
        "source": client,
        "event_type": "rate_limit_observed",
        "metadata": {
            "client": client,
            "captured_at": captured,
            "windows": [{"kind": "7d", "window_minutes": 10080, "used_percent": pct}],
        },
    })


def _record_section(
    service: SentinelService,
    *,
    client: str,
    session_id: str,
    status: str,
    title: str,
    created_at: float,
) -> None:
    # The identity-preserving append: record_event re-stamps created_at at
    # receive time (correct in production — a section's created_at IS its
    # activity time), so backdating a section for the recency-window test needs
    # the verbatim merge path.
    service.append_events_preserving_identity([
        {
            "event_id": f"evt_sec_{client}_{session_id}_{status}_{int(created_at)}",
            "created_at": created_at,
            "source": client,
            "event_type": f"section_{status}",
            "metadata": {
                "client": client,
                "client_session_id": session_id,
                "section_id": f"sec-{session_id}",
                "section_status": status,
                "section_title": title,
            },
        }
    ])


# ---------------------------------------------------------------------------
# discovery file
# ---------------------------------------------------------------------------


def test_discovery_file_roundtrip_perms_and_pid_gate(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    path = write_discovery_file(store, host="127.0.0.1", port=8791, token="tok-1", version="0.9.0")
    assert path == discovery_file_path(store)
    # 0600: the token must never be readable by another local user.
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    payload = read_discovery_file(store)
    assert payload is not None
    assert payload["schema"] == DISCOVERY_SCHEMA_VERSION
    assert payload["host"] == "127.0.0.1"
    assert payload["port"] == 8791
    assert payload["token"] == "tok-1"
    assert payload["pid"] == os.getpid()
    assert payload["version"] == "0.9.0"

    # A restart overwrites in place (fresh per-boot token).
    write_discovery_file(store, host="127.0.0.1", port=8792, token="tok-2", version="0.9.1")
    payload = read_discovery_file(store)
    assert payload is not None and payload["token"] == "tok-2" and payload["port"] == 8792

    # The pid gate: a foreign pid must NOT delete the current owner's file.
    assert remove_discovery_file(store, pid=os.getpid() + 999_983) is False
    assert discovery_file_path(store).exists()
    assert remove_discovery_file(store) is True
    assert not discovery_file_path(store).exists()
    # Idempotent once gone.
    assert remove_discovery_file(store) is False


def test_read_discovery_file_never_raises(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    assert read_discovery_file(store) is None  # absent
    discovery_file_path(store).write_text("{not json", encoding="utf-8")
    assert read_discovery_file(store) is None  # corrupt
    discovery_file_path(store).write_text('{"schema": "some.future.v9", "port": 1, "token": "t"}', encoding="utf-8")
    assert read_discovery_file(store) is None  # foreign schema


# ---------------------------------------------------------------------------
# bearer gate
# ---------------------------------------------------------------------------


def test_v1_routes_fail_closed_without_configured_token(tmp_path):
    SentinelService(tmp_path)
    client = TestClient(create_local_api_app(store_dir=tmp_path))
    for route in ("/v1/glance", "/v1/version"):
        response = client.get(route, headers={"Authorization": "Bearer anything"})
        assert response.status_code == 503, route


def test_v1_routes_require_the_exact_bearer_token(tmp_path):
    SentinelService(tmp_path)
    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    assert client.get("/v1/glance").status_code == 401  # no header
    assert client.get("/v1/glance", headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
    assert client.get("/v1/glance", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.get("/v1/glance", headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200
    version = client.get("/v1/version", headers={"Authorization": f"Bearer {TOKEN}"})
    assert version.status_code == 200
    body = version.json()
    assert body["glance_schema"] == GLANCE_SCHEMA_VERSION
    assert body["pid"] == os.getpid()
    assert isinstance(body["version"], str) and body["version"]


def test_localhost_guard_still_covers_v1(tmp_path):
    SentinelService(tmp_path)
    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    response = client.get(
        "/v1/glance",
        headers={"Authorization": f"Bearer {TOKEN}", "Host": "evil.example"},
    )
    assert response.status_code == 403  # a valid token never overrides the Host guard


# ---------------------------------------------------------------------------
# glance payload
# ---------------------------------------------------------------------------


def test_glance_payload_shape_and_agreement(tmp_path):
    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(
        service,
        client="claude-code",
        model="claude-opus-4-8",
        session_id="sess-a",
        input_tokens=1_000,
        output_tokens=2_000,
        updated_at=now - 600,
        estimated_cost_usd=0.42,
    )
    _record_7d_limit(service, captured=now - 300, pct=37.5)
    _record_section(service, client="claude-code", session_id="sess-a", status="started", title="fix the login bug", created_at=now - 550)
    _record_section(service, client="claude-code", session_id="sess-a", status="completed", title="fix the login bug", created_at=now - 500)
    _record_section(service, client="codex", session_id="sess-old", status="started", title="ancient work", created_at=now - 8 * 3600)

    api_client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    payload = api_client.get("/v1/glance", headers={"Authorization": f"Bearer {TOKEN}"}).json()

    assert payload["schema"] == GLANCE_SCHEMA_VERSION
    assert payload["daemon"]["pid"] == os.getpid()

    # Usage windows: same aggregation `agentacct now` renders — today's bucket
    # carries the seeded session's tokens.
    windows = {window["label"]: window["totals"] for window in payload["usage"]["windows"]}
    assert "today" in windows and "all time" in windows
    assert windows["all time"]["fresh_tokens"] == 3_000
    assert windows["all time"]["total_tokens_including_cached"] == 3_000
    assert payload["usage"]["usage_record_count"] == 1

    # Limits: the byte-stable limit_json_entry shape plus the stale flag.
    assert len(payload["limits"]) == 1
    limit = payload["limits"][0]
    assert limit["client"] == "claude-code"
    assert limit["stale"] is False
    assert limit["windows"][0]["used_percent"] == 37.5

    # Plan: both plan-bearing clients report an honest confidence string
    # (nothing calibrates from one seeded interval — and no number is invented).
    plan = {entry["client"]: entry["confidence"] for entry in payload["plan"]}
    assert set(plan) == {"claude-code", "codex"}
    assert all(isinstance(value, str) and value for value in plan.values())

    # Recent sessions: latest section wins; sessions older than the activity
    # window are excluded; plan_pct is None while uncalibrated (honesty rule).
    sessions = payload["recent_sessions"]
    assert [row["session_id"] for row in sessions] == ["sess-a"]
    assert sessions[0]["status"] == "completed"
    assert sessions[0]["title"] == "fix the login bug"
    assert sessions[0]["plan_pct"] is None


def test_glance_cache_rebuilds_only_on_event_change(tmp_path, monkeypatch):
    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(
        service,
        client="claude-code",
        model="claude-opus-4-8",
        session_id="sess-a",
        input_tokens=10,
        output_tokens=20,
        updated_at=now - 60,
    )
    calls = {"count": 0}
    real_build = glance_module.build_glance_snapshot

    def _counting_build(*args, **kwargs):
        calls["count"] += 1
        return real_build(*args, **kwargs)

    monkeypatch.setattr(glance_module, "build_glance_snapshot", _counting_build)
    api_client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    headers = {"Authorization": f"Bearer {TOKEN}"}

    first = api_client.get("/v1/glance", headers=headers).json()
    second = api_client.get("/v1/glance", headers=headers).json()
    assert calls["count"] == 1  # unchanged events -> pure cache hit
    assert second == first

    _record_usage(
        service,
        client="claude-code",
        model="claude-opus-4-8",
        session_id="sess-b",
        input_tokens=5,
        output_tokens=5,
        updated_at=now - 30,
    )
    third = api_client.get("/v1/glance", headers=headers).json()
    assert calls["count"] == 2  # new event -> rebuild
    assert third["usage"]["usage_record_count"] == 2


# ---------------------------------------------------------------------------
# serve wiring
# ---------------------------------------------------------------------------


def test_serve_writes_discovery_before_run_and_removes_after(tmp_path, monkeypatch):
    store = tmp_path / "store"
    SentinelService(store)
    seen: dict[str, object] = {}

    import uvicorn

    def _fake_run(app, **kwargs):  # noqa: ANN001 - uvicorn.run signature
        seen["during"] = read_discovery_file(store)
        seen["port"] = kwargs.get("port")

    monkeypatch.setattr(uvicorn, "run", _fake_run)
    result = CliRunner().invoke(cli_app, ["serve", "--store-dir", str(store)])
    assert result.exit_code == 0, result.output

    during = seen["during"]
    assert isinstance(during, dict)
    # The file must carry the ACTUAL bound port uvicorn was launched with.
    assert during["port"] == seen["port"]
    assert during["token"]
    # And it must be gone once the server loop exits (pid-gated cleanup).
    assert read_discovery_file(store) is None
