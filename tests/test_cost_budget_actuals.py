from fastapi.testclient import TestClient

from agent_chronicle.cost import CostLedger, CostPolicy, UsageEstimate
from agent_chronicle.proxy import create_app


class CountingForwarder:
    def __init__(self):
        self.calls = 0

    def __call__(self, *, provider, endpoint, payload, api_key, upstream_base_url):
        self.calls += 1
        return {
            "status_code": 200,
            "json": {
                "id": f"fake-{self.calls}",
                "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "cost": 0.00004,
                },
            },
        }


def test_budget_cutoff_uses_actual_provider_cost_from_previous_forwarded_events(tmp_path):
    ledger = CostLedger(tmp_path)
    ledger.record_usage(
        UsageEstimate(
            provider="openrouter",
            model="deepseek/deepseek-chat",
            endpoint="/openrouter/v1/chat/completions",
            estimated_input_tokens=10,
            estimated_output_tokens=5,
            estimated_cost_usd=0.000001,
            actual_provider_cost_usd=0.000199,
            cost_confidence="exact",
            usage_confidence="exact",
            forwarded=True,
        ),
        run_id="run_actual_cutoff",
        decision="forwarded",
    )
    fake = CountingForwarder()
    client = TestClient(
        create_app(
            store_dir=tmp_path,
            policy=CostPolicy(max_total_usd=0.0002),
            dry_run=False,
            enable_forwarding=True,
            allowed_forward_providers={"openrouter"},
            openrouter_api_key="sk-or-v1-",
            forwarder=fake,
        )
    )

    response = client.post(
        "/openrouter/v1/chat/completions",
        headers={"X-Agent-Sentinel-Run-Id": "run_actual_cutoff"},
        json={"model": "deepseek/deepseek-chat", "messages": [{"role": "user", "content": "continue"}], "max_tokens": 8},
    )

    assert response.status_code == 402
    assert fake.calls == 0
    assert "Budget would be exceeded" in response.json()["error"]["message"]


def test_mock_multicall_task_stops_when_actual_provider_cost_reaches_budget(tmp_path):
    fake = CountingForwarder()
    client = TestClient(
        create_app(
            store_dir=tmp_path,
            policy=CostPolicy(max_total_usd=0.000081),
            dry_run=False,
            enable_forwarding=True,
            allowed_forward_providers={"openrouter"},
            openrouter_api_key="sk-or-v1-",
            forwarder=fake,
        )
    )

    statuses = []
    for step in range(4):
        response = client.post(
            "/openrouter/v1/chat/completions",
            headers={"X-Agent-Sentinel-Run-Id": "run_mock_complex_task"},
            json={
                "model": "deepseek/deepseek-chat",
                "messages": [{"role": "user", "content": f"Complex task step {step}: produce a small plan chunk."}],
                "max_tokens": 8,
            },
        )
        statuses.append(response.status_code)
        if response.status_code == 402:
            break

    assert statuses == [200, 200, 402]
    assert fake.calls == 2
    events = CostLedger(tmp_path).read_events(run_id="run_mock_complex_task")
    assert [event["decision"] for event in events] == ["forwarded", "forwarded", "blocked"]
    assert CostLedger(tmp_path).total_actual_provider_cost_usd(run_id="run_mock_complex_task") == 0.00008


def test_blocked_events_do_not_count_as_billable_spend(tmp_path):
    ledger = CostLedger(tmp_path)
    ledger.record_usage(
        UsageEstimate(
            provider="openrouter",
            model="anthropic/claude-sonnet-4",
            endpoint="/openrouter/v1/chat/completions",
            estimated_input_tokens=100,
            estimated_output_tokens=3000,
            estimated_cost_usd=0.036,
            actual_provider_cost_usd=0.006,
            cost_confidence="exact",
            usage_confidence="exact",
            forwarded=True,
        ),
        run_id="run_blocked_not_spent",
        decision="forwarded",
    )
    ledger.record_usage(
        UsageEstimate(
            provider="openrouter",
            model="anthropic/claude-sonnet-4",
            endpoint="/openrouter/v1/chat/completions",
            estimated_input_tokens=100,
            estimated_output_tokens=3000,
            estimated_cost_usd=0.036,
            actual_provider_cost_usd=None,
            cost_confidence="estimated",
            usage_confidence="estimated",
            forwarded=False,
        ),
        run_id="run_blocked_not_spent",
        decision="blocked",
    )

    assert ledger.total_billable_cost_usd(run_id="run_blocked_not_spent") == 0.006
