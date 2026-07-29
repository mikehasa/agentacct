from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentacct.cli import app
from agentacct.cost import CostLedger, CostPolicy, estimate_openai_chat_usage
from agentacct.proxy import create_app


class FakeCostForwarder:
    def __init__(self, usage):
        self.usage = usage
        self.calls = 0

    def __call__(self, *, provider, endpoint, payload, api_key, upstream_base_url):
        self.calls += 1
        return {
            "status_code": 200,
            "json": {
                "id": "fake-cost-response",
                "choices": [{"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
                "usage": self.usage,
            },
        }


def test_estimated_usage_has_estimated_cost_confidence_and_no_actual_provider_cost():
    usage = estimate_openai_chat_usage({"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hello"}]})

    assert usage.cost_confidence == "estimated_from_tokens"
    assert usage.actual_provider_cost_usd is None


def test_openrouter_forwarding_extracts_actual_cost_when_provider_returns_it(tmp_path):
    fake = FakeCostForwarder({"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7, "cost": 0.0000042})
    client = TestClient(
        create_app(
            store_dir=tmp_path,
            policy=CostPolicy(max_total_usd=0.01),
            dry_run=False,
            enable_forwarding=True,
            allowed_forward_providers={"openrouter"},
            openrouter_api_key="sk-or-v1-",
            forwarder=fake,
        )
    )

    response = client.post(
        "/openrouter/v1/chat/completions",
        headers={"X-Agent-Sentinel-Run-Id": "run_actual_cost"},
        json={"model": "deepseek/deepseek-chat", "messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 200
    agent = response.json()["agent_sentinel"]
    assert agent["cost_confidence"] == "provider_billed"
    assert agent["actual_provider_cost_usd"] == 0.0000042
    event = CostLedger(tmp_path).read_events(run_id="run_actual_cost")[0]
    assert event["cost_confidence"] == "provider_billed"
    assert event["actual_provider_cost_usd"] == 0.0000042


def test_openrouter_forwarding_keeps_estimated_cost_confidence_without_provider_cost(tmp_path):
    fake = FakeCostForwarder({"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7})
    client = TestClient(
        create_app(
            store_dir=tmp_path,
            policy=CostPolicy(max_total_usd=0.01),
            dry_run=False,
            enable_forwarding=True,
            allowed_forward_providers={"openrouter"},
            openrouter_api_key="sk-or-v1-",
            forwarder=fake,
        )
    )

    response = client.post(
        "/openrouter/v1/chat/completions",
        json={"model": "deepseek/deepseek-chat", "messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 200
    agent = response.json()["agent_sentinel"]
    assert agent["usage_confidence"] == "provider_reported"
    assert agent["cost_confidence"] == "estimated_from_tokens"
    assert agent["actual_provider_cost_usd"] is None


def test_cost_status_shows_cost_confidence_counts(tmp_path):
    fake = FakeCostForwarder({"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7, "cost": 0.0000042})
    client = TestClient(
        create_app(
            store_dir=tmp_path,
            policy=CostPolicy(max_total_usd=0.01),
            dry_run=False,
            enable_forwarding=True,
            allowed_forward_providers={"openrouter"},
            openrouter_api_key="sk-or-v1-",
            forwarder=fake,
        )
    )
    client.post(
        "/openrouter/v1/chat/completions",
        headers={"X-Agent-Sentinel-Run-Id": "run_cost_status"},
        json={"model": "deepseek/deepseek-chat", "messages": [{"role": "user", "content": "hello"}]},
    )

    result = CliRunner().invoke(app, ["cost", "status", "--store-dir", str(tmp_path), "--run-id", "run_cost_status"])

    assert result.exit_code == 0
    assert "Actual provider cost: $0.000004" in result.output
    assert "Cost confidence:" in result.output
    assert "provider_billed: 1" in result.output
