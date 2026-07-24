from agent_chronicle.cost import estimate_gemini_generate_content_usage, estimate_model_cost_breakdown_usd, estimate_model_cost_usd, estimate_openai_chat_usage
from agent_chronicle.proxy import usage_from_provider_response


def test_openai_compatible_contract_extracts_usage_and_cost():
    fallback = estimate_openai_chat_usage(
        {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 8},
        provider="openai",
        endpoint="/openai/v1/chat/completions",
    )

    usage = usage_from_provider_response(
        provider="openai",
        endpoint="/openai/v1/chat/completions",
        model="gpt-4o-mini",
        fallback=fallback,
        body={
            "id": "chatcmpl_mock",
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
                "total_cost_usd": 0.0000042,
            },
        },
    )

    assert usage.provider == "openai"
    assert usage.estimated_input_tokens == 11
    assert usage.estimated_output_tokens == 7
    assert usage.actual_provider_cost_usd == 0.0000042
    assert usage.usage_confidence == "provider_reported"
    assert usage.cost_confidence == "provider_billed"
    assert usage.forwarded is True


def test_deepseek_openai_compatible_contract_extracts_usage_without_cost_as_estimated_cost():
    fallback = estimate_openai_chat_usage(
        {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 8},
        provider="deepseek",
        endpoint="/deepseek/v1/chat/completions",
    )

    usage = usage_from_provider_response(
        provider="deepseek",
        endpoint="/deepseek/v1/chat/completions",
        model="deepseek-chat",
        fallback=fallback,
        body={"usage": {"prompt_tokens": 13, "completion_tokens": 5, "total_tokens": 18}},
    )

    assert usage.estimated_input_tokens == 13
    assert usage.estimated_output_tokens == 5
    assert usage.actual_provider_cost_usd is None
    assert usage.usage_confidence == "provider_reported"
    assert usage.cost_confidence == "estimated_from_tokens"


def test_anthropic_contract_extracts_native_input_output_tokens():
    fallback = estimate_openai_chat_usage(
        {"model": "claude-3-5-haiku", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 8},
        provider="anthropic",
        endpoint="/anthropic/v1/messages",
    )

    usage = usage_from_provider_response(
        provider="anthropic",
        endpoint="/anthropic/v1/messages",
        model="claude-3-5-haiku",
        fallback=fallback,
        body={"usage": {"input_tokens": 21, "output_tokens": 9}},
    )

    assert usage.estimated_input_tokens == 21
    assert usage.estimated_output_tokens == 9
    assert usage.usage_confidence == "provider_reported"
    assert usage.cost_confidence == "estimated_from_tokens"


def test_claude_cache_pricing_uses_write_and_read_rates():
    breakdown = estimate_model_cost_breakdown_usd(
        "claude-code",
        "claude-opus-4-8",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_input_tokens=2_000_000,
        cache_read_input_tokens=10_000_000,
        cache_creation_5m_input_tokens=1_000_000,
        cache_creation_1h_input_tokens=1_000_000,
    )

    assert breakdown["input_cost_usd"] == 5.0
    assert breakdown["output_cost_usd"] == 25.0
    assert breakdown["cache_creation_cost_usd"] == 16.25
    assert breakdown["cache_read_cost_usd"] == 5.0
    assert breakdown["total_cost_usd"] == 51.25


def test_codex_gpt_5_5_pricing_matches_ccusage_fast_cache_convention():
    breakdown = estimate_model_cost_breakdown_usd(
        "codex",
        "gpt-5.5",
        input_tokens=4_319_238,
        output_tokens=363_198,
        cache_read_input_tokens=106_956_672,
    )

    assert round(breakdown["input_cost_usd"], 2) == 53.99
    assert round(breakdown["output_cost_usd"], 2) == 27.24
    assert round(breakdown["cache_read_cost_usd"], 2) == 133.70
    assert round(breakdown["total_cost_usd"], 2) == 214.93


def test_gemini_contract_extracts_usage_metadata_shape():
    fallback = estimate_gemini_generate_content_usage(
        "gemini-2.5-flash",
        {"contents": [{"role": "user", "parts": [{"text": "hello"}]}], "generationConfig": {"maxOutputTokens": 8}},
    )

    usage = usage_from_provider_response(
        provider="gemini",
        endpoint="/gemini/v1beta/models/gemini-2.5-flash:generateContent",
        model="gemini-2.5-flash",
        fallback=fallback,
        body={
            "candidates": [{"content": {"parts": [{"text": "hi"}]}}],
            "usageMetadata": {
                "promptTokenCount": 17,
                "candidatesTokenCount": 6,
                "totalTokenCount": 23,
            },
        },
    )

    assert usage.estimated_input_tokens == 17
    assert usage.estimated_output_tokens == 6
    assert usage.usage_confidence == "provider_reported"
    assert usage.cost_confidence == "estimated_from_tokens"


def test_provider_error_contract_keeps_fallback_estimated_confidence():
    fallback = estimate_openai_chat_usage(
        {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 8},
        provider="openai",
        endpoint="/openai/v1/chat/completions",
    )

    usage = usage_from_provider_response(
        provider="openai",
        endpoint="/openai/v1/chat/completions",
        model="gpt-4o-mini",
        fallback=fallback,
        body={"error": {"message": "invalid api key", "type": "invalid_request_error"}},
    )

    assert usage.estimated_input_tokens == fallback.estimated_input_tokens
    assert usage.estimated_output_tokens == fallback.estimated_output_tokens
    assert usage.actual_provider_cost_usd is None
    assert usage.usage_confidence == "estimated"
    assert usage.cost_confidence == "estimated_from_tokens"
    assert usage.forwarded is True


def test_provider_cost_rejects_non_finite_or_negative_values():
    fallback = estimate_openai_chat_usage(
        {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 8},
        provider="openai",
        endpoint="/openai/v1/chat/completions",
    )

    for bad_usage in (
        {"prompt_tokens": 1, "completion_tokens": 1, "cost": "nan"},
        {"prompt_tokens": 1, "completion_tokens": 1, "cost": "inf"},
        {"prompt_tokens": 1, "completion_tokens": 1, "cost": "-1"},
        {"prompt_tokens": 1, "completion_tokens": 1, "prompt_cost": "1e308", "completion_cost": "1e308"},
    ):
        usage = usage_from_provider_response(
            provider="openai",
            endpoint="/openai/v1/chat/completions",
            model="gpt-4o-mini",
            fallback=fallback,
            body={"usage": bad_usage},
        )
        assert usage.actual_provider_cost_usd is None
        assert usage.cost_confidence == "estimated_from_tokens"


def test_exact_usage_recomputes_estimated_cost_from_returned_tokens():
    fallback = estimate_openai_chat_usage(
        {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 1},
        provider="openai",
        endpoint="/openai/v1/chat/completions",
    )

    usage = usage_from_provider_response(
        provider="openai",
        endpoint="/openai/v1/chat/completions",
        model="gpt-4o-mini",
        fallback=fallback,
        body={"usage": {"prompt_tokens": 1000, "completion_tokens": 1000, "total_tokens": 2000}},
    )

    assert usage.estimated_input_tokens == 1000
    assert usage.estimated_output_tokens == 1000
    assert usage.estimated_cost_usd == estimate_model_cost_usd("openai", "gpt-4o-mini", 1000, 1000)
    assert usage.estimated_cost_usd > fallback.estimated_cost_usd
    assert usage.cost_confidence == "estimated_from_tokens"
