"""Usage-cube and /usage/summary regressions (originally PRD §5 Tokens explorer).

The usage cube (pure aggregation: bucketing, weekly rollup, empty periods,
distinct-session counting, dominant/mixed cost confidence with its shared
label rule, unknown-time guard, quarantine of non-additive rows,
cache-reporting capability, stored-cost basis) and the /usage/summary JSON
endpoint (shape, whitelist validation, the locked unknown-model-echoes-empty
decision, trusted-import-only intake, range context). The /tokens HTML page
and its SVG chart were retired with the HTML display layer; the data
contracts they rendered live on here against the JSON lane.

All stores are throwaway tmp_path stores (suite conftest guards the real
dogfood ledger)."""

import time
from datetime import date, datetime, time as dtime, timedelta

import pytest
from fastapi.testclient import TestClient

import agentacct.api as api_module
from agentacct.api import create_local_api_app
from agentacct.usage_view import DashboardUsageRecord, _usage_record_time
from agentacct.service import SentinelService
from agentacct.usage_cube import (
    build_usage_cube,
    client_lane_class,
    filter_usage_records,
    models_in_records,
    resolve_granularity,
    week_start,
)

TODAY = date(2026, 7, 10)


def _ts(day: date, hour: int = 12) -> float:
    return datetime.combine(day, dtime(hour, 0)).timestamp()


def _cube_record(
    *,
    client="codex",
    provider=None,
    model="gpt-5.5",
    session="session-1",
    day=None,
    timestamp=None,
    input_tokens=100,
    output_tokens=25,
    cache_creation=0,
    cache_read=0,
    cache_creation_reported=True,
    cache_read_reported=True,
    cost=0.01,
    cost_confidence="estimated_from_tokens",
):
    if timestamp is None and day is not None:
        timestamp = _ts(day)
    return DashboardUsageRecord(
        client=client,
        provider=provider or client,
        model=model,
        session_id=session,
        session_kind="root",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cache_creation + cache_read,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
        cache_creation_tokens_reported=cache_creation_reported,
        cache_read_tokens_reported=cache_read_reported,
        estimated_cost_usd=cost,
        cost_confidence=cost_confidence,
        started_at=timestamp,
        updated_at=timestamp,
    )


def _cube(records, **kwargs):
    kwargs.setdefault("today", TODAY)
    return build_usage_cube(records, record_time=_usage_record_time, **kwargs)


def _trusted_usage(
    store_root,
    *,
    session,
    client="codex",
    model="gpt-5.5",
    input_tokens=100,
    output_tokens=25,
    cache_creation=0,
    cache_read=0,
    cost=0.01,
    cost_confidence="estimated_from_tokens",
    started_at=None,
    project_dir=None,
    cache_creation_reported=True,
    cache_read_reported=True,
    session_kind="root",
    parent_session=None,
):
    metadata = {
        "usage_source": "local_client_session_store",
        "client": client,
        "client_session_id": session,
        "client_session_kind": session_kind,
        "parent_client_session_id": parent_session,
        "cached_input_tokens": cache_creation + cache_read,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
    }
    metadata["cache_creation_tokens_reported"] = cache_creation_reported
    metadata["cache_read_tokens_reported"] = cache_read_reported
    if started_at is not None:
        metadata["started_at"] = started_at
        metadata["updated_at"] = started_at
    if project_dir is not None:
        metadata["project_dir"] = project_dir
    return SentinelService(store_root).record_event(
        {
            "source": f"{client}-local-session-import",
            "event_type": "model_usage",
            "provider": client,
            "model": model,
            "estimated_input_tokens": input_tokens,
            "estimated_output_tokens": output_tokens,
            "estimated_cost_usd": cost,
            "usage_confidence": "client_reported",
            "cost_confidence": cost_confidence,
            "metadata": metadata,
        },
        trusted_usage_import=True,
    )


def _client(store_root, **kwargs):
    return TestClient(create_local_api_app(store_dir=store_root, **kwargs))


# ---------------------------------------------------------------------------
# Usage cube — pure unit tests
# ---------------------------------------------------------------------------


def test_cube_daily_bucketing_totals_and_unknown_period(tmp_path):
    records = [
        _cube_record(day=TODAY, session="a", input_tokens=100, output_tokens=25, cache_creation=10, cache_read=1000),
        _cube_record(day=TODAY - timedelta(days=2), session="b", input_tokens=50, output_tokens=5),
        # Absurd-but-finite client-authored timestamp: buckets as unknown,
        # never crashes (the bad-timestamp guard).
        _cube_record(timestamp=1e300, session="c", input_tokens=7, output_tokens=3),
    ]

    cube = _cube(records, days=None, granularity="daily")

    totals = cube["totals"]
    assert totals["rows"] == 3
    assert totals["sessions"] == 3
    assert totals["fresh_tokens"] == 190
    assert totals["input_tokens"] == 157
    assert totals["output_tokens"] == 33
    assert totals["cache_creation_tokens"] == 10
    assert totals["cache_read_tokens"] == 1000
    assert totals["total_tokens_including_cached"] == 190 + 1010
    assert totals["unknown_time_rows"] == 1
    # Empty day gap-filled between the two dated rows; unknown sorts last.
    periods = [entry["period"] for entry in cube["by_period"]]
    assert periods == ["2026-07-08", "2026-07-09", "2026-07-10", "unknown"]
    empty_day = cube["by_period"][1]
    assert empty_day["rows"] == 0
    assert empty_day["fresh_tokens"] == 0
    assert empty_day["estimated_cost_usd"] is None
    assert empty_day["by_client"] == {}
    # The chart's stacking input rides along per period.
    assert cube["by_period"][2]["by_client"]["codex"]["fresh_tokens"] == 125
    assert cube["by_period"][2]["by_client"]["codex"]["cache_read_tokens"] == 1000


def test_cube_bounded_range_excludes_out_of_range_and_unknown_time_rows():
    records = [
        _cube_record(day=TODAY, session="in-range"),
        _cube_record(day=TODAY - timedelta(days=7), session="too-old"),  # start is TODAY-6
        _cube_record(timestamp=float("inf"), session="bad-ts"),
    ]

    cube = _cube(records, days=7, granularity="daily")

    assert cube["totals"]["rows"] == 1
    assert cube["totals"]["sessions"] == 1
    # Unknown-time rows cannot honestly join a bounded date range; they are
    # excluded AND counted, never silently dropped.
    assert cube["totals"]["unknown_time_rows"] == 1
    periods = [entry["period"] for entry in cube["by_period"]]
    assert periods == [(TODAY - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]
    assert "unknown" not in periods


def test_cube_weekly_rollup_labels_by_week_start_and_fills_empty_weeks():
    monday_a = date(2026, 6, 22)
    monday_c = date(2026, 7, 6)
    records = [
        _cube_record(day=date(2026, 6, 24), session="a"),  # Wednesday of week A
        _cube_record(day=date(2026, 7, 8), session="b"),  # Wednesday of week C
    ]

    cube = _cube(records, days=None, granularity="weekly")

    assert week_start(date(2026, 6, 24)) == monday_a
    periods = [entry["period"] for entry in cube["by_period"]]
    assert periods == ["2026-06-22", "2026-06-29", "2026-07-06"]
    assert cube["by_period"][0]["rows"] == 1
    assert cube["by_period"][1]["rows"] == 0  # the empty week is information
    assert [entry["period"] for entry in cube["by_period"] if entry["rows"]] == [
        monday_a.isoformat(),
        monday_c.isoformat(),
    ]


def test_cube_sessions_count_distinct_base_session_ids():
    records = [
        # Two per-model lane rows of ONE claude-code session (same base id).
        _cube_record(client="claude-code", model="fable-5", session="base-1", day=TODAY),
        _cube_record(client="claude-code", model="haiku-4", session="base-1", day=TODAY),
        # Same session id string on a DIFFERENT client is a different session.
        _cube_record(client="codex", model="gpt-5.5", session="base-1", day=TODAY),
    ]

    cube = _cube(records, days=30, granularity="daily")

    assert cube["totals"]["rows"] == 3
    assert cube["totals"]["sessions"] == 2
    by_client = {entry["client"]: entry for entry in cube["by_client"]}
    assert by_client["claude-code"]["sessions"] == 1
    assert by_client["claude-code"]["rows"] == 2
    assert by_client["claude-code"]["models"] == ["fable-5", "haiku-4"]
    # by_model keys on (client, provider, model): the two lanes stay separate.
    assert len(cube["by_model"]) == 3
    assert all(entry["sessions"] == 1 for entry in cube["by_model"])


def test_cube_client_and_model_filters_and_unknown_model_is_empty():
    records = [
        _cube_record(client="codex", model="gpt-5.5", session="a", day=TODAY),
        _cube_record(client="claude-code", model="fable-5", session="b", day=TODAY),
    ]

    codex_only = _cube(records, client="codex", days=30, granularity="daily")
    assert codex_only["totals"]["rows"] == 1
    assert [entry["client"] for entry in codex_only["by_client"]] == ["codex"]

    model_only = _cube(records, model="fable-5", days=30, granularity="daily")
    assert model_only["totals"]["rows"] == 1
    assert [entry["model"] for entry in model_only["by_model"]] == ["fable-5"]

    # Unknown model -> truly empty result (no gap-filled zero wall either).
    unknown = _cube(records, model="never-seen", days=30, granularity="daily")
    assert unknown["totals"]["rows"] == 0
    assert unknown["by_client"] == []
    assert unknown["by_model"] == []
    assert unknown["by_period"] == []

    assert models_in_records(records) == ["fable-5", "gpt-5.5"]


def test_cube_dominant_cost_confidence_and_mixed_flag():
    records = [
        _cube_record(session="a", day=TODAY, cost=0.10, cost_confidence="estimated_from_tokens"),
        _cube_record(session="b", day=TODAY, cost=0.20, cost_confidence="estimated_from_tokens"),
        _cube_record(session="c", day=TODAY, cost=0.30, cost_confidence="client_reported"),
    ]

    cube = _cube(records, days=30, granularity="daily")
    totals = cube["totals"]
    assert totals["estimated_cost_usd"] == pytest.approx(0.60)
    assert totals["cost_confidence"] == "estimated_from_tokens"
    assert totals["cost_confidence_mixed"] is True
    assert totals["priced_rows"] == 3

    single = _cube(records[:2], days=30, granularity="daily")
    assert single["totals"]["cost_confidence"] == "estimated_from_tokens"
    assert single["totals"]["cost_confidence_mixed"] is False

    # No priced row -> cost is None (never a fake $0.00) and no confidence.
    unpriced = _cube([_cube_record(session="d", day=TODAY, cost=None)], days=30, granularity="daily")
    assert unpriced["totals"]["estimated_cost_usd"] is None
    assert unpriced["totals"]["cost_confidence"] is None
    assert unpriced["totals"]["cost_confidence_mixed"] is False
    assert unpriced["totals"]["unpriced_rows"] == 1


def test_cube_filter_rule_is_shared_with_per_record_views():
    records = [
        _cube_record(client="codex", session="a", day=TODAY),
        _cube_record(client="claude-code", session="b", day=TODAY),
        _cube_record(client="codex", session="c", day=TODAY - timedelta(days=40)),
    ]

    kept, unknown_time_rows = filter_usage_records(
        records, record_time=_usage_record_time, client="codex", days=30, today=TODAY
    )

    assert [record.session_id for record in kept] == ["a"]
    assert unknown_time_rows == 0
    assert resolve_granularity("30", "auto") == "daily"
    assert resolve_granularity("90", "auto") == "weekly"
    assert resolve_granularity("all", "daily") == "daily"
    assert client_lane_class("codex") == "lane-codex"
    assert client_lane_class("cursor") == "lane-cursor"
    assert client_lane_class("mystery-agent") == "lane-other"


# ---------------------------------------------------------------------------
# GET /usage/summary — shape, validation, honesty of the intake
# ---------------------------------------------------------------------------


def test_usage_summary_shape_totals_and_periods(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    _trusted_usage(store_root, session="sum-a", client="codex", started_at=now - 3600, cache_read=500)
    _trusted_usage(store_root, session="sum-b", client="claude-code", model="fable-5", started_at=now - 86400)
    client = _client(store_root)

    payload = client.get("/usage/summary").json()

    assert set(payload) == {
        "schema_version",
        "filters_echo",
        "totals",
        "usage_exclusions",
        "range_context",
        "by_client",
        "by_model",
        "by_period",
    }
    assert payload["schema_version"] == "agent-sentinel.usage-summary.v1"
    assert payload["filters_echo"] == {
        "client": "all",
        "model": "all",
        "days": "30",
        "granularity": "daily",
        "granularity_requested": "auto",
        "model_matches_saved_rows": True,
    }
    assert payload["usage_exclusions"] == {
        "non_additive_rows": 0,
        "unknown_time_rows": 0,
        "reason": "legacy_codex_descendant_cumulative_unproven",
        "raw_evidence_preserved": True,
    }
    assert payload["range_context"] == {"history_outside_range": []}
    totals = payload["totals"]
    assert totals["rows"] == 2
    assert totals["sessions"] == 2
    assert totals["fresh_tokens"] == 250
    assert totals["cache_read_tokens"] == 500
    assert totals["cost_confidence"] == "estimated_from_tokens"
    # 30 daily periods including empty ones; period rows sum to the totals.
    assert len(payload["by_period"]) == 30
    assert sum(entry["rows"] for entry in payload["by_period"]) == totals["rows"]
    assert sum(entry["fresh_tokens"] for entry in payload["by_period"]) == totals["fresh_tokens"]
    assert {entry["client"] for entry in payload["by_client"]} == {"codex", "claude-code"}
    assert {entry["model"] for entry in payload["by_model"]} == {"gpt-5.5", "fable-5"}
    # Filtering by client narrows totals and echoes the filter.
    codex = client.get("/usage/summary?client=codex").json()
    assert codex["filters_echo"]["client"] == "codex"
    assert codex["totals"]["rows"] == 1
    assert [entry["client"] for entry in codex["by_client"]] == ["codex"]


def test_usage_summary_explains_filtered_out_history_without_polluting_current_cube(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    latest_hermes = now - 45 * 24 * 60 * 60
    _trusted_usage(store_root, session="current-codex", client="codex", started_at=now - 3600)
    _trusted_usage(
        store_root,
        session="older-hermes-a",
        client="hermes",
        model="gpt-5.4-mini",
        started_at=latest_hermes,
    )
    _trusted_usage(
        store_root,
        session="older-hermes-b",
        client="hermes",
        model="gpt-5.4-mini",
        started_at=now - 60 * 24 * 60 * 60,
    )
    client = _client(store_root)

    payload = client.get("/usage/summary").json()

    # The normal cube remains strictly bounded: no synthetic Hermes zero
    # bucket is inserted. Additive context says why that saved lane vanished.
    assert [row["client"] for row in payload["by_client"]] == ["codex"]
    assert payload["range_context"]["history_outside_range"] == [
        {
            "client": "hermes",
            "rows": 2,
            "sessions": 2,
            "latest_activity_at": pytest.approx(latest_hermes),
        }
    ]

    # The same client/model filters apply to both the bounded and all-time
    # populations used to derive the context.
    hermes = client.get("/usage/summary?client=hermes&model=gpt-5.4-mini").json()
    assert hermes["by_client"] == []
    assert [row["client"] for row in hermes["range_context"]["history_outside_range"]] == ["hermes"]
    codex_model = client.get("/usage/summary?model=gpt-5.5").json()
    assert codex_model["range_context"]["history_outside_range"] == []

    # All time already includes the saved rows, so no outside-range context
    # remains to explain.
    all_time = client.get("/usage/summary?days=all").json()
    assert {row["client"] for row in all_time["by_client"]} == {"codex", "hermes"}
    assert all_time["range_context"] == {"history_outside_range": []}


def test_usage_summary_range_context_fails_closed_for_unknown_time_history(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(
        store_root,
        session="unknown-hermes",
        client="hermes",
        model="gpt-5.4-mini",
        started_at=1e300,
    )
    client = _client(store_root)

    bounded = client.get("/usage/summary?client=hermes").json()
    all_time = client.get("/usage/summary?client=hermes&days=all").json()

    assert bounded["by_client"] == []
    assert bounded["totals"]["unknown_time_rows"] == 1
    assert bounded["range_context"]["history_outside_range"] == []
    assert all_time["by_client"][0]["rows"] == 1
    assert all_time["range_context"]["history_outside_range"] == []


def test_usage_summary_does_not_call_future_rows_preserved_history(tmp_path):
    store_root = tmp_path / "state"
    future = time.time() + 45 * 24 * 60 * 60
    _trusted_usage(
        store_root,
        session="future-hermes",
        client="hermes",
        model="gpt-5.4-mini",
        started_at=future,
    )
    client = _client(store_root)

    bounded = client.get("/usage/summary?client=hermes").json()
    all_time = client.get("/usage/summary?client=hermes&days=all").json()

    assert bounded["by_client"] == []
    assert bounded["range_context"]["history_outside_range"] == []
    assert all_time["by_client"][0]["rows"] == 1


def test_usage_summary_range_context_fails_closed_for_mixed_old_and_future_rows(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    _trusted_usage(
        store_root,
        session="older-hermes",
        client="hermes",
        started_at=now - 45 * 24 * 60 * 60,
    )
    _trusted_usage(
        store_root,
        session="future-hermes",
        client="hermes",
        started_at=now + 45 * 24 * 60 * 60,
    )
    client = _client(store_root)

    bounded = client.get("/usage/summary?client=hermes").json()
    all_time = client.get("/usage/summary?client=hermes&days=all").json()

    assert bounded["by_client"] == []
    assert bounded["range_context"]["history_outside_range"] == []
    assert all_time["by_client"][0]["rows"] == 2


def test_usage_summary_range_context_uses_held_rows_from_the_same_all_time_cube(tmp_path):
    store_root = tmp_path / "state"
    old = time.time() - 45 * 24 * 60 * 60
    _trusted_usage(
        store_root,
        session="held-old-codex-child",
        client="codex",
        session_kind="child",
        parent_session="missing-parent",
        started_at=old,
    )
    client = _client(store_root)

    bounded = client.get("/usage/summary?client=codex").json()

    assert bounded["by_client"] == []
    assert bounded["range_context"]["history_outside_range"] == [
        {
            "client": "codex",
            "rows": 1,
            "sessions": 1,
            "latest_activity_at": pytest.approx(old),
        }
    ]


def test_usage_summary_quarantines_huge_codex_descendant_without_hiding_raw_evidence(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    _trusted_usage(
        store_root,
        session="codex-root",
        client="codex",
        input_tokens=100,
        output_tokens=25,
        cache_read=500,
        cost=0.01,
        started_at=now - 3600,
    )
    _trusted_usage(
        store_root,
        session="codex-child",
        client="codex",
        session_kind="child",
        parent_session="codex-root",
        input_tokens=9_000_000_000,
        output_tokens=2_000_000_000,
        cache_read=70_000_000_000,
        cost=999.0,
        started_at=now - 1800,
    )
    client = _client(store_root)

    payload = client.get("/usage/summary?days=all").json()

    # Both saved rows remain countable evidence, but only the independently
    # additive root enters token and cost subtotals. The cumulative descendant
    # is explicit instead of inflating the aggregate by orders of magnitude.
    assert payload["totals"]["rows"] == 2
    assert payload["totals"]["additive_rows"] == 1
    assert payload["totals"]["excluded_non_additive_rows"] == 1
    assert payload["totals"]["sessions"] == 2
    assert payload["totals"]["fresh_tokens"] == 125
    assert payload["totals"]["cache_read_tokens"] == 500
    assert payload["totals"]["total_tokens_including_cached"] == 625
    assert payload["totals"]["estimated_cost_usd"] is None
    assert payload["totals"]["known_additive_cost_usd"] == pytest.approx(0.01)
    assert payload["totals"]["cost_complete"] is False
    assert payload["totals"]["usage_availability"] == "partial"
    assert payload["usage_exclusions"] == {
        "non_additive_rows": 1,
        "unknown_time_rows": 0,
        "reason": "legacy_codex_descendant_cumulative_unproven",
        "raw_evidence_preserved": True,
    }
    assert [row["client"] for row in payload["by_client"]] == ["codex"]
    assert payload["by_client"][0]["rows"] == 2
    assert payload["by_client"][0]["additive_rows"] == 1
    assert payload["by_client"][0]["excluded_non_additive_rows"] == 1

    # Quarantine is a derived aggregation rule, not deletion: the event
    # endpoint still exposes the row's original client counters for
    # forensic inspection.
    events = client.get("/events?limit=10").json()["events"]
    child = next(event for event in events if event["metadata"].get("client_session_id") == "codex-child")
    assert child["estimated_input_tokens"] == 9_000_000_000
    assert child["estimated_output_tokens"] == 2_000_000_000
    assert child["metadata"]["cache_read_input_tokens"] == 70_000_000_000


def test_all_held_codex_usage_stays_unavailable_not_a_zero_usage_or_cost_claim(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(
        store_root,
        session="held-only-child",
        client="codex",
        session_kind="internal",
        parent_session="missing-parent",
        input_tokens=7_000_000_000,
        output_tokens=1_000_000_000,
        cache_read=30_000_000_000,
        cost=777.0,
        started_at=time.time() - 3600,
    )
    client = _client(store_root)

    payload = client.get("/usage/summary?days=all").json()

    assert payload["totals"]["rows"] == 1
    assert payload["totals"]["additive_rows"] == 0
    assert payload["totals"]["excluded_non_additive_rows"] == 1
    assert payload["totals"]["estimated_cost_usd"] is None
    assert payload["totals"]["usage_availability"] == "held"
    assert payload["usage_exclusions"]["non_additive_rows"] == 1
    assert payload["usage_exclusions"]["raw_evidence_preserved"] is True

    # Raw evidence stays inspectable even while held out of every subtotal.
    raw_events = client.get("/events?limit=10").json()["events"]
    held = next(event for event in raw_events if event["metadata"].get("client_session_id") == "held-only-child")
    assert held["estimated_input_tokens"] == 7_000_000_000
    assert held["metadata"]["cache_read_input_tokens"] == 30_000_000_000


def test_usage_summary_whitelist_validation_and_granularity_override(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="gran", started_at=time.time() - 3600)
    client = _client(store_root)

    assert client.get("/usage/summary?client=not-a-client").status_code == 422
    assert client.get("/usage/summary?days=14").status_code == 422
    assert client.get("/usage/summary?granularity=hourly").status_code == 422

    weekly = client.get("/usage/summary?days=90").json()
    assert weekly["filters_echo"]["granularity"] == "weekly"
    # Weekly periods are labeled by their ISO week start (a Monday).
    first_period = date.fromisoformat(weekly["by_period"][0]["period"])
    assert first_period.isoweekday() == 1

    overridden = client.get("/usage/summary?days=90&granularity=daily").json()
    assert overridden["filters_echo"]["granularity"] == "daily"
    assert overridden["filters_echo"]["granularity_requested"] == "daily"
    assert len(overridden["by_period"]) == 90


def test_usage_summary_unknown_model_returns_empty_result_with_echo(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="model-a", started_at=time.time() - 3600)
    client = _client(store_root)

    payload = client.get("/usage/summary?model=never-imported").json()

    # Locked decision: unknown model -> EMPTY result with the filter echoed,
    # never a guess and never a 422 (model names are data, not a whitelist).
    assert payload["filters_echo"]["model"] == "never-imported"
    assert payload["filters_echo"]["model_matches_saved_rows"] is False
    assert payload["totals"]["rows"] == 0
    assert payload["by_client"] == []
    assert payload["by_model"] == []
    assert payload["by_period"] == []


def test_usage_summary_uses_trusted_import_rows_only_and_never_scans(tmp_path, monkeypatch):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="trusted-row", started_at=time.time() - 3600)
    service = SentinelService(store_root)
    # A model_usage event WITHOUT the trusted-import provenance (e.g. an
    # agent-reported estimate) must not enter the cube.
    service.record_event(
        {
            "source": "codex",
            "event_type": "model_usage",
            "provider": "codex",
            "model": "gpt-5.5",
            "estimated_input_tokens": 999_999,
            "estimated_output_tokens": 999_999,
            "metadata": {"client": "codex", "client_session_id": "untrusted-row"},
        }
    )
    # Chronicle's own diagnostic traffic stays out of product views entirely.
    service.record_event(
        {
            "source": "agent-sentinel-mcp-workflow-smoke",
            "event_type": "workflow_smoke",
            "estimated_input_tokens": 555_555,
            "metadata": {"summary": "diagnostic self-test"},
        }
    )

    def _boom(*args, **kwargs):
        raise AssertionError("the live client-log scan must not run for /usage/summary")

    monkeypatch.setattr(api_module, "_discover_local_usage", _boom)
    monkeypatch.setattr(api_module, "_discover_local_usage_sources", _boom)
    client = _client(store_root)

    payload = client.get("/usage/summary").json()

    assert payload["totals"]["rows"] == 1
    assert payload["totals"]["fresh_tokens"] == 125


# ---------------------------------------------------------------------------
# Cache-reporting capability — the cube states reporting status instead of
# inventing zeros. (The /tokens HTML surface is retired; the JSON data lane
# keeps the honesty contract.)
# ---------------------------------------------------------------------------


def test_codex_cache_write_counter_not_reported_stays_not_reported_not_zero(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(
        store_root,
        session="codex-no-write-counter",
        client="codex",
        input_tokens=100,
        output_tokens=25,
        cache_read=50,
        cache_creation_reported=False,
        started_at=time.time() - 3600,
    )
    client = _client(store_root)

    totals = client.get("/usage/summary?client=codex&days=all").json()["totals"]

    # Codex does not expose a distinct cache-write counter: the cube says so
    # ("not_reported") instead of presenting a fabricated zero as a report.
    assert totals["cache_creation_reporting"] == "not_reported"
    assert totals["cache_creation_unreported_rows"] == 1
    assert totals["cache_creation_tokens"] == 0
    assert totals["cache_read_reporting"] == "reported"
    assert totals["cache_read_tokens"] == 50


def test_saved_legacy_row_without_capability_flags_stays_unknown_in_api(tmp_path):
    store_root = tmp_path / "state"
    SentinelService(store_root).record_event(
        {
            "source": "codex-local-session-import",
            "event_type": "model_usage",
            "provider": "codex",
            "model": "legacy-model",
            "estimated_input_tokens": 100,
            "estimated_output_tokens": 25,
            "estimated_cost_usd": 0.01,
            "usage_confidence": "client_reported",
            "cost_confidence": "estimated_from_tokens",
            "metadata": {
                "usage_source": "local_client_session_store",
                "client": "codex",
                "client_session_id": "legacy-no-capabilities",
                "cached_input_tokens": 50,
                "cache_creation_input_tokens": 10,
                "cache_read_input_tokens": 40,
                "started_at": time.time() - 3600,
                "updated_at": time.time() - 3600,
            },
        },
        trusted_usage_import=True,
    )
    client = _client(store_root)

    summary = client.get("/usage/summary?days=all").json()

    # A legacy row saved before the capability flags existed cannot honestly
    # claim its caches were "reported" or "not reported" — it stays unknown.
    assert summary["totals"]["cache_creation_tokens"] == 10
    assert summary["totals"]["cache_creation_reporting"] == "unknown"
    assert summary["totals"]["cache_read_tokens"] == 40
    assert summary["totals"]["cache_read_reporting"] == "unknown"


def test_usage_summary_redaction_sweep(tmp_path):
    store_root = tmp_path / "state"
    session_uuid = "ab12cd34-5678-4abc-9def-0123456789ab"
    _trusted_usage(
        store_root,
        session=session_uuid,
        client="claude-code",
        model="fable-5",
        project_dir="/Users/testuser/secret-project",
        started_at=time.time() - 3600,
    )
    client = _client(store_root)

    body = client.get("/usage/summary?days=all").text

    # /usage/summary is pure aggregate: no absolute paths, no usernames, no
    # session ids (full or short). Model names are fine (and required).
    assert "/Users/" not in body
    assert "testuser" not in body
    assert session_uuid not in body
    assert session_uuid[:8] not in body
    assert "fable-5" in body


# ---------------------------------------------------------------------------
# Stored-cost basis — by_model carries the cube's STORED cost with its
# confidence, never a pricing-catalog re-estimate of the same rows.
# ---------------------------------------------------------------------------


def test_by_model_reports_stored_cost_for_unpriced_model(tmp_path):
    # An unpriced (not-in-catalog) model whose row carries a client-reported
    # $5.00 stored cost — the cube must say $5.00, never "no estimate".
    store_root = tmp_path / "state"
    _trusted_usage(
        store_root,
        session="stored-cost-a",
        model="mystery-model",
        cost=5.0,
        cost_confidence="client_reported",
        started_at=time.time() - 3600,
    )
    client = _client(store_root)

    summary = client.get("/usage/summary").json()

    entry = summary["by_model"][0]
    assert entry["estimated_cost_usd"] == pytest.approx(5.0)
    assert entry["cost_confidence"] == "client_reported"
    assert entry["priced_rows"] == 1
    assert summary["totals"]["estimated_cost_usd"] == pytest.approx(5.0)
    assert summary["totals"]["cost_confidence"] == "client_reported"


def test_by_model_never_replaces_client_reported_cost_with_list_price(tmp_path):
    # A catalog-priced model with a client-reported $50.00 stored cost — the
    # cube must keep the stored figure with its confidence, never a silent
    # catalog re-estimate (1M input of gpt-5.5 would re-price at $12.50).
    store_root = tmp_path / "state"
    _trusted_usage(
        store_root,
        session="stored-cost-b",
        model="gpt-5.5",
        input_tokens=1_000_000,
        output_tokens=0,
        cost=50.0,
        cost_confidence="client_reported",
        started_at=time.time() - 3600,
    )
    client = _client(store_root)

    summary = client.get("/usage/summary").json()

    entry = summary["by_model"][0]
    assert entry["estimated_cost_usd"] == pytest.approx(50.0)
    assert entry["cost_confidence"] == "client_reported"
    assert summary["totals"]["estimated_cost_usd"] == pytest.approx(50.0)


def test_by_model_row_without_stored_cost_stays_honest_none(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(
        store_root, session="uncosted-row", model="mystery-model", cost=None, started_at=time.time() - 3600
    )
    client = _client(store_root)

    summary = client.get("/usage/summary").json()

    entry = summary["by_model"][0]
    assert entry["estimated_cost_usd"] is None
    assert entry["priced_rows"] == 0
    assert entry["unpriced_rows"] == 1


def test_partial_cost_sum_exposes_row_coverage_in_every_bucket(tmp_path):
    store_root = tmp_path / "state"
    # Anchor both rows to local midday so they always fall in one day bucket. A
    # bare time.time() offset straddles midnight when the suite runs just after
    # 00:00, and the per-period breakdown would then show two single-row days
    # (each fully priced/unpriced) instead of one partially-priced day.
    now = datetime.combine(date.today(), dtime(12, 0)).timestamp()
    _trusted_usage(
        store_root,
        session="priced-row",
        client="codex",
        model="partial-model",
        cost=5.0,
        started_at=now - 3600,
    )
    _trusted_usage(
        store_root,
        session="unpriced-row",
        client="codex",
        model="partial-model",
        cost=None,
        cost_confidence=None,
        started_at=now - 1800,
    )
    client = _client(store_root)

    summary = client.get("/usage/summary?days=all").json()

    # $5.00 is a partial sum, and every bucket says so via priced/unpriced row
    # coverage instead of presenting it as a whole-population figure.
    assert summary["totals"]["estimated_cost_usd"] == pytest.approx(5.0)
    assert summary["totals"]["priced_rows"] == 1
    assert summary["totals"]["unpriced_rows"] == 1
    assert summary["totals"]["cost_complete"] is False
    by_model = summary["by_model"][0]
    assert by_model["priced_rows"] == 1
    assert by_model["unpriced_rows"] == 1
    by_client = summary["by_client"][0]
    assert by_client["priced_rows"] == 1
    assert by_client["unpriced_rows"] == 1
    populated_periods = [entry for entry in summary["by_period"] if entry["rows"]]
    assert len(populated_periods) == 1
    assert populated_periods[0]["priced_rows"] == 1
    assert populated_periods[0]["unpriced_rows"] == 1


# ---------------------------------------------------------------------------
# ONE shared dominant-confidence rule (usage_cube.dominant_cost_confidence,
# surfaced as the bucket's cost_confidence_label) — weighted by summed cost
# (fallback row count), "mostly X" only when strictly dominant, ties name
# both labels and never break toward the higher-authority label.
# ---------------------------------------------------------------------------


def test_confidence_tie_labels_mixed_naming_both_never_mostly():
    records = [
        _cube_record(session="tie-a", day=TODAY, cost=0.05, cost_confidence="provider_billed"),
        _cube_record(session="tie-b", day=TODAY, cost=0.05, cost_confidence="estimated_from_tokens"),
    ]

    totals = _cube(records, days=30, granularity="daily")["totals"]

    assert "mostly" not in totals["cost_confidence_label"]
    assert totals["cost_confidence_label"] == (
        "mixed confidence (estimated_from_tokens + provider_billed)"
    )
    # The tie never resolves toward the higher-authority label.
    assert totals["cost_confidence"] == "estimated_from_tokens"
    assert totals["cost_confidence_mixed"] is True


def test_confidence_majority_by_cost_weight_labels_mostly():
    records = [
        _cube_record(session="w-a", day=TODAY, cost=0.99, cost_confidence="estimated_from_tokens"),
        _cube_record(session="w-b", day=TODAY, cost=0.01, cost_confidence="client_reported"),
    ]

    totals = _cube(records, days=30, granularity="daily")["totals"]

    assert totals["cost_confidence_label"] == "mixed confidence (mostly estimated_from_tokens)"


def test_confidence_weighting_uses_cost_not_row_counts():
    # 99 tiny estimated rows vs one $50 client-reported row: cost weight
    # dominates, so the label says mostly client_reported.
    records = [
        _cube_record(session=f"tiny-{index}", day=TODAY, cost=0.0001, cost_confidence="estimated_from_tokens")
        for index in range(99)
    ] + [_cube_record(session="big", day=TODAY, cost=50.0, cost_confidence="client_reported")]

    totals = _cube(records, days=30, granularity="daily")["totals"]

    assert totals["cost_confidence_label"] == "mixed confidence (mostly client_reported)"


def test_confidence_single_bucket_labels_plain():
    records = [_cube_record(session="solo", day=TODAY, cost=0.10, cost_confidence="estimated_from_tokens")]

    totals = _cube(records, days=30, granularity="daily")["totals"]

    assert totals["cost_confidence_label"] == "estimated_from_tokens confidence"


# ---------------------------------------------------------------------------
# One `today` per request — the cube never resolves its own clock on a
# request path, so a request served across local midnight cannot render two
# different row populations.
# ---------------------------------------------------------------------------


def test_usage_summary_resolves_today_once_and_passes_it_to_the_cube(tmp_path, monkeypatch):
    import agentacct.usage_cube as usage_cube_module

    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="today-sess", started_at=time.time() - 3600)
    client = _client(store_root)

    class _NoClock(date):
        @classmethod
        def today(cls):
            raise AssertionError("usage_cube must receive today= from the route, never resolve its own clock")

    monkeypatch.setattr(usage_cube_module, "date", _NoClock)

    assert client.get("/usage/summary").status_code == 200
