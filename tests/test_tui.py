"""Headless tests for the `agentacct tui` Textual app.

Driven with Textual's ``App.run_test()`` (no real terminal). The suite has no
pytest-asyncio, so each scenario is a coroutine run via ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from typer.testing import CliRunner

from agentacct.cli import app as cli_app
from agentacct.client_usage import ClientUsageEvent
from agentacct.service import SentinelService
import agentacct.rate_limits as rl
from agentacct.tui import AgentAcctTUI
from agentacct.usage_snapshot import (
    ClientLimit,
    LimitWindow,
    LiveSnapshot,
    UsageSnapshot,
)

from textual.widgets import DataTable


def _run(coro) -> None:
    asyncio.run(coro)


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


def _seed_codex_limit(service: SentinelService, *, used: float, resets_at: int, captured: float) -> None:
    service.record_event(
        rl.snapshot_to_event(
            rl.normalize_codex_rate_limits(
                {"primary": {"used_percent": used, "window_minutes": 10080, "resets_at": resets_at}, "plan_type": "pro"},
                captured_at=captured,
            )
        )
    )


def test_tui_mounts_and_populates(tmp_path):
    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(service, client="claude-code", model="claude-opus-4-8", session_id="s1",
                  input_tokens=1000, output_tokens=200, updated_at=int(now - 3600), estimated_cost_usd=3.0)
    _record_usage(service, client="codex", model="gpt-5", session_id="s2",
                  input_tokens=500, output_tokens=100, updated_at=int(now - 2 * 3600), estimated_cost_usd=1.0)
    _seed_codex_limit(service, used=52.0, resets_at=int(now + 7200), captured=now - 60)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._snapshot is not None
            # four calendar windows are always present.
            assert app.query_one("#windows", DataTable).row_count == 4
            # both clients seeded → by-client rows.
            assert app.query_one("#byclient", DataTable).row_count >= 2
            assert app.query_one("#bymodel", DataTable).row_count >= 2
            # limits panel shows the codex reading with a live reset countdown.
            assert "codex" in app._limits_text
            assert "resets in" in app._limits_text
            assert "usage records" in app._status_text

    _run(scenario())


def test_tui_empty_store(tmp_path):
    SentinelService(tmp_path)  # create an empty store

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._snapshot is not None
            assert app._snapshot.usage.usage_record_count == 0
            # windows still render (all zero), by-client/by-model empty.
            assert app.query_one("#windows", DataTable).row_count == 4
            assert app.query_one("#byclient", DataTable).row_count == 0
            assert "No provider limit data" in app._limits_text
            assert "0 usage records" in app._status_text

    _run(scenario())


def test_tui_refresh_picks_up_new_events(tmp_path):
    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(service, client="codex", model="gpt-5", session_id="s1",
                  input_tokens=100, output_tokens=10, updated_at=int(now - 3600), estimated_cost_usd=0.5)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._snapshot.usage.usage_record_count == 1
            # a new session appears in the store, then the user hits `r`.
            _record_usage(service, client="codex", model="gpt-5", session_id="s2",
                          input_tokens=200, output_tokens=20, updated_at=int(now - 1800), estimated_cost_usd=0.7)
            await pilot.press("r")
            await pilot.pause()
            assert app._snapshot.usage.usage_record_count == 2

    _run(scenario())


def test_tui_cycle_window(tmp_path):
    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(service, client="codex", model="gpt-5", session_id="s1",
                  input_tokens=100, output_tokens=10, updated_at=int(now - 3600), estimated_cost_usd=0.5)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, window_token="7d", refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._snapshot.usage.breakdown_window == "last 7 days"
            await pilot.press("w")  # 7d → 30d
            await pilot.pause()
            assert app.window_token == "30d"
            assert app._snapshot.usage.breakdown_window == "last 30 days"

    _run(scenario())


def test_tui_renders_none_percent_limit_without_crashing(tmp_path):
    SentinelService(tmp_path)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            # A limit window with an unparseable percentage (used_percent=None) must
            # render as an em-dash, never raise.
            app._snapshot = LiveSnapshot(
                usage=UsageSnapshot(
                    as_of=None, generated_at=time.time(), event_count=0, usage_record_count=0,
                    client_filter=None, windows=[], breakdown_window="last 7 days",
                    by_client=[], by_model=[],
                ),
                limits=[
                    ClientLimit(
                        client="codex", origin=None, origin_label=None, plan_type=None, org=None,
                        captured_at=None,
                        windows=[LimitWindow(kind="7d", label="7-day", used_percent=None, window_minutes=10080, resets_at=None)],
                        credits=None, reached_type=None, source_file=None, raw_event={},
                    )
                ],
            )
            app._render_limits()
            assert "codex" in app._limits_text
            assert "—" in app._limits_text  # None percent → em-dash, no crash

    _run(scenario())


def test_tui_refresh_picks_up_in_place_supersede(tmp_path):
    # Regression: the usage importer supersedes a growing session IN PLACE
    # (replace_events), so the event COUNT stays fixed while tokens change. A
    # count-keyed cache would freeze the live view; the fingerprint key must catch
    # it on a normal (non-forced) poll tick.
    from agentacct.client_usage import is_local_usage_import_event, local_usage_event_key

    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(service, client="codex", model="gpt-5", session_id="s1",
                  input_tokens=1000, output_tokens=200, updated_at=int(now - 3600), estimated_cost_usd=1.0)

    def _tokens(app):
        by_label = {w.label: w for w in app._snapshot.usage.windows}
        return int(by_label["all time"].totals.get("total_tokens_including_cached") or 0)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._snapshot.usage.usage_record_count == 1
            before = _tokens(app)

            # Supersede s1 in place with a higher-token reading — count stays 1.
            grown = ClientUsageEvent(
                client="codex", client_session_id="s1", source_path=Path("/tmp/codex/s1.jsonl"),
                title=None, cwd="/tmp/project", model="gpt-5",
                input_tokens=5000, output_tokens=1000, cached_input_tokens=0,
                cache_creation_input_tokens=0, cache_read_input_tokens=0,
                cache_creation_tokens_reported=True, cache_read_tokens_reported=True,
                reasoning_output_tokens=0, provider_name="codex",
                started_at=int(now - 3600), updated_at=int(now - 1800), turn_count=2,
                usage_row_lane="model:gpt-5", source_namespace_fingerprint="sha256:codex",
                input_tokens_reported=True, output_tokens_reported=True,
                reasoning_output_tokens_reported=True, total_tokens=6000, total_tokens_reported=True,
            ).to_sentinel_event()
            grown["estimated_cost_usd"] = 6.0
            grown["cost_confidence"] = "estimated_from_tokens"
            service.replace_events(
                lambda e: is_local_usage_import_event(e) and local_usage_event_key(e) == ("codex", "s1"),
                [grown],
                trusted_usage_import=True,
            )
            assert len(service.list_all_events()) == 1  # count unchanged

            # The exact non-forced call the auto-refresh timer makes.
            app.refresh_data()
            await pilot.pause()
            assert app._snapshot.usage.usage_record_count == 1
            assert _tokens(app) > before  # picked up the in-place growth, not stale

    _run(scenario())


def test_tui_survives_markup_in_provider_limit_data(tmp_path):
    # A provider field containing a Rich-markup closing tag (e.g. plan_type='pro[/]')
    # must not crash the live view (it is escaped before rendering).
    service = SentinelService(tmp_path)
    now = time.time()
    service.record_event({
        "source": "t", "event_type": "rate_limit_observed", "run_id": "r1",
        "provider": None, "model": None, "estimated_input_tokens": None,
        "estimated_output_tokens": None, "estimated_cost_usd": None,
        "usage_confidence": None, "cost_confidence": None,
        "metadata": {
            "client": "codex", "origin": None, "captured_at": now - 60,
            "windows": [{"kind": "7d", "used_percent": 52.0, "window_minutes": 10080, "resets_at": int(now + 7200)}],
            "plan_type": "pro[/]", "credits": {"has_credits": True, "balance": "9[/]"},
            "reached_type": "usage[/]", "org": "[/]abcdef", "source_file": None,
        },
    })

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._snapshot is not None  # did not crash on mount
            assert "codex" in app._limits_text

    _run(scenario())


def test_tui_survives_markup_in_model_name(tmp_path):
    # A model name with a dangling closing tag would crash Textual's DataTable
    # cell formatter unless escaped.
    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(service, client="codex", model="gpt[/]5", session_id="s1",
                  input_tokens=100, output_tokens=10, updated_at=int(now - 3600), estimated_cost_usd=0.5)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            # by_model row present and the app is alive (no MarkupError from add_row).
            assert app.query_one("#bymodel", DataTable).row_count == 1

    _run(scenario())


def test_events_fingerprint_is_total_and_change_sensitive():
    # Regression: the fingerprint runs on refresh_data's unguarded path, so it must
    # never raise — a corrupted/hand-injected ledger row can round-trip event_id /
    # created_at as an (unhashable) JSON list/object.
    from agentacct.tui import _events_fingerprint

    events = [
        {"event_id": "e1", "created_at": 1.0},
        {"event_id": "e2", "created_at": [1, 2]},   # unhashable created_at
        {"event_id": ["x"], "created_at": 3.0},     # unhashable event_id
        {},                                          # missing keys
    ]
    fp = _events_fingerprint(events)  # must not raise
    assert isinstance(fp, int)
    # still change-sensitive: any value change flips the key.
    changed = [{"event_id": "e1", "created_at": 2.0}, *events[1:]]
    assert _events_fingerprint(changed) != fp


def test_tui_command_requires_tty(tmp_path):
    # Under CliRunner stdout is not a TTY, so the command must fail fast with a
    # clear pointer instead of launching a blocking full-screen app.
    result = CliRunner().invoke(cli_app, ["tui", "--store-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "interactive terminal" in result.output


def test_tui_command_invalid_window(tmp_path):
    result = CliRunner().invoke(cli_app, ["tui", "--store-dir", str(tmp_path), "--window", "5m"])
    assert result.exit_code != 0
