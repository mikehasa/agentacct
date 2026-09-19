"""Tests for `agentacct now` (current usage/cost snapshot on the event-log cube)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from typer.testing import CliRunner

from agentacct import cli as cli_module
from agentacct.cli import app
from agentacct.client_usage import ClientUsageEvent
from agentacct.service import SentinelService


def _pin_render_width(monkeypatch, columns: int) -> None:
    """Pin the width `agentacct` renders at, for tests that assert on layout.

    This has to be set on the Console OBJECT, because the obvious way does not
    work: rich reads ``COLUMNS`` once inside ``Console.__init__`` and freezes it
    in ``_width``, and ``agentacct.cli.console`` is constructed at import time —
    long before any test body runs. A ``monkeypatch.setenv("COLUMNS", ...)`` is
    therefore a silent no-op, and a test written that way really renders at
    whatever width launched pytest. That is how the duplicate-``tokens`` header
    below passed on wide developer terminals and failed only in CI at 80.

    ``_width`` rather than the public ``width`` setter so that monkeypatch's undo
    restores ``None`` as ``None``; going through the property would read back a
    concrete number and pin it for every later test in the process.
    """
    monkeypatch.setattr(cli_module.console, "_width", columns)


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


def test_now_human_render_smoke(tmp_path, monkeypatch):
    # Pinned for the same reason as the header test below: this asserts on
    # rendered table cells ("last 7 days"), which a narrow terminal truncates to
    # "last …". Unpinned, the test's verdict depends on the window size of
    # whoever ran it.
    _pin_render_width(monkeypatch, 80)
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


def test_now_headlines_fresh_tokens_with_cache_reads_named_apart(tmp_path, monkeypatch):
    # 80 columns: the standard narrow terminal, and the width CI runs at. The
    # header collapse this test guards against only happens when the table is
    # squeezed, so asserting at a comfortable width would prove nothing.
    _pin_render_width(monkeypatch, 80)
    service = SentinelService(tmp_path)
    now = time.time()
    _record_usage(
        service, client="codex", model="gpt-5", session_id="f1",
        input_tokens=1_200_000, output_tokens=34_567, updated_at=int(now - 3600), estimated_cost_usd=1554.67,
    )
    result = CliRunner().invoke(app, ["now", "--store-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "fresh tokens" in out and "cache-read tokens" in out
    assert "1,234,567" in out  # input + output, thousands-grouped
    # Money keeps its magnitude at every width. A truncated figure ("≈$1,554…")
    # is worse than no figure: it reads as a real number but could be $1,554.67
    # or $1,554,000. The ellipsis check is separate from the equality check
    # because a squeezed cost column fails it while the digits still "appear".
    assert "≈$1,554.67" in out
    assert "$1,554…" not in out and "1,554.6…" not in out
    # No column header degrades to a bare, unqualified "tokens". Wrapping is the
    # degradation that does this: both headers end in the same noun, so a split
    # leaves two adjacent columns reading "tokens │ tokens" with the qualifier
    # stranded on the line above.
    header_cells = [cell.strip() for line in out.splitlines() if "window" in line for cell in line.split("┃")]
    assert "tokens" not in header_cells
    # ...and the qualifiers themselves survive the squeeze, in the vocabulary's
    # exact wording — the point is that the reader can tell the two apart here,
    # not merely that the word "tokens" is absent.
    assert "fresh tokens" in out and "cache-read tokens" in out


def test_now_cost_text_uses_cost_complete():
    # The cost cell logic now lives in the shared snapshot layer that `now`,
    # `limits`, and the TUI all consume.
    from agentacct.usage_snapshot import cost_text

    # complete → the complete figure ($ reported/billed, ≈$ estimate); the
    # presence of estimated_cost_usd alone is NOT enough.
    assert cost_text({"cost_complete": True, "estimated_cost_usd": 4.0, "known_additive_cost_usd": 4.0}) == "≈$4.00"
    assert cost_text({"cost_complete": True, "estimated_cost_usd": 4.0, "known_additive_cost_usd": 4.0,
                      "cost_confidence": "provider_billed"}) == "$4.00"
    # priced subtotal present but NOT complete (unpriced rows) → partial with ~.
    assert cost_text({"cost_complete": False, "estimated_cost_usd": 4.0, "known_additive_cost_usd": 4.0}) == "~$4.00"
    # nothing priced → a named absence
    assert cost_text({"rows": 1, "cost_complete": False, "estimated_cost_usd": None, "known_additive_cost_usd": None}) == "unpriced"
    # non-finite degrades to the named absence (no $nan)
    assert cost_text({"rows": 1, "cost_complete": True, "estimated_cost_usd": float("nan"), "known_additive_cost_usd": float("inf")}) == "unpriced"


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
