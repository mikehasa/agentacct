from agentacct.cost import estimate_openai_chat_usage


def test_estimated_usage_has_estimated_cost_confidence_and_no_actual_provider_cost():
    usage = estimate_openai_chat_usage({"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hello"}]})

    assert usage.cost_confidence == "estimated_from_tokens"
    assert usage.actual_provider_cost_usd is None
