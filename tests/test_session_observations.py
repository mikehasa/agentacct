from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from agentacct.session_observations import (
    build_session_observations,
    select_session_projection_envelopes,
)


def _session_envelope(
    *,
    session_id: str = "session-1",
    idempotency_key: str = "capture:codex:session-1",
    evidence_id: str = "ev-session-1",
    observed_at: str = "2026-07-16T08:00:00Z",
    event_timestamp: str = "2026-07-16T08:00:00Z",
    source_event_id: str = "source-session-1",
    project_id: str | None = None,
    organization: str | None = None,
    capture_time_fallback: bool = False,
) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if organization is not None:
        extra["organization"] = organization
    attributes: dict[str, Any] = {"model": "gpt-5"}
    note_codes: list[str] = []
    if capture_time_fallback:
        attributes["time_basis"] = "capture_observed"
        note_codes.append("host_timestamp")
    return {
        "schema_version": "agent-chronicle.evidence-envelope.v2",
        "evidence_id": evidence_id,
        "idempotency_key": idempotency_key,
        "integrity_hash": f"sha256:{'a' * 64}",
        "assertion": "observed",
        "source_type": "client_hook",
        "source_system": "codex",
        "source_instance": "codex-local",
        "source_event_id": source_event_id,
        "event_type": "session_observed",
        "event_timestamp": event_timestamp,
        "observed_at": observed_at,
        "dimensions": ["activity", "session_identity"],
        "tags": ["mechanical_capture"],
        "subjects": {
            "client_session_id": session_id,
            "project_id": project_id,
            "extra": extra,
        },
        "payload": {
            "capture_basis": "host_hook",
            "host_event": "SessionStart",
            "attributes": attributes,
        },
        "completeness": {"note_codes": note_codes},
    }


def test_hook_session_ids_are_opaque_and_model_suffix_is_not_rewritten() -> None:
    opaque_id = "conversation:model:gpt-5"

    observations = build_session_observations([_session_envelope(session_id=opaque_id)])

    assert len(observations) == 1
    assert observations[0]["client_session_id"] == opaque_id
    assert observations[0]["observed_models"] == ["gpt-5"]


def test_cross_namespace_collision_is_dropped_instead_of_merged() -> None:
    first = _session_envelope(project_id="project-a", organization="org-a")
    second = _session_envelope(
        project_id="project-b",
        organization="org-a",
        evidence_id="ev-session-2",
        source_event_id="source-session-2",
    )

    diagnostics: dict[str, int] = {}
    assert build_session_observations([first, second], diagnostics=diagnostics) == []
    assert diagnostics["namespace_collision_sessions_dropped"] == 1
    assert diagnostics["projected_sessions"] == 0


def test_top_level_organization_namespace_collision_is_dropped() -> None:
    first = _session_envelope(project_id="project-a")
    first["subjects"]["organization"] = "org-a"
    second = _session_envelope(
        project_id="project-a",
        evidence_id="ev-session-2",
        source_event_id="source-session-2",
    )
    second["subjects"]["organization"] = "org-b"

    assert build_session_observations([first, second]) == []


def test_material_conflict_group_is_dropped_from_product_projection() -> None:
    first = _session_envelope(capture_time_fallback=True)
    second = deepcopy(first)
    second["evidence_id"] = "ev-conflict"
    second["integrity_hash"] = f"sha256:{'b' * 64}"
    second["payload"]["host_event"] = "SessionEnd"
    records = [
        SimpleNamespace(envelope=first, is_conflict=True),
        SimpleNamespace(envelope=second, is_conflict=True),
    ]

    diagnostics: dict[str, int] = {}
    assert select_session_projection_envelopes(
        records,
        complete_conflict_keys={first["idempotency_key"]},
        diagnostics=diagnostics,
    ) == []
    assert diagnostics["material_conflict_groups_dropped"] == 1


def test_timestamp_only_retry_conflict_collapses_to_earliest_capture() -> None:
    later = _session_envelope(
        capture_time_fallback=True,
        event_timestamp="2026-07-16T08:00:02Z",
        observed_at="2026-07-16T08:00:02Z",
    )
    earlier = deepcopy(later)
    earlier["evidence_id"] = "ev-earlier"
    earlier["integrity_hash"] = f"sha256:{'b' * 64}"
    earlier["event_timestamp"] = "2026-07-16T08:00:01Z"
    earlier["observed_at"] = "2026-07-16T08:00:01Z"
    records = [
        SimpleNamespace(envelope=later, is_conflict=True),
        SimpleNamespace(envelope=earlier, is_conflict=False),
    ]

    selected = select_session_projection_envelopes(
        records,
        complete_conflict_keys={later["idempotency_key"]},
    )

    assert selected == [records[1]]
    observations = build_session_observations(selected)
    assert len(observations) == 1
    assert observations[0]["observation_count"] == 1
    assert observations[0]["activity_time_basis"] == "capture_observed"
    assert observations[0]["first_activity_at"] == 1_784_188_801.0


def test_singleton_conflict_version_is_never_treated_as_a_safe_retry() -> None:
    envelope = _session_envelope(capture_time_fallback=True)
    record = SimpleNamespace(envelope=envelope, is_conflict=True, first_receipt_sequence=9)
    diagnostics: dict[str, int] = {}

    assert select_session_projection_envelopes(
        [record],
        complete_conflict_keys={envelope["idempotency_key"]},
        diagnostics=diagnostics,
    ) == []
    assert diagnostics["material_conflict_groups_dropped"] == 1


def test_retry_versions_without_the_base_version_are_dropped() -> None:
    first = _session_envelope(capture_time_fallback=True)
    second = deepcopy(first)
    second["evidence_id"] = "ev-second-conflict"
    second["integrity_hash"] = f"sha256:{'b' * 64}"
    second["event_timestamp"] = "2026-07-16T08:00:01Z"
    second["observed_at"] = "2026-07-16T08:00:01Z"
    records = [
        SimpleNamespace(envelope=first, is_conflict=True, first_receipt_sequence=8),
        SimpleNamespace(envelope=second, is_conflict=True, first_receipt_sequence=9),
    ]

    assert select_session_projection_envelopes(records) == []


def test_session_projection_never_copies_body_fields() -> None:
    canary = "CANARY_SESSION_BODY_MUST_NOT_LEAK"
    envelope = _session_envelope()
    envelope["body"] = canary
    envelope["subjects"]["extra"]["raw_body"] = canary
    envelope["payload"]["prompt"] = canary
    envelope["payload"]["response"] = canary
    envelope["payload"]["attributes"].update(
        {"command": canary, "stdout": canary, "stderr": canary}
    )

    rendered = json.dumps(build_session_observations([envelope]), sort_keys=True)

    assert canary not in rendered
