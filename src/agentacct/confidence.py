from __future__ import annotations

from typing import Final

USAGE_PROVIDER_REPORTED: Final = "provider_reported"
USAGE_CLIENT_REPORTED: Final = "client_reported"
USAGE_ESTIMATED: Final = "estimated"
USAGE_UNKNOWN: Final = "unknown"

COST_PROVIDER_BILLED: Final = "provider_billed"
COST_CLIENT_REPORTED: Final = "client_reported"
COST_SUBSCRIPTION_EQUIVALENT: Final = "subscription_equivalent"
COST_ESTIMATED_FROM_TOKENS: Final = "estimated_from_tokens"
COST_APPROXIMATE_SUBSCRIPTION_ALLOCATION: Final = "approximate_subscription_allocation"
COST_SUBSCRIPTION_UNAVAILABLE: Final = "subscription_unavailable"
COST_UNKNOWN: Final = "unknown"

COST_BASIS_PROVIDER_INVOICE: Final = "provider_invoice"
COST_BASIS_CLIENT_SESSION: Final = "local_client_session"
COST_BASIS_PRICING_TABLE: Final = "pricing_table"
COST_BASIS_USER_SUBSCRIPTION: Final = "user_subscription"
COST_BASIS_NONE: Final = "none"

EXACT_COST_CONFIDENCES: Final = frozenset({COST_PROVIDER_BILLED})
ESTIMATED_COST_CONFIDENCES: Final = frozenset({COST_ESTIMATED_FROM_TOKENS, COST_APPROXIMATE_SUBSCRIPTION_ALLOCATION, COST_SUBSCRIPTION_EQUIVALENT})


def normalize_usage_confidence(value: object) -> str:
    text = str(value or "").strip()
    if text == "exact":
        return USAGE_PROVIDER_REPORTED
    return text or USAGE_UNKNOWN


def normalize_cost_confidence(value: object) -> str:
    text = str(value or "").strip()
    if text == "exact":
        return COST_PROVIDER_BILLED
    if text == "estimated":
        return COST_ESTIMATED_FROM_TOKENS
    return text or COST_UNKNOWN


def normalize_cost_basis(value: object) -> str:
    text = str(value or "").strip()
    return text or COST_BASIS_NONE
