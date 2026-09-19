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
from agentacct.work_ledger import _project_identity  # noqa: E402
import agentacct.tui as tui  # noqa: E402
from agentacct.tui import (  # noqa: E402
    AgentAcctTUI,
    HelpScreen,
    WorksetDetailScreen,
    _DARK,
    _LIGHT,
    _WORK_TABS,
    _ZoomWindow,
)
from textual.widgets import DataTable, Input, ListView  # noqa: E402


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
    assert "Diagnostics" in plain
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


def test_steps_lists_all_current_checks_without_dead_toggle():
    """'Other current checks' renders in full (the detail scrolls by keyboard) —
    no capped '▾ Show N more' cue that nothing could open."""

    pal = _DARK
    receipt = {
        "task_id": "t1", "title": "Big task", "last_activity_at": time.time(),
        "axes": {"decision_status": {"key": "verified"},
                 "evidence_strength": {"key": "independently_checked"}},
    }
    checks = [{"result": "passed", "name": f"c{i}", "kind": "test", "summary": f"check {i} ok",
               "command": "pytest -q", "exit_code": 0, "source": "claude-code",
               "at": time.time() - i} for i in range(6)]
    parts = tui._build_steps_parts(receipt, checks, pal, 150, task=None)
    plain = Text.from_markup(parts["body"]).plain
    assert "OTHER CURRENT CHECKS · 6" in plain            # section cap is upper-cased
    assert "Show" not in plain and "▾" not in plain       # no dead expand cue
    for i in range(6):                                     # every check, not just 4
        assert f"check {i} ok" in plain


def test_sessions_jk_moves_cursor(tmp_path):
    """j/k move the Sessions list cursor (the help overlay advertises them, so
    they must be wired, not just decorative)."""

    _seed(tmp_path)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("3")  # Sessions
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            first = app._selected_task_id
            assert first is not None
            await pilot.press("j")
            await pilot.pause()
            assert app._selected_task_id != first          # j moved down
            await pilot.press("k")
            await pilot.pause()
            assert app._selected_task_id == first           # k moved back up

    _run(scenario())


def test_ctrl_d_scrolls_the_detail(tmp_path):
    """ctrl+d pages the receipt/steps detail. Its VerticalScroll never holds focus
    and the focused ListView already binds pagedown to scroll ITSELF, so a
    dedicated, non-shadowed key is the only keyboard path to a tall detail."""

    from textual.containers import VerticalScroll

    now = time.time()
    _seed_finding_task(tmp_path, now)
    svc = SentinelService(tmp_path)
    for i in range(24):  # pile on distinct checks so the steps body overflows
        _record_check(svc, session="s-find", section_id="s-find-1", result="passed",
                      at=now - 3600 + i, summary=f"extra check {i}", command=f"cmd-{i}")

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("3")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            tid = next(str(s.get("task_id")) for s in app._work_summaries
                       if str(s.get("title")) == "Review dashboard visual regression")
            app._show_receipt(tid)
            await pilot.pause()
            app._open_steps()
            await pilot.pause()
            ds = app.query_one("#work-detail", VerticalScroll)
            assert ds.max_scroll_y > 0                 # the detail overflows → scrollable
            before = ds.scroll_offset.y
            await pilot.press("ctrl+d")
            await pilot.pause()
            assert ds.scroll_offset.y > before          # ctrl+d paged the detail down

    _run(scenario())


def test_steps_activity_timeline_times_checks_and_fits_column(tmp_path):
    """Regression for the 'messy' session-detail timeline: (1) every check must
    carry a real time — build_timeline_events reads created_at, so the TUI passes
    the RAW task checks (checks=None), not the projected list (whose time is under
    'at') — otherwise checks bunch timeless at the start; and (2) the track is
    sized to the detail COLUMN, never the full terminal, so it can't wrap."""

    from agentacct.api import _task_title, build_store_task_projection
    from agentacct.receipt import _project_checks, build_receipt
    from agentacct.task_timeline import build_timeline_events

    now = time.time()
    _seed_finding_task(tmp_path, now)
    proj = build_store_task_projection(tmp_path)
    task = next(t for t in proj["tasks"] if _task_title(t) == "Review dashboard visual regression")

    # (1) The raw-check path times every check event (the fix for timeless checks).
    events = build_timeline_events(task)
    check_events = [e for e in events if e.get("kind") == "check"]
    assert check_events, "the seeded task has recorded checks"
    assert all(isinstance(e.get("occurred_at"), (int, float)) and not isinstance(e.get("occurred_at"), bool)
               for e in check_events)

    receipt = build_receipt(task, public_task_id=str(task.get("public_task_id")), title=_task_title(task))
    checks = _project_checks(task)
    width = 150
    parts = tui._build_steps_parts(receipt, checks, _DARK, width, task=task)
    body_plain = Text.from_markup(parts["body"]).plain
    assert "no recorded time" not in body_plain      # nothing bunched timeless

    # (2) Every timeline row fits the detail column (dw), so it never wraps.
    dw = max(46, int(width * 0.54) - 10)
    tl = tui._task_activity_timeline(events, _DARK, dw)
    for row in tl["rows"]:
        assert tui._plainlen(row) <= dw
    if tl["axis"]:
        assert tui._plainlen(tl["axis"]) <= dw


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
            await pilot.press("3")  # Sessions (receipts)
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
            # The per-task activity timeline (recorded work + checks on a time axis).
            steps_plain = Text.from_markup(app._steps_text).plain
            assert "ACTIVITY" in steps_plain

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
            for key, pane in (
                ("2", "worksets"),
                ("3", "work"),
                ("4", "usage"),
                ("5", "sources"),
                ("1", "dashboard"),
            ):
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
            # ↵ review deep-link is real; the old dead "Copy review brief" chip is gone
            assert "Review evidence" in plain
            assert "Copy review brief" not in plain

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
            await pilot.press("3")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.query_one("#work-list", DataTable)
            assert table.row_count >= 2
            # a receipt is auto-selected and its detail rendered
            detail = Text.from_markup(app._work_detail_text).plain
            assert "Current outcome" in detail or "All receipts" in detail
            # the plain-text row mirror carries the receipt titles
            assert "payment" in app._work_rows_text.lower()

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
            await pilot.press("3")
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
            await pilot.press("3")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            all_rows = app.query_one("#work-list", DataTable).row_count
            # jump to the Attention bucket → only the blocked task
            app._work_status = "attention"
            app._render_work_list()
            await pilot.pause()
            att_rows = app.query_one("#work-list", DataTable).row_count
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
            await pilot.press("3")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.query_one("#work-filter", Input).value = "payment"
            await pilot.pause()
            rows = app.query_one("#work-list", DataTable).row_count
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
            await pilot.press("3")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._work_sort == "latest"  # default is time order
            app.action_work_sort()
            assert app._work_sort == "cost"
            app.action_work_sort()
            assert app._work_sort == "attention"

    _run(scenario())


def test_work_cursor_follows_selection(tmp_path):
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
            table = app.query_one("#work-list", DataTable)
            if table.row_count >= 2:
                first = app._selected_task_id
                await pilot.press("down")  # cursor-follows: detail tracks the row
                await pilot.pause()
                assert app._selected_task_id is not None
                assert app._selected_task_id != first

    _run(scenario())


def test_sessions_datatable_columns_enter_and_sort(tmp_path):
    """The Sessions master list is a DataTable: five columns, Enter opens the steps
    drill-in for the highlighted row, and cycling sort to 'cost' reorders by cost."""

    _seed(tmp_path)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("3")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            dt = app.query_one("#work-list", DataTable)
            assert len(dt.columns) == 5          # Task/Outcome/Evidence/Cost/Age
            assert dt.row_count >= 2
            # Enter drills the highlighted row into its steps.
            await pilot.press("enter")
            await pilot.pause()
            assert app._work_detail_mode == "steps"
            await pilot.press("escape")           # back to the receipt view
            await pilot.pause()
            assert app._work_detail_mode == "receipt"
            # Cycle the sort to cost → rows come back ordered by descending cost.
            app.action_work_sort()                # latest → cost
            await pilot.pause()
            costs = [float((s.get("cost") or {}).get("estimated_cost_usd") or 0.0)
                     for s in app._work_visible_rows]
            assert costs == sorted(costs, reverse=True)

    _run(scenario())


def test_sessions_filter_enter_returns_focus_to_table(tmp_path):
    """Submitting the filter (Enter) moves focus back to the DataTable — a stale
    ListView query used to raise WrongType and leave focus stuck in the Input."""

    _seed(tmp_path)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("3")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            inp = app.query_one("#work-filter", Input)
            inp.focus()
            inp.value = "payment"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app.focused is app.query_one("#work-list", DataTable)

    _run(scenario())


def test_steps_survives_refresh_non_top_row(tmp_path):
    """Sticky steps must survive the rebuild for a NON-top row too — the rebuild
    posts a transient row-0 highlight that used to clobber the drill-in for any
    task that wasn't the cursor's first row."""

    _seed(tmp_path)  # three receipts

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("3")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            dt = app.query_one("#work-list", DataTable)
            assert dt.row_count >= 2
            dt.move_cursor(row=1)                 # drill into a NON-top row
            await pilot.pause()
            second = app._selected_task_id
            app._open_steps()
            await pilot.pause()
            assert app._work_detail_mode == "steps"
            app.refresh_data(force=True)           # the periodic-refresh path
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.pause()
            assert app._selected_task_id == second        # same non-top task
            assert app._work_detail_mode == "steps"       # still drilled in
            assert app.query_one("#work-steps").display

    _run(scenario())


def test_empty_filter_dismisses_stale_steps(tmp_path):
    """Filtering to no matches while drilled into steps drops the drill-in instead
    of leaving a stale steps card under the 'No receipts' head."""

    _seed(tmp_path)

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("3")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            app._open_steps()
            await pilot.pause()
            assert app._work_detail_mode == "steps"
            app.query_one("#work-filter", Input).value = "zzz-no-such-receipt-zzz"
            await pilot.pause()
            assert app._work_detail_mode == "receipt"
            assert not app.query_one("#work-steps").display

    _run(scenario())


def test_work_row_evidence_never_overstates_ungradeable():
    """The Evidence cell must not show a green passing tally next to an ungradeable
    receipt (its checks are unattributed pool runs, not evidence for this task)."""

    pal = _DARK
    base = {"task_id": "t", "title": "x", "last_activity_at": time.time(), "cost": {},
            "decision_status": {"key": "reported"}}
    ungradeable = {**base, "evidence_strength": {
        "gradeable": False, "key": "undefined",
        "checks_total": 2, "checks_passed": 2, "checks_failed": 0}}
    gradeable = {**base, "evidence_strength": {
        "gradeable": True, "key": "independently_checked", "checkable_total": 1, "checked_total": 1,
        "checks_total": 2, "checks_passed": 2, "checks_failed": 0}}
    cells_u, _ = tui._work_row_cells(ungradeable, pal, 30)
    cells_g, _ = tui._work_row_cells(gradeable, pal, 30)
    assert "2/2" not in cells_u[2].plain     # ungradeable → pip only, no green tally
    assert "2/2" in cells_g[2].plain          # gradeable → shows the count


# --------------------------------------------------------------------------- #
# Worksets ("Work" tab)                                                        #
# --------------------------------------------------------------------------- #

def _seed_workset(tmp: Path, *, name: str = "proj") -> str:
    """The default seed already runs claude-code AND codex sessions in /tmp/proj;
    group that folder so the Work tab has one cross-agent card to draw."""

    _seed(tmp)
    svc = SentinelService(tmp)
    identity = _project_identity("/tmp/proj")
    svc.record_workset_action(
        action="create",
        workset_id="ws_proj",
        name=name,
        project_identity=identity,
        expected_revision=0,
        idempotency_key="test:ws_proj:create",
    )
    return identity


def test_worksets_pane_renders_cross_agent_timeline(tmp_path):
    _seed_workset(tmp_path, name="webapp")

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("2")  # Work (the folder-anchored groupings)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            plain = Text.from_markup(app._worksets_text).plain
            # The card: the group name + the "grouped by folder" chip.
            assert "webapp" in plain
            assert "grouped by folder" in plain
            # The cross-agent legend and the shared-axis block glyph (the timeline).
            assert "Claude Code" in plain and "Codex" in plain
            assert "█" in plain
            # The honesty note: a labelled SUM, never a re-graded combined verdict.
            assert "not a combined verdict" in plain
            # A real card ListItem mounted (not just the empty state).
            lv = app.query_one("#worksets-list", ListView)
            assert len(lv.children) >= 1

    _run(scenario())


def test_worksets_empty_state_when_no_groups(tmp_path):
    _seed(tmp_path)  # sessions, but no workset grouping created

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("2")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            plain = Text.from_markup(app._worksets_text).plain
            assert "No work groups yet" in plain

    _run(scenario())


def test_worksets_cursor_survives_refresh(tmp_path):
    """The highlighted group stays on the SAME group across a rebuild (the 5s
    refresh), instead of snapping back to the top card."""

    _seed_workset(tmp_path, name="alpha")  # ws_proj anchored on /tmp/proj
    svc = SentinelService(tmp_path)
    now = time.time()
    _record_usage(svc, client="claude-code", model="claude-opus-4-8", session_id="s-beta",
                  tokens=10_000_000, updated_at=int(now - 1000), cost=2.0, title="beta work", project="proj2")
    _record_section(svc, session="s-beta", section_id="s-beta-1", title="beta work",
                    status="completed", at=now - 900, project="proj2")
    svc.record_workset_action(action="create", workset_id="ws_proj2", name="beta",
                              project_identity=_project_identity("/tmp/proj2"),
                              expected_revision=0, idempotency_key="test:ws_proj2:create")

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("2")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            lv = app.query_one("#worksets-list", ListView)
            assert len(lv.children) >= 2
            lv.index = 1
            await pilot.pause()
            picked = app._selected_workset_id
            assert picked is not None
            # A rebuild — what the periodic refresh triggers.
            app._start_worksets(force=True)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._selected_workset_id == picked           # same group, by id
            idx = app.query_one("#worksets-list", ListView).index
            assert str(app._worksets[idx].get("workset_id")) == picked

    _run(scenario())


def test_steps_view_survives_refresh(tmp_path):
    """A background refresh while reading a task's steps keeps you in steps (with
    fresh data), instead of bouncing you back out to the receipt view."""

    _seed_finding_task(tmp_path, time.time())

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("3")  # Sessions (receipts)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            tid = next(str(s.get("task_id")) for s in app._work_summaries
                       if str(s.get("title")) == "Review dashboard visual regression")
            app._show_receipt(tid)
            await pilot.pause()
            app._open_steps()
            await pilot.pause()
            assert app._work_detail_mode == "steps"
            assert app.query_one("#work-steps").display
            # A forced rebuild — the periodic-refresh path.
            app.refresh_data(force=True)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._work_detail_mode == "steps"       # still drilled in
            assert app.query_one("#work-steps").display   # steps card still shown

    _run(scenario())


def test_worksets_card_markup_survives_hostile_fields():
    """A group name / session title with stray markup must not crash the view."""

    pal = _DARK
    card = {
        "name": "webapp [/] ]",
        "summary": {
            "session_count": 2,
            "sources": [{"client": "claude-code", "session_count": 1}, {"client": "codex", "session_count": 1}],
            "first_activity_at": 1000.0,
            "last_activity_at": 1000.0 + 4 * 86400,
            "estimated_cost_usd": 70.5,
            "cost_complete": True,
            "priced_sessions": 2,
            "unpriced_sessions": 0,
            "cost_confidence": "estimated_from_tokens",
        },
        "sessions": [
            {"title": "Emit OTLP [/] metrics", "client": "claude-code", "status": "completed",
             "first_activity_at": 1000.0, "last_activity_at": 1100.0},
            {"title": "Trace the slow query", "client": "codex", "status": "blocked",
             "first_activity_at": 1000.0 + 4 * 86400, "last_activity_at": 1000.0 + 4 * 86400},
        ],
        "sessions_total": 2,
        "sessions_truncated": False,
    }
    markup = tui._workset_card_markup(card, pal, width=140)
    # markup-safety invariant: it must parse cleanly as Rich markup.
    Text.from_markup(markup)
    assert "grouped by folder" in Text.from_markup(markup).plain
    assert "~$70.50" not in markup  # complete estimate is ≈$, not a partial ~$
    assert "≈$70.50" in markup


def test_cjk_titles_align_by_cell_width():
    """Wide/CJK titles must be measured and padded by terminal CELLS, not chars,
    so timeline rows with Chinese titles line up with ASCII rows (the alignment
    bug the owner hit). A char-count pad would drift ~1 col per 2 CJK glyphs."""

    # CJK glyphs are 2 cells wide, so cell width exceeds character count.
    assert tui._cell_len("升级 SaaS 落地页") > len("升级 SaaS 落地页")
    # Padding to a fixed cell width yields equal visual width for any script.
    a = tui._pad_vis("[b]升级 agentacct 落地页[/]", 34)
    b = tui._pad_vis("[b]Refactor the auth store[/]", 34)
    assert tui._plainlen(a) == tui._plainlen(b) == 34
    # Truncation respects cell width (never over-runs the column, never splits a
    # wide glyph past the budget).
    t = tui._trunc("升级 agentacct 落地页测试项目", 12)
    assert tui._cell_len(t) <= 12 and t.endswith("…")

    # In a real timeline, a CJK row and an ASCII row start their track at the
    # same column: the left label pads to the same cell width on both.
    pal = _DARK
    base = 1_700_000_000.0
    lanes = [
        {"session_key": "z", "title": "升级 agentacct SaaS 落地页", "client": "claude-code",
         "status": "completed", "first_activity_at": base, "last_activity_at": base + 100},
        {"session_key": "a", "title": "Refactor auth", "client": "codex",
         "status": "completed", "first_activity_at": base, "last_activity_at": base + 100},
    ]
    tl = tui._workset_timeline(lanes, pal, 120)
    # every rendered row has the identical total cell width (the grid is aligned)
    widths = {tui._plainlen(r) for r in tl["rows"]}
    assert len(widths) == 1


def test_worksets_timeline_draws_duration_bars():
    """A session is a BAR spanning first→last activity: a long run is a wide bar,
    a point-in-time session is a single cell (the fix for 'everything is a square')."""

    pal = _DARK

    def blocks(markup):
        return Text.from_markup(markup).plain.count("█")

    base = 1_700_000_000.0  # realistic epoch times (0.0 is the "no timestamp" sentinel)
    lanes = [
        {"session_key": "k1", "title": "long run", "client": "claude-code", "status": "completed",
         "first_activity_at": base, "last_activity_at": base + 90_000.0},
        {"session_key": "k2", "title": "quick", "client": "codex", "status": "completed",
         "first_activity_at": base + 95_000.0, "last_activity_at": base + 95_000.0},
    ]
    tl = tui._workset_timeline(lanes, pal, 120)
    long_bar, short_bar = blocks(tl["rows"][0]), blocks(tl["rows"][1])
    assert short_bar == 1               # a point session quantises to one cell
    assert long_bar >= 10               # a long session is a clearly multi-cell bar
    assert long_bar > short_bar


def test_worksets_share_one_global_axis():
    """Every Work card is drawn against ONE axis (earliest start → latest activity
    across all groups), so a bar's column is comparable card-to-card and a group
    that finished earlier does not get stretched to the right edge."""

    pal = _DARK
    base = 1_700_000_000.0
    DAY = 86400.0
    early = {"sessions": [  # group that ran days 0-1
        {"session_key": "e1", "title": "early", "client": "claude-code", "status": "completed",
         "first_activity_at": base, "last_activity_at": base + DAY},
    ]}
    late = {"sessions": [   # group that ran days 2-3
        {"session_key": "l1", "title": "late", "client": "codex", "status": "completed",
         "first_activity_at": base + 2 * DAY, "last_activity_at": base + 3 * DAY},
    ]}

    lo, hi = tui._worksets_axis_bounds([early, late])
    assert (lo, hi) == (base, base + 3 * DAY)      # union of every group's span

    e_lanes, l_lanes = early["sessions"], late["sessions"]
    e_shared = tui._workset_timeline(e_lanes, pal, 120, axis_bounds=(lo, hi))
    l_shared = tui._workset_timeline(l_lanes, pal, 120, axis_bounds=(lo, hi))
    assert e_shared["axis"] == l_shared["axis"]    # identical axis on both cards

    def track_plain(row):
        # The row is "<left column> <track>"; keep the track (drop the label).
        return Text.from_markup(row).plain.rsplit(" ", 1)[-1]

    e_row = track_plain(e_shared["rows"][0])
    l_row = track_plain(l_shared["rows"][0])
    # The early group ends before the global hi -> its bar leaves trailing hair,
    # so the row does NOT end in a block. The late group reaches hi -> ends in a block.
    assert e_row.endswith("─") and not e_row.endswith("█")
    assert l_row.endswith("█")
    # The early group's bar sits left of the late group's bar (real comparison).
    assert e_row.index("█") < l_row.index("█")

    # Without the shared axis each card normalises to its own span, so both bars
    # start at column 0 and the comparison is lost — the regression this guards.
    e_solo = track_plain(tui._workset_timeline(e_lanes, pal, 120)["rows"][0])
    l_solo = track_plain(tui._workset_timeline(l_lanes, pal, 120)["rows"][0])
    assert e_solo.index("█") == 0 and l_solo.index("█") == 0


def test_zoom_window_math_matches_swift():
    # Half-range window centred: 2x zoom over [0,100] -> [25,75], 50% coverage.
    w = _ZoomWindow(0.0, 100.0, 2.0, 0.5)
    assert (round(w.start), round(w.end)) == (25, 75)
    assert round(w.coverage(0.0, 100.0), 3) == 0.5
    # Clamp: a window pushed past the edge slides back inside the data.
    edge = _ZoomWindow(0.0, 100.0, 2.0, 1.0)
    assert round(edge.end) == 100 and round(edge.start) == 50
    # applyZoom keeps the anchored time fixed; zoom is clamped to [1, 64].
    z, p = _ZoomWindow.apply_zoom(1.0, 0.5, 1000.0, 0.5, 0.0, 100.0)
    assert z == 64.0
    # A timeless lane stays visible in any window; a timed lane out of range culls.
    assert _ZoomWindow.lane_visible(None, None, 10.0, 20.0) is True
    assert _ZoomWindow.lane_visible(5.0, 5.0, 10.0, 20.0) is False
    assert _ZoomWindow.lane_visible(15.0, 15.0, 10.0, 20.0) is True


def test_worksets_detail_opens_zooms_and_scrubs(tmp_path):
    _seed_workset(tmp_path, name="webapp")

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test(size=(150, 45)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("2")  # Work (worksets)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("enter")  # open the highlighted group's timeline
            await pilot.pause()
            assert isinstance(app.screen, WorksetDetailScreen)
            body = Text.from_markup(app.screen._body_text).plain
            assert "full range" in body
            # Zoom in: the coverage line becomes a percentage of the full range.
            await pilot.press("plus")
            await pilot.pause()
            assert "% of range" in Text.from_markup(app.screen._body_text).plain
            # Reset returns to the full range.
            await pilot.press("0")
            await pilot.pause()
            assert "full range" in Text.from_markup(app.screen._body_text).plain
            # Scrub down: the focused session's own facts read out.
            await pilot.press("down")
            await pilot.pause()
            facts = Text.from_markup(app.screen._body_text).plain
            assert "duration" in facts and "cost" in facts and "tokens" in facts
            # Esc returns to the Work list.
            await pilot.press("escape")
            await pilot.pause()
            assert app.current_pane == "worksets"

    _run(scenario())


def test_dashboard_review_deeplink_jumps_to_attention(tmp_path):
    _seed(tmp_path)  # includes a blocked (attention) task

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.current_pane == "dashboard"
            await pilot.press("enter")  # Review evidence deep-link
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.current_pane == "work"
            assert app._work_status == "attention"

    _run(scenario())


def test_worksets_create_group_via_g(tmp_path):
    _seed(tmp_path)  # cross-agent sessions in /tmp/proj, but no grouping yet

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test(size=(150, 45)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("2")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "No work groups yet" in Text.from_markup(app._worksets_text).plain
            await pilot.press("g")  # open the create flow
            await pilot.pause()
            from agentacct.tui import WorksetCreateScreen
            assert isinstance(app.screen, WorksetCreateScreen)
            await pilot.press("enter")  # folder row -> name field (name prefilled)
            await pilot.pause()
            await pilot.press("enter")  # submit the prefilled name -> create
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.current_pane == "worksets"
            plain = Text.from_markup(app._worksets_text).plain
            assert "grouped by folder" in plain  # a real card exists now
            assert len(app._worksets) == 1

    _run(scenario())


def test_worksets_rename_and_delete(tmp_path):
    _seed_workset(tmp_path, name="webapp")

    async def scenario():
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test(size=(150, 45)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("2")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._worksets and app._worksets[0]["name"] == "webapp"
            # Rename.
            await pilot.press("e")
            await pilot.pause()
            from agentacct.tui import _WorksetPromptScreen, _WorksetConfirmScreen
            assert isinstance(app.screen, _WorksetPromptScreen)
            app.screen.query_one("#ws-prompt-input", Input).value = "renamed-web"
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._worksets and app._worksets[0]["name"] == "renamed-web"
            # Delete -> back to the empty state.
            await pilot.press("x")
            await pilot.pause()
            assert isinstance(app.screen, _WorksetConfirmScreen)
            await pilot.press("y")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert len(app._worksets) == 0
            assert "No work groups yet" in Text.from_markup(app._worksets_text).plain

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
            await pilot.press("4")
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
            await pilot.press("4")
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
            await pilot.press("5")
            await pilot.pause()
            plain = Text.from_markup(app._sources_text).plain
            assert "Diagnostics" in plain
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
            await pilot.press("3")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            Text.from_markup(app._work_detail_text)
            await pilot.press("4")  # usage
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            Text.from_markup(app._usage_text)
            await pilot.press("5")  # sources
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
