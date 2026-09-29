"""`usage unpriced` names stored rows without a cost, and why each one stays that way."""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from agentacct.client_usage import (
    ClientUsageEvent,
    summarize_unpriced_usage_events,
    unpriced_usage_repair_command,
)
from agentacct.cli import app
from agentacct.pricing_catalog import default_pricing_catalog_snapshot_path
from agentacct.service import SentinelService
from agentacct.usage_truth import mark_trusted_local_usage_import_event

_DEEPSEEK_ROW = {
    "deepseek/deepseek-flash": {
        "litellm_provider": "deepseek",
        "input_cost_per_token": 0.0000005,
        "output_cost_per_token": 0.0000015,
        "cache_read_input_token_cost": 0.0000001,
    }
}


def _dsh_event(session_id="dsh-unpriced", *, model="deepseek-flash", provider="deepseek-official"):
    event = ClientUsageEvent(
        client="dsh", client_session_id=session_id, source_path=Path("/old/session.v4.jsonl.zstd"),
        title=None, cwd="/old/project", model=model,
        input_tokens=1_200, output_tokens=300, cached_input_tokens=5_040,
        cache_read_input_tokens=5_000, cache_creation_input_tokens=40,
        cache_creation_tokens_reported=True, cache_read_tokens_reported=True,
        provider_name=provider,
        source_namespace_fingerprint="sha256:" + "b" * 64,
        source_revision_at=200_000_000, source_revision_basis="source_timestamp",
    ).to_sentinel_event()
    event.update(event_id="evt_" + session_id, created_at=300)
    return mark_trusted_local_usage_import_event(event)


def _codex_event(session_id="codex-unpriced", *, model="gpt-6-astra"):
    event = ClientUsageEvent(
        client="codex", client_session_id=session_id, source_path=Path("/old/rollout.jsonl"),
        title=None, cwd="/old/project", model=model,
        input_tokens=1_000, output_tokens=100, cached_input_tokens=2_000,
        cache_read_input_tokens=2_000, started_at=100, updated_at=200,
        cache_creation_tokens_reported=False, cache_read_tokens_reported=True,
        source_namespace_fingerprint="sha256:" + "a" * 64,
        source_revision_at=200_000_000, source_revision_basis="source_timestamp",
    ).to_sentinel_event()
    event.update(event_id="evt_" + session_id, created_at=300)
    return mark_trusted_local_usage_import_event(event)


def _write_catalog(store: Path, entries: dict) -> None:
    path = default_pricing_catalog_snapshot_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries))


def _unpriced(store: Path, *extra: str) -> dict:
    result = CliRunner().invoke(app, ["usage", "unpriced", "--store-dir", str(store), "--json", *extra])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_repairable_rows_name_their_catalog_row_and_the_exact_repair(tmp_path):
    store = tmp_path / "state"
    empty_dsh_home = tmp_path / "empty-dsh"
    empty_dsh_home.mkdir()
    service = SentinelService(store)
    service.record_event(_dsh_event(), trusted_usage_import=True)
    _write_catalog(store, _DEEPSEEK_ROW)

    payload = _unpriced(store)

    assert payload["rows"] == 1
    assert payload["reprice_available_rows"] == 1
    model = payload["models"][0]
    assert (model["client"], model["provider"], model["model"]) == ("dsh", "deepseek-official", "deepseek-flash")
    assert model["reason"] == "reprice_available"
    # The row the client reported ("deepseek-official") resolves through the
    # provider alias to the catalog's own key, and the report says so.
    assert (model["catalog_provider"], model["catalog_model"]) == ("deepseek", "deepseek/deepseek-flash")
    assert model["repair_command"] == unpriced_usage_repair_command("dsh")
    assert model["repair_command"] == "agentacct usage import-local --refresh --estimate-costs --client dsh"
    assert payload["repair_commands"] == [model["repair_command"]]

    # The claim is provable: the named command really prices the row, and the
    # report then has nothing left to list.
    repair = CliRunner().invoke(app, [
        "usage", "import-local", "--store-dir", str(store), "--client", "dsh",
        "--dsh-home", str(empty_dsh_home), "--refresh", "--estimate-costs", "--json",
    ])
    assert repair.exit_code == 0, repair.output
    assert json.loads(repair.output)["repriced_events"] == 1
    stored = SentinelService(store, create=False).list_all_events()[0]
    assert stored["cost_confidence"] == "estimated_from_tokens"
    assert stored["metadata"]["pricing_source_model"] == "deepseek/deepseek-flash"
    assert _unpriced(store)["rows"] == 0


def test_rows_no_catalog_row_covers_say_so_instead_of_offering_a_repair(tmp_path):
    store = tmp_path / "state"
    SentinelService(store).record_event(_dsh_event(), trusted_usage_import=True)

    payload = _unpriced(store)

    assert payload["rows"] == 1
    assert payload["no_catalog_row_rows"] == 1
    assert payload["repair_commands"] == []
    model = payload["models"][0]
    assert model["reason"] == "no_catalog_row"
    assert model["catalog_model"] is None
    assert model["repair_command"] is None

    human = CliRunner().invoke(app, ["usage", "unpriced", "--store-dir", str(store)])
    assert human.exit_code == 0, human.output
    # Rich wraps the table cell, so assert on the footer it prints verbatim.
    plain = " ".join(human.output.split())
    assert "have no local price row for their model" in plain
    assert "stay cost-unknown rather than guessed" in plain
    assert "Repair:" not in plain


def test_excluded_rows_report_the_blocking_fact(tmp_path):
    store = tmp_path / "state"
    service = SentinelService(store)
    split_unreported = _dsh_event("dsh-no-split")
    split_unreported["metadata"]["input_tokens_reported"] = False
    non_additive = _dsh_event("dsh-cumulative")
    non_additive["metadata"]["usage_additive"] = False
    service.record_event(split_unreported, trusted_usage_import=True)
    service.record_event(non_additive, trusted_usage_import=True)
    _write_catalog(store, _DEEPSEEK_ROW)

    payload = _unpriced(store)

    assert payload["rows"] == 2
    assert payload["not_priceable_rows"] == 2
    assert payload["reprice_available_rows"] == 0
    assert payload["repair_commands"] == []
    assert payload["blocked_by"] == {"input_output_split_not_reported": 1, "non_additive": 1}
    model = payload["models"][0]
    assert model["reason"] == "usage_not_priceable"
    # A catalog row exists; the row itself is why it stays unpriced.
    assert (model["catalog_provider"], model["catalog_model"]) == ("deepseek", "deepseek/deepseek-flash")
    assert model["repair_command"] is None

    human = CliRunner().invoke(app, ["usage", "unpriced", "--store-dir", str(store)])
    assert human.exit_code == 0, human.output
    plain = " ".join(human.output.split())
    assert "reported no input/output split" in plain
    assert "non-additive usage" in plain


def test_priced_rows_are_never_listed(tmp_path):
    store = tmp_path / "state"
    service = SentinelService(store)
    priced = _dsh_event("dsh-priced")
    priced.update(cost_basis="pricing_table", cost_confidence="estimated_from_tokens", estimated_cost_usd=0.5)
    service.record_event(priced, trusted_usage_import=True)
    service.record_event(_dsh_event("dsh-unpriced"), trusted_usage_import=True)

    summary = summarize_unpriced_usage_events(SentinelService(store, create=False).list_all_events())

    assert summary["rows"] == 1
    assert summary["models"][0]["rows"] == 1


def test_client_filter_limits_the_report(tmp_path):
    store = tmp_path / "state"
    service = SentinelService(store)
    service.record_event(_dsh_event(), trusted_usage_import=True)
    service.record_event(_codex_event(), trusted_usage_import=True)

    everything = _unpriced(store)
    only_dsh = _unpriced(store, "--client", "dsh")

    assert {model["client"] for model in everything["models"]} == {"codex", "dsh"}
    assert [model["client"] for model in only_dsh["models"]] == ["dsh"]
    assert only_dsh["rows"] == 1


def test_store_without_unpriced_rows_says_nothing_is_unpriced(tmp_path):
    store = tmp_path / "state"
    SentinelService(store).record_event(_dsh_event(), trusted_usage_import=True)

    payload = _unpriced(store)
    assert payload["rows"] == 1

    human = CliRunner().invoke(app, ["usage", "unpriced", "--store-dir", str(store), "--client", "codex"])
    assert human.exit_code == 0, human.output
    assert "No unpriced usage rows" in " ".join(human.output.split())
