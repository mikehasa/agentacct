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
from agentacct.tui import AgentAcctTUI, SessionDetailScreen, SessionsScreen, UsageScreen  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else REPO_ROOT / "docs" / "assets"
OUT.mkdir(parents=True, exist_ok=True)
STORE = Path(_FAKE_HOME) / ".local" / "state" / "agentacct" / "state"

NOW = time.time()
OPUS = "claude-opus-4-8"
OPUS_W = BASELINE_MODEL_WEIGHTS[OPUS]  # %/Mtoken
SHOTS = ("tui-home", "tui-sessions", "tui-session-detail", "tui-usage")


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
             kind="implementation", summary="", blocker=None, files=None):
    svc.record_event({
        "event_id": f"evt_section_{session}_{section_id}_{status}",
        "created_at": float(at), "source": client, "event_type": f"section_{status}", "run_id": None,
        "metadata": {
            "sentinel_semantic_kind": "section", "client": client, "client_session_id": session,
            "client_transcript_id": session,
            "client_context_keys_authored": ["client_session_id", "client_transcript_id"],
            "project_dir": f"/demo/{project}", "section_id": section_id, "section_status": status,
            "section_title": title, "summary": summary, "kind": kind,
            "files": files or ["src/app/module.py"], "blocker": blocker, "next_step": None,
        },
    })


def _check(svc, *, session, section_id, result, at, summary, command, exit_code, name="pytest"):
    svc.record_event({
        "event_id": f"evt_evidence_{session}_{section_id}_{result}_{int(at)}",
        "created_at": float(at), "source": "claude-code", "event_type": "machine_check",
        "metadata": {
            "sentinel_semantic_kind": "evidence", "client": "claude-code", "client_session_id": session,
            "section_id": section_id, "evidence_type": "test", "result": result, "name": name,
            "summary": summary, "command": command, "exit_code": exit_code,
        },
    })


def _limit7d(svc, *, captured, pct, client="claude-code", index=0):
    svc.record_event({
        "event_id": f"evt_rl_{client}_{index}", "created_at": float(captured), "source": client,
        "event_type": "rate_limit_observed",
        "metadata": {"client": client, "captured_at": float(captured),
                     "windows": [{"kind": "7d", "window_minutes": 10080, "used_percent": pct}]},
    })


def build_store():
    svc = SentinelService(STORE)
    # Calibrated weekly-plan series: hourly Opus intervals that move the meter exactly
    # as the baseline predicts (scale ~1.0 -> calibrated), one per named session so
    # each shows a real plan %.
    base = NOW - 6 * 3600
    sessions = [
        ("cc-login", "Add rate-limit to the login endpoint", 250_000_000, 190.0, "completed", "acme-web"),
        ("cc-auth", "Refactor the auth session store", 120_000_000, 92.0, "checkpoint", "acme-web"),
        ("cc-sqlite", "Migrate the event log to SQLite", 300_000_000, 228.0, "handed_off", "agentacct"),
        ("cc-pay", "Fix the flaky payment test", 90_000_000, 70.0, "blocked", "billing-svc"),
        ("cc-report", "Add the weekly usage report", 60_000_000, 46.0, "completed", "agentacct"),
    ]
    pct = 0.0
    _limit7d(svc, captured=base, pct=pct, index=0)
    svc.record_event({  # a 5h reading so the limits panel shows both windows
        "event_id": "evt_rl_cc_5h", "created_at": NOW - 120, "source": "claude-code",
        "event_type": "rate_limit_observed",
        "metadata": {"client": "claude-code", "captured_at": NOW - 120, "windows": [
            {"kind": "5h", "window_minutes": 300, "used_percent": 41.0},
            {"kind": "7d", "window_minutes": 10080, "used_percent": 0.0},
        ]},
    })
    for i, (sid, title, tokens, cost, status, project) in enumerate(sessions):
        mid = base + i * 3600 + 1800
        _usage(svc, client="claude-code", model=OPUS, session=sid, title=title, tokens=tokens, at=mid, cost=cost, project=project)
        pct += OPUS_W * (tokens / 1_000_000.0)
        _limit7d(svc, captured=base + (i + 1) * 3600, pct=pct, index=i + 1)
        _section(svc, session=sid, title="Plan & write the tests", section_id=f"{sid}-plan",
                 status="completed", at=mid - 300, project=project, kind="planning",
                 summary="Scoped the change and the tests to add.")
        _section(svc, session=sid, title=title, section_id=f"{sid}-impl", status=status, at=mid, project=project,
                 summary="Implemented the change." if status != "blocked" else "Started, then hit a blocker on staging.",
                 blocker="staging DB credentials unavailable" if status == "blocked" else None)
    _check(svc, session="cc-login", section_id="cc-login-impl", result="passed", at=base + 1 * 3600 - 60,
           summary="42 passed", command="pytest tests/test_login.py -q", exit_code=0)
    _check(svc, session="cc-report", section_id="cc-report-impl", result="passed", at=base + 5 * 3600 - 60,
           summary="18 passed", command="pytest tests/test_report.py -q", exit_code=0)
    _check(svc, session="cc-pay", section_id="cc-pay-impl", result="failed", at=base + 4 * 3600 - 60,
           summary="1 failed, 7 passed", command="pytest tests/test_payment.py -q", exit_code=1)

    # a codex session — no plan estimate (honest '-'), shows tokens/cost + a step
    _usage(svc, client="codex", model="gpt-5.6-sol", session="cx-perf", title="Investigate the perf regression",
           tokens=1_400_000_000, at=NOW - 5400, cost=6.20, project="acme-web")
    svc.record_event({
        "event_id": "evt_rl_cx", "created_at": NOW - 300, "source": "codex", "event_type": "rate_limit_observed",
        "metadata": {"client": "codex", "captured_at": NOW - 300, "windows": [
            {"kind": "5h", "window_minutes": 300, "used_percent": 12.0, "resets_at": int(NOW + 9000)},
            {"kind": "7d", "window_minutes": 10080, "used_percent": 63.0, "resets_at": int(NOW + 300000)},
        ]},
    })
    _section(svc, session="cx-perf", title="Investigate the perf regression", section_id="cx-perf-1",
             status="completed", at=NOW - 5400, client="codex", kind="debugging",
             summary="Traced the N+1 query and cached it.")
    return svc


async def shoot():
    build_store()
    app = AgentAcctTUI(store_dir=STORE, refresh_seconds=3600)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.save_screenshot(str(OUT / "tui-home.svg"))

        await pilot.press("s")
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.save_screenshot(str(OUT / "tui-sessions.svg"))

        scr = app.screen
        if isinstance(scr, SessionsScreen):
            entry = scr._by_key.get("claude-code::cc-login") or next(iter(scr._by_key.values()))
            app.push_screen(SessionDetailScreen(entry))
            await pilot.pause()
            from textual.widgets import Collapsible  # expand steps to show check evidence
            for c in app.screen.query(Collapsible):
                c.collapsed = False
            await pilot.pause()
            app.save_screenshot(str(OUT / "tui-session-detail.svg"))
            app.pop_screen()
            await pilot.pause()

        app.push_screen(UsageScreen(range_index=1))
        await pilot.pause()
        app.screen._client_filter = "claude-code"
        app.screen._rebuild()
        await pilot.pause()
        app.save_screenshot(str(OUT / "tui-usage.svg"))


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


def to_png() -> None:
    chrome = _chrome()
    if not chrome:
        for name in SHOTS:
            print(f"  (no Chrome found) convert by hand: <chrome> --headless=new "
                  f"--screenshot={OUT / (name + '.png')} --window-size=1482,1030 {OUT / (name + '.svg')}")
        return
    # A THROWAWAY profile + a real HOME + first-run flags: without them Chrome tries
    # to set up a profile under the isolated demo HOME and hangs.
    profile = tempfile.mkdtemp(prefix="agentacct-chrome-")
    env = {**os.environ, "HOME": _REAL_HOME}
    try:
        for name in SHOTS:
            svg, png = OUT / f"{name}.svg", OUT / f"{name}.png"
            # Drop the remote (CDN) web-font src so Chrome renders offline with the
            # local mono instead of blocking ~20s per shot on a network fetch.
            svg.write_text(
                re.sub(r'\s*url\("https?://[^"]*"\)\s*format\([^)]*\),?', "", svg.read_text(encoding="utf-8")),
                encoding="utf-8",
            )
            try:
                subprocess.run(
                    [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
                     "--no-default-browser-check", f"--user-data-dir={profile}", "--hide-scrollbars",
                     "--force-device-scale-factor=1", f"--screenshot={png}",
                     "--window-size=1482,1030", svg.as_uri()],
                    check=False, capture_output=True, timeout=60, env=env,
                )
            except subprocess.TimeoutExpired:
                print(f"  WARNING: Chrome timed out on {name}.svg")
    finally:
        shutil.rmtree(profile, ignore_errors=True)
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
