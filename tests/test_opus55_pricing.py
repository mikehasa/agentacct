import json

import pytest
from typer.testing import CliRunner

from agentacct.cli import app
from agentacct.cost import (
    LEGACY_PRICING_CATALOG_PATH_ENV,
    PRICING_CATALOG_PATH_ENV,
    PRICING_PROVIDER_ALIASES,
    _builtin_pricing_entries,
    estimate_model_cost_breakdown_usd,
    has_model_price,
    model_pricing_entry,
    pricing_catalog_scope,
    reset_pricing_catalog_cache,
)
from agentacct.pricing_catalog import PricingCatalog
from agentacct.service import SentinelService


@pytest.fixture(autouse=True)
def _builtin_catalog_only(monkeypatch):
    for name in (
        PRICING_CATALOG_PATH_ENV,
        LEGACY_PRICING_CATALOG_PATH_ENV,
        "AGENT_SENTINEL_PRICING_CATALOG_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_pricing_catalog_cache()
    yield
    reset_pricing_catalog_cache()


@pytest.mark.parametrize("provider", ["anthropic", "claude-code"])
def test_opus55_standard_prices_preserve_every_cache_category(provider):
    """Opus 5.5 cache reads are 0.05x input, not the usual 0.1x fallback."""
    entry = model_pricing_entry(provider, "claude-opus-5-5")
    assert entry is not None
    assert entry.source == "agent_sentinel_builtin"
    assert entry.source_provider == "anthropic"
    assert entry.cost_multiplier == 1.0
    breakdown = estimate_model_cost_breakdown_usd(
        provider,
        "claude-opus-5-5",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_input_tokens=3_000_000,
        cache_creation_5m_input_tokens=1_000_000,
        cache_creation_1h_input_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
    )
    assert breakdown == pytest.approx({
        "input_cost_usd": 4.0,
        "output_cost_usd": 20.0,
        # The unspecified cache-write remainder uses the standard 5m rate.
        "cache_creation_5m_cost_usd": 10.0,
        "cache_creation_1h_cost_usd": 8.0,
        "cache_creation_cost_usd": 18.0,
        "cache_read_cost_usd": 0.20,
        "total_cost_usd": 42.20,
    })
    assert has_model_price(provider, "claude-opus-5-50") is False


def test_opus55_import_and_upgrade_reprice_unchanged_usage_once(tmp_path):
    claude_home = tmp_path / "claude-home"
    project = claude_home / "projects" / "test-project"
    project.mkdir(parents=True)
    transcript = project / "opus55-session.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "sessionId": "opus55-session",
        "timestamp": "2026-09-22T12:00:00Z",
        "message": {
            "id": "msg_opus55",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 500,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 200,
                    "ephemeral_1h_input_tokens": 300,
                },
                "cache_read_input_tokens": 1_000,
                "service_tier": "standard",
                "speed": "standard",
            },
        },
    }) + "\n", encoding="utf-8")
    store_dir = tmp_path / "store"
    runner = CliRunner()
    args = [
        "usage", "import-local", "--client", "claude-code",
        "--claude-home", str(claude_home), "--store-dir", str(store_dir),
        "--estimate-costs", "--json",
    ]

    def import_usage(*extra):
        result = runner.invoke(app, [*args, *extra])
        assert result.exit_code == 0, result.output
        return json.loads(result.output)

    def stored_usage():
        rows = [row for row in SentinelService(store_dir).list_all_events()
                if row.get("event_type") == "model_usage"]
        assert len(rows) == 1
        return rows[0]

    # Simulate an import on the pre-upgrade catalog without touching the log.
    old_catalog = PricingCatalog(
        [entry for entry in _builtin_pricing_entries() if entry.model != "claude-opus-5-5"],
        provider_aliases=PRICING_PROVIDER_ALIASES,
    )
    with pricing_catalog_scope(old_catalog):
        assert import_usage()["imported_events"] == 1
    unknown = stored_usage()
    assert unknown["cost_confidence"] == "unknown"
    assert unknown["estimated_cost_usd"] is None

    upgraded = import_usage("--refresh")
    assert upgraded["repriced_events"] == 1
    assert upgraded["refreshed_events"] == 0  # Same source and token totals.
    priced = stored_usage()
    assert priced["event_id"] != unknown["event_id"]
    assert priced["estimated_input_tokens"] == 100
    assert priced["estimated_output_tokens"] == 50
    assert priced["estimated_cost_usd"] == pytest.approx(0.005)
    assert priced["cost_confidence"] == "estimated_from_tokens"
    assert priced["metadata"]["pricing_source"] == "agent_sentinel_builtin"
    assert priced["metadata"]["pricing_source_provider"] == "anthropic"
    assert priced["metadata"]["pricing_source_model"] == "claude-opus-5-5"

    settled = import_usage("--refresh")
    assert settled["repriced_events"] == 0
    assert settled["imported_events"] == 0
    assert stored_usage()["event_id"] == priced["event_id"]
