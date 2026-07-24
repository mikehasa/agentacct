import pytest

from agent_chronicle.cost import CostLedger
from agent_chronicle.provider_smoke import ProviderSmokeError, build_provider_smoke_spec, run_provider_smoke


class MockResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def test_provider_smoke_missing_key_does_not_call_network(tmp_path):
    called = False

    def fake_post(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("network should not be called")

    with pytest.raises(ProviderSmokeError, match="Missing AGENT_CHRONICLE_OPENAI_API_KEY"):
        run_provider_smoke("openai", max_usd=0.01, store_dir=tmp_path, env={}, http_post=fake_post)

    assert called is False
    assert CostLedger(tmp_path).read_events() == []


def test_provider_smoke_requires_small_positive_budget(tmp_path):
    env = {"AGENT_CHRONICLE_OPENAI_API_KEY": "test-key"}

    with pytest.raises(ProviderSmokeError, match="positive"):
        run_provider_smoke("openai", max_usd=0, store_dir=tmp_path, env=env)
    with pytest.raises(ProviderSmokeError, match="<= 0.05"):
        run_provider_smoke("openai", max_usd=0.10, store_dir=tmp_path, env=env)


def test_provider_smoke_rejects_quoted_key_before_network(tmp_path):
    called = False

    def fake_post(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("network should not be called")

    with pytest.raises(ProviderSmokeError, match="wrapped in quotes"):
        run_provider_smoke(
            "openai",
            max_usd=0.01,
            store_dir=tmp_path,
            env={"AGENT_CHRONICLE_OPENAI_API_KEY": "'test-key'"},
            http_post=fake_post,
        )

    assert called is False


def test_provider_smoke_openai_mock_records_safe_summary_without_secret(tmp_path):
    seen = {}

    def fake_post(url, *, headers, json, timeout):
        seen["url"] = url
        seen["headers"] = headers
        seen["json"] = json
        seen["timeout"] = timeout
        return MockResponse(
            200,
            {
                "id": "chatcmpl_mock",
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 3,
                    "total_tokens": 15,
                    "total_cost_usd": 0.000003,
                },
            },
        )

    summary = run_provider_smoke(
        "openai",
        max_usd=0.01,
        store_dir=tmp_path,
        env={"AGENT_CHRONICLE_OPENAI_API_KEY": "secret-test-key"},
        http_post=fake_post,
    )

    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer " + "secret-test-key"
    assert summary["provider"] == "openai"
    assert summary["status_code"] == 200
    assert summary["decision"] == "forwarded"
    assert summary["usage_confidence"] == "provider_reported"
    assert summary["cost_confidence"] == "provider_billed"
    assert summary["actual_provider_cost_usd"] == 0.000003
    assert "secret-test-key" not in repr(summary)

    events = CostLedger(tmp_path).read_events(run_id="real_provider_smoke")
    assert len(events) == 1
    assert events[0]["decision"] == "forwarded"
    assert "secret-test-key" not in repr(events[0])


@pytest.mark.parametrize(
    ("provider", "env_var", "expected_url"),
    [
        ("openai", "AGENT_CHRONICLE_OPENAI_API_KEY", "https://api.openai.com/v1/chat/completions"),
        ("anthropic", "AGENT_CHRONICLE_ANTHROPIC_API_KEY", "https://api.anthropic.com/v1/messages"),
        ("gemini", "AGENT_CHRONICLE_GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"),
        ("deepseek", "AGENT_CHRONICLE_DEEPSEEK_API_KEY", "https://api.deepseek.com/v1/chat/completions"),
    ],
)
def test_provider_smoke_specs_use_expected_endpoints(provider, env_var, expected_url):
    spec = build_provider_smoke_spec(provider, "secret-test-key")

    assert spec.env_var == env_var
    assert spec.url == expected_url
    assert spec.fallback.estimated_cost_usd > 0
    assert "secret-test-key" not in repr(spec.payload)


def test_provider_smoke_spec_repr_does_not_expose_secret_headers():
    spec = build_provider_smoke_spec("openai", "secret-test-key")

    assert "secret-test-key" not in repr(spec)
    assert "Authorization" not in repr(spec)


def test_provider_smoke_transport_error_records_safe_estimated_event(tmp_path):
    def failing_post(*args, **kwargs):
        raise TimeoutError("request timed out with secret-test-key in exception")

    summary = run_provider_smoke(
        "openai",
        max_usd=0.01,
        store_dir=tmp_path,
        env={"AGENT_CHRONICLE_OPENAI_API_KEY": "secret-test-key"},
        http_post=failing_post,
    )

    assert summary["decision"] == "transport_error"
    assert summary["status_code"] is None
    assert summary["usage_confidence"] == "estimated"
    assert summary["cost_confidence"] == "estimated_from_tokens"
    assert "secret-test-key" not in repr(summary)

    events = CostLedger(tmp_path).read_events(run_id="real_provider_smoke")
    assert len(events) == 1
    assert events[0]["decision"] == "transport_error"
    assert "secret-test-key" not in repr(events[0])


def test_provider_smoke_rate_limit_gets_explicit_decision(tmp_path):
    def rate_limited_post(*args, **kwargs):
        return MockResponse(429, {"error": {"message": "rate limited"}})

    summary = run_provider_smoke(
        "openai",
        max_usd=0.01,
        store_dir=tmp_path,
        env={"AGENT_CHRONICLE_OPENAI_API_KEY": "secret-test-key"},
        http_post=rate_limited_post,
    )

    assert summary["decision"] == "rate_limited"
    assert summary["status_code"] == 429
    events = CostLedger(tmp_path).read_events(run_id="real_provider_smoke")
    assert len(events) == 1
    assert events[0]["decision"] == "rate_limited"
    assert events[0]["forwarded"] is False


def test_provider_smoke_provider_error_records_not_forwarded(tmp_path):
    def provider_error_post(*args, **kwargs):
        return MockResponse(401, {"error": {"message": "bad key", "type": "auth_error"}})

    summary = run_provider_smoke(
        "openai",
        max_usd=0.01,
        store_dir=tmp_path,
        env={"AGENT_CHRONICLE_OPENAI_API_KEY": "secret-test-key"},
        http_post=provider_error_post,
    )

    assert summary["decision"] == "provider_error"
    assert summary["status_code"] == 401
    events = CostLedger(tmp_path).read_events(run_id="real_provider_smoke")
    assert len(events) == 1
    assert events[0]["decision"] == "provider_error"
    assert events[0]["forwarded"] is False
    assert CostLedger(tmp_path).total_billable_cost_usd(run_id="real_provider_smoke") == 0.0


def test_provider_smoke_requires_explicit_store_dir_upfront():
    """Minor fix (Phase 1 review): no silent home-store default anywhere; the
    old None default crashed deep inside CostLedger after env validation."""
    with pytest.raises(ProviderSmokeError, match="store_dir is required"):
        run_provider_smoke("openai", max_usd=0.01, env={"AGENT_CHRONICLE_OPENAI_API_KEY": "test-key"})
