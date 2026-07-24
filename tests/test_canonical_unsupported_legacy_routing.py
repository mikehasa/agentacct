from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_chronicle.canonical.legacy_recovery import RECOVERY_DESIGN_ID
from agent_chronicle.canonical.unsupported_legacy_routing import (
    STATIC_UNSUPPORTED_LEGACY_EVENT_TYPE_COUNTS,
    STATIC_UNSUPPORTED_LEGACY_ROW_COUNT,
    UNSUPPORTED_LEGACY_EVENT_TYPE_TO_FUTURE_PRODUCT_LANE,
    UNSUPPORTED_LEGACY_ROUTING_POLICY_ID,
    UNSUPPORTED_LEGACY_ROUTING_POLICY_VERSION,
    UNSUPPORTED_LEGACY_ROUTING_RULES_DIGEST,
    UnsupportedLegacyRoute,
    UnsupportedLegacyRoutingError,
    route_unsupported_legacy_event_type,
    summarize_unsupported_legacy_event_types,
    verify_static_unsupported_legacy_classification,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_CLASSIFICATION = (
    REPO_ROOT
    / "docs"
    / "evidence"
    / "canonical-sqlite-unscoped-legacy-classification.json"
)

EXPECTED_LANES = {
    "machine_check": "verification_health",
    "agent_usage_debug_reported": "audit_debug",
    "client_context_attached": "audit_debug",
    "artifact_created": "artifact",
    "instrumentation_installed": "operational_metadata",
    "run_started": "operational_metadata",
    "sentinel_install_check": "operational_metadata",
}


def test_each_approved_type_routes_only_to_a_future_noncanonical_lane() -> None:
    assert (
        dict(UNSUPPORTED_LEGACY_EVENT_TYPE_TO_FUTURE_PRODUCT_LANE)
        == EXPECTED_LANES
    )

    for event_type, expected_lane in EXPECTED_LANES.items():
        route = route_unsupported_legacy_event_type(event_type)

        assert route.event_type == event_type
        assert route.future_product_lane == expected_lane
        assert route.policy_id == UNSUPPORTED_LEGACY_ROUTING_POLICY_ID
        assert route.policy_version == UNSUPPORTED_LEGACY_ROUTING_POLICY_VERSION
        assert route.rules_digest == UNSUPPORTED_LEGACY_ROUTING_RULES_DIGEST
        assert route.migration_disposition == "unsupported_retained"
        assert route.current_product_state == "retained_quarantined"
        assert route.typed_lane_implemented is False
        assert route.canonical_admission_authorized is False
        assert route.work_contribution is False
        assert route.task_contribution is False
        assert route.usage_contribution is False
        assert route.durable_archive_required is True


@pytest.mark.parametrize(
    "event_type",
    (
        None,
        "",
        "machine_check ",
        "machine_check_observed",
        "unknown_support_event",
        123,
    ),
)
def test_unknown_or_nonexact_event_types_fail_closed(event_type: object) -> None:
    with pytest.raises(UnsupportedLegacyRoutingError):
        route_unsupported_legacy_event_type(event_type)


def test_route_invariants_cannot_be_overridden_by_direct_construction() -> None:
    with pytest.raises(UnsupportedLegacyRoutingError, match="exactly match"):
        UnsupportedLegacyRoute(
            event_type="machine_check",
            future_product_lane="artifact",
        )

    with pytest.raises(TypeError):
        UnsupportedLegacyRoute(
            event_type="machine_check",
            future_product_lane="verification_health",
            canonical_admission_authorized=True,
        )


def test_static_counts_route_to_the_approved_lane_totals() -> None:
    summary = verify_static_unsupported_legacy_classification(
        STATIC_UNSUPPORTED_LEGACY_EVENT_TYPE_COUNTS
    )

    assert summary.total_rows == STATIC_UNSUPPORTED_LEGACY_ROW_COUNT == 1033
    assert dict(summary.future_product_lane_counts) == {
        "artifact": 2,
        "audit_debug": 42,
        "operational_metadata": 4,
        "verification_health": 985,
    }


@pytest.mark.parametrize("invalid_count", (-1, True, 1.0, "1", None))
def test_invalid_counts_fail_closed(invalid_count: object) -> None:
    with pytest.raises(UnsupportedLegacyRoutingError):
        summarize_unsupported_legacy_event_types({"machine_check": invalid_count})


def test_static_contract_rejects_unknown_or_changed_counts() -> None:
    with pytest.raises(UnsupportedLegacyRoutingError, match="not approved"):
        verify_static_unsupported_legacy_classification(
            {**STATIC_UNSUPPORTED_LEGACY_EVENT_TYPE_COUNTS, "new_type": 1}
        )

    changed = dict(STATIC_UNSUPPORTED_LEGACY_EVENT_TYPE_COUNTS)
    changed["machine_check"] -= 1
    with pytest.raises(UnsupportedLegacyRoutingError, match="differ"):
        verify_static_unsupported_legacy_classification(changed)


def test_sanitized_classification_contract_matches_executable_policy() -> None:
    payload = json.loads(STATIC_CLASSIFICATION.read_text(encoding="utf-8"))
    support_counts = payload["unscoped_surface"]["support_event_types"]
    routing = payload["unsupported_product_routing"]
    classification = payload["classification"]
    summary = verify_static_unsupported_legacy_classification(support_counts)

    assert payload["schema_version"] == (
        "agent-chronicle.unscoped-legacy-classification.v4"
    )
    assert payload["design_id"] == RECOVERY_DESIGN_ID
    assert classification["candidate_recovery_eligible_rows"] == 1163
    assert classification["visible_quarantine_rows"] == 1161
    assert classification["partition_sum"] == 2324
    assert routing["policy_id"] == UNSUPPORTED_LEGACY_ROUTING_POLICY_ID
    assert routing["policy_version"] == UNSUPPORTED_LEGACY_ROUTING_POLICY_VERSION
    assert routing["rules_digest"] == UNSUPPORTED_LEGACY_ROUTING_RULES_DIGEST
    assert routing["source_migration_disposition"] == "unsupported_retained"
    assert routing["current_product_state"] == "retained_quarantined"
    assert routing["typed_lanes_implemented"] is False
    assert routing["canonical_admission_authorized"] is False
    assert routing["legacy_receipts_reclassified"] is False
    assert routing["canonical_contribution"] == {
        "work": 0,
        "task": 0,
        "usage": 0,
    }
    assert routing["durable_archive_required"] is True
    assert routing["unknown_event_type_behavior"] == (
        "refuse_route_and_retain_quarantine"
    )
    assert routing["total_rows"] == summary.total_rows
    assert routing["future_product_lane_totals"] == dict(
        summary.future_product_lane_counts
    )
    assert routing["event_types"] == {
        event_type: {
            "rows": count,
            "future_product_lane": EXPECTED_LANES[event_type],
        }
        for event_type, count in support_counts.items()
    }
