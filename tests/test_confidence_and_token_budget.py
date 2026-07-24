import json
import math

import pytest
from typer.testing import CliRunner

from agent_chronicle.cli import app
from agent_chronicle.cost import CostLedger, CostPolicy, estimate_openai_chat_usage, estimate_subscription_cost
from agent_chronicle.proxy import create_app


def test_usage_estimates_default_to_estimated_confidence():
    usage = estimate_openai_chat_usage({"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hello"}]})

    assert usage.usage_confidence == "estimated"


def test_cost_ledger_persists_usage_confidence(tmp_path):
    ledger = CostLedger(tmp_path)
    usage = estimate_openai_chat_usage({"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hello"}]})

    event = ledger.record_usage(usage, run_id="run_confidence", decision="dry_run_allow")

    assert event["usage_confidence"] == "estimated"
    assert ledger.read_events()[0]["usage_confidence"] == "estimated"


def test_policy_blocks_when_total_token_budget_would_be_exceeded():
    usage = estimate_openai_chat_usage({
        "model": "gpt-5-mini",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hello world"}],
    })
    policy = CostPolicy(max_total_tokens=1)

    decision = policy.check(usage, already_spent_usd=0.0, already_used_tokens=0)

    assert decision.allowed is False
    assert "token" in decision.reason.lower()


def test_policy_blocks_when_input_or_output_token_budget_exceeded():
    usage = estimate_openai_chat_usage({
        "model": "gpt-5-mini",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hello world"}],
    })

    assert CostPolicy(max_input_tokens=1).check(usage, already_spent_usd=0.0, already_used_input_tokens=0).allowed is False
    assert CostPolicy(max_output_tokens=1).check(usage, already_spent_usd=0.0, already_used_output_tokens=0).allowed is False


def test_policy_classifies_cost_budget_confidence_tiers():
    estimated = estimate_openai_chat_usage({"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hello"}]})
    exact = estimate_openai_chat_usage({"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hello"}]})
    exact.cost_confidence = "provider_billed"
    unknown = estimate_openai_chat_usage({"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hello"}]})
    unknown.cost_confidence = "unknown"

    policy = CostPolicy(max_total_usd=1)

    assert policy.classify_cost_budget(exact).tier == "exact_cost"
    assert policy.classify_cost_budget(exact).can_hard_block is True
    assert policy.classify_cost_budget(estimated).tier == "estimated_cost"
    assert policy.classify_cost_budget(estimated).default_action == "warn_or_pause"
    assert policy.classify_cost_budget(unknown).tier == "token_runtime_only"


def test_proxy_blocks_on_token_budget_and_records_confidence(tmp_path):
    from fastapi.testclient import TestClient

    client = TestClient(create_app(store_dir=tmp_path, policy=CostPolicy(max_total_tokens=1), dry_run=True))
    response = client.post(
        "/openai/v1/chat/completions",
        json={"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hello token budget"}]},
    )

    assert response.status_code == 402
    body = response.json()
    assert body["agent_sentinel"]["usage_confidence"] == "estimated"
    events = CostLedger(tmp_path).read_events()
    assert events[0]["usage_confidence"] == "estimated"
    assert events[0]["decision"] == "blocked"


def test_cost_status_shows_confidence_counts(tmp_path):
    ledger = CostLedger(tmp_path)
    ledger.record_usage(
        estimate_openai_chat_usage({"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hello"}]}),
        run_id="run_confidence",
        decision="dry_run_allow",
    )

    result = CliRunner().invoke(app, ["cost", "status", "--store-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert "Confidence:" in result.output
    assert "estimated_from_tokens: 1" in result.output


def test_subscription_cost_estimate_allocates_user_entered_price_by_runs_and_tokens(tmp_path):
    ledger = CostLedger(tmp_path)
    usage_a = estimate_openai_chat_usage({"model": "gpt-5-mini", "max_tokens": 100, "messages": [{"role": "user", "content": "hello"}]})
    usage_b = estimate_openai_chat_usage({"model": "gpt-5-mini", "max_tokens": 50, "messages": [{"role": "user", "content": "world"}]})
    ledger.record_usage(usage_a, run_id="run_a", decision="dry_run_allow")
    ledger.record_usage(usage_b, run_id="run_b", decision="dry_run_allow")

    estimate = estimate_subscription_cost(
        ledger.read_events(),
        subscription_price_usd=20,
        period_days=30,
        period_run_budget=10,
        period_token_budget=1000,
    ).to_dict()

    allocations = {allocation["method"]: allocation for allocation in estimate["allocations"]}
    assert estimate["event_count"] == 2
    assert estimate["observed_run_count"] == 2
    assert estimate["estimated_total_tokens"] == usage_a.estimated_input_tokens + usage_a.estimated_output_tokens + usage_b.estimated_input_tokens + usage_b.estimated_output_tokens
    assert allocations["per_observed_run"]["allocated_cost_usd"] == 4.0
    assert allocations["per_observed_run"]["confidence"] == "approximate_subscription_allocation"
    assert allocations["per_estimated_token"]["allocated_cost_usd"] == 20 * estimate["estimated_total_tokens"] / 1000
    assert "does not read exact Claude Code/Codex subscription billing" in estimate["note"]


def test_subscription_cost_estimate_without_allocation_basis_is_unavailable(tmp_path):
    ledger = CostLedger(tmp_path)
    ledger.record_usage(
        estimate_openai_chat_usage({"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hello"}]}),
        run_id="run_a",
        decision="dry_run_allow",
    )

    estimate = estimate_subscription_cost(ledger.read_events(), subscription_price_usd=20).to_dict()

    assert estimate["allocations"] == [
        {
            "method": "unallocated_subscription",
            "allocated_cost_usd": None,
            "confidence": "subscription_unavailable",
            "reason": "agentacct cannot infer exact subscription billing or a fair amortization denominator. Pass --period-run-budget or --period-token-budget to compute an explicit approximation.",
            "observed_units": 0,
            "period_units": None,
        }
    ]


def test_subscription_cost_estimate_rejects_non_finite_price() -> None:
    for value in (math.nan, math.inf):
        with pytest.raises(ValueError, match="finite value > 0"):
            estimate_subscription_cost([], subscription_price_usd=value, period_run_budget=1)


def test_subscription_estimate_cli_json_filters_run_and_labels_approximate(tmp_path):
    ledger = CostLedger(tmp_path)
    usage_a = estimate_openai_chat_usage({"model": "gpt-5-mini", "max_tokens": 100, "messages": [{"role": "user", "content": "hello"}]})
    ledger.record_usage(usage_a, run_id="run_a", decision="dry_run_allow")
    ledger.record_usage(
        estimate_openai_chat_usage({"model": "gpt-5-mini", "max_tokens": 300, "messages": [{"role": "user", "content": "other"}]}),
        run_id="run_b",
        decision="dry_run_allow",
    )

    result = CliRunner().invoke(
        app,
        [
            "cost",
            "subscription-estimate",
            "--store-dir",
            str(tmp_path),
            "--run-id",
            "run_a",
            "--subscription-price-usd",
            "20",
            "--period-run-budget",
            "40",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["run_id"] == "run_a"
    assert payload["event_count"] == 1
    assert payload["observed_run_count"] == 1
    assert payload["allocations"][0]["allocated_cost_usd"] == 0.5
    assert payload["allocations"][0]["confidence"] == "approximate_subscription_allocation"
    assert "source of truth" in payload["note"]


def test_subscription_config_can_be_saved_listed_and_used_for_estimate(tmp_path):
    ledger = CostLedger(tmp_path)
    ledger.record_usage(
        estimate_openai_chat_usage({"model": "gpt-5-mini", "max_tokens": 100, "messages": [{"role": "user", "content": "hello"}]}),
        run_id="run_a",
        decision="dry_run_allow",
    )
    runner = CliRunner()

    saved = runner.invoke(
        app,
        [
            "cost",
            "subscription-set",
            "--store-dir",
            str(tmp_path),
            "--name",
            "codex-pro",
            "--provider",
            "codex",
            "--monthly-price-usd",
            "200",
            "--period-run-budget",
            "40",
            "--json",
        ],
    )
    assert saved.exit_code == 0, saved.output
    assert json.loads(saved.output)["subscription"]["name"] == "codex-pro"

    listed = runner.invoke(app, ["cost", "subscription-list", "--store-dir", str(tmp_path), "--json"])
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output)["subscriptions"][0]["provider"] == "codex"

    estimate = runner.invoke(
        app,
        [
            "cost",
            "subscription-estimate",
            "--store-dir",
            str(tmp_path),
            "--subscription-name",
            "codex-pro",
            "--run-id",
            "run_a",
            "--json",
        ],
    )
    assert estimate.exit_code == 0, estimate.output
    payload = json.loads(estimate.output)
    assert payload["subscription_price_usd"] == 200
    assert payload["allocations"][0]["allocated_cost_usd"] == 5.0
    assert payload["allocations"][0]["confidence"] == "approximate_subscription_allocation"


def test_subscription_estimate_cli_rejects_invalid_values(tmp_path):
    bad_price = CliRunner().invoke(app, ["cost", "subscription-estimate", "--store-dir", str(tmp_path), "--subscription-price-usd", "0"])
    bad_nan = CliRunner().invoke(
        app,
        ["cost", "subscription-estimate", "--store-dir", str(tmp_path), "--subscription-price-usd", "nan", "--period-run-budget", "1", "--json"],
    )
    bad_inf = CliRunner().invoke(
        app,
        ["cost", "subscription-estimate", "--store-dir", str(tmp_path), "--subscription-price-usd", "inf", "--period-run-budget", "1", "--json"],
    )
    bad_run = CliRunner().invoke(app, ["cost", "subscription-estimate", "--store-dir", str(tmp_path), "--subscription-price-usd", "20", "--run-id", "../bad"])
    bad_budget = CliRunner().invoke(
        app,
        ["cost", "subscription-estimate", "--store-dir", str(tmp_path), "--subscription-price-usd", "20", "--period-token-budget", "0"],
    )

    assert bad_price.exit_code != 0
    assert bad_nan.exit_code != 0
    assert "NaN" not in bad_nan.output
    assert bad_inf.exit_code != 0
    assert "Infinity" not in bad_inf.output
    assert bad_run.exit_code != 0
    assert bad_budget.exit_code != 0


def test_public_docs_describe_subscription_estimate_as_approximate() -> None:
    from pathlib import Path

    # The subscription-allocation section moved from the README to
    # docs/reference.md in the value-first README rewrite.
    reference = Path("docs/reference.md").read_text(encoding="utf-8")
    checklist = Path("docs/public-alpha-checklist.md").read_text(encoding="utf-8")

    # The reference uses the public agentacct CLI; the maintainer checklist
    # still uses the transition-alias name (a later doc-rebrand pass).
    assert "agentacct cost subscription-estimate" in reference
    assert "agentacct cost subscription-estimate" in checklist
    for text in (reference, checklist):
        assert "subscription_unavailable" in text
    assert "explicit approximation, not as exact provider billing" in reference
    assert "Provider invoices or exposed provider usage data remain the source of truth" in reference
