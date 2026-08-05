"""Tests for the /v1/sessions lane and the shared work-ledger cache.

Contract under test: bearer gate parity with the other /v1 routes, server-side
roots_only filtering BEFORE the slice (the "12 root sessions" regression),
offset/limit pagination with disclosed totals + truncation, per-session
weekly-plan shares with children folded into their root (calibrated-or-nothing),
the three-state plan calibration payload, and the fingerprint+TTL caches that
keep polling cheap (one ledger build shared across routes and polls).
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

import agentacct.api as api_module
from agentacct import plan_cost as pc
from agentacct.api import create_local_api_app
from agentacct.client_usage import ClientUsageEvent
from agentacct.service import SentinelService
from agentacct.v1_sessions import V1_SESSIONS_SCHEMA_VERSION
from agentacct.work_ledger import WorkLedgerCache

TOKEN = "test-v1-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _record_usage(
    service: SentinelService,
    *,
    client: str = "claude-code",
    model: str = "claude-opus-4-8",
    session_id: str,
    tokens: int,
    updated_at: float,
    session_kind: str | None = None,
    parent_session_id: str | None = None,
) -> None:
    event = ClientUsageEvent(
        client=client,
        client_session_id=session_id,
        client_session_kind=session_kind,
        parent_client_session_id=parent_session_id,
        source_path=Path(f"/tmp/{client}/{session_id}.jsonl"),
        title=None,
        cwd="/tmp/project",
        model=model,
        input_tokens=tokens,
        output_tokens=0,
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
        total_tokens=tokens,
        total_tokens_reported=True,
    ).to_sentinel_event()
    service.record_event(event, trusted_usage_import=True)


def _record_7d(service: SentinelService, *, captured: float, pct: float, client: str = "claude-code", index: int = 0) -> None:
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
    client: str = "claude-code",
    session_id: str,
    status: str,
    title: str = "t",
    created_at: float,
    section_id: str | None = None,
) -> None:
    resolved_section = section_id or f"sec-{session_id}-{status}"
    service.append_events_preserving_identity([
        {
            "event_id": f"evt_sec_{client}_{session_id}_{resolved_section}_{int(created_at)}",
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


def _calibrate_claude(service: SentinelService, *, now: float) -> None:
    """Recorded 7d history whose movement matches the baseline exactly → the
    fitted scale is ~1.0 (inside the trusted band) and claude-code calibrates.
    The calibration burner sessions are named ``cal-N``."""

    opus = pc.BASELINE_MODEL_WEIGHTS["claude-opus-4-8"]
    move = 100.0 * opus  # 100M Opus tokens per interval → observed == predicted
    t0 = now - 5 * 3600
    pct = 10.0
    _record_7d(service, captured=t0, pct=pct, index=0)
    for i in range(4):
        mid = t0 + i * 3600 + 1800
        _record_usage(service, session_id=f"cal-{i}", tokens=100_000_000, updated_at=mid)
        pct += move
        _record_7d(service, captured=t0 + (i + 1) * 3600, pct=pct, index=i + 1)


def _sessions(client: TestClient, **params) -> dict:
    response = client.get("/v1/sessions", headers=AUTH, params=params)
    assert response.status_code == 200
    return response.json()


def _rows_by_id(payload: dict) -> dict[str, dict]:
    return {row["client_session_id"]: row for row in payload["sessions"]}


# ---------------------------------------------------------------------------
# auth parity
# ---------------------------------------------------------------------------


def test_v1_sessions_requires_the_bearer_token(tmp_path):
    SentinelService(tmp_path)
    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    assert client.get("/v1/sessions").status_code == 401
    assert client.get("/v1/sessions", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.get("/v1/sessions", headers=AUTH)
    assert ok.status_code == 200
    assert ok.json()["schema"] == V1_SESSIONS_SCHEMA_VERSION
    # The Host guard covers /v1/sessions like every other route.
    assert client.get("/v1/sessions", headers={**AUTH, "Host": "evil.example"}).status_code == 403


def test_version_advertises_the_sessions_schema(tmp_path):
    SentinelService(tmp_path)
    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    body = client.get("/v1/version", headers=AUTH).json()
    assert body["sessions_schema"] == V1_SESSIONS_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# roots_only + pagination (the "12 root sessions" fix)
# ---------------------------------------------------------------------------


def test_roots_only_filters_before_the_slice(tmp_path):
    """A root whose subagent children dominate the recency window must still
    be served: the filter runs server-side BEFORE offset/limit, so children
    can never starve roots out of a page."""

    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(service, session_id="root-a", tokens=1000, updated_at=now - 3600)
    for i in range(6):  # six children, all more recent than the root itself
        _record_usage(
            service,
            session_id=f"11111111-aaaa-bbbb-cccc-00000000000{i}",
            tokens=10,
            updated_at=now - 60 - i,
            session_kind="child",
            parent_session_id="root-a",
        )

    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    payload = _sessions(client, limit=3)  # roots_only defaults to true
    ids = [row["client_session_id"] for row in payload["sessions"]]
    assert ids == ["root-a"]
    assert payload["roots_only"] is True
    assert payload["total_sessions"] == 7
    assert payload["total_root_sessions"] == 1
    assert payload["filtered_total"] == 1
    assert payload["truncated"] is False
    # Internal bookkeeping never leaks to the wire.
    assert "is_root" not in payload["sessions"][0]
    assert "namespace_fingerprint" not in payload["sessions"][0]

    everyone = _sessions(client, roots_only="false", limit=50)
    assert everyone["filtered_total"] == 7
    child_rows = [row for row in everyone["sessions"] if (row["related"] or {}).get("parent")]
    assert len(child_rows) == 6


def test_pagination_disclose_totals_and_truncation(tmp_path):
    service = SentinelService(tmp_path)
    now = time.time()
    for i in range(3):
        _record_usage(service, session_id=f"root-{i}", tokens=100, updated_at=now - 100 * (i + 1))

    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    first = _sessions(client, limit=2)
    assert [row["client_session_id"] for row in first["sessions"]] == ["root-0", "root-1"]
    assert first["returned"] == 2
    assert first["truncated"] is True

    rest = _sessions(client, limit=2, offset=2)
    assert [row["client_session_id"] for row in rest["sessions"]] == ["root-2"]
    assert rest["returned"] == 1
    assert rest["truncated"] is False

    beyond = _sessions(client, limit=2, offset=10)
    assert beyond["sessions"] == []
    assert beyond["truncated"] is False


# ---------------------------------------------------------------------------
# status + row shape
# ---------------------------------------------------------------------------


def test_row_status_uses_blocked_over_completed_precedence(tmp_path):
    """A later completed section must not erase a blocked one (TUI/glance
    badge parity)."""

    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(service, session_id="root-a", tokens=100, updated_at=now - 500)
    _record_section(service, session_id="root-a", status="blocked", created_at=now - 400, section_id="s1")
    _record_section(service, session_id="root-a", status="completed", created_at=now - 300, section_id="s2")

    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    row = _rows_by_id(_sessions(client))["root-a"]
    assert row["status"] == "blocked"
    assert row["title"] == "t"
    # The pass-through blocks the app renders are present.
    assert isinstance(row["usage"], dict) and isinstance(row["work"], dict)
    assert isinstance(row["related"], dict)


# ---------------------------------------------------------------------------
# plan shares (calibrated-or-nothing, children folded into their root)
# ---------------------------------------------------------------------------


def test_plan_pct_folds_children_into_the_root_row(tmp_path):
    service = SentinelService(tmp_path)
    now = time.time()
    _calibrate_claude(service, now=now)
    opus = pc.BASELINE_MODEL_WEIGHTS["claude-opus-4-8"]
    _record_usage(service, session_id="root-a", tokens=10_000_000, updated_at=now - 600)
    _record_usage(
        service,
        session_id="22222222-aaaa-bbbb-cccc-333333333333",
        tokens=5_000_000,
        updated_at=now - 300,
        session_kind="child",
        parent_session_id="root-a",
    )

    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    payload = _sessions(client, limit=50)
    plan_by_client = {entry["client"]: entry for entry in payload["plan"]}
    assert plan_by_client["claude-code"]["confidence"] == "calibrated"
    scale = plan_by_client["claude-code"]["scale"]

    row = _rows_by_id(payload)["root-a"]
    own = 10.0 * opus * scale       # 10 Mtok
    children = 5.0 * opus * scale   # 5 Mtok
    assert abs(row["plan_pct_own"] - own) < 1e-6
    assert abs(row["plan_pct_children"] - children) < 1e-6
    assert abs(row["plan_pct"] - (own + children)) < 1e-6

    # The child row (visible without roots_only) carries only its OWN share —
    # the root's headline already includes it; summing a mixed list must not
    # double-count within one row.
    everyone = _rows_by_id(_sessions(client, roots_only="false", limit=50))
    child = everyone["22222222-aaaa-bbbb-cccc-333333333333"]
    assert abs(child["plan_pct"] - children) < 1e-6
    assert child["plan_pct_children"] is None


def test_plan_pct_is_none_when_uncalibrated(tmp_path):
    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(service, session_id="root-a", tokens=10_000_000, updated_at=now - 600)

    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    payload = _sessions(client)
    row = _rows_by_id(payload)["root-a"]
    assert row["plan_pct"] is None and row["plan_pct_own"] is None


def test_plan_entries_carry_the_three_state_semantic(tmp_path):
    """codex can never calibrate: the payload must say so (``never``), so no
    shell can render a 'calibrating' promise that will not arrive. claude-code
    without history is honestly ``calibrating``."""

    service = SentinelService(tmp_path)
    _record_usage(service, session_id="root-a", tokens=1000, updated_at=time.time() - 60)

    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    plan = {entry["client"]: entry for entry in _sessions(client)["plan"]}
    assert plan["claude-code"]["calibration_state"] == "calibrating"
    assert plan["claude-code"]["calibratable"] is True
    assert plan["codex"]["calibration_state"] == "never"
    assert plan["codex"]["calibratable"] is False
    for entry in plan.values():
        assert isinstance(entry["basis"], str) and entry["basis"]
        assert "scale" in entry and "intervals_used" in entry


# ---------------------------------------------------------------------------
# caches (one ledger build shared across routes and polls)
# ---------------------------------------------------------------------------


def test_ledger_routes_share_one_cached_build(tmp_path, monkeypatch):
    service = SentinelService(tmp_path)
    _record_usage(service, session_id="root-a", tokens=100, updated_at=time.time() - 60)

    calls = {"count": 0}
    real_build = api_module.build_work_ledger

    def _counting_build(*args, **kwargs):
        calls["count"] += 1
        return real_build(*args, **kwargs)

    monkeypatch.setattr(api_module, "build_work_ledger", _counting_build)
    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))

    assert client.get("/sessions").status_code == 200
    assert client.get("/overview").status_code == 200
    assert client.get("/v1/sessions", headers=AUTH).status_code == 200
    assert calls["count"] == 1  # one build served all three routes

    _record_usage(service, session_id="root-b", tokens=100, updated_at=time.time() - 30)
    assert client.get("/sessions").status_code == 200
    assert calls["count"] == 2  # the fingerprint saw the new event


def test_sessions_view_cache_rebuilds_only_on_event_change(tmp_path, monkeypatch):
    service = SentinelService(tmp_path)
    _record_usage(service, session_id="root-a", tokens=100, updated_at=time.time() - 60)

    calls = {"count": 0}
    real_build = api_module.build_v1_sessions_view

    def _counting_build(*args, **kwargs):
        calls["count"] += 1
        return real_build(*args, **kwargs)

    monkeypatch.setattr(api_module, "build_v1_sessions_view", _counting_build)
    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))

    assert client.get("/v1/sessions", headers=AUTH).status_code == 200
    assert client.get("/v1/sessions", headers=AUTH, params={"roots_only": "false"}).status_code == 200
    assert client.get("/v1/sessions", headers=AUTH, params={"offset": 1}).status_code == 200
    assert calls["count"] == 1  # every paging/filter combination slices one cached view

    _record_usage(service, session_id="root-b", tokens=100, updated_at=time.time() - 30)
    assert client.get("/v1/sessions", headers=AUTH).status_code == 200
    assert calls["count"] == 2


def test_ledger_cache_expires_by_age_even_when_events_are_unchanged():
    """The ledger's secondary inputs (run reports, cost events, observations)
    are outside the fingerprint — the TTL is what bounds their staleness."""

    calls = {"count": 0}

    def _build():
        calls["count"] += 1
        return {"n": calls["count"]}

    cache = WorkLedgerCache(max_age_seconds=30.0)
    t0 = time.time()
    assert cache.ledger(1, _build, now=t0) == {"n": 1}
    assert cache.ledger(1, _build, now=t0 + 29) == {"n": 1}   # fresh: same fp, inside TTL
    assert cache.ledger(1, _build, now=t0 + 31) == {"n": 2}   # TTL expired → rebuild
    assert cache.ledger(2, _build, now=t0 + 31.5) == {"n": 3}  # fp change → rebuild
