"""Backfill unknown prices without widening transcript discovery or changing usage."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentacct.api import create_local_api_app
from agentacct.cli import app
from agentacct.client_usage import (
    ClientUsageEvent,
    apply_pricing_estimate_to_event,
    build_stored_unknown_cost_reprice_batch,
)
from agentacct.cost import pricing_catalog_scope
from agentacct.pricing_catalog import PricingCatalog, PricingCatalogEntry, default_pricing_catalog_snapshot_path
from agentacct.refreshable_usage import refreshable_usage_truth_digest
from agentacct.service import SentinelService
from agentacct.usage_truth import mark_trusted_local_usage_import_event


def _event(session_id="historical", *, client="codex", model="gpt-6-astra"):
    event = ClientUsageEvent(
        client=client, client_session_id=session_id, source_path=Path("/old/rollout.jsonl"),
        title=None, cwd="/old/project", model=model,
        input_tokens=1_000, output_tokens=100, cached_input_tokens=2_000,
        cache_read_input_tokens=2_000, started_at=100, updated_at=200,
        cache_creation_tokens_reported=False, cache_read_tokens_reported=True,
        source_namespace_fingerprint="sha256:" + "a" * 64,
        source_revision_at=200_000_000, source_revision_basis="source_timestamp",
    ).to_sentinel_event()
    event.update(event_id="evt_" + session_id, created_at=300)
    return mark_trusted_local_usage_import_event(event)


def _catalog(*, cache_read=1.0, cache_write_5m=None, cache_write_1h=None):
    return PricingCatalog([
        PricingCatalogEntry("openai", "gpt-6-astra", 10, 50, cache_read_cost_per_1m=cache_read,
                            cache_write_5m_cost_per_1m=cache_write_5m,
                            cache_write_1h_cost_per_1m=cache_write_1h),
    ], provider_aliases={"codex": "openai"})


def _batch(events, **kwargs):
    return build_stored_unknown_cost_reprice_batch(events, client="codex", excluded_bases=set(), **kwargs)


def _args(store, home, *extra):
    return ["usage", "import-local", "--store-dir", str(store), "--client", "codex",
            "--codex-home", str(home), "--limit-sessions", "1", "--json", *extra]


def _write_catalog(store):
    path = default_pricing_catalog_snapshot_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"gpt-6-astra": {
        "litellm_provider": "openai", "input_cost_per_token": 0.00001,
        "output_cost_per_token": 0.00005, "cache_read_input_token_cost": 0.000001,
    }}))


def test_refresh_prices_history_without_rediscovering_source_and_keeps_evidence(tmp_path):
    store = tmp_path / "state"
    home = tmp_path / "empty-codex"
    home.mkdir()
    service = SentinelService(store)
    before = service.record_event(_event(), trusted_usage_import=True)
    service.reconcile_evidence_refreshable_usage_snapshot(complete=True, transport="internal")
    before_truth = refreshable_usage_truth_digest(before)
    heads_before = service.evidence.store.refreshable_usage_stats().current_heads
    _write_catalog(store)
    runner = CliRunner()

    preview = runner.invoke(app, _args(store, home, "--refresh", "--estimate-costs", "--dry-run"))
    assert preview.exit_code == 0, preview.output
    payload = json.loads(preview.output)
    assert payload["scanned_sessions"] == 0
    assert payload["repriced_events"] == payload["historical_repriced_events"] == 1
    assert service.list_all_events() == [before]

    result = runner.invoke(app, _args(store, home, "--refresh", "--estimate-costs"))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["repriced_events"] == payload["historical_repriced_events"] == 1
    assert payload["refreshed_events"] == 0
    assert payload["evidence_refreshable_usage"]["complete_applied"] is True
    after = service.list_all_events()[0]
    assert after["estimated_cost_usd"] == pytest.approx(0.017)
    assert after["cost_confidence"] == "estimated_from_tokens"
    assert after["metadata"]["pricing_source_provider"] == "openai"
    assert after["metadata"]["pricing_source_model"] == "gpt-6-astra"
    assert refreshable_usage_truth_digest(after) == before_truth
    assert service.evidence.store.refreshable_usage_stats().current_heads == heads_before
    for key, value in before["metadata"].items():
        assert after["metadata"][key] == value

    client = TestClient(create_local_api_app(store_dir=store, v1_auth_token="test-token"))
    response = client.get("/v1/sessions?client=codex", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 200
    usage = response.json()["sessions"][0]["usage"]
    assert usage["priced_rows"] == 1 and usage["unpriced_rows"] == 0
    assert usage["estimated_cost_usd"] == pytest.approx(0.017)

    again = runner.invoke(app, _args(store, home, "--refresh", "--estimate-costs"))
    assert again.exit_code == 0, again.output
    assert json.loads(again.output)["repriced_events"] == 0
    assert service.list_all_events()[0]["event_id"] == after["event_id"]


@pytest.mark.parametrize("flags", [[], ["--refresh"], ["--estimate-costs"]])
def test_history_reprice_requires_both_flags(tmp_path, flags):
    store = tmp_path / "state"
    home = tmp_path / "empty-codex"
    home.mkdir()
    service = SentinelService(store)
    before = service.record_event(_event(), trusted_usage_import=True)
    _write_catalog(store)
    result = CliRunner().invoke(app, _args(store, home, *flags))
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["historical_repriced_events"] == 0
    assert service.list_all_events() == [before]


@pytest.mark.parametrize("change", [
    "already_priced", "client_reported", "unclear_amount", "held", "legacy_child",
    "unknown_model", "other_client", "untrusted", "duplicate_identity",
    "namespace_conflict", "missing_input_counter", "missing_output_counter", "total_only_zero",
])
def test_history_reprice_preserves_rows_outside_safe_pricing_eligibility(change):
    """Every non-rate safety gate still withholds the row.

    A row is only ours to reprice when it is trusted, additive, unique for its
    identity, unknown-cost, unredacted and complete on both reported counters.
    Missing category rates are deliberately NOT one of those gates: they now
    price through the same fallbacks a fresh import uses, covered by
    ``test_history_reprice_prices_missing_category_rates_with_the_same_fallbacks_as_fresh_imports``.
    """

    event = _event()
    rows = [event]
    catalog = _catalog()
    if change == "already_priced":
        event.update(cost_confidence="estimated_from_tokens", estimated_cost_usd=42)
    elif change == "client_reported":
        event.update(cost_confidence="client_reported", estimated_cost_usd=42)
    elif change == "unclear_amount":
        event["estimated_cost_usd"] = 42
    elif change == "held":
        event["metadata"]["usage_additive"] = False
    elif change == "legacy_child":
        event["metadata"].pop("usage_additive", None)
        event["metadata"].update(client_session_kind="child", parent_client_session_id="parent")
    elif change == "unknown_model":
        event["model"] = "gpt-unknown"
    elif change == "other_client":
        event["metadata"]["client"] = "claude-code"
    elif change == "untrusted":
        event["metadata"].pop("usage_provenance")
    elif change in {"missing_input_counter", "missing_output_counter", "total_only_zero"}:
        event["metadata"]["input_tokens_reported" if change == "missing_input_counter" else "output_tokens_reported"] = False
        if change == "total_only_zero":
            event.update(estimated_input_tokens=0, estimated_output_tokens=0)
            event["metadata"].update(cached_input_tokens=0, cache_read_input_tokens=0, total_tokens=1_000,
                                     usage_update_semantics="codex_sqlite_tokens_used_fallback")
    elif change in {"duplicate_identity", "namespace_conflict"}:
        other = deepcopy(event)
        other["event_id"] = "evt_duplicate"
        if change == "namespace_conflict":
            other["metadata"]["usage_row_lane"] = "other-lane"
            other["metadata"]["source_namespace_fingerprint"] = "sha256:" + "b" * 64
        rows.append(other)
    before = deepcopy(rows)
    with pricing_catalog_scope(catalog):
        assert _batch(rows)[0] == []
    assert rows == before


@pytest.mark.parametrize("change", ["missing_cache_read_rate", "missing_cache_write_rate", "missing_cache_1h_rate"])
def test_history_reprice_prices_missing_category_rates_with_the_same_fallbacks_as_fresh_imports(change):
    """A category rate the catalog omits no longer vetoes a historical reprice.

    A fresh import prices the same row through ``apply_pricing_estimate_to_event``
    -> ``estimate_model_cost_breakdown_usd``, which substitutes 0.1x the input
    price for a missing cache-read rate and the input price for a missing
    cache-write rate. Repricing must land on that identical row, so the expected
    amount is the fresh-import result instead of a frozen constant."""

    event = _event()
    catalog = _catalog()
    if change == "missing_cache_read_rate":
        catalog = _catalog(cache_read=None)
        spelled_out = _catalog(cache_read=1.0)  # 0.1 x the 10/1M input price
    elif change == "missing_cache_write_rate":
        event["metadata"]["cache_creation_input_tokens"] = 20
        spelled_out = _catalog(cache_write_5m=10.0)  # the input price
    else:
        assert change == "missing_cache_1h_rate"
        event["metadata"].update(cache_creation_input_tokens=20, cache_creation_1h_input_tokens=20)
        spelled_out = _catalog(cache_write_1h=10.0)  # the input price
    stored = deepcopy(event)

    with pricing_catalog_scope(catalog):
        fresh_import = deepcopy(stored)
        assert apply_pricing_estimate_to_event(fresh_import) is True
        repriced = _batch([stored])[0]
    with pricing_catalog_scope(spelled_out):
        explicit_rate = deepcopy(stored)
        assert apply_pricing_estimate_to_event(explicit_rate) is True

    # Same row, same dollars, same provenance: import timing is not a factor.
    assert [row["event_id"] for row in repriced] == [stored["event_id"]]
    row = repriced[0]
    assert row == fresh_import
    assert row["estimated_cost_usd"] == fresh_import["estimated_cost_usd"]
    assert row["cost_confidence"] == fresh_import["cost_confidence"] == "estimated_from_tokens"
    assert row["cost_basis"] == fresh_import["cost_basis"] == "pricing_table"
    assert row["metadata"]["pricing_source"] == fresh_import["metadata"]["pricing_source"]
    # Priced under the documented fallback rather than skipped: writing the
    # fallback rate out explicitly yields the same amount.
    assert row["estimated_cost_usd"] == pytest.approx(explicit_rate["estimated_cost_usd"])
    # Planning never mutates the stored row it read.
    assert stored["estimated_cost_usd"] is None and stored["cost_confidence"] == "unknown"


def test_history_reprice_leaves_scanned_bases_to_normal_importer():
    with pricing_catalog_scope(_catalog()):
        batch = build_stored_unknown_cost_reprice_batch(
            [_event()], client="codex", excluded_bases={("codex", "historical")},
        )
    assert batch[0] == []


def test_history_reprice_preserves_server_redaction_provenance(tmp_path):
    service = SentinelService(tmp_path / "state")
    event = _event()
    event["metadata"]["client_session_title"] = "Example sk-abcdefghijk123456789abcdefghijk123456789"
    before = service.record_event(event, trusted_usage_import=True)
    assert before["metadata"]["value_redaction_applied"] is True
    assert before["metadata"]["value_redaction_fields"]
    with pricing_catalog_scope(_catalog()):
        assert _batch([before])[0] == []
    assert service.list_all_events() == [before]


@pytest.mark.parametrize("race", ["updated", "deleted", "new_sibling"])
def test_history_reprice_cannot_overwrite_concurrent_usage(tmp_path, race):
    service = SentinelService(tmp_path / "state")
    before = service.record_event(_event(), trusted_usage_import=True)
    with pricing_catalog_scope(_catalog()):
        events, predicate, guard, dedup = _batch([before])
    assert len(events) == 1
    if race == "deleted":
        service.replace_events(lambda row: row["event_id"] == before["event_id"], [])
    elif race == "updated":
        newer = _event()
        newer["estimated_input_tokens"] = 10_000
        service.replace_events(lambda row: row["event_id"] == before["event_id"], [newer], trusted_usage_import=True)
    else:
        sibling = _event()
        sibling["metadata"]["usage_row_lane"] = "new-lane"
        service.record_event(sibling, trusted_usage_import=True)
    concurrent = service.list_all_events()
    assert service.replace_events(predicate, events, trusted_usage_import=True, replace_guard=guard, dedup_key=dedup) == []
    assert service.list_all_events() == concurrent
