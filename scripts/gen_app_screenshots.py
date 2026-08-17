#!/usr/bin/env python3
"""Regenerate the README's macOS-app screenshots from SYNTHETIC demo data.

Builds a throwaway store of invented sessions across all four supported agents
(Claude Code, Codex, OpenCode, Hermes) — recorded work steps, tool-activity
Actions, machine-check evidence, usage, and provider limits — with one rich
flagship Work Receipt. It stands up a daemon on that demo store, points the app
at it (``AGENTACCT_STORE_DIR``), and runs the app's built-in ``--snapshot`` mode
to render each pane offscreen. Every pixel is synthetic; the real store/daemon is
never touched (the demo store lives under a fixed fake HOME and has its own
discovery file + port).

    PYTHONPATH=src <venv>/python scripts/gen_app_screenshots.py

Requires: agentacct importable (run from a clone with dev deps), and a built app
binary at apps/agentacct/.build/agentacct.app/Contents/MacOS/agentacct
(build with `apps/agentacct/Scripts/build-app.sh`). macOS 14+ (SwiftUI ImageRenderer).
The curated light-mode PNGs are copied into docs/assets/.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_HOME = "/tmp/agentacct-app-demo-home"
STORE = Path(FAKE_HOME) / ".local" / "state" / "agentacct" / "state"
SHOTS_TMP = Path("/tmp/agentacct-app-shots")
OUT = REPO_ROOT / "docs" / "assets"
APP_BIN = REPO_ROOT / "apps" / "agentacct" / ".build" / "agentacct.app" / "Contents" / "MacOS" / "agentacct"

# Curated: the light-mode panes we surface in the README, renamed for the docs.
CURATE = {
    "window-dashboard-light.png": "app-dashboard.png",
    "window-work-light.png": "app-work-receipt.png",
    "window-usage-light.png": "app-usage.png",
}

sys.path.insert(0, str(REPO_ROOT / "src"))
from agentacct.client_usage import ClientUsageEvent  # noqa: E402
from agentacct.plan_cost import BASELINE_MODEL_WEIGHTS, baseline_weight_fresh  # noqa: E402
from agentacct.service import SentinelService  # noqa: E402

NOW = time.time()
DAY = 86400
OPUS = "claude-opus-4-8"
# Re-anchored FRESH-component weekly-plan weight (% of plan per 1M fresh tokens).
# The plan-cost calibrator predicts each interval's movement against THIS weight,
# so the seeded 7-day readings must use it too (a raw-weight series fits a scale
# ~8x below the trusted band and never calibrates).
FRESH_W = baseline_weight_fresh(OPUS)


# --- event helpers (mirror the shapes the receipt/glance projections read) ----

def _usage(svc, *, client, model, session, title, tokens, at, cost, project, cache_read=None):
    # Agents with prompt caching read far more from cache than they spend fresh;
    # default the cache-read tokens to a realistic multiple of the fresh tokens so
    # the Usage pane's CACHE READ stat reflects real usage instead of a bare 0.
    if cache_read is None:
        cache_read = int(tokens * 4)
    ev = ClientUsageEvent(
        client=client, client_session_id=session,
        source_path=Path(f"/demo/{client}/{session}.jsonl"), title=title, cwd=f"/demo/{project}",
        model=model, input_tokens=tokens, output_tokens=0, cached_input_tokens=0,
        cache_creation_input_tokens=0, cache_read_input_tokens=cache_read,
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
            "files": files if files is not None else ["src/app/module.py"], "blocker": blocker, "next_step": None,
        },
    })


def _check(svc, *, session, section_id, result, at, summary, command, exit_code, client="claude-code", name="pytest"):
    svc.record_event({
        "event_id": f"evt_evidence_{session}_{section_id}_{result}_{int(at)}",
        "created_at": float(at), "source": client, "event_type": "machine_check",
        "metadata": {
            "sentinel_semantic_kind": "evidence", "client": client, "client_session_id": session,
            "section_id": section_id, "evidence_type": "test", "result": result, "name": name,
            "summary": summary, "command": command, "exit_code": exit_code,
        },
    })


def _tool_activity(svc, *, session, at, client="claude-code", basis="client_hook_tool_category",
                   categories=None, names=None, touched=None, commands=None):
    """A tool_activity_observed event — the Actions dimension. ``basis`` drives the
    honest provenance label (client_hook_tool_category -> hook, transcript_scan_
    tool_activity -> transcript scan)."""
    svc.record_event({
        "event_id": f"evt_toolact_{session}_{int(at)}",
        "created_at": float(at), "source": client, "event_type": "tool_activity_observed", "run_id": None,
        "metadata": {
            "sentinel_semantic_kind": "tool_activity", "client": client, "client_session_id": session,
            "capture_basis": basis, "captured_at": float(at),
            "tool_category_counts": categories or {},
            "tool_names": [{"name": n, "count": c} for n, c in (names or [])],
            "touched_files": touched or [],
            "commands": commands or [],
        },
    })


def _rl(svc, *, client, captured, windows, index=0):
    svc.record_event({
        "event_id": f"evt_rl_{client}_{index}", "created_at": float(captured), "source": client,
        "event_type": "rate_limit_observed",
        "metadata": {"client": client, "captured_at": float(captured), "windows": windows},
    })


def _limit7d(svc, *, captured, pct, client="claude-code", index=0):
    _rl(svc, client=client, captured=captured, index=index,
        windows=[{"kind": "7d", "window_minutes": 10080, "used_percent": pct}])


# --- the synthetic ledger -----------------------------------------------------

def build_store():
    svc = SentinelService(STORE)

    # Claude Code backdrop, spread across the last week (``days_ago``) so the DAILY
    # FRESH TOKENS chart has bars every day, not just today. Sorted oldest-first
    # below so the weekly-plan % series climbs monotonically and each reading's
    # delta matches the tokens since the last — a clean calibration.
    # claude-code fresh-token volumes are kept modest on purpose: the calibrated
    # weekly-plan weight (FRESH_W) times the TOTAL cc fresh tokens this week is the
    # attributed "% of this week", and that must stay well under 100% to read as a
    # healthy meter. Backdrop (~250M) + tunes (~155M) + flagship (210M) => ~65%.
    cc_sessions = [
        ("cc-sqlite",  "Migrate the event log to SQLite",    50_000_000, 38.0, "handed_off", "agentacct",   6),
        ("cc-auth",    "Refactor the auth session store",    28_000_000, 21.0, "checkpoint", "acme-web",    6),
        ("cc-metrics", "Emit OTLP metrics from the API",     32_000_000, 24.0, "completed",  "billing-svc", 5),
        ("cc-search",  "Add full-text search to the docs",   40_000_000, 30.0, "completed",  "acme-web",    4),
        ("cc-webhook", "Wire the Stripe webhook handler",    30_000_000, 23.0, "checkpoint", "billing-svc", 3),
        ("cc-cache",   "Cache the dashboard queries",        26_000_000, 20.0, "completed",  "acme-web",    2),
        ("cc-report",  "Add the weekly usage report",        22_000_000, 17.0, "completed",  "agentacct",   1),
        ("cc-pay",     "Fix the flaky payment test",         22_000_000, 17.0, "blocked",    "billing-svc", 1),
    ]
    cc_sessions.sort(key=lambda s: -s[6])  # oldest (largest days_ago) first
    at_by: dict[str, float] = {}
    for i, (sid, title, tokens, cost, status, project, days_ago) in enumerate(cc_sessions):
        at = NOW - days_ago * DAY - 5 * 3600 + (i % 3) * 2400
        at_by[sid] = at
        _usage(svc, client="claude-code", model=OPUS, session=sid, title=title, tokens=tokens, at=at, cost=cost, project=project)
        _section(svc, session=sid, title="Plan & write the tests", section_id=f"{sid}-plan",
                 status="completed", at=at - 300, project=project, kind="planning",
                 summary="Scoped the change and the tests to add.")
        _section(svc, session=sid, title=title, section_id=f"{sid}-impl", status=status, at=at, project=project,
                 summary="Implemented the change." if status != "blocked" else "Started, then hit a blocker on staging.",
                 blocker="staging DB credentials unavailable" if status == "blocked" else None)
    _check(svc, session="cc-report", section_id="cc-report-impl", result="passed", at=at_by["cc-report"] + 120,
           summary="18 passed", command="pytest tests/test_report.py -q", exit_code=0)
    _check(svc, session="cc-pay", section_id="cc-pay-impl", result="failed", at=at_by["cc-pay"] + 120,
           summary="1 failed, 7 passed", command="pytest tests/test_payment.py -q", exit_code=1)

    # ---- Weekly-plan calibration chain (claude-code) -------------------------
    # The plan-cost estimator only calibrates from consecutive 7-day-limit
    # readings <=12h apart with tracked tokens between them, and only trusts a fit
    # whose scale lands in [0.5, 2.5]. So seed a short, self-consistent chain over
    # the last ~14h: a few small claude-code runs, each followed by a reading whose
    # delta is exactly the re-anchored fresh weight times that run's tokens (=>
    # scale 1.0, cache-read discount 0). The flagship (+210M, below) is the last
    # link, so the meter reads calibrated and TODAY - TRACKED PLAN shows a real
    # number instead of "calibration pending". These runs are usage-only (no
    # sections), so they add plan history without cluttering the Work list.
    HOUR = 3600.0
    tune = [
        ("cc-tune-fmt",   "Format + type-check the API package",  55, 12.0, 11.0),
        ("cc-tune-deps",  "Bump the pinned dependencies",         48,  9.0,  8.0),
        ("cc-tune-flaky", "Quarantine a flaky integration test",  52,  6.0,  5.0),
    ]
    # Start the chain at the plan % the backdrop sessions (all older than this
    # window) already consumed, so the final reading = FRESH_W x TOTAL cc fresh
    # tokens = the attributed "% of this week" the ring shows (no >100% overflow).
    backdrop_m = sum(s[2] for s in cc_sessions) / 1_000_000.0
    pct = FRESH_W * backdrop_m
    _limit7d(svc, captured=NOW - 14 * HOUR, pct=round(pct, 2), index=200)
    for j, (sid, title, mtok, use_h, read_h) in enumerate(tune):
        _usage(svc, client="claude-code", model=OPUS, session=sid, title=title,
               tokens=mtok * 1_000_000, at=NOW - use_h * HOUR, cost=round(mtok * 0.76, 2), project="agentacct")
        pct += FRESH_W * mtok
        _limit7d(svc, captured=NOW - read_h * HOUR, pct=round(pct, 2), index=201 + j)
    pct += FRESH_W * 210  # the flagship's 210M fresh tokens close the chain

    # The current 5h + 7d reading (the flagship interval's endpoint), so the
    # Limits pane shows both windows and the weekly meter reads calibrated.
    _rl(svc, client="claude-code", captured=NOW - 120, index=99, windows=[
        {"kind": "5h", "window_minutes": 300, "used_percent": 34.0, "resets_at": int(NOW + 9000)},
        {"kind": "7d", "window_minutes": 10080, "used_percent": round(pct, 1),
         "resets_at": int(NOW + 300000)},
    ])

    # ---- Codex — three runs spread across the week, each a comparable bar so no
    # single session dwarfs the daily chart (the old single 1.4B run did) ----
    cx_at = NOW - 2 * DAY - 3 * 3600
    _usage(svc, client="codex", model="gpt-5.6-sol", session="cx-perf", title="Investigate the perf regression",
           tokens=520_000_000, at=cx_at, cost=2.30, project="acme-web")
    _usage(svc, client="codex", model="gpt-5.6-sol", session="cx-index", title="Rebuild the search index",
           tokens=480_000_000, at=NOW - 4 * DAY - 3 * 3600, cost=2.13, project="acme-web")
    _usage(svc, client="codex", model="gpt-5.6-sol", session="cx-trace", title="Trace the slow query path",
           tokens=400_000_000, at=NOW - 5 * DAY - 3 * 3600, cost=1.77, project="billing-svc")
    _rl(svc, client="codex", captured=NOW - 300, index=0, windows=[
        {"kind": "5h", "window_minutes": 300, "used_percent": 12.0, "resets_at": int(NOW + 9000)},
        {"kind": "7d", "window_minutes": 10080, "used_percent": 63.0, "resets_at": int(NOW + 300000)},
    ])
    _section(svc, session="cx-perf", title="Trace the N+1 query", section_id="cx-perf-1",
             status="completed", at=cx_at, client="codex", project="acme-web", kind="debugging",
             summary="Traced the N+1 in the order loader and cached it.")
    _section(svc, session="cx-index", title="Rebuild the search index nightly", section_id="cx-index-1",
             status="completed", at=NOW - 4 * DAY - 3 * 3600, client="codex", project="acme-web", kind="implementation",
             summary="Moved the index rebuild to an incremental nightly job.")
    _section(svc, session="cx-trace", title="Trace the slow invoice query", section_id="cx-trace-1",
             status="completed", at=NOW - 5 * DAY - 3 * 3600, client="codex", project="billing-svc", kind="debugging",
             summary="Found the missing index on invoices(account_id, created_at).")
    _tool_activity(svc, session="cx-perf", at=cx_at + 100, client="codex", basis="transcript_scan_tool_activity",
                   categories={"read": 14, "edit": 3, "execute": 6, "search": 5},
                   names=[("read_file", 14), ("apply_patch", 3), ("exec_command", 6), ("grep", 5)],
                   touched=["src/orders/loader.py", "src/orders/cache.py"],
                   commands=["pytest tests/test_orders.py -q", "python -m pyinstrument bench/orders.py"])

    # ---- OpenCode (yesterday) — discovery-side Actions + an independent check ----
    oc_at = NOW - DAY - 4 * 3600
    _usage(svc, client="opencode", model="gpt-5.6-luna", session="oc-export", title="Add a CSV export endpoint",
           tokens=520_000_000, at=oc_at, cost=2.30, project="billing-svc")
    _section(svc, session="oc-export", title="Add the /export.csv route + tests", section_id="oc-export-1",
             status="completed", at=oc_at, client="opencode", project="billing-svc",
             summary="Streamed the CSV; added 6 tests for quoting and large results.",
             files=["src/billing/export.py", "tests/test_export.py"])
    _tool_activity(svc, session="oc-export", at=oc_at + 100, client="opencode", basis="transcript_scan_tool_activity",
                   categories={"read": 9, "edit": 4, "execute": 5, "search": 3},
                   names=[("read", 9), ("apply_patch", 4), ("bash", 5), ("glob", 3)],
                   touched=["src/billing/export.py", "tests/test_export.py"],
                   commands=["npm test", "npm run build"])
    _check(svc, session="oc-export", section_id="oc-export-1", result="passed", at=oc_at + 150,
           summary="6 passed", command="npm test", exit_code=0, client="opencode")
    # A second OpenCode run mid-week (a 5th model, and a bar on an otherwise thin day).
    _usage(svc, client="opencode", model="gpt-5.6-nova", session="oc-invoices", title="Paginate the invoices API",
           tokens=220_000_000, at=NOW - 3 * DAY - 4 * 3600, cost=0.97, project="billing-svc")
    _section(svc, session="oc-invoices", title="Add cursor pagination to /invoices", section_id="oc-invoices-1",
             status="completed", at=NOW - 3 * DAY - 4 * 3600, client="opencode", project="billing-svc",
             summary="Cursor pagination + a covering index; p95 down 40%.",
             files=["src/billing/invoices.py"])

    # ---- Hermes (recent) ----
    _usage(svc, client="hermes", model="claude-sonnet-5", session="hm-infra", title="Add a health-check probe",
           tokens=140_000_000, at=NOW - 3000, cost=0.62, project="agentacct")
    _section(svc, session="hm-infra", title="Add /healthz + wire the probe", section_id="hm-infra-1",
             status="completed", at=NOW - 3000, client="hermes", project="agentacct",
             summary="Added a readiness probe and documented it.", files=["src/server/health.py"])
    _tool_activity(svc, session="hm-infra", at=NOW - 2950, client="hermes", basis="client_hook_tool_category",
                   categories={"read": 5, "edit": 2, "execute": 3},
                   names=[("terminal", 3), ("str_replace", 2), ("read_file", 5)],
                   touched=["src/server/health.py"], commands=["pytest tests/test_health.py -q"])

    # ---- Flagship Claude Code Receipt (MOST RECENT -> receiptTasks.first) ----
    # The detailed page: seven steps (its drill-down shows the trail), a
    # red->green + lint check series, and a focused Actions dimension (two files).
    f = "cc-ratelimit"
    fbase = NOW - 1500
    RL = "src/api/middleware/ratelimit.py"
    TST = "tests/test_ratelimit.py"
    _usage(svc, client="claude-code", model=OPUS, session=f,
           title="Add a token-bucket rate limiter to the login API",
           tokens=210_000_000, at=NOW - 200, cost=161.0, project="acme-web")
    steps = [
        ("design",  "Design the token-bucket limiter",      "planning",       [RL],
         "Chose a Redis token bucket — 100 req/min per IP, burst 20."),
        ("tests",   "Write the failing tests",              "testing",        [TST],
         "Added 12 cases: under limit, at limit, burst, window reset."),
        ("impl",    "Implement the token-bucket middleware", "implementation", [RL],
         "Implemented the bucket + refill; the 12 tests pass."),
        ("window",  "Add the sliding-window reset",         "implementation", [RL],
         "Reset the bucket on the window boundary, monotonic clock."),
        ("burst",   "Handle bursts + concurrent requests",  "implementation", [RL],
         "Made the refill atomic under concurrent hits (Lua CAS)."),
        ("keys",    "Key limits per IP and per account",    "implementation", [RL],
         "Composite key so an authed account and its IP are limited apart."),
        ("review",  "Code review + document the limits",    "review",         [RL],
         "Addressed review comments; documented the limits in the API guide."),
    ]
    for i, (sid, title, kind, files, summary) in enumerate(steps):
        _section(svc, session=f, title=title, section_id=f"{f}-{sid}", status="completed",
                 at=fbase + 60 + i * 180, project="acme-web", kind=kind, summary=summary, files=files)
    _check(svc, session=f, section_id=f"{f}-tests", result="failed", at=fbase + 250,
           summary="12 failed (red)", command="pytest tests/test_ratelimit.py -q", exit_code=1)
    _check(svc, session=f, section_id=f"{f}-impl", result="passed", at=fbase + 440,
           summary="12 passed", command="pytest tests/test_ratelimit.py -q", exit_code=0)
    _check(svc, session=f, section_id=f"{f}-burst", result="passed", at=fbase + 800,
           summary="18 passed", command="pytest tests/test_ratelimit.py -q", exit_code=0)
    _check(svc, session=f, section_id=f"{f}-review", result="passed", at=fbase + 1150,
           summary="ruff clean", command="ruff check src/", exit_code=0, name="lint")
    _tool_activity(svc, session=f, at=fbase + 1300, client="claude-code", basis="client_hook_tool_category",
                   categories={"read": 24, "edit": 9, "execute": 11, "search": 6, "plan": 3},
                   names=[("Read", 24), ("Edit", 9), ("Bash", 11), ("Grep", 6), ("TodoWrite", 3)],
                   touched=[RL, TST],
                   commands=["pytest tests/test_ratelimit.py -q", "ruff check src/", "git diff --stat"])

    # Bonus: an INDEPENDENT (client_hook) check on the flagship impl step, so its
    # evidence tier reads `independently checked` — the strong local signal.
    try:
        from agentacct.mechanical_capture import (
            build_discovery_mechanical_check_envelopes, command_digest,
        )
        env = build_discovery_mechanical_check_envelopes([{
            "c": "claude-code", "s": f, "k": "test", "r": "pytest",
            "d": command_digest("pytest tests/test_ratelimit.py -q"), "x": 0, "t": fbase + 830,
        }])
        for e in env:
            svc.evidence.append(e)
    except Exception as exc:  # best-effort: a self-checked flagship still screenshots well.
        print(f"  (independent-check injection skipped: {exc})")

    return svc


# --- orchestration ------------------------------------------------------------

def _daemon_env():
    env = {**os.environ, "HOME": FAKE_HOME,
           "PYTHONPATH": str(REPO_ROOT / "src"),
           "AGENTACCT_TUI_AUTO_IMPORT": "0", "AGENTACCT_SCAN_GLOBAL_LIMITS": "0"}
    for var in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "AGENTACCT_STORE_DIR",
                "AGENTACCT_GLOBAL_STORE_DIR", "CODEX_HOME", "OPENCODE_DATA_DIR", "HERMES_HOME",
                "OPENCLAW_DIR", "CURSOR_HOME"):
        env.pop(var, None)
    return env


def main():
    shutil.rmtree(FAKE_HOME, ignore_errors=True)
    STORE.mkdir(parents=True, exist_ok=True)
    print("seeding synthetic 4-agent store…")
    build_store()

    if not APP_BIN.exists():
        sys.exit(f"app binary not found: {APP_BIN}\n  build it first: apps/agentacct/Scripts/build-app.sh")

    disc = STORE / "local-api.json"
    disc.unlink(missing_ok=True)
    print("starting demo daemon…")
    daemon = subprocess.Popen([sys.executable, "-m", "agentacct.cli", "serve", "--store-dir", str(STORE)],
                              env=_daemon_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            if disc.exists():
                break
            if daemon.poll() is not None:
                sys.exit("demo daemon exited before writing its discovery file")
            time.sleep(0.5)
        else:
            sys.exit("demo daemon never wrote its discovery file")
        time.sleep(1.0)  # let the first /v1 projection warm

        shutil.rmtree(SHOTS_TMP, ignore_errors=True)
        SHOTS_TMP.mkdir(parents=True, exist_ok=True)
        print("rendering app panes (offscreen)…")
        # Real HOME for the app process (GUI/WindowServer); AGENTACCT_STORE_DIR
        # is the only thing that points it at the demo store.
        app_env = {**os.environ, "AGENTACCT_STORE_DIR": str(STORE)}
        r = subprocess.run([str(APP_BIN), "--snapshot", str(SHOTS_TMP)], env=app_env,
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            sys.exit(f"snapshot failed (exit {r.returncode}):\n{r.stdout}\n{r.stderr}")
        print(r.stdout.strip() or "snapshot done")
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()

    OUT.mkdir(parents=True, exist_ok=True)
    curated = []
    for src_name, dst_name in CURATE.items():
        src = SHOTS_TMP / src_name
        if not src.exists():
            print(f"  WARNING: missing {src_name}")
            continue
        shutil.copyfile(src, OUT / dst_name)
        curated.append(dst_name)
    print(f"curated -> {OUT}: {', '.join(curated)}")

    # Wrap each curated pane in the macOS window chrome (frame_screenshots.py is
    # a sibling in scripts/, on sys.path when this runs as a script).
    print("framing in the macOS window chrome…")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from frame_screenshots import frame
    for name in curated:
        frame(OUT / name)

    print(f"(all raw panes light+dark are in {SHOTS_TMP})")
    shutil.rmtree(FAKE_HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
