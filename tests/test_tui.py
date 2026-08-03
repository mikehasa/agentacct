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
