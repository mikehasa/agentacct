"""Phase 3.5b regressions: the Tokens explorer (PRD §5).

The usage cube (pure aggregation: bucketing, weekly rollup, empty periods,
distinct-session counting, dominant/mixed cost confidence, unknown-time
guard); the /usage/summary endpoint (shape, whitelist validation, the locked
unknown-model-echoes-empty decision, trusted-import-only intake); the rebuilt
/tokens page (filter URLs, total-first filtered totals, explicit category
breakdowns, and cache-reporting capability labels); the total-token SVG chart
(cache-heavy rows affect bar height; a11y pins; empty-store state); and Usage
page ownership of the chart and ranked breakdowns.

All stores are throwaway tmp_path stores (suite conftest guards the real
dogfood ledger)."""

import re
import time
from datetime import date, datetime, time as dtime, timedelta

import pytest
from fastapi.testclient import TestClient

import agent_chronicle.api as api_module
from agent_chronicle.api import DashboardUsageRecord, _usage_record_time, create_local_api_app
from agent_chronicle.service import SentinelService
from agent_chronicle.usage_cube import (
    build_usage_cube,
    client_lane_class,
    filter_usage_records,
    models_in_records,
    resolve_granularity,
    week_start,
)

HTML_ACCEPT = {"Accept": "text/html"}
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


def _bar_rect_signature(html: str) -> list[tuple[str, str]]:
    """(class, height) of every stacked-bar rect, in document order."""

    return re.findall(r'<rect class="(chart-bar [^"]*)" [^>]*height="([0-9.]+)"', html)


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


def test_usage_summary_and_tokens_do_not_call_future_rows_preserved_history(tmp_path):
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
    html = client.get("/tokens?client=hermes").text

    assert bounded["by_client"] == []
    assert bounded["range_context"]["history_outside_range"] == []
    assert all_time["by_client"][0]["rows"] == 1
    assert "Saved history is preserved." not in html
    assert "Hermes — 1 all-time row(s)" not in html


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

    tokens_html = client.get("/tokens?days=all", headers=HTML_ACCEPT).text
    assert (
        "1 source-conflicted or lineage-dependent usage row is held out of token and cost totals"
        in tokens_html
    )
    assert "9,000,000,000" not in tokens_html
    overview_html = client.get("/", headers=HTML_ACCEPT).text
    assert "Known subtotal incl. cache" in overview_html
    assert "Known cost subtotal" in overview_html

    # Quarantine is a derived aggregation rule, not deletion: the raw page
    # labels the row, while the event endpoint still exposes its original
    # client counters for forensic inspection.
    raw_html = client.get("/raw", headers=HTML_ACCEPT).text
    assert "Held from totals" in raw_html
    assert "81,000,000,000 raw cumulative" in raw_html
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

    tokens_html = client.get("/tokens?days=all", headers=HTML_ACCEPT).text
    assert "Usage normalization in progress" in tokens_html
    assert "No saved usage rows in this range yet" not in tokens_html
    assert "0 total tokens" not in tokens_html
    assert "$0.00" not in tokens_html

    raw_html = client.get("/raw", headers=HTML_ACCEPT).text
    assert "Held from totals" in raw_html
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


def test_usage_summary_documented_in_raw_debug_endpoints_table(tmp_path):
    client = _client(tmp_path / "state")

    raw_html = client.get("/raw").text

    assert "/usage/summary" in raw_html
    assert "Usage cube JSON" in raw_html


# ---------------------------------------------------------------------------
# /tokens explorer page
# ---------------------------------------------------------------------------


def test_tokens_filter_urls_render_and_filtered_totals_restate_basis(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    _trusted_usage(
        store_root,
        session="filter-codex",
        client="codex",
        input_tokens=100,
        output_tokens=25,
        cache_read=9_999,
        cache_creation_reported=False,
        started_at=now - 3600,
    )
    _trusted_usage(store_root, session="filter-claude", client="claude-code", model="fable-5", input_tokens=500, output_tokens=50, started_at=now - 7200)
    client = _client(store_root)

    html = client.get("/tokens?client=codex&days=7").text

    # Every filter combination is a URL: pills re-encode the current state.
    assert 'href="/tokens?client=all&amp;model=all&amp;days=7&amp;granularity=auto&amp;cost_sort=total"' in html
    assert 'href="/tokens?client=claude-code&amp;model=all&amp;days=7&amp;granularity=auto&amp;cost_sort=total"' in html
    assert 'href="/tokens?client=codex&amp;model=all&amp;days=30&amp;granularity=auto&amp;cost_sort=total"' in html
    assert 'href="/tokens?client=codex&amp;model=all&amp;days=7&amp;granularity=weekly&amp;cost_sort=total"' in html
    assert 'href="/tokens?client=codex&amp;model=fable-5&amp;days=7&amp;granularity=auto&amp;cost_sort=total"' in html
    # Filtered totals lead with the cache-inclusive volume and estimated cost,
    # then preserve the component split. Codex does not expose a distinct
    # cache-write counter, so the UI must say that rather than inventing zero.
    assert "Filtered totals: <strong>10,124 total tokens</strong> (incl. caches)" in html
    assert "Breakdown: 100 input after reported cache" in html
    assert "25 output" in html
    assert "cache writes not reported by this source" in html
    assert "9,999 cache reads" in html
    assert "estimated, not a provider bill" in html
    # The other client's rows are genuinely filtered out.
    assert "fable-5" not in html.split("Filtered totals:")[1].split("By model")[0]
    # Bogus whitelist values fall back to defaults, never a 500.
    assert client.get("/tokens?client=bogus&days=999&granularity=hourly").status_code == 200


def test_tokens_date_window_says_preserved_platform_history_is_not_deleted(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    _trusted_usage(
        store_root,
        session="current-codex",
        client="codex",
        started_at=now - 3600,
    )
    _trusted_usage(
        store_root,
        session="older-hermes",
        client="hermes",
        model="gpt-5.4-mini",
        started_at=now - 45 * 24 * 60 * 60,
    )
    client = _client(store_root)

    default_html = client.get("/tokens").text
    hermes_html = client.get("/tokens?client=hermes").text
    all_time_html = client.get("/tokens?client=hermes&days=all").text

    for html in (default_html, hermes_html):
        assert "Saved history is preserved." in html
        assert "Hermes — 1 all-time row(s)" in html
        assert "View all time" in html
    assert (
        'href="/tokens?client=hermes&amp;model=all&amp;days=all&amp;granularity=auto&amp;cost_sort=total"'
        in hermes_html
    )
    assert "Saved history is preserved." not in all_time_html
    assert "Hermes" in all_time_html


def test_tokens_unknown_model_renders_empty_result_with_echo(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="model-echo", started_at=time.time() - 3600)
    client = _client(store_root)

    html = client.get("/tokens?model=never-imported").text

    assert "<code>never-imported</code> has no saved usage rows" in html
    assert "never a guess" in html
    assert "Usage unavailable for this filter." in html
    assert "token and cost totals are unknown, not zero" in html
    assert "Filtered totals: <strong>0 total tokens</strong>" not in html
    assert "No saved usage rows match this filter." in html


def test_tokens_observation_only_store_never_claims_zero_usage(tmp_path):
    store_root = tmp_path / "state"
    SentinelService(store_root).record_trusted_session_observation(
        {
            "source": "codex-local-session-observation-import",
            "event_type": "session_observed",
            "run_id": "client_codex_observed_only",
            "metadata": {
                "client": "codex",
                "client_session_id": "observed-only",
                "source_namespace_fingerprint": "sha256:" + "a" * 64,
                "started_at": 100.0,
                "updated_at": 200.0,
                "source_parse_complete": True,
            },
        }
    )

    html = _client(store_root).get("/tokens?days=all").text

    assert "Usage unavailable for this filter." in html
    assert "token and cost totals are unknown, not zero" in html
    assert "0 total tokens" not in html
    assert "$0.00" not in html


def test_tokens_tables_lead_with_total_and_show_component_breakdown(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(
        store_root,
        session="honesty-row",
        client="claude-code",
        model="fable-5",
        cache_creation=10,
        cache_read=1_000_000,
        started_at=time.time() - 3600,
    )
    client = _client(store_root)

    html = client.get("/tokens").text

    # The old ambiguous bare token column and caption excuse are gone.
    assert "By agent" not in html
    assert "include cache reads at full weight" not in html
    assert "<th>Tokens</th>" not in html
    # Every aggregate table leads with Total tokens, then shows the same four
    # input/output/cache-write/cache-read categories.
    assert "By platform" in html
    assert "By model" in html
    assert "By period" in html
    platform_header = html.index("<th>Platform</th>")
    assert html.index("<th>Total tokens</th>", platform_header) < html.index(
        "<th>Input after reported cache</th>", platform_header
    )
    assert "<th>Output</th>" in html
    assert "<th>Cache writes</th>" in html
    assert "<th>Cache reads</th>" in html
    # Fix round, cluster A: the By-model cost column renders the cube's
    # stored-cost basis in ONE confidence-chipped column (the per-component
    # catalog re-estimates are gone from /tokens).
    assert "<th>Est. cost</th>" in html
    assert "<th>Total tokens incl. caches / cost</th>" not in html
    assert "Tokens incl. caches" in html  # confidence tables label their basis
    assert "Usage basics" in html  # the confidence tables stay at the bottom
    # Ranked platform bar exists and uses the lane palette; the platform
    # badge carries the SAME lane class as bars/chart/legend (batch D
    # consistency: one lane-color source everywhere).
    assert 'class="bar-fill lane-claude-code"' in html
    assert 'class="badge-client lane-claude-code"' in html
    # The platform/model/period rows lead with the full 1,000,135-token volume.
    assert "<strong>1,000,135</strong>" in html


def test_codex_cache_write_counter_not_reported_renders_as_unknown_not_zero(tmp_path):
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

    html = client.get("/tokens?client=codex&days=all").text
    overview = client.get("/").text
    platform_table = html[html.index("By platform") : html.index("By model")]

    assert "cache writes not reported by this source" in html
    assert "Not reported by this source" in platform_table
    assert "<th>Cache writes</th>" in platform_table
    assert "cache writes not reported by this source" in overview
    assert "0 reported cache writes" not in overview
    assert "Est. API cost" in overview
    assert "Partial est. cost" not in overview
    filtered_totals = html[html.index("Filtered totals:") : html.index("By platform")]
    assert "estimated, not a provider bill" in filtered_totals
    assert "partial estimate" not in filtered_totals


def test_saved_legacy_row_without_capability_flags_stays_unknown_in_api_and_ui(tmp_path):
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
    html = client.get("/tokens?days=all").text
    overview = client.get("/").text

    assert summary["totals"]["cache_creation_tokens"] == 10
    assert summary["totals"]["cache_creation_reporting"] == "unknown"
    assert summary["totals"]["cache_read_tokens"] == 40
    assert summary["totals"]["cache_read_reporting"] == "unknown"
    assert "cache-write reporting capability unknown" in html
    assert "cache-read reporting capability unknown" in html
    assert "Reporting capability unknown" in html
    assert "0 reported cache writes" not in overview
    assert "0 reported cache reads" not in overview


def test_tokens_by_period_table_renders_empty_periods(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    _trusted_usage(store_root, session="period-a", started_at=now - 3600)
    _trusted_usage(store_root, session="period-b", started_at=now - 3 * 86400)
    client = _client(store_root)

    html = client.get("/tokens?days=7").text

    period_section = html[html.index("By period") :]
    for offset in range(7):
        day = (date.today() - timedelta(days=offset)).isoformat()
        assert day in period_section, day


def test_chart_cache_read_heavy_day_changes_total_token_bar(tmp_path):
    """The §5.3 total-first regression: every reported token category,
    including cache reads, contributes to the main bar height."""

    now = time.time()

    def seed(store_root, *, heavy_reads):
        _trusted_usage(store_root, session="chart-a", client="codex", input_tokens=800, output_tokens=200, started_at=now - 3600)
        _trusted_usage(
            store_root,
            session="chart-b",
            client="claude-code",
            model="fable-5",
            input_tokens=400,
            output_tokens=100,
            cache_creation=250,
            cache_read=500_000_000 if heavy_reads else 0,
            started_at=now - 2 * 86400,
        )

    plain_store = tmp_path / "plain" / "state"
    heavy_store = tmp_path / "heavy" / "state"
    seed(plain_store, heavy_reads=False)
    seed(heavy_store, heavy_reads=True)

    plain_html = _client(plain_store).get("/tokens").text
    heavy_html = _client(heavy_store).get("/tokens").text

    plain_bars = _bar_rect_signature(plain_html)
    heavy_bars = _bar_rect_signature(heavy_html)
    assert plain_bars, "expected stacked bar rects"
    assert plain_bars != heavy_bars
    assert "500,000,750 total tokens" in heavy_html
    assert "500,000,000 cache reads" in heavy_html
    assert 'class="chart-read"' not in heavy_html


def test_chart_a11y_pins_and_empty_periods_and_empty_store_state(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="a11y-row", started_at=time.time() - 3600)
    client = _client(store_root)

    html = client.get("/tokens?days=7").text
    chart = html[html.index('<figure class="chart" id="tokens-chart">') : html.index("</figure>")]
    assert 'role="img"' in chart
    assert 'aria-labelledby="tokens-chart-title tokens-chart-desc"' in chart
    assert "Total tokens per day, last 7 days, stacked by platform" in chart
    # The <desc> states the basis in words.
    assert "every reported input, output, cache-write, and cache-read token" in chart
    assert "Tooltips show the category breakdown" in chart
    # Native per-rect tooltips, incl. labeled empty-period hover slots.
    assert "<title>" in chart
    assert "no usage rows</title>" in chart
    assert 'class="chart-hover"' in chart
    # Legend chips carry the platform lane palette; category detail stays in
    # the native tooltip instead of creating a second cache-read axis.
    assert 'class="legend-swatch lane-codex"' in chart
    assert "Bar height = all reported token categories" in chart
    assert 'class="chart-read"' not in chart

    # Empty store: an explicit empty state, never an all-zero fake chart.
    empty_html = _client(tmp_path / "empty-state").get("/tokens").text
    assert "No saved usage rows in this range yet" in empty_html
    assert "<svg" not in empty_html


def test_tokens_page_redaction_sweep(tmp_path):
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

    html = client.get("/tokens?days=all").text

    # No absolute paths, no usernames, no session ids (full or short) —
    # /tokens is pure aggregate. Model names are fine (and required).
    assert "/Users/" not in html
    assert "testuser" not in html
    assert session_uuid not in html
    assert session_uuid[:8] not in html
    assert "fable-5" in html


# ---------------------------------------------------------------------------
# Phase 3.5 fix round, cluster A (major): the By-model table renders the
# cube's STORED-cost basis — the pricing-catalog re-estimate is gone from
# /tokens — so By platform, By model, filtered totals, the Overview card,
# and /usage/summary agree on identical filters.
# ---------------------------------------------------------------------------


def _by_model_slice(html: str) -> str:
    return html[html.index("By model") : html.index("By period")]


def test_by_model_renders_stored_cost_for_unpriced_model_consistently(tmp_path):
    # Probe scenario 1: an unpriced model whose row carries a client-reported
    # $5.00 stored cost — every surface must say $5.00, never "No estimate".
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

    html = client.get("/tokens").text
    by_model = _by_model_slice(html)
    assert "$5.00" in by_model
    assert "client_reported confidence" in by_model
    assert "No estimate" not in by_model
    # Stored-cost coverage column: rows with a stored cost vs without.
    assert "1 costed" in by_model
    assert "$5.00" in html[html.index("By platform") : html.index("By model")]
    assert "$5.00" in html[html.index("Filtered totals:") : html.index("By platform")]
    # Overview cost card and the JSON cube agree.
    assert "$5.00" in client.get("/").text
    summary = client.get("/usage/summary").json()
    assert summary["by_model"][0]["estimated_cost_usd"] == pytest.approx(5.0)
    assert summary["by_model"][0]["cost_confidence"] == "client_reported"


def test_by_model_never_replaces_client_reported_cost_with_list_price(tmp_path):
    # Probe scenario 2: a priced model with a client-reported $50.00 stored
    # cost — the table must render the stored figure with its confidence
    # chip, never a silent catalog re-estimate of the same rows.
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

    html = client.get("/tokens").text
    by_model = _by_model_slice(html)
    assert "$50.00" in by_model
    assert "client_reported confidence" in by_model
    assert "$12.50" not in html  # the old list-price re-estimate of 1M input
    assert "$50.00" in html[html.index("By platform") : html.index("By model")]
    summary = client.get("/usage/summary").json()
    assert summary["by_model"][0]["estimated_cost_usd"] == pytest.approx(50.0)


def test_by_model_row_without_stored_cost_keeps_honest_no_estimate(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(
        store_root, session="uncosted-row", model="mystery-model", cost=None, started_at=time.time() - 3600
    )
    client = _client(store_root)

    by_model = _by_model_slice(client.get("/tokens").text)
    assert "No estimate" in by_model
    assert "1 uncosted" in by_model


def test_partial_cost_sum_is_labeled_with_row_coverage_everywhere(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
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

    html = client.get("/tokens?days=all").text
    totals = html[html.index("Filtered totals:") : html.index("By platform")]

    assert "$5.00 partial estimate, not a provider bill" in totals
    assert "1/2 additive rows priced" in totals
    # Platform, model, and populated period buckets all expose the same
    # incomplete-row coverage instead of presenting $5.00 as a whole sum.
    assert html.count("partial estimate · 1/2 rows priced") >= 3
    assert "1 costed / 1 uncosted" in _by_model_slice(html)
    assert "Partial est. cost" in client.get("/").text
    summary = client.get("/usage/summary?days=all").json()
    assert summary["totals"]["estimated_cost_usd"] == pytest.approx(5.0)
    assert summary["totals"]["priced_rows"] == 1
    assert summary["totals"]["unpriced_rows"] == 1


def test_default_cost_sort_places_complete_buckets_before_partial_sums():
    entries = [
        {"model": "partial-high", "estimated_cost_usd": 100.0, "unpriced_rows": 1},
        {"model": "complete-low", "estimated_cost_usd": 1.0, "unpriced_rows": 0},
        {"model": "unpriced", "estimated_cost_usd": None, "unpriced_rows": 1},
    ]

    ordered = sorted(entries, key=api_module._tokens_by_model_sort_key("total"), reverse=True)

    assert [entry["model"] for entry in ordered] == ["complete-low", "partial-high", "unpriced"]


# ---------------------------------------------------------------------------
# Phase 3.5 fix round, cluster C (major): chart/table truth — the cap note
# never claims completeness, the Unknown-time row survives the cap, the
# empty state never lies about saved rows, zero-fresh axes stay unmeasured,
# and the leading partial weekly bucket says so.
# ---------------------------------------------------------------------------


def test_chart_cap_note_names_both_caps_and_unknown_row_survives_the_cap(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    # Two rows ~130 weeks apart gap-fill into >120 weekly periods; one row
    # with an absurd-but-finite timestamp lands under Unknown time.
    _trusted_usage(store_root, session="cap-old-row", started_at=now - 130 * 7 * 86400)
    _trusted_usage(store_root, session="cap-new-row", started_at=now - 3600)
    _trusted_usage(store_root, session="cap-bad-ts", started_at=1e300)
    client = _client(store_root)

    html = client.get("/tokens?days=all").text

    assert "the By period table is complete" not in html
    match = re.search(
        r"Chart draws the most recent 120 of ([\d,]+) periods; "
        r"the By period table below shows the most recent 60 of \1\.",
        html,
    )
    assert match, "cap note must state both caps over the same period total"
    assert f"Showing 60 of {match.group(1)} periods." in html
    # The Unknown time row is pinned outside the cap — the filtered-totals
    # pointer ("appear under Unknown time") must always be true.
    period_section = html[html.index("By period") : html.index("Usage basics")]
    assert "Unknown time" in period_section
    assert "appear under Unknown time" in html


def test_chart_distinguishes_unknown_time_only_store_from_truly_empty(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="unknown-only-a", started_at=1e300)
    _trusted_usage(store_root, session="unknown-only-b", started_at=9e299)
    client = _client(store_root)

    html = client.get("/tokens?days=all").text
    figure = html[html.index('<figure class="chart" id="tokens-chart">') : html.index("</figure>")]

    # Rows ARE saved and in range (days=all keeps unknown-time rows in the
    # totals) — the chart must not claim otherwise or prescribe a re-import.
    assert "No saved usage rows in this range yet" not in figure
    assert "usable timestamp" in figure
    assert "Unknown time" in figure
    assert "2 usage row(s)" in html  # totals line still counts them
    assert "Unknown time" in html[html.index("By period") : html.index("Usage basics")]


def test_cache_read_only_range_renders_real_total_bar_without_fabricated_ticks(tmp_path):
    # Cache-read-only usage is still real token volume on the total axis. It
    # gets a bar while retaining the no-fabricated-fractional-ticks guarantee.
    store_root = tmp_path / "state"
    _trusted_usage(
        store_root,
        session="reads-only-row",
        input_tokens=0,
        output_tokens=0,
        cache_read=250_000_000,
        started_at=time.time() - 3600,
    )
    client = _client(store_root)

    html = client.get("/tokens?days=7").text
    chart = html[html.index('<figure class="chart" id="tokens-chart">') : html.index("</figure>")]

    assert ">0.5</text>" not in chart
    assert ">1</text>" not in chart
    assert 'class="chart-bar lane-codex"' in chart
    assert "250,000,000 total tokens" in chart
    assert "250,000,000 cache reads" in chart
    assert 'class="chart-read"' not in chart
    assert ">0</text>" in chart


def test_leading_partial_weekly_bucket_hover_says_partial_not_no_usage():
    # TODAY is a Friday: days=7 starts Saturday 2026-07-04, inside the ISO
    # week of Monday 2026-06-29 — the leading bucket is a partial week.
    records = [_cube_record(day=date(2026, 7, 8), session="in-range")]
    cube = _cube(records, days=7, granularity="weekly")

    svg = api_module._tokens_chart_html(
        cube,
        esc=api_module._esc_html,
        chart_id="t",
        granularity="weekly",
        range_label="last 7 days",
        range_days=7,
        today=TODAY,
    )

    assert "Week of 2026-06-29 · partial week (range starts mid-week)" in svg
    assert "Week of 2026-06-29 · no usage rows" not in svg


# ---------------------------------------------------------------------------
# Phase 3.5 fix round, cluster D: bounded surfaces — the By-model table and
# the data-driven filter pill rows are capped with honest notes, and unknown
# filter values are never echoed into constructed URLs.
# ---------------------------------------------------------------------------


def test_by_model_table_capped_at_60_with_honest_note_and_pills_capped_at_12(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    for index in range(65):
        _trusted_usage(store_root, session=f"cap-{index:03d}", model=f"model-{index:03d}", started_at=now - 60 - index)
    # A model with almost no fresh volume but a large cache-read volume must
    # rank into the total-token pill top-12 despite sorting last alphabetically.
    _trusted_usage(
        store_root,
        session="cap-busy",
        model="zz-busy-model",
        input_tokens=1,
        output_tokens=0,
        cache_read=1_000_000,
        started_at=now - 30,
    )
    client = _client(store_root)

    html = client.get("/tokens?days=all").text

    by_model = _by_model_slice(html)
    assert "Showing 60 of 66 model rows." in html
    assert by_model.count("<tr>") - 1 == 60  # one header row
    # Model pills: top 12 by total tokens in the current range + an honest
    # overflow note; the cache-heavy model ranks in, the tail does not render.
    model_row = html[html.index('<span class="filter-label">Model</span>') : html.index('<span class="filter-label">Date range</span>')]
    assert ">zz-busy-model</a>" in model_row
    assert ">model-010</a>" in model_row
    assert ">model-011</a>" not in model_row  # rank 13 (after the busy model)
    assert "and 54 more (use ?model=)" in model_row


def test_unknown_model_value_never_echoes_into_urls_and_stays_escaped(tmp_path):
    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="echo-guard", started_at=time.time() - 3600)
    client = _client(store_root)

    evil = "<script>alert(1)</script>"
    response = client.get("/tokens", params={"model": evil})

    assert response.status_code == 200
    html = response.text
    assert "<script>alert(1)</script>" not in html  # escaped everywhere it appears
    assert "&lt;script&gt;" in html  # the unknown-filter note names it, escaped
    assert "has no saved usage rows" in html  # the empty-result state renders
    for href in re.findall(r'href="([^"]*)"', html):
        assert "script" not in href and "alert" not in href, href


# ---------------------------------------------------------------------------
# Phase 3.5 fix round, cluster E: ONE shared dominant-confidence rule —
# weighted by summed cost (fallback row count), "mostly X" only when
# strictly dominant, ties name both labels and never break toward the
# higher-authority label.
# ---------------------------------------------------------------------------


def test_confidence_tie_renders_mixed_naming_both_never_mostly():
    records = [
        _cube_record(session="tie-a", day=TODAY, cost=0.05, cost_confidence="provider_billed"),
        _cube_record(session="tie-b", day=TODAY, cost=0.05, cost_confidence="estimated_from_tokens"),
    ]

    totals = _cube(records, days=30, granularity="daily")["totals"]

    assert "mostly" not in api_module._bucket_confidence_label(totals)
    assert api_module._bucket_confidence_label(totals) == (
        "mixed confidence (estimated_from_tokens + provider_billed)"
    )
    # The tie never resolves toward the higher-authority label.
    assert totals["cost_confidence"] == "estimated_from_tokens"
    assert totals["cost_confidence_mixed"] is True


def test_confidence_majority_by_cost_weight_renders_mostly():
    records = [
        _cube_record(session="w-a", day=TODAY, cost=0.99, cost_confidence="estimated_from_tokens"),
        _cube_record(session="w-b", day=TODAY, cost=0.01, cost_confidence="client_reported"),
    ]

    totals = _cube(records, days=30, granularity="daily")["totals"]

    assert api_module._bucket_confidence_label(totals) == "mixed confidence (mostly estimated_from_tokens)"


def test_confidence_weighting_uses_cost_not_row_counts():
    # 99 tiny estimated rows vs one $50 client-reported row: cost weight
    # dominates, so the label says mostly client_reported.
    records = [
        _cube_record(session=f"tiny-{index}", day=TODAY, cost=0.0001, cost_confidence="estimated_from_tokens")
        for index in range(99)
    ] + [_cube_record(session="big", day=TODAY, cost=50.0, cost_confidence="client_reported")]

    totals = _cube(records, days=30, granularity="daily")["totals"]

    assert api_module._bucket_confidence_label(totals) == "mixed confidence (mostly client_reported)"


def test_confidence_single_bucket_renders_plain_label():
    records = [_cube_record(session="solo", day=TODAY, cost=0.10, cost_confidence="estimated_from_tokens")]

    totals = _cube(records, days=30, granularity="daily")["totals"]

    assert api_module._bucket_confidence_label(totals) == "estimated_from_tokens confidence"


def test_work_keeps_cost_compact_while_usage_owns_confidence(tmp_path):
    # Work shows compact per-session estimates. Confidence analysis remains
    # on Usage and in its JSON contract.
    store_root = tmp_path / "state"
    now = time.time()
    for index in range(5):
        _trusted_usage(store_root, session=f"conf-{index}", cost=0.01, started_at=now - 600 - index)
    _trusted_usage(store_root, session="conf-unpriced", cost=None, cost_confidence=None, started_at=now - 300)
    client = _client(store_root)

    overview = client.get("/").text
    assert "$0.01 est." in overview
    assert "estimated_from_tokens confidence" not in overview
    assert "mixed confidence" not in overview
    usage_html = client.get("/tokens?days=all").text
    assert "estimated_from_tokens" in usage_html
    summary = client.get("/usage/summary?days=all").json()
    assert summary["totals"]["cost_confidence"] == "estimated_from_tokens"
    assert summary["totals"]["cost_confidence_mixed"] is False


# ---------------------------------------------------------------------------
# Phase 3.5 fix round, cluster H: one `today` per request — the cube never
# resolves its own clock on a page path, so a request served across local
# midnight cannot render two different row populations.
# ---------------------------------------------------------------------------


def test_pages_resolve_today_once_and_pass_it_to_the_cube(tmp_path, monkeypatch):
    import agent_chronicle.usage_cube as usage_cube_module

    store_root = tmp_path / "state"
    _trusted_usage(store_root, session="today-sess", started_at=time.time() - 3600)
    client = _client(store_root)

    class _NoClock(date):
        @classmethod
        def today(cls):
            raise AssertionError("usage_cube must receive today= from the route, never resolve its own clock")

    monkeypatch.setattr(usage_cube_module, "date", _NoClock)

    assert client.get("/tokens").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/usage/summary").status_code == 200


# ---------------------------------------------------------------------------
# Phase 3.5 fix round, cluster L: y-axis labels get a gutter wide enough
# that >=10M values never clip the leading digit.
# ---------------------------------------------------------------------------


def test_chart_total_axis_labels_fit_the_gutter_at_525m_scale():
    records = [
        _cube_record(
            session="big-day",
            day=TODAY,
            input_tokens=20_000_000,
            output_tokens=5_000_000,
            cache_read=500_000_000,
        )
    ]
    cube = _cube(records, days=7, granularity="daily")

    svg = api_module._tokens_chart_html(
        cube,
        esc=api_module._esc_html,
        chart_id="t",
        granularity="daily",
        range_label="last 7 days",
        range_days=7,
        today=TODAY,
    )

    labels = re.findall(r'<text class="chart-axis-label" x="([0-9.]+)"[^>]*text-anchor="end">([\d,.]+)</text>', svg)
    # 525M total rounds to the chart's 1B nice ceiling; 500M is its midpoint.
    assert any(text == "1,000,000,000" for _x, text in labels)
    assert any(text == "500,000,000" for _x, text in labels)
    for x, text in labels:
        # Conservative 11px system-ui advance (~6.5 viewBox units/char): the
        # right-aligned label must fit entirely inside the viewBox.
        assert float(x) - len(text) * 6.5 >= 0, (x, text)


# ---------------------------------------------------------------------------
# Phase 7 Usage ownership
# ---------------------------------------------------------------------------


def test_usage_page_owns_the_chart_and_ranked_breakdowns(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    # Claude Code has fewer fresh tokens but a huge cache-read pile: total-first
    # ranking and chart scale must put it ahead of Codex.
    _trusted_usage(store_root, session="ov-codex", client="codex", input_tokens=250, output_tokens=50, started_at=now - 3600)
    _trusted_usage(
        store_root,
        session="ov-claude",
        client="claude-code",
        model="fable-5",
        input_tokens=100,
        output_tokens=50,
        cache_read=800_000_000,
        started_at=now - 7200,
    )
    client = _client(store_root)

    work_html = client.get("/").text
    usage_html = client.get("/tokens").text

    # Work keeps only a compact saved-usage snapshot. The chart and detailed
    # rankings have one clear owner: Usage.
    assert '<figure class="chart"' not in work_html
    assert "Top platforms · fresh tokens" not in work_html
    assert '<figure class="chart" id="tokens-chart">' in usage_html
    assert "Total tokens per day, last 30 days, stacked by platform" in usage_html
    assert "By platform" in usage_html
    assert "By model" in usage_html
    platforms_table = usage_html[usage_html.index("By platform") : usage_html.index("By model")]
    # Claude Code leads on total volume because cache reads count in the total.
    assert platforms_table.index("Claude Code") < platforms_table.index("Codex")
    assert "800,000,000" in platforms_table
    assert "fable-5" in usage_html
    assert "gpt-5.5" in usage_html


def test_usage_range_empty_state_stays_on_usage_page(tmp_path):
    store_root = tmp_path / "state"
    # A single OLD row: store totals exist, but the 30-day window is empty.
    _trusted_usage(store_root, session="old-row", started_at=1_700_000_000.0)
    client = _client(store_root)

    work_html = client.get("/").text
    usage_html = client.get("/tokens").text

    assert "Usage snapshot" not in work_html
    assert "No saved usage rows in the last 30 days." not in work_html
    assert "No saved usage rows in this range yet" in usage_html


def test_tokens_page_has_instant_hover_on_chart_and_table_rows(tmp_path):
    store_root = tmp_path / "state"
    now = time.time()
    for d in range(3):
        _trusted_usage(store_root, session=f"cx{d}", client="codex", model="gpt-5.5", started_at=now - d * 86400 - 100)
        _trusted_usage(store_root, session=f"cc{d}", client="claude-code", model="opus", started_at=now - d * 86400 - 200)
    html = _client(store_root).get("/tokens", headers=HTML_ACCEPT).text
    # Chart gets the same instant no-JS hover as the homepage charts, while
    # keeping the verbose per-rect <title> and the empty-slot chart-hover.
    start = html.index('id="tokens-chart"')
    chart = html[start : html.index("</figure>", start)]
    assert 'class="ovh-hit"' in chart
    assert 'class="ovh-tip"' in chart
    assert ">Total<" in chart
    assert '<rect class="chart-bar' in chart
    # Tables highlight rows on hover, scoped to the usage page.
    assert "#tokens-explorer tbody tr:hover" in html
    assert "#usage-basics tbody tr:hover" in html
    # Still zero JavaScript (CSP forbids it).
    assert "<script" not in html.lower()
