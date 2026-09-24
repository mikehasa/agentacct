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
    providers_in_records,
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


def _trusted_usage_event(
    *,
    session,
    client="codex",
    provider=None,
    model="gpt-5.5",
    input_tokens=100,
    output_tokens=25,
    cache_creation=0,
    cache_read=0,
    cost=0.01,
    cost_confidence="estimated_from_tokens",
    started_at=None,
    updated_at=None,
    project_dir=None,
    cache_creation_reported=True,
    cache_read_reported=True,
    session_kind="root",
    parent_session=None,
):
    """The trusted-import event body — one builder, so a row with distinct
    start/update times (the cross-day span cases) can never drift from the
    single-row helper's provenance shape."""

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
    if updated_at is not None:
        metadata["updated_at"] = updated_at
    if project_dir is not None:
        metadata["project_dir"] = project_dir
    return {
        "source": f"{client}-local-session-import",
        "event_type": "model_usage",
        "provider": provider or client,
        "model": model,
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": output_tokens,
        "estimated_cost_usd": cost,
        "usage_confidence": "client_reported",
        "cost_confidence": cost_confidence,
        "metadata": metadata,
    }


def _trusted_usage(
    store_root,
    *,
    session,
    client="codex",
    provider=None,
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
    return SentinelService(store_root).record_event(
        _trusted_usage_event(
            session=session,
            client=client,
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation=cache_creation,
            cache_read=cache_read,
            cost=cost,
            cost_confidence=cost_confidence,
            # One saved activity timestamp: both slots carry it, exactly as
            # before this helper gained an explicit ``updated_at``.
            started_at=started_at,
            updated_at=started_at,
            project_dir=project_dir,
            cache_creation_reported=cache_creation_reported,
            cache_read_reported=cache_read_reported,
            session_kind=session_kind,
            parent_session=parent_session,
        ),
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


def test_cube_provider_filter_keeps_only_that_provider_and_unknown_is_empty():
    records = [
        _cube_record(client="claude-code", provider="anthropic", model="fable-5", session="a", day=TODAY),
        _cube_record(client="kimi-code", provider="moonshot", model="kimi-k2", session="b", day=TODAY),
        _cube_record(client="codex", provider="openai", model="gpt-5.5", session="c", day=TODAY),
    ]

    moonshot = _cube(records, provider="moonshot", days=30, granularity="daily")

    assert moonshot["totals"]["rows"] == 1
    assert [entry["client"] for entry in moonshot["by_client"]] == ["kimi-code"]
    assert [entry["provider"] for entry in moonshot["by_model"]] == ["moonshot"]
    assert moonshot["by_period"][-1]["by_client"]["kimi-code"]["rows"] == 1

    # Same rule as the model filter: providers are data, so an unmatched one
    # is a truly empty result (no gap-filled zero wall), never a 422 or a guess.
    unknown = _cube(records, provider="never-seen", days=30, granularity="daily")
    assert unknown["totals"]["rows"] == 0
    assert unknown["by_client"] == []
    assert unknown["by_model"] == []
    assert unknown["by_period"] == []

    assert providers_in_records(records) == ["anthropic", "moonshot", "openai"]


def test_cube_explicit_range_is_closed_and_takes_precedence_over_days():
    records = [
        _cube_record(day=TODAY, session="today"),
        _cube_record(day=TODAY - timedelta(days=6), session="first-day"),  # the closed start
        _cube_record(day=TODAY - timedelta(days=7), session="day-before"),
        # Dated after today: outside the window, but an explicit interval is
        # the caller's own question and answers it honestly.
        _cube_record(day=TODAY + timedelta(days=2), session="after-today"),
    ]

    explicit = _cube(records, days=None, start=TODAY - timedelta(days=6), end=TODAY, granularity="daily")
    preset = _cube(records, days=7, granularity="daily")

    # start=today-6, end=today IS the 7-day preset, every section included.
    assert explicit == preset
    assert explicit["totals"]["rows"] == 2
    assert [entry["period"] for entry in explicit["by_period"]] == [
        (TODAY - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)
    ]

    after = _cube(records, days=None, start=TODAY + timedelta(days=2), end=TODAY + timedelta(days=2),
                  granularity="daily")
    assert [entry["period"] for entry in after["by_period"]] == [(TODAY + timedelta(days=2)).isoformat()]
    assert after["totals"]["rows"] == 1

    # The explicit interval replaces the days window instead of intersecting it.
    overriding = _cube(records, days=30, start=TODAY, end=TODAY, granularity="daily")
    assert overriding["totals"]["rows"] == 1
    assert [entry["period"] for entry in overriding["by_period"]] == [TODAY.isoformat()]

    # An open side keeps unknown-time rows excluded-but-counted (a bounded
    # range cannot honestly claim a row with no usable date) ...
    bad_timestamp = _cube_record(timestamp=1e300, session="bad-ts")
    bounded, unknown_time_rows = filter_usage_records(
        [*records, bad_timestamp],
        record_time=_usage_record_time,
        start=TODAY - timedelta(days=6),
        today=TODAY,
    )
    assert unknown_time_rows == 1
    assert bad_timestamp not in bounded
    # ... while an unbounded filter still keeps and counts it.
    unbounded, unbounded_unknown = filter_usage_records(
        [*records, bad_timestamp], record_time=_usage_record_time, days=None, today=TODAY
    )
    assert unbounded_unknown == 1
    assert bad_timestamp in unbounded


# ---------------------------------------------------------------------------
# GET /usage/summary — shape, validation, honesty of the intake
# ---------------------------------------------------------------------------


def test_usage_summary_shape_totals_and_periods(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    today = date.today()
    _trusted_usage(store_root, session="sum-a", client="codex", started_at=now - 3600, cache_read=500)
    _trusted_usage(store_root, session="sum-b", client="claude-code", model="fable-5", started_at=now - 86400)
    client = _client(store_root)

    payload = client.get("/usage/summary").json()
    if date.today() != today:
        pytest.skip("local midnight crossed while the request resolved its own today")

    assert set(payload) == {
        "schema_version",
        "filters_echo",
        "period_attribution",
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
        "provider": "all",
        "days": "30",
        "granularity": "daily",
        "granularity_requested": "auto",
        "range_mode": "days",
        "resolved_start": (today - timedelta(days=29)).isoformat(),
        "resolved_end": today.isoformat(),
        "model_matches_saved_rows": True,
        "provider_matches_saved_rows": True,
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


def test_usage_summary_provider_filter_narrows_every_section_and_echoes_unknown(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    _trusted_usage(store_root, session="prov-anthropic", client="claude-code", provider="anthropic",
                   model="fable-5", started_at=now - 3600, cost=0.10)
    _trusted_usage(store_root, session="prov-moonshot", client="kimi-code", provider="moonshot",
                   model="kimi-k2", started_at=now - 3600, cost=0.20)
    _trusted_usage(store_root, session="prov-openai", client="codex", provider="openai",
                   model="gpt-5.5", started_at=now - 3600, cost=0.40)
    client = _client(store_root)

    everything = client.get("/usage/summary").json()
    assert everything["filters_echo"]["provider"] == "all"
    assert everything["filters_echo"]["provider_matches_saved_rows"] is True
    assert everything["totals"]["rows"] == 3

    moonshot = client.get("/usage/summary?provider=moonshot").json()

    assert moonshot["filters_echo"]["provider"] == "moonshot"
    assert moonshot["filters_echo"]["provider_matches_saved_rows"] is True
    assert moonshot["totals"]["rows"] == 1
    assert moonshot["totals"]["fresh_tokens"] == 125
    assert moonshot["totals"]["estimated_cost_usd"] == pytest.approx(0.20)
    assert [entry["client"] for entry in moonshot["by_client"]] == ["kimi-code"]
    assert [(entry["client"], entry["provider"], entry["model"]) for entry in moonshot["by_model"]] == [
        ("kimi-code", "moonshot", "kimi-k2")
    ]
    # The per-period slices carry the same filter as the period totals.
    active = next(entry for entry in moonshot["by_period"] if entry["rows"])
    assert set(active["by_client"]) == {"kimi-code"}
    assert active["rows"] == 1

    # Provider is data too, not a whitelist: an unknown or merely unmatched
    # provider returns the EMPTY result with the echo saying so — no 422, no
    # fallback to a nearby provider.
    unknown = client.get("/usage/summary?provider=never-seen").json()
    assert unknown["filters_echo"]["provider"] == "never-seen"
    assert unknown["filters_echo"]["provider_matches_saved_rows"] is False
    assert unknown["totals"]["rows"] == 0
    assert unknown["by_client"] == []
    assert unknown["by_model"] == []
    assert unknown["by_period"] == []

    # A provider that IS saved but whose rows the client filter excludes stays
    # echo-true (the flag reports the saved-row vocabulary, not the join).
    joined = client.get("/usage/summary?client=codex&provider=moonshot").json()
    assert joined["filters_echo"]["provider_matches_saved_rows"] is True
    assert joined["totals"]["rows"] == 0
    assert joined["by_client"] == []


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


@pytest.mark.parametrize("granularity", ["daily", "weekly"])
def test_all_token_basis_counts_normalized_cache_buckets_once_everywhere(granularity):
    # Claude reports uncached input separately. Codex raw input is inclusive:
    # 1,000 input - 700 reads - 50 writes = 250 normalized fresh input.
    records = [
        _cube_record(client="claude-code", session="claude", day=TODAY,
                     input_tokens=100, output_tokens=20, cache_creation=30, cache_read=850),
        _cube_record(client="codex", session="codex", day=TODAY,
                     input_tokens=250, output_tokens=100, cache_creation=50, cache_read=700),
    ]
    cube = _cube(records, days=7, granularity=granularity)
    assert cube["totals"]["fresh_tokens"] == 470
    assert cube["totals"]["total_tokens_including_cached"] == 2100
    for dimension in ("by_client", "by_model", "by_period"):
        assert sum(bucket["fresh_tokens"] for bucket in cube[dimension]) == 470
        assert sum(bucket["total_tokens_including_cached"] for bucket in cube[dimension]) == 2100
    active = next(bucket for bucket in cube["by_period"] if bucket["total_tokens_including_cached"])
    assert active["by_client"]["claude-code"]["total_tokens_including_cached"] == 1000
    assert active["by_client"]["codex"]["total_tokens_including_cached"] == 1100
    assert active["by_client"]["codex"]["fresh_tokens"] == 350


# ---------------------------------------------------------------------------
# Range scoping — `GET /usage/summary?days=N` is the macOS Usage range
# picker's contract (7d/30d/90d): `by_client` and `by_model` must be the sum
# of exactly the rows whose ATTRIBUTED local date falls in the trailing N
# days, with no page-limit truncation of the underlying ledger and the cube's
# cost honesty intact in every bucket.
#
# Range tests anchor rows to the REAL clock: the route resolves its own
# `date.today()`, so the module's fixed TODAY would land every row outside
# the window. Offsets are local midnights, which no DST transition moves.
# ---------------------------------------------------------------------------


def _range_day(offset, today):
    """Local midday ``offset`` days before ``today`` — the range-window anchor."""

    day = today - timedelta(days=offset)
    return datetime.combine(day, dtime(12, 0)).timestamp()


def _seed_usage_rows(store_root, rows, *, today):
    """Record a row table through ONE service for the same trusted-import
    intake `_trusted_usage` uses (one service keeps the 250-row volume
    fixture fast instead of re-opening the store per row).

    Range tests anchor their rows to ``today``, which the route resolves for
    itself; a run that crosses local midnight mid-seed would compare the
    fixture against a window that has since moved, so that case skips.
    """

    service = SentinelService(store_root)
    for row in rows:
        service.record_event(
            _trusted_usage_event(
                session=row["session"],
                client=row["client"],
                model=row["model"],
                input_tokens=row["input"],
                output_tokens=row["output"],
                cache_creation=row.get("cache_creation", 0),
                cache_read=row.get("cache_read", 0),
                cost=row["cost"],
                cost_confidence=row["confidence"],
                started_at=_range_day(row["started_offset"], today),
                updated_at=_range_day(row["updated_offset"], today),
                session_kind=row.get("kind", "root"),
                parent_session=row.get("parent"),
            ),
            trusted_usage_import=True,
        )
    if date.today() != today:
        pytest.skip("local midnight crossed while seeding the range fixture")


def _attributed_offset(row):
    """The day offset a row is attributed to — the one rule the range filter
    applies: Hermes rides its session start, every other client its latest
    saved activity (`period_attribution.description`)."""

    return row["started_offset"] if row["client"] == "hermes" else row["updated_offset"]


def _expected_range_bucket():
    return {
        "rows": 0,
        "held_rows": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens_including_cached": 0,
        "priced_rows": 0,
        "unpriced_rows": 0,
        "cost": 0.0,
        "session_keys": set(),
    }


def _add_expected_row(bucket, row):
    """One row into one independently-summed bucket."""

    bucket["rows"] += 1
    bucket["session_keys"].add((row["client"], row["session"]))
    if not row["additive"]:
        bucket["held_rows"] += 1
        return
    bucket["input_tokens"] += row["input"]
    bucket["output_tokens"] += row["output"]
    bucket["cache_creation_tokens"] += row["cache_creation"]
    bucket["cache_read_tokens"] += row["cache_read"]
    bucket["total_tokens_including_cached"] += row["input"] + row["output"] + row["cached"]
    if row["cost"] is None:
        bucket["unpriced_rows"] += 1
        return
    bucket["priced_rows"] += 1
    bucket["cost"] += row["cost"]


def _expected_range_cube(rows, *, days, today, granularity="daily"):
    """Plain-arithmetic recomputation of the range cube — no cube code runs.

    Returns ``(by_client, by_model, by_period)``; ``by_period`` is keyed by the
    period label (local day, or its ISO week start for weekly). The documented
    window rule — ``days=N`` keeps the local days ``today-N+1 .. today`` — is
    applied to the row table's own numbers, so the endpoint cannot pass these
    checks by reusing the aggregation under test.
    """

    by_client: dict[str, dict] = {}
    by_model: dict[tuple[str, str, str], dict] = {}
    by_period: dict[str, dict[str, dict]] = {}
    for row in rows:
        offset = _attributed_offset(row)
        if offset > days - 1:
            continue
        day = today - timedelta(days=offset)
        period_key = (week_start(day) if granularity == "weekly" else day).isoformat()
        model_key = (row["client"], row["client"], row["model"])
        period = by_period.setdefault(period_key, {"by_client": {}, "by_model": {}})
        for bucket in (
            by_client.setdefault(row["client"], _expected_range_bucket()),
            by_model.setdefault(model_key, _expected_range_bucket()),
            period["by_client"].setdefault(row["client"], _expected_range_bucket()),
            period["by_model"].setdefault(model_key, _expected_range_bucket()),
        ):
            _add_expected_row(bucket, row)
    return by_client, by_model, by_period


def _assert_bucket_matches_expected(bucket, expected):
    """Compare one response bucket field-by-field with the independent sum.

    Cost keeps the cube's honesty contract: a bucket whose rows all carry a
    priced estimate reports that sum (a 0.0 from a priced row is a reported
    zero, not an unknown), a bucket with no priced row reports None and no
    confidence, and a bucket holding a quarantined row withholds its total
    while ``known_additive_cost_usd`` keeps the priced subset visible.
    """

    assert bucket["rows"] == expected["rows"]
    assert bucket["sessions"] == len(expected["session_keys"])
    assert bucket["input_tokens"] == expected["input_tokens"]
    assert bucket["output_tokens"] == expected["output_tokens"]
    assert bucket["fresh_tokens"] == expected["input_tokens"] + expected["output_tokens"]
    assert bucket["cache_creation_tokens"] == expected["cache_creation_tokens"]
    assert bucket["cache_read_tokens"] == expected["cache_read_tokens"]
    assert bucket["total_tokens_including_cached"] == expected["total_tokens_including_cached"]
    assert bucket["priced_rows"] == expected["priced_rows"]
    assert bucket["unpriced_rows"] == expected["unpriced_rows"]
    if not expected["priced_rows"]:
        assert bucket["estimated_cost_usd"] is None
        assert bucket["known_additive_cost_usd"] is None
    elif expected["held_rows"]:
        assert bucket["estimated_cost_usd"] is None
        assert bucket["known_additive_cost_usd"] == pytest.approx(expected["cost"])
    else:
        assert bucket["estimated_cost_usd"] == pytest.approx(expected["cost"])
        assert bucket["known_additive_cost_usd"] == pytest.approx(expected["cost"])
    assert bucket["cost_complete"] is bool(
        expected["rows"] and not expected["held_rows"] and not expected["unpriced_rows"]
    )


def _range_row(
    client,
    model,
    session,
    offset,
    *,
    cost,
    confidence,
    input_tokens=100,
    output_tokens=25,
    cache_read=0,
    cache_creation=0,
    started_offset=None,
    updated_offset=None,
    additive=True,
    kind="root",
    parent=None,
):
    """One range-fixture row. ``offset`` is the saved activity day;

    ``cached`` mirrors the ``cached_input_tokens`` the importer stores
    (creation + read), ``additive=False`` marks a held row (non-additive rows
    stay countable evidence but never enter token/cost sums).
    """

    return {
        "client": client,
        "model": model,
        "session": session,
        "input": input_tokens,
        "output": output_tokens,
        "cache_read": cache_read,
        "cache_creation": cache_creation,
        "cached": cache_read + cache_creation,
        "cost": cost,
        "confidence": confidence,
        "started_offset": offset if started_offset is None else started_offset,
        "updated_offset": offset if updated_offset is None else updated_offset,
        "additive": additive,
        "kind": kind,
        "parent": parent,
    }


def _range_rows():
    """The multi-day range fixture: two clients, two models under one client,
    a cross-day span, a Hermes session-start row and an unpriced row.

    Offsets: 0 = today, 6 = the 7d window's first day, 8 = inside 30d only,
    45 = inside 90d only.
    """

    return [
        _range_row("codex", "gpt-5.5", "range-codex-today", 0, input_tokens=100, output_tokens=25,
                   cache_read=500, cost=0.10, confidence="estimated_from_tokens"),
        _range_row("codex", "gpt-5.5", "range-codex-day3", 3, input_tokens=200, output_tokens=50,
                   cost=0.20, confidence="estimated_from_tokens"),
        _range_row("claude-code", "fable-5", "range-fable-day6", 6, input_tokens=300, output_tokens=75,
                   cache_creation=40, cost=0.30, confidence="client_reported"),
        # Offset 8: the row a 7d window must exclude and a 30d window include.
        _range_row("claude-code", "fable-5", "range-fable-day8", 8, input_tokens=400, output_tokens=100,
                   cost=0.40, confidence="client_reported"),
        # Offset 45: only the 90d window reaches it.
        _range_row("codex", "gpt-5.5", "range-codex-day45", 45, input_tokens=1000, output_tokens=0,
                   cost=1.00, confidence="estimated_from_tokens"),
        _range_row("claude-code", "mystery-model", "range-unpriced-day2", 2, input_tokens=50,
                   output_tokens=10, cost=None, confidence=None),
        # Cross-day spans: same start/activity pair, attributed by each
        # client's own rule (codex -> latest activity, Hermes -> session start).
        _range_row("codex", "gpt-5.5", "range-codex-span", 0, input_tokens=10, output_tokens=5,
                   cost=0.01, confidence="estimated_from_tokens", started_offset=6, updated_offset=0),
        _range_row("hermes", "gpt-5.4-mini", "range-hermes-span", 3, input_tokens=7, output_tokens=3,
                   cost=0.02, confidence="estimated_from_tokens", started_offset=3, updated_offset=0),
        # Hermes session active TODAY but started 10 days ago: the 7d window
        # excludes it (session-start attribution), the 30d window includes it.
        _range_row("hermes", "gpt-5.4-mini", "range-hermes-stale-start", 10, input_tokens=70,
                   output_tokens=30, cost=0.07, confidence="estimated_from_tokens",
                   started_offset=10, updated_offset=0),
        # One session with rows on two different days: the per-period session
        # counts split it, the range's distinct-session count does not.
        _range_row("codex", "gpt-5.5", "range-codex-multiday", 1, input_tokens=11, output_tokens=1,
                   cost=0.01, confidence="estimated_from_tokens"),
        _range_row("codex", "gpt-5.5", "range-codex-multiday", 5, input_tokens=12, output_tokens=2,
                   cost=0.01, confidence="estimated_from_tokens"),
    ]


def test_usage_summary_range_client_and_model_totals_are_the_in_range_row_sums(tmp_path):
    store_root = tmp_path / "state"
    today = date.today()
    rows = _range_rows()
    _seed_usage_rows(store_root, rows, today=today)
    client = _client(store_root)

    for days in (7, 30, 90):
        payload = client.get(f"/usage/summary?days={days}&granularity=daily").json()
        expected_clients, expected_models, _ = _expected_range_cube(rows, days=days, today=today)

        by_client = {bucket["client"]: bucket for bucket in payload["by_client"]}
        assert set(by_client) == set(expected_clients)
        # Order is part of the contract (the app renders the list as delivered):
        # biggest total token volume first, client name breaking ties.
        assert [bucket["client"] for bucket in payload["by_client"]] == sorted(
            by_client,
            key=lambda name: (-by_client[name]["total_tokens_including_cached"], name),
        )
        for name, expected in expected_clients.items():
            _assert_bucket_matches_expected(by_client[name], expected)

        by_model = {
            (bucket["client"], bucket["provider"], bucket["model"]): bucket
            for bucket in payload["by_model"]
        }
        assert set(by_model) == set(expected_models)
        assert [(b["client"], b["provider"], b["model"]) for b in payload["by_model"]] == sorted(
            by_model, key=lambda key: (-by_model[key]["total_tokens_including_cached"], key)
        )
        for key, expected in expected_models.items():
            _assert_bucket_matches_expected(by_model[key], expected)

        # The range totals are one more bucket over the same population.
        assert payload["totals"]["rows"] == sum(bucket["rows"] for bucket in payload["by_client"])
        assert payload["totals"]["sessions"] == sum(
            len(expected["session_keys"]) for expected in expected_clients.values()
        )
        assert payload["totals"]["total_tokens_including_cached"] == sum(
            bucket["total_tokens_including_cached"] for bucket in payload["by_client"]
        )

    seven = client.get("/usage/summary?days=7&granularity=daily").json()
    thirty = client.get("/usage/summary?days=30&granularity=daily").json()
    ninety = client.get("/usage/summary?days=90&granularity=daily").json()

    # The window is applied to SAVED-ROW attribution dates, and the payload
    # says so: the recomputation above deliberately uses those dates rather
    # than any reconstructed per-call history (`exact_daily_usage` false), so
    # a session that straddles days is counted whole on one side.
    assert seven["period_attribution"] == {
        "basis": "saved_session_row",
        "timezone": "daemon_local",
        "exact_daily_usage": False,
        "label": "Session totals by activity date",
        "description": (
            "Session totals are assigned to their saved activity date "
            "(Hermes: session start; other clients: latest saved activity). "
            "Multi-day sessions are not split into exact daily usage."
        ),
    }

    def _client_tokens(payload, name):
        return next(bucket["total_tokens_including_cached"] for bucket in payload["by_client"]
                    if bucket["client"] == name)

    def _model_rows(payload, name, model):
        return next(bucket["rows"] for bucket in payload["by_model"]
                    if bucket["client"] == name and bucket["model"] == model)

    # 7d: codex 625 + 250 + 15 + 12 + 14, claude-code 415 + 60 (the day-8
    # fable-5 row and the day-45 codex row are outside the window).
    assert _client_tokens(seven, "codex") == 916
    assert _client_tokens(seven, "claude-code") == 475
    assert _model_rows(seven, "claude-code", "fable-5") == 1
    # 30d: the day-8 fable-5 row joins, the day-45 codex row stays out.
    assert _client_tokens(thirty, "codex") == 916
    assert _client_tokens(thirty, "claude-code") == 975
    assert _model_rows(thirty, "claude-code", "fable-5") == 2
    # 90d: the day-45 codex row finally enters.
    assert _client_tokens(ninety, "codex") == 1916
    assert _client_tokens(ninety, "claude-code") == 975
    assert _model_rows(ninety, "codex", "gpt-5.5") == 6


def test_usage_summary_range_attributes_cross_day_sessions_by_the_disclosed_client_rule(tmp_path):
    store_root = tmp_path / "state"
    today = date.today()
    _seed_usage_rows(store_root, _range_rows(), today=today)
    client = _client(store_root)

    seven = client.get("/usage/summary?days=7&granularity=daily").json()
    thirty = client.get("/usage/summary?days=30&granularity=daily").json()

    # The rule the range filter applies is disclosed in-band, and this fixture
    # is only meaningful while it keeps naming both client behaviours.
    attribution = seven["period_attribution"]
    assert attribution["basis"] == "saved_session_row"
    assert attribution["timezone"] == "daemon_local"
    assert attribution["exact_daily_usage"] is False
    assert "Hermes: session start" in attribution["description"]
    assert "latest saved activity" in attribution["description"]

    periods = {entry["period"]: entry for entry in seven["by_period"]}
    today_key = today.isoformat()
    # The cross-day codex span rides its LATEST ACTIVITY (today)...
    assert periods[today_key]["by_client"]["codex"]["rows"] == 2
    assert "hermes" not in periods[today_key]["by_client"]
    # ...while the Hermes span rides its SESSION START (3 days ago), even
    # though its latest activity is today.
    start_key = (today - timedelta(days=3)).isoformat()
    assert periods[start_key]["by_client"]["hermes"]["input_tokens"] == 7
    # The codex span's own start (6 days ago) is NOT where its row lives: the
    # only lane on that day is the genuine day-6 claude-code row.
    assert set(periods[(today - timedelta(days=6)).isoformat()]["by_client"]) == {"claude-code"}

    # The visible consequence for a range view: a Hermes session that was
    # active today but started 10 days ago is NOT in the 7d window (its
    # tokens cannot be split into fictional daily usage), and it appears once
    # the window reaches its session start.
    hermes_seven = next(bucket for bucket in seven["by_client"] if bucket["client"] == "hermes")
    hermes_thirty = next(bucket for bucket in thirty["by_client"] if bucket["client"] == "hermes")
    assert hermes_seven["rows"] == 1 and hermes_seven["input_tokens"] == 7
    assert hermes_thirty["rows"] == 2 and hermes_thirty["input_tokens"] == 77

    # Attribution never loses or duplicates a row: the per-period slices of a
    # client always add back up to that client's range totals.
    for field in ("rows", "input_tokens", "output_tokens", "fresh_tokens",
                  "total_tokens_including_cached"):
        for name in ("codex", "hermes"):
            assert sum(entry["by_client"].get(name, {}).get(field, 0)
                       for entry in seven["by_period"]) == next(
                bucket[field] for bucket in seven["by_client"] if bucket["client"] == name
            ), (field, name)


def test_usage_summary_range_cost_columns_appear_only_where_rows_are_priced(tmp_path):
    store_root = tmp_path / "state"
    today = date.today()
    rows = [
        _range_row("codex", "gpt-5.5", "cost-estimated", 0, cost=0.10,
                   confidence="estimated_from_tokens"),
        _range_row("codex", "gpt-5.5", "cost-client-reported", 1, cost=0.20,
                   confidence="client_reported"),
        _range_row("claude-code", "fable-5", "cost-unpriced", 2, cost=None, confidence=None),
        # A stored cost with no confidence label: priced, and never upgraded.
        _range_row("claude-code", "unlabeled-model", "cost-unlabeled", 3, cost=0.30, confidence=None),
        # A reported zero is a measurement, not an unknown.
        _range_row("hermes", "gpt-5.4-mini", "cost-reported-zero", 4, cost=0.0,
                   confidence="client_reported"),
    ]
    _seed_usage_rows(store_root, rows, today=today)
    client = _client(store_root)

    payload = client.get("/usage/summary?days=7&granularity=daily").json()
    by_client = {bucket["client"]: bucket for bucket in payload["by_client"]}
    by_model = {bucket["model"]: bucket for bucket in payload["by_model"]}

    unpriced = by_model["fable-5"]
    assert unpriced["estimated_cost_usd"] is None
    assert unpriced["known_additive_cost_usd"] is None
    assert unpriced["priced_rows"] == 0
    assert unpriced["unpriced_rows"] == 1
    assert unpriced["cost_confidence"] is None
    assert unpriced["cost_confidence_label"] is None
    assert unpriced["cost_confidence_mixed"] is False
    assert unpriced["cost_complete"] is False

    # The unpriced row never removes the priced lane's number, and the client
    # bucket says its $0.30 covers one of two rows.
    unlabeled = by_model["unlabeled-model"]
    assert unlabeled["estimated_cost_usd"] == pytest.approx(0.30)
    assert unlabeled["cost_confidence"] == "unknown"
    assert unlabeled["cost_confidence_label"] == "unknown confidence"
    assert unlabeled["cost_complete"] is True
    claude_code = by_client["claude-code"]
    assert claude_code["estimated_cost_usd"] == pytest.approx(0.30)
    assert claude_code["known_additive_cost_usd"] == pytest.approx(0.30)
    assert claude_code["priced_rows"] == 1
    assert claude_code["unpriced_rows"] == 1
    assert claude_code["cost_complete"] is False

    # $0.00 from a priced row stays a number; cost weight (not row count)
    # picks the dominant confidence, and a split names both labels.
    reported_zero = by_client["hermes"]
    assert reported_zero["estimated_cost_usd"] == 0.0
    assert reported_zero["cost_confidence"] == "client_reported"
    assert reported_zero["cost_complete"] is True
    codex = by_client["codex"]
    assert codex["estimated_cost_usd"] == pytest.approx(0.30)
    assert codex["cost_confidence"] == "client_reported"
    assert codex["cost_confidence_mixed"] is True
    assert codex["cost_confidence_label"] == "mixed confidence (mostly client_reported)"

    # Rule for every bucket of this range: a cost number appears exactly when
    # a priced row backs it, and a bucket with no priced row reports None
    # instead of a fabricated 0.00.
    buckets = [payload["totals"], *payload["by_client"], *payload["by_model"],
               *[entry for entry in payload["by_period"] if entry["rows"]],
               *[lane for entry in payload["by_period"] for lane in entry["by_client"].values()],
               *[slice_bucket for entry in payload["by_period"] for slice_bucket in entry["by_model"]]]
    for bucket in buckets:
        assert (bucket["estimated_cost_usd"] is None) is (bucket["priced_rows"] == 0)
        if bucket["priced_rows"] == 0:
            assert bucket["cost_confidence"] is None
            assert bucket["cost_complete"] is False
        else:
            assert bucket["cost_confidence"] is not None

    assert payload["totals"]["estimated_cost_usd"] == pytest.approx(0.60)
    assert payload["totals"]["cost_complete"] is False
    assert payload["totals"]["unpriced_rows"] == 1


@pytest.mark.parametrize(("days", "granularity"), [("7", "daily"), ("30", "daily"), ("90", "weekly")])
def test_usage_summary_range_period_slices_add_up_to_the_range_totals(tmp_path, days, granularity):
    store_root = tmp_path / "state"
    today = date.today()
    rows = _range_rows()
    _seed_usage_rows(store_root, rows, today=today)
    client = _client(store_root)

    payload = client.get(f"/usage/summary?days={days}&granularity={granularity}").json()
    expected_clients, expected_models, expected_periods = _expected_range_cube(
        rows, days=int(days), today=today, granularity=granularity
    )
    periods = payload["by_period"]
    lanes = {bucket["client"]: bucket for bucket in payload["by_client"]}
    model_lanes = {(b["client"], b["provider"], b["model"]): b for b in payload["by_model"]}

    # Every period in the window is present (gaps included) and nothing
    # outside it leaks in; the populated ones carry exactly the rows whose
    # attributed day falls there.
    populated = {entry["period"]: entry for entry in periods if entry["rows"]}
    assert set(populated) == set(expected_periods)
    if granularity == "daily":
        # The whole window is enumerated, gap days included.
        assert [entry["period"] for entry in periods] == [
            (today - timedelta(days=offset)).isoformat() for offset in range(int(days) - 1, -1, -1)
        ]

    additive_fields = (
        "rows", "input_tokens", "output_tokens", "fresh_tokens", "cache_creation_tokens",
        "cache_read_tokens", "total_tokens_including_cached", "priced_rows", "unpriced_rows",
    )
    for field in additive_fields:
        assert sum(entry[field] for entry in periods) == payload["totals"][field], field
        for name, lane in lanes.items():
            assert sum(entry["by_client"].get(name, {}).get(field, 0)
                       for entry in periods) == lane[field], (field, name)
        for key, lane in model_lanes.items():
            assert sum(
                slice_bucket[field]
                for entry in periods
                for slice_bucket in entry["by_model"]
                if (slice_bucket["client"], slice_bucket["provider"], slice_bucket["model"]) == key
            ) == lane[field], (field, key)

    # Every row here is priced and additive except the unpriced one, and a
    # bucket whose only row is unpriced reports None (never $0.00) — so the
    # priced-only view is the one that must add up across grains.
    priced_period_cost = sum(entry["estimated_cost_usd"] for entry in periods if entry["priced_rows"])
    priced_lane_cost = sum(lane["estimated_cost_usd"] for lane in lanes.values() if lane["priced_rows"])
    priced_model_cost = sum(
        lane["estimated_cost_usd"] for lane in model_lanes.values() if lane["priced_rows"]
    )
    assert priced_period_cost == pytest.approx(payload["totals"]["estimated_cost_usd"])
    assert priced_lane_cost == pytest.approx(payload["totals"]["estimated_cost_usd"])
    assert priced_model_cost == pytest.approx(payload["totals"]["estimated_cost_usd"])
    unpriced_only = [entry["period"] for entry in periods if entry["rows"] and not entry["priced_rows"]]
    if granularity == "daily":
        # Only the unpriced row's own day lacks a priced row entirely.
        assert unpriced_only == [(today - timedelta(days=2)).isoformat()]
    # A populated period whose rows are all unpriced reports None, never $0.00.
    for entry in periods:
        assert (entry["estimated_cost_usd"] is None) is (entry["priced_rows"] == 0), entry["period"]
    for entry in periods:
        if not entry["rows"]:
            assert entry["estimated_cost_usd"] is None
            assert entry["by_client"] == {} and entry["by_model"] == []
            continue
        expected = expected_periods[entry["period"]]
        for name, expected_lane in expected["by_client"].items():
            _assert_bucket_matches_expected(entry["by_client"][name], expected_lane)
        got_slices = {(b["client"], b["provider"], b["model"]): b for b in entry["by_model"]}
        assert set(got_slices) == set(expected["by_model"])
        for key, expected_lane in expected["by_model"].items():
            _assert_bucket_matches_expected(got_slices[key], expected_lane)

    # Sessions are the one NON-additive counter: each period counts its own
    # distinct sessions, so a session with rows on two days appears twice —
    # which is exactly why period_attribution flags exact_daily_usage false
    # and why the range total is the number to quote, never the column sum.
    assert sum(entry["sessions"] for entry in periods) > payload["totals"]["sessions"]
    assert payload["totals"]["sessions"] == sum(
        len(expected["session_keys"]) for expected in expected_clients.values()
    )
    assert {name for entry in periods for name in entry["by_client"]} == set(lanes)


def test_usage_summary_range_client_totals_are_not_a_page_of_events(tmp_path):
    store_root = tmp_path / "state"
    today = date.today()
    volume_rows = [
        _range_row("codex", "gpt-5.5", f"vol-{index}", 0, input_tokens=10, output_tokens=2,
                   cache_read=3, cost=0.001, confidence="estimated_from_tokens")
        for index in range(250)
    ]
    rows = [
        *volume_rows,
        _range_row("claude-code", "fable-5", "vol-claude", 0, input_tokens=100, output_tokens=25,
                   cost=0.01, confidence="client_reported"),
        # Two rows outside the 7d window: they must not leak into it.
        _range_row("codex", "gpt-5.5", "vol-old-a", 20, input_tokens=5000, output_tokens=900,
                   cost=5.0, confidence="estimated_from_tokens"),
        _range_row("codex", "gpt-5.5", "vol-old-b", 25, input_tokens=5000, output_tokens=900,
                   cost=5.0, confidence="estimated_from_tokens"),
    ]
    _seed_usage_rows(store_root, rows, today=today)
    client = _client(store_root)

    seven = client.get("/usage/summary?days=7&granularity=daily").json()

    # 250 codex rows inside the window (the two day-20/25 rows are not) and one
    # claude-code row. /events cannot return more than 200 rows per page, so a
    # client bucket of 250 rows proves the aggregate is a full-population sum,
    # not a truncated page or a scan limit.
    assert len(client.get("/events?limit=200").json()["events"]) == 200
    by_client = {bucket["client"]: bucket for bucket in seven["by_client"]}
    assert set(by_client) == {"codex", "claude-code"}
    assert by_client["codex"]["rows"] == 250
    assert by_client["codex"]["fresh_tokens"] == 250 * 12
    assert by_client["codex"]["cache_read_tokens"] == 250 * 3
    assert by_client["codex"]["total_tokens_including_cached"] == 250 * 15
    assert by_client["codex"]["estimated_cost_usd"] == pytest.approx(0.25)
    assert by_client["claude-code"]["rows"] == 1
    assert by_client["claude-code"]["fresh_tokens"] == 125
    assert seven["totals"]["rows"] == 251
    assert seven["totals"]["fresh_tokens"] == 250 * 12 + 125
    assert seven["totals"]["sessions"] == 251

    # The two older rows join only once the window reaches them, and the
    # out-of-range tokens never appear in the 7d picture.
    thirty = client.get("/usage/summary?days=30&granularity=daily").json()
    thirty_codex = next(bucket for bucket in thirty["by_client"] if bucket["client"] == "codex")
    assert thirty_codex["rows"] == 252
    assert thirty_codex["fresh_tokens"] == 250 * 12 + 2 * 5900


def test_usage_summary_range_keeps_held_rows_counted_but_never_summed(tmp_path):
    store_root = tmp_path / "state"
    today = date.today()
    rows = [
        # A cumulative Codex descendant is held from every token/cost subtotal
        # even when it lands inside the selected range.
        _range_row("codex", "gpt-5.5", "held-child", 1, input_tokens=5_000_000_000,
                   output_tokens=1_000_000_000, cache_read=70_000_000_000, cost=500.0,
                   confidence="estimated_from_tokens", additive=False, kind="child",
                   parent="missing-parent"),
        _range_row("codex", "gpt-5.5", "held-neighbour", 1, input_tokens=100, output_tokens=25,
                   cost=0.10, confidence="estimated_from_tokens"),
        _range_row("claude-code", "fable-5", "held-clean", 2, input_tokens=300, output_tokens=75,
                   cost=0.30, confidence="client_reported"),
    ]
    _seed_usage_rows(store_root, rows, today=today)
    client = _client(store_root)

    for days in ("7", "30"):
        payload = client.get(f"/usage/summary?days={days}&granularity=daily").json()
        expected_clients, expected_models, expected_periods = _expected_range_cube(
            rows, days=int(days), today=today
        )
        by_client = {bucket["client"]: bucket for bucket in payload["by_client"]}
        by_model = {(b["client"], b["provider"], b["model"]): b for b in payload["by_model"]}

        codex = by_client["codex"]
        assert codex["rows"] == 2
        assert codex["additive_rows"] == 1
        assert codex["excluded_non_additive_rows"] == 1
        # The held row is still a session (it is evidence), its tokens are not.
        assert codex["sessions"] == 2
        assert codex["fresh_tokens"] == 125
        assert codex["total_tokens_including_cached"] == 125
        assert codex["estimated_cost_usd"] is None
        assert codex["known_additive_cost_usd"] == pytest.approx(0.10)
        assert codex["cost_complete"] is False
        assert codex["usage_availability"] == "partial"
        # The $500 and the multi-billion-token counters reach no bucket at any
        # grain.
        assert by_client["claude-code"]["estimated_cost_usd"] == pytest.approx(0.30)
        assert payload["totals"]["estimated_cost_usd"] is None
        assert payload["totals"]["known_additive_cost_usd"] == pytest.approx(0.40)
        assert payload["totals"]["excluded_non_additive_rows"] == 1
        assert payload["usage_exclusions"] == {
            "non_additive_rows": 1,
            "unknown_time_rows": 0,
            "reason": "legacy_codex_descendant_cumulative_unproven",
            "raw_evidence_preserved": True,
        }
        for bucket in (*by_client.values(), *by_model.values(), payload["totals"]):
            assert bucket["total_tokens_including_cached"] < 1_000_000

        for name, expected in expected_clients.items():
            _assert_bucket_matches_expected(by_client[name], expected)
        for key, expected in expected_models.items():
            _assert_bucket_matches_expected(by_model[key], expected)
        assert {entry["period"] for entry in payload["by_period"] if entry["rows"]} == set(expected_periods)


def test_usage_summary_range_drops_future_dated_rows_from_every_bounded_window(tmp_path):
    """A row dated after today is outside every bounded window.

    The exclusion is deliberate (``usage_snapshot``: future / absurd
    timestamps are excluded exactly as the dashboard's summary does, and a
    bounded range cannot honestly include them). Note for the range view: it
    is also undisclosed at row level — ``unknown_time_rows`` counts only rows
    that FAIL the bad-timestamp guard, and ``history_outside_range``
    deliberately never calls a future-dated client "preserved history" — so
    the only place such a row shows up is the all-time cube. This test pins
    the scoping (no leakage into any bounded bucket) rather than a disclosure
    the endpoint does not make.
    """

    store_root = tmp_path / "state"
    today = date.today()
    rows = [
        _range_row("codex", "gpt-5.5", "future-row", -3, input_tokens=999, output_tokens=99,
                   cost=9.0, confidence="estimated_from_tokens"),
        _range_row("codex", "gpt-5.5", "dated-today", 0, input_tokens=100, output_tokens=25,
                   cost=0.10, confidence="estimated_from_tokens"),
    ]
    _seed_usage_rows(store_root, rows, today=today)
    client = _client(store_root)

    for days in ("7", "30", "90"):
        payload = client.get(f"/usage/summary?days={days}&granularity=daily").json()
        codex = next(bucket for bucket in payload["by_client"] if bucket["client"] == "codex")
        assert codex["rows"] == 1
        assert codex["fresh_tokens"] == 125
        assert codex["estimated_cost_usd"] == pytest.approx(0.10)
        assert payload["totals"]["rows"] == 1
        assert payload["totals"]["unknown_time_rows"] == 0
        assert payload["range_context"]["history_outside_range"] == []
        assert (today + timedelta(days=3)).isoformat() not in {
            entry["period"] for entry in payload["by_period"]
        }

    # The row is stored, not deleted: all-time shows it on its own date.
    all_time = client.get("/usage/summary?days=all&granularity=daily").json()
    future_period = next(
        entry for entry in all_time["by_period"]
        if entry["period"] == (today + timedelta(days=3)).isoformat()
    )
    assert future_period["fresh_tokens"] == 1098
    assert all_time["totals"]["rows"] == 2


def test_usage_summary_range_unknown_time_row_is_disclosed_by_totals_and_exclusions(tmp_path):
    """Dropped rows, one count: both disclosures agree on the same lane set.

    A row whose timestamp fails the bad-timestamp guard cannot join a bounded
    range: it is dropped from every ``by_client`` / ``by_model`` /
    ``by_period`` bucket and counted by ``totals.unknown_time_rows``
    (docs/reference.md: "unknown timestamps ... are counted separately in
    bounded ranges"). That holds for both lanes — the fixture carries one
    additive row and one held (non-additive) row with the same unusable stamp.

    ``usage_exclusions`` is the object a JSON consumer reads to ask "what did
    this range leave out?", so its ``unknown_time_rows`` carries that same
    scope — every in-range row dropped for an unusable timestamp, additive and
    held alike — and is by construction the cube's own count, so a consumer can
    never see a dropped row that only one of the two objects admits to. The
    field overlaps ``totals`` on purpose; ``non_additive_rows`` stays the
    held-lane-only counter, and an undated held row is disclosed by the
    unknown-time counter rather than by that range-scoped held counter.
    """

    store_root = tmp_path / "state"
    today = date.today()
    _seed_usage_rows(
        store_root,
        [_range_row("codex", "gpt-5.5", "undated-ok", 0, input_tokens=100, output_tokens=25,
                    cost=0.10, confidence="estimated_from_tokens")],
        today=today,
    )
    SentinelService(store_root).record_event(
        _trusted_usage_event(
            session="undated-row",
            client="codex",
            model="gpt-5.5",
            input_tokens=7,
            output_tokens=3,
            cost=0.01,
            cost_confidence="estimated_from_tokens",
            started_at=1e300,
            updated_at=1e300,
        ),
        trusted_usage_import=True,
    )
    # The HELD lane carries the same stamp: this row is non-additive (an
    # unproven Codex cumulative descendant) AND undated, so neither counter may
    # lose it.
    SentinelService(store_root).record_event(
        _trusted_usage_event(
            session="undated-held-row",
            client="codex",
            model="gpt-5.5",
            input_tokens=9,
            output_tokens=1,
            cost=0.02,
            cost_confidence="estimated_from_tokens",
            started_at=1e300,
            updated_at=1e300,
            session_kind="child",
            parent_session="missing-parent",
        ),
        trusted_usage_import=True,
    )
    client = _client(store_root)

    payload = client.get("/usage/summary?days=7&granularity=daily").json()

    # Documented behaviour: excluded from every bucket, counted in totals.
    assert payload["totals"]["unknown_time_rows"] == 2
    assert payload["totals"]["rows"] == 1
    codex = next(bucket for bucket in payload["by_client"] if bucket["client"] == "codex")
    assert codex["rows"] == 1
    assert codex["input_tokens"] == 100
    assert [entry["period"] for entry in payload["by_period"]] == [
        (today - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)
    ]
    # All-time keeps the rows under the explicit unknown period — they are
    # dropped from the range, never deleted from the ledger. Only the additive
    # row's tokens reach the period bucket; the held row stays evidence-only.
    all_time = client.get("/usage/summary?days=all&granularity=daily").json()
    unknown_period = next(entry for entry in all_time["by_period"] if entry["period"] == "unknown")
    assert unknown_period["rows"] == 2 and unknown_period["fresh_tokens"] == 10

    # Both disclosures name the same two dropped rows — one additive, one held
    # — and the undated HELD row is disclosed by the unknown-time counter, not
    # by the range-scoped held counter (it carries no date to be held "inside
    # the range" with).
    assert payload["usage_exclusions"] == {
        "non_additive_rows": 0,
        "unknown_time_rows": 2,
        "reason": "legacy_codex_descendant_cumulative_unproven",
        "raw_evidence_preserved": True,
    }
    assert payload["usage_exclusions"]["unknown_time_rows"] == payload["totals"]["unknown_time_rows"]
    # Same rule with days=all: nothing is dropped for an unusable timestamp
    # there (the rows are kept under the "unknown" period), so the shared count
    # matches those kept rows rather than claiming an exclusion — and the held
    # row now also lands in the held-lane counter, since no range excludes it.
    assert all_time["usage_exclusions"]["unknown_time_rows"] == all_time["totals"]["unknown_time_rows"] == 2
    assert all_time["usage_exclusions"]["non_additive_rows"] == 1
    assert unknown_period["rows"] == all_time["usage_exclusions"]["unknown_time_rows"]


# ---------------------------------------------------------------------------
# Explicit date range + provider filter — the independent filter axes the
# macOS Usage range/filter controls drive. `start`/`end` are a closed
# interval that replaces `days` whenever either bound is given; `provider`
# follows the locked "unknown value = empty result + honest echo, never a
# guess" rule the model filter established.
# ---------------------------------------------------------------------------


def test_usage_summary_explicit_range_equals_the_days_preset_and_echoes_the_mode(tmp_path):
    """``start=today-6 & end=today`` IS ``days=7`` — every section — and the
    echo says which mode produced the numbers."""

    store_root = tmp_path / "state"
    today = date.today()
    _seed_usage_rows(store_root, _range_rows(), today=today)
    client = _client(store_root)

    start = (today - timedelta(days=6)).isoformat()
    end = today.isoformat()
    explicit = client.get(f"/usage/summary?start={start}&end={end}&granularity=daily").json()
    preset = client.get("/usage/summary?days=7&granularity=daily").json()

    # Both boundary days are inclusive, so the equivalence holds in every
    # section — including the gap-filled period list and range_context.
    for section in ("totals", "by_client", "by_model", "by_period", "usage_exclusions", "range_context"):
        assert explicit[section] == preset[section]
    assert [entry["period"] for entry in explicit["by_period"]][0] == start
    assert [entry["period"] for entry in explicit["by_period"]][-1] == end

    assert explicit["filters_echo"]["range_mode"] == "explicit"
    assert explicit["filters_echo"]["resolved_start"] == start
    assert explicit["filters_echo"]["resolved_end"] == end
    assert preset["filters_echo"]["range_mode"] == "days"
    assert preset["filters_echo"]["resolved_start"] == start
    assert preset["filters_echo"]["resolved_end"] == end

    # An explicit interval REPLACES the days window; it never intersects with
    # the rolling default the request also carried.
    overriding = client.get(
        f"/usage/summary?days=30&start={start}&end={end}&granularity=daily"
    ).json()
    assert overriding["totals"] == preset["totals"]
    assert overriding["by_period"] == preset["by_period"]
    # ...and `days` is still echoed exactly as requested, because range_mode is
    # what says it did not run.
    assert overriding["filters_echo"]["days"] == "30"
    assert overriding["filters_echo"]["range_mode"] == "explicit"

    # days=all is the unbounded mode: it has no resolved bounds to name.
    all_time = client.get("/usage/summary?days=all").json()
    assert all_time["filters_echo"]["range_mode"] == "days"
    assert all_time["filters_echo"]["resolved_start"] is None
    assert all_time["filters_echo"]["resolved_end"] is None


def test_usage_summary_rejects_unparseable_dates_and_a_reversed_range(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="date-validation", started_at=time.time() - 3600)
    client = _client(store_root)

    # Same validation class as the whitelists: junk is a 422, never a quietly
    # ignored filter (that would answer a different question than the one asked).
    for query in (
        "start=not-a-date",
        "start=2026-7-1",  # not zero-padded, so not the documented YYYY-MM-DD
        "end=2026-02-30",  # no such day
        "end=2026-13-01",  # no such month
    ):
        assert client.get(f"/usage/summary?{query}").status_code == 422
    assert client.get("/usage/summary?start=2026-07-10&end=2026-07-09").status_code == 422
    # A single bound needs no partner and is not a reversed range.
    assert client.get("/usage/summary?start=2026-07-09&end=2026-07-10").status_code == 200
    assert client.get("/usage/summary?end=2026-07-10").status_code == 200

    # A valid window with no saved rows is an empty 200 that echoes the range:
    # an empty result, never an error and never a fallback to a nearby window.
    empty = client.get("/usage/summary?start=2000-01-01&end=2000-01-31").json()
    assert empty["totals"]["rows"] == 0
    assert empty["by_client"] == [] and empty["by_model"] == [] and empty["by_period"] == []
    assert empty["filters_echo"]["range_mode"] == "explicit"
    assert empty["filters_echo"]["resolved_start"] == "2000-01-01"
    assert empty["filters_echo"]["resolved_end"] == "2000-01-31"
    assert empty["range_context"] == {"history_outside_range": []}


def test_usage_summary_combined_filters_are_the_matching_row_sums(tmp_path):
    """client + provider + model + explicit interval: totals are exactly the
    rows that pass EVERY filter, recomputed with plain arithmetic."""

    store_root = tmp_path / "state"
    today = date.today()
    # (client, provider, model, day offset, input, output, cost)
    rows = [
        ("claude-code", "anthropic", "fable-5", 0, 100, 25, 0.10),
        ("claude-code", "anthropic", "fable-5", 3, 200, 50, 0.20),
        ("claude-code", "anthropic", "fable-5", 8, 400, 100, 0.40),
        ("claude-code", "anthropic", "haiku-4", 1, 7, 3, 0.07),
        ("claude-code", "router", "fable-5", 1, 5, 5, 0.05),
        ("codex", "openai", "fable-5", 1, 900, 900, 9.00),
    ]
    for client_name, provider, model, offset, input_tokens, output_tokens, cost in rows:
        _trusted_usage(
            store_root,
            session=f"combo-{offset}-{provider}-{model}-{client_name}",
            client=client_name,
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=cost,
            started_at=_range_day(offset, today),
        )
    client = _client(store_root)

    start, end = today - timedelta(days=6), today
    payload = client.get(
        "/usage/summary?client=claude-code&provider=anthropic&model=fable-5"
        f"&start={start.isoformat()}&end={end.isoformat()}&granularity=daily"
    ).json()

    # Independent recomputation: the rows passing all four filters, summed with
    # nothing but the documented closed-interval rule.
    expected = [
        row
        for row in rows
        if row[0] == "claude-code"
        and row[1] == "anthropic"
        and row[2] == "fable-5"
        and start <= today - timedelta(days=row[3]) <= end
    ]
    expected_cost = sum(row[6] for row in expected)
    assert len(expected) == 2
    assert expected_cost == pytest.approx(0.30)
    totals = payload["totals"]
    assert totals["rows"] == len(expected)
    assert totals["sessions"] == len(expected)
    assert totals["input_tokens"] == sum(row[4] for row in expected) == 300
    assert totals["output_tokens"] == sum(row[5] for row in expected) == 75
    assert totals["fresh_tokens"] == 375
    assert totals["total_tokens_including_cached"] == 375
    assert totals["estimated_cost_usd"] == pytest.approx(expected_cost)
    assert [entry["client"] for entry in payload["by_client"]] == ["claude-code"]
    assert [(entry["client"], entry["provider"], entry["model"]) for entry in payload["by_model"]] == [
        ("claude-code", "anthropic", "fable-5")
    ]
    assert sum(entry["rows"] for entry in payload["by_client"]) == totals["rows"]
    assert sum(entry["rows"] for entry in payload["by_period"]) == totals["rows"]

    # The filters really did cut: the same store unfiltered holds all six rows.
    everything = client.get("/usage/summary?days=all&granularity=daily").json()
    assert everything["totals"]["rows"] == 6
    assert len(everything["by_model"]) == 4


def test_usage_summary_open_ended_explicit_range_bounds_one_side_and_keeps_the_contract(tmp_path):
    """One bound is enough: it still replaces days, still drops-and-counts
    unknown-time rows, and still explains older saved history."""

    store_root = tmp_path / "state"
    today = date.today()
    _seed_usage_rows(store_root, _range_rows(), today=today)
    SentinelService(store_root).record_event(
        _trusted_usage_event(
            session="undated-in-explicit-range",
            client="codex",
            model="gpt-5.5",
            started_at=1e300,
            updated_at=1e300,
        ),
        trusted_usage_import=True,
    )
    client = _client(store_root)

    start = (today - timedelta(days=6)).isoformat()
    payload = client.get(f"/usage/summary?start={start}&granularity=daily").json()

    assert payload["filters_echo"]["range_mode"] == "explicit"
    assert payload["filters_echo"]["resolved_start"] == start
    # An open side is named as open, not silently filled with a guess.
    assert payload["filters_echo"]["resolved_end"] is None
    assert [entry["period"] for entry in payload["by_period"]] == [
        (today - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)
    ]
    # A bounded range drops the unusable timestamp AND counts it, in both
    # disclosures (the count cannot drift between them).
    assert payload["totals"]["unknown_time_rows"] == 1
    assert payload["usage_exclusions"]["unknown_time_rows"] == 1

    # Older saved history is still explained in explicit mode: every saved
    # Hermes row sits before a start of today, so its absence is disclosed
    # instead of reading as deleted data.
    only_today = client.get(
        f"/usage/summary?client=hermes&start={today.isoformat()}&end={today.isoformat()}"
    ).json()
    assert only_today["by_client"] == []
    assert [entry["client"] for entry in only_today["range_context"]["history_outside_range"]] == ["hermes"]
    assert only_today["range_context"]["history_outside_range"][0]["rows"] == 2
