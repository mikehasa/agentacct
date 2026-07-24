import pytest

from agent_chronicle.cost import CostLedger
from agent_chronicle.deep_provider_validation import build_deep_validation_cases, run_deep_provider_validation
from agent_chronicle.provider_smoke import ProviderSmokeError


class MockResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _mock_response_for_url(url, payload):
    model = payload.get("model") or url.rsplit("/models/", 1)[-1].split(":", 1)[0]
    if model == "agent-chronicle-nonexistent-model":
        return MockResponse(404, {"error": {"message": "model not found", "type": "not_found_error"}})
    if "generativelanguage.googleapis.com" in url:
        max_tokens = int((payload.get("generationConfig") or {}).get("maxOutputTokens") or 4)
        return MockResponse(
            200,
            {"usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": min(max_tokens, 3), "totalTokenCount": 13}},
        )
    if "anthropic.com" in url:
        max_tokens = int(payload.get("max_tokens") or 4)
        return MockResponse(200, {"usage": {"input_tokens": 11, "output_tokens": min(max_tokens, 3)}})
    max_tokens = int(payload.get("max_tokens") or 4)
    return MockResponse(200, {"usage": {"prompt_tokens": 9, "completion_tokens": min(max_tokens, 3), "total_tokens": 12}})


def test_deep_validation_missing_key_does_not_call_network(tmp_path):
    called = False

    def fake_post(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("network should not be called")

    with pytest.raises(ProviderSmokeError, match="Missing AGENT_CHRONICLE_OPENAI_API_KEY"):
        run_deep_provider_validation(["openai"], max_provider_usd=0.01, store_dir=tmp_path, env={}, http_post=fake_post)

    assert called is False
    assert CostLedger(tmp_path).read_events() == []


def test_deep_validation_requires_bounded_positive_provider_budget(tmp_path):
    env = {"AGENT_CHRONICLE_OPENAI_API_KEY": "test-key"}

    with pytest.raises(ProviderSmokeError, match="positive"):
        run_deep_provider_validation(["openai"], max_provider_usd=0, store_dir=tmp_path, env=env)
    with pytest.raises(ProviderSmokeError, match="<= 1.0"):
        run_deep_provider_validation(["openai"], max_provider_usd=1.01, store_dir=tmp_path, env=env)


def test_deep_validation_rejects_duplicate_providers_before_network(tmp_path):
    called = False

    def fake_post(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("network should not be called")

    with pytest.raises(ProviderSmokeError, match="Duplicate provider: openai"):
        run_deep_provider_validation(
            ["openai", "OPENAI"],
            max_provider_usd=0.01,
            store_dir=tmp_path,
            env={"AGENT_CHRONICLE_OPENAI_API_KEY": "test-key"},
            http_post=fake_post,
        )

    assert called is False


def test_deep_validation_rejects_negative_delay_before_network(tmp_path):
    called = False

    def fake_post(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("network should not be called")

    with pytest.raises(ProviderSmokeError, match="delay_seconds must be non-negative"):
        run_deep_provider_validation(
            ["openai"],
            max_provider_usd=0.01,
            store_dir=tmp_path,
            env={"AGENT_CHRONICLE_OPENAI_API_KEY": "test-key"},
            http_post=fake_post,
            delay_seconds=-1,
        )

    assert called is False


def test_deep_validation_keeps_per_request_budget_under_smoke_cap(tmp_path):
    seen_payloads = []

    def fake_post(url, *, headers, json, timeout):
        seen_payloads.append(json)
        return _mock_response_for_url(url, json)

    result = run_deep_provider_validation(
        ["openai"],
        max_provider_usd=1.0,
        store_dir=tmp_path,
        env={"AGENT_CHRONICLE_OPENAI_API_KEY": "test-key"},
        http_post=fake_post,
    )

    assert result["overall_passed"] is True
    assert seen_payloads


def test_deep_validation_cases_cover_expected_matrix():
    names = [case.name for case in build_deep_validation_cases("openai")]

    assert "basic_success" in names
    assert names.count("sequential_1") == 1
    assert "sequential_5" in names
    assert "max_tokens_1" in names
    assert "max_tokens_8" in names
    assert "max_tokens_32" in names
    assert "longer_prompt" in names
    assert "provider_error_bad_model" in names
    assert "budget_guard_blocks_before_upstream" in names


def test_deep_validation_mock_openai_records_success_error_and_budget_guard(tmp_path):
    calls = []

    def fake_post(url, *, headers, json, timeout):
        calls.append({"url": url, "json": json, "auth": headers.get("Authorization") or headers.get("x-api-key") or headers.get("x-goog-api-key")})
        return _mock_response_for_url(url, json)

    result = run_deep_provider_validation(
        ["openai"],
        max_provider_usd=0.01,
        store_dir=tmp_path,
        env={"AGENT_CHRONICLE_OPENAI_API_KEY": "secret-test-key"},
        http_post=fake_post,
    )

    assert result["overall_passed"] is True
    provider = result["providers"]["openai"]
    assert provider["passed"] is True
    assert provider["case_count"] == len(build_deep_validation_cases("openai"))
    by_name = {case["name"]: case for case in provider["cases"]}
    assert by_name["basic_success"]["decision"] == "forwarded"
    assert by_name["provider_error_bad_model"]["decision"] == "provider_error"
    assert by_name["provider_error_bad_model"]["usage_confidence"] == "estimated"
    assert by_name["budget_guard_blocks_before_upstream"]["decision"] == "blocked"
    assert by_name["budget_guard_blocks_before_upstream"]["status_code"] is None
    assert len(calls) == len(build_deep_validation_cases("openai")) - 1
    assert calls[-1]["json"].get("model") == "agent-chronicle-nonexistent-model"
    assert "secret-test-key" not in repr(result)
    assert "secret-test-key" not in repr(CostLedger(tmp_path).read_events())


def test_deep_validation_mock_all_supported_providers(tmp_path):
    env = {
        "AGENT_CHRONICLE_OPENAI_API_KEY": "openai-test-key",
        "AGENT_CHRONICLE_ANTHROPIC_API_KEY": "anthropic-test-key",
        "AGENT_CHRONICLE_GEMINI_API_KEY": "gemini-test-key",
        "AGENT_CHRONICLE_DEEPSEEK_API_KEY": "deepseek-test-key",
    }

    result = run_deep_provider_validation(
        ["openai", "anthropic", "gemini", "deepseek"],
        max_provider_usd=0.01,
        store_dir=tmp_path,
        env=env,
        http_post=lambda url, *, headers, json, timeout: _mock_response_for_url(url, json),
    )

    assert result["overall_passed"] is True
    assert sorted(result["providers"].keys()) == ["anthropic", "deepseek", "gemini", "openai"]
    for provider_result in result["providers"].values():
        assert provider_result["passed"] is True
        assert provider_result["case_count"] == 12


def test_deep_validation_stops_provider_after_rate_limit(tmp_path):
    calls = []

    def rate_limited_after_first(url, *, headers, json, timeout):
        calls.append(json)
        if len(calls) == 1:
            return _mock_response_for_url(url, json)
        return MockResponse(429, {"error": {"message": "rate limited"}})

    result = run_deep_provider_validation(
        ["openai"],
        max_provider_usd=0.01,
        store_dir=tmp_path,
        env={"AGENT_CHRONICLE_OPENAI_API_KEY": "test-key"},
        http_post=rate_limited_after_first,
    )

    provider = result["providers"]["openai"]
    assert result["overall_passed"] is False
    assert provider["passed"] is False
    assert len(calls) == 2
    assert provider["cases"][1]["decision"] == "rate_limited"
    assert provider["cases"][2]["decision"] == "skipped_after_rate_limit"


def test_deep_validation_requires_explicit_store_dir_upfront():
    """Minor fix (Phase 1 review): no silent home-store default anywhere; the
    old None default crashed deep inside CostLedger after env validation."""
    with pytest.raises(ProviderSmokeError, match="store_dir is required"):
        run_deep_provider_validation(
            ["openai"], max_provider_usd=0.01, env={"AGENT_CHRONICLE_OPENAI_API_KEY": "test-key"}
        )
