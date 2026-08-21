from agentacct.cost import (
    estimate_anthropic_messages_usage,
    estimate_gemini_generate_content_usage,
    estimate_openai_chat_usage,
)


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
