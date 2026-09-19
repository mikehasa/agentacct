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

# --- demo-content locale ------------------------------------------------------
# `--locale zh-CN` renders the same store with its CONTENT (task and step
# titles, summaries, blockers, check results) in Chinese and writes the assets
# to docs/assets/zh-CN/ for README.zh-CN.md. The app's own chrome stays English:
# that is what the product looks like, and file paths, commands, and model names
# are English in real use too.
LOCALE = "en"
_UNTRANSLATED: set[str] = set()

ZH_CN = {
    # flagship receipt
    "Add a token-bucket rate limiter to the login API": "给登录接口加一个令牌桶限流",
    "Design the token-bucket limiter": "设计令牌桶限流方案",
    "Chose a Redis token bucket — 100 req/min per IP, burst 20.": "选用 Redis 令牌桶：每个 IP 每分钟 100 次，突发上限 20。",
    "Write the failing tests": "先写会失败的测试",
    "Added 12 cases: under limit, at limit, burst, window reset.": "加了 12 个用例：未达上限、刚到上限、突发、窗口重置。",
    "Implement the token-bucket middleware": "实现令牌桶中间件",
    "Implemented the bucket + refill; the 12 tests pass.": "实现了令牌桶和补充逻辑，12 个测试通过。",
    "Handle bursts + concurrent requests": "处理突发和并发请求",
    "Made the refill atomic under concurrent hits (Lua CAS).": "并发场景下把补充改成原子操作（Lua CAS）。",
    "Code review + document the limits": "代码评审并把限流规则写进文档",
    "Addressed review comments; documented the limits in the API guide.": "处理了评审意见，限流规则已写进 API 文档。",
    "12 failed (red)": "12 个失败",
    "12 passed": "12 个通过",
    "38 passed": "38 个通过",
    "6 passed": "6 个通过",
    "ruff clean": "ruff 无告警",
    # the workday on billing-svc
    "Wire the Stripe webhook handler": "接入 Stripe webhook 处理",
    "Plan the webhook + idempotency keys": "设计 webhook 和幂等键",
    "Scoped the handler, replay protection, and the tests.": "定了处理逻辑、防重放和测试范围。",
    "Handler + signature check in; retries still to do.": "处理逻辑和签名校验已提交，重试还没做。",
    "14 passed": "14 个通过",
    "Trace the slow query path": "排查慢查询路径",
    "Trace the slow invoice query": "排查发票慢查询",
    "Found the missing index on invoices(account_id, created_at).": "发现 invoices(account_id, created_at) 缺索引。",
    "Paginate the invoices API": "给发票接口加分页",
    "Add cursor pagination to /invoices": "给 /invoices 加游标分页",
    "Cursor pagination + a covering index; p95 down 40%.": "游标分页加覆盖索引，p95 降了 40%。",
    "Rotate the webhook signing secret": "轮换 webhook 签名密钥",
    "Rotated the secret and updated the deploy config.": "换了密钥并更新了部署配置。",
    "Emit OTLP metrics from the API": "API 输出 OTLP 指标",
    "Plan the metrics + exporter": "设计指标和导出器",
    "Chose the OTLP exporter and the request/latency histograms.": "选定 OTLP 导出器和请求/延迟直方图。",
    "Instrumented the handlers; exporter wired to the collector.": "各处理函数已埋点，导出器接到了 collector。",
    "9 passed": "9 个通过",
    "Backfill the invoice index": "回填发票索引",
    "Backfilled 2.1M rows in batches; verified the covering index is used.": "分批回填了 210 万行，确认覆盖索引已生效。",
    "4 passed": "4 个通过",
    "Add a CSV export endpoint": "加一个 CSV 导出接口",
    "Add the /export.csv route + tests": "加 /export.csv 路由和测试",
    "Streamed the CSV; added 6 tests for quoting and large results.": "CSV 改为流式输出，补了 6 个引号和大结果集的测试。",
    "Fix the flaky payment test": "修复不稳定的支付测试",
    "Plan & write the tests": "梳理改动并补测试",
    "Scoped the change and the tests to add.": "确定了改动范围和要补的测试。",
    "Started, then hit a blocker on staging.": "开始后在 staging 环境卡住了。",
    "staging DB credentials unavailable": "拿不到 staging 数据库的凭证",
    "1 failed, 7 passed": "1 个失败，7 个通过",
    # the week's backdrop
    "Migrate the event log to SQLite": "把事件日志迁移到 SQLite",
    "Refactor the auth session store": "重构登录会话存储",
    "Add full-text search to the docs": "给文档加全文搜索",
    "Cache the dashboard queries": "缓存仪表盘查询",
    "Add the weekly usage report": "加每周用量报告",
    "Implemented the change.": "改动已完成。",
    "18 passed": "18 个通过",
    "Format + type-check the API package": "格式化并类型检查 API 包",
    "Formatted + type-checked the API package; zero new findings.": "API 包格式化和类型检查完成，没有新问题。",
    "Bump the pinned dependencies": "升级锁定的依赖",
    "Bumped 14 pinned dependencies; lockfile regenerated.": "升级了 14 个锁定依赖，重新生成了 lockfile。",
    "Quarantine a flaky integration test": "隔离一个不稳定的集成测试",
    "Investigate the perf regression": "排查性能回退",
    "Trace the N+1 query": "排查 N+1 查询",
    "Traced the N+1 in the order loader and cached it.": "在订单加载器里找到 N+1 并加了缓存。",
    "21 passed": "21 个通过",
    "Rebuild the search index": "重建搜索索引",
    "Rebuild the search index nightly": "搜索索引改成每晚重建",
    "Moved the index rebuild to an incremental nightly job.": "索引重建改成每晚增量任务。",
    "Add a health-check probe": "加一个健康检查探针",
    "Add /healthz + wire the probe": "加 /healthz 并接上探针",
    "Added a readiness probe and documented it.": "加了就绪探针并写了说明。",
    "Instrument the checkout funnel": "给结账漏斗加埋点",
    "Adding step events to the checkout funnel pages.": "正在给结账流程各页面加步骤事件。",
}


def _t(text):
    """Localize one piece of demo CONTENT (a title, summary, blocker, or check
    result). English is the identity; an untranslated string falls back to
    English and is reported at the end so the table can be completed."""
    if LOCALE == "en" or not text:
        return text
    table = ZH_CN if LOCALE == "zh-CN" else {}
    if text in table:
        return table[text]
    _UNTRANSLATED.add(text)
    return text
APP_BIN = REPO_ROOT / "apps" / "agentacct" / ".build" / "agentacct.app" / "Contents" / "MacOS" / "agentacct"

# Curated: the light-mode panes we surface in the README, renamed for the docs.
# Whole panes are copied 1:1 (CURATE); everything else is a crop of a raw render
# (CROPS below) — the receipt hero and timeline come out of one wide Sessions
# render, the Work card and the Usage capacity table out of their own panes.
#
# The tab labels: `worksets` (the pane loop writes it as window-work-*.png) is
# the "Work" tab — folder-anchored groupings across agents. The receipts
# collection (window-work-table-*.png, rendered task-unselected) is the
# "Sessions" tab. Keep those two straight when re-measuring crops.
CURATE = {
    "window-dashboard-light.png": "app-dashboard.png",
}

# The Work Receipt is the README hero. SnapshotRunner renders the flagship record
# once more at a WIDE canvas (window-work-wide-light.png) sized so the receipt
# fills it without the timeline card stretching (a shorter frame keeps the
# offscreen ScrollBox from top-padding a flexible child into a tall empty band).
# The single-column receipt reads top to bottom: verdict + summary strip, the
# activity timeline (steps + their checks over time), then the Usage / Cost /
# Weekly plan / Sessions / Recording dimensions. We crop two docs assets — the
# hero (verdict + timeline) and the dimensions ledger — from the detail column
# (right of the ~320 pt master list · 2 + 1 px divider). Regions are
# (left, top, right, bottom) in device pixels; None means the render's own edge.
# Re-measure if the demo store or the receipt layout changes materially (the raw
# render is kept in SHOTS_TMP for exactly that).
WIDE_SRC = "window-work-wide-light.png"
# The wide render's frame height in points. It must sit at the record's natural
# height: the offscreen ScrollBox pins content to the top, so a taller frame
# stretches the flexible timeline card into an empty band, and a shorter one
# clips the supporting sections. SnapshotRunner reads it from the environment.
WIDE_HEIGHT = 1993
# Which steps the receipt opens in the docs render: "<with checks>,<without>".
# One expanded step keeps the spine short enough for the hero to fit the step
# spine and the activity timeline in one screenshot.
EXPANDED_STEPS = "1,0"
# Crops out of the raw renders: dst -> (raw render, (left, top, right, bottom)).
# Regions are device pixels; None means the render's own edge.
CROPS = {
    # Hero: the task list, then the open receipt — title + verdict, the Steps /
    # Checks outcome bars, the numbered step spine (one step expanded), and the
    # activity timeline card — from the wide Sessions render.
    "app-work-receipt.png": (WIDE_SRC, (0, 130, None, 2760)),
    # The Work tab: the nav bar, the page title, and the first (workday) card.
    "app-work.png": ("window-work-light.png", (0, 0, None, 1122)),
    # Usage: the page title and the per-client Current capacity table only.
    "app-usage.png": ("window-usage-light.png", (0, 0, None, 1640)),
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

def _usage(svc, *, client, model, session, title, tokens, at, cost, project, cache_read=None, started_at=None):
    # Agents with prompt caching read far more from cache than they spend fresh;
    # default the cache-read tokens to a realistic multiple of the fresh tokens so
    # the Usage pane's CACHE READ stat reflects real usage instead of a bare 0.
    if cache_read is None:
        cache_read = int(tokens * 4)
    ev = ClientUsageEvent(
        client=client, client_session_id=session,
        source_path=Path(f"/demo/{client}/{session}.jsonl"), title=_t(title), cwd=f"/demo/{project}",
        model=model, input_tokens=tokens, output_tokens=0, cached_input_tokens=0,
        cache_creation_input_tokens=0, cache_read_input_tokens=cache_read,
        cache_creation_tokens_reported=True, cache_read_tokens_reported=True,
        reasoning_output_tokens=0, provider_name=client,
        started_at=int(started_at if started_at is not None else at), updated_at=int(at),
        turn_count=1, usage_row_lane=f"model:{model}", source_namespace_fingerprint=f"sha256:{client}",
        input_tokens_reported=True, output_tokens_reported=True, reasoning_output_tokens_reported=True,
        total_tokens=tokens, total_tokens_reported=True,
    ).to_sentinel_event()
    ev["estimated_cost_usd"] = cost
    ev["cost_confidence"] = "estimated_from_tokens"
    # The demo's costs are pricing-table estimates — the truth table says these
    # clients never report a billed cost, so the receipt basis must agree.
    ev["cost_basis"] = "pricing_table"
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
            "demo_occurred_at": float(at),
            "project_dir": f"/demo/{project}", "section_id": section_id, "section_status": status,
            "section_title": _t(title), "summary": _t(summary), "kind": kind,
            "files": files if files is not None else ["src/app/module.py"], "blocker": _t(blocker), "next_step": None,
        },
    })


def _check(svc, *, session, section_id, result, at, summary, command, exit_code, client="claude-code", name="pytest"):
    svc.record_event({
        "event_id": f"evt_evidence_{session}_{section_id}_{result}_{int(at)}",
        "created_at": float(at), "source": client, "event_type": "machine_check",
        "metadata": {
            "sentinel_semantic_kind": "evidence", "client": client, "client_session_id": session,
            "demo_occurred_at": float(at),
            "section_id": section_id, "evidence_type": "test", "result": result, "name": name,
            "summary": _t(summary), "command": command, "exit_code": exit_code,
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
            "demo_occurred_at": float(at),
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
    # claude-code fresh-token volumes sit in the low millions per session (what a
    # real heavy day looks like); the weekly-plan chain starts at a base % and
    # each reading's DELTA is FRESH_W x the tracked tokens since the last, which
    # is all the calibrator needs to fit cleanly.
    # billing-svc's sessions live in yesterday's WORKDAY block below (the Work
    # tab's shared-axis timeline needs sessions that span hours and overlap).
    cc_sessions = [
        ("cc-sqlite",  "Migrate the event log to SQLite",    2_500_000, 38.0, "handed_off", "agentacct",   6),
        ("cc-auth",    "Refactor the auth session store",    1_400_000, 21.0, "checkpoint", "acme-web",    6),
        ("cc-search",  "Add full-text search to the docs",   2_000_000, 30.0, "completed",  "acme-web",    5),
        ("cc-cache",   "Cache the dashboard queries",        1_300_000, 20.0, "completed",  "acme-web",    3),
        ("cc-report",  "Add the weekly usage report",        1_100_000, 17.0, "completed",  "agentacct",   1),
    ]
    cc_sessions.sort(key=lambda s: -s[6])  # oldest (largest days_ago) first
    at_by: dict[str, float] = {}
    for i, (sid, title, tokens, cost, status, project, days_ago) in enumerate(cc_sessions):
        at = _clock(days_ago, 14) + (i % 3) * 2400
        at_by[sid] = at
        _usage(svc, client="claude-code", model=OPUS, session=sid, title=title, tokens=tokens, at=at, cost=cost,
               project=project, started_at=at - 2 * 3600)
        _section(svc, session=sid, title="Plan & write the tests", section_id=f"{sid}-plan",
                 status="completed", at=at - 300, project=project, kind="planning",
                 summary="Scoped the change and the tests to add.")
        _section(svc, session=sid, title=title, section_id=f"{sid}-impl", status=status, at=at, project=project,
                 summary="Implemented the change." if status != "blocked" else "Started, then hit a blocker on staging.",
                 blocker="staging DB credentials unavailable" if status == "blocked" else None)
    _check(svc, session="cc-report", section_id="cc-report-impl", result="passed", at=at_by["cc-report"] + 120,
           summary="18 passed", command="pytest tests/test_report.py -q", exit_code=0)

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
        ("cc-tune-fmt",   "Format + type-check the API package",  0.8, 12.0, 11.0),
        ("cc-tune-deps",  "Bump the pinned dependencies",         0.6,  9.0,  8.0),
        ("cc-tune-flaky", "Quarantine a flaky integration test",  0.7,  6.0,  5.0),
    ]
    # The chain starts at the plan % the provider already shows for this week
    # (usage on other devices / before the tracked window — the calibrator only
    # needs consecutive DELTAS to match FRESH_W x the tracked tokens between
    # readings). Readings keep 3 decimals so small deltas survive rounding.
    pct = 58.0
    _limit7d(svc, captured=NOW - 14 * HOUR, pct=round(pct, 3), index=200)
    tune_sections = {
        "cc-tune-fmt": ("Formatted + type-checked the API package; zero new findings.", "review"),
        "cc-tune-deps": ("Bumped 14 pinned dependencies; lockfile regenerated.", "implementation"),
        # cc-tune-flaky stays usage-only: an honest `Observed` row (activity
        # recorded, no assertion) belongs in the demo too.
    }
    for j, (sid, title, mtok, use_h, read_h) in enumerate(tune):
        at_tune = NOW - use_h * HOUR
        _usage(svc, client="claude-code", model=OPUS, session=sid, title=title,
               tokens=int(mtok * 1_000_000), at=at_tune, cost=round(mtok * 14.5, 2), project="agentacct")
        if sid in tune_sections:
            summary, kind = tune_sections[sid]
            _section(svc, session=sid, title=title, section_id=f"{sid}-1",
                     status="completed", at=at_tune + 240, project="agentacct",
                     kind=kind, summary=summary)
        pct += FRESH_W * mtok
        _limit7d(svc, captured=NOW - read_h * HOUR, pct=round(pct, 3), index=201 + j)
    pct += FRESH_W * 9.5  # the flagship's 9.5M fresh tokens close the chain

    # The current 5h + 7d reading (the flagship interval's endpoint), so the
    # The merged Usage pane shows both windows and calibrated plan details.
    _rl(svc, client="claude-code", captured=NOW - 120, index=99, windows=[
        {"kind": "5h", "window_minutes": 300, "used_percent": 34.0, "resets_at": int(NOW + 9000)},
        {"kind": "7d", "window_minutes": 10080, "used_percent": round(pct, 1),
         "resets_at": int(NOW + 300000)},
    ])

    # ---- Codex — three runs spread across the week, each a comparable bar so no
    # single session dwarfs the daily chart (the old single 1.4B run did) ----
    cx_at = _clock(2, 16)
    _usage(svc, client="codex", model="gpt-5.6-sol", session="cx-perf", title="Investigate the perf regression",
           tokens=6_500_000, at=cx_at, cost=4.20, project="acme-web", started_at=cx_at - 2 * 3600)
    cx_index_at = _clock(4, 15)
    _usage(svc, client="codex", model="gpt-5.6-sol", session="cx-index", title="Rebuild the search index",
           tokens=5_800_000, at=cx_index_at, cost=3.70, project="acme-web", started_at=cx_index_at - 2 * 3600)
    # Codex's meter is a rolling window, so its reset time is independent of
    # Claude's — sharing one resets_at constant read as copy-paste fake data.
    _rl(svc, client="codex", captured=NOW - 300, index=0, windows=[
        {"kind": "5h", "window_minutes": 300, "used_percent": 12.0, "resets_at": int(NOW + 4200)},
        {"kind": "7d", "window_minutes": 10080, "used_percent": 63.0, "resets_at": int(NOW + 121000)},
    ])
    _section(svc, session="cx-perf", title="Trace the N+1 query", section_id="cx-perf-1",
             status="completed", at=cx_at, client="codex", project="acme-web", kind="debugging",
             summary="Traced the N+1 in the order loader and cached it.")
    _section(svc, session="cx-index", title="Rebuild the search index nightly", section_id="cx-index-1",
             status="completed", at=cx_index_at, client="codex", project="acme-web", kind="implementation",
             summary="Moved the index rebuild to an incremental nightly job.")
    _check(svc, session="cx-perf", section_id="cx-perf-1", result="passed", at=cx_at + 200,
           summary="21 passed", command="pytest tests/test_orders.py -q", exit_code=0, client="codex")
    _tool_activity(svc, session="cx-perf", at=cx_at + 100, client="codex", basis="transcript_scan_tool_activity",
                   categories={"read": 14, "edit": 3, "execute": 6, "search": 5},
                   names=[("read_file", 14), ("apply_patch", 3), ("exec_command", 6), ("grep", 5)],
                   touched=["src/orders/loader.py", "src/orders/cache.py"],
                   commands=["pytest tests/test_orders.py -q", "python -m pyinstrument bench/orders.py"])

    seed_workday(svc)

    # ---- Hermes (recent) ----
    _usage(svc, client="hermes", model="claude-sonnet-5", session="hm-infra", title="Add a health-check probe",
           tokens=900_000, at=NOW - 3000, cost=0.62, project="agentacct")
    _section(svc, session="hm-infra", title="Add /healthz + wire the probe", section_id="hm-infra-1",
             status="completed", at=NOW - 3000, client="hermes", project="agentacct",
             summary="Added a readiness probe and documented it.", files=["src/server/health.py"])
    _tool_activity(svc, session="hm-infra", at=NOW - 2950, client="hermes", basis="client_hook_tool_category",
                   categories={"read": 5, "edit": 2, "execute": 3},
                   names=[("terminal", 3), ("str_replace", 2), ("read_file", 5)],
                   touched=["src/server/health.py"], commands=["pytest tests/test_health.py -q"])

    # ---- A session that is active RIGHT NOW (Active work card + a live
    # in-progress row in the table's In progress tab) ----
    _usage(svc, client="claude-code", model=OPUS, session="cc-funnel",
           title="Instrument the checkout funnel", tokens=800_000, at=NOW - 700,
           cost=11.0, project="acme-web")
    _section(svc, session="cc-funnel", title="Instrument the checkout funnel", section_id="cc-funnel-impl",
             status="started", at=NOW - 700, project="acme-web",
             summary="Adding step events to the checkout funnel pages.",
             files=["src/web/checkout/analytics.ts"])

    # ---- Flagship Claude Code Receipt (MOST RECENT -> receiptTasks.first) ----
    # The detailed page: seven steps (its drill-down shows the trail), a
    # red->green + lint check series, and a focused Actions dimension (two files).
    f = "cc-ratelimit"
    fbase = NOW - 1500
    RL = "src/api/middleware/ratelimit.py"
    TST = "tests/test_ratelimit.py"
    _usage(svc, client="claude-code", model=OPUS, session=f,
           title="Add a token-bucket rate limiter to the login API",
           tokens=9_500_000, at=NOW - 200, cost=118.0, project="acme-web")
    steps = [
        ("design",  "Design the token-bucket limiter",      "planning",       [RL],
         "Chose a Redis token bucket — 100 req/min per IP, burst 20."),
        ("tests",   "Write the failing tests",              "testing",        [TST],
         "Added 12 cases: under limit, at limit, burst, window reset."),
        ("impl",    "Implement the token-bucket middleware", "implementation", [RL],
         "Implemented the bucket + refill; the 12 tests pass."),
        ("burst",   "Handle bursts + concurrent requests",  "implementation", [RL],
         "Made the refill atomic under concurrent hits (Lua CAS)."),
        ("review",  "Code review + document the limits",    "review",         [RL],
         "Addressed review comments; documented the limits in the API guide."),
    ]
    for i, (sid, title, kind, files, summary) in enumerate(steps):
        _section(svc, session=f, title=title, section_id=f"{f}-{sid}", status="completed",
                 at=fbase + 60 + i * 180, project="acme-web", kind=kind, summary=summary, files=files)
    # The honest arc: red first, then green — and the FINAL quality gate runs
    # after the last work update, so every live check actually postdates the
    # newest code (older greens are superseded by the closing runs).
    _check(svc, session=f, section_id=f"{f}-tests", result="failed", at=fbase + 250,
           summary="12 failed (red)", command="pytest tests/test_ratelimit.py -q", exit_code=1)
    _check(svc, session=f, section_id=f"{f}-impl", result="passed", at=fbase + 440,
           summary="12 passed", command="pytest tests/ -q", exit_code=0)
    # Closing quality gate (after the review step completes at fbase+780):
    _check(svc, session=f, section_id=f"{f}-tests", result="passed", at=fbase + 850,
           summary="12 passed", command="pytest tests/test_ratelimit.py -q", exit_code=0)
    _check(svc, session=f, section_id=f"{f}-impl", result="passed", at=fbase + 860,
           summary="38 passed", command="pytest tests/ -q", exit_code=0)
    _check(svc, session=f, section_id=f"{f}-burst", result="passed", at=fbase + 870,
           summary="6 passed", command="pytest tests/test_ratelimit.py -k concurrency -q",
           exit_code=0, name="pytest -k concurrency")
    _check(svc, session=f, section_id=f"{f}-review", result="passed", at=fbase + 880,
           summary="ruff clean", command="ruff check src/", exit_code=0, name="lint")
    _tool_activity(svc, session=f, at=fbase + 700, client="claude-code", basis="client_hook_tool_category",
                   categories={"read": 24, "edit": 9, "execute": 11, "search": 6, "plan": 3},
                   names=[("Read", 24), ("Edit", 9), ("Bash", 11), ("Grep", 6), ("TodoWrite", 3)],
                   touched=[RL, TST],
                   commands=["pytest tests/test_ratelimit.py -q", "ruff check src/", "git diff --stat"])

    seed_worksets(svc)
    return svc


# ---- Yesterday's workday on billing-svc (the Work tab's timeline) -------------
# The Work tab draws one bar per session from its first to its last activity on
# a shared axis, so the group has to look like a real day: long runs, short
# runs nested inside them, and runs from different agents overlapping. Anchored
# to yesterday's local wall clock so the axis reads 09:05 → 18:20.

def _clock(days_ago, hour, minute=0):
    """Epoch for ``hour:minute`` local wall-clock time ``days_ago`` days back.
    Anchoring on the wall clock (not on NOW minus hours) keeps a session on the
    calendar day it belongs to whatever time of day the script runs, so the
    seven-day charts have a bar every day and the workday axis reads 09:05."""
    import datetime as _dt
    day = (_dt.datetime.fromtimestamp(NOW) - _dt.timedelta(days=days_ago)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return (day + _dt.timedelta(hours=hour, minutes=minute)).timestamp()


def _workday_clock(hour, minute=0):
    return _clock(1, hour, minute)


def seed_workday(svc):
    P = "billing-svc"
    c = _workday_clock

    # A. Claude Code, 09:05–12:40 — the long morning run (still open at day end).
    _usage(svc, client="claude-code", model=OPUS, session="cc-webhook", title="Wire the Stripe webhook handler",
           tokens=1_500_000, at=c(12, 40), cost=23.0, project=P, started_at=c(9, 5))
    _section(svc, session="cc-webhook", title="Plan the webhook + idempotency keys", section_id="cc-webhook-plan",
             status="completed", at=c(9, 20), project=P, kind="planning",
             summary="Scoped the handler, replay protection, and the tests.")
    _section(svc, session="cc-webhook", title="Wire the Stripe webhook handler", section_id="cc-webhook-impl",
             status="checkpoint", at=c(12, 35), project=P,
             summary="Handler + signature check in; retries still to do.", files=["src/billing/webhooks.py"])
    _check(svc, session="cc-webhook", section_id="cc-webhook-impl", result="passed", at=c(11, 50),
           summary="14 passed", command="pytest tests/test_webhook.py -q", exit_code=0)

    # B. Codex, 09:40–10:25 — nested inside A.
    _usage(svc, client="codex", model="gpt-5.6-sol", session="cx-trace", title="Trace the slow query path",
           tokens=4_600_000, at=c(10, 25), cost=2.90, project=P, started_at=c(9, 40))
    _section(svc, session="cx-trace", title="Trace the slow invoice query", section_id="cx-trace-1",
             status="completed", at=c(10, 20), client="codex", project=P, kind="debugging",
             summary="Found the missing index on invoices(account_id, created_at).")

    # C. OpenCode, 11:20–13:50 — overlaps the end of A.
    _usage(svc, client="opencode", model="gpt-5.6-nova", session="oc-invoices", title="Paginate the invoices API",
           tokens=1_900_000, at=c(13, 50), cost=1.30, project=P, started_at=c(11, 20))
    _section(svc, session="oc-invoices", title="Add cursor pagination to /invoices", section_id="oc-invoices-1",
             status="completed", at=c(13, 45), client="opencode", project=P,
             summary="Cursor pagination + a covering index; p95 down 40%.",
             files=["src/billing/invoices.py"])

    # D. Hermes, 12:05–12:20 — a short run nested inside A and C.
    _usage(svc, client="hermes", model="claude-sonnet-5", session="hm-secret", title="Rotate the webhook signing secret",
           tokens=300_000, at=c(12, 20), cost=0.21, project=P, started_at=c(12, 5))
    _section(svc, session="hm-secret", title="Rotate the webhook signing secret", section_id="hm-secret-1",
             status="completed", at=c(12, 18), client="hermes", project=P, kind="other",
             summary="Rotated the secret and updated the deploy config.", files=["deploy/secrets.yml"])

    # E. Claude Code, 13:30–17:10 — the long afternoon run.
    _usage(svc, client="claude-code", model=OPUS, session="cc-metrics", title="Emit OTLP metrics from the API",
           tokens=1_600_000, at=c(17, 10), cost=24.0, project=P, started_at=c(13, 30))
    _section(svc, session="cc-metrics", title="Plan the metrics + exporter", section_id="cc-metrics-plan",
             status="completed", at=c(13, 40), project=P, kind="planning",
             summary="Chose the OTLP exporter and the request/latency histograms.")
    _section(svc, session="cc-metrics", title="Emit OTLP metrics from the API", section_id="cc-metrics-impl",
             status="completed", at=c(17, 5), project=P,
             summary="Instrumented the handlers; exporter wired to the collector.", files=["src/api/metrics.py"])
    _check(svc, session="cc-metrics", section_id="cc-metrics-impl", result="passed", at=c(16, 40),
           summary="9 passed", command="pytest tests/test_metrics.py -q", exit_code=0)

    # F. Codex, 14:15–16:00 — nested inside E.
    _usage(svc, client="codex", model="gpt-5.6-sol", session="cx-backfill", title="Backfill the invoice index",
           tokens=3_100_000, at=c(16, 0), cost=2.00, project=P, started_at=c(14, 15))
    _section(svc, session="cx-backfill", title="Backfill the invoice index", section_id="cx-backfill-1",
             status="completed", at=c(15, 55), client="codex", project=P,
             summary="Backfilled 2.1M rows in batches; verified the covering index is used.")
    _check(svc, session="cx-backfill", section_id="cx-backfill-1", result="passed", at=c(15, 50),
           summary="4 passed", command="pytest tests/test_index_backfill.py -q", exit_code=0, client="codex")

    # G. OpenCode, 16:30–18:20 — overlaps the end of E; discovery-side Actions + an independent check.
    _usage(svc, client="opencode", model="gpt-5.6-luna", session="oc-export", title="Add a CSV export endpoint",
           tokens=3_200_000, at=c(18, 20), cost=2.30, project=P, started_at=c(16, 30))
    _section(svc, session="oc-export", title="Add the /export.csv route + tests", section_id="oc-export-1",
             status="completed", at=c(18, 15), client="opencode", project=P,
             summary="Streamed the CSV; added 6 tests for quoting and large results.",
             files=["src/billing/export.py", "tests/test_export.py"])
    _tool_activity(svc, session="oc-export", at=c(17, 30), client="opencode", basis="transcript_scan_tool_activity",
                   categories={"read": 9, "edit": 4, "execute": 5, "search": 3},
                   names=[("read", 9), ("apply_patch", 4), ("bash", 5), ("glob", 3)],
                   touched=["src/billing/export.py", "tests/test_export.py"],
                   commands=["npm test", "npm run build"])
    _check(svc, session="oc-export", section_id="oc-export-1", result="passed", at=c(18, 10),
           summary="6 passed", command="npm test", exit_code=0, client="opencode")

    # H. Claude Code, 17:40–18:15 — short, nested inside G, and blocked (the
    # Dashboard's primary attention item).
    _usage(svc, client="claude-code", model=OPUS, session="cc-pay", title="Fix the flaky payment test",
           tokens=1_100_000, at=c(18, 15), cost=17.0, project=P, started_at=c(17, 40))
    _section(svc, session="cc-pay", title="Plan & write the tests", section_id="cc-pay-plan",
             status="completed", at=c(17, 45), project=P, kind="planning",
             summary="Scoped the change and the tests to add.")
    _section(svc, session="cc-pay", title="Fix the flaky payment test", section_id="cc-pay-impl",
             status="blocked", at=c(18, 12), project=P,
             summary="Started, then hit a blocker on staging.",
             blocker="staging DB credentials unavailable")
    _check(svc, session="cc-pay", section_id="cc-pay-impl", result="failed", at=c(18, 5),
           summary="1 failed, 7 passed", command="pytest tests/test_payment.py -q", exit_code=1)


# ---- Worksets (the Work tab) --------------------------------------------------
# Folder-anchored groupings the user defines: a workset says "the sessions under
# this folder are one piece of work" and gathers a project's runs across every
# agent onto one shared timeline. It never re-grades a session — each keeps its
# own receipt; the group is a labeled sum of independently-attributed parts.
#
# `project_identity` is NOT the raw path: a session's identity is
# `project:{leaf}:{sha256(path)[:16]}` (agentacct.work_ledger._project_identity),
# and the workset's stored project_identity must be byte-identical to it or
# membership resolves empty. Seeded via record_workset_action (the only trusted
# workset-stamping path — a workset from a raw record_event is stripped). Two
# groups so the pane renders a populated list; billing-svc is created last so,
# ordered newest-first, its 3-agent (Claude Code + Codex + OpenCode) card leads.

def _workset_identity(project: str) -> str:
    """The hashed session identity for `/demo/{project}`, computed from the real
    projection function so the seed can never drift from what membership joins on."""
    from agentacct.work_ledger import _project_identity
    return _project_identity(f"/demo/{project}")


def seed_worksets(svc):
    svc.record_workset_action(
        action="create", workset_id="ws-acme-web", name="acme-web",
        project_identity=_workset_identity("acme-web"),
        expected_revision=0, idempotency_key="demo-workset-acme-web",
    )
    svc.record_workset_action(
        action="create", workset_id="ws-billing-svc", name="billing-svc",
        project_identity=_workset_identity("billing-svc"),
        expected_revision=0, idempotency_key="demo-workset-billing-svc",
    )


def backdate_ledger():
    """Rewrite the throwaway demo ledger's server-stamped ``created_at`` to the
    scripted wall times, so recency ("2h ago", "4d ago") reads like a real
    week of work instead of a store seeded seconds before the render. The
    scripted time rides in each event: ``demo_occurred_at`` on sections /
    checks / tool activity, the usage row's own ``updated_at`` otherwise.
    Demo-store-only surgery — the real store is never touched."""
    import json as _json
    import sqlite3

    db = STORE / "events.sqlite3"
    con = sqlite3.connect(db)
    rows = con.execute("SELECT seq, line FROM event_lines").fetchall()
    changed = 0
    for seq, line in rows:
        ev = _json.loads(line)
        meta = ev.get("metadata") or {}
        at = meta.get("demo_occurred_at") or meta.get("updated_at")
        if not at:
            continue
        ev["created_at"] = float(at)
        con.execute(
            "UPDATE event_lines SET created_at = ?, line = ? WHERE seq = ?",
            (float(at), _json.dumps(ev, sort_keys=True), seq),
        )
        changed += 1
    con.commit()
    con.close()
    print(f"  backdated {changed} ledger events to their scripted times")


def seed_ingestion():
    """Seed the ingestion-health state the Sources pane renders: recent
    successful scans for each agent's source plus a live watcher lease held by
    THIS process (the pid stays alive through the render, so the watcher reads
    `running` and healthy sources earn their green `Reporting` lozenge)."""
    from agentacct.ingestion_health import IngestionHealthStore

    health = IngestionHealthStore(STORE)
    sources = ["claude-code", "codex", "opencode", "hermes"]
    # The lease starts BEFORE the seeded scan completes: a source is only
    # `healthy` when its last success lands inside the current watcher's tenure.
    health.acquire_watcher(
        lease_id="demo-watcher", pid=os.getpid(), importer_version="demo",
        interval_seconds=60.0, scan_limit=400, sources=sources,
        now=NOW - 300,
    )
    scan = health.begin_scan(sources=sources, scan_limit=400,
                             importer_version="demo", pid=os.getpid(),
                             started_at=NOW - 40)
    health.complete_scan(scan, completed_at=NOW - 32, results={
        "claude-code": {"discovered": 412, "parsed": 405, "skipped": 7,
                        "returned_rows": 405, "observed_sessions": 61, "usage_sessions": 58},
        "codex": {"discovered": 118, "parsed": 118,
                  "returned_rows": 118, "observed_sessions": 19, "usage_sessions": 19},
        "opencode": {"discovered": 37, "parsed": 37,
                     "returned_rows": 37, "observed_sessions": 8, "usage_sessions": 8},
        "hermes": {"discovered": 22, "parsed": 20, "skipped": 2,
                   "returned_rows": 20, "observed_sessions": 5, "usage_sessions": 5},
    })
    health.heartbeat_watcher("demo-watcher")


def seed_activation():
    """Seed the activation record the Diagnostics pane's per-agent Connections
    card joins against: agentacct "set up" the four demo agents, so their live,
    healthy sources read as Recording. Unlisted agents stay honest — dsh renders
    as not connected (with its Connect action), OpenClaw and Cursor as read-only."""
    from agentacct.activation import ActivationStateStore
    from agentacct.cli import _package_version

    ActivationStateStore(STORE).mark_configured(
        project_dir=FAKE_HOME, clients=["claude-code", "codex", "opencode", "hermes"],
        configured_at=NOW - 3 * DAY, agentacct_version=_package_version(),
    )


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
    backdate_ledger()
    seed_ingestion()
    seed_activation()

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
        app_env = {**os.environ, "AGENTACCT_STORE_DIR": str(STORE),
                   "AGENTACCT_SNAPSHOT_WIDE_HEIGHT": str(WIDE_HEIGHT),
                   "AGENTACCT_SNAPSHOT_EXPANDED_STEPS": EXPANDED_STEPS}
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
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from frame_screenshots import frame
    from PIL import Image

    curated = []
    for src_name, dst_name in CURATE.items():
        src = SHOTS_TMP / src_name
        if not src.exists():
            print(f"  WARNING: missing {src_name}")
            continue
        shutil.copyfile(src, OUT / dst_name)
        curated.append(dst_name)

    # Crop the receipt hero, the timeline, the Work card, and the Usage capacity
    # table out of their raw renders (see CROPS). Cropping the region, then
    # framing, makes each read like its own window in the docs.
    for dst_name, (src_name, (left, top, right, bottom)) in CROPS.items():
        src = SHOTS_TMP / src_name
        if not src.exists():
            print(f"  WARNING: missing {src_name} (for {dst_name})")
            continue
        raw = Image.open(src)
        box = (left, top, raw.width if right is None else right,
               raw.height if bottom is None else bottom)
        raw.crop(box).save(OUT / dst_name)
        curated.append(dst_name)
    print(f"curated -> {OUT}: {', '.join(curated)}")

    # Wrap each curated asset in the macOS window chrome (frame_screenshots.py is
    # a sibling in scripts/, on sys.path when this runs as a script).
    print("framing in the macOS window chrome…")
    for name in curated:
        frame(OUT / name)

    print(f"(all raw panes light+dark are in {SHOTS_TMP})")
    shutil.rmtree(FAKE_HOME, ignore_errors=True)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--locale", choices=["en", "zh-CN"], default="en",
                        help="language of the demo CONTENT (titles, summaries, check results); "
                             "zh-CN writes to docs/assets/zh-CN/ for README.zh-CN.md")
    args = parser.parse_args()
    LOCALE = args.locale
    if LOCALE != "en":
        OUT = OUT / LOCALE
    main()
    if _UNTRANSLATED:
        print(f"WARNING: {len(_UNTRANSLATED)} demo strings have no {LOCALE} translation (rendered in English):")
        for text in sorted(_UNTRANSLATED):
            print(f"  - {text}")
