"""Versioned product routing for retained unsupported legacy rows.

This policy is deliberately downstream of ``unscoped_legacy_recovery_v3``.
It gives already-quarantined support events an intended future product lane;
it does not import them, reinterpret them as Work, or implement those lanes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal


UNSUPPORTED_LEGACY_ROUTING_POLICY_ID = "unsupported_legacy_product_routing_v1"
UNSUPPORTED_LEGACY_ROUTING_POLICY_VERSION = "v1"
UNSUPPORTED_LEGACY_MIGRATION_DISPOSITION = "unsupported_retained"
UNSUPPORTED_LEGACY_CURRENT_PRODUCT_STATE = "retained_quarantined"

UnsupportedLegacyProductLane = Literal[
    "verification_health",
    "audit_debug",
    "artifact",
    "operational_metadata",
]

_EVENT_TYPE_TO_FUTURE_PRODUCT_LANE: dict[str, UnsupportedLegacyProductLane] = {
    "machine_check": "verification_health",
    "agent_usage_debug_reported": "audit_debug",
    "client_context_attached": "audit_debug",
    "artifact_created": "artifact",
    "instrumentation_installed": "operational_metadata",
    "run_started": "operational_metadata",
    "sentinel_install_check": "operational_metadata",
}
UNSUPPORTED_LEGACY_EVENT_TYPE_TO_FUTURE_PRODUCT_LANE = MappingProxyType(
    _EVENT_TYPE_TO_FUTURE_PRODUCT_LANE
)

_STATIC_EVENT_TYPE_COUNTS = {
    "machine_check": 985,
    "agent_usage_debug_reported": 34,
    "client_context_attached": 8,
    "artifact_created": 2,
    "instrumentation_installed": 2,
    "run_started": 1,
    "sentinel_install_check": 1,
}
STATIC_UNSUPPORTED_LEGACY_EVENT_TYPE_COUNTS = MappingProxyType(
    _STATIC_EVENT_TYPE_COUNTS
)
STATIC_UNSUPPORTED_LEGACY_ROW_COUNT = sum(_STATIC_EVENT_TYPE_COUNTS.values())

_RULES_DOCUMENT = {
    "policy_id": UNSUPPORTED_LEGACY_ROUTING_POLICY_ID,
    "policy_version": UNSUPPORTED_LEGACY_ROUTING_POLICY_VERSION,
    "event_type_to_future_product_lane": dict(
        sorted(_EVENT_TYPE_TO_FUTURE_PRODUCT_LANE.items())
    ),
    "migration_disposition": UNSUPPORTED_LEGACY_MIGRATION_DISPOSITION,
    "current_product_state": UNSUPPORTED_LEGACY_CURRENT_PRODUCT_STATE,
    "typed_lanes_implemented": False,
    "canonical_admission_authorized": False,
    "work_task_usage_contribution": False,
    "durable_archive_required": True,
    "unknown_event_type": "refuse_route_and_retain_quarantine",
}
UNSUPPORTED_LEGACY_ROUTING_RULES_DIGEST = hashlib.sha256(
    json.dumps(
        _RULES_DOCUMENT,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()


class UnsupportedLegacyRoutingError(ValueError):
    """The retained row cannot be assigned under the approved routing policy."""


@dataclass(frozen=True, slots=True)
class UnsupportedLegacyRoute:
    """One safe interpretation of an already-quarantined support event."""

    event_type: str
    future_product_lane: UnsupportedLegacyProductLane
    policy_id: str = field(init=False, default=UNSUPPORTED_LEGACY_ROUTING_POLICY_ID)
    policy_version: str = field(
        init=False,
        default=UNSUPPORTED_LEGACY_ROUTING_POLICY_VERSION,
    )
    rules_digest: str = field(
        init=False,
        default=UNSUPPORTED_LEGACY_ROUTING_RULES_DIGEST,
    )
    migration_disposition: str = field(
        init=False,
        default=UNSUPPORTED_LEGACY_MIGRATION_DISPOSITION,
    )
    current_product_state: str = field(
        init=False,
        default=UNSUPPORTED_LEGACY_CURRENT_PRODUCT_STATE,
    )
    typed_lane_implemented: bool = field(init=False, default=False)
    canonical_admission_authorized: bool = field(init=False, default=False)
    work_contribution: bool = field(init=False, default=False)
    task_contribution: bool = field(init=False, default=False)
    usage_contribution: bool = field(init=False, default=False)
    durable_archive_required: bool = field(init=False, default=True)

    def __post_init__(self) -> None:
        if not isinstance(self.event_type, str) or not self.event_type:
            raise UnsupportedLegacyRoutingError(
                "unsupported legacy route requires a non-empty event_type"
            )
        approved_lane = UNSUPPORTED_LEGACY_EVENT_TYPE_TO_FUTURE_PRODUCT_LANE.get(
            self.event_type
        )
        if approved_lane is None or approved_lane != self.future_product_lane:
            raise UnsupportedLegacyRoutingError(
                "unsupported legacy route must exactly match the approved v1 mapping"
            )


@dataclass(frozen=True, slots=True)
class UnsupportedLegacyRoutingSummary:
    """Counts-only routing summary safe for a sanitized evidence contract."""

    total_rows: int
    event_type_counts: Mapping[str, int]
    future_product_lane_counts: Mapping[UnsupportedLegacyProductLane, int]

    def __post_init__(self) -> None:
        if type(self.total_rows) is not int or self.total_rows < 0:
            raise UnsupportedLegacyRoutingError(
                "unsupported legacy routing total must be a non-negative integer"
            )
        normalized_event_counts: dict[str, int] = {}
        expected_lane_counts: dict[UnsupportedLegacyProductLane, int] = {
            "verification_health": 0,
            "audit_debug": 0,
            "artifact": 0,
            "operational_metadata": 0,
        }
        for event_type, count in self.event_type_counts.items():
            route = route_unsupported_legacy_event_type(event_type)
            if type(count) is not int or count < 0:
                raise UnsupportedLegacyRoutingError(
                    "unsupported legacy event counts must be non-negative integers"
                )
            normalized_event_counts[event_type] = count
            expected_lane_counts[route.future_product_lane] += count
        supplied_lane_counts = dict(self.future_product_lane_counts)
        if set(supplied_lane_counts) != set(expected_lane_counts) or any(
            type(count) is not int or count < 0
            for count in supplied_lane_counts.values()
        ):
            raise UnsupportedLegacyRoutingError(
                "unsupported legacy lane counts must cover the approved lanes "
                "with non-negative integers"
            )
        if supplied_lane_counts != expected_lane_counts:
            raise UnsupportedLegacyRoutingError(
                "unsupported legacy lane counts differ from the approved event routing"
            )
        if self.total_rows != sum(normalized_event_counts.values()):
            raise UnsupportedLegacyRoutingError(
                "unsupported legacy routing total differs from the event counts"
            )
        object.__setattr__(
            self,
            "event_type_counts",
            MappingProxyType(dict(sorted(normalized_event_counts.items()))),
        )
        object.__setattr__(
            self,
            "future_product_lane_counts",
            MappingProxyType(dict(sorted(self.future_product_lane_counts.items()))),
        )


def route_unsupported_legacy_event_type(event_type: object) -> UnsupportedLegacyRoute:
    """Return the v1 future lane or refuse an unapproved event type.

    Refusal does not change the row's existing archive/quarantine disposition.
    Callers must never invent a default lane for a refused type.
    """

    if not isinstance(event_type, str) or not event_type:
        raise UnsupportedLegacyRoutingError(
            "unsupported legacy event_type must be a non-empty string"
        )
    lane = UNSUPPORTED_LEGACY_EVENT_TYPE_TO_FUTURE_PRODUCT_LANE.get(event_type)
    if lane is None:
        raise UnsupportedLegacyRoutingError(
            f"unsupported legacy event_type is not approved by "
            f"{UNSUPPORTED_LEGACY_ROUTING_POLICY_ID}: {event_type!r}"
        )
    return UnsupportedLegacyRoute(event_type=event_type, future_product_lane=lane)


def summarize_unsupported_legacy_event_types(
    event_type_counts: Mapping[str, object],
) -> UnsupportedLegacyRoutingSummary:
    """Aggregate approved retained types without admitting them canonically."""

    if not isinstance(event_type_counts, Mapping):
        raise UnsupportedLegacyRoutingError("event_type_counts must be a mapping")

    normalized: dict[str, int] = {}
    lane_counts: dict[UnsupportedLegacyProductLane, int] = {
        "verification_health": 0,
        "audit_debug": 0,
        "artifact": 0,
        "operational_metadata": 0,
    }
    for event_type, value in event_type_counts.items():
        route = route_unsupported_legacy_event_type(event_type)
        if type(value) is not int or value < 0:
            raise UnsupportedLegacyRoutingError(
                f"unsupported legacy count for {event_type!r} must be a "
                "non-negative integer"
            )
        normalized[event_type] = value
        lane_counts[route.future_product_lane] += value

    return UnsupportedLegacyRoutingSummary(
        total_rows=sum(normalized.values()),
        event_type_counts=normalized,
        future_product_lane_counts=lane_counts,
    )


def verify_static_unsupported_legacy_classification(
    event_type_counts: Mapping[str, object],
) -> UnsupportedLegacyRoutingSummary:
    """Fail closed unless counts exactly match the sanitized 1,033-row contract."""

    summary = summarize_unsupported_legacy_event_types(event_type_counts)
    if dict(summary.event_type_counts) != dict(
        STATIC_UNSUPPORTED_LEGACY_EVENT_TYPE_COUNTS
    ):
        raise UnsupportedLegacyRoutingError(
            "unsupported legacy event counts differ from the static sanitized "
            "classification contract"
        )
    if summary.total_rows != STATIC_UNSUPPORTED_LEGACY_ROW_COUNT:
        raise UnsupportedLegacyRoutingError(
            "unsupported legacy row total differs from the static sanitized "
            "classification contract"
        )
    return summary


__all__ = [
    "STATIC_UNSUPPORTED_LEGACY_EVENT_TYPE_COUNTS",
    "STATIC_UNSUPPORTED_LEGACY_ROW_COUNT",
    "UNSUPPORTED_LEGACY_CURRENT_PRODUCT_STATE",
    "UNSUPPORTED_LEGACY_EVENT_TYPE_TO_FUTURE_PRODUCT_LANE",
    "UNSUPPORTED_LEGACY_MIGRATION_DISPOSITION",
    "UNSUPPORTED_LEGACY_ROUTING_POLICY_ID",
    "UNSUPPORTED_LEGACY_ROUTING_POLICY_VERSION",
    "UNSUPPORTED_LEGACY_ROUTING_RULES_DIGEST",
    "UnsupportedLegacyProductLane",
    "UnsupportedLegacyRoute",
    "UnsupportedLegacyRoutingError",
    "UnsupportedLegacyRoutingSummary",
    "route_unsupported_legacy_event_type",
    "summarize_unsupported_legacy_event_types",
    "verify_static_unsupported_legacy_classification",
]
