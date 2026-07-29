import os

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentacct.cli import app
from agentacct.cost import CostLedger, CostPolicy
from agentacct.proxy import create_app


class FakeForwarder:
    def __init__(self):
        self.calls = []

    def __call__(self, *, provider, endpoint, payload, api_key, upstream_base_url):
        self.calls.append(
            {
                "provider": provider,
                "endpoint": endpoint,
                "payload": payload,
                "api_key": api_key,
                "upstream_base_url": upstream_base_url,
            }
        )
        return {
            "status_code": 200,
            "json": {
                "id": "fake-openrouter-response",
                "choices": [{"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        }


def test_openrouter_forwarding_is_disabled_by_default_even_with_forwarder(tmp_path):
    fake = FakeForwarder()
    client = TestClient(create_app(store_dir=tmp_path, policy=CostPolicy(max_total_usd=1), dry_run=True, forwarder=fake))

    response = client.post(
        "/openrouter/v1/chat/completions",
        json={"model": "deepseek/deepseek-chat", "messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 200
    assert response.json()["agent_sentinel"]["forwarded"] is False
    assert fake.calls == []


def test_openrouter_forwarding_requires_budget(tmp_path):
    fake = FakeForwarder()
    client = TestClient(
        create_app(
            store_dir=tmp_path,
            policy=CostPolicy(),
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

    assert response.status_code == 400
    assert "budget" in response.json()["error"]["message"].lower()
    assert fake.calls == []


def test_openrouter_forwarding_rejects_invalid_key_shape_in_app_path(tmp_path):
    fake = FakeForwarder()
    client = TestClient(
        create_app(
            store_dir=tmp_path,
            policy=CostPolicy(max_total_usd=1),
            dry_run=False,
            enable_forwarding=True,
            allowed_forward_providers={"openrouter"},
            openrouter_api_key="not-an-openrouter-key",
            forwarder=fake,
        )
    )

    response = client.post(
        "/openrouter/v1/chat/completions",
        json={"model": "deepseek/deepseek-chat", "messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "agent_sentinel_invalid_api_key_format"
    assert fake.calls == []


def test_openrouter_forwarding_blocks_before_upstream_when_budget_exceeded(tmp_path):
    fake = FakeForwarder()
    client = TestClient(
        create_app(
            store_dir=tmp_path,
            policy=CostPolicy(max_total_usd=0.000001),
            dry_run=False,
            enable_forwarding=True,
            allowed_forward_providers={"openrouter"},
            openrouter_api_key="sk-or-v1-",
            forwarder=fake,
        )
    )

    response = client.post(
        "/openrouter/v1/chat/completions",
        json={"model": "anthropic/claude-sonnet-4", "messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 402
    assert fake.calls == []
    event = CostLedger(tmp_path).read_events()[0]
    assert event["decision"] == "blocked"


def test_openrouter_forwarding_records_forwarded_event_and_does_not_log_api_key(tmp_path):
    fake = FakeForwarder()
    client = TestClient(
        create_app(
            store_dir=tmp_path,
            policy=CostPolicy(max_total_usd=1),
            dry_run=False,
            enable_forwarding=True,
            allowed_forward_providers={"openrouter"},
            openrouter_api_key="sk-or-v1-",
            forwarder=fake,
        )
    )

    response = client.post(
        "/openrouter/v1/chat/completions",
        headers={"X-Agent-Sentinel-Run-Id": "run_openrouter_test"},
        json={"model": "deepseek/deepseek-chat", "messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["agent_sentinel"]["forwarded"] is True
    assert body["agent_sentinel"]["usage_confidence"] == "provider_reported"
    assert fake.calls[0]["api_key"] == "sk-or-v1-"
    event = CostLedger(tmp_path).read_events()[0]
    assert event["decision"] == "forwarded"
    assert event["usage_confidence"] == "provider_reported"
    assert event["run_id"] == "run_openrouter_test"
    assert "sk-or-v1-" not in str(event)


def test_openrouter_forwarding_transport_error_records_safe_event(tmp_path):
    class FailingForwarder:
        def __call__(self, **kwargs):
            raise TimeoutError("transport failure with ***")

    client = TestClient(
        create_app(
            store_dir=tmp_path,
            policy=CostPolicy(max_total_usd=1),
            dry_run=False,
            enable_forwarding=True,
            allowed_forward_providers={"openrouter"},
            openrouter_api_key="sk-or-v1-",
            forwarder=FailingForwarder(),
        )
    )

    response = client.post(
        "/openrouter/v1/chat/completions",
        json={"model": "deepseek/deepseek-chat", "messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["type"] == "agent_sentinel_transport_error"
    assert "***" not in str(body)
    events = CostLedger(tmp_path).read_events()
    assert len(events) == 1
    assert events[0]["decision"] == "transport_error"
    assert events[0]["usage_confidence"] == "estimated"
    assert "***" not in str(events[0])


def test_cli_forwarding_requires_budget_and_env_key(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_CHRONICLE_OPENROUTER_API_KEY", raising=False)
    result = CliRunner().invoke(app, ["cost", "proxy", "--enable-forwarding", "--forward-provider", "openrouter", "--store-dir", str(tmp_path)])

    assert result.exit_code != 0

    monkeypatch.setenv("AGENT_CHRONICLE_OPENROUTER_API_KEY", "sk-or-v1-")
    result = CliRunner().invoke(app, ["cost", "proxy", "--enable-forwarding", "--forward-provider", "openrouter", "--max-total-usd", "0.01", "--dry-run-only", "--store-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert "Dry-run-only validation passed" in result.output
