from fastapi.testclient import TestClient

from agentacct.cost import (
    CostLedger,
    CostPolicy,
    estimate_anthropic_messages_usage,
    estimate_gemini_generate_content_usage,
    estimate_openai_chat_usage,
)
from agentacct.proxy import create_app


def test_anthropic_messages_estimator_parses_native_request():
    usage = estimate_anthropic_messages_usage(
        {
            "model": "claude-sonnet-4",
            "max_tokens": 256,
            "system": "You are careful.",
            "messages": [
                {"role": "user", "content": "Explain the failing tests."},
                {"role": "assistant", "content": [{"type": "text", "text": "I will inspect them."}]},
            ],
        }
    )

    assert usage.provider == "anthropic"
    assert usage.model == "claude-sonnet-4"
    assert usage.endpoint == "/anthropic/v1/messages"
    assert usage.estimated_input_tokens > 0
    assert usage.estimated_output_tokens == 256
    assert usage.forwarded is False


def test_gemini_generate_content_estimator_parses_native_request():
    usage = estimate_gemini_generate_content_usage(
        "gemini-2.5-pro",
        {
            "contents": [
                {"role": "user", "parts": [{"text": "Summarize this code."}]},
                {"role": "model", "parts": [{"text": "Sure."}]},
            ],
            "generationConfig": {"maxOutputTokens": 333},
        },
    )

    assert usage.provider == "gemini"
    assert usage.model == "gemini-2.5-pro"
    assert usage.endpoint == "/gemini/v1beta/models/gemini-2.5-pro:generateContent"
    assert usage.estimated_input_tokens > 0
    assert usage.estimated_output_tokens == 333
    assert usage.forwarded is False


def test_openrouter_and_deepseek_are_namespaced_openai_compatible():
    openrouter = estimate_openai_chat_usage(
        {"model": "anthropic/claude-sonnet-4", "messages": [{"role": "user", "content": "hello"}]},
        provider="openrouter",
        endpoint="/openrouter/v1/chat/completions",
    )
    deepseek = estimate_openai_chat_usage(
        {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hello"}]},
        provider="deepseek",
        endpoint="/deepseek/v1/chat/completions",
    )

    assert openrouter.provider == "openrouter"
    assert openrouter.model == "anthropic/claude-sonnet-4"
    assert deepseek.provider == "deepseek"
    assert deepseek.model == "deepseek-chat"
    assert openrouter.estimated_cost_usd != deepseek.estimated_cost_usd


def test_anthropic_proxy_dry_run_records_event(tmp_path):
    client = TestClient(create_app(store_dir=tmp_path, policy=CostPolicy(max_total_usd=1.0), dry_run=True))

    response = client.post(
        "/anthropic/v1/messages",
        json={"model": "claude-sonnet-4", "max_tokens": 128, "messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 200
    assert response.json()["agent_sentinel"]["forwarded"] is False
    events = CostLedger(tmp_path).read_events()
    assert events[-1]["provider"] == "anthropic"


def test_gemini_proxy_dry_run_records_event(tmp_path):
    client = TestClient(create_app(store_dir=tmp_path, policy=CostPolicy(max_total_usd=1.0), dry_run=True))

    response = client.post(
        "/gemini/v1beta/models/gemini-2.5-flash:generateContent",
        json={"contents": [{"parts": [{"text": "hello gemini"}]}], "generationConfig": {"maxOutputTokens": 64}},
    )

    assert response.status_code == 200
    assert response.json()["agent_sentinel"]["forwarded"] is False
    events = CostLedger(tmp_path).read_events()
    assert events[-1]["provider"] == "gemini"
    assert events[-1]["model"] == "gemini-2.5-flash"


def test_openrouter_and_deepseek_proxy_routes_are_dry_run(tmp_path):
    client = TestClient(create_app(store_dir=tmp_path, policy=CostPolicy(max_total_usd=1.0), dry_run=True))

    r1 = client.post(
        "/openrouter/v1/chat/completions",
        json={"model": "anthropic/claude-sonnet-4", "messages": [{"role": "user", "content": "hello"}]},
    )
    r2 = client.post(
        "/deepseek/v1/chat/completions",
        json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "hello"}]},
    )

    assert r1.status_code == 200
    assert r2.status_code == 200
    events = CostLedger(tmp_path).read_events()
    assert [event["provider"] for event in events[-2:]] == ["openrouter", "deepseek"]
    assert all(event["forwarded"] is False for event in events[-2:])
