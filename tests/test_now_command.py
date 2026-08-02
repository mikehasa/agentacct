"""Tests for `agentacct now` (current usage/cost snapshot on the event-log cube)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from typer.testing import CliRunner

from agentacct.cli import app
from agentacct.client_usage import ClientUsageEvent
from agentacct.service import SentinelService


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
    # trusted_usage_import=True stamps the usage-source markers the reader requires.
    service.record_event(event, trusted_usage_import=True)


def test_now_empty_store(tmp_path):
    result = CliRunner().invoke(app, ["now", "--store-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "No usage recorded yet" in result.stdout


def test_now_windows_breakdown_and_cost(tmp_path):
    service = SentinelService(tmp_path)
    now = time.time()
    # ~2 days old → in 7d/30d/all, NOT in 24h
    _record_usage(
        service, client="claude-code", model="claude-opus-4-8", session_id="s-recent",
        input_tokens=1000, output_tokens=200, updated_at=int(now - 2 * 86400), estimated_cost_usd=3.0,
    )
    # ~10 days old → in 30d/all, NOT in 7d/24h
    _record_usage(
        service, client="codex", model="gpt-5", session_id="s-old",
        input_tokens=500, output_tokens=100, updated_at=int(now - 10 * 86400), estimated_cost_usd=1.0,
    )

    result = CliRunner().invoke(app, ["now", "--store-dir", str(tmp_path), "--window", "7d", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)

    windows = {w["window"]: w["totals"] for w in payload["windows"]}
    assert windows["today"].get("total_tokens_including_cached", 0) == 0
    # last 7 days: only the recent (2-day-old) event
    assert windows["last 7 days"]["total_tokens_including_cached"] == 1200
    assert windows["last 7 days"]["estimated_cost_usd"] == 3.0
    assert windows["last 7 days"]["cost_complete"] is True
    # last 30 days + all: both events
    assert windows["last 30 days"]["total_tokens_including_cached"] == 1800
    assert windows["all time"]["total_tokens_including_cached"] == 1800

    # breakdown window is 7d → only claude-code present
    clients = {row["client"] for row in payload["by_client"]}
    assert clients == {"claude-code"}


def test_now_client_filter(tmp_path):
    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(
        service, client="claude-code", model="claude-opus-4-8", session_id="c1",
        input_tokens=100, output_tokens=50, updated_at=int(now - 3600), estimated_cost_usd=0.5,
    )
    _record_usage(
        service, client="codex", model="gpt-5", session_id="x1",
        input_tokens=80, output_tokens=20, updated_at=int(now - 3600), estimated_cost_usd=0.2,
    )
    result = CliRunner().invoke(
        app, ["now", "--store-dir", str(tmp_path), "--window", "7d", "--client", "codex", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["client_filter"] == "codex"
    assert {row["client"] for row in payload["by_client"]} == {"codex"}
    assert payload["windows"][1]["totals"]["total_tokens_including_cached"] == 100  # last 7d, codex only


def test_now_human_render_smoke(tmp_path):
    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(
        service, client="claude-code", model="claude-opus-4-8", session_id="h1",
        input_tokens=1000, output_tokens=500, updated_at=int(now - 3600), estimated_cost_usd=2.5,
    )
    result = CliRunner().invoke(app, ["now", "--store-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "agentacct now" in out
    assert "last 7 days" in out and "all time" in out
    assert "by client" in out
    assert "claude-opus-4-8" in out  # top models


def test_now_cost_text_uses_cost_complete():
    from agentacct.cli import _now_cost_text

    # complete → plain $; the presence of estimated_cost_usd alone is NOT enough.
    assert _now_cost_text({"cost_complete": True, "estimated_cost_usd": 4.0, "known_additive_cost_usd": 4.0}) == "$4.00"
    # priced subtotal present but NOT complete (unpriced rows) → partial with ~.
    assert _now_cost_text({"cost_complete": False, "estimated_cost_usd": 4.0, "known_additive_cost_usd": 4.0}) == "~$4.00"
    # nothing priced → em-dash
    assert _now_cost_text({"cost_complete": False, "estimated_cost_usd": None, "known_additive_cost_usd": None}) == "—"
    # non-finite degrades to em-dash (no $nan)
    assert _now_cost_text({"cost_complete": True, "estimated_cost_usd": float("nan"), "known_additive_cost_usd": float("inf")}) == "—"


def test_now_client_all_is_no_filter(tmp_path):
    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(
        service, client="claude-code", model="claude-opus-4-8", session_id="a1",
        input_tokens=100, output_tokens=50, updated_at=int(now - 3600), estimated_cost_usd=0.5,
    )
    _record_usage(
        service, client="codex", model="gpt-5", session_id="b1",
        input_tokens=80, output_tokens=20, updated_at=int(now - 3600), estimated_cost_usd=0.2,
    )
    result = CliRunner().invoke(app, ["now", "--store-dir", str(tmp_path), "--client", "all", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["client_filter"] is None  # 'all' normalized to no filter
    assert {row["client"] for row in payload["by_client"]} == {"claude-code", "codex"}


def test_now_invalid_window(tmp_path):
    result = CliRunner().invoke(app, ["now", "--store-dir", str(tmp_path), "--window", "5m"])
    assert result.exit_code != 0
