from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .env_compat import read_env_alias
from .cost import CostLedger, CostPolicy, UsageEstimate, estimate_anthropic_messages_usage, estimate_gemini_generate_content_usage, estimate_openai_chat_usage
from .proxy import usage_from_provider_response

HttpPost = Callable[..., Any]


@dataclass(frozen=True)
class ProviderSmokeSpec:
    provider: str
    env_var: str
    endpoint: str
    url: str
    model: str
    payload: dict[str, Any]
    headers: dict[str, str] = field(repr=False)
    fallback: UsageEstimate


class ProviderSmokeError(RuntimeError):
    pass


def build_provider_smoke_spec(
    provider: str,
    api_key: str,
    *,
    model: str | None = None,
    prompt: str = "Reply with exactly: ok",
    max_tokens: int = 4,
) -> ProviderSmokeSpec:
    provider = provider.lower().strip()
    if max_tokens <= 0:
        raise ProviderSmokeError("max_tokens must be positive")
    if provider == "openai":
        selected_model = model or "gpt-4o-mini"
        payload = {
            "model": selected_model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        return ProviderSmokeSpec(
            provider="openai",
            env_var="AGENTACCT_OPENAI_API_KEY",
            endpoint="/openai/v1/chat/completions",
            url="https://api.openai.com/v1/chat/completions",
            model=selected_model,
            payload=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            fallback=estimate_openai_chat_usage(payload, provider="openai", endpoint="/openai/v1/chat/completions"),
        )
    if provider == "deepseek":
        selected_model = model or "deepseek-chat"
        payload = {
            "model": selected_model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        return ProviderSmokeSpec(
            provider="deepseek",
            env_var="AGENTACCT_DEEPSEEK_API_KEY",
            endpoint="/deepseek/v1/chat/completions",
            url="https://api.deepseek.com/v1/chat/completions",
            model=selected_model,
            payload=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            fallback=estimate_openai_chat_usage(payload, provider="deepseek", endpoint="/deepseek/v1/chat/completions"),
        )
    if provider == "anthropic":
        selected_model = model or "claude-haiku-4-5-20251001"
        payload = {
            "model": selected_model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        }
        return ProviderSmokeSpec(
            provider="anthropic",
            env_var="AGENTACCT_ANTHROPIC_API_KEY",
            endpoint="/anthropic/v1/messages",
            url="https://api.anthropic.com/v1/messages",
            model=selected_model,
            payload=payload,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
            fallback=estimate_anthropic_messages_usage(payload),
        )
    if provider == "gemini":
        selected_model = model or "gemini-2.5-flash"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0},
        }
        return ProviderSmokeSpec(
            provider="gemini",
            env_var="AGENTACCT_GEMINI_API_KEY",
            endpoint=f"/gemini/v1beta/models/{selected_model}:generateContent",
            url=f"https://generativelanguage.googleapis.com/v1beta/models/{selected_model}:generateContent",
            model=selected_model,
            payload=payload,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            fallback=estimate_gemini_generate_content_usage(selected_model, payload),
        )
    raise ProviderSmokeError(f"Unsupported provider: {provider}")


def env_var_for_provider(provider: str) -> str:
    provider = provider.lower().strip()
    mapping = {
        "openai": "AGENTACCT_OPENAI_API_KEY",
        "anthropic": "AGENTACCT_ANTHROPIC_API_KEY",
        "gemini": "AGENTACCT_GEMINI_API_KEY",
        "deepseek": "AGENTACCT_DEEPSEEK_API_KEY",
    }
    try:
        return mapping[provider]
    except KeyError as exc:
        raise ProviderSmokeError(f"Unsupported provider: {provider}") from exc


def run_provider_smoke(
    provider: str,
    *,
    max_usd: float,
    store_dir: Path | str | None = None,
    model: str | None = None,
    prompt: str = "Reply with exactly: ok",
    max_tokens: int = 4,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
) -> dict[str, Any]:
    if store_dir is None:
        # No silent home-store default anywhere (Phase 1 decision 4a): fail
        # upfront with an actionable message instead of a ValueError deep
        # inside CostLedger after budget/env validation.
        raise ProviderSmokeError(
            "store_dir is required: pass an explicit store directory "
            "(e.g. resolve one with agentacct.store_resolution.resolve_store_dir()); "
            "there is no ~/.agent-sentinel fallback anymore"
        )
    if max_usd <= 0:
        raise ProviderSmokeError("max_usd must be positive")
    if max_usd > 0.05:
        raise ProviderSmokeError("provider smoke max_usd must be <= 0.05")

    env = os.environ if env is None else env
    env_var = env_var_for_provider(provider)
    # New AGENTACCT_* name wins; pre-rename AGENT_CHRONICLE_* / AGENT_SENTINEL_*
    # accepted silently (read_env_alias).
    api_key = (read_env_alias(env_var, env) or "").strip()
    if not api_key:
        raise ProviderSmokeError(f"Missing {env_var}")
    if api_key[0] in {'\"', "'"} or api_key[-1:] in {'\"', "'"}:
        raise ProviderSmokeError(f"{env_var} appears to be wrapped in quotes")

    spec = build_provider_smoke_spec(provider, api_key, model=model, prompt=prompt, max_tokens=max_tokens)
    ledger = CostLedger(store_dir)
    decision = CostPolicy(max_total_usd=max_usd).check(spec.fallback, already_spent_usd=0.0)
    if not decision.allowed:
        event = ledger.record_usage(spec.fallback, run_id="real_provider_smoke", decision="blocked", reason=decision.reason)
        return _safe_summary(spec, event, status_code=None, decision="blocked", reason=decision.reason)

    if http_post is None:
        import httpx

        http_post = httpx.post

    try:
        response = http_post(spec.url, headers=spec.headers, json=spec.payload, timeout=60)
    except Exception:
        reason = "transport error during provider request"
        event = ledger.record_usage(spec.fallback, run_id="real_provider_smoke", decision="transport_error", reason=reason)
        return _safe_summary(spec, event, status_code=None, decision="transport_error", reason=reason)
    status_code = int(getattr(response, "status_code", 0) or 0)
    try:
        body = response.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    usage = usage_from_provider_response(spec.provider, spec.endpoint, spec.model, spec.fallback, body)
    if status_code == 429:
        decision_name = "rate_limited"
    else:
        decision_name = "forwarded" if 200 <= status_code < 300 else "provider_error"
    usage.forwarded = decision_name == "forwarded"
    reason = "real provider smoke completed" if decision_name == "forwarded" else f"provider returned HTTP {status_code}"
    event = ledger.record_usage(usage, run_id="real_provider_smoke", decision=decision_name, reason=reason)
    return _safe_summary(spec, event, status_code=status_code, decision=decision_name, reason=reason)


def _safe_summary(
    spec: ProviderSmokeSpec,
    event: dict[str, Any],
    *,
    status_code: int | None,
    decision: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "provider": spec.provider,
        "model": spec.model,
        "status_code": status_code,
        "decision": decision,
        "reason": reason,
        "event_id": event["event_id"],
        "usage_confidence": event["usage_confidence"],
        "cost_confidence": event["cost_confidence"],
        "estimated_input_tokens": event["estimated_input_tokens"],
        "estimated_output_tokens": event["estimated_output_tokens"],
        "estimated_cost_usd": event["estimated_cost_usd"],
        "actual_provider_cost_usd": event["actual_provider_cost_usd"],
        "store_decision": event["decision"],
    }
