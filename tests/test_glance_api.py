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


def _record_child_usage(
    service: SentinelService,
    *,
    client: str,
    session_id: str,
    parent_session_id: str,
    updated_at: float,
    namespace: str | None = None,
) -> None:
    event = ClientUsageEvent(
        client=client,
        client_session_id=session_id,
        client_session_kind="child",
        parent_client_session_id=parent_session_id,
        source_path=Path(f"/tmp/{client}/{session_id}.jsonl"),
        title=None,
        cwd="/tmp/project",
        model="claude-opus-4-8",
        input_tokens=10,
        output_tokens=10,
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
        usage_row_lane="model:claude-opus-4-8",
        source_namespace_fingerprint=namespace or f"sha256:{client}",
        input_tokens_reported=True,
        output_tokens_reported=True,
        reasoning_output_tokens_reported=True,
        total_tokens=20,
        total_tokens_reported=True,
    ).to_sentinel_event()
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
    section_id: str | None = None,
) -> None:
    # The identity-preserving append: record_event re-stamps created_at at
    # receive time (correct in production — a section's created_at IS its
    # activity time), so backdating a section for the recency-window test needs
    # the verbatim merge path.
    resolved_section = section_id or f"sec-{session_id}"
    service.append_events_preserving_identity([
        {
            "event_id": f"evt_sec_{client}_{session_id}_{resolved_section}_{status}_{int(created_at)}",
            "created_at": created_at,
            "source": client,
            "event_type": f"section_{status}",
            "metadata": {
                "client": client,
                "client_session_id": session_id,
                "section_id": resolved_section,
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
    for route in (
        "/v1/glance",
        "/v1/version",
        "/v1/sessions",
        "/v1/session?client=x&session_id=y",
        "/v1/plan",
    ):
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


def test_recent_sessions_fold_children_into_their_root(tmp_path):
    """A glance list full of a root's own subagent children is noise: child
    usage activity folds into the root row (kind/parent metadata first, the
    ':' suffix as the legacy fallback), keeping the root's recency fresh."""

    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(
        service,
        client="claude-code",
        model="claude-opus-4-8",
        session_id="root-a",
        input_tokens=100,
        output_tokens=100,
        updated_at=now - 3600,
    )
    # A subagent child (own uuid, kind child + parent metadata) more recent
    # than the root's own activity.
    _record_child_usage(
        service,
        client="claude-code",
        session_id="11111111-aaaa-bbbb-cccc-222222222222",
        parent_session_id="root-a",
        updated_at=now - 60,
    )
    # A legacy ':'-suffixed child lane without parent metadata.
    _record_usage(
        service,
        client="claude-code",
        model="claude-opus-4-8",
        session_id="root-a:agentstem",
        input_tokens=5,
        output_tokens=5,
        updated_at=now - 30,
    )

    api_client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    payload = api_client.get("/v1/glance", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    rows = payload["recent_sessions"]
    assert [row["session_id"] for row in rows] == ["root-a"]
    # The root's recency reflects its children's freshest activity.
    assert rows[0]["last_activity_at"] >= now - 31


# ---------------------------------------------------------------------------
# review-finding regressions (adversarial review of 7042c1d)
# ---------------------------------------------------------------------------


def test_glance_cache_expires_by_age_even_when_events_are_unchanged(tmp_path, monkeypatch):
    """The HIGH finding: 'today'/stale/recency are clock-derived, so an
    unchanged event list must still rebuild after the TTL (midnight boundary)."""

    from agentacct.glance import GlanceCache

    calls = {"count": 0}
    real_build = glance_module.build_glance_snapshot

    def _counting_build(*args, **kwargs):
        calls["count"] += 1
        return real_build(*args, **kwargs)

    monkeypatch.setattr(glance_module, "build_glance_snapshot", _counting_build)
    cache = GlanceCache(max_age_seconds=60.0)
    t0 = time.time()
    cache.snapshot([], store_dir=tmp_path, version="0.9.0", now=t0)
    cache.snapshot([], store_dir=tmp_path, version="0.9.0", now=t0 + 30)
    assert calls["count"] == 1  # young cache, unchanged events -> hit
    cache.snapshot([], store_dir=tmp_path, version="0.9.0", now=t0 + 61)
    assert calls["count"] == 2  # TTL crossed -> rebuild despite identical events


def test_events_fingerprint_sees_the_namespace_bind_rewrite():
    """The MEDIUM finding: the TOFU bind rewrites namespace metadata in place
    (event_id and created_at preserved) and flips additivity — the fingerprint
    must change with those fields or live views serve stale totals."""

    base = [{
        "event_id": "evt_1",
        "created_at": 111.0,
        "metadata": {"client": "codex", "client_session_id": "s1"},
    }]
    bound = [{
        "event_id": "evt_1",
        "created_at": 111.0,
        "metadata": {
            "client": "codex",
            "client_session_id": "s1",
            "source_namespace_fingerprint": "sha256:" + "a" * 64,
            "source_namespace_binding": "tofu",
        },
    }]
    assert glance_module.events_fingerprint(base) != glance_module.events_fingerprint(bound)
    # Still total on hostile metadata shapes.
    hostile = [{"event_id": ["x"], "created_at": {"y": 1}, "metadata": "not-a-dict"}]
    glance_module.events_fingerprint(hostile)  # must not raise


def test_recent_session_status_uses_per_section_precedence(tmp_path):
    """The MEDIUM finding: a later completed section must not erase a still-open
    or blocked one — the glance must agree with the TUI badge."""

    service = SentinelService(tmp_path)
    now = time.time()
    # Session A: section one started, section two started, section one completed
    # -> still in progress (section two is open).
    _record_section(service, client="claude-code", session_id="sess-a", status="started", title="t", created_at=now - 300, section_id="sec-1")
    _record_section(service, client="claude-code", session_id="sess-a", status="started", title="t", created_at=now - 250, section_id="sec-2")
    _record_section(service, client="claude-code", session_id="sess-a", status="completed", title="t", created_at=now - 200, section_id="sec-1")
    # Session B: a blocked section followed by a later completed one -> blocked wins.
    _record_section(service, client="codex", session_id="sess-b", status="blocked", title="stuck", created_at=now - 400, section_id="sec-1")
    _record_section(service, client="codex", session_id="sess-b", status="completed", title="done bit", created_at=now - 100, section_id="sec-2")

    api_client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    payload = api_client.get("/v1/glance", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    statuses = {row["session_id"]: row["status"] for row in payload["recent_sessions"]}
    assert statuses["sess-a"] == "in_progress"
    assert statuses["sess-b"] == "blocked"


def test_usage_only_sessions_appear_and_stay_recent(tmp_path):
    """The LOW finding: usage activity counts toward recency — a session
    burning tokens now must appear even without any section events."""

    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(
        service,
        client="codex",
        model="gpt-5",
        session_id="sess-usage-only",
        input_tokens=100,
        output_tokens=100,
        updated_at=now - 120,
    )
    # A session whose last SECTION is ancient but whose usage is fresh stays listed.
    _record_section(service, client="claude-code", session_id="sess-mixed", status="started", title="old section", created_at=now - 7 * 3600)
    _record_usage(
        service,
        client="claude-code",
        model="claude-opus-4-8",
        session_id="sess-mixed",
        input_tokens=10,
        output_tokens=10,
        updated_at=now - 60,
    )

    api_client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    payload = api_client.get("/v1/glance", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    rows = {row["session_id"]: row for row in payload["recent_sessions"]}
    assert "sess-usage-only" in rows
    assert rows["sess-usage-only"]["status"] is None and rows["sess-usage-only"]["title"] is None
    assert "sess-mixed" in rows  # fresh usage keeps it recent despite the old section
    assert rows["sess-mixed"]["status"] == "in_progress"


def _dead_pid() -> int:
    pid = 4_100_000  # above default pid ranges on macOS and Linux
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except PermissionError:
            pass
        pid -= 1


def test_claim_respects_a_live_owner_and_takes_over_a_dead_one(tmp_path):
    """The lifecycle MEDIUM finding: a second server must not clobber a LIVE
    owner's slot (and later delete it), but a stale file from a crashed owner
    is taken over."""

    from agentacct.glance import claim_discovery_file

    store = tmp_path / "store"
    store.mkdir()
    # pid 1 (launchd/init) is alive and not ours -> the slot is owned.
    write_discovery_file(store, host="127.0.0.1", port=9001, token="owner-token", version="0.9.0", pid=1)
    assert claim_discovery_file(store, host="127.0.0.1", port=9002, token="mine", version="0.9.0") is None
    kept = read_discovery_file(store)
    assert kept is not None and kept["pid"] == 1 and kept["token"] == "owner-token"
    # And the pid-gated removal from the non-owner leaves the live slot alone.
    assert remove_discovery_file(store) is False
    assert read_discovery_file(store) is not None

    # A dead owner is stale: the claim takes the slot over.
    write_discovery_file(store, host="127.0.0.1", port=9003, token="stale", version="0.9.0", pid=_dead_pid())
    claimed = claim_discovery_file(store, host="127.0.0.1", port=9004, token="fresh", version="0.9.0")
    assert claimed is not None
    taken = read_discovery_file(store)
    assert taken is not None and taken["pid"] == os.getpid() and taken["token"] == "fresh"


def test_serve_declines_publishing_when_a_live_owner_holds_the_slot(tmp_path, monkeypatch):
    store = tmp_path / "store"
    SentinelService(store)
    write_discovery_file(store, host="127.0.0.1", port=9001, token="owner-token", version="0.9.0", pid=1)

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)
    result = CliRunner().invoke(cli_app, ["serve", "--store-dir", str(store)])
    assert result.exit_code == 0, result.output
    assert "unpublished" in result.output
    kept = read_discovery_file(store)
    assert kept is not None and kept["pid"] == 1 and kept["token"] == "owner-token"


def test_recent_session_plan_pct_is_gated_by_client(tmp_path):
    """The LOW finding: the pct lookup is keyed (client, session_id) — one
    client's calibrated number can never attach to another client's row."""

    import agentacct.plan_cost as plan_cost_module

    service = SentinelService(tmp_path)
    now = time.time()
    shared_session = "11111111-2222-3333-4444-555555555555"
    _record_usage(service, client="claude-code", model="claude-opus-4-8", session_id=shared_session,
                  input_tokens=100, output_tokens=100, updated_at=now - 120)
    _record_section(service, client="claude-code", session_id=shared_session, status="started", title="a", created_at=now - 110)
    _record_section(service, client="codex", session_id=shared_session, status="started", title="b", created_at=now - 100)

    api_client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    payload = api_client.get("/v1/glance", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    rows = {(row["client"], row["session_id"]): row for row in payload["recent_sessions"]}
    # Neither client is calibrated in this store, so both must be None — and
    # structurally the codex row can never receive a claude-code pct because
    # the lookup key carries the client.
    assert rows[("claude-code", shared_session)]["plan_pct"] is None
    assert rows[("codex", shared_session)]["plan_pct"] is None


def test_glance_plan_entries_carry_three_state_and_basis(tmp_path):
    """Additive plan fields: the payload itself says which clients can ever
    calibrate (codex: never), so no shell hard-codes that knowledge again."""

    service = SentinelService(tmp_path)
    _record_usage(service, client="claude-code", model="claude-opus-4-8", session_id="s1",
                  input_tokens=100, output_tokens=0, updated_at=time.time() - 60)

    api_client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    payload = api_client.get("/v1/glance", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    plan = {entry["client"]: entry for entry in payload["plan"]}
    assert plan["claude-code"]["confidence"] == "baseline"  # the original key survives
    assert plan["claude-code"]["calibration_state"] == "calibrating"
    assert plan["claude-code"]["calibratable"] is True
    assert plan["codex"]["calibration_state"] == "never"
    assert plan["codex"]["calibratable"] is False
    assert plan["claude-code"]["basis"]


def test_folded_root_plan_pct_includes_child_shares(tmp_path):
    """The recents list HIDES child sessions, so the root row's plan_pct must
    carry their share — an unfolded join understates a workflow-heavy root
    exactly when it matters most (the #65 fold moved only the activity key)."""

    from agentacct import plan_cost as pc

    service = SentinelService(tmp_path)
    now = time.time()
    opus = pc.baseline_weight_fresh("claude-opus-4-8")
    # Calibration history: observed movement == baseline prediction → scale ~1.
    move = 100.0 * opus
    t0 = now - 5 * 3600
    pct = 10.0
    _record_7d_limit(service, captured=t0, pct=pct, index=0)
    for i in range(4):
        _record_usage(service, client="claude-code", model="claude-opus-4-8",
                      session_id=f"cal-{i}", input_tokens=100_000_000, output_tokens=0,
                      updated_at=t0 + i * 3600 + 1800)
        pct += move
        _record_7d_limit(service, captured=t0 + (i + 1) * 3600, pct=pct, index=i + 1)

    _record_usage(service, client="claude-code", model="claude-opus-4-8", session_id="root-a",
                  input_tokens=10_000_000, output_tokens=0, updated_at=now - 600)
    _record_child_usage(service, client="claude-code",
                        session_id="11111111-aaaa-bbbb-cccc-222222222222",
                        parent_session_id="root-a", updated_at=now - 60)

    api_client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    payload = api_client.get("/v1/glance", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    plan = {entry["client"]: entry for entry in payload["plan"]}
    assert plan["claude-code"]["confidence"] == "calibrated"
    scale = plan["claude-code"]["scale"]
    rows = {row["session_id"]: row for row in payload["recent_sessions"]}
    assert "root-a" in rows
    # own 10 Mtok + child 20 tokens (≈0), both at the calibrated opus weight.
    expected = (10.0 + 20 / 1_000_000) * opus * scale
    assert abs(rows["root-a"]["plan_pct"] - expected) < 1e-6


def test_glance_refuses_cross_home_child_fold(tmp_path):
    """A child from a DIFFERENT source home must not fold its activity or its
    plan share into a same-id root — the glance applies the ledger's own
    namespace refusal (round-2 adversarial finding: the glance kept folding
    what the sessions lane refused, so the two surfaces showed different
    money for the same root)."""

    from agentacct import plan_cost as pc

    service = SentinelService(tmp_path)
    now = time.time()
    opus = pc.baseline_weight_fresh("claude-opus-4-8")
    move = 100.0 * opus
    t0 = now - 5 * 3600
    pct = 10.0
    _record_7d_limit(service, captured=t0, pct=pct, index=0)
    for i in range(4):
        _record_usage(service, client="claude-code", model="claude-opus-4-8",
                      session_id=f"cal-{i}", input_tokens=100_000_000, output_tokens=0,
                      updated_at=t0 + i * 3600 + 1800)
        pct += move
        _record_7d_limit(service, captured=t0 + (i + 1) * 3600, pct=pct, index=i + 1)

    _record_usage(service, client="claude-code", model="claude-opus-4-8", session_id="root-a",
                  input_tokens=10_000_000, output_tokens=0, updated_at=now - 600)
    _record_child_usage(service, client="claude-code",
                        session_id="55555555-aaaa-bbbb-cccc-666666666666",
                        parent_session_id="root-a", updated_at=now - 60,
                        namespace="sha256:other-home")

    api_client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    payload = api_client.get("/v1/glance", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    plan = {entry["client"]: entry for entry in payload["plan"]}
    assert plan["claude-code"]["confidence"] == "calibrated"
    scale = plan["claude-code"]["scale"]
    rows = {row["session_id"]: row for row in payload["recent_sessions"]}
    # The root keeps only its OWN share; the foreign child stands as its own
    # row with its own share — nothing misattributed, nothing hidden.
    assert abs(rows["root-a"]["plan_pct"] - 10.0 * opus * scale) < 1e-9
    foreign = rows["55555555-aaaa-bbbb-cccc-666666666666"]
    assert foreign["plan_pct"] is not None and foreign["plan_pct"] > 0


def test_non_ascii_bearer_token_yields_401_not_500(tmp_path):
    """Round-2 security finding: Starlette decodes header bytes as latin-1, and
    hmac.compare_digest raises TypeError on non-ASCII str — the gate must stay
    a clean 401 (the reader contract's re-read signal), never a 500. httpx
    refuses to SEND such a header, so this drives the ASGI app directly."""

    import asyncio

    SentinelService(tmp_path)
    app = create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN)

    async def _get_status(raw_authorization: bytes) -> int:
        messages: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/v1/glance",
            "raw_path": b"/v1/glance",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"testserver"), (b"authorization", raw_authorization)],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 80),
        }
        await app(scope, receive, send)
        return next(m["status"] for m in messages if m["type"] == "http.response.start")

    assert asyncio.run(_get_status(b"Bearer caf\xe9")) == 401
    assert asyncio.run(_get_status(b"Bearer \xff\xfe")) == 401


def test_discovery_heartbeat_reclaims_a_freed_slot(tmp_path):
    """Round-2 lifecycle finding: a serve that started unpublished (live owner
    still draining) must take the slot over once the owner's file goes away —
    the heartbeat is what closes the stranded-undiscoverable end state."""

    import threading

    from agentacct.glance import run_discovery_heartbeat

    store = tmp_path / "store"
    store.mkdir()
    # A live foreign owner holds the slot (pid 1).
    write_discovery_file(store, host="127.0.0.1", port=9001, token="owner", version="0.9.0", pid=1)

    stop = threading.Event()
    thread = threading.Thread(
        target=run_discovery_heartbeat,
        kwargs={
            "store_dir": store,
            "host": "127.0.0.1",
            "port": 9002,
            "token": "mine",
            "version": "0.9.1",
            "stop": stop,
            "interval_seconds": 0.01,
        },
        daemon=True,
    )
    thread.start()
    try:
        time.sleep(0.1)
        held = read_discovery_file(store)
        assert held is not None and held["pid"] == 1  # live owner respected

        # The owner exits and removes its file (simulated): the heartbeat must
        # re-claim within a tick.
        remove_discovery_file(store, pid=1)
        deadline = time.time() + 2.0
        claimed = None
        while time.time() < deadline:
            claimed = read_discovery_file(store)
            if claimed is not None and claimed["pid"] == os.getpid():
                break
            time.sleep(0.02)
        assert claimed is not None and claimed["pid"] == os.getpid() and claimed["token"] == "mine"
    finally:
        stop.set()
        thread.join(timeout=2)


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
