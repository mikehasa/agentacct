"""Headless tests for the `agentacct tui` Textual app (the GUI-mirroring rewrite).

Driven with Textual's ``App.run_test()`` (no real terminal). The suite has no
pytest-asyncio, so each scenario is a coroutine run via ``asyncio.run``. Panes are
asserted through the app's plain-string mirror hooks (``_dashboard_text``,
``_work_detail_text``, ``_usage_text``, ``_sources_text``, ``_topbar_text``,
``_status_text``) so tests never touch Rich renderable internals; every
data-derived field must survive ``rich.text.Text.from_markup`` (the markup-safety
invariant that keeps a stray ``[/]`` from crashing the live view).
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

os.environ.setdefault("AGENTACCT_TUI_AUTO_IMPORT", "0")

from rich.text import Text  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from agentacct.cli import app as cli_app  # noqa: E402
from agentacct.client_usage import ClientUsageEvent  # noqa: E402
from agentacct.service import SentinelService  # noqa: E402
import agentacct.tui as tui  # noqa: E402
from agentacct.tui import (  # noqa: E402
    AgentAcctTUI,
    HelpScreen,
    _DARK,
    _LIGHT,
    _WORK_TABS,
)
from textual.widgets import Input, ListView  # noqa: E402


def _run(coro) -> None:
    asyncio.run(coro)


# --------------------------------------------------------------------------- #
# store seeding                                                               #
# --------------------------------------------------------------------------- #

def _record_usage(service, *, client, model, session_id, tokens, updated_at, cost, title=None, project="proj"):
    event = ClientUsageEvent(
        client=client, client_session_id=session_id,
        source_path=Path(f"/tmp/{client}/{session_id}.jsonl"), title=title, cwd=f"/tmp/{project}",
        model=model, input_tokens=tokens, output_tokens=0, cached_input_tokens=0,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
        cache_creation_tokens_reported=True, cache_read_tokens_reported=True,
        reasoning_output_tokens=0, provider_name=client, started_at=updated_at, updated_at=updated_at,
        turn_count=1, usage_row_lane=f"model:{model}", source_namespace_fingerprint=f"sha256:{client}",
        input_tokens_reported=True, output_tokens_reported=True, reasoning_output_tokens_reported=True,
        total_tokens=tokens, total_tokens_reported=True,
    ).to_sentinel_event()
    if cost is not None:
        event["estimated_cost_usd"] = cost
        event["cost_confidence"] = "estimated_from_tokens"
    service.record_event(event, trusted_usage_import=True)


def _record_section(service, *, session, section_id, title, status, at, client="claude-code", project="proj",
                    kind="implementation", summary="", blocker=None):
    service.record_event({
        "event_id": f"evt_section_{session}_{section_id}_{status}",
        "created_at": float(at), "source": client, "event_type": f"section_{status}", "run_id": None,
        "metadata": {
            "sentinel_semantic_kind": "section", "client": client, "client_session_id": session,
            "client_transcript_id": session,
            "client_context_keys_authored": ["client_session_id", "client_transcript_id"],
            "project_dir": f"/tmp/{project}", "section_id": section_id, "section_status": status,
            "section_title": title, "summary": summary, "kind": kind,
            "files": ["src/mod.py"], "blocker": blocker, "next_step": None,
        },
    })


def _record_check(service, *, session, section_id, result, at, summary="ok", command="pytest -q", exit_code=0):
    service.record_event({
        "event_id": f"evt_ev_{session}_{section_id}_{result}_{int(at)}",
        "created_at": float(at), "source": "claude-code", "event_type": "machine_check",
        "metadata": {
            "sentinel_semantic_kind": "evidence", "client": "claude-code", "client_session_id": session,
            "section_id": section_id, "evidence_type": "test", "result": result, "name": "pytest",
            "summary": summary, "command": command, "exit_code": exit_code,
        },
    })


def _record_7d(service, *, captured, pct, client="claude-code", index=0, five_hour=None):
    windows = [{"kind": "7d", "window_minutes": 10080, "used_percent": pct}]
    if five_hour is not None:
        windows.append({"kind": "5h", "window_minutes": 300, "used_percent": five_hour})
    service.record_event({
        "event_id": f"evt_rl_{client}_{index}", "created_at": float(captured), "source": client,
        "event_type": "rate_limit_observed",
        "metadata": {"client": client, "captured_at": float(captured), "windows": windows},
    })


def _seed(tmp: Path, *, now: float | None = None) -> None:
    """A store with a verified task, a blocked task (attention), a codex usage,
    and a claude-code rate-limit reading (both windows). Seeded against the REAL
    clock so capacity/"today"/staleness (all wall-clock relative) see it fresh."""

    now = now if now is not None else time.time()
    svc = SentinelService(tmp)
    base = now - 4 * 3600
    # verified: completed step + passing check recorded after it
    _record_usage(svc, client="claude-code", model="claude-opus-4-8", session_id="s-ok",
                  tokens=250_000_000, updated_at=int(base + 1800), cost=190.0, title="Add rate-limit to login")
    _record_section(svc, session="s-ok", section_id="s-ok-1", title="Add rate-limit to login",
                    status="completed", at=base + 1800)
    _record_check(svc, session="s-ok", section_id="s-ok-1", result="passed", at=base + 1900, summary="12 passed")
    # blocked: attention
    _record_usage(svc, client="claude-code", model="claude-opus-4-8", session_id="s-blocked",
                  tokens=90_000_000, updated_at=int(base + 2 * 3600), cost=70.0, title="Fix the flaky payment test")
    _record_section(svc, session="s-blocked", section_id="s-blocked-1", title="Fix the flaky payment test",
                    status="blocked", at=base + 2 * 3600, summary="hit a blocker", blocker="staging creds missing")
    # codex usage (no plan)
    _record_usage(svc, client="codex", model="gpt-5.6-sol", session_id="cx", tokens=1_400_000_000,
                  updated_at=int(now - 900), cost=6.2, title="Investigate the perf regression")
    # a claude-code rate-limit reading
    _record_7d(svc, captured=now - 120, pct=47.0, five_hour=41.0, index=1)


# --------------------------------------------------------------------------- #
# vocabulary (pure)                                                           #
# --------------------------------------------------------------------------- #

def test_vocabulary_markup_is_always_valid():
    for pal in (_DARK, _LIGHT):
        for grade in ["externally_verified", "independently_checked", "self_checked",
                      "claimed", "unchecked", "none", "made[/]up"]:
            Text.from_markup(tui.pip(grade, pal))
        for key in ["blocked", "in_progress", "reported", "handed_off", "ended_open",
                    "inactive", "verified", "finding", "mostly_done", "surprise[/]"]:
            Text.from_markup(tui.decision_badge(key, pal))
        for frac in (0.0, 0.47, 0.83, 1.0, 1.5):
            Text.from_markup(tui.meter(frac, 20, pal))
        Text.from_markup(tui.sparkline([1, 2, 3, 8, 14], pal))
        Text.from_markup(tui.sparkline([], pal))
        Text.from_markup(tui.caps("shift brief", pal))


def test_pip_shape_carries_tier():
    # Shape is the tier; colour is redundant. Each tier maps to a distinct glyph.
    assert tui.tier_style("externally_verified")[0] == "◉"
    assert tui.tier_style("independently_checked")[0] == "●"
    assert tui.tier_style("self_checked")[0] == "◐"
    assert tui.tier_style("unchecked")[0] == "○"
    assert tui.tier_style("none")[0] == "○"


def test_decision_label_and_families():
    assert tui.decision_label("in_progress") == "In progress"
    assert tui.decision_label("finding") == "Open finding"
    assert tui.decision_label("handed_off") == "Handed off"
    assert tui.decision_label("inactive") == "Inactive"
    # danger keys wear a coral wash; every decision (live included) is a filled
    # chip now, for consistent badges across the surfaces.
    coral = _DARK["coral"]
    assert coral in tui.decision_badge("blocked", _DARK)
    assert " on " in tui.decision_badge("in_progress", _DARK)
    assert " on " in tui.decision_badge("reported", _DARK)


def test_cost_grammar():
    assert tui.cost_display(4.82, complete=True, confidence="client_reported") == "$4.82"
    assert tui.cost_display(4.82, complete=True, confidence="provider_billed") == "$4.82"
    assert tui.cost_display(4.82, complete=True, confidence="estimated_from_tokens") == "≈$4.82"
    assert tui.cost_display(3.0, complete=False, confidence=None, known_additive=2.5) == "~$2.50"
    assert tui.cost_display(None, complete=False, confidence=None) is None
    assert tui.receipt_cost_text({}) == "unpriced"
    assert tui.receipt_cost_text(
        {"estimated_cost_usd": 4.82, "cost_complete": True, "cost_confidence": "client_reported"}) == "$4.82"


def test_meter_threshold_colours():
    # accent < 75% ≤ amber < 100% ≤ coral
    assert _DARK["accent"] in tui.meter(0.40, 20, _DARK)
    assert _DARK["amber"] in tui.meter(0.80, 20, _DARK)
    assert _DARK["coral"] in tui.meter(1.0, 20, _DARK)


def test_check_mark_vocabulary():
    assert tui.check_mark("passed", _DARK)[0] == "✓"
    assert tui.check_mark("failed", _DARK)[0] == "✗"
    assert tui.check_mark("error", _DARK)[0] == "✗"
    assert tui.check_mark("skipped", _DARK)[0] == "»"
    assert tui.check_mark("other", _DARK)[0] == "•"


def test_work_tabs_cover_the_lifecycle():
    ids = {tab for tab, _label in _WORK_TABS}
    assert {"all", "attention", "verified", "reported",
            "in_progress", "observed", "stopped", "other"} == ids


def test_attention_and_buckets_match_the_swift_mapping():
    from agentacct.tui import needs_attention, task_bucket
    # danger keys and any failing check escalate to Attention...
    assert needs_attention("blocked", 0)
    assert needs_attention("reported", 1)
    assert task_bucket({"decision_status": {"key": "reported"},
                        "evidence_strength": {"checks_failed": 1}}) == "attention"
    # ...unless the finding is already settled
    assert not needs_attention("finding_superseded", 1)
    # forKey buckets, verbatim from Swift WorkGroup.forKey
    assert task_bucket({"decision_status": {"key": "handed_off"}, "evidence_strength": {}}) == "stopped"
    assert task_bucket({"decision_status": {"key": "inactive"}, "evidence_strength": {}}) == "stopped"
    assert task_bucket({"decision_status": {"key": "ended_open"}, "evidence_strength": {}}) == "stopped"
    assert task_bucket({"decision_status": {"key": "finding_superseded"}, "evidence_strength": {}}) == "reported"
    assert task_bucket({"decision_status": {"key": "observed"}, "evidence_strength": {}}) == "observed"
    assert task_bucket({"decision_status": {"key": "weird"}, "evidence_strength": {}}) == "other"


def test_sources_markup_builder_renders_states():
    snap = {
        "state": "healthy",
        "watcher": {"state": "running", "interval_seconds": 60, "heartbeat_at": 1_700_000_000.0},
        "sources": [
            {"source": "claude-code", "state": "healthy", "scope": "watched",
             "discovered": 1240, "parsed": 1200, "last_success_at": 1_700_000_000.0, "error_count": 0},
            {"source": "codex", "state": "degraded", "scope": "watched", "error_count": 2,
             "last_failure_at": 1_700_000_000.0},
        ],
        "issues": [{"code": "source_order_collision", "source": "codex", "action": "run agentacct doctor"}],
    }
    parts = tui._build_sources_parts(snap, "/tmp/store", _DARK)
    plain = Text.from_markup("\n".join(parts.values())).plain
    assert "Evidence sources" in plain
    assert "Reporting" in plain and "Degraded" in plain
    assert "CC" in plain  # claude-code monogram
    assert "Running" in plain
    assert "Source order collision" in plain
    assert "Nothing leaves this machine" in plain


def test_monogram():
    assert tui._monogram("claude-code") == "CC"
    assert tui._monogram("codex") == "CX"
    assert tui._monogram("opencode") == "OE"


def _seed_finding_task(tmp: Path, now: float) -> None:
    """A completed task whose recorded check is currently failing — a finding that
    needs attention — plus a distinct passing check and a touched file, so the
    sessions & steps timeline has all three groups to render."""

    svc = SentinelService(tmp)
    base = now - 3600
    _record_usage(svc, client="claude-code", model="claude-opus-4-8", session_id="s-find",
                  tokens=40_000_000, updated_at=int(base + 100), cost=1.1,
                  title="Review dashboard visual regression")
    _record_section(svc, session="s-find", section_id="s-find-1",
                    title="Review dashboard visual regression", status="completed", at=base + 100,
                    summary="The snapshot differs from its reviewed reference.")
    # The finding: a currently-failing check carrying a touched file.
    svc.record_event({
        "event_id": "evt_find_fail", "created_at": float(base + 120),
        "source": "claude-code", "event_type": "machine_check",
        "metadata": {
            "sentinel_semantic_kind": "evidence", "client": "claude-code",
            "client_session_id": "s-find", "section_id": "s-find-1", "evidence_type": "test",
            "result": "failed", "name": "snapshot",
            "summary": "The snapshot differs from its reviewed reference.",
            "command": "pytest -q", "exit_code": 1,
            "files": ["apps/agentacct/Sources/agentacct/WorkPane.swift"],
        },
    })
    # A distinct passing check (different command → never supersedes the finding).
    _record_check(svc, session="s-find", section_id="s-find-1", result="passed",
                  at=base + 130, summary="Style checks are clean.", command="ruff check src/")


def test_steps_builder_renders_checks_timeline(tmp_path):
    from agentacct.api import _task_title, build_store_task_projection
    from agentacct.receipt import _project_checks, build_receipt

    now = time.time()
    _seed_finding_task(tmp_path, now)
    proj = build_store_task_projection(tmp_path)
    tasks = [t for t in proj.get("tasks", []) if str(t.get("public_task_id") or "")]
    task = next(t for t in tasks if _task_title(t) == "Review dashboard visual regression")
    receipt = build_receipt(task, public_task_id=str(task.get("public_task_id")), title=_task_title(task))
    checks = _project_checks(task)
    parts = tui._build_steps_parts(receipt, checks, _DARK, 150)

    assert parts["title"] == "SESSIONS & STEPS"
    plain = Text.from_markup("\n".join([parts["head"], parts["title"], parts["body"]])).plain
    assert "Receipt" in plain                       # the ‹ Receipt back-link
    assert "NEEDS ATTENTION" in plain               # the failing-check group
    assert "snapshot differs" in plain              # the finding's summary
    assert "OTHER CURRENT CHECKS" in plain          # the passing-check group
    assert "Style checks are clean" in plain        # the passing check
    assert "FILES" in plain and "WorkPane.swift" in plain


def test_work_receipt_drills_into_steps_and_back(tmp_path):
    """↵ on a receipt swaps the detail cards for the sessions & steps card; esc
    (action_steps_back) returns to the receipt view."""

    _seed_finding_task(tmp_path, time.time())

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("2")  # Work
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            tid = next(str(s.get("task_id")) for s in app._work_summaries
                       if str(s.get("title")) == "Review dashboard visual regression")
            app._show_receipt(tid)
            await pilot.pause()
            assert app._work_detail_mode == "receipt"
            assert app.query_one("#work-outcome").display          # receipt card visible
            assert not app.query_one("#work-steps").display        # steps card hidden

            app._open_steps()
            await pilot.pause()
            assert app._work_detail_mode == "steps"
            assert app.query_one("#work-steps").display            # steps card visible
            assert not app.query_one("#work-outcome").display      # receipt cards hidden
            assert "SESSIONS" in Text.from_markup(app._steps_head).plain

            app.action_steps_back()
            await pilot.pause()
            assert app._work_detail_mode == "receipt"
            assert app.query_one("#work-outcome").display
            assert not app.query_one("#work-steps").display

    _run(scenario())


# --------------------------------------------------------------------------- #
# app: mount, navigation, theme, help, snapshot                               #
# --------------------------------------------------------------------------- #

def test_mounts_and_switches_panes(tmp_path):
    _seed(tmp_path)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.current_pane == "dashboard"
            assert "agentacct" in Text.from_markup(app._topbar_text).plain
            for key, pane in (("2", "work"), ("3", "usage"), ("4", "sources"), ("1", "dashboard")):
                await pilot.press(key)
                await pilot.pause()
                assert app.current_pane == pane

    _run(scenario())


def test_theme_toggle_switches_palette(tmp_path):
    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_cycle_theme()  # auto -> dark
            await pilot.pause()
            assert app.theme == "agentacct-dark" and app.pal is _DARK
            app.action_cycle_theme()  # dark -> light
            await pilot.pause()
            assert app.theme == "agentacct-light" and app.pal is _LIGHT

    _run(scenario())


def test_help_overlay_opens_and_closes(tmp_path):
    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_help()
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            app.pop_screen()
            await pilot.pause()
            assert not isinstance(app.screen, HelpScreen)

    _run(scenario())


def test_snapshot_writes_svg_and_survives(tmp_path):
    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_screenshot()
            await pilot.pause()
            assert app.is_running
            assert list((tmp_path / "snapshots").glob("*.svg"))

    _run(scenario())


# --------------------------------------------------------------------------- #
# Dashboard                                                                    #
# --------------------------------------------------------------------------- #

def test_dashboard_shows_attention_and_recent_work(tmp_path):
    _seed(tmp_path)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            plain = Text.from_markup(app._dashboard_text).plain
            assert "SHIFT BRIEF" in plain
            assert "SIGNAL RAIL" in plain
            assert "RECENT WORK" in plain
            # the blocked task drives the attention hero
            assert "PRIMARY ATTENTION" in plain
            assert "Fix the flaky payment test" in plain

    _run(scenario())


def test_dashboard_empty_store_is_all_clear(tmp_path):
    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            plain = Text.from_markup(app._dashboard_text).plain
            assert "All clear" in plain

    _run(scenario())


# --------------------------------------------------------------------------- #
# Work                                                                         #
# --------------------------------------------------------------------------- #

def test_work_list_populates_and_detail_follows(tmp_path):
    _seed(tmp_path)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.query_one("#work-list", ListView)
            assert len(table.children) >= 2
            # a receipt is auto-selected and its detail rendered
            detail = Text.from_markup(app._work_detail_text).plain
            assert "Current outcome" in detail or "All receipts" in detail

    _run(scenario())


def test_work_refreshes_after_poll_without_manual_refresh(tmp_path):
    _record_usage(_svc := SentinelService(tmp_path), client="claude-code", model="synthetic-model",
                  session_id="first", tokens=100, updated_at=int(time.time() - 20), cost=1, title="first")
    _record_section(_svc, session="first", section_id="first", title="first", status="completed",
                    at=time.time() - 20)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            before = len(app._work_summaries)

            _record_usage(_svc, client="claude-code", model="synthetic-model", session_id="second",
                          tokens=100, updated_at=int(time.time() - 10), cost=1, title="second")
            _record_section(_svc, session="second", section_id="second", title="second",
                            status="completed", at=time.time() - 10)
            app.refresh_data()
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            after_poll = len(app._work_summaries)

            assert before == 1
            assert after_poll == 2

    _run(scenario())


def test_work_status_tab_filters(tmp_path):
    _seed(tmp_path)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            all_rows = len(app.query_one("#work-list", ListView).children)
            # jump to the Attention bucket → only the blocked task
            app._work_status = "attention"
            app._render_work_list()
            await pilot.pause()
            att_rows = len(app.query_one("#work-list", ListView).children)
            assert 0 < att_rows <= all_rows

    _run(scenario())


def test_work_filter_narrows_rows(tmp_path):
    _seed(tmp_path)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.query_one("#work-filter", Input).value = "payment"
            await pilot.pause()
            rows = len(app.query_one("#work-list", ListView).children)
            assert rows == 1

    _run(scenario())


def test_work_sort_cycles(tmp_path):
    _seed(tmp_path)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._work_sort == "attention"
            app.action_work_sort()
            assert app._work_sort == "latest"
            app.action_work_sort()
            assert app._work_sort == "cost"

    _run(scenario())


def test_work_cursor_follows_selection(tmp_path):
    _seed(tmp_path)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.query_one("#work-list", ListView)
            if len(table.children) >= 2:
                first = app._selected_task_id
                await pilot.press("down")  # cursor-follows: detail tracks the row
                await pilot.pause()
                assert app._selected_task_id is not None
                assert app._selected_task_id != first

    _run(scenario())


# --------------------------------------------------------------------------- #
# #220: attention/search over the FULL store, not the truncated recent slice   #
# --------------------------------------------------------------------------- #

def _seed_stale_blocker(tmp: Path, *, now: float, n_completed: int) -> str:
    """A store with ONE blocked task OLDER than `n_completed` newer completed
    tasks (#220 fixture). The blocker's `last_activity_at` is the oldest, so it
    falls outside the newest-`_RECEIPTS_LIMIT` display slice: the pre-fix
    Dashboard classified attention over that slice (reporting "All clear") and
    the pre-fix Work pane never built a row for it (unsearchable). Returns the
    blocker's title."""

    svc = SentinelService(tmp)
    blocker_title = "Rotate the leaked deploy key"
    old = now - 12 * 3600
    _record_usage(svc, client="claude-code", model="claude-opus-4-8", session_id="s-stale-blocked",
                  tokens=50_000_000, updated_at=int(old), cost=40.0, title=blocker_title)
    _record_section(svc, session="s-stale-blocked", section_id="s-stale-blocked-1", title=blocker_title,
                    status="blocked", at=old, summary="hit a blocker", blocker="prod vault unreachable")
    # `n_completed` completed tasks, every one NEWER than the blocker so the
    # blocker sorts last (oldest) in the newest-first list.
    for i in range(n_completed):
        at = old + 60 + i * 30
        sid = f"s-done-{i:04d}"
        _record_usage(svc, client="claude-code", model="claude-opus-4-8", session_id=sid,
                      tokens=1_000_000, updated_at=int(at), cost=0.5, title=f"Completed task {i:04d}")
        _record_section(svc, session=sid, section_id=f"{sid}-1", title=f"Completed task {i:04d}",
                        status="completed", at=at)
    return blocker_title


def test_dashboard_attention_scans_full_store_not_recent_slice(tmp_path):
    """#220 BUG: a blocker older than the newest `_RECEIPTS_LIMIT` completed
    tasks must still drive the Dashboard. Pre-fix, attention was classified over
    the truncated recent slice, so this store falsely read "All clear". (Slow —
    it seeds `_RECEIPTS_LIMIT` + 1 tasks; that is expected.)"""

    now = time.time()
    blocker_title = _seed_stale_blocker(tmp_path, now=now, n_completed=tui._RECEIPTS_LIMIT)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            plain = Text.from_markup(app._dashboard_text).plain
            assert "All clear" not in plain          # the whole queue is NOT empty
            assert blocker_title in plain             # the stale blocker surfaces
            assert "PRIMARY ATTENTION" in plain
            # exactly one task needs the user, counted over the FULL store
            assert "1 review item" in plain
            assert "1 OF 1" in plain

    _run(scenario())


def test_work_search_reaches_stale_blocker_and_discloses_total(tmp_path):
    """#220 BUG: with more than `_RECEIPTS_LIMIT` tasks the older blocker is
    outside the capped display list, yet text search must still find it and the
    head must disclose the true total. Pre-fix only the newest `_RECEIPTS_LIMIT`
    rows were built, so the blocker was unsearchable and the total undisclosed."""

    now = time.time()
    cap = tui._RECEIPTS_LIMIT
    blocker_title = _seed_stale_blocker(tmp_path, now=now, n_completed=cap)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            # a row is built for EVERY task; the true total is cap + 1
            assert len(app._work_summaries) == cap + 1
            blocker_id = next(str(s.get("task_id")) for s in app._work_summaries
                              if str(s.get("title")) == blocker_title)
            # Sort by recency so the OLDEST row (the blocker) falls past the
            # display cap — under the default attention sort it would be pinned to
            # the top instead, which would not exercise the hidden-row case.
            app._work_sort = "latest"
            app._render_work_head()
            app._render_work_list()
            await pilot.pause()
            # the DISPLAY is still capped, so the older blocker is now hidden
            assert len(app._work_visible_ids) == cap
            assert blocker_id not in app._work_visible_ids
            # the head discloses the cap so the hidden row is never silent
            head = Text.from_markup(app._work_head_text).plain
            assert f"showing {cap} of {cap + 1}" in head
            # ...and text search still reaches the hidden blocker
            app.query_one("#work-filter", Input).value = "leaked deploy key"
            await pilot.pause()
            matches = app._filtered_work()
            assert [str(s.get("title")) for s in matches] == [blocker_title]
            assert blocker_id in app._work_visible_ids

    _run(scenario())


def test_work_under_cap_surfaces_blocker_with_no_truncation_disclosure(tmp_path):
    """#220 LEGIT (preserve): when the whole store fits under `_RECEIPTS_LIMIT`,
    the blocker still surfaces on the Dashboard and in the Work list, and the head
    shows NO truncation disclosure."""

    now = time.time()
    blocker_title = _seed_stale_blocker(tmp_path, now=now, n_completed=3)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            dash = Text.from_markup(app._dashboard_text).plain
            assert "All clear" not in dash
            assert blocker_title in dash
            await pilot.press("2")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert len(app._work_summaries) == 4
            blocker_id = next(str(s.get("task_id")) for s in app._work_summaries
                              if str(s.get("title")) == blocker_title)
            # within the cap → shown in the list, and no "showing N of M" disclosure
            assert blocker_id in app._work_visible_ids
            head = Text.from_markup(app._work_head_text).plain
            assert "showing" not in head

    _run(scenario())


# --------------------------------------------------------------------------- #
# Usage                                                                        #
# --------------------------------------------------------------------------- #

def test_usage_shows_capacity_and_recorded(tmp_path):
    _seed(tmp_path)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            plain = Text.from_markup(app._usage_text).plain
            assert "Usage & limits" in plain
            assert "CURRENT CAPACITY" in plain
            assert "claude-code" in plain
            assert "47% used" in plain
            assert "RECORDED USAGE" in plain

    _run(scenario())


def test_usage_range_cycles(tmp_path):
    _seed(tmp_path)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._usage_range_index == 0
            app.action_usage_range()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._usage_range_index == 1
            assert "30D" in Text.from_markup(app._usage_text).plain

    _run(scenario())


# --------------------------------------------------------------------------- #
# Sources                                                                      #
# --------------------------------------------------------------------------- #

def test_sources_renders_local_only(tmp_path):
    _seed(tmp_path)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            plain = Text.from_markup(app._sources_text).plain
            assert "Evidence sources" in plain
            assert "Nothing leaves this machine" in plain

    _run(scenario())


# --------------------------------------------------------------------------- #
# markup safety                                                                #
# --------------------------------------------------------------------------- #

def test_markup_safety_with_hostile_fields(tmp_path):
    svc = SentinelService(tmp_path)
    now = 1_700_000_000.0
    _record_usage(svc, client="claude-code", model="gpt[/]4", session_id="s1",
                  tokens=1000, updated_at=int(now - 60), cost=1.0, title="pwn[/]title[/]")
    _record_section(svc, session="s1", section_id="s1-1", title="[/]boom", status="completed", at=now - 60)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            # every composed pane string must parse as valid markup
            Text.from_markup(app._dashboard_text)
            await pilot.press("2")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            Text.from_markup(app._work_detail_text)
            await pilot.press("3")  # usage
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            Text.from_markup(app._usage_text)
            await pilot.press("4")  # sources
            await pilot.pause()
            Text.from_markup(app._sources_text)

    _run(scenario())


def test_sources_markup_survives_hostile_fields():
    snap = {
        "state": "de[/]graded",
        "watcher": {"state": "stopped", "heartbeat_at": 1_700_000_000.0},
        "sources": [{"source": "ev[/]il", "state": "healthy", "scope": "watched",
                     "parsed": 5, "error_count": 1, "last_success_at": 1_700_000_000.0}],
        "issues": [{"code": "x[/]y", "source": "z[/]", "action": "run [/]doctor"}],
    }
    Text.from_markup("\n".join(tui._build_sources_parts(snap, "/tmp/[/]store", _DARK).values()))


# --------------------------------------------------------------------------- #
# CLI guard                                                                    #
# --------------------------------------------------------------------------- #

def test_tui_requires_interactive_terminal():
    result = CliRunner().invoke(cli_app, ["tui"])
    assert result.exit_code == 1
    assert "interactive terminal" in result.output


def test_tui_rejects_bad_window():
    result = CliRunner().invoke(cli_app, ["tui", "--window", "5m"])
    assert result.exit_code != 0
