#!/usr/bin/env python3
"""Regenerate the README's `agentacct tui` screenshots from SYNTHETIC demo data.

Builds a throwaway store of invented sessions — recorded work steps, machine-check
evidence, usage, and a calibrated weekly-plan series — renders the home / sessions /
detail / usage screens headlessly (Textual `save_screenshot`), and converts the SVGs
to PNGs with headless Chrome. The environment is isolated so no real data can leak
into a committed image: the TUI's on-launch usage import is DISABLED outright
(`AGENTACCT_TUI_AUTO_IMPORT=0` — build_store() supplies every row the screens need),
and, as defense in depth, HOME plus every client-home / XDG override are pointed away
from this machine. Every pixel is synthetic.

    python scripts/gen_tui_screenshots.py            # writes docs/assets/tui-*.png
    python scripts/gen_tui_screenshots.py /tmp/out   # writes elsewhere

Run from a clone with the dev deps installed (`pip install -e .`). Chrome is used
only for SVG->PNG; if it's absent the SVGs are still written and the command prints
the conversion line to run by hand.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# --- isolate BEFORE importing agentacct: no real client logs, no global scans ------
_REAL_HOME = os.environ.get("HOME", "")  # kept for the Chrome SVG->PNG step only
# A FIXED, clean demo home so the store path shown on screen is tidy and carries no
# real username / temp-dir hash (tempfile.gettempdir() is /var/folders/... on macOS).
# Wiped first + at the end.
_FAKE_HOME = "/tmp/agentacct-demo-home"
shutil.rmtree(_FAKE_HOME, ignore_errors=True)
os.environ["HOME"] = _FAKE_HOME
os.environ["CLAUDE_CONFIG_DIR"] = str(Path(_FAKE_HOME) / ".claude")
# The surest guard: turn the TUI's on-launch usage import OFF entirely, so no client
# log is ever scanned. build_store() below provides everything the screens render.
os.environ["AGENTACCT_TUI_AUTO_IMPORT"] = "0"
os.environ["AGENTACCT_SCAN_GLOBAL_LIMITS"] = "0"
# Defense in depth: drop every client-home / XDG override that would otherwise resolve
# a client's logs AHEAD of the (empty) fake HOME.
for _var in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "AGENTACCT_STORE_DIR",
             "AGENTACCT_GLOBAL_STORE_DIR", "CODEX_HOME", "OPENCODE_DATA_DIR", "HERMES_HOME",
             "OPENCLAW_DIR", "CURSOR_HOME"):
    os.environ.pop(_var, None)

import asyncio  # noqa: E402

from agentacct.client_usage import ClientUsageEvent  # noqa: E402
from agentacct.plan_cost import BASELINE_MODEL_WEIGHTS  # noqa: E402
from agentacct.service import SentinelService  # noqa: E402
from agentacct.tui import AgentAcctTUI  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else REPO_ROOT / "docs" / "assets"
OUT.mkdir(parents=True, exist_ok=True)
STORE = Path(_FAKE_HOME) / ".local" / "state" / "agentacct" / "state"

NOW = time.time()
OPUS = "claude-opus-4-8"
OPUS_W = BASELINE_MODEL_WEIGHTS[OPUS]  # %/Mtoken
SHOTS = ("tui-dashboard", "tui-work", "tui-steps", "tui-usage", "tui-sources")


def _usage(svc, *, client, model, session, title, tokens, at, cost, project="acme-web"):
    ev = ClientUsageEvent(
        client=client, client_session_id=session,
        source_path=Path(f"/demo/{client}/{session}.jsonl"), title=title, cwd=f"/demo/{project}",
        model=model, input_tokens=tokens, output_tokens=0, cached_input_tokens=0,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
        cache_creation_tokens_reported=True, cache_read_tokens_reported=True,
        reasoning_output_tokens=0, provider_name=client, started_at=int(at), updated_at=int(at),
        turn_count=1, usage_row_lane=f"model:{model}", source_namespace_fingerprint=f"sha256:{client}",
        input_tokens_reported=True, output_tokens_reported=True, reasoning_output_tokens_reported=True,
        total_tokens=tokens, total_tokens_reported=True,
    ).to_sentinel_event()
    ev["estimated_cost_usd"] = cost
    ev["cost_confidence"] = "estimated_from_tokens"
    svc.record_event(ev, trusted_usage_import=True)


def _section(svc, *, session, title, section_id, status, at, client="claude-code", project="acme-web",
             kind="implementation", summary="", blocker=None, files=None, next_step=None):
    svc.record_event({
        "event_id": f"evt_section_{session}_{section_id}_{status}",
        "created_at": float(at), "source": client, "event_type": f"section_{status}", "run_id": None,
        "metadata": {
            "sentinel_semantic_kind": "section", "client": client, "client_session_id": session,
            "client_transcript_id": session,
            "client_context_keys_authored": ["client_session_id", "client_transcript_id"],
            "project_dir": f"/demo/{project}", "section_id": section_id, "section_status": status,
            "section_title": title, "summary": summary, "kind": kind,
            "files": files or ["src/app/module.py"], "blocker": blocker, "next_step": next_step,
        },
    })


def _check(svc, *, session, section_id, result, at, summary, command, exit_code, name="pytest", evidence_type="test", files=None):
    meta = {
        "sentinel_semantic_kind": "evidence", "client": "claude-code", "client_session_id": session,
        "section_id": section_id, "evidence_type": evidence_type, "result": result, "name": name,
        "summary": summary, "command": command, "exit_code": exit_code,
    }
    if files:
        meta["files"] = list(files)
    svc.record_event({
        "event_id": f"evt_evidence_{session}_{section_id}_{result}_{int(at)}",
        "created_at": float(at), "source": "claude-code", "event_type": "machine_check",
        "metadata": meta,
    })


def _limit7d(svc, *, captured, pct, client="claude-code", index=0):
    svc.record_event({
        "event_id": f"evt_rl_{client}_{index}", "created_at": float(captured), "source": client,
        "event_type": "rate_limit_observed",
        "metadata": {"client": client, "captured_at": float(captured),
                     "windows": [{"kind": "7d", "window_minutes": 10080, "used_percent": pct}]},
    })


def _tool_activity(svc, *, session, counts, names, touched, commands, at, client="claude-code"):
    """Seed ONE tool_activity_observed event (the exact shape the hook drain emits)
    so the Receipt's Actions dimension shows a real by-type breakdown + bars."""

    from agentacct.tool_activity import build_discovery_tool_activity_event

    event = build_discovery_tool_activity_event(
        client=client, session_id=session, captured_at=float(at),
        activity={
            "tool_category_counts": counts,
            "tool_names": [{"name": n, "count": c} for n, c in names],
            "touched_files": touched, "commands": commands,
        },
    )
    if event is not None:
        svc.record_event(event)


def _seed_sources(now):
    """Seed ingestion health so Sources shows three reporting sources + a running
    watcher, and the Dashboard's EVIDENCE TRUST reads 'Sources healthy'."""

    from agentacct.ingestion_health import IngestionHealthStore, importer_build_id

    ver = importer_build_id()
    health = IngestionHealthStore(STORE)
    # The watcher must start BEFORE the scan completes, and its importer version
    # must match the scan's, for the health surface to count the success as
    # "current" and read the source as Reporting (not Pending).
    health.acquire_watcher(
        lease_id="demo-watcher", pid=os.getpid(), importer_version=ver,
        interval_seconds=60, scan_limit=500,
        sources=["claude-code", "codex", "hermes"], now=now - 90,
    )
    scan = health.begin_scan(
        sources=["claude-code", "codex", "hermes"], scan_limit=500,
        importer_version=ver, started_at=now - 45,
    )
    health.complete_scan(scan, completed_at=now - 30, results={
        "claude-code": {"discovered": 1240, "parsed": 1200, "skipped": 40},
        "codex": {"discovered": 980, "parsed": 980},
        "hermes": {"discovered": 0, "parsed": 0},  # configured, no data yet
    })


def build_store():
    svc = SentinelService(STORE)

    # --- Backfilled daily usage across ~14 days: a real 90-day sparkline + history
    # total, plus the weekly capacity meter. All under ONE session id per client so
    # the backfill is a single 'Observed' receipt, not a dozen — the Work list stays
    # the showcase tasks. by-period bucketing keeps the daily sparkline bars. ---
    day = 86400
    ramp = [6, 7, 9, 8, 11, 10, 12, 9, 13, 11, 14, 12, 16, 15]  # Mtoken/day
    for d, mtok in enumerate(ramp):
        at = NOW - (len(ramp) - d) * day + 3600
        _usage(svc, client="claude-code", model=OPUS, session="cc-history",
               title="Recorded activity (history)", tokens=mtok * 1_000_000, at=at, cost=mtok * 3.4, project="agentacct")
    for d in range(6):
        _usage(svc, client="codex", model="gpt-5.6-sol", session="cx-history", title="Recorded activity (history)",
               tokens=5_200_000, at=NOW - (6 - d) * day + 5400, cost=13.7, project="agentacct-gui")
    _usage(svc, client="hermes", model=OPUS, session="hx-history", title="Recorded activity (history)",
           tokens=200_000, at=NOW - 2 * day, cost=2.5, project="agentacct")

    # --- Two live (in-progress) sessions so WORKING NOW is not empty. ---
    for sid, title, proj, at in (
        ("cc-live-1", "Wire the settings sync", "agentacct-gui", NOW - 240),
        ("cc-live-2", "Port the receipt cache", "agentacct", NOW - 480),
    ):
        _usage(svc, client="claude-code", model=OPUS, session=sid, title=title,
               tokens=1_800_000, at=at, cost=1.4, project=proj)
        _section(svc, session=sid, title="Plan the change", section_id=f"{sid}-plan",
                 status="completed", at=at - 60, project=proj, kind="planning", summary="Scoped the change.")
        _section(svc, session=sid, title=title, section_id=f"{sid}-impl", status="checkpoint",
                 at=at, project=proj, summary="Mid-implementation.")

    # --- Provider capacity: claude-code (53% headroom weekly), codex, hermes(none). ---
    svc.record_event({
        "event_id": "evt_rl_cc", "created_at": NOW - 120, "source": "claude-code",
        "event_type": "rate_limit_observed",
        "metadata": {"client": "claude-code", "captured_at": NOW - 120, "windows": [
            {"kind": "5h", "window_minutes": 300, "used_percent": 41.0},
            {"kind": "7d", "window_minutes": 10080, "used_percent": 47.0},
        ]},
    })
    svc.record_event({
        "event_id": "evt_rl_cx", "created_at": NOW - 300, "source": "codex", "event_type": "rate_limit_observed",
        "metadata": {"client": "codex", "captured_at": NOW - 300, "windows": [
            {"kind": "5h", "window_minutes": 300, "used_percent": 12.0, "resets_at": int(NOW + 9000)},
            {"kind": "7d", "window_minutes": 10080, "used_percent": 39.0, "resets_at": int(NOW + 300000)},
        ]},
    })

    # --- The four showcase receipts (mirroring the artifact's Work list). ---
    T = NOW - 1500  # recent

    # 1) Verified, richly instrumented — the receipt the Work screenshot opens on.
    _usage(svc, client="claude-code", model=OPUS, session="cc-harness",
           title="Build reusable snapshot harness", tokens=6_400_000, at=T, cost=4.82, project="agentacct")
    _section(svc, session="cc-harness", title="Design the deterministic fixture", section_id="cc-harness-design",
             status="completed", at=T - 400, project="agentacct", kind="planning",
             summary="Chose a fixed store + golden-SVG approach; scoped light & dark.")
    for step, (sid, title, kind, summ) in enumerate((
        ("cc-harness-fixture", "Build the fixture store", "implementation", "Seeded the six-session fixture."),
        ("cc-harness-render", "Render the four panes", "implementation", "Wired headless render of every pane."),
        ("cc-harness-golden", "Lock the golden SVGs", "testing", "Baselined light & dark goldens."),
        ("cc-harness-review", "Review + document", "review", "Addressed review notes; documented the harness."),
    )):
        _section(svc, session="cc-harness", title=title, section_id=sid, status="completed",
                 at=T - 300 + step * 60, project="agentacct", kind=kind, summary=summ)
    for sid, summ, cmd in (
        ("cc-harness-fixture", "6 passed", "pytest tests/test_fixture.py -q"),
        ("cc-harness-render", "12 passed", "pytest tests/test_render.py -q"),
        ("cc-harness-golden", "18 passed", "pytest tests/test_golden.py -q"),
        ("cc-harness-review", "ruff clean", "ruff check src/"),
    ):
        _check(svc, session="cc-harness", section_id=sid, result="passed", at=T + 40,
               summary=summ, command=cmd, exit_code=0, name="lint" if cmd.startswith("ruff") else "pytest")
    _check(svc, session="cc-harness", section_id="cc-harness-fixture", result="passed", at=T + 45,
           summary="6 passed", command="pytest tests/test_fixture.py::extra -q", exit_code=0)
    _check(svc, session="cc-harness", section_id="cc-harness-render", result="passed", at=T + 46,
           summary="4 passed", command="pytest tests/test_render.py::themes -q", exit_code=0)
    _tool_activity(svc, session="cc-harness", at=T + 50,
                   counts={"read": 38, "edit": 20, "execute": 14, "search": 8},
                   names=[("Read", 38), ("Edit", 20), ("Bash", 14), ("Grep", 8)],
                   touched=["scripts/gen_tui_screenshots.py", "src/agentacct/tui.py", "tests/test_tui.py"],
                   commands=["pytest -q", "ruff check src/"])

    # 2) Agent reported — steps done, not all checked (2/3).
    _usage(svc, client="claude-code", model=OPUS, session="cc-hierarchy",
           title="Rethink dashboard product hierarchy", tokens=2_900_000, at=NOW - 660, cost=2.16, project="agentacct-gui")
    for sid, title, kind, summ in (
        ("cc-hierarchy-plan", "Map the current hierarchy", "planning", "Catalogued every dashboard surface."),
        ("cc-hierarchy-impl", "Regroup into shift-brief order", "implementation", "Reordered into attention-first."),
        ("cc-hierarchy-copy", "Rewrite the section labels", "implementation", "New labels for each rail block."),
    ):
        _section(svc, session="cc-hierarchy", title=title, section_id=sid, status="completed",
                 at=NOW - 700, project="agentacct-gui", kind=kind, summary=summ)
    _check(svc, session="cc-hierarchy", section_id="cc-hierarchy-impl", result="passed", at=NOW - 650,
           summary="7 passed", command="pytest tests/test_dashboard.py -q", exit_code=0)
    _tool_activity(svc, session="cc-hierarchy", at=NOW - 650,
                   counts={"read": 12, "edit": 9, "search": 4},
                   names=[("Read", 12), ("Edit", 9), ("Grep", 4)],
                   touched=["apps/agentacct/Sources/agentacct/DashboardPane.swift"], commands=[])

    # 3) Blocked — the PRIMARY attention item (most recent), WITH a recorded next
    # step so the dashboard's PRIMARY ATTENTION card shows its next-step inset.
    _usage(svc, client="claude-code", model=OPUS, session="cc-calibration",
           title="Resolve provider calibration", tokens=900_000, at=NOW - 300, cost=None, project="agentacct")
    _section(svc, session="cc-calibration", title="Plan the calibration probe", section_id="cc-calibration-plan",
             status="completed", at=NOW - 350, project="agentacct", kind="planning",
             summary="Scoped a probe against the staging provider.")
    _section(svc, session="cc-calibration", title="Run the calibration probe", section_id="cc-calibration-impl",
             status="blocked", at=NOW - 300, project="agentacct",
             summary="Blocked before the probe could run.",
             blocker="staging DB credentials unavailable",
             next_step="Ask the owner to restore staging DB credentials, then re-run the probe.")

    # 4) Open finding — a failed check plus a spread of passing/skipped checks, so
    # its sessions & steps drill-down shows a full timeline (the artifact's frame).
    _usage(svc, client="claude-code", model=OPUS, session="cc-regression",
           title="Review dashboard visual regression", tokens=1_400_000, at=NOW - 720, cost=1.08, project="agentacct-gui")
    _section(svc, session="cc-regression", title="Plan the visual diff", section_id="cc-regression-plan",
             status="completed", at=NOW - 760, project="agentacct-gui", kind="planning",
             summary="Set up a snapshot diff against the reviewed reference.")
    _section(svc, session="cc-regression", title="Run the visual diff", section_id="cc-regression-impl",
             status="completed", at=NOW - 720, project="agentacct-gui",
             summary="The minimum-window snapshot differs from its reviewed reference.",
             files=["apps/agentacct/Sources/agentacct/DashboardPane.swift",
                    "apps/agentacct/Sources/agentacct/WorkPane.swift"])
    # The finding (a currently-failing check) → Needs attention.
    _check(svc, session="cc-regression", section_id="cc-regression-impl", result="failed", at=NOW - 700,
           summary="The minimum-window snapshot differs from its reviewed reference.",
           command="pytest tests/test_snapshot.py -q", exit_code=1, name="snapshot",
           files=["apps/agentacct/Sources/agentacct/DashboardPane.swift",
                  "apps/agentacct/Sources/agentacct/WorkPane.swift"])
    # Other current checks (passed / skipped) around it.
    # Distinct commands/scopes from the failed snapshot check, so none supersedes
    # the finding — it stays an OPEN finding that needs attention.
    for i, (result, kind, code, summ, cmd) in enumerate((
        ("passed", "artifact", 0, "Canonical light and dark references match on the pinned macOS renderer.", "ruff check src/"),
        ("passed", "build", 0, "The release app built successfully without replacing the installed application.", "swift build -c release"),
        ("passed", "test", 0, "Focused Swift tests passed with zero failures across the presentation matrix.", "swift test"),
        ("skipped", "test", None, "Optional Intel compatibility pass.", "swift test --filter Intel"),
        ("passed", "lint", 0, "Style and type checks are clean across the changed files.", "ruff check --select ALL src/"),
    )):
        _check(svc, session="cc-regression", section_id="cc-regression-impl", result=result,
               at=NOW - 690 + i * 5, summary=summ, command=cmd, exit_code=code,
               name=kind, evidence_type=kind)

    _seed_sources(NOW)
    return svc


async def shoot():
    build_store()
    app = AgentAcctTUI(store_dir=STORE, refresh_seconds=3600)
    async with app.run_test(size=(150, 60)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        await app.workers.wait_for_complete()  # the dashboard build worker
        await pilot.pause()
        app.save_screenshot(str(OUT / "tui-dashboard.svg"))

        await pilot.press("2")  # Work — the receipts list + master/detail
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        # Open on the verified, richly-instrumented receipt (as the artifact does)
        # so the detail showcases the outcome KPIs, summary strip, and tool bars.
        from textual.widgets import ListView as _LV
        for summary in app._work_summaries:
            if str(summary.get("title")) == "Build reusable snapshot harness":
                app._selected_task_id = str(summary.get("task_id"))
                app._render_work_list()
                break
        await pilot.pause()
        lv = app.query_one("#work-list", _LV)
        lv.focus()
        # Setting `index` right after an async clear()+append misses the not-yet-
        # mounted child, so light the selected card's highlight directly (live
        # keyboard navigation applies it on its own).
        if app._expanded_index is not None:
            for i, item in enumerate(lv.children):
                item.highlighted = (i == app._expanded_index)
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.pause()
        app.save_screenshot(str(OUT / "tui-work.svg"))

        # Sessions & steps — drill the finding receipt (mixed checks) into its
        # checks timeline (the artifact's sessions & steps frame).
        for summary in app._work_summaries:
            if str(summary.get("title")) == "Review dashboard visual regression":
                app._selected_task_id = str(summary.get("task_id"))
                app._render_work_list()
                break
        await pilot.pause()
        app._open_steps()
        await pilot.pause()
        await pilot.pause()
        app.save_screenshot(str(OUT / "tui-steps.svg"))
        app.action_steps_back()  # leave the pane in receipt mode
        await pilot.pause()

        # Usage and Sources hold less content than the dense dashboard/work views,
        # so — like the artifact, whose frames are each sized to their content —
        # render them in a shorter terminal. This keeps the two capacity/ingestion
        # cards filling the frame instead of floating over a tall black void.
        await pilot.press("3")  # Usage — capacity meters + recorded usage
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.resize_terminal(150, 50)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.save_screenshot(str(OUT / "tui-usage.svg"))

        await pilot.press("4")  # Sources — ingestion health
        await pilot.pause()
        await pilot.resize_terminal(150, 46)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.save_screenshot(str(OUT / "tui-sources.svg"))


def _chrome() -> str | None:
    for candidate in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _svg_size(svg_text: str) -> tuple[int, int]:
    """The SVG's natural pixel size from its viewBox, so Chrome renders the
    terminal 1:1 (no downscaling, no letterbox padding)."""
    m = re.search(r'viewBox="0 0 ([0-9.]+) ([0-9.]+)"', svg_text)
    if m:
        import math
        return math.ceil(float(m.group(1))), math.ceil(float(m.group(2)))
    return 1848, 1224


# Render each terminal at its exact natural size at 2x device scale: a big,
# crisp, retina-quality image that fills the frame — not a small window
# downscaled into a larger padded canvas.
_SCALE = 2


def to_png() -> None:
    chrome = _chrome()
    if not chrome:
        for name in SHOTS:
            print(f"  (no Chrome found) convert {name}.svg by hand with headless Chrome.")
        return
    # A real HOME + first-run flags: without them Chrome tries to set up a profile
    # under the isolated demo HOME and hangs.
    env = {**os.environ, "HOME": _REAL_HOME}
    for name in SHOTS:
        svg, png = OUT / f"{name}.svg", OUT / f"{name}.png"
        # Drop the remote (CDN) web-font src so Chrome renders offline with the
        # local mono instead of blocking ~20s per shot on a network fetch.
        text = re.sub(r'\s*url\("https?://[^"]*"\)\s*format\([^)]*\),?', "", svg.read_text(encoding="utf-8"))
        svg.write_text(text, encoding="utf-8")
        w, h = _svg_size(text)
        # A FRESH throwaway profile PER shot: a shared --user-data-dir makes a later
        # invocation contend on the previous run's profile lock and time out (this
        # reliably killed the last 1-2 shots). Retry once on any failure.
        for attempt in (1, 2):
            profile = tempfile.mkdtemp(prefix="agentacct-chrome-")
            try:
                subprocess.run(
                    [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
                     "--no-default-browser-check", f"--user-data-dir={profile}", "--hide-scrollbars",
                     f"--force-device-scale-factor={_SCALE}", f"--screenshot={png}",
                     f"--window-size={w},{h}", svg.as_uri()],
                    check=False, capture_output=True, timeout=90, env=env,
                )
            except subprocess.TimeoutExpired:
                pass
            finally:
                shutil.rmtree(profile, ignore_errors=True)
            if png.exists() and png.stat().st_size > 0:
                break
            if attempt == 2:
                print(f"  WARNING: Chrome failed on {name}.svg after 2 attempts")
    for name in SHOTS:  # SVGs are intermediate; keep only the PNGs
        (OUT / f"{name}.svg").unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(shoot())
    if os.environ.get("AGENTACCT_SHOTS_NO_PNG"):  # SVG-only: convert with your own Chrome
        print(f"wrote {', '.join(n + '.svg' for n in SHOTS)} to {OUT}")
    else:
        to_png()
        print(f"wrote {', '.join(n + '.png' for n in SHOTS)} to {OUT}")
    shutil.rmtree(_FAKE_HOME, ignore_errors=True)
