from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Callable, Iterable

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from .confidence import COST_PROVIDER_BILLED, USAGE_PROVIDER_REPORTED
from .localhost_guard import install_localhost_guard
from .cost import (
    CostLedger,
    CostPolicy,
    UsageEstimate,
    estimate_anthropic_messages_usage,
    estimate_gemini_generate_content_usage,
    estimate_model_cost_usd,
    estimate_openai_chat_usage,
)

Forwarder = Callable[..., dict[str, Any]]


def _default_forwarder(*, provider: str, endpoint: str, payload: dict[str, Any], api_key: str, upstream_base_url: str) -> dict[str, Any]:
    """Synchronous upstream call used only when forwarding is explicitly enabled."""
    import httpx

    url = upstream_base_url.rstrip("/") + endpoint.replace(f"/{provider}", "", 1)
    response = httpx.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    try:
        body = response.json()
    except Exception:
        body = {"raw_text": response.text}
    return {"status_code": response.status_code, "json": body}


def usage_from_provider_response(
    provider: str,
    endpoint: str,
    model: str,
    fallback: UsageEstimate,
    body: dict[str, Any],
) -> UsageEstimate:
    """Normalize provider-specific response usage into a agentacct usage event.

    This is a contract-test seam for direct provider adapters. It deliberately
    accepts already-parsed JSON and never performs network calls or handles API
    keys. Providers that do not return recognizable usage keep the preflight
    estimate instead of pretending the result is exact.
    """
    usage = _extract_usage_payload(provider, body)
    if not isinstance(usage, dict):
        return UsageEstimate(
            provider=provider,
            model=model,
            endpoint=endpoint,
            estimated_input_tokens=fallback.estimated_input_tokens,
            estimated_output_tokens=fallback.estimated_output_tokens,
            estimated_cost_usd=fallback.estimated_cost_usd,
            actual_provider_cost_usd=None,
            cost_confidence=fallback.cost_confidence,
            usage_confidence=fallback.usage_confidence,
            forwarded=True,
        )

    input_tokens = _coerce_int(
        usage.get("prompt_tokens")
        or usage.get("input_tokens")
        or usage.get("promptTokenCount")
        or usage.get("prompt_token_count")
        or fallback.estimated_input_tokens,
        fallback.estimated_input_tokens,
    )
    output_tokens = _coerce_int(
        usage.get("completion_tokens")
        or usage.get("output_tokens")
        or usage.get("candidatesTokenCount")
        or usage.get("candidates_token_count")
        or fallback.estimated_output_tokens,
        fallback.estimated_output_tokens,
    )
    actual_cost = _extract_actual_cost_usd(usage)
    return UsageEstimate(
        provider=provider,
        model=model,
        endpoint=endpoint,
        estimated_input_tokens=input_tokens,
        estimated_output_tokens=output_tokens,
        estimated_cost_usd=estimate_model_cost_usd(provider, model, input_tokens, output_tokens),
        actual_provider_cost_usd=actual_cost,
        cost_confidence=COST_PROVIDER_BILLED if actual_cost is not None else fallback.cost_confidence,
        usage_confidence=USAGE_PROVIDER_REPORTED,
        forwarded=True,
    )


def _usage_from_upstream(provider: str, endpoint: str, model: str, fallback: UsageEstimate, body: dict[str, Any]) -> UsageEstimate:
    return usage_from_provider_response(provider, endpoint, model, fallback, body)


def _extract_usage_payload(provider: str, body: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(body, dict) or isinstance(body.get("error"), dict):
        return None
    usage = body.get("usage")
    if isinstance(usage, dict):
        return usage
    if provider == "gemini":
        usage_metadata = body.get("usageMetadata") or body.get("usage_metadata")
        if isinstance(usage_metadata, dict):
            return usage_metadata
    return None


def _coerce_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _valid_provider_cost(value: Any) -> float | None:
    try:
        cost = float(value)
    except (TypeError, ValueError):
        return None
    if math.isfinite(cost) and cost >= 0:
        return cost
    return None


def _extract_actual_cost_usd(usage: dict[str, Any]) -> float | None:
    for key in ("cost", "total_cost", "total_cost_usd", "actual_cost_usd"):
        value = usage.get(key)
        if value is not None:
            cost = _valid_provider_cost(value)
            if cost is not None:
                return cost
    prompt_cost = usage.get("prompt_cost") or usage.get("input_cost")
    completion_cost = usage.get("completion_cost") or usage.get("output_cost")
    if prompt_cost is not None or completion_cost is not None:
        prompt = _valid_provider_cost(prompt_cost or 0.0)
        completion = _valid_provider_cost(completion_cost or 0.0)
        if prompt is not None and completion is not None:
            return _valid_provider_cost(prompt + completion)
    return None


def create_app(
    *,
    store_dir: Path | str,
    policy: CostPolicy | None = None,
    dry_run: bool = True,
    enable_forwarding: bool = False,
    allowed_forward_providers: set[str] | None = None,
    openrouter_api_key: str | None = None,
    openai_api_key: str | None = None,
    deepseek_api_key: str | None = None,
    openrouter_upstream_base_url: str = "https://openrouter.ai/api",
    openai_upstream_base_url: str = "https://api.openai.com",
    deepseek_upstream_base_url: str = "https://api.deepseek.com",
    forwarder: Forwarder | None = None,
    extra_allowed_hosts: Iterable[str] = (),
) -> FastAPI:
    """Create the v0 cost proxy app.

    Safety boundary: forwarding is disabled by default. Real forwarding requires
    explicit enable_forwarding=True, an allowlisted provider, provider-specific
    API key, and a hard USD budget.
    """
    # FROZEN WIRE VOCABULARY (pre-rename, kept forever): the "agent_sentinel"
    # response-envelope key, the "agent_sentinel_*" error type strings, and the
    # X-Agent-Sentinel-Run-Id header are parsed by deployed agent loops and
    # hook consumers; they are wire format, not branding — never rename.
    app = FastAPI(title="agentacct cost proxy", version="0.1.0")
    ledger = CostLedger(store_dir)
    policy = policy or CostPolicy()
    allowed_forward_providers = allowed_forward_providers or set()
    forwarder = forwarder or _default_forwarder

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "dry_run": dry_run, "forwarding_enabled": enable_forwarding}

    def _policy_decision(usage: UsageEstimate):
        return policy.check(
            usage,
            already_spent_usd=ledger.total_billable_cost_usd(),
            already_used_tokens=ledger.total_estimated_tokens(),
            already_used_input_tokens=ledger.total_estimated_input_tokens(),
            already_used_output_tokens=ledger.total_estimated_output_tokens(),
        )

    def _blocked_response(usage: UsageEstimate, run_id: str | None, reason: str, status_code: int = 402) -> tuple[int, dict[str, Any]]:
        event = ledger.record_usage(usage, run_id=run_id, decision="blocked", reason=reason)
        return status_code, {
            "error": {"message": reason, "type": "agent_sentinel_budget_exceeded"},
            "agent_sentinel": {
                "allowed": False,
                "dry_run": dry_run,
                "forwarded": False,
                "event_id": event["event_id"],
                "provider": usage.provider,
                "model": usage.model,
                "usage_confidence": usage.usage_confidence,
                "estimated_cost_usd": usage.estimated_cost_usd,
                "actual_provider_cost_usd": usage.actual_provider_cost_usd,
                "cost_confidence": usage.cost_confidence,
                "estimated_input_tokens": usage.estimated_input_tokens,
                "estimated_output_tokens": usage.estimated_output_tokens,
            },
        }

    def evaluate_and_record(usage: UsageEstimate, run_id: str | None) -> tuple[int, dict[str, Any]]:
        decision = _policy_decision(usage)
        if not decision.allowed:
            return _blocked_response(usage, run_id, decision.reason)

        event = ledger.record_usage(
            usage,
            run_id=run_id,
            decision="dry_run_allow" if dry_run else "would_forward_disabled_in_v0",
            reason=decision.reason,
        )
        return 200, {
            "id": "agent-chronicle-dry-run",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "agentacct dry-run proxy: request recorded, not forwarded upstream.",
                    },
                }
            ],
            "usage": {
                "prompt_tokens": usage.estimated_input_tokens,
                "completion_tokens": usage.estimated_output_tokens,
                "total_tokens": usage.estimated_input_tokens + usage.estimated_output_tokens,
            },
            "agent_sentinel": {
                "allowed": True,
                "dry_run": dry_run,
                "forwarded": False,
                "event_id": event["event_id"],
                "provider": usage.provider,
                "model": usage.model,
                "usage_confidence": usage.usage_confidence,
                "estimated_cost_usd": usage.estimated_cost_usd,
                "actual_provider_cost_usd": usage.actual_provider_cost_usd,
                "cost_confidence": usage.cost_confidence,
                "estimated_input_tokens": usage.estimated_input_tokens,
                "estimated_output_tokens": usage.estimated_output_tokens,
            },
        }

    def evaluate_forward_provider(
        *,
        provider: str,
        endpoint: str,
        payload: dict[str, Any],
        usage: UsageEstimate,
        run_id: str | None,
        api_key: str | None,
        upstream_base_url: str,
    ) -> tuple[int, dict[str, Any]]:
        if not enable_forwarding or dry_run or provider not in allowed_forward_providers:
            return evaluate_and_record(usage, run_id)
        if policy.max_total_usd is None:
            return 400, {"error": {"message": "Forwarding requires --max-total-usd local budget cap", "type": "agent_sentinel_missing_budget"}}
        if not api_key:
            return 400, {"error": {"message": f"Forwarding requires API key for {provider}", "type": "agent_sentinel_missing_api_key"}}
        if provider == "openrouter" and not api_key.startswith("sk-or-v1-"):
            return 400, {"error": {"message": "AGENT_CHRONICLE_OPENROUTER_API_KEY (or its pre-rename alias AGENT_SENTINEL_OPENROUTER_API_KEY) does not look like a full OpenRouter key", "type": "agent_sentinel_invalid_api_key_format"}}
        decision = _policy_decision(usage)
        if not decision.allowed:
            return _blocked_response(usage, run_id, decision.reason)

        try:
            upstream = forwarder(
                provider=provider,
                endpoint=endpoint,
                payload=payload,
                api_key=api_key,
                upstream_base_url=upstream_base_url,
            )
        except Exception:
            reason = "transport error during upstream request"
            event = ledger.record_usage(usage, run_id=run_id, decision="transport_error", reason=reason)
            return 502, {
                "error": {"message": reason, "type": "agent_sentinel_transport_error"},
                "agent_sentinel": {
                    "allowed": False,
                    "dry_run": False,
                    "forwarded": False,
                    "event_id": event["event_id"],
                    "provider": usage.provider,
                    "model": usage.model,
                    "usage_confidence": usage.usage_confidence,
                    "estimated_cost_usd": usage.estimated_cost_usd,
                    "actual_provider_cost_usd": usage.actual_provider_cost_usd,
                    "cost_confidence": usage.cost_confidence,
                    "estimated_input_tokens": usage.estimated_input_tokens,
                    "estimated_output_tokens": usage.estimated_output_tokens,
                },
            }
        upstream_body = upstream.get("json") or {}
        forwarded_usage = _usage_from_upstream(provider, endpoint, usage.model, usage, upstream_body)
        upstream_status = int(upstream.get("status_code") or 502)
        if upstream_status == 429:
            decision_name = "rate_limited"
            allowed = False
            forwarded = False
            reason = "provider returned HTTP 429"
        elif 200 <= upstream_status < 300:
            decision_name = "forwarded"
            allowed = True
            forwarded = True
            reason = "forwarded upstream after budget check"
        else:
            decision_name = "provider_error"
            allowed = False
            forwarded = False
            reason = f"provider returned HTTP {upstream_status}"
        forwarded_usage.forwarded = forwarded
        event = ledger.record_usage(forwarded_usage, run_id=run_id, decision=decision_name, reason=reason)
        if isinstance(upstream_body, dict):
            upstream_body = dict(upstream_body)
            upstream_body["agent_sentinel"] = {
                "allowed": allowed,
                "dry_run": False,
                "forwarded": forwarded,
                "event_id": event["event_id"],
                "provider": forwarded_usage.provider,
                "model": forwarded_usage.model,
                "usage_confidence": forwarded_usage.usage_confidence,
                "estimated_cost_usd": forwarded_usage.estimated_cost_usd,
                "actual_provider_cost_usd": forwarded_usage.actual_provider_cost_usd,
                "cost_confidence": forwarded_usage.cost_confidence,
                "estimated_input_tokens": forwarded_usage.estimated_input_tokens,
                "estimated_output_tokens": forwarded_usage.estimated_output_tokens,
            }
        return upstream_status, upstream_body

    @app.post("/openai/v1/chat/completions")
    async def openai_chat_completions(request: Request, x_agent_sentinel_run_id: str | None = Header(default=None)) -> JSONResponse:
        payload = await request.json()
        usage = estimate_openai_chat_usage(payload)
        status, body = evaluate_forward_provider(
            provider="openai",
            endpoint="/openai/v1/chat/completions",
            payload=payload,
            usage=usage,
            run_id=x_agent_sentinel_run_id,
            api_key=openai_api_key,
            upstream_base_url=openai_upstream_base_url,
        )
        return JSONResponse(status_code=status, content=body)

    @app.post("/openrouter/v1/chat/completions")
    async def openrouter_chat_completions(request: Request, x_agent_sentinel_run_id: str | None = Header(default=None)) -> JSONResponse:
        payload = await request.json()
        usage = estimate_openai_chat_usage(payload, provider="openrouter", endpoint="/openrouter/v1/chat/completions")
        status, body = evaluate_forward_provider(
            provider="openrouter",
            endpoint="/openrouter/v1/chat/completions",
            payload=payload,
            usage=usage,
            run_id=x_agent_sentinel_run_id,
            api_key=openrouter_api_key,
            upstream_base_url=openrouter_upstream_base_url,
        )
        return JSONResponse(status_code=status, content=body)

    @app.post("/deepseek/v1/chat/completions")
    async def deepseek_chat_completions(request: Request, x_agent_sentinel_run_id: str | None = Header(default=None)) -> JSONResponse:
        payload = await request.json()
        usage = estimate_openai_chat_usage(payload, provider="deepseek", endpoint="/deepseek/v1/chat/completions")
        status, body = evaluate_forward_provider(
            provider="deepseek",
            endpoint="/deepseek/v1/chat/completions",
            payload=payload,
            usage=usage,
            run_id=x_agent_sentinel_run_id,
            api_key=deepseek_api_key,
            upstream_base_url=deepseek_upstream_base_url,
        )
        return JSONResponse(status_code=status, content=body)

    @app.post("/anthropic/v1/messages")
    async def anthropic_messages(request: Request, x_agent_sentinel_run_id: str | None = Header(default=None)) -> JSONResponse:
        payload = await request.json()
        usage = estimate_anthropic_messages_usage(payload)
        status, body = evaluate_and_record(usage, x_agent_sentinel_run_id)
        return JSONResponse(status_code=status, content=body)

    @app.post("/gemini/v1beta/models/{model}:generateContent")
    async def gemini_generate_content(model: str, request: Request, x_agent_sentinel_run_id: str | None = Header(default=None)) -> JSONResponse:
        payload = await request.json()
        usage = estimate_gemini_generate_content_usage(model, payload)
        status, body = evaluate_and_record(usage, x_agent_sentinel_run_id)
        return JSONResponse(status_code=status, content=body)

    # Host/Origin guard: a browser page must not burn budget or pollute the
    # ledger with cross-site POSTs; SDK clients send no Origin header and pass
    # through untouched (header-only checks, body untouched). Installed LAST so
    # Starlette makes it the OUTERMOST middleware — rejected cross-site/rebound
    # requests never reach any middleware added above or a route handler
    # (contract in install_localhost_guard; pinned by the proxy ordering test).
    install_localhost_guard(app, extra_allowed_hosts=extra_allowed_hosts)
    return app
