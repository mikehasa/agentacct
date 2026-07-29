from __future__ import annotations

import pytest

from agentacct.context_bridge import build_usage_context_bridge
from agentacct.log_evidence import build_log_evidence_index
from agentacct.service import SentinelService, SessionObservationConflict
from agentacct.task_projection import build_task_projection
from agentacct.usage_truth import (
    LOCAL_SESSION_OBSERVATION_PROVENANCE,
    LOCAL_SESSION_OBSERVATION_SOURCE,
    is_local_session_observation_event,
    mark_trusted_local_session_observation_event,
    reduce_local_session_observation_events,
)
from agentacct.work_ledger import build_work_ledger


SESSION = "zero-usage-session"
NAMESPACE = "sha256:" + "a" * 64


def _observation(
    *,
    session: str = SESSION,
    parent: str | None = None,
    kind: str = "root",
    updated_at: float = 200.0,
    namespace: str | None = NAMESPACE,
    title: str = "Observed without usage",
    evidenced: list[str] | None = None,
    event_id: str = "evt_observation0001",
    source_revision_at: int | float | None = None,
) -> dict:
    return {
        "event_id": event_id,
        "created_at": 999.0,
        "source": "codex-local-session-observation-import",
        "event_type": "session_observed",
        "run_id": "client_codex_zero_usage_session",
        "provider": "forbidden-provider",
        "model": "forbidden-model",
        "estimated_input_tokens": 0,
        "estimated_output_tokens": 0,
        "estimated_cost_usd": 0.0,
        "metadata": {
            "client": "codex",
            "client_session_id": session,
            "client_session_kind": kind,
            "parent_client_session_id": parent,
            "parent_source_namespace_fingerprint": namespace if parent else None,
            "source_namespace_fingerprint": namespace,
            "project_dir": "/tmp/observed-project",
            "started_at": 100.0,
            "updated_at": updated_at,
            "source_revision_at": source_revision_at,
            "source_parse_complete": True,
            "observed_models": ["gpt-observed"],
            "client_session_title": title,
            "client_session_title_source": "explicit_client_title_field",
            "client_session_title_sanitized": True,
            "title_redacted": False,
            "cached_input_tokens": 0,
            "evidenced_event_ids": list(evidenced or []),
            "evidenced_event_id_total": len(evidenced or []),
        },
    }


def _trusted_observation(**kwargs: object) -> dict:
    return mark_trusted_local_session_observation_event(_observation(**kwargs))


def _section(event_id: str, *, session: str | None = None) -> dict:
    return {
        "event_id": event_id,
        "created_at": 150.0,
        "source": "codex",
        "event_type": "section_completed",
        "metadata": {
            "sentinel_semantic_kind": "section",
            "section_id": "observed-work",
            "section_status": "completed",
            "section_title": "Observed work",
            "client": "codex",
            "client_session_id": session,
        },
    }


def _usage(
    evidenced: list[str],
    *,
    session: str = SESSION,
    namespace: str = NAMESPACE,
    parent: str | None = None,
    event_id: str = "evt_usage00000001",
) -> dict:
    return {
        "event_id": event_id,
        "created_at": 250.0,
        "source": "codex-local-session-import",
        "event_type": "model_usage",
        "estimated_input_tokens": 10,
        "estimated_output_tokens": 2,
        "metadata": {
            "usage_source": "local_client_session_store",
            "usage_provenance": "agent_sentinel_local_usage_import",
            "client": "codex",
            "client_session_id": session,
            "client_session_kind": "child" if parent else "root",
            "parent_client_session_id": parent,
            "source_namespace_fingerprint": namespace,
            "parent_source_namespace_fingerprint": namespace if parent else None,
            "started_at": 100.0,
            "updated_at": 200.0,
            "evidenced_event_ids": evidenced,
            "evidenced_event_id_total": len(evidenced),
        },
    }


def test_trusted_marker_is_distinct_and_usage_free() -> None:
    event = _trusted_observation()

    assert is_local_session_observation_event(event)
    assert event["metadata"]["observation_source"] == LOCAL_SESSION_OBSERVATION_SOURCE
    assert event["metadata"]["observation_provenance"] == LOCAL_SESSION_OBSERVATION_PROVENANCE
    assert event["metadata"]["source_updated_at"] == 200.0
    for key in (
        "provider",
        "model",
        "estimated_input_tokens",
        "estimated_output_tokens",
        "estimated_cost_usd",
    ):
        assert key not in event
    assert "cached_input_tokens" not in event["metadata"]


def test_trusted_writer_strips_unknown_and_nested_usage_aliases(tmp_path) -> None:
    event = _observation()
    event["cost_basis"] = "provider_invoice"
    event["metadata"].update(
        {
            "client_reported_cost_usd": 999,
            "cache_creation_5m_input_tokens": 123,
            "tokenUsage": {"input_tokens": 456},
        }
    )

    recorded = SentinelService(tmp_path / "state").record_trusted_session_observation(
        event
    )

    assert is_local_session_observation_event(recorded)
    assert "cost_basis" not in recorded
    assert "client_reported_cost_usd" not in recorded["metadata"]
    assert "cache_creation_5m_input_tokens" not in recorded["metadata"]
    assert "tokenUsage" not in recorded["metadata"]


def test_generic_writer_cannot_forge_observation_provenance(tmp_path) -> None:
    forged = _observation()
    forged["metadata"]["observation_source"] = LOCAL_SESSION_OBSERVATION_SOURCE
    forged["metadata"]["observation_provenance"] = LOCAL_SESSION_OBSERVATION_PROVENANCE

    service = SentinelService(tmp_path / "state")
    recorded = service.record_event(forged)

    assert not is_local_session_observation_event(recorded)
    assert "observation_provenance" not in recorded["metadata"]
    assert recorded["metadata"]["reserved_observation_provenance_stripped"] is True

    replacement = service.replace_events(lambda _event: False, [forged])[0]
    assert not is_local_session_observation_event(replacement)
    assert "observation_provenance" not in replacement["metadata"]


def test_trusted_writer_compacts_by_source_watermark_and_is_idempotent(tmp_path) -> None:
    service = SentinelService(tmp_path / "state")
    first = service.record_trusted_session_observation(_observation())
    replay = service.record_trusted_session_observation(_observation())
    newer = service.record_trusted_session_observation(
        _observation(updated_at=201.0, title="New source revision")
    )
    stale = service.record_trusted_session_observation(
        _observation(updated_at=199.0, title="Stale source revision")
    )

    assert replay["event_id"] == first["event_id"]
    assert newer["event_id"] != first["event_id"]
    assert stale["event_id"] == newer["event_id"]
    trusted = [
        event for event in service.list_all_events() if is_local_session_observation_event(event)
    ]
    assert len(trusted) == 1
    assert trusted[0]["metadata"]["client_session_title"] == "New source revision"


def test_trusted_writer_orders_two_revisions_in_the_same_activity_second(tmp_path) -> None:
    service = SentinelService(tmp_path / "state")
    first_revision = 1_752_345_678_901_234_567
    service.record_trusted_session_observation(
        _observation(
            updated_at=200.0,
            source_revision_at=first_revision,
            title="First file revision",
        )
    )

    newer = service.record_trusted_session_observation(
        _observation(
            updated_at=200.0,
            source_revision_at=first_revision + 10,
            title="Second file revision",
        )
    )

    assert newer["metadata"]["client_session_title"] == "Second file revision"
    ledger = build_work_ledger(service.list_all_events())
    session = ledger["session_rollup"]["sessions"][0]
    assert session["last_activity_at"] == 200.0


def test_trusted_writer_rejects_incomplete_source_parse(tmp_path) -> None:
    event = _observation()
    event["metadata"]["source_parse_complete"] = False

    service = SentinelService(tmp_path / "state")
    with pytest.raises(SessionObservationConflict) as error:
        service.record_trusted_session_observation(event)
    assert error.value.reason == "invalid_observation"
    assert service.list_all_events() == []

    with pytest.raises(SessionObservationConflict) as replacement_error:
        service.replace_events(
            lambda _event: False,
            [event],
            trusted_session_observation_import=True,
        )
    assert replacement_error.value.reason == "invalid_observation"
    assert service.list_all_events() == []


def test_identical_observation_without_source_watermark_is_still_idempotent(tmp_path) -> None:
    service = SentinelService(tmp_path / "state")
    event = _observation()
    event["metadata"].pop("updated_at")

    first = service.record_trusted_session_observation(event)
    replay = service.record_trusted_session_observation(event)

    assert replay["event_id"] == first["event_id"]
    assert len(service.list_all_events()) == 1


def test_trusted_writer_fails_closed_on_equal_watermark_or_namespace_conflict(tmp_path) -> None:
    service = SentinelService(tmp_path / "state")
    service.record_trusted_session_observation(_observation())

    with pytest.raises(SessionObservationConflict) as same_watermark:
        service.record_trusted_session_observation(_observation(title="Different content"))
    assert same_watermark.value.reason == "same_watermark_conflict"

    with pytest.raises(SessionObservationConflict) as namespace:
        service.record_trusted_session_observation(
            _observation(updated_at=201.0, namespace="sha256:" + "b" * 64)
        )
    assert namespace.value.reason == "source_namespace_conflict"


def test_writer_persists_conflict_across_restart_and_removes_donor_authority(
    tmp_path,
) -> None:
    store = tmp_path / "state"
    service = SentinelService(store, evidence_v2_enabled=True)
    section = service.record_event(_section("evt_writer_conflict"))
    home_b = "sha256:" + "b" * 64

    service.record_trusted_session_observation(
        _observation(evidenced=[section["event_id"]])
    )
    with pytest.raises(SessionObservationConflict) as conflict:
        service.record_trusted_session_observation(
            _observation(
                namespace=home_b,
                title="Conflicting source home",
                evidenced=[section["event_id"]],
            )
        )
    assert conflict.value.reason == "source_namespace_conflict"

    restarted = SentinelService(store, create=False, evidence_v2_enabled=True)
    persisted = restarted.list_all_events()
    trusted = [event for event in persisted if is_local_session_observation_event(event)]
    assert len(trusted) == 2
    selected, diagnostics = reduce_local_session_observation_events(trusted)
    assert selected == []
    assert diagnostics["namespace_conflict_sessions"] == 1

    ledger = build_work_ledger(persisted)
    assert ledger["session_rollup"]["sessions"] == []
    assert len(ledger["work_items"]) == 1
    assert ledger["work_items"][0]["log_evidence_donor_kind"] is None
    assert len(
        [
            record
            for record in restarted.evidence.records(limit=100)
            if record.envelope.event_type == "session_observed"
        ]
    ) == 2


def test_trusted_replace_persists_every_conflicting_identity_before_refusal(
    tmp_path,
) -> None:
    store = tmp_path / "state"
    service = SentinelService(store, evidence_v2_enabled=True)
    home_b = "sha256:" + "b" * 64
    for session in ("multi-conflict-a", "multi-conflict-b"):
        service.record_trusted_session_observation(_observation(session=session))

    with pytest.raises(SessionObservationConflict) as conflict:
        service.replace_events(
            lambda _event: True,
            [
                _observation(session="multi-conflict-a", namespace=home_b),
                _observation(session="multi-conflict-b", namespace=home_b),
            ],
            trusted_session_observation_import=True,
        )
    assert conflict.value.reason == "source_namespace_conflict"

    restarted = SentinelService(store, create=False, evidence_v2_enabled=True)
    trusted = [
        event
        for event in restarted.list_all_events()
        if is_local_session_observation_event(event)
    ]
    assert {
        session: sum(
            event["metadata"]["client_session_id"] == session
            for event in trusted
        )
        for session in ("multi-conflict-a", "multi-conflict-b")
    } == {"multi-conflict-a": 2, "multi-conflict-b": 2}
    selected, diagnostics = reduce_local_session_observation_events(trusted)
    assert selected == []
    assert diagnostics["namespace_conflict_sessions"] == 2
    assert len(
        [
            record
            for record in restarted.evidence.records(limit=100)
            if record.envelope.event_type == "session_observed"
        ]
    ) == 4


def test_generic_replace_cannot_clear_observation_conflict_quarantine(
    tmp_path,
) -> None:
    store = tmp_path / "state"
    service = SentinelService(store)
    home_b = "sha256:" + "b" * 64
    service.record_trusted_session_observation(_observation())
    with pytest.raises(SessionObservationConflict):
        service.record_trusted_session_observation(
            _observation(namespace=home_b, title="Other source home")
        )

    service.replace_events(
        lambda event: (
            is_local_session_observation_event(event)
            and event["metadata"]["source_namespace_fingerprint"] == home_b
        ),
        [],
    )

    trusted = [
        event
        for event in SentinelService(store, create=False).list_all_events()
        if is_local_session_observation_event(event)
    ]
    assert len(trusted) == 2
    selected, diagnostics = reduce_local_session_observation_events(trusted)
    assert selected == []
    assert diagnostics["namespace_conflict_sessions"] == 1


def test_reconcile_refuses_stale_conflict_snapshot_after_concurrent_write(
    tmp_path,
) -> None:
    store = tmp_path / "state"
    service = SentinelService(store)
    home_b = "sha256:" + "b" * 64
    service.record_trusted_session_observation(_observation())
    with pytest.raises(SessionObservationConflict):
        service.record_trusted_session_observation(
            _observation(namespace=home_b, title="Other source home")
        )
    snapshot = service.trusted_session_observation_conflict_snapshot()

    concurrent = SentinelService(store, create=False)
    with pytest.raises(SessionObservationConflict):
        concurrent.record_trusted_session_observation(
            _observation(
                namespace=home_b,
                updated_at=300.0,
                source_revision_at=300.0,
                title="Newer concurrent source fact",
            )
        )

    reconciled = service.reconcile_trusted_session_observation_conflicts(
        [_observation()],
        expected_conflict_revisions=snapshot,
    )
    assert reconciled == []
    trusted = [
        event
        for event in SentinelService(store, create=False).list_all_events()
        if is_local_session_observation_event(event)
    ]
    assert len(trusted) == 3
    selected, diagnostics = reduce_local_session_observation_events(trusted)
    assert selected == []
    assert diagnostics["namespace_conflict_sessions"] == 1


def test_replace_events_supports_trusted_observation_lane(tmp_path) -> None:
    service = SentinelService(tmp_path / "state")
    recorded = service.replace_events(
        lambda _event: False,
        [_observation()],
        trusted_session_observation_import=True,
    )

    assert len(recorded) == 1
    assert is_local_session_observation_event(recorded[0])


def test_local_observation_projects_session_without_usage_or_mechanical_claim() -> None:
    ledger = build_work_ledger([_trusted_observation()])

    entry = ledger["session_rollup"]["sessions"][0]
    assert entry["client_session_id"] == SESSION
    assert entry["client_session_title"] == "Observed without usage"
    assert entry["usage"]["rows"] == 0
    assert entry["usage"]["total_tokens"] == 0
    assert entry["usage"]["estimated_cost_usd"] is None
    assert "not a zero-cost claim" in entry["usage_note"]
    assert entry["local_client_observation"]["measurement_basis"] == "local_client_log_observed"
    assert entry["local_client_observation"]["observation_count"] == 1
    assert entry["mechanical_capture"]["observation_count"] == 0
    assert ledger["session_rollup"]["summary"]["sessions_with_mechanical_activity"] == 0
    assert ledger["session_rollup"]["summary"]["sessions_with_local_client_observation"] == 1


def test_local_observation_creates_one_task_with_usage_unknown() -> None:
    ledger = build_work_ledger([_trusted_observation()])

    projection = build_task_projection(
        ledger["session_rollup"]["sessions"],
        ledger["work_items"],
    )

    assert projection["summary"]["task_count"] == 1
    task = projection["tasks"][0]
    assert task["primary_root"]["client_session_id"] == SESSION
    assert task["session_count"] == 1
    assert task["usage"]["rows"] == 0
    assert task["usage"]["estimated_cost_usd"] is None
    assert task["usage"]["usage_availability"] == "unknown"


def test_same_source_observation_child_folds_into_parent_task() -> None:
    root = _trusted_observation(
        session="root-session",
        updated_at=100.0,
        event_id="evt_observation_root",
    )
    child = _trusted_observation(
        session="child-session",
        parent="root-session",
        kind="child",
        updated_at=200.0,
        event_id="evt_observation_child",
    )
    ledger = build_work_ledger([root, child])

    projection = build_task_projection(
        ledger["session_rollup"]["sessions"],
        ledger["work_items"],
    )

    assert projection["summary"]["task_count"] == 1
    task = projection["tasks"][0]
    assert task["primary_root"]["client_session_id"] == "root-session"
    assert task["session_count"] == 2
    assert task["supporting_count"] == 1
    assert task["root_groups"][0]["lineage_state"] == "resolved_root"


def test_projection_uses_latest_revision_and_quarantines_conflicts() -> None:
    old = _trusted_observation(updated_at=100.0, title="Old")
    new = _trusted_observation(updated_at=200.0, title="New", event_id="evt_observation0002")
    ledger = build_work_ledger([old, new])
    assert ledger["session_rollup"]["sessions"][0]["client_session_title"] == "New"
    assert ledger["insights"]["local_session_observations"]["diagnostics"][
        "historical_revisions_collapsed"
    ] == 1

    equal_conflict = _trusted_observation(
        updated_at=200.0,
        title="Different at same watermark",
        event_id="evt_observation0003",
    )
    conflicted = build_work_ledger([new, equal_conflict])
    assert conflicted["session_rollup"]["sessions"] == []
    assert conflicted["insights"]["local_session_observations"]["diagnostics"][
        "watermark_conflict_sessions"
    ] == 1


def test_observation_can_donate_mcp_identity_without_becoming_usage() -> None:
    target = "evt_abc000000001"
    observation = _trusted_observation(evidenced=[target])
    ledger = build_work_ledger([observation, _section(target)])

    item = ledger["work_items"][0]
    assert item["client_session_id"] == SESSION
    assert item["log_evidence_donor_kind"] == "session_observation"
    assert item["log_evidenced_by_event_id"] == observation["event_id"]
    assert item["log_evidenced_by_usage_event_id"] is None
    assert ledger["usage_events"] == []


def test_unscoped_raw_session_claim_cannot_attach_to_observation_only_session() -> None:
    section = _section("evt_rawclaim00001", session=SESSION)
    ledger = build_work_ledger([_trusted_observation(), section])

    session = ledger["session_rollup"]["sessions"][0]
    assert session["work"]["counts"]["total"] == 0
    assert session["join"]["client_log_evidence"] is None
    assert ledger["session_rollup"]["summary"]["unassigned_work_items"] == 1
    assert ledger["session_rollup"]["summary"][
        "local_observation_work_namespace_join_refusals"
    ] == 1

    projection = build_task_projection(
        ledger["session_rollup"]["sessions"],
        ledger["work_items"],
    )
    assert projection["tasks"][0]["work_items"] == []
    assert projection["unresolved_work"][0]["reason"] == (
        "work_session_source_namespace_mismatch"
    )


def test_unscoped_mechanical_claim_cannot_enrich_observation_only_session() -> None:
    mechanical = {
        "client": "codex",
        "client_session_id": SESSION,
        "session_kind": "root",
        "observation_count": 1,
        "activity_time_basis": "capture_observed",
        "first_activity_at": 150.0,
        "last_activity_at": 151.0,
    }

    ledger = build_work_ledger(
        [_trusted_observation()],
        session_observations=[mechanical],
    )

    session = ledger["session_rollup"]["sessions"][0]
    assert session["mechanical_capture"]["observation_count"] == 0
    assert ledger["session_rollup"]["summary"][
        "local_observation_mechanical_namespace_join_refusals"
    ] == 1


def test_usage_donor_keeps_priority_and_legacy_fields() -> None:
    target = "evt_abc000000002"
    ledger = build_work_ledger(
        [_trusted_observation(evidenced=[target]), _usage([target]), _section(target)]
    )

    item = ledger["work_items"][0]
    assert item["log_evidence_donor_kind"] == "usage"
    assert item["log_evidenced_by_event_id"] == "evt_usage00000001"
    assert item["log_evidenced_by_usage_event_id"] == "evt_usage00000001"


def test_cross_home_usage_cannot_override_observation_donor() -> None:
    target = "evt_abc000000090"
    home_a = "sha256:" + "a" * 64
    home_b = "sha256:" + "b" * 64

    index = build_log_evidence_index(
        [
            _trusted_observation(namespace=home_a, evidenced=[target]),
            _usage([target], namespace=home_b),
            _section(target),
        ]
    )

    assert target not in index
    assert index.diagnostics["rejected_donor_links"] == 2
    assert index.diagnostics["rejection_reasons"] == {
        "source_namespace_conflict": 2
    }


def test_same_home_different_session_usage_cannot_override_observation() -> None:
    target = "evt_abc000000091"
    observation = _trusted_observation(evidenced=[target])
    usage = _usage([target], session="different-session")

    index = build_log_evidence_index([observation, usage, _section(target)])

    assert target not in index
    assert index.diagnostics["rejected_donor_links"] == 2
    assert index.diagnostics["rejection_reasons"] == {
        "cross_kind_donor_identity_conflict": 2
    }


def test_observation_log_evidence_cannot_join_usage_from_another_home() -> None:
    target = "evt_abc000000099"
    other_namespace = "sha256:" + "b" * 64
    observation = _trusted_observation(
        namespace=other_namespace,
        evidenced=[target],
    )
    ledger = build_work_ledger([observation, _usage([]), _section(target)])

    attribution = ledger["attributions"][0]
    assert attribution["join_strategy"] == "unjoined"
    assert "log_evidenced_source_namespace_conflict" in attribution["join_vetoes"]
    assert attribution["usage_tokens"] == 12
    item = ledger["work_items"][0]
    assert item["log_evidenced_source_namespace_fingerprint"] == other_namespace
    assert item["source_namespace_fingerprint"] == other_namespace
    session = ledger["session_rollup"]["sessions"][0]
    assert session["work"]["counts"]["total"] == 0
    assert ledger["session_rollup"]["summary"]["unassigned_work_items"] == 1

    projection = build_task_projection(
        ledger["session_rollup"]["sessions"],
        ledger["work_items"],
    )
    assert projection["summary"]["task_count"] == 1
    assert projection["tasks"][0]["work_items"] == []
    assert projection["unresolved_work"][0]["reason"] == (
        "work_session_source_namespace_mismatch"
    )


def test_source_constraint_stays_sticky_across_section_revisions() -> None:
    start_id = "evt_abc000000010"
    other_namespace = "sha256:" + "b" * 64
    observation = _trusted_observation(
        namespace=other_namespace,
        evidenced=[start_id],
    )
    started = _section(start_id)
    completed = _section("evt_abc000000011", session=SESSION)
    ledger = build_work_ledger([observation, _usage([]), started, completed])

    attribution = ledger["attributions"][0]
    assert attribution["work_id"] is None
    assert attribution["join_strategy"] == "unjoined"
    assert "log_evidenced_source_namespace_conflict" in attribution["join_vetoes"]
    constrained = [
        item
        for item in ledger["work_items"]
        if item.get("client_session_id") == SESSION
    ]
    assert len(constrained) == 2
    assert all(
        item["log_evidenced_source_namespace_fingerprint"] == other_namespace
        for item in constrained
    )
    assert all(item["usage_total"] == 0 for item in constrained)

    projection = build_task_projection(
        ledger["session_rollup"]["sessions"],
        ledger["work_items"],
    )
    assert projection["tasks"][0]["work_items"] == []
    assert all(
        row["reason"] == "work_session_source_namespace_mismatch"
        for row in projection["unresolved_work"]
        if row["item"].get("client_session_id") == SESSION
    )


def test_source_constraint_stays_sticky_for_same_work_id_across_run_ids() -> None:
    target = "evt_abc000000012"
    home_a = "sha256:" + "a" * 64
    home_b = "sha256:" + "b" * 64
    observation = _trusted_observation(namespace=home_a, evidenced=[target])
    started = _section(target, session=SESSION)
    started["run_id"] = "run-a"
    started["event_type"] = "section_started"
    started["metadata"]["section_status"] = "started"
    completed = _section("evt_abc000000013", session=SESSION)
    completed["run_id"] = "run-b"

    ledger = build_work_ledger(
        [observation, _usage([], namespace=home_b), started, completed]
    )

    constrained = [
        item
        for item in ledger["work_items"]
        if item.get("client_session_id") == SESSION
    ]
    assert constrained
    assert all(
        item["log_evidenced_source_namespace_fingerprint"] == home_a
        for item in constrained
    )
    assert all(item["usage_total"] == 0 for item in constrained)
    assert ledger["attributions"][0]["join_strategy"] == "unjoined"
    assert "log_evidenced_source_namespace_conflict" in ledger[
        "attributions"
    ][0]["join_vetoes"]


def test_cross_home_child_never_contributes_to_parent_rollup() -> None:
    home_a = "sha256:" + "a" * 64
    home_b = "sha256:" + "b" * 64
    root = _usage(
        [],
        session="root-session",
        namespace=home_b,
        event_id="evt_usage_root0001",
    )
    child = _usage(
        [],
        session="child-session",
        namespace=home_a,
        parent="root-session",
        event_id="evt_usage_child001",
    )

    ledger = build_work_ledger([root, child])
    sessions = {
        row["client_session_id"]: row
        for row in ledger["session_rollup"]["sessions"]
    }

    assert sessions["root-session"]["related"]["child_session_count"] == 0
    assert sessions["root-session"]["related"]["children_usage"] is None
    assert sessions["child-session"]["rollup_group_key"] == (
        "codex::child-session"
    )
    assert sessions["child-session"]["related"][
        "parent_source_namespace_mismatch"
    ] is True
    assert ledger["session_rollup"]["summary"][
        "parent_source_namespace_join_refusals"
    ] == 1


def test_cross_home_raw_session_collision_never_allocates_both_usage_rows() -> None:
    home_a = "sha256:" + "a" * 64
    home_b = "sha256:" + "b" * 64
    section = _section("evt_rawcollision01", session=SESSION)
    ledger = build_work_ledger(
        [
            _usage([], namespace=home_a, event_id="evt_usage_home_a01"),
            _usage([], namespace=home_b, event_id="evt_usage_home_b01"),
            section,
        ]
    )

    assert len(ledger["attributions"]) == 2
    assert all(row["work_id"] is None for row in ledger["attributions"])
    assert all(
        "client_session_id_source_namespace_ambiguous" in row["join_vetoes"]
        for row in ledger["attributions"]
    )
    assert ledger["work_items"][0]["usage_total"] == 0
    assert ledger["work_items"][0]["linked_usage_records"] == 0


def test_cross_home_raw_transcript_collision_never_allocates_both_rows() -> None:
    home_a = "sha256:" + "a" * 64
    home_b = "sha256:" + "b" * 64
    transcript = "shared-transcript"
    usage_a = _usage(
        [],
        session="session-a",
        namespace=home_a,
        event_id="evt_usage_trans_a01",
    )
    usage_b = _usage(
        [],
        session="session-b",
        namespace=home_b,
        event_id="evt_usage_trans_b01",
    )
    usage_a["metadata"]["client_transcript_id"] = transcript
    usage_b["metadata"]["client_transcript_id"] = transcript
    section = _section("evt_transcollision", session=None)
    section["metadata"]["client_transcript_id"] = transcript

    ledger = build_work_ledger([usage_a, usage_b, section])

    assert all(row["work_id"] is None for row in ledger["attributions"])
    assert all(
        "client_transcript_id_source_namespace_ambiguous" in row["join_vetoes"]
        for row in ledger["attributions"]
    )
    assert ledger["work_items"][0]["usage_total"] == 0

    bridge = build_usage_context_bridge([usage_a, usage_b, section])
    assert bridge["attributed_usage_records"] == 0
    assert bridge["context_matched_usage_records"] == 0
    assert all(link["join_strategy"] == "unjoined" for link in bridge["links"])


def test_matching_log_evidence_allocates_only_its_home_in_raw_collision() -> None:
    target = "evt_abc000000092"
    home_a = "sha256:" + "a" * 64
    home_b = "sha256:" + "b" * 64
    ledger = build_work_ledger(
        [
            _trusted_observation(namespace=home_a, evidenced=[target]),
            _usage([], namespace=home_a, event_id="evt_usage_match_a01"),
            _usage([], namespace=home_b, event_id="evt_usage_match_b01"),
            _section(target),
        ]
    )

    joined = [row for row in ledger["attributions"] if row["work_id"]]
    refused = [row for row in ledger["attributions"] if row["work_id"] is None]
    assert len(joined) == 1
    assert joined[0]["usage_event_id"] == "evt_usage_match_a01"
    assert len(refused) == 1
    assert "client_session_id_source_namespace_ambiguous" in refused[0][
        "join_vetoes"
    ]
    assert ledger["work_items"][0]["usage_total"] == 12
    assert ledger["work_items"][0]["linked_usage_records"] == 1


def test_only_latest_observation_revision_can_donate_and_namespace_conflicts_refuse() -> None:
    old_target = "evt_abc000000003"
    new_target = "evt_abc000000004"
    old = _trusted_observation(updated_at=100.0, evidenced=[old_target])
    new = _trusted_observation(
        updated_at=200.0,
        evidenced=[new_target],
        event_id="evt_observation0002",
    )
    index = build_log_evidence_index(
        [old, new, _section(old_target), _section(new_target)]
    )
    assert old_target not in index
    assert index[new_target][0]["donor_kind"] == "session_observation"

    other_namespace = _trusted_observation(
        updated_at=300.0,
        namespace="sha256:" + "b" * 64,
        evidenced=[new_target],
        event_id="evt_observation0003",
    )
    conflicted = build_log_evidence_index(
        [new, other_namespace, _section(new_target)]
    )
    assert new_target not in conflicted


def test_rejected_usage_donor_falls_back_to_valid_observation_donor() -> None:
    target = "evt_abc000000088"
    observation = _trusted_observation(evidenced=[target])
    rejected_usage = _usage(
        [target],
        session="late-child",
        parent="late-parent",
        event_id="evt_usage_late001",
    )
    rejected_usage["metadata"]["started_at"] = 10_000.0

    index = build_log_evidence_index(
        [observation, rejected_usage, _section(target)]
    )

    assert index[target][0]["donor_kind"] == "session_observation"
    assert index[target][0]["client_session_id"] == SESSION
    assert index.diagnostics["rejection_reasons"][
        "codex_event_before_session_start"
    ] == 1
