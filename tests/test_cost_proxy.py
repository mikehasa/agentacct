import json
from pathlib import Path

from typer.testing import CliRunner

from agent_chronicle.cli import app
from agent_chronicle.cost import CostLedger, CostPolicy, estimate_openai_chat_usage
from agent_chronicle.proxy import create_app


def test_estimate_openai_chat_usage_counts_tokens_without_network():
    payload = {
        "model": "gpt-5-mini",
        "messages": [
            {"role": "system", "content": "You are careful."},
            {"role": "user", "content": "Fix tests and explain the change."},
        ],
    }

    usage = estimate_openai_chat_usage(payload)

    assert usage.provider == "openai"
    assert usage.model == "gpt-5-mini"
    assert usage.estimated_input_tokens > 0
    assert usage.estimated_cost_usd > 0
    assert usage.forwarded is False


def test_cost_ledger_records_events_and_totals(tmp_path):
    ledger = CostLedger(tmp_path)
    usage = estimate_openai_chat_usage({"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hello"}]})

    event = ledger.record_usage(usage, run_id="run_demo", decision="allow")

    assert event["run_id"] == "run_demo"
    assert event["decision"] == "allow"
    events = ledger.read_events()
    assert len(events) == 1
    assert events[0]["provider"] == "openai"
    assert ledger.total_estimated_cost_usd() == events[0]["estimated_cost_usd"]


def test_policy_blocks_when_budget_would_be_exceeded(tmp_path):
    policy = CostPolicy(max_total_usd=0.000001)
    usage = estimate_openai_chat_usage({
        "model": "gpt-5-mini",
        "messages": [{"role": "user", "content": "This request should exceed a tiny budget."}],
    })

    decision = policy.check(usage, already_spent_usd=0.0)

    assert decision.allowed is False
    assert "budget" in decision.reason.lower()


def test_proxy_dry_run_records_but_never_forwards(tmp_path):
    app_obj = create_app(store_dir=tmp_path, policy=CostPolicy(max_total_usd=1.0), dry_run=True)
    from fastapi.testclient import TestClient
    client = TestClient(app_obj)

    response = client.post(
        "/openai/v1/chat/completions",
        json={"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["agent_sentinel"]["dry_run"] is True
    assert body["agent_sentinel"]["forwarded"] is False
    events = CostLedger(tmp_path).read_events()
    assert len(events) == 1
    assert events[0]["decision"] == "dry_run_allow"


def test_proxy_blocks_over_budget_without_forwarding(tmp_path):
    app_obj = create_app(store_dir=tmp_path, policy=CostPolicy(max_total_usd=0.000001), dry_run=True)
    from fastapi.testclient import TestClient
    client = TestClient(app_obj)

    response = client.post(
        "/openai/v1/chat/completions",
        json={"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hello world this costs something"}]},
    )

    assert response.status_code == 402
    body = response.json()
    assert body["agent_sentinel"]["allowed"] is False
    assert body["agent_sentinel"]["forwarded"] is False


def test_proxy_cli_status_reads_cost_ledger(tmp_path):
    ledger = CostLedger(tmp_path)
    usage = estimate_openai_chat_usage({"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hello"}]})
    ledger.record_usage(usage, run_id="run_demo", decision="dry_run_allow")

    result = CliRunner().invoke(app, ["cost", "status", "--store-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert "Estimated total cost" in result.output
    assert "Events: 1" in result.output


def test_cost_proxy_refuses_non_local_bind_by_default(tmp_path):
    result = CliRunner().invoke(app, ["cost", "proxy", "--host", "0.0.0.0", "--store-dir", str(tmp_path), "--dry-run-only"])

    assert result.exit_code != 0
    assert "Refusing non-local bind" in result.output
