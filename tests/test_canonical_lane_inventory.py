"""Canonical lane inventory guard (migration increment I2).

Pins the v1-only retention register against the REAL canonical classifiers so
the two cannot silently drift. The property that matters for the authoritative
flip: an event may be dropped from v1 only if its payload is fully modeled in
canonical; an event whose session identity is incidentally observed but whose
payload (evidence, disposition, note, provenance) has no lane must be retained.
"""

from __future__ import annotations

from typing import Any

from agentacct.canonical.lane_inventory import (
    PAYLOAD_COMPLETE_SESSION_EVENT_TYPES,
    V1_ONLY_RETAINED,
    canonical_lanes_for_event,
    is_canonical_touched_event,
    must_retain_in_v1,
    payload_fully_modeled,
    retention_reason,
)


def _event(event_type: str, metadata: dict[str, Any] | None = None, **top: Any) -> dict[str, Any]:
    event: dict[str, Any] = {"event_type": event_type, "metadata": metadata or {}}
    event.update(top)
    return event


def _usage_event(session_id: str = "u-1") -> dict[str, Any]:
    return _event(
        "model_usage",
        {
            "client": "codex",
            "client_session_id": session_id,
            "usage_update_semantics": "cumulative_snapshot",
            "total_tokens": 150,
            "updated_at": 1_700_000_000.0,
        },
        provider="openai",
        model="gpt-test",
        usage_confidence="client_reported_total",
        estimated_input_tokens=100,
        estimated_output_tokens=50,
        created_at=1_700_000_000.0,
    )


# --- fully-modeled lanes land where expected --------------------------------


def test_work_claim_events_are_modeled_in_the_work_claim_lane():
    section = _event(
        "section_completed",
        {
            "client": "claude-code",
            "client_session_id": "s1",
            "sentinel_semantic_kind": "section",
            "section_status": "completed",
        },
    )
    task = _event(
        "task_started",
        {
            "client": "claude-code",
            "client_session_id": "s1",
            "sentinel_semantic_kind": "task",
            "task_status": "started",
        },
    )
    for event in (section, task):
        assert "work_claim" in canonical_lanes_for_event(event)
        assert payload_fully_modeled(event) is True
        assert must_retain_in_v1(event) is False


def test_usage_events_are_modeled_in_the_usage_lane():
    event = _usage_event()
    assert "usage" in canonical_lanes_for_event(event)
    assert payload_fully_modeled(event) is True
    assert must_retain_in_v1(event) is False


def test_session_observation_payload_is_fully_modeled():
    event = _event("session_observed", {"client": "codex", "client_session_id": "s1"})
    assert canonical_lanes_for_event(event) == ("session",)
    # session_observed is on the payload-complete allow-list.
    assert payload_fully_modeled(event) is True
    assert must_retain_in_v1(event) is False
    assert "session_observed" in PAYLOAD_COMPLETE_SESSION_EVENT_TYPES


# --- v1-only payloads are retained even when the session lane touches them ---


def test_v1_only_payloads_are_retained_despite_incidental_session_lane():
    # Each carries a client_session_id, so the session lane observes its
    # identity — but the PAYLOAD (evidence/disposition/note/provenance) has no
    # work-claim or usage lane, so it must stay v1's record.
    for event_type in (
        "machine_check",
        "finding_disposition",
        "note",
        "client_context_attached",
        # A debug usage snapshot: its event_type contains the substring "usage"
        # yet it is NOT a usage measurement. The classifier must not put it in
        # the usage lane (regression guard for the _is_usage_event fix).
        "agent_usage_debug_reported",
    ):
        event = _event(
            event_type,
            {"client": "claude-code", "client_session_id": "s1", "sentinel_semantic_kind": "evidence"},
        )
        lanes = canonical_lanes_for_event(event)
        assert "work_claim" not in lanes, (event_type, lanes)
        assert "usage" not in lanes, (event_type, lanes)
        assert payload_fully_modeled(event) is False, event_type
        assert must_retain_in_v1(event) is True, event_type


def test_every_registered_v1_only_type_is_actually_retained():
    # The register cannot claim a type is v1-only if the classifiers would
    # model its payload. Build a representative event (no session id, so no
    # incidental lane) and prove it is retained with a non-empty reason.
    for event_type, reason in V1_ONLY_RETAINED.items():
        assert isinstance(reason, str) and reason.strip(), event_type
        event = _event(event_type, {"client": "claude-code"})
        assert must_retain_in_v1(event) is True, event_type
        assert payload_fully_modeled(event) is False, event_type
        assert retention_reason(event_type) == reason


def test_unknown_event_type_is_retained_by_default():
    # A brand new, unclassified event type is fail-safe retained, never dropped.
    event = _event("some_brand_new_event_type", {"client": "claude-code", "client_session_id": "s1"})
    assert retention_reason("some_brand_new_event_type") is None
    assert must_retain_in_v1(event) is True


def test_touched_is_not_the_same_as_fully_modeled():
    # A machine_check with a session is TOUCHED (session lane) but NOT payload
    # modeled — the exact distinction the retention gate depends on.
    event = _event("machine_check", {"client": "claude-code", "client_session_id": "s1"})
    assert is_canonical_touched_event(event) is True
    assert payload_fully_modeled(event) is False
