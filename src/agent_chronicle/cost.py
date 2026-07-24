from __future__ import annotations

import contextvars
import json
import math
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

from .env_compat import legacy_env_name, read_env_alias
from .confidence import (
    COST_APPROXIMATE_SUBSCRIPTION_ALLOCATION,
    COST_BASIS_PRICING_TABLE,
    COST_ESTIMATED_FROM_TOKENS,
    COST_PROVIDER_BILLED,
    COST_SUBSCRIPTION_UNAVAILABLE,
    COST_UNKNOWN,
    EXACT_COST_CONFIDENCES,
    ESTIMATED_COST_CONFIDENCES,
    USAGE_ESTIMATED,
    normalize_cost_confidence,
    normalize_usage_confidence,
)
from .pricing_catalog import (
    PRICING_CATALOG_PATH_ENV,
    PricingCatalog,
    PricingCatalogEntry,
    default_pricing_catalog_snapshot_path,
    ensure_fresh_pricing_snapshot,
    load_pricing_catalog_entries,
)


# Local published-price estimates for preflight validation and ledger estimates.
# Provider invoices remain the source of truth; these values are intentionally
# treated as estimated cost unless a provider returns actual cost directly.
MODEL_PRICES_PER_1M_INPUT: dict[tuple[str, str], float] = {
    ("openai", "gpt-5-mini"): 0.25,
    ("openai", "gpt-5.4-mini"): 0.75,
    ("openai", "gpt-5.5"): 5.00,
    ("codex", "gpt-5.5"): 5.00,
    ("codex", "gpt-5"): 1.25,
    ("claude-code", "claude-fable-5"): 10.00,
    ("claude-code", "claude-opus-4-8"): 5.00,
    ("claude-code", "claude-haiku-4-5-20251001"): 1.00,
    ("openai", "gpt-4o-mini"): 0.15,
    ("openrouter", "anthropic/claude-sonnet-4"): 3.00,
    ("openrouter", "anthropic/claude-sonnet-4.5"): 3.00,
    ("openrouter", "anthropic/claude-sonnet-4.6"): 3.00,
    ("openrouter", "anthropic/claude-3.5-haiku"): 0.80,
    ("openrouter", "anthropic/claude-3-haiku"): 0.25,
    ("openrouter", "openai/gpt-4o-mini"): 0.15,
    ("openrouter", "google/gemini-2.5-flash"): 0.30,
    ("openrouter", "google/gemini-2.5-flash-lite"): 0.10,
    ("openrouter", "deepseek/deepseek-chat"): 0.2002,
    ("deepseek", "deepseek-chat"): 0.14,
    ("anthropic", "claude-fable-5"): 10.00,
    ("anthropic", "claude-sonnet-4"): 3.00,
    ("anthropic", "claude-haiku-4-5-20251001"): 1.00,
    ("anthropic", "claude-3-5-haiku"): 0.80,
    ("gemini", "gemini-2.5-pro"): 1.25,
    ("gemini", "gemini-2.5-flash"): 0.30,
    ("default", "default"): 1.00,
}

MODEL_PRICES_PER_1M_OUTPUT: dict[tuple[str, str], float] = {
    ("openai", "gpt-5-mini"): 2.00,
    ("openai", "gpt-5.4-mini"): 4.50,
    ("openai", "gpt-5.5"): 30.00,
    ("codex", "gpt-5.5"): 30.00,
    ("codex", "gpt-5"): 10.00,
    ("claude-code", "claude-fable-5"): 50.00,
    ("claude-code", "claude-opus-4-8"): 25.00,
    ("claude-code", "claude-haiku-4-5-20251001"): 5.00,
    ("openai", "gpt-4o-mini"): 0.60,
    ("openrouter", "anthropic/claude-sonnet-4"): 15.00,
    ("openrouter", "anthropic/claude-sonnet-4.5"): 15.00,
    ("openrouter", "anthropic/claude-sonnet-4.6"): 15.00,
    ("openrouter", "anthropic/claude-3.5-haiku"): 4.00,
    ("openrouter", "anthropic/claude-3-haiku"): 1.25,
    ("openrouter", "openai/gpt-4o-mini"): 0.60,
    ("openrouter", "google/gemini-2.5-flash"): 2.50,
    ("openrouter", "google/gemini-2.5-flash-lite"): 0.40,
    ("openrouter", "deepseek/deepseek-chat"): 0.80,
    ("deepseek", "deepseek-chat"): 0.28,
    ("anthropic", "claude-fable-5"): 50.00,
    ("anthropic", "claude-sonnet-4"): 15.00,
    ("anthropic", "claude-haiku-4-5-20251001"): 5.00,
    ("anthropic", "claude-3-5-haiku"): 4.00,
    ("gemini", "gemini-2.5-pro"): 10.00,
    ("gemini", "gemini-2.5-flash"): 2.50,
    ("default", "default"): 4.00,
}

MODEL_PRICES_PER_1M_CACHE_WRITE_5M: dict[tuple[str, str], float] = {
    ("claude-code", "claude-fable-5"): 12.50,
    ("claude-code", "claude-opus-4-8"): 6.25,
    ("claude-code", "claude-haiku-4-5-20251001"): 1.25,
    ("anthropic", "claude-fable-5"): 12.50,
    ("anthropic", "claude-sonnet-4"): 3.75,
    ("anthropic", "claude-haiku-4-5-20251001"): 1.25,
    ("anthropic", "claude-3-5-haiku"): 1.00,
}

MODEL_PRICES_PER_1M_CACHE_WRITE_1H: dict[tuple[str, str], float] = {
    ("claude-code", "claude-fable-5"): 20.00,
    ("claude-code", "claude-opus-4-8"): 10.00,
    ("claude-code", "claude-haiku-4-5-20251001"): 2.00,
    ("anthropic", "claude-fable-5"): 20.00,
    ("anthropic", "claude-sonnet-4"): 6.00,
    ("anthropic", "claude-haiku-4-5-20251001"): 2.00,
    ("anthropic", "claude-3-5-haiku"): 1.60,
}

MODEL_PRICES_PER_1M_CACHE_READ: dict[tuple[str, str], float] = {
    ("openai", "gpt-5.4-mini"): 0.075,
    ("codex", "gpt-5.5"): 0.50,
    ("codex", "gpt-5"): 0.125,
    ("claude-code", "claude-fable-5"): 1.00,
    ("claude-code", "claude-opus-4-8"): 0.50,
    ("claude-code", "claude-haiku-4-5-20251001"): 0.10,
    ("anthropic", "claude-fable-5"): 1.00,
    ("anthropic", "claude-sonnet-4"): 0.30,
    ("anthropic", "claude-haiku-4-5-20251001"): 0.10,
    ("anthropic", "claude-3-5-haiku"): 0.08,
}

MODEL_COST_MULTIPLIERS: dict[tuple[str, str], float] = {
    # Codex local usage follows ccusage's "fast" pricing convention for gpt-5.5.
    # Keep this provider-scoped so ordinary OpenAI API estimates stay list-price.
    ("codex", "gpt-5.5"): 2.50,
}

# PRICING_CATALOG_PATH_ENV lives in pricing_catalog.py (the refresh path needs
# it without a circular import) and is re-exported above for the many callers
# that import it from cost. The pre-rename alias is accepted silently forever
# (reads go through read_env_alias; writers set only the new name).
LEGACY_PRICING_CATALOG_PATH_ENV = legacy_env_name(PRICING_CATALOG_PATH_ENV)
PRICING_PROVIDER_ALIASES = {
    # Claude Code local logs use a client provider name, while public list prices
    # are usually keyed as Anthropic model prices.
    "claude-code": "anthropic",
    # Codex local logs likewise report the client name while LiteLLM keys OpenAI
    # models under "openai". Exact (provider, model) entries always win before
    # this alias (lookup order in PricingCatalog.lookup), and the deliberate
    # builtin ("codex", "gpt-5.5") fast-pricing rows — with their 2.5x ccusage
    # multiplier — are ALSO protected on the merge path (PricingCatalog.merged
    # refuses to replace a builtin multiplier row with an external row keyed
    # exactly the same), so neither the alias nor an upstream/pinned catalog
    # row can drop the convention; external entries only extend coverage to
    # models the builtin table never listed (e.g. gpt-5.6*) at LiteLLM list
    # price.
    "codex": "openai",
}


def pricing_catalog_path_for_store(store_dir: Path | str | None) -> Path | None:
    return default_pricing_catalog_snapshot_path(store_dir)


def activate_pricing_catalog_for_store(
    store_dir: Path | str | None,
    *,
    catalog_path: Path | str | None = None,
    override_env: bool = False,
) -> Path | None:
    existing = read_env_alias(PRICING_CATALOG_PATH_ENV)
    if existing and not override_env:
        return Path(existing).expanduser()
    path = Path(catalog_path).expanduser() if catalog_path is not None else pricing_catalog_path_for_store(store_dir)
    if path is None or not path.exists():
        return None
    os.environ[PRICING_CATALOG_PATH_ENV] = str(path)
    reset_pricing_catalog_cache()
    return path


@dataclass
class UsageEstimate:
    provider: str
    model: str
    endpoint: str
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_cost_usd: float
    actual_provider_cost_usd: float | None = None
    cost_confidence: str = COST_ESTIMATED_FROM_TOKENS
    usage_confidence: str = USAGE_ESTIMATED
    cost_basis: str = COST_BASIS_PRICING_TABLE
    forwarded: bool = False


@dataclass
class PolicyDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class BudgetTier:
    tier: str
    default_action: str
    reason: str
    can_hard_block: bool


@dataclass(frozen=True)
class SubscriptionAllocationEstimate:
    method: str
    allocated_cost_usd: float | None
    confidence: str
    reason: str
    observed_units: int
    period_units: int | None


@dataclass(frozen=True)
class SubscriptionCostEstimate:
    subscription_price_usd: float
    period_days: int
    run_id: str | None
    event_count: int
    observed_run_count: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_total_tokens: int
    allocations: list[SubscriptionAllocationEstimate]
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "subscription_price_usd": self.subscription_price_usd,
            "period_days": self.period_days,
            "run_id": self.run_id,
            "event_count": self.event_count,
            "observed_run_count": self.observed_run_count,
            "estimated_input_tokens": self.estimated_input_tokens,
            "estimated_output_tokens": self.estimated_output_tokens,
            "estimated_total_tokens": self.estimated_total_tokens,
            "allocations": [asdict(allocation) for allocation in self.allocations],
            "note": self.note,
        }


@dataclass
class CostPolicy:
    max_total_usd: float | None = None
    max_total_tokens: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None

    def classify_cost_budget(self, usage: UsageEstimate) -> BudgetTier:
        confidence = normalize_cost_confidence(usage.cost_confidence)
        if confidence in EXACT_COST_CONFIDENCES:
            return BudgetTier(
                tier="exact_cost",
                default_action="block",
                reason="Provider-billed cost is known; hard cost budget enforcement is safe.",
                can_hard_block=True,
            )
        if confidence in ESTIMATED_COST_CONFIDENCES:
            return BudgetTier(
                tier="estimated_cost",
                default_action="warn_or_pause",
                reason="Cost is estimated from tokens or subscription allocation; label it clearly and require opt-in for hard stops.",
                can_hard_block=False,
            )
        return BudgetTier(
            tier="token_runtime_only",
            default_action="token_or_runtime_budget",
            reason="Cost is unknown; use token, runtime, or checkpoint budgets rather than dollar hard stops.",
            can_hard_block=False,
        )

    def check(
        self,
        usage: UsageEstimate,
        already_spent_usd: float,
        already_used_tokens: int = 0,
        already_used_input_tokens: int = 0,
        already_used_output_tokens: int = 0,
    ) -> PolicyDecision:
        if self.max_total_usd is not None and already_spent_usd + usage.estimated_cost_usd > self.max_total_usd:
            return PolicyDecision(
                allowed=False,
                reason=(
                    f"Budget would be exceeded: spent ${already_spent_usd:.6f} + "
                    f"request ${usage.estimated_cost_usd:.6f} > max ${self.max_total_usd:.6f}"
                ),
            )
        request_tokens = usage.estimated_input_tokens + usage.estimated_output_tokens
        if self.max_total_tokens is not None and already_used_tokens + request_tokens > self.max_total_tokens:
            return PolicyDecision(
                allowed=False,
                reason=(
                    f"Token budget would be exceeded: used {already_used_tokens} + request {request_tokens} "
                    f"> max {self.max_total_tokens}"
                ),
            )
        if self.max_input_tokens is not None and already_used_input_tokens + usage.estimated_input_tokens > self.max_input_tokens:
            return PolicyDecision(
                allowed=False,
                reason=(
                    f"Input token budget would be exceeded: used {already_used_input_tokens} + "
                    f"request {usage.estimated_input_tokens} > max {self.max_input_tokens}"
                ),
            )
        if self.max_output_tokens is not None and already_used_output_tokens + usage.estimated_output_tokens > self.max_output_tokens:
            return PolicyDecision(
                allowed=False,
                reason=(
                    f"Output token budget would be exceeded: used {already_used_output_tokens} + "
                    f"request {usage.estimated_output_tokens} > max {self.max_output_tokens}"
                ),
            )
        return PolicyDecision(allowed=True, reason="within budget")


def _safe_nonnegative_int(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, number)


def estimate_subscription_cost(
    events: list[dict[str, Any]],
    *,
    subscription_price_usd: float,
    period_days: int = 30,
    run_id: str | None = None,
    period_run_budget: int | None = None,
    period_token_budget: int | None = None,
) -> SubscriptionCostEstimate:
    """Approximate subscription amortization from local ledger observations.

    This never claims exact Claude Code/Codex subscription billing. It only
    allocates a user-entered subscription price across a user-entered run or
    token budget, then applies local agentacct observations to that denominator.
    """
    if not math.isfinite(subscription_price_usd) or subscription_price_usd <= 0:
        raise ValueError("subscription_price_usd must be a finite value > 0")
    if period_days <= 0:
        raise ValueError("period_days must be > 0")
    if period_run_budget is not None and period_run_budget <= 0:
        raise ValueError("period_run_budget must be > 0")
    if period_token_budget is not None and period_token_budget <= 0:
        raise ValueError("period_token_budget must be > 0")

    estimated_input_tokens = sum(_safe_nonnegative_int(event.get("estimated_input_tokens")) for event in events)
    estimated_output_tokens = sum(_safe_nonnegative_int(event.get("estimated_output_tokens")) for event in events)
    estimated_total_tokens = estimated_input_tokens + estimated_output_tokens
    run_ids = {str(event.get("run_id")) for event in events if event.get("run_id")}
    observed_run_count = 1 if run_id and events else len(run_ids)
    if not run_id and observed_run_count == 0 and events:
        observed_run_count = 1

    allocations: list[SubscriptionAllocationEstimate] = []
    if period_run_budget is not None:
        allocations.append(
            SubscriptionAllocationEstimate(
                method="per_observed_run",
                allocated_cost_usd=subscription_price_usd * observed_run_count / period_run_budget,
                confidence=COST_APPROXIMATE_SUBSCRIPTION_ALLOCATION,
                reason=(
                    "Allocated user-entered subscription price across user-entered period run budget. "
                    "This is not provider-reported billing."
                ),
                observed_units=observed_run_count,
                period_units=period_run_budget,
            )
        )
    if period_token_budget is not None:
        allocations.append(
            SubscriptionAllocationEstimate(
                method="per_estimated_token",
                allocated_cost_usd=subscription_price_usd * estimated_total_tokens / period_token_budget,
                confidence=COST_APPROXIMATE_SUBSCRIPTION_ALLOCATION,
                reason=(
                    "Allocated user-entered subscription price across user-entered period token budget using local token estimates. "
                    "This is not provider-reported billing."
                ),
                observed_units=estimated_total_tokens,
                period_units=period_token_budget,
            )
        )
    if not allocations:
        allocations.append(
            SubscriptionAllocationEstimate(
                method="unallocated_subscription",
                allocated_cost_usd=None,
                confidence=COST_SUBSCRIPTION_UNAVAILABLE,
                reason=(
                    "agentacct cannot infer exact subscription billing or a fair amortization denominator. "
                    "Pass --period-run-budget or --period-token-budget to compute an explicit approximation."
                ),
                observed_units=0,
                period_units=None,
            )
        )

    return SubscriptionCostEstimate(
        subscription_price_usd=subscription_price_usd,
        period_days=period_days,
        run_id=run_id,
        event_count=len(events),
        observed_run_count=observed_run_count,
        estimated_input_tokens=estimated_input_tokens,
        estimated_output_tokens=estimated_output_tokens,
        estimated_total_tokens=estimated_total_tokens,
        allocations=allocations,
        note=(
            "Approximate subscription-cost allocation only. Provider invoices or exposed usage data remain the source of truth; "
            "agentacct does not read exact Claude Code/Codex subscription billing."
        ),
    )


def _builtin_pricing_entries() -> list[PricingCatalogEntry]:
    entries: list[PricingCatalogEntry] = []
    for key, input_cost in MODEL_PRICES_PER_1M_INPUT.items():
        output_cost = MODEL_PRICES_PER_1M_OUTPUT.get(key)
        if output_cost is None:
            continue
        provider, model = key
        entries.append(
            PricingCatalogEntry(
                provider=provider,
                model=model,
                input_cost_per_1m=input_cost,
                output_cost_per_1m=output_cost,
                cache_write_5m_cost_per_1m=MODEL_PRICES_PER_1M_CACHE_WRITE_5M.get(key),
                cache_write_1h_cost_per_1m=MODEL_PRICES_PER_1M_CACHE_WRITE_1H.get(key),
                cache_read_cost_per_1m=MODEL_PRICES_PER_1M_CACHE_READ.get(key),
                cost_multiplier=MODEL_COST_MULTIPLIERS.get(key, 1.0),
                # frozen stored vocabulary (pre-rename pricing_source value).
                source="agent_sentinel_builtin",
                source_provider=provider,
                source_model=model,
            )
        )
    return entries


@lru_cache(maxsize=1)
def _builtin_pricing_catalog() -> PricingCatalog:
    return PricingCatalog(_builtin_pricing_entries(), provider_aliases=PRICING_PROVIDER_ALIASES)


def _external_pricing_catalog_signature() -> tuple[str, int, int] | None:
    raw_path = read_env_alias(PRICING_CATALOG_PATH_ENV)
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    try:
        stat = path.stat()
    except OSError:
        return (str(path), -1, -1)
    return (str(path), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=8)
def _pricing_catalog_for_signature(path_text: str, mtime_ns: int, size: int) -> PricingCatalog:
    del mtime_ns, size
    catalog = _builtin_pricing_catalog()
    if not path_text:
        return catalog
    try:
        entries = load_pricing_catalog_entries(Path(path_text), source=f"file:{Path(path_text).name}")
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return catalog
    return catalog.merged(entries)


# An explicit catalog can be scoped to the current context/thread so cost
# calculation does NOT depend on the process-global PRICING_CATALOG_PATH env
# var. This is what lets a background refresh price against a catalog it holds
# directly, immune to another request's middleware pinning/unpinning that env
# concurrently. Every cost lookup funnels through ``pricing_catalog()``, so
# overriding here covers has_model_price / estimate_* / the whole surface.
_PRICING_CATALOG_OVERRIDE: contextvars.ContextVar["PricingCatalog | None"] = (
    contextvars.ContextVar("pricing_catalog_override", default=None)
)


@contextmanager
def pricing_catalog_scope(catalog: "PricingCatalog") -> Iterator[None]:
    """Price against ``catalog`` for the duration of this context, ignoring the
    global PRICING_CATALOG_PATH env var. Resolve the catalog once where the env
    is correctly pinned (e.g. inside a request), then run the priced work under
    this scope on a background thread."""

    token = _PRICING_CATALOG_OVERRIDE.set(catalog)
    try:
        yield
    finally:
        _PRICING_CATALOG_OVERRIDE.reset(token)


def pricing_catalog() -> PricingCatalog:
    override = _PRICING_CATALOG_OVERRIDE.get()
    if override is not None:
        return override
    signature = _external_pricing_catalog_signature()
    if signature is None:
        return _pricing_catalog_for_signature("", 0, 0)
    return _pricing_catalog_for_signature(*signature)


def reset_pricing_catalog_cache() -> None:
    _builtin_pricing_catalog.cache_clear()
    _pricing_catalog_for_signature.cache_clear()


def model_pricing_entry(provider: str, model: str, *, allow_default: bool = False) -> PricingCatalogEntry | None:
    return pricing_catalog().lookup(provider, model, allow_default=allow_default)


def has_model_price(provider: str, model: str) -> bool:
    return model_pricing_entry(provider, model) is not None


def _price_for(provider: str, model: str) -> float:
    entry = model_pricing_entry(provider, model, allow_default=True)
    return entry.input_cost_per_1m if entry is not None else MODEL_PRICES_PER_1M_INPUT[("default", "default")]


def _output_price_for(provider: str, model: str) -> float:
    entry = model_pricing_entry(provider, model, allow_default=True)
    return entry.output_cost_per_1m if entry is not None else _price_for(provider, model) * 4


def _cache_write_5m_price_for(provider: str, model: str) -> float:
    entry = model_pricing_entry(provider, model, allow_default=True)
    if entry is not None and entry.cache_write_5m_cost_per_1m is not None:
        return entry.cache_write_5m_cost_per_1m
    return _price_for(provider, model)


def _cache_write_1h_price_for(provider: str, model: str) -> float:
    entry = model_pricing_entry(provider, model, allow_default=True)
    if entry is not None and entry.cache_write_1h_cost_per_1m is not None:
        return entry.cache_write_1h_cost_per_1m
    return _cache_write_5m_price_for(provider, model)


def _cache_read_price_for(provider: str, model: str) -> float:
    entry = model_pricing_entry(provider, model, allow_default=True)
    if entry is not None and entry.cache_read_cost_per_1m is not None:
        return entry.cache_read_cost_per_1m
    return _price_for(provider, model) * 0.1


def _cost_multiplier_for(provider: str, model: str) -> float:
    entry = model_pricing_entry(provider, model, allow_default=True)
    return entry.cost_multiplier if entry is not None else 1.0


def _rough_token_count(text: str) -> int:
    # Intentionally approximate: cheap local estimator for guardrail skeleton.
    # Uses chars/4 heuristic with a minimum of one token for non-empty strings.
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if "text" in part:
                    chunks.append(str(part["text"]))
                elif part.get("type") == "image_url":
                    chunks.append("[image_url]")
                elif "inlineData" in part or "fileData" in part:
                    chunks.append("[media]")
            else:
                chunks.append(str(part))
        return "\n".join(chunks)
    return str(content or "")


def _estimate_cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    return estimate_model_cost_usd(provider, model, input_tokens, output_tokens)


def estimate_model_cost_usd(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    input_price = _price_for(provider, model)
    output_price = _output_price_for(provider, model)
    multiplier = _cost_multiplier_for(provider, model)
    return (input_tokens * input_price + output_tokens * output_price) * multiplier / 1_000_000


def estimate_model_cost_breakdown_usd(
    provider: str,
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    cache_creation_5m_input_tokens: int = 0,
    cache_creation_1h_input_tokens: int = 0,
) -> dict[str, float]:
    """Estimate list-price cost while preserving cache pricing categories."""

    input_price = _price_for(provider, model)
    output_price = _output_price_for(provider, model)
    cache_write_5m_price = _cache_write_5m_price_for(provider, model)
    cache_write_1h_price = _cache_write_1h_price_for(provider, model)
    cache_read_price = _cache_read_price_for(provider, model)
    multiplier = _cost_multiplier_for(provider, model)
    creation_split = _safe_nonnegative_int(cache_creation_5m_input_tokens) + _safe_nonnegative_int(cache_creation_1h_input_tokens)
    unspecified_creation = max(0, _safe_nonnegative_int(cache_creation_input_tokens) - creation_split)
    input_cost = _safe_nonnegative_int(input_tokens) * input_price * multiplier / 1_000_000
    output_cost = _safe_nonnegative_int(output_tokens) * output_price * multiplier / 1_000_000
    cache_creation_5m_cost = (_safe_nonnegative_int(cache_creation_5m_input_tokens) + unspecified_creation) * cache_write_5m_price * multiplier / 1_000_000
    cache_creation_1h_cost = _safe_nonnegative_int(cache_creation_1h_input_tokens) * cache_write_1h_price * multiplier / 1_000_000
    cache_read_cost = _safe_nonnegative_int(cache_read_input_tokens) * cache_read_price * multiplier / 1_000_000
    cache_creation_cost = cache_creation_5m_cost + cache_creation_1h_cost
    total_cost = input_cost + output_cost + cache_creation_cost + cache_read_cost
    return {
        "input_cost_usd": input_cost,
        "output_cost_usd": output_cost,
        "cache_creation_cost_usd": cache_creation_cost,
        "cache_creation_5m_cost_usd": cache_creation_5m_cost,
        "cache_creation_1h_cost_usd": cache_creation_1h_cost,
        "cache_read_cost_usd": cache_read_cost,
        "total_cost_usd": total_cost,
    }


def estimate_openai_chat_usage(
    payload: dict[str, Any],
    *,
    provider: str = "openai",
    endpoint: str = "/openai/v1/chat/completions",
) -> UsageEstimate:
    model = str(payload.get("model") or "default")
    messages = payload.get("messages") or []
    text_parts: list[str] = [model]
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict):
                text_parts.append(str(message.get("role") or ""))
                text_parts.append(_message_content_to_text(message.get("content")))
            else:
                text_parts.append(str(message))
    else:
        text_parts.append(str(messages))
    input_tokens = _rough_token_count("\n".join(text_parts))
    # v0 skeleton cannot know completion length before forwarding; reserve a small
    # estimate so budget checks are conservative enough to be non-zero.
    output_tokens = int(payload.get("max_tokens") or payload.get("max_completion_tokens") or 128)
    return UsageEstimate(
        provider=provider,
        model=model,
        endpoint=endpoint,
        estimated_input_tokens=input_tokens,
        estimated_output_tokens=output_tokens,
        estimated_cost_usd=_estimate_cost(provider, model, input_tokens, output_tokens),
        forwarded=False,
    )


def estimate_anthropic_messages_usage(payload: dict[str, Any]) -> UsageEstimate:
    model = str(payload.get("model") or "default")
    text_parts: list[str] = [model]
    system = payload.get("system")
    if system:
        text_parts.append(_message_content_to_text(system))
    messages = payload.get("messages") or []
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict):
                text_parts.append(str(message.get("role") or ""))
                text_parts.append(_message_content_to_text(message.get("content")))
            else:
                text_parts.append(str(message))
    else:
        text_parts.append(str(messages))
    input_tokens = _rough_token_count("\n".join(text_parts))
    output_tokens = int(payload.get("max_tokens") or 128)
    return UsageEstimate(
        provider="anthropic",
        model=model,
        endpoint="/anthropic/v1/messages",
        estimated_input_tokens=input_tokens,
        estimated_output_tokens=output_tokens,
        estimated_cost_usd=_estimate_cost("anthropic", model, input_tokens, output_tokens),
        forwarded=False,
    )


def _gemini_part_to_text(part: Any) -> str:
    if isinstance(part, dict):
        if "text" in part:
            return str(part["text"])
        if "inlineData" in part or "fileData" in part:
            return "[media]"
    return str(part or "")


def estimate_gemini_generate_content_usage(model: str, payload: dict[str, Any]) -> UsageEstimate:
    text_parts: list[str] = [model]
    contents = payload.get("contents") or []
    if isinstance(contents, list):
        for content in contents:
            if isinstance(content, dict):
                text_parts.append(str(content.get("role") or ""))
                parts = content.get("parts") or []
                if isinstance(parts, list):
                    text_parts.extend(_gemini_part_to_text(part) for part in parts)
                else:
                    text_parts.append(str(parts))
            else:
                text_parts.append(str(content))
    else:
        text_parts.append(str(contents))
    generation_config = payload.get("generationConfig") or {}
    output_tokens = int(generation_config.get("maxOutputTokens") or payload.get("maxOutputTokens") or 128)
    input_tokens = _rough_token_count("\n".join(text_parts))
    return UsageEstimate(
        provider="gemini",
        model=model,
        endpoint=f"/gemini/v1beta/models/{model}:generateContent",
        estimated_input_tokens=input_tokens,
        estimated_output_tokens=output_tokens,
        estimated_cost_usd=_estimate_cost("gemini", model, input_tokens, output_tokens),
        forwarded=False,
    )


@dataclass(frozen=True)
class SubscriptionPlan:
    name: str
    provider: str
    monthly_price_usd: float
    period_days: int = 30
    period_run_budget: int | None = None
    period_token_budget: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SubscriptionStore:
    def __init__(self, root: Path | str, *, create: bool = True) -> None:
        if root is None:
            raise ValueError("store root is required; resolve it with agent_chronicle.store_resolution.resolve_store_dir()")
        self.root = Path(root).expanduser()
        # Read-only callers pass create=False so a typo'd --store-dir on a read
        # command never leaves a stray empty store (list_plans handles an
        # absent file). Write paths keep the default create=True.
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "subscriptions.json"

    def list_plans(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        plans = data.get("subscriptions") if isinstance(data, dict) else None
        if not isinstance(plans, list):
            return []
        return [plan for plan in plans if isinstance(plan, dict)]

    def set_plan(self, plan: SubscriptionPlan) -> dict[str, Any]:
        plans = [existing for existing in self.list_plans() if existing.get("name") != plan.name]
        plans.append(plan.to_dict())
        plans.sort(key=lambda item: str(item.get("name") or ""))
        self.path.write_text(json.dumps({"subscriptions": plans}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return plan.to_dict()

    def get_plan(self, name: str) -> dict[str, Any] | None:
        for plan in self.list_plans():
            if plan.get("name") == name:
                return plan
        return None


class CostLedger:
    def __init__(self, root: Path | str, *, create: bool = True) -> None:
        if root is None:
            raise ValueError("store root is required; resolve it with agent_chronicle.store_resolution.resolve_store_dir()")
        self.root = Path(root).expanduser()
        # Read-only callers (cost status / subscription-estimate) pass
        # create=False so a typo'd --store-dir never mkdirs a stray empty store;
        # read_events already treats an absent file as empty. Write paths keep
        # the default create=True.
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "cost_events.jsonl"

    def record_usage(self, usage: UsageEstimate, *, run_id: str | None, decision: str, reason: str = "") -> dict[str, Any]:
        usage_payload = asdict(usage)
        usage_payload["usage_confidence"] = normalize_usage_confidence(usage_payload.get("usage_confidence"))
        usage_payload["cost_confidence"] = normalize_cost_confidence(usage_payload.get("cost_confidence"))
        event = {
            "event_id": "cost_" + uuid.uuid4().hex[:12],
            "created_at": time.time(),
            "run_id": run_id,
            "decision": decision,
            "reason": reason,
            **usage_payload,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, sort_keys=True) + "\n")
        return event

    def read_events(self, run_id: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                event = json.loads(line)
                if run_id is None or event.get("run_id") == run_id:
                    events.append(event)
        return events

    def total_estimated_cost_usd(self, run_id: str | None = None) -> float:
        return sum(float(event.get("estimated_cost_usd") or 0.0) for event in self.read_events(run_id=run_id))

    def total_actual_provider_cost_usd(self, run_id: str | None = None) -> float:
        return sum(float(event.get("actual_provider_cost_usd") or 0.0) for event in self.read_events(run_id=run_id))

    def total_billable_cost_usd(self, run_id: str | None = None) -> float:
        """Best-known cost for enforcing future budget checks.

        Once a provider reports actual cost, use it for past forwarded events;
        otherwise fall back to the local estimate. This keeps the cutoff tied to
        real spend as soon as real spend is available, while preserving dry-run
        and estimated-provider behavior.
        """
        total = 0.0
        non_billable_decisions = {"blocked", "provider_error", "rate_limited", "transport_error"}
        for event in self.read_events(run_id=run_id):
            if event.get("decision") in non_billable_decisions:
                continue
            actual = event.get("actual_provider_cost_usd")
            if actual is not None:
                total += float(actual or 0.0)
            else:
                total += float(event.get("estimated_cost_usd") or 0.0)
        return total

    def total_estimated_tokens(self, run_id: str | None = None) -> int:
        return sum(
            int(event.get("estimated_input_tokens") or 0) + int(event.get("estimated_output_tokens") or 0)
            for event in self.read_events(run_id=run_id)
        )

    def total_estimated_input_tokens(self, run_id: str | None = None) -> int:
        return sum(int(event.get("estimated_input_tokens") or 0) for event in self.read_events(run_id=run_id))

    def total_estimated_output_tokens(self, run_id: str | None = None) -> int:
        return sum(int(event.get("estimated_output_tokens") or 0) for event in self.read_events(run_id=run_id))
