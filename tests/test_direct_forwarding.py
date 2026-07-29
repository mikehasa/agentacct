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
                "id": f"fake-{provider}-response",
                "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
            },
        }


def test_openai_forwarding_is_disabled_by_default_even_with_forwarder(tmp_path):
    fake = FakeForwarder()
    client = TestClient(create_app(store_dir=tmp_path, policy=CostPolicy(max_total_usd=1), dry_run=True, forwarder=fake))

    response = client.post(
        "/openai/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 200
    assert response.json()["agent_sentinel"]["forwarded"] is False
    assert fake.calls == []


def test_openai_forwarding_requires_provider_allowlist_and_key(tmp_path):
    fake = FakeForwarder()
    client = TestClient(
        create_app(
            store_dir=tmp_path,
            policy=CostPolicy(max_total_usd=1),
            dry_run=False,
            enable_forwarding=True,
            allowed_forward_providers={"deepseek"},
            openai_api_key="FAKE_TEST_KEY",
            forwarder=fake,
        )
    )

    response = client.post(
        "/openai/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 200
    assert response.json()["agent_sentinel"]["forwarded"] is False
    assert fake.calls == []

    client_missing_key = TestClient(
        create_app(
            store_dir=tmp_path,
            policy=CostPolicy(max_total_usd=1),
            dry_run=False,
            enable_forwarding=True,
            allowed_forward_providers={"openai"},
            forwarder=fake,
        )
    )
    missing_key = client_missing_key.post(
        "/openai/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert missing_key.status_code == 400
    assert missing_key.json()["error"]["type"] == "agent_sentinel_missing_api_key"


def test_openai_forwarding_requires_local_budget_cap_before_upstream(tmp_path):
    fake = FakeForwarder()
    client = TestClient(
        create_app(
            store_dir=tmp_path,
            policy=CostPolicy(),
            dry_run=False,
            enable_forwarding=True,
            allowed_forward_providers={"openai"},
            openai_api_key="FAKE_TEST_KEY",
            forwarder=fake,
        )
    )

    response = client.post(
        "/openai/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "agent_sentinel_missing_budget"
    assert fake.calls == []


def test_deepseek_forwarding_blocks_before_upstream_when_budget_exceeded(tmp_path):
    fake = FakeForwarder()
    client = TestClient(
        create_app(
            store_dir=tmp_path,
            policy=CostPolicy(max_total_usd=0.000001),
            dry_run=False,
            enable_forwarding=True,
            allowed_forward_providers={"deepseek"},
            deepseek_api_key="FAKE_TEST_KEY",
            forwarder=fake,
        )
    )

    response = client.post(
        "/deepseek/v1/chat/completions",
        json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 32},
    )

    assert response.status_code == 402
    assert fake.calls == []
    event = CostLedger(tmp_path).read_events()[0]
    assert event["decision"] == "blocked"


def test_openai_forwarding_non_2xx_upstream_records_provider_error(tmp_path):
    class ErrorForwarder:
        def __call__(self, **kwargs):
            return {"status_code": 401, "json": {"error": {"message": "bad key", "type": "auth_error"}}}

    client = TestClient(
        create_app(
            store_dir=tmp_path,
            policy=CostPolicy(max_total_usd=1),
            dry_run=False,
            enable_forwarding=True,
            allowed_forward_providers={"openai"},
            openai_api_key="FAKE_TEST_KEY",
            forwarder=ErrorForwarder(),
        )
    )

    response = client.post(
        "/openai/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 401
    body = response.json()
    assert body["agent_sentinel"]["forwarded"] is False
    assert body["agent_sentinel"]["allowed"] is False
    ledger = CostLedger(tmp_path)
    event = ledger.read_events()[0]
    assert event["decision"] == "provider_error"
    assert event["forwarded"] is False
    assert ledger.total_billable_cost_usd() == 0.0


def test_openai_forwarding_records_exact_usage_and_hides_key(tmp_path):
    fake = FakeForwarder()
    client = TestClient(
        create_app(
            store_dir=tmp_path,
            policy=CostPolicy(max_total_usd=1),
            dry_run=False,
            enable_forwarding=True,
            allowed_forward_providers={"openai"},
            openai_api_key="FAKE_TEST_KEY",
            forwarder=fake,
        )
    )

    response = client.post(
        "/openai/v1/chat/completions",
        headers={"X-Agent-Sentinel-Run-Id": "run_openai_forwarding"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["agent_sentinel"]["forwarded"] is True
    assert body["agent_sentinel"]["usage_confidence"] == "provider_reported"
    assert fake.calls[0]["provider"] == "openai"
    assert fake.calls[0]["endpoint"] == "/openai/v1/chat/completions"
    assert fake.calls[0]["api_key"] == "FAKE_TEST_KEY"
    assert fake.calls[0]["upstream_base_url"] == "https://api.openai.com"
    event = CostLedger(tmp_path).read_events(run_id="run_openai_forwarding")[0]
    assert event["decision"] == "forwarded"
    assert event["provider"] == "openai"
    assert event["estimated_input_tokens"] == 11
    assert event["estimated_output_tokens"] == 3
    assert "FAKE_TEST_KEY" not in str(event)
    assert "FAKE_TEST_KEY" not in str(body)


def test_deepseek_forwarding_uses_deepseek_base_url_and_records_usage(tmp_path):
    fake = FakeForwarder()
    client = TestClient(
        create_app(
            store_dir=tmp_path,
            policy=CostPolicy(max_total_usd=1),
            dry_run=False,
            enable_forwarding=True,
            allowed_forward_providers={"deepseek"},
            deepseek_api_key="FAKE_TEST_KEY",
            forwarder=fake,
        )
    )

    response = client.post(
        "/deepseek/v1/chat/completions",
        headers={"X-Agent-Sentinel-Run-Id": "run_deepseek_forwarding"},
        json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["agent_sentinel"]["forwarded"] is True
    assert body["agent_sentinel"]["usage_confidence"] == "provider_reported"
    assert fake.calls[0]["provider"] == "deepseek"
    assert fake.calls[0]["endpoint"] == "/deepseek/v1/chat/completions"
    assert fake.calls[0]["api_key"] == "FAKE_TEST_KEY"
    assert fake.calls[0]["upstream_base_url"] == "https://api.deepseek.com"
    event = CostLedger(tmp_path).read_events(run_id="run_deepseek_forwarding")[0]
    assert event["decision"] == "forwarded"
    assert event["provider"] == "deepseek"
    assert event["estimated_input_tokens"] == 11
    assert event["estimated_output_tokens"] == 3
    assert "FAKE_TEST_KEY" not in str(event)
    assert "FAKE_TEST_KEY" not in str(body)


def test_deepseek_forwarding_transport_error_records_safe_event(tmp_path):
    class FailingForwarder:
        def __call__(self, **kwargs):
            raise TimeoutError("deepseek transport failure with ***")

    client = TestClient(
        create_app(
            store_dir=tmp_path,
            policy=CostPolicy(max_total_usd=1),
            dry_run=False,
            enable_forwarding=True,
            allowed_forward_providers={"deepseek"},
            deepseek_api_key="FAKE_TEST_KEY",
            forwarder=FailingForwarder(),
        )
    )

    response = client.post(
        "/deepseek/v1/chat/completions",
        json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["type"] == "agent_sentinel_transport_error"
    assert "FAKE_TEST_KEY" not in str(body)
    events = CostLedger(tmp_path).read_events()
    assert len(events) == 1
    assert events[0]["decision"] == "transport_error"
    assert events[0]["provider"] == "deepseek"
    assert "FAKE_TEST_KEY" not in str(events[0])


def test_cli_accepts_openai_and_deepseek_forwarding_keys_for_dry_run_only(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CHRONICLE_OPENAI_API_KEY", "FAKE_TEST_KEY")
    monkeypatch.setenv("AGENT_CHRONICLE_DEEPSEEK_API_KEY", "FAKE_TEST_KEY")

    result = CliRunner().invoke(
        app,
        [
            "cost",
            "proxy",
            "--enable-forwarding",
            "--forward-provider",
            "openai",
            "--forward-provider",
            "deepseek",
            "--max-total-usd",
            "0.01",
            "--dry-run-only",
            "--store-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Dry-run-only validation passed" in result.output
    assert "FAKE_TEST_KEY" not in result.output
