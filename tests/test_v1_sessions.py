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
    namespace: str | None = "__default__",
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
        source_namespace_fingerprint=(f"sha256:{client}" if namespace == "__default__" else namespace),
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
    kind: str | None = None,
    summary: str | None = None,
) -> None:
    resolved_section = section_id or f"sec-{session_id}-{status}"
    metadata: dict = {
        "client": client,
        "client_session_id": session_id,
        "section_id": resolved_section,
        "section_status": status,
        "section_title": title,
    }
    if kind is not None:
        metadata["kind"] = kind
    if summary is not None:
        metadata["summary"] = summary
    service.append_events_preserving_identity([
        {
            "event_id": f"evt_sec_{client}_{session_id}_{resolved_section}_{int(created_at)}",
            "created_at": created_at,
            "source": client,
            "event_type": f"section_{status}",
            "metadata": metadata,
        }
    ])


def _record_check(
    service: SentinelService,
    *,
    client: str = "claude-code",
    session_id: str,
    section_id: str,
    result: str = "passed",
    evidence_type: str = "test",
    summary: str = "suite green",
    created_at: float,
) -> None:
    service.append_events_preserving_identity([
        {
            "event_id": f"evt_chk_{client}_{session_id}_{section_id}_{result}_{int(created_at)}",
            "created_at": created_at,
            "source": client,
            "event_type": "machine_check",
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "client": client,
                "client_session_id": session_id,
                "section_id": section_id,
                "evidence_type": evidence_type,
                "result": result,
                "summary": summary,
                "command": "pytest -q --token secret",
                "exit_code": 0 if result == "passed" else 1,
            },
        }
    ])


def _calibrate_claude(service: SentinelService, *, now: float) -> None:
    """Recorded 7d history whose movement matches the baseline exactly → the
    fitted scale is ~1.0 (inside the trusted band) and claude-code calibrates.
    The calibration burner sessions are named ``cal-N``."""

    opus = pc.baseline_weight_fresh("claude-opus-4-8")
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
    opus = pc.baseline_weight_fresh("claude-opus-4-8")
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
    assert plan["codex"]["calibration_state"] == "calibrating"
    assert plan["codex"]["calibratable"] is True
    for entry in plan.values():
        assert isinstance(entry["basis"], str) and entry["basis"]
        assert "scale" in entry and "intervals_used" in entry


# ---------------------------------------------------------------------------
# review-finding regressions (adversarial review of PR #66)
# ---------------------------------------------------------------------------


def test_refused_namespace_parent_join_is_not_folded(tmp_path):
    """The ledger refuses a cross-home parent join (namespace mismatch) — the
    plan fold must respect that refusal: the foreign child stays visible as
    its own root with its own share, and the same-id 'parent' row never claims
    another identity's weekly-plan consumption."""

    service = SentinelService(tmp_path)
    now = time.time()
    _calibrate_claude(service, now=now)
    _record_usage(service, session_id="root-a", tokens=10_000_000, updated_at=now - 600)
    _record_usage(
        service,
        session_id="44444444-aaaa-bbbb-cccc-555555555555",
        tokens=5_000_000,
        updated_at=now - 300,
        session_kind="child",
        parent_session_id="root-a",
        namespace="sha256:other-home",
    )

    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    payload = _sessions(client, limit=50)
    rows = _rows_by_id(payload)
    root = rows["root-a"]
    # The ledger's own view of this root says zero children — the plan share
    # must agree, not contradict the same row's related block.
    assert (root["related"] or {}).get("child_session_count") == 0
    assert root["plan_pct_children"] is None
    foreign = rows.get("44444444-aaaa-bbbb-cccc-555555555555")
    assert foreign is not None, "the refused child must stay visible on the roots page"
    assert foreign["plan_pct"] is not None and foreign["plan_pct_children"] is None


def test_orphan_child_appears_as_root_with_its_share(tmp_path):
    """A child whose parent has no rollup entry (no stub entries) is its own
    root on this surface — previously it vanished from the default roots page
    together with its plan share."""

    service = SentinelService(tmp_path)
    now = time.time()
    _calibrate_claude(service, now=now)
    _record_usage(
        service,
        session_id="33333333-aaaa-bbbb-cccc-666666666666",
        tokens=5_000_000,
        updated_at=now - 300,
        session_kind="child",
        parent_session_id="ghost-root",
    )

    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    payload = _sessions(client, limit=50)
    row = _rows_by_id(payload).get("33333333-aaaa-bbbb-cccc-666666666666")
    assert row is not None, "an orphan child must not vanish from the roots page"
    assert row["plan_pct"] is not None


def test_legacy_suffix_lane_folds_like_the_glance(tmp_path):
    """A legacy ':stem' child lane without parent metadata folds into its
    prefix root — and the two /v1 surfaces report the SAME headline number
    for that root instead of the sessions lane re-counting the child as a
    root."""

    service = SentinelService(tmp_path)
    now = time.time()
    _calibrate_claude(service, now=now)
    _record_usage(service, session_id="root-b", tokens=10_000_000, updated_at=now - 600)
    _record_usage(service, session_id="root-b:wf1", tokens=5_000_000, updated_at=now - 300)

    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    payload = _sessions(client, limit=50)
    rows = _rows_by_id(payload)
    assert "root-b:wf1" not in rows, "the legacy child lane must not be listed as a root"
    glance = client.get("/v1/glance", headers=AUTH).json()
    glance_root = {row["session_id"]: row for row in glance["recent_sessions"]}["root-b"]
    assert abs(rows["root-b"]["plan_pct"] - glance_root["plan_pct"]) < 1e-9


def test_title_prefers_the_newest_work_item(tmp_path):
    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(service, session_id="root-a", tokens=100, updated_at=now - 900)
    _record_section(service, session_id="root-a", status="completed", title="old goal",
                    created_at=now - 800, section_id="s1")
    _record_section(service, session_id="root-a", status="started", title="new goal",
                    created_at=now - 100, section_id="s2")

    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    assert _rows_by_id(_sessions(client))["root-a"]["title"] == "new goal"


def test_mutual_parent_cycle_keeps_shares_on_a_visible_root(tmp_path):
    """Corrupt/hostile mutual parent pointers must not make sessions and
    their money vanish from the default roots page: the cycle breaks
    deterministically (min member becomes the root) and carries every
    member's share — on BOTH /v1 surfaces."""

    service = SentinelService(tmp_path)
    now = time.time()
    _calibrate_claude(service, now=now)
    opus = pc.baseline_weight_fresh("claude-opus-4-8")
    _record_usage(service, session_id="cyc-b", tokens=5_000_000, updated_at=now - 300,
                  session_kind="child", parent_session_id="cyc-a")
    _record_usage(service, session_id="cyc-a", tokens=10_000_000, updated_at=now - 200,
                  session_kind="child", parent_session_id="cyc-b")

    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    payload = _sessions(client, limit=50)
    rows = _rows_by_id(payload)
    assert "cyc-a" in rows and "cyc-b" not in rows  # min member is the canonical root
    scale = {entry["client"]: entry for entry in payload["plan"]}["claude-code"]["scale"]
    expected_total = 15.0 * opus * scale
    assert abs(rows["cyc-a"]["plan_pct"] - expected_total) < 1e-6
    glance = client.get("/v1/glance", headers=AUTH).json()
    glance_rows = {row["session_id"]: row for row in glance["recent_sessions"]}
    assert "cyc-a" in glance_rows and "cyc-b" not in glance_rows
    assert abs(glance_rows["cyc-a"]["plan_pct"] - expected_total) < 1e-6


def test_title_skips_untitled_item_placeholders(tmp_path):
    """A newer UNTITLED section (whose item title is backfilled with its
    work_id) must not shadow an older human title."""

    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(service, session_id="root-a", tokens=100, updated_at=now - 900)
    _record_section(service, session_id="root-a", status="completed", title="human goal",
                    created_at=now - 800, section_id="s1")
    _record_section(service, session_id="root-a", status="started", title="",
                    created_at=now - 100, section_id="s2")

    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    assert _rows_by_id(_sessions(client))["root-a"]["title"] == "human goal"


def test_fold_decision_is_event_order_independent(tmp_path):
    """A session whose rows mix source homes must refuse every fold touching
    it, in ANY event order — never 'last fingerprint wins' (round-3 finding:
    reordering the same events flipped the fold decision)."""

    from agentacct.glance import child_root_plan_fold

    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(service, session_id="mixed-p", tokens=100, updated_at=now - 900,
                  namespace="sha256:home-1")
    _record_usage(service, model="claude-fable-5", session_id="mixed-p", tokens=100,
                  updated_at=now - 800, namespace="sha256:home-2")
    _record_usage(service, session_id="77777777-aaaa-bbbb-cccc-888888888888", tokens=100,
                  updated_at=now - 700, session_kind="child", parent_session_id="mixed-p",
                  namespace="sha256:home-1")

    events = service.list_all_events()
    forward = child_root_plan_fold(events)
    backward = child_root_plan_fold(list(reversed(events)))
    assert forward == backward == {}


def test_sections_only_parent_receives_the_fold_on_both_surfaces(tmp_path):
    """An orchestrator that records sections but imports no usage of its own
    still EXISTS — a legacy (fingerprint-less) child's share folds onto it on
    BOTH /v1 surfaces (round-3 finding: the glance treated it as a ghost
    while the sessions lane folded)."""

    service = SentinelService(tmp_path)
    now = time.time()
    _calibrate_claude(service, now=now)
    _record_section(service, session_id="sec-root", status="started", title="orchestrating",
                    created_at=now - 700)
    _record_usage(service, session_id="99999999-aaaa-bbbb-cccc-000000000000",
                  tokens=5_000_000, updated_at=now - 300,
                  session_kind="child", parent_session_id="sec-root", namespace=None)

    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    payload = _sessions(client, limit=50)
    rows = _rows_by_id(payload)
    assert "99999999-aaaa-bbbb-cccc-000000000000" not in rows
    v1_pct = rows["sec-root"]["plan_pct"]
    assert v1_pct is not None
    glance = client.get("/v1/glance", headers=AUTH).json()
    glance_rows = {row["session_id"]: row for row in glance["recent_sessions"]}
    assert "99999999-aaaa-bbbb-cccc-000000000000" not in glance_rows
    assert abs(glance_rows["sec-root"]["plan_pct"] - v1_pct) < 1e-9


def test_metadata_parent_id_joins_raw_not_prefix_split(tmp_path):
    """A metadata parent id like 'rootr:lane' joins RAW (the rollup's rule).
    The glance must not split it and gate against 'rootr' — a session the
    metadata never named (round-3 finding)."""

    service = SentinelService(tmp_path)
    now = time.time()
    _calibrate_claude(service, now=now)
    _record_usage(service, session_id="rootr:lane", tokens=10_000_000, updated_at=now - 600)
    _record_usage(service, session_id="88888888-aaaa-bbbb-cccc-111111111111",
                  tokens=5_000_000, updated_at=now - 300,
                  session_kind="child", parent_session_id="rootr:lane")

    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    payload = _sessions(client, limit=50)
    rows = _rows_by_id(payload)
    assert "88888888-aaaa-bbbb-cccc-111111111111" not in rows
    v1_pct = rows["rootr:lane"]["plan_pct"]
    glance = client.get("/v1/glance", headers=AUTH).json()
    glance_rows = {row["session_id"]: row for row in glance["recent_sessions"]}
    assert abs(glance_rows["rootr:lane"]["plan_pct"] - v1_pct) < 1e-9


def test_ledger_parent_refusal_blocks_the_suffix_fallback(tmp_path):
    """When the ledger refused a recorded parent link (conflicting pointers —
    'missing beats wrong'), the ':' id-shape fallback must not re-fold what
    the ledger refused; both surfaces keep the lane as its own root."""

    service = SentinelService(tmp_path)
    now = time.time()
    _calibrate_claude(service, now=now)
    _record_usage(service, session_id="r5", tokens=10_000_000, updated_at=now - 700)
    _record_usage(service, session_id="r5:lane", tokens=3_000_000, updated_at=now - 600,
                  session_kind="child", parent_session_id="r5")
    _record_usage(service, model="claude-fable-5", session_id="r5:lane", tokens=2_000_000,
                  updated_at=now - 500, session_kind="child", parent_session_id="q5")

    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    payload = _sessions(client, limit=50)
    rows = _rows_by_id(payload)
    lane = rows.get("r5:lane")
    assert lane is not None, "a refused-parent lane must stand as its own root"
    assert rows["r5"]["plan_pct_children"] is None
    glance = client.get("/v1/glance", headers=AUTH).json()
    glance_rows = {row["session_id"]: row for row in glance["recent_sessions"]}
    assert "r5:lane" in glance_rows
    assert abs(glance_rows["r5:lane"]["plan_pct"] - lane["plan_pct"]) < 1e-9


def test_explicit_vs_missing_namespace_refuses_the_fold(tmp_path):
    """A parent whose rows mix a fingerprint with fingerprint-less rows (the
    upgrade-boundary state the ledger quarantines) must refuse folds on the
    glance too — the child keeps its own share on both surfaces."""

    service = SentinelService(tmp_path)
    now = time.time()
    _calibrate_claude(service, now=now)
    _record_usage(service, session_id="root-m", tokens=1_000_000, updated_at=now - 900,
                  namespace=None)
    _record_usage(service, model="claude-fable-5", session_id="root-m", tokens=1_000_000,
                  updated_at=now - 800)
    _record_usage(service, session_id="66666666-aaaa-bbbb-cccc-777777777777",
                  tokens=5_000_000, updated_at=now - 300,
                  session_kind="child", parent_session_id="root-m")

    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    rows = _rows_by_id(_sessions(client, limit=50))
    child = rows.get("66666666-aaaa-bbbb-cccc-777777777777")
    assert child is not None, "the child must stand as its own root"
    assert child["plan_pct"] is not None and child["plan_pct_children"] is None
    glance = client.get("/v1/glance", headers=AUTH).json()
    glance_rows = {row["session_id"]: row for row in glance["recent_sessions"]}
    g_child = glance_rows["66666666-aaaa-bbbb-cccc-777777777777"]
    assert abs(g_child["plan_pct"] - child["plan_pct"]) < 1e-9


def test_descendant_task_label_is_bounded_and_redacted(tmp_path, monkeypatch):
    """The descendants' role enrichment ships a LABEL, not the prompt: first
    line, bounded, secret spans redacted (round-2 findings: a live payload
    blew past 1MB of raw Task prompts, and prompts can carry keys)."""

    import agentacct.subagent_roles as roles_module
    from agentacct.subagent_roles import SubagentRole
    from agentacct.v1_sessions import build_v1_session_detail, build_v1_sessions_view
    from agentacct.work_ledger import build_work_ledger

    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(service, session_id="root-a", tokens=1000, updated_at=now - 900)
    _record_usage(service, session_id="root-a:agent-abc123", tokens=100, updated_at=now - 300,
                  session_kind="child", parent_session_id="root-a")
    _record_usage(service, session_id="root-a:agent-def456", tokens=100, updated_at=now - 200,
                  session_kind="child", parent_session_id="root-a")
    _record_usage(service, session_id="root-a:agent-ghi789", tokens=100, updated_at=now - 100,
                  session_kind="child", parent_session_id="root-a")

    secret = "sk-ant-abc123def456ghi789jkl012mno345pqr678stu901vwx234yz"
    long_line = "review the deploy " + "x" * 400 + " end"
    # A secret that STRADDLES the 160-char bound: it starts before 160 and runs
    # past it, so truncate-then-redact would cut it into a short prefix that
    # could fall under the redactor's min-length floor. redact-then-truncate
    # must scrub it whole regardless of where the bound lands.
    boundary_secret = "sk-ant-" + "z" * 80
    boundary_task = "x" * 149 + " " + boundary_secret + " tail"
    # A first line of ONLY control/format chars (BOM + zero-width space): these
    # survive .strip() (non-whitespace category-C), so the sanitizer returns
    # None. The label path must not crash on that None (regression: an unguarded
    # None[:160] 500'd the whole /v1/session response).
    format_only_task = "﻿​\nDo the real work"
    monkeypatch.setattr(roles_module, "scan_enabled", lambda: True)
    monkeypatch.setattr(
        roles_module,
        "read_roles_for_children",
        lambda parent, ids, projects_root=None: {
            "root-a:agent-abc123": SubagentRole(
                agent_type="Explore",
                task=f"use {secret} then {long_line}\nsecond line ignored",
            ),
            "root-a:agent-def456": SubagentRole(
                agent_type="Explore",
                task=boundary_task,
            ),
            "root-a:agent-ghi789": SubagentRole(
                agent_type="Explore",
                task=format_only_task,
            ),
        },
    )

    events = service.list_all_events()
    ledger = build_work_ledger(events)
    view = build_v1_sessions_view(ledger, events)
    detail = build_v1_session_detail(view, ledger, client="claude-code", session_id="root-a")
    by_id = {c["client_session_id"]: c for c in detail["descendants"]}
    child = by_id["root-a:agent-abc123"]
    assert child["agent_type"] == "Explore"
    assert secret not in (child["task"] or "")
    assert "second line" not in (child["task"] or "")
    assert len(child["task"] or "") <= 170

    # No fragment of the boundary-straddling secret survives, and no bare
    # "sk-ant-" prefix leaks even though the bound falls inside the key.
    boundary_label = by_id["root-a:agent-def456"]["task"] or ""
    assert "sk-ant-" not in boundary_label
    assert "z" * 20 not in boundary_label
    assert len(boundary_label) <= 170

    # A format-only first line sanitizes to None: the enrichment must degrade to
    # task=None (present key) rather than crash the whole detail response.
    assert by_id["root-a:agent-ghi789"]["task"] is None


def test_non_plan_client_detail_plan_block_keeps_the_full_key_set(tmp_path):
    """The no-plan block mirrors plan_status_entry's key set exactly, so the
    schema shape is uniform across clients on this endpoint."""

    service = SentinelService(tmp_path)
    _record_usage(service, client="hermes", model="gpt-5.5", session_id="h1",
                  tokens=1000, updated_at=time.time() - 60)
    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    detail = client.get("/v1/session", headers=AUTH,
                        params={"client": "hermes", "session_id": "h1"}).json()
    plan = detail["plan"]
    for key in ("client", "confidence", "calibration_state", "calibratable", "basis",
                "scale", "alpha", "intervals_used", "intervals_needed", "raw_scale",
                "trusted_band", "state_detail", "by_model"):
        assert key in plan, key


def test_generated_at_is_the_view_build_time(tmp_path):
    """A cache-hit response must not stamp a fresh clock on cached content."""

    service = SentinelService(tmp_path)
    _record_usage(service, session_id="root-a", tokens=100, updated_at=time.time() - 60)
    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    first = _sessions(client)
    second = _sessions(client, offset=0)
    assert second["generated_at"] == first["generated_at"]


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


# ---------------------------------------------------------------------------
# /v1/session detail + /v1/plan
# ---------------------------------------------------------------------------


def test_detail_and_plan_require_the_bearer_token(tmp_path):
    SentinelService(tmp_path)
    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    detail = "/v1/session?client=claude-code&session_id=x"
    assert client.get(detail).status_code == 401
    assert client.get("/v1/plan").status_code == 401
    no_token = TestClient(create_local_api_app(store_dir=tmp_path))
    assert no_token.get(detail, headers=AUTH).status_code == 503
    assert no_token.get("/v1/plan", headers=AUTH).status_code == 503


def test_detail_404_for_an_unknown_session(tmp_path):
    service = SentinelService(tmp_path)
    _record_usage(service, session_id="root-a", tokens=100, updated_at=time.time() - 60)
    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    response = client.get("/v1/session", headers=AUTH,
                          params={"client": "claude-code", "session_id": "nope"})
    assert response.status_code == 404


def test_detail_steps_carry_tui_grade_depth(tmp_path):
    """The bar the owner set: per-step status/kind/title/summary/timestamps
    plus the machine checks with their results — everything the TUI detail
    screen shows, on the wire."""

    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(service, session_id="root-a", tokens=1000, updated_at=now - 900)
    _record_section(service, session_id="root-a", status="started", title="build the thing",
                    created_at=now - 800, section_id="s1", kind="implementation",
                    summary="working on it")
    _record_check(service, session_id="root-a", section_id="s1", result="passed",
                  created_at=now - 700)
    _record_section(service, session_id="root-a", status="completed", title="build the thing",
                    created_at=now - 600, section_id="s1", kind="implementation")

    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    detail = client.get("/v1/session", headers=AUTH,
                        params={"client": "claude-code", "session_id": "root-a"}).json()
    assert detail["schema"] == "agentacct.v1-session-detail.v1"
    assert detail["session"]["client_session_id"] == "root-a"
    assert "is_root" not in detail["session"] and "fold_top" not in detail["session"]
    steps = detail["steps"]
    assert len(steps) == 1
    step = steps[0]
    assert step["section_id"] == "s1"
    assert step["latest_status"] == "completed"
    assert step["kind"] == "implementation"
    assert step["title"] == "build the thing"
    assert step["started_at"] is not None and step["updated_at"] is not None
    assert isinstance(step["models"], list)
    assert step["evidence_status"] == "strong"
    checks = step["checks"]
    assert len(checks) == 1
    assert checks[0]["evidence_type"] == "test"
    assert checks[0]["result"] == "passed"
    assert checks[0]["summary"] == "suite green"
    assert checks[0]["exit_code"] == 0
    # No raw command exists on this wire at all — only the boolean disclosure
    # that one was recorded (the ledger's evidence projection never carries
    # the string). Regression for the booleans-under-string-keys finding.
    assert "command" not in checks[0]
    assert checks[0]["command_redacted"] is True
    assert "pytest -q --token secret" not in str(checks[0])
    # M2 per-step grade on the wire — the PRIMARY signal the app's StepCard reads.
    # An agent-reported passing check grades self_checked (never independent), and
    # the check carries its trusted source_type so a surface can show independence.
    assert step["evidence_grade"] == "self_checked"
    assert step["evidence_grade_reason"]
    assert checks[0]["source_type"] == "mcp_agent_reported"


def test_detail_descendants_and_plan_block(tmp_path):
    service = SentinelService(tmp_path)
    now = time.time()
    _calibrate_claude(service, now=now)
    opus = pc.baseline_weight_fresh("claude-opus-4-8")
    _record_usage(service, session_id="root-a", tokens=10_000_000, updated_at=now - 600)
    _record_usage(service, session_id="22222222-aaaa-bbbb-cccc-333333333333",
                  tokens=5_000_000, updated_at=now - 300,
                  session_kind="child", parent_session_id="root-a")

    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    detail = client.get("/v1/session", headers=AUTH,
                        params={"client": "claude-code", "session_id": "root-a"}).json()
    scale = detail["plan"]["scale"]
    assert detail["plan"]["confidence"] == "calibrated"
    assert abs(detail["plan"]["pct_own"] - 10.0 * opus * scale) < 1e-6
    assert abs(detail["plan"]["pct_children"] - 5.0 * opus * scale) < 1e-6
    assert abs(detail["plan"]["pct"] - 15.0 * opus * scale) < 1e-6
    by_model = {entry["model"]: entry for entry in detail["plan"]["by_model"]}
    assert abs(by_model["claude-opus-4-8"]["pct"] - 10.0 * opus * scale) < 1e-6
    descendants = detail["descendants"]
    assert len(descendants) == 1
    child = descendants[0]
    assert child["client_session_id"] == "22222222-aaaa-bbbb-cccc-333333333333"
    assert abs(child["plan_pct"] - 5.0 * opus * scale) < 1e-6


def test_observed_models_falls_back_and_unions_with_usage():
    """A session's model list is completed from the authoritative usage records
    when the instrumentation-sourced observed_models is thin — so a multi-model
    (e.g. mid-run model switch) session never shows a blank at the row level."""

    from agentacct.v1_sessions import _observed_models_with_usage_fallback

    usage = {
        "identity_models": ["claude-opus-4-8", "claude-fable-5"],
        "model_lanes": [
            {"model": "claude-fable-5"},
            {"model": "claude-opus-4-8"},
            {"model": "claude-haiku-4-5-20251001"},
        ],
    }
    # Empty instrumentation → the models come entirely from usage.
    got = _observed_models_with_usage_fallback([], usage)
    assert got == ["claude-opus-4-8", "claude-fable-5", "claude-haiku-4-5-20251001"]

    # Instrumentation order is preserved first; usage adds only what's missing.
    got2 = _observed_models_with_usage_fallback(["claude-fable-5"], usage)
    assert got2 == ["claude-fable-5", "claude-opus-4-8", "claude-haiku-4-5-20251001"]

    # No models anywhere → null-not-empty on the wire.
    assert _observed_models_with_usage_fallback(None, None) is None
    assert _observed_models_with_usage_fallback([], {"identity_models": []}) is None


def test_step_models_join_unit():
    """The attribution join, in isolation: tokens come from the attribution
    rows, dangling usage events are skipped, a KNOWN event without a model
    keeps a null lane (lane sums must reconcile with the step total), lanes
    sort by tokens."""

    from agentacct.v1_sessions import _step_models

    attributions = {
        "w1": [
            {"usage_event_id": "u1", "usage_tokens": 100},
            {"usage_event_id": "u2", "usage_tokens": 900},
            {"usage_event_id": "u3", "usage_tokens": 400},
            {"usage_event_id": "missing", "usage_tokens": 50},
        ]
    }
    models = {
        "u1": ("claude-opus-4-8", "claude-code"),
        "u2": ("claude-fable-5", "claude-code"),
        "u3": (None, "codex"),
    }
    lanes = _step_models("w1", attributions, models)
    assert [lane["model"] for lane in lanes] == ["claude-fable-5", None, "claude-opus-4-8"]
    assert lanes[0]["total_tokens"] == 900
    assert lanes[1]["total_tokens"] == 400  # model-less lane kept, not dropped
    assert _step_models("unknown", attributions, models) == []


def test_step_cost_is_none_when_nothing_priced():
    """estimated_cost_usd on the wire means None-never-$0 when nothing was
    priced — the ledger's internal 0.0 default must not leak as a measured
    zero (adversarial-review finding)."""

    from agentacct.v1_sessions import _project_step

    unpriced = _project_step(
        {"work_id": "w", "estimated_cost_total": 0.0, "priced_usage_records": 0,
         "unpriced_usage_records": 2, "work": {}},
        [],
    )
    assert unpriced["usage"]["estimated_cost_usd"] is None
    priced = _project_step(
        {"work_id": "w", "estimated_cost_total": 1.25, "priced_usage_records": 2,
         "unpriced_usage_records": 0, "work": {}},
        [],
    )
    assert priced["usage"]["estimated_cost_usd"] == 1.25


def test_detail_reuses_the_cached_fit_no_per_request_usage_view(tmp_path, monkeypatch):
    """A polling detail screen must not pay a full usage-view rebuild per
    request: everything plan-shaped comes from the cached view's own fit
    (which also keeps the payload self-consistent)."""

    import agentacct.usage_snapshot as usage_snapshot_module

    service = SentinelService(tmp_path)
    _record_usage(service, session_id="root-a", tokens=1000, updated_at=time.time() - 60)
    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    assert client.get("/v1/sessions", headers=AUTH).status_code == 200  # warm the view

    calls = {"count": 0}
    real = usage_snapshot_module.usage_records

    def _counting(*args, **kwargs):
        calls["count"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(usage_snapshot_module, "usage_records", _counting)
    detail = client.get("/v1/session", headers=AUTH,
                        params={"client": "claude-code", "session_id": "root-a"})
    assert detail.status_code == 200
    assert calls["count"] == 0  # pure cache lookups, no usage-view rebuild


def test_plan_endpoint_uncalibrated_carries_states_not_numbers(tmp_path):
    service = SentinelService(tmp_path)
    _record_usage(service, session_id="root-a", tokens=1_000_000, updated_at=time.time() - 60)
    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    payload = client.get("/v1/plan", headers=AUTH).json()
    assert payload["schema"] == "agentacct.v1-plan.v1"
    clients = {entry["client"]: entry for entry in payload["clients"]}
    assert clients["claude-code"]["calibration_state"] == "calibrating"
    assert clients["claude-code"]["window_pcts"] is None
    assert clients["claude-code"]["daily"] is None
    assert clients["codex"]["calibration_state"] == "calibrating"
    assert clients["codex"]["by_model"] is None


def test_plan_endpoint_calibrated_aggregates_agree(tmp_path):
    service = SentinelService(tmp_path)
    now = time.time()
    _calibrate_claude(service, now=now)
    opus = pc.baseline_weight_fresh("claude-opus-4-8")
    _record_usage(service, session_id="root-a", tokens=10_000_000, updated_at=now - 60)

    client = TestClient(create_local_api_app(store_dir=tmp_path, v1_auth_token=TOKEN))
    payload = client.get("/v1/plan", headers=AUTH, params={"days": 14}).json()
    entry = {row["client"]: row for row in payload["clients"]}["claude-code"]
    assert entry["confidence"] == "calibrated"
    scale = entry["scale"]
    # All calibration usage (400M) + root-a (10M) landed within 7 days. The
    # calibration hours can straddle local midnight (a test running at 1am),
    # so "today" is only bounded, not pinned: it must include at least
    # root-a's just-now share and never exceed the window total.
    expected_total = 410.0 * opus * scale
    assert abs(entry["window_pcts"]["7d"] - expected_total) < 1e-6
    assert 10.0 * opus * scale - 1e-6 <= entry["window_pcts"]["today"] <= expected_total + 1e-6
    daily = entry["daily"]
    assert len(daily) == 14  # trailing days incl. empty ones
    assert abs(sum(day["pct"] for day in daily) - expected_total) < 1e-6
    by_model = {row["model"]: row for row in entry["by_model"]}
    assert abs(by_model["claude-opus-4-8"]["pct"] - expected_total) < 1e-6
    # Cache: an unchanged store serves the same build (same generated_at).
    again = client.get("/v1/plan", headers=AUTH, params={"days": 14}).json()
    assert again["generated_at"] == payload["generated_at"]
