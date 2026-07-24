from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .confidence import USAGE_ESTIMATED, USAGE_PROVIDER_REPORTED
from .cost import CostLedger
from .env_compat import read_env_alias
from .provider_smoke import HttpPost, ProviderSmokeError, env_var_for_provider, run_provider_smoke

SUPPORTED_DEEP_PROVIDERS = ("openai", "anthropic", "gemini", "deepseek")
_LONG_PROMPT = (
    "You are validating a local cost safety layer for autonomous coding agents. "
    "Reply with one concise sentence that says the validation request was received. "
    "Do not include secrets, credentials, markdown tables, or code blocks."
)


@dataclass(frozen=True)
class DeepValidationCase:
    name: str
    prompt: str = "Reply with exactly: ok"
    max_tokens: int = 4
    model: str | None = None
    expect_success: bool = True
    max_usd: float | None = None


def build_deep_validation_cases(provider: str) -> list[DeepValidationCase]:
    provider = provider.lower().strip()
    if provider not in SUPPORTED_DEEP_PROVIDERS:
        raise ProviderSmokeError(f"Unsupported provider: {provider}")
    return [
        DeepValidationCase("basic_success", max_tokens=4),
        *[DeepValidationCase(f"sequential_{i}", max_tokens=4) for i in range(1, 6)],
        DeepValidationCase("max_tokens_1", max_tokens=1),
        DeepValidationCase("max_tokens_8", max_tokens=8),
        DeepValidationCase("max_tokens_32", max_tokens=32),
        DeepValidationCase("longer_prompt", prompt=_LONG_PROMPT, max_tokens=24),
        DeepValidationCase("provider_error_bad_model", model="agent-chronicle-nonexistent-model", expect_success=False, max_tokens=4),
        DeepValidationCase("budget_guard_blocks_before_upstream", max_tokens=32, max_usd=0.000000001, expect_success=False),
    ]


def run_deep_provider_validation(
    providers: list[str],
    *,
    max_provider_usd: float,
    store_dir: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
    delay_seconds: float = 0.0,
) -> dict[str, Any]:
    if store_dir is None:
        # No silent home-store default anywhere (Phase 1 decision 4a): fail
        # upfront with an actionable message instead of a ValueError deep
        # inside CostLedger after budget/env validation.
        raise ProviderSmokeError(
            "store_dir is required: pass an explicit store directory "
            "(e.g. resolve one with agent_chronicle.store_resolution.resolve_store_dir()); "
            "there is no ~/.agent-sentinel fallback anymore"
        )
    if max_provider_usd <= 0:
        raise ProviderSmokeError("max_provider_usd must be positive")
    if max_provider_usd > 1.0:
        raise ProviderSmokeError("max_provider_usd must be <= 1.0")
    if delay_seconds < 0:
        raise ProviderSmokeError("delay_seconds must be non-negative")
    if not providers:
        raise ProviderSmokeError("at least one provider is required")

    env = os.environ if env is None else env
    normalized = [p.lower().strip() for p in providers]
    seen: set[str] = set()
    for provider in normalized:
        if provider in seen:
            raise ProviderSmokeError(f"Duplicate provider: {provider}")
        seen.add(provider)
        env_var = env_var_for_provider(provider)
        if not (read_env_alias(env_var, env) or "").strip():
            raise ProviderSmokeError(f"Missing {env_var}")

    root = Path(store_dir).expanduser() if store_dir is not None else None
    result: dict[str, Any] = {"providers": {}, "overall_passed": True}
    for provider in normalized:
        provider_result = _run_one_provider(
            provider,
            max_provider_usd=max_provider_usd,
            store_dir=root,
            env=env,
            http_post=http_post,
            delay_seconds=delay_seconds,
        )
        result["providers"][provider] = provider_result
        if not provider_result["passed"]:
            result["overall_passed"] = False
    return result


def _run_one_provider(
    provider: str,
    *,
    max_provider_usd: float,
    store_dir: Path | None,
    env: Mapping[str, str],
    http_post: HttpPost | None,
    delay_seconds: float,
) -> dict[str, Any]:
    ledger = CostLedger(store_dir)
    start_billable = ledger.total_billable_cost_usd(run_id="real_provider_smoke")
    cases: list[dict[str, Any]] = []
    passed = True
    validation_cases = build_deep_validation_cases(provider)
    stop_for_rate_limit = False
    for case in validation_cases:
        if stop_for_rate_limit:
            cases.append({"name": case.name, "passed": False, "decision": "skipped_after_rate_limit"})
            passed = False
            continue
        before_billable = ledger.total_billable_cost_usd(run_id="real_provider_smoke")
        spent_this_provider = max(0.0, before_billable - start_billable)
        remaining = max_provider_usd - spent_this_provider
        if remaining <= 0:
            cases.append({"name": case.name, "passed": False, "decision": "skipped_budget_exhausted"})
            passed = False
            continue
        # Keep the user-approved per-provider cap separate from the per-request
        # smoke safety cap. Deep validation may allow up to $1/provider overall,
        # but no single provider request should get more than the smoke harness
        # hard limit.
        case_budget = min(case.max_usd if case.max_usd is not None else remaining, remaining, 0.05)
        try:
            summary = run_provider_smoke(
                provider,
                max_usd=case_budget,
                store_dir=store_dir,
                model=case.model,
                prompt=case.prompt,
                max_tokens=case.max_tokens,
                env=env,
                http_post=http_post,
            )
        except ProviderSmokeError as exc:
            summary = {"decision": "harness_error", "reason": str(exc), "status_code": None}
        case_passed = _case_passed(case, summary)
        if not case_passed:
            passed = False
        cases.append(_safe_case_summary(case, summary, case_passed))
        if summary.get("decision") == "rate_limited":
            stop_for_rate_limit = True
        if delay_seconds > 0:
            time.sleep(delay_seconds)
    end_billable = ledger.total_billable_cost_usd(run_id="real_provider_smoke")
    return {
        "passed": passed,
        "case_count": len(cases),
        "cases": cases,
        "billable_delta_usd": max(0.0, end_billable - start_billable),
    }


def _case_passed(case: DeepValidationCase, summary: dict[str, Any]) -> bool:
    decision = summary.get("decision")
    if case.name == "budget_guard_blocks_before_upstream":
        return decision == "blocked" and summary.get("status_code") is None
    if case.expect_success:
        return decision == "forwarded" and int(summary.get("status_code") or 0) in range(200, 300) and summary.get("usage_confidence") == USAGE_PROVIDER_REPORTED
    return decision == "provider_error" and int(summary.get("status_code") or 0) >= 400 and summary.get("usage_confidence") == USAGE_ESTIMATED


def _safe_case_summary(case: DeepValidationCase, summary: dict[str, Any], passed: bool) -> dict[str, Any]:
    return {
        "name": case.name,
        "passed": passed,
        "decision": summary.get("decision"),
        "status_code": summary.get("status_code"),
        "model": summary.get("model"),
        "usage_confidence": summary.get("usage_confidence"),
        "cost_confidence": summary.get("cost_confidence"),
        "estimated_input_tokens": summary.get("estimated_input_tokens"),
        "estimated_output_tokens": summary.get("estimated_output_tokens"),
        "estimated_cost_usd": summary.get("estimated_cost_usd"),
        "actual_provider_cost_usd": summary.get("actual_provider_cost_usd"),
        "reason": summary.get("reason"),
    }
