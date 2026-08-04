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
from agentacct.tui import AgentAcctTUI, SessionsScreen, SessionDetailScreen
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


def _seed_calibrated_claude(service: SentinelService, *, now: float) -> None:
    """Recent history where the account's weekly-% moves exactly as the baseline
    predicts (fitted scale ~1.0, trusted) so claude-code calibrates: 4 clean hourly
    intervals of 100M Opus tokens, all within the recency window."""
    from agentacct.plan_cost import BASELINE_MODEL_WEIGHTS

    move = 100.0 * BASELINE_MODEL_WEIGHTS["claude-opus-4-8"]  # 100M Opus → baseline move
    base = now - 6 * 3600
    pct = 0.0
    _record_7d(service, captured=base, pct=pct, index=0)
    for i in range(4):
        _record_usage(service, client="claude-code", model="claude-opus-4-8", session_id=f"s{i}",
                      input_tokens=100_000_000, output_tokens=0, updated_at=int(base + i * 3600 + 1800),
                      estimated_cost_usd=1.0)
        pct += move
        _record_7d(service, captured=base + (i + 1) * 3600, pct=pct, index=i + 1)


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


def test_tui_refresh_updates_visible_timestamp(tmp_path):
    # Pressing `r` gives visible feedback even when the data is unchanged: the
    # status line's "refreshed HH:MM:SS" advances because `r` forces a rebuild.
    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(service, client="codex", model="gpt-5", session_id="s1",
                  input_tokens=100, output_tokens=10, updated_at=int(now - 3600), estimated_cost_usd=0.5)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._last_refresh_at is not None
            assert "refreshed" in app._status_text
            before = app._last_refresh_at
            await pilot.press("r")  # force a rebuild even though nothing changed
            await pilot.pause()
            assert app._last_refresh_at >= before

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


def _record_section(service, *, section_id, title, status, client, session_id, created_at, summary=None):
    service.record_event({
        "event_id": f"evt_sec_{section_id}_{status}",
        "created_at": created_at,
        "source": client,
        "event_type": f"section_{status}",
        "run_id": None,
        "metadata": {
            "sentinel_semantic_kind": "section",
            "client": client,
            "client_session_id": session_id,
            "client_context_keys_authored": ["client_session_id"],
            "project_dir": "/tmp/project",
            "section_id": section_id,
            "section_status": status,
            "section_title": title,
            "summary": summary or "",
            "kind": "implementation",
        },
    })


def _record_check(service, *, section_id, client, result, created_at):
    service.record_event({
        "event_id": f"evt_chk_{section_id}_{result}",
        "created_at": created_at,
        "source": client,
        "event_type": "machine_check",
        "metadata": {
            "sentinel_semantic_kind": "evidence",
            "section_id": section_id,
            "evidence_type": "test",
            "result": result,
            "summary": "Tests passed.",
            "command": "pytest",
            "exit_code": 0,
            "client": client,
        },
    })


def test_tui_work_helpers():
    from agentacct.tui import _work_status_color, _evidence_mark, _humanize_ago, _session_matches

    assert _work_status_color("completed") == "green"
    assert _work_status_color("handed_off") == "green"
    assert _work_status_color("started") == "yellow"
    assert _work_status_color("blocked") == "red"
    assert _work_status_color("weird") == "dim"
    assert _evidence_mark("passed")[0] == "✓"
    assert _evidence_mark("failed")[0] == "✗"
    assert _evidence_mark("skipped")[0] == "»"
    now = 1000.0
    assert _humanize_ago(now - 60, now).endswith("ago")
    assert _humanize_ago(None, now) == "—"
    assert _humanize_ago(now + 100, now) == "—"  # future never shows a bogus age
    assert _session_matches({"client": "codex", "client_session_id": "s1"}, "codex", "s1")
    assert not _session_matches({"client": "codex", "client_session_id": "s2"}, "codex", "s1")


def test_session_detail_step_render_and_markup_safe():
    from rich.text import Text

    now = time.time()
    good = {
        "title": "Ship it", "latest_status": "completed", "kind": "testing",
        "updated_at": now - 120, "summary": "done",
        "evidence_events": [{"result": "passed", "evidence_type": "test", "summary": "all green", "exit_code": 0}],
    }
    screen = SessionDetailScreen({})
    title_line, content = screen._step_render(good, now)
    assert "Ship it" in title_line and "✓" in title_line  # status glyph in the collapsible title
    assert "all green" in content and "exit 0" in content
    Text.from_markup(title_line)  # valid markup
    Text.from_markup(content)

    # markup injection in any data field must yield VALID markup (escaped), never crash.
    evil = {
        "title": "pwn[/]", "latest_status": "blocked", "kind": "x[/]", "updated_at": now,
        "summary": "[/]bad", "blocker": "[/]boom",
        "evidence_events": [{"result": "failed", "evidence_type": "y[/]", "summary": "[/]z", "exit_code": 1}],
    }
    t2, c2 = screen._step_render(evil, now)
    Text.from_markup(t2)  # raises MarkupError if a field were left unescaped
    Text.from_markup(c2)
    assert "pwn" in t2 and "boom" in c2


def test_sessions_screen_folds_children(tmp_path):
    # Child sessions (related.parent present) must be folded out of the top-level
    # list and indexed under their parent for the detail.
    SentinelService(tmp_path)  # empty store so the worker builds an empty ledger fast

    # Folding keys on the ledger's authoritative rollup_group_key (a child's ==
    # "{client}::{parent}"; a root's == its own session_key).
    fake_ledger = {"session_rollup": {"sessions": [
        {"client": "cc", "client_session_id": "P", "session_key": "cc::P", "rollup_group_key": "cc::P",
         "client_session_title": "Parent",
         "usage": {"total_tokens": 100}, "work": {"counts": {"total": 1, "completed": 1}}},
        {"client": "cc", "client_session_id": "P:agent-1", "session_key": "cc::P:agent-1", "rollup_group_key": "cc::P",
         "session_kind": "child",
         "usage": {"total_tokens": 9000}, "work": {"counts": {}}},
    ]}}

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("s")
            await app.workers.wait_for_complete()
            await pilot.pause()
            scr = app.screen
            scr._populate(fake_ledger)  # inject a parent+child ledger
            table = scr.query_one("#sessions", DataTable)
            assert table.row_count == 1  # only the parent is top-level
            assert "cc::P" in app._children_by_parent  # keyed by parent session_key
            assert len(app._children_by_parent["cc::P"]) == 1

    _run(scenario())


def test_sessions_screen_folding_multilevel_and_cycle_safe(tmp_path):
    # Multi-level folding (grandchildren fold under their top-most root too) +
    # cycle safety (a mutual parent cycle folds neither side, so nothing vanishes).
    SentinelService(tmp_path)

    def s(csid, group):
        return {"client": "cc", "client_session_id": csid, "session_key": f"cc::{csid}",
                "rollup_group_key": f"cc::{group}", "usage": {}, "work": {"counts": {}}}

    fake = {"session_rollup": {"sessions": [
        s("R", "R"),        # root
        s("C", "R"),        # child of root R      → folds under R
        s("G", "C"),        # grandchild (parent C) → also folds under R (top-most)
        s("A", "B"),        # cycle A<->B
        s("B", "A"),
    ]}}

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("s")
            await app.workers.wait_for_complete()
            await pilot.pause()
            scr = app.screen
            scr._populate(fake)
            top = set(scr._by_key.keys())
            assert scr.query_one("#sessions", DataTable).row_count == 3  # R, A, B
            assert "cc::R" in top
            assert "cc::C" not in top and "cc::G" not in top  # both fold under root R
            assert "cc::A" in top and "cc::B" in top  # cycle: neither vanishes
            assert {c["client_session_id"] for c in app._children_by_parent.get("cc::R", [])} == {"C", "G"}

    _run(scenario())


def test_subagents_render_reads_roles(tmp_path):
    # 3b: the detail's Subagents section reads each child's role/task on the fly
    # from its transcript on disk.
    import json as _json

    root = tmp_path / "projects"
    directory = root / "proj" / "P" / "subagents"
    directory.mkdir(parents=True)
    (directory / "agent-1.jsonl").write_text(
        _json.dumps({"attributionAgent": "Explore", "type": "assistant"}) + "\n"
        + _json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "Map the data layer"}]}}) + "\n",
        encoding="utf-8",
    )
    entry = {"client": "cc", "client_session_id": "P",
             "related": {"children_usage": {"total_tokens": 9000, "estimated_cost_usd": 2.0, "priced_rows": 1}}}
    children = [{
        "client_session_id": "P:agent-1", "session_kind": "child",
        "usage": {"total_tokens": 9000, "estimated_cost_usd": 2.0, "priced_rows": 1},
        "observed_models": ["claude-fable-5"], "work": {"counts": {"total": 0}},
    }]
    screen = SessionDetailScreen(entry)
    subtitle, content = screen._subagents_render(entry, children, time.time(), projects_root=root)
    assert "Subagents (1)" in subtitle
    assert "9,000 tok" in subtitle  # aggregated child usage
    assert "Explore" in content  # role type from the transcript
    assert "Map the data layer" in content  # task from the transcript
    assert "9,000 tok" in content


def test_sessions_screen_worker_and_detail(tmp_path):
    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(service, client="codex", model="gpt-5", session_id="sess-1",
                  input_tokens=1000, output_tokens=200, updated_at=int(now - 600), estimated_cost_usd=2.0)
    _record_section(service, section_id="sec1", title="Do the work", status="started",
                    client="codex", session_id="sess-1", created_at=now - 600)
    _record_section(service, section_id="sec1", title="Do the work", status="completed",
                    client="codex", session_id="sess-1", created_at=now - 300, summary="finished")
    _record_check(service, section_id="sec1", client="codex", result="passed", created_at=now - 290)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("s")  # open the sessions drill-down
            await app.workers.wait_for_complete()  # let the ledger worker finish
            await pilot.pause()
            scr = app.screen
            assert isinstance(scr, SessionsScreen)
            table = scr.query_one("#sessions", DataTable)
            assert table.row_count >= 1
            entry = next((e for e in scr._by_key.values() if e.get("client_session_id") == "sess-1"), None)
            assert entry is not None, "seeded session should appear in the rollup"

            app.push_screen(SessionDetailScreen(entry))
            await pilot.pause()
            det = app.screen
            assert isinstance(det, SessionDetailScreen)
            assert "Do the work" in det._body_text  # the step
            assert "✓" in det._body_text  # its passing check

    _run(scenario())


def test_tui_repeat_s_does_not_stack_sessions(tmp_path):
    # Regression: the app-level `s` binding stays active on the (non-modal)
    # sessions screen, so a second `s` must NOT stack a duplicate screen or spawn
    # a second expensive build worker.
    SentinelService(tmp_path)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("s")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("s")  # `s` again while already on the sessions screen
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert sum(1 for s in app.screen_stack if isinstance(s, SessionsScreen)) == 1

    _run(scenario())


def test_sessions_screen_callbacks_guard_unmounted():
    # Regression: a build worker that finishes after its screen is dismissed calls
    # _populate/_show_error on an unmounted screen — these must no-op, not raise
    # NoMatches on query_one.
    screen = SessionsScreen()  # constructed, never mounted → is_mounted is False
    assert not screen.is_mounted
    screen._populate({"session_rollup": {"sessions": []}})  # must not raise
    screen._show_error("boom")  # must not raise


def test_sessions_screen_empty_store(tmp_path):
    SentinelService(tmp_path)  # empty store → empty (but non-crashing) sessions list

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("s")
            await app.workers.wait_for_complete()
            await pilot.pause()
            scr = app.screen
            assert isinstance(scr, SessionsScreen)
            assert scr.query_one("#sessions", DataTable).row_count == 0

    _run(scenario())


def test_tui_auto_imports_usage_on_launch(tmp_path, monkeypatch):
    # #1/#3: the TUI freshens the store from the client logs on launch, so the
    # in-flight session's usage shows even if it wasn't imported yet. Stub the
    # importer so it writes into the tmp store (no real client-log scan).
    import agentacct.cli as cli_mod

    monkeypatch.setenv("AGENTACCT_TUI_AUTO_IMPORT", "1")  # opt in (conftest pins off)
    SentinelService(tmp_path)  # empty store initially
    now = time.time()

    def fake_import(*, store_dir, client, estimate_costs=False, **kwargs):
        _record_usage(SentinelService(store_dir), client="claude-code", model="claude-opus-4-8",
                      session_id="live-1", input_tokens=1000, output_tokens=200,
                      updated_at=int(now - 60), estimated_cost_usd=3.0)
        return {}

    monkeypatch.setattr(cli_mod, "_local_usage_import_payload", fake_import)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()  # let the import worker finish
            await pilot.pause()
            assert app._snapshot is not None
            assert app._snapshot.usage.usage_record_count >= 1  # imported usage now shows

    _run(scenario())


def test_tui_serializes_concurrent_imports(tmp_path, monkeypatch):
    # Regression: pressing r during the launch import must NOT start a second,
    # overlapping import — overlapping imports corrupt the process-global pricing
    # catalog (env + cache restored on exit) and persist mis-priced rows.
    import threading
    import agentacct.cli as cli_mod

    monkeypatch.setenv("AGENTACCT_TUI_AUTO_IMPORT", "1")
    SentinelService(tmp_path)
    gate = threading.Event()
    calls = {"n": 0}

    def blocking_import(*, store_dir, client, estimate_costs=False, **kwargs):
        calls["n"] += 1
        gate.wait(timeout=5)  # hold this import "in flight"
        return {}

    monkeypatch.setattr(cli_mod, "_local_usage_import_payload", blocking_import)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()  # on_mount launches import #1 (blocks in the worker)
            for _ in range(3):
                await pilot.press("r")  # must be skipped while import #1 is in flight
                await pilot.pause()
            assert app._importing is True  # still in flight
            assert calls["n"] <= 1  # no second (overlapping) import launched
            gate.set()  # release import #1
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert calls["n"] == 1  # exactly the launch import ran

    _run(scenario())


def test_tui_auto_import_gated_off_by_default(tmp_path, monkeypatch):
    # With the gate off (the conftest default), launch must NOT call the importer
    # (so tests never scan real client logs).
    import agentacct.cli as cli_mod

    called = {"n": 0}
    monkeypatch.setattr(cli_mod, "_local_usage_import_payload", lambda **kw: called.__setitem__("n", called["n"] + 1))
    SentinelService(tmp_path)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
    _run(scenario())
    assert called["n"] == 0


def test_tui_command_requires_tty(tmp_path):
    # Under CliRunner stdout is not a TTY, so the command must fail fast with a
    # clear pointer instead of launching a blocking full-screen app.
    result = CliRunner().invoke(cli_app, ["tui", "--store-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "interactive terminal" in result.output


def test_tui_command_invalid_window(tmp_path):
    result = CliRunner().invoke(cli_app, ["tui", "--store-dir", str(tmp_path), "--window", "5m"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# #4 session status badge + shared folding helper
# ---------------------------------------------------------------------------


def test_session_status_and_badge_helpers():
    from agentacct.tui import _session_status, _session_badge
    from rich.text import Text

    # No recorded steps → no badge (usage-only session).
    assert _session_status({}) == ("·", "—", "dim")
    assert _session_status({"work": {"items": []}}) == ("·", "—", "dim")
    # Precedence: blocked > handed-off > in-progress > done.
    assert _session_status({"work": {"items": [{"latest_status": "resolved"}]}})[1] == "done"
    assert _session_status({"work": {"items": [{"latest_status": "completed"}]}})[1] == "done"
    assert _session_status(
        {"work": {"items": [{"latest_status": "handed_off"}, {"latest_status": "completed"}]}}
    )[1] == "handed off"
    # A hand-off outranks a stray still-open step: a real handed-off session often
    # leaves one section `started` that was never closed, and must still read
    # "handed off", not "in progress".
    assert _session_status(
        {"work": {"items": [{"latest_status": "started"}, {"latest_status": "handed_off"}]}}
    )[1] == "handed off"
    # Regression for the exact live shape (one stray `started` amid handed_off +
    # completed) that used to misread as "in progress".
    assert _session_status(
        {"work": {"items": [
            {"latest_status": "handed_off"}, {"latest_status": "completed"},
            {"latest_status": "started"}, {"latest_status": "completed"},
        ]}}
    )[1] == "handed off"
    # blocked still dominates everything (an alarm worth surfacing).
    assert _session_status(
        {"work": {"items": [{"latest_status": "blocked"}, {"latest_status": "handed_off"}]}}
    )[1] == "blocked"
    # a genuinely active session (open step, no hand-off) still reads in progress.
    assert _session_status(
        {"work": {"items": [{"latest_status": "started"}, {"latest_status": "completed"}]}}
    )[1] == "in progress"
    # The badge is valid Rich markup for every state (fixed vocab, never data).
    for st in ("blocked", "started", "handed_off", "completed"):
        Text.from_markup(_session_badge({"work": {"items": [{"latest_status": st}]}}))
    Text.from_markup(_session_badge({}))


def test_fold_sessions_helper_multilevel_and_cycle_safe():
    from agentacct.tui import _fold_sessions

    def s(csid, group, last=0.0):
        return {
            "client": "cc", "client_session_id": csid, "session_key": f"cc::{csid}",
            "rollup_group_key": f"cc::{group}", "last_activity_at": last,
        }

    # R root, C child of R, G grandchild (parent C) → both fold under R (top-most);
    # A<->B is a mutual cycle → neither folds, so nothing vanishes.
    sessions = [s("R", "R", 3), s("C", "R", 2), s("G", "C", 1), s("A", "B"), s("B", "A")]
    top, children = _fold_sessions(sessions)
    top_keys = {t["session_key"] for t in top}
    assert "cc::R" in top_keys and "cc::A" in top_keys and "cc::B" in top_keys
    assert "cc::C" not in top_keys and "cc::G" not in top_keys
    assert {c["client_session_id"] for c in children["cc::R"]} == {"C", "G"}
    assert top[0]["session_key"] == "cc::R"  # sorted newest-first


def test_sessions_screen_status_column(tmp_path):
    SentinelService(tmp_path)  # empty store → the worker builds an empty ledger fast
    fake = {"session_rollup": {"sessions": [
        {"client": "cc", "client_session_id": "P", "session_key": "cc::P", "rollup_group_key": "cc::P",
         "client_session_title": "Parent", "usage": {"total_tokens": 100},
         "work": {"items": [{"latest_status": "blocked"}], "counts": {"total": 1, "blocked": 1}}},
    ]}}

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("s")
            await app.workers.wait_for_complete()
            await pilot.pause()
            scr = app.screen
            scr._populate(fake)
            table = scr.query_one("#sessions", DataTable)
            assert table.row_count == 1
            # status is the 5th column (session, client, project, activity, STATUS, …).
            assert "blocked" in str(table.get_row_at(0)[4])

    _run(scenario())


def test_session_detail_header_shows_status_badge():
    from rich.text import Text

    now = time.time()
    entry = {
        "client_session_title": "Big task", "client": "codex", "project": "/p",
        "last_activity_at": now - 60, "usage": {"total_tokens": 1234},
        "work": {"items": [{"latest_status": "handed_off"}]},
    }
    header = SessionDetailScreen(entry)._render_header(entry, now)
    assert "handed off" in header
    Text.from_markup(header)  # valid markup

    # A usage-only session (no steps) shows no badge on the title line.
    entry2 = {"client_session_title": "solo", "client": "codex", "usage": {}, "work": {"items": []}}
    header2 = SessionDetailScreen(entry2)._render_header(entry2, now)
    assert "—" not in header2.split("\n")[0]
    Text.from_markup(header2)


# ---------------------------------------------------------------------------
# #5 usage screen
# ---------------------------------------------------------------------------


def test_usage_screen_renders_and_cycles_range(tmp_path):
    from agentacct.tui import UsageScreen

    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(service, client="codex", model="gpt-5", session_id="s1",
                  input_tokens=1000, output_tokens=200, updated_at=int(now - 3600), estimated_cost_usd=1.0)
    _record_usage(service, client="claude-code", model="claude-opus-4-8", session_id="s2",
                  input_tokens=500, output_tokens=50, updated_at=int(now - 2 * 86400), estimated_cost_usd=0.5)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("u")
            await pilot.pause()
            scr = app.screen
            assert isinstance(scr, UsageScreen)
            assert scr.query_one("#usage-periods", DataTable).row_count > 0
            assert scr.query_one("#usage-models", DataTable).row_count >= 2
            assert "range: 30d" in scr._status_text and "daily" in scr._status_text
            # `d` cycles 30d → 90d, which flips the granularity to weekly.
            await pilot.press("d")
            await pilot.pause()
            assert "range: 90d" in scr._status_text and "weekly" in scr._status_text

    _run(scenario())


def test_top_level_nav_works_from_any_screen(tmp_path):
    # Regression: the app-level `s`/`u` bindings stay active on sub-screens, but the
    # actions used to no-op there — so `u` did nothing on the sessions screen (you
    # had to Esc home first). They must switch between top-level views from anywhere,
    # without stacking duplicates.
    from agentacct.tui import SessionsScreen, UsageScreen

    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(service, client="claude-code", model="claude-opus-4-8", session_id="s1",
                  input_tokens=500, output_tokens=50, updated_at=int(now - 3600), estimated_cost_usd=0.5)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            assert isinstance(app.screen, SessionsScreen)
            # The bug: `u` on the sessions screen did nothing. It must switch to usage.
            await pilot.press("u")
            await pilot.pause()
            assert isinstance(app.screen, UsageScreen)
            assert len(app.screen_stack) == 2  # swapped, not stacked
            # And back, again without going home first.
            await pilot.press("s")
            await pilot.pause()
            assert isinstance(app.screen, SessionsScreen)
            assert len(app.screen_stack) == 2
            # Pressing the current screen's own key is a harmless no-op.
            await pilot.press("s")
            await pilot.pause()
            assert isinstance(app.screen, SessionsScreen)
            assert len(app.screen_stack) == 2

    _run(scenario())


def test_snapshot_key_writes_file_and_app_survives(tmp_path):
    # `p` saves an SVG under <store>/snapshots/ and must leave the live app intact
    # (export_screenshot clears the compositor's dirty regions; the action forces a
    # repaint so the terminal isn't left with stale/blank cells).
    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(service, client="claude-code", model="claude-opus-4-8", session_id="s1",
                  input_tokens=500, output_tokens=50, updated_at=int(now - 3600), estimated_cost_usd=0.5)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            assert app.is_running
            assert list((tmp_path / "snapshots").glob("*.svg"))

    _run(scenario())


def test_usage_screen_empty_store(tmp_path):
    from agentacct.tui import UsageScreen

    SentinelService(tmp_path)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(UsageScreen())
            await pilot.pause()
            scr = app.screen
            assert isinstance(scr, UsageScreen)
            assert scr.query_one("#usage-periods", DataTable).row_count == 0
            assert scr.query_one("#usage-models", DataTable).row_count == 0

    _run(scenario())


def test_usage_screen_repeat_u_does_not_stack(tmp_path):
    from agentacct.tui import UsageScreen

    SentinelService(tmp_path)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("u")
            await pilot.pause()
            await pilot.press("u")  # app-level `u` stays active under the sub-screen
            await pilot.pause()
            assert sum(1 for s in app.screen_stack if isinstance(s, UsageScreen)) == 1

    _run(scenario())


# ---------------------------------------------------------------------------
# #6 recent-sessions panel on home
# ---------------------------------------------------------------------------


def test_recent_panel_populates_on_home(tmp_path):
    service = SentinelService(tmp_path)
    now = time.time()
    _record_section(service, section_id="sec1", title="Ship the thing", status="started",
                    client="codex", session_id="s1", created_at=now - 200, summary="wip")
    _record_section(service, section_id="sec1", title="Ship the thing", status="handed_off",
                    client="codex", session_id="s1", created_at=now - 100, summary="handed off")

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()  # the recent-sessions ledger worker
            await pilot.pause()
            recent = app.query_one("#recent", DataTable)
            assert recent.row_count >= 1
            # the status column (index 2) reflects the handed-off section.
            assert "handed off" in str(recent.get_row_at(0)[2])
            assert "press s for all" in app._recent_text

    _run(scenario())


def test_recent_panel_empty_store(tmp_path):
    SentinelService(tmp_path)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one("#recent", DataTable).row_count == 0
            assert app._recent_text == "no sessions yet"

    _run(scenario())


def test_recent_panel_uses_private_cache_not_shared(tmp_path):
    # Regression (adversarial review): the home recent panel must NOT write the
    # SessionsScreen shared cache. Its build lineage (group="recent", App node) and
    # SessionsScreen._load (default group, Screen node) cannot coalesce via
    # exclusive=, so sharing _work_ledger / _children_by_parent would let the two
    # stale-clobber / tear each other. The panel keeps a private _recent_ledger.
    service = SentinelService(tmp_path)
    now = time.time()
    _record_section(service, section_id="sec1", title="t", status="completed",
                    client="codex", session_id="s1", created_at=now - 100)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one("#recent", DataTable).row_count >= 1
            # the panel populated its PRIVATE cache…
            assert app._recent_ledger is not None
            # …and left the SessionsScreen shared cache untouched (sole-writer).
            assert app._work_ledger is None
            assert app._work_ledger_fp is None
            assert app._children_by_parent == {}

    _run(scenario())


def test_home_body_is_scrollable(tmp_path):
    # Regression: the home body must be a VerticalScroll so the provider-limits and
    # recent-sessions panels below the fold are reachable on a short terminal
    # (they were clipped when #body was a plain, non-scrolling Vertical).
    from textual.containers import VerticalScroll

    SentinelService(tmp_path)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.query_one("#body"), VerticalScroll)
            # the limits + recent panels are inside it and mounted.
            assert app.query_one("#limits-body") is not None
            assert app.query_one("#recent") is not None

    _run(scenario())


# ---------------------------------------------------------------------------
# provider-limit staleness + usage-screen limits & filters
# ---------------------------------------------------------------------------


def _claude_limit(org, captured_age, used=40.0):
    from agentacct.usage_snapshot import ClientLimit, LimitWindow
    return ClientLimit(
        client="claude-code", origin="claude_plan_usage", origin_label="desktop app",
        plan_type=None, org=org, captured_at=time.time() - captured_age,
        windows=[LimitWindow(kind="7d", label="7-day", used_percent=used, window_minutes=10080, resets_at=None)],
        credits=None, reached_type=None, source_file=None, raw_event={},
    )


def test_format_limit_lines_and_panel_text():
    from agentacct.tui import _format_limit_lines, _limits_panel_text
    from rich.text import Text

    now = time.time()
    dead = _claude_limit("deadorg", 8 * 86400, used=100.0)  # 8d old → stale
    live = _claude_limit("liveorg", 600, used=35.0)          # 10m old → fresh

    text, hidden = _format_limit_lines([dead, live], now)
    assert hidden == 1
    assert "liveorg" in text and "deadorg" not in text  # the dead account is dropped
    Text.from_markup(text)  # valid markup

    panel = _limits_panel_text([dead, live], now)
    assert "1 stale reading(s) hidden" in panel  # hiding is disclosed, never silent
    # all-stale → explicit note, still valid markup.
    all_stale = _limits_panel_text([dead], now)
    assert "No active provider limit readings" in all_stale
    Text.from_markup(all_stale)
    # no data at all → the record-data hint.
    assert "No provider limit data recorded yet" in _limits_panel_text([], now)


def test_home_render_limits_hides_stale(tmp_path):
    SentinelService(tmp_path)
    from agentacct.usage_snapshot import UsageSnapshot, LiveSnapshot

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            now = time.time()
            app._snapshot = LiveSnapshot(
                usage=UsageSnapshot(
                    as_of=None, generated_at=now, event_count=0, usage_record_count=0,
                    client_filter=None, windows=[], breakdown_window="last 7 days",
                    by_client=[], by_model=[],
                ),
                limits=[_claude_limit("deadorg", 8 * 86400, used=100.0),
                        _claude_limit("liveorg", 600, used=35.0)],
            )
            app._render_limits()
            assert "liveorg" in app._limits_text and "deadorg" not in app._limits_text
            assert "1 stale reading(s) hidden" in app._limits_text

    _run(scenario())


def test_usage_screen_limits_and_filters(tmp_path):
    from agentacct.tui import UsageScreen

    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(service, client="codex", model="gpt-5", session_id="c1",
                  input_tokens=1000, output_tokens=100, updated_at=int(now - 3600), estimated_cost_usd=1.0)
    _record_usage(service, client="claude-code", model="claude-opus-4-8", session_id="a1",
                  input_tokens=500, output_tokens=50, updated_at=int(now - 3600), estimated_cost_usd=0.5)
    _record_usage(service, client="claude-code", model="claude-fable-5", session_id="a2",
                  input_tokens=300, output_tokens=30, updated_at=int(now - 3600), estimated_cost_usd=0.4)
    _seed_codex_limit(service, used=52.0, resets_at=int(now + 7200), captured=now - 60)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("u")
            await pilot.pause()
            scr = app.screen
            assert isinstance(scr, UsageScreen)
            # the fresh codex limit renders in the usage-screen Plan limits section.
            assert "codex" in scr._limits_text
            assert set(scr._available_clients) >= {"claude-code", "codex"}
            assert "client: all" in scr._status_text
            # `c` scopes to a client; `m` then scopes to one of that client's models.
            await pilot.press("c")
            await pilot.pause()
            assert scr._client_filter in ("claude-code", "codex")
            await pilot.press("m")
            await pilot.pause()
            assert scr._model_filter is not None
            assert scr.query_one("#usage-models", DataTable).row_count == 1

    _run(scenario())


def test_usage_screen_model_filter_dropped_when_absent_in_range(tmp_path):
    # Regression (review): cycling the range must not strand the model filter on a
    # model with no usage in the new range (empty page + stale "model: X" status).
    from agentacct.tui import UsageScreen

    service = SentinelService(tmp_path)
    now = time.time()
    # fable only 40d ago (present in 90d/all, absent in 7d/30d); opus is recent.
    _record_usage(service, client="claude-code", model="claude-fable-5", session_id="f1",
                  input_tokens=100, output_tokens=10, updated_at=int(now - 40 * 86400), estimated_cost_usd=0.5)
    _record_usage(service, client="claude-code", model="claude-opus-4-8", session_id="o1",
                  input_tokens=100, output_tokens=10, updated_at=int(now - 3600), estimated_cost_usd=0.5)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(UsageScreen(range_index=2))  # 90d, where fable exists
            await pilot.pause()
            scr = app.screen
            scr._client_filter = "claude-code"
            scr._model_filter = "claude-fable-5"
            scr._rebuild()
            await pilot.pause()
            assert scr._model_filter == "claude-fable-5"
            assert "claude-fable-5" in scr._available_models
            # 90d → all (fable still present) → 7d (fable absent → filter dropped).
            scr.action_cycle_range()  # all
            await pilot.pause()
            scr.action_cycle_range()  # 7d
            await pilot.pause()
            assert "claude-fable-5" not in scr._available_models
            assert scr._model_filter is None  # dropped, not stranded on an empty page

    _run(scenario())


def test_session_detail_shows_calibrated_plan_estimate(tmp_path):
    service = SentinelService(tmp_path)
    now = time.time()
    _seed_calibrated_claude(service, now=now)  # calibrates claude-code (scale ~1.0)
    entry = {"client": "claude-code", "client_session_id": "s0", "client_session_title": "Big",
             "usage": {"total_tokens": 100_000_000}, "work": {"items": []}}

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(SessionDetailScreen(entry))
            await pilot.pause()
            det = app.screen
            # Calibrated from the account's own history → a real % + the calibrated note.
            assert "of your weekly plan" in det._header_text
            assert "calibrated to your usage" in det._header_text
            assert app._plan_cache is not None  # cached on the app for reuse

    _run(scenario())


def test_session_detail_uncalibrated_shows_honest_line(tmp_path):
    service = SentinelService(tmp_path)
    now = time.time()
    # Usage but NO limit history → baseline → we must NOT fabricate a %.
    _record_usage(service, client="claude-code", model="claude-opus-4-8", session_id="cs1",
                  input_tokens=100_000_000, output_tokens=0, updated_at=int(now - 3600), estimated_cost_usd=50.0)
    entry = {"client": "claude-code", "client_session_id": "cs1", "client_session_title": "Big",
             "usage": {"total_tokens": 100_000_000}, "work": {"items": []}}

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(SessionDetailScreen(entry))
            await pilot.pause()
            det = app.screen
            assert "calibrating from your own usage" in det._header_text
            assert "% of your weekly plan" not in det._header_text  # no fabricated number

    _run(scenario())


def test_session_detail_no_plan_line_for_non_plan_client(tmp_path):
    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(service, client="opencode", model="gpt-5", session_id="oc1",
                  input_tokens=1_000_000, output_tokens=0, updated_at=int(now - 3600), estimated_cost_usd=1.0)
    entry = {"client": "opencode", "client_session_id": "oc1", "client_session_title": "OC",
             "usage": {"total_tokens": 1_000_000}, "work": {"items": []}}

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(SessionDetailScreen(entry))
            await pilot.pause()
            det = app.screen
            assert "weekly plan" not in det._header_text  # non-plan client → no line at all

    _run(scenario())


def test_plan_pct_cell_helper():
    from agentacct.tui import _plan_pct_cell
    cc = {"client": "claude-code", "client_session_id": "s1"}
    assert _plan_pct_cell(cc, {"s1": 2.5}) == "≈2.5%"
    assert _plan_pct_cell(cc, {"s1": 0.04}) == "≈<0.1%"   # tiny but non-zero
    assert _plan_pct_cell(cc, {"s1": 0.0}) == "—"          # zero → no estimate
    assert _plan_pct_cell(cc, {}) == "—"                    # missing (uncalibrated) → no estimate
    # codex is a plan-bearing client now: a calibrated value renders.
    cx = {"client": "codex", "client_session_id": "s1"}
    assert _plan_pct_cell(cx, {"s1": 2.5}) == "≈2.5%"
    # a non-plan client never shows a number, even with a value in the map.
    oc = {"client": "opencode", "client_session_id": "s1"}
    assert _plan_pct_cell(oc, {"s1": 2.5}) == "—"
    # Calibrating: a plan-bearing client not calibrated yet shows a distinct '⋯'
    # (not a bare '—'), so the column is not misread as "there is no weekly plan".
    from agentacct.tui import _PLAN_CALIBRATING_CELL
    assert _plan_pct_cell(cc, {}, {"claude-code": "baseline"}) == _PLAN_CALIBRATING_CELL
    # codex is a plan client, but its rolling meter never calibrates — so an
    # uncalibrated codex cell stays '—', not a false "calibrating" promise.
    assert _plan_pct_cell(cx, {}, {"codex": "baseline"}) == "—"
    # A calibrated client with a zero-token session still shows '—', not '⋯'.
    assert _plan_pct_cell(cc, {}, {"claude-code": "calibrated"}) == "—"
    # A non-plan client never shows the calibrating marker.
    assert _plan_pct_cell(oc, {}, {"opencode": "baseline"}) == "—"
    # Backward-compatible: without confidence info, an uncalibrated cell is '—'.
    assert _plan_pct_cell(cc, {}) == "—"


def test_recent_panel_renders_plan_column(tmp_path):
    SentinelService(tmp_path)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            claude = {"client": "claude-code", "client_session_id": "cs1",
                      "client_session_title": "C", "usage": {"total_tokens": 100}, "work": {"items": []}}
            codex = {"client": "codex", "client_session_id": "cx1",
                     "client_session_title": "X", "usage": {"total_tokens": 100}, "work": {"items": []}}
            app._populate_recent([claude, codex], {"cs1": 1.3})
            recent = app.query_one("#recent", DataTable)
            assert recent.row_count == 2
            # columns: session, client, status, tokens, PLAN(4), activity
            assert str(recent.get_row_at(0)[4]) == "≈1.3%"   # claude, in the calibrated map
            assert str(recent.get_row_at(1)[4]) == "—"        # codex not in the map → no estimate

    _run(scenario())


def test_sessions_list_renders_plan_column(tmp_path):
    SentinelService(tmp_path)
    fake = {"session_rollup": {"sessions": [
        {"client": "claude-code", "client_session_id": "cs1", "session_key": "cc::cs1",
         "rollup_group_key": "cc::cs1", "client_session_title": "C",
         "usage": {"total_tokens": 100}, "work": {"counts": {}}},
        {"client": "codex", "client_session_id": "cx1", "session_key": "cx::cx1",
         "rollup_group_key": "cx::cx1", "client_session_title": "X",
         "usage": {"total_tokens": 100}, "work": {"counts": {}}},
    ]}}

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("s")
            await app.workers.wait_for_complete()
            await pilot.pause()
            scr = app.screen
            scr._populate(fake, {"cs1": 2.7})  # only claude in the calibrated map
            table = scr.query_one("#sessions", DataTable)
            # columns: session,client,project,activity,status,tokens,est.cost,PLAN(7),steps,sub
            by_client = {str(table.get_row_at(i)[1]): table.get_row_at(i) for i in range(table.row_count)}
            assert str(by_client["claude-code"][7]) == "≈2.7%"
            assert str(by_client["codex"][7]) == "—"  # not in the map → honest —

    _run(scenario())


def test_screenshot_action_saves_svg(tmp_path):
    # `p` saves a shareable SVG under the store's snapshots/ dir (NOT the cwd, so it
    # can't litter a repo) — and it must work from a sub-screen too, where the
    # screen-level `p` binding bubbles up to the app-level action.
    SentinelService(tmp_path)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("p")  # home
            await pilot.pause()
            await pilot.press("s")  # into the sessions screen
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("p")  # sub-screen snapshot (bubbles to the app)
            await pilot.pause()

    _run(scenario())
    snaps = list((tmp_path / "snapshots").glob("*.svg"))
    assert len(snaps) >= 2, "p should save a snapshot from the home AND a sub-screen"
