"""Tests for the shared usage/limit snapshot layer (`agentacct.usage_snapshot`).

This module is the single data path `agentacct now`, `agentacct limits`, and the
Textual TUI all consume, so it is where the cube-windowing, cost-completeness,
and rate-limit-selection semantics are pinned.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time as dtime
from pathlib import Path

from agentacct.client_usage import ClientUsageEvent
from agentacct.service import SentinelService
from agentacct import usage_snapshot as us


# ---------------------------------------------------------------------------
# seeding helpers
# ---------------------------------------------------------------------------


def _record_usage(
    service: SentinelService,
    *,
    client: str,
    model: str,
    session_id: str,
    input_tokens: int,
    output_tokens: int,
    updated_at: int,
    estimated_cost_usd: float | None,
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


def _rl_event(
    *,
    client: str,
    run_id: str,
    captured_at: float,
    windows: list,
    org: str | None = None,
    origin: str | None = None,
    created_at: float = 0.0,
    plan_type: str | None = None,
    credits: dict | None = None,
    reached_type: str | None = None,
    source_file: str | None = None,
) -> dict:
    """A hand-built ``rate_limit_observed`` event (the selection/parsing layer is
    pure over such dicts, so limit tests need no store)."""

    return {
        "event_type": "rate_limit_observed",
        "source": "test",
        "run_id": run_id,
        "created_at": created_at,
        "metadata": {
            "client": client,
            "org": org,
            "origin": origin,
            "captured_at": captured_at,
            "windows": windows,
            "plan_type": plan_type,
            "credits": credits,
            "reached_type": reached_type,
            "source_file": source_file,
        },
    }


# a fixed local-noon "now" so trailing-day windows never straddle midnight.
_TODAY = date(2026, 6, 15)
_NOW = datetime.combine(_TODAY, dtime(12, 0, 0)).timestamp()


# ---------------------------------------------------------------------------
# pure formatters
# ---------------------------------------------------------------------------


def test_finite():
    assert us.finite(3) == 3.0
    assert us.finite(2.5) == 2.5
    assert us.finite(True) is None  # bool is not a number here
    assert us.finite(float("nan")) is None
    assert us.finite(float("inf")) is None
    assert us.finite(None) is None
    assert us.finite("4") is None


def test_format_tokens():
    assert us.format_tokens(1234567) == "1,234,567"
    assert us.format_tokens(0) == "0"
    assert us.format_tokens(None) == "—"
    assert us.format_tokens(True) == "—"


def test_cost_text_honours_cost_complete():
    # complete → plain $; presence of estimated_cost_usd alone is NOT enough.
    assert us.cost_text({"cost_complete": True, "estimated_cost_usd": 4.0, "known_additive_cost_usd": 4.0}) == "$4.00"
    # priced subtotal present but not complete → partial with ~.
    assert us.cost_text({"cost_complete": False, "estimated_cost_usd": 4.0, "known_additive_cost_usd": 4.0}) == "~$4.00"
    # nothing priced → em-dash.
    assert us.cost_text({"cost_complete": False, "estimated_cost_usd": None, "known_additive_cost_usd": None}) == "—"
    # non-finite degrades to em-dash (never $nan).
    assert us.cost_text({"cost_complete": True, "estimated_cost_usd": float("nan"), "known_additive_cost_usd": float("inf")}) == "—"


def test_usage_bar_clamps_and_sizes():
    assert us.usage_bar(0) == "░" * 20
    assert us.usage_bar(100) == "█" * 20
    assert us.usage_bar(50, width=10) == "█" * 5 + "░" * 5
    # out-of-range clamps rather than overflows/underflows.
    assert us.usage_bar(150) == "█" * 20
    assert us.usage_bar(-10) == "░" * 20


def test_humanize_seconds():
    assert us.humanize_seconds(0) == "<1m"
    assert us.humanize_seconds(59) == "<1m"
    assert us.humanize_seconds(90) == "1m"
    assert us.humanize_seconds(3660) == "1h 1m"
    assert us.humanize_seconds(2 * 86400 + 3 * 3600) == "2d 3h"
    assert us.humanize_seconds(-100) == "<1m"  # negative floors to 0


def test_window_label():
    assert us.window_label({"kind": "5h"}) == "5-hour"
    assert us.window_label({"kind": "7d"}) == "7-day"
    assert us.window_label({"kind": "other", "window_minutes": 120}) == "120m"
    assert us.window_label({"kind": "other"}) == "window"
    # non-finite window_minutes must not reach int() (would raise) → "window".
    assert us.window_label({"kind": "other", "window_minutes": float("inf")}) == "window"
    assert us.window_label({"kind": "other", "window_minutes": float("nan")}) == "window"


# ---------------------------------------------------------------------------
# build_usage_snapshot — windows, cost honesty, freshness, filters
# ---------------------------------------------------------------------------


def _seed_windows_store(service: SentinelService) -> None:
    # today, 3d, 20d, 200d, and a FUTURE row — one distinct session each.
    _record_usage(service, client="claude-code", model="claude-opus-4-8", session_id="s-today",
                  input_tokens=100, output_tokens=10, updated_at=int(_NOW - 3600), estimated_cost_usd=1.0)
    _record_usage(service, client="claude-code", model="claude-opus-4-8", session_id="s-3d",
                  input_tokens=200, output_tokens=20, updated_at=int(_NOW - 3 * 86400), estimated_cost_usd=2.0)
    _record_usage(service, client="codex", model="gpt-5", session_id="s-20d",
                  input_tokens=300, output_tokens=30, updated_at=int(_NOW - 20 * 86400), estimated_cost_usd=3.0)
    _record_usage(service, client="codex", model="gpt-5", session_id="s-200d",
                  input_tokens=400, output_tokens=40, updated_at=int(_NOW - 200 * 86400), estimated_cost_usd=4.0)
    _record_usage(service, client="codex", model="gpt-5", session_id="s-future",
                  input_tokens=500, output_tokens=50, updated_at=int(_NOW + 5 * 86400), estimated_cost_usd=5.0)


def _sessions(window: us.UsageWindow) -> int:
    return int(window.totals.get("sessions") or 0)


def test_usage_snapshot_calendar_windows(tmp_path):
    service = SentinelService(tmp_path)
    _seed_windows_store(service)
    snap = us.build_usage_snapshot(service.list_all_events(), now=_NOW, today=_TODAY)

    by_label = {w.label: w for w in snap.windows}
    # today: only the today row. 7d: today + 3d. 30d: + 20d.
    assert _sessions(by_label["today"]) == 1
    assert _sessions(by_label["last 7 days"]) == 2
    assert _sessions(by_label["last 30 days"]) == 3
    # all time keeps every valid-dated row INCLUDING the future one (a bounded
    # window would drop future; all-time has no upper bound) → all 5 sessions.
    assert _sessions(by_label["all time"]) == 5

    # token totals are monotonic across the trailing windows.
    tok = lambda w: int(by_label[w].totals.get("total_tokens_including_cached") or 0)
    assert tok("today") < tok("last 7 days") < tok("last 30 days")

    assert snap.event_count == 5
    assert snap.usage_record_count == 5


def test_usage_snapshot_as_of_excludes_future(tmp_path):
    service = SentinelService(tmp_path)
    _seed_windows_store(service)
    snap = us.build_usage_snapshot(service.list_all_events(), now=_NOW, today=_TODAY)
    # freshness = newest real (<= now) row; the future row must not win.
    assert snap.as_of is not None
    assert snap.as_of <= _NOW
    assert snap.as_of == float(int(_NOW - 3600))


def test_usage_snapshot_client_filter(tmp_path):
    service = SentinelService(tmp_path)
    _seed_windows_store(service)
    snap = us.build_usage_snapshot(service.list_all_events(), client="codex", now=_NOW, today=_TODAY)
    assert snap.client_filter == "codex"
    # only codex rows counted (20d + 200d + future = 3 in all-time; 0 in 7d).
    by_label = {w.label: w for w in snap.windows}
    assert _sessions(by_label["all time"]) == 3
    assert _sessions(by_label["last 7 days"]) == 0
    assert all(row["client"] == "codex" for row in snap.by_client)


def test_usage_snapshot_cost_complete_vs_partial(tmp_path):
    service = SentinelService(tmp_path)
    # one priced + one UNPRICED row on the same day → the day's cost is not complete.
    _record_usage(service, client="codex", model="gpt-5", session_id="priced",
                  input_tokens=100, output_tokens=10, updated_at=int(_NOW - 3600), estimated_cost_usd=1.5)
    _record_usage(service, client="codex", model="gpt-5", session_id="unpriced",
                  input_tokens=100, output_tokens=10, updated_at=int(_NOW - 3600), estimated_cost_usd=None)
    snap = us.build_usage_snapshot(service.list_all_events(), now=_NOW, today=_TODAY)
    today = {w.label: w for w in snap.windows}["today"].totals
    assert today["cost_complete"] is False
    assert us.cost_text(today) == "~$1.50"  # partial: only the priced subtotal


def test_usage_snapshot_all_priced_is_complete(tmp_path):
    service = SentinelService(tmp_path)
    _record_usage(service, client="codex", model="gpt-5", session_id="p1",
                  input_tokens=100, output_tokens=10, updated_at=int(_NOW - 3600), estimated_cost_usd=1.5)
    _record_usage(service, client="codex", model="gpt-5", session_id="p2",
                  input_tokens=100, output_tokens=10, updated_at=int(_NOW - 3600), estimated_cost_usd=2.5)
    snap = us.build_usage_snapshot(service.list_all_events(), now=_NOW, today=_TODAY)
    today = {w.label: w for w in snap.windows}["today"].totals
    assert today["cost_complete"] is True
    assert us.cost_text(today) == "$4.00"


def test_usage_snapshot_empty_store(tmp_path):
    service = SentinelService(tmp_path)
    snap = us.build_usage_snapshot(service.list_all_events(), now=_NOW, today=_TODAY)
    assert snap.usage_record_count == 0
    assert snap.as_of is None
    assert [w.label for w in snap.windows] == ["today", "last 7 days", "last 30 days", "all time"]
    assert snap.by_client == []
    assert snap.by_model == []


def test_usage_snapshot_breakdown_window_alias(tmp_path):
    service = SentinelService(tmp_path)
    _seed_windows_store(service)
    events = service.list_all_events()
    assert us.build_usage_snapshot(events, breakdown_window="24h", now=_NOW, today=_TODAY).breakdown_window == "today"
    assert us.build_usage_snapshot(events, breakdown_window="7d", now=_NOW, today=_TODAY).breakdown_window == "last 7 days"
    assert us.build_usage_snapshot(events, breakdown_window="all", now=_NOW, today=_TODAY).breakdown_window == "all time"


def test_usage_snapshot_bad_breakdown_window_raises():
    import pytest

    with pytest.raises(ValueError):
        us.build_usage_snapshot([], breakdown_window="5m")


# ---------------------------------------------------------------------------
# rate-limit selection & parsing
# ---------------------------------------------------------------------------


def _codex_7d(used: float, *, captured: float, resets_at=42):
    return _rl_event(
        client="codex", run_id="codex_rate_limit", captured_at=captured, plan_type="pro",
        windows=[{"kind": "7d", "used_percent": used, "window_minutes": 10080, "resets_at": resets_at}],
    )


def _claude_desktop(*, org: str, fh: float, sd: float, captured: float):
    return _rl_event(
        client="claude-code", org=org, run_id=f"claude_plan_usage_{org}",
        origin="claude_plan_usage", captured_at=captured,
        windows=[
            {"kind": "5h", "used_percent": fh, "window_minutes": 300, "resets_at": None},
            {"kind": "7d", "used_percent": sd, "window_minutes": 10080, "resets_at": None},
        ],
    )


def test_latest_limit_events_picks_newest_per_stream():
    events = [_codex_7d(40.0, captured=1000.0), _codex_7d(55.0, captured=2000.0)]
    latest = us.latest_limit_events(events)
    assert len(latest) == 1  # one codex stream
    assert latest[0]["metadata"]["captured_at"] == 2000.0  # the newer reading wins


def test_latest_limit_events_client_filter():
    events = [_codex_7d(55.0, captured=2000.0), _claude_desktop(org="abc", fh=10.0, sd=20.0, captured=1500.0)]
    assert {e["metadata"]["client"] for e in us.latest_limit_events(events)} == {"codex", "claude-code"}
    assert {e["metadata"]["client"] for e in us.latest_limit_events(events, client="codex")} == {"codex"}
    assert {e["metadata"]["client"] for e in us.latest_limit_events(events, client="claude-code")} == {"claude-code"}


def test_build_client_limits_parses_windows():
    events = [_codex_7d(55.0, captured=2000.0), _claude_desktop(org="abc", fh=10.0, sd=20.0, captured=1500.0)]
    by_client = {c.client: c for c in us.build_client_limits(events)}

    codex = by_client["codex"]
    assert codex.plan_type == "pro"
    assert len(codex.windows) == 1
    assert codex.windows[0].kind == "7d"
    assert codex.windows[0].label == "7-day"
    assert codex.windows[0].used_percent == 55.0
    assert codex.windows[0].resets_at == 42

    claude = by_client["claude-code"]
    assert claude.origin == "claude_plan_usage"
    assert claude.origin_label == "desktop app"
    assert [w.kind for w in claude.windows] == ["5h", "7d"]
    assert claude.windows[0].used_percent == 10.0
    assert claude.windows[0].resets_at is None


def test_build_client_limits_survives_malformed_windows():
    ev = _rl_event(
        client="codex", run_id="codex_rate_limit", captured_at=3000.0,
        windows=[
            "not-a-mapping",
            # non-finite used_percent AND window_minutes on one window.
            {"kind": "5h", "used_percent": "bad", "window_minutes": float("inf")},
            # non-finite resets_at (would crash a bare int()); finite window_minutes kept.
            {"kind": "7d", "used_percent": float("inf"), "window_minutes": 10080, "resets_at": float("nan")},
        ],
    )
    limits = us.build_client_limits([ev])
    assert len(limits) == 1
    windows = limits[0].windows
    assert len(windows) == 2  # the non-mapping entry is skipped
    assert all(w.used_percent is None for w in windows)  # bad + inf → None, no crash
    # non-finite window_minutes / resets_at degrade to None instead of crashing int().
    assert windows[0].window_minutes is None  # inf
    assert windows[1].window_minutes == 10080
    assert windows[1].resets_at is None  # nan


def test_limit_json_entry_and_parity():
    ev = _codex_7d(40.0, captured=1000.0)
    entry = us.limit_json_entry(ev)
    assert entry == {
        "client": "codex",
        "origin": None,
        "plan_type": "pro",
        "org": None,
        "captured_at": 1000.0,
        "windows": [{"kind": "7d", "used_percent": 40.0, "window_minutes": 10080, "resets_at": 42}],
        "credits": None,
        "reached_type": None,
        "source_file": None,
    }
    # the typed object serializes back to the byte-stable JSON shape.
    assert us.build_client_limits([ev])[0].to_json_entry() == entry


def test_limit_teaser_lines():
    events = [_codex_7d(52.4, captured=2000.0), _claude_desktop(org="abc", fh=12.0, sd=26.0, captured=1500.0)]
    lines = us.limit_teaser_lines(events)
    assert "codex: 7d 52%" in lines
    assert "claude-code: 5h 12% · 7d 26%" in lines


# ---------------------------------------------------------------------------
# build_live_snapshot — the TUI's one-scan bundle
# ---------------------------------------------------------------------------


def test_build_live_snapshot_bundles_usage_and_limits(tmp_path):
    import agentacct.rate_limits as rl

    service = SentinelService(tmp_path)
    _record_usage(service, client="codex", model="gpt-5", session_id="s1",
                  input_tokens=100, output_tokens=10, updated_at=int(_NOW - 3600), estimated_cost_usd=1.0)
    service.record_event(
        rl.snapshot_to_event(
            rl.normalize_codex_rate_limits(
                {"primary": {"used_percent": 52.0, "window_minutes": 10080, "resets_at": 42}, "plan_type": "pro"},
                captured_at=_NOW - 100,
            )
        )
    )
    live = us.build_live_snapshot(service.list_all_events(), now=_NOW, today=_TODAY)
    assert isinstance(live, us.LiveSnapshot)
    assert live.usage.usage_record_count == 1
    assert [c.client for c in live.limits] == ["codex"]
    assert live.limits[0].windows[0].used_percent == 52.0


# ---------------------------------------------------------------------------
# ratio_bar + build_usage_page (the dedicated usage screen's data path)
# ---------------------------------------------------------------------------


def test_ratio_bar():
    assert us.ratio_bar(0, 0) == "░" * 20  # zero max → empty, never a full bar
    assert us.ratio_bar(10, 10) == "█" * 20
    assert us.ratio_bar(5, 10, width=10) == "█" * 5 + "░" * 5
    assert us.ratio_bar(-3, 10) == "░" * 20  # non-positive value → empty
    assert us.ratio_bar(10, 0) == "░" * 20  # zero denominator → empty
    assert us.ratio_bar(20, 10) == "█" * 20  # value > max clamps to full
    # non-finite inputs must never reach the arithmetic (would raise/overflow).
    assert us.ratio_bar(float("inf"), 10) == "░" * 20
    assert us.ratio_bar(5, float("nan")) == "░" * 20


def test_build_usage_page_periods_models_and_granularity(tmp_path):
    service = SentinelService(tmp_path)
    _seed_windows_store(service)  # today / 3d / 20d / 200d / future rows
    events = service.list_all_events()

    page = us.build_usage_page(events, days=30, now=_NOW, today=_TODAY)
    assert page.range_days == 30
    assert page.granularity == "daily"
    # 30 daily buckets, gap-filled (a gap is information), newest day present.
    assert len(page.by_period) == 30
    periods = {p["period"]: p for p in page.by_period}
    assert _TODAY.isoformat() in periods
    assert int(periods[_TODAY.isoformat()]["total_tokens_including_cached"]) > 0
    # by_model has the two in-range models (claude-opus within 7d, gpt-5 at 20d).
    models = {m["model"] for m in page.by_model}
    assert {"claude-opus-4-8", "gpt-5"} <= models
    assert int(page.totals["total_tokens_including_cached"]) > 0
    # freshness: newest real (not-future) row → finite, not the future row.
    assert page.as_of is not None and page.as_of <= _NOW
    assert page.usage_record_count == 5


def test_build_usage_page_weekly_for_long_ranges(tmp_path):
    service = SentinelService(tmp_path)
    _seed_windows_store(service)
    events = service.list_all_events()
    assert us.build_usage_page(events, days=7, now=_NOW, today=_TODAY).granularity == "daily"
    assert us.build_usage_page(events, days=90, now=_NOW, today=_TODAY).granularity == "weekly"
    # all time (days=None) also collapses to weekly buckets.
    assert us.build_usage_page(events, days=None, now=_NOW, today=_TODAY).granularity == "weekly"


def test_build_usage_page_client_filter(tmp_path):
    service = SentinelService(tmp_path)
    _seed_windows_store(service)
    events = service.list_all_events()
    page = us.build_usage_page(events, client="codex", days=None, now=_NOW, today=_TODAY)
    assert page.client_filter == "codex"
    # only codex rows counted → no claude model in the detail.
    assert all(m["client"] == "codex" for m in page.by_model)


def test_build_usage_page_empty_store(tmp_path):
    service = SentinelService(tmp_path)
    page = us.build_usage_page(service.list_all_events(), days=30, now=_NOW, today=_TODAY)
    # a truly empty result stays empty (the screen renders an explicit empty
    # state instead of a wall of zero rows).
    assert page.by_period == []
    assert page.by_model == []
    assert page.usage_record_count == 0
    assert page.as_of is None


# ---------------------------------------------------------------------------
# limit_is_stale + build_usage_page model filter
# ---------------------------------------------------------------------------


def _limit(captured_age_days, windows_minutes, now):
    wins = [us.LimitWindow(kind="w", label="w", used_percent=50.0, window_minutes=m, resets_at=None)
            for m in windows_minutes]
    captured = None if captured_age_days is None else now - captured_age_days * 86400
    return us.ClientLimit(client="c", origin=None, origin_label=None, plan_type=None, org=None,
                          captured_at=captured, windows=wins, credits=None, reached_type=None,
                          source_file=None, raw_event={})


def test_limit_is_stale():
    now = 1_000_000.0
    # older than the (7d) horizon → stale (e.g. a signed-out account).
    assert us.limit_is_stale(_limit(8, [300, 10080], now), now) is True
    # within the week → fresh.
    assert us.limit_is_stale(_limit(2, [10080], now), now) is False
    # a 5h-ONLY stream read 6h ago must NOT be hidden: the one-week floor keeps a
    # live-but-idle account visible rather than implying it signed out.
    assert us.limit_is_stale(_limit(0.25, [300], now), now) is False
    # …but a 5h-only stream abandoned for over a week IS stale.
    assert us.limit_is_stale(_limit(9, [300], now), now) is True
    # unknown capture time → never stale (can't judge; don't drop a live account).
    assert us.limit_is_stale(_limit(None, [300], now), now) is False
    # no usable window length → the week floor: 3d fresh, 9d stale.
    assert us.limit_is_stale(_limit(3, [], now), now) is False
    assert us.limit_is_stale(_limit(9, [], now), now) is True


def test_build_usage_page_model_filter(tmp_path):
    service = SentinelService(tmp_path)
    _record_usage(service, client="codex", model="gpt-5", session_id="a",
                  input_tokens=100, output_tokens=10, updated_at=int(_NOW - 3600), estimated_cost_usd=1.0)
    _record_usage(service, client="codex", model="gpt-4", session_id="b",
                  input_tokens=200, output_tokens=20, updated_at=int(_NOW - 3600), estimated_cost_usd=2.0)
    page = us.build_usage_page(service.list_all_events(), model="gpt-5", days=30, now=_NOW, today=_TODAY)
    assert page.model_filter == "gpt-5"
    assert {m["model"] for m in page.by_model} == {"gpt-5"}  # scoped to the one model
