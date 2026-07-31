from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from agentacct.evidence_runtime import EvidenceRuntime
from agentacct.refreshable_usage import refreshable_usage_source_order
from agentacct.service import SentinelService
from agentacct.usage_truth import (
    LOCAL_USAGE_PROVENANCE,
    LOCAL_USAGE_SOURCE,
    is_legacy_local_usage_import_shape,
    is_local_usage_import_event,
    mark_trusted_local_usage_import_event,
)


NAMESPACE_A = "sha256:" + "a" * 64
NAMESPACE_B = "sha256:" + "b" * 64

_COUNT_FIELDS = (
    "inserted",
    "updated",
    "deleted",
    "noop",
    "stale",
    "conflicts",
    "existing_conflicts",
    "excluded",
    "spool_written",
)


def _usage_event(
    *,
    event_id: str = "evt_usage_a",
    created_at: float = 1_000.0,
    source_order: float = 100.0,
    client: str = "codex",
    session_id: str = "session-a",
    namespace: str = NAMESPACE_A,
    lane: str | None = None,
    representation: str = "local-client-session-usage-v1",
    model: str | None = None,
    input_tokens: int = 100,
    output_tokens: int = 20,
    cost_usd: float | None = None,
    client_reported_cost: bool = False,
    evidenced_event_ids: tuple[str, ...] = ("evt_evidence_a",),
    update_semantics: str | None = None,
) -> dict[str, Any]:
    if model is None:
        model = {
            "claude-code": "claude-opus-4-8",
            "hermes": "gpt-5.5",
        }.get(client, "gpt-5.6-sol")
    if update_semantics is None:
        update_semantics = {
            "claude-code": "claude_assistant_message_usage_rows",
            "hermes": "hermes_state_db_session_rows",
        }.get(client, "codex_rollout_token_count_events")
    provider = "claude-code" if client == "claude-code" else ("openai" if client == "hermes" else "codex")
    metadata: dict[str, Any] = {
        "client": client,
        "client_session_id": session_id,
        "client_transcript_id": f"{client}-transcript-{session_id}",
        "source_namespace_fingerprint": namespace,
        "usage_update_semantics": update_semantics,
        "usage_representation": representation,
        "usage_additive": True,
        # Source order is durable client truth. Chronicle arrival fields below
        # are deliberately allowed to change on every refresh.
        "source_revision_at": source_order,
        "updated_at": source_order,
        "started_at": 10.0,
        "cached_input_tokens": 5,
        "cache_creation_input_tokens": 2,
        "cache_read_input_tokens": 3,
        "reasoning_output_tokens": 4,
        "evidenced_event_ids": list(evidenced_event_ids),
        "evidenced_event_id_total": len(evidenced_event_ids),
    }
    if lane is not None:
        metadata["usage_row_lane"] = lane
    if client_reported_cost:
        metadata["client_reported_cost_usd"] = cost_usd
    elif cost_usd is not None:
        metadata["pricing_source"] = "test-derived-catalog"
    event = {
        "event_id": event_id,
        "created_at": created_at,
        "source": f"{client}-local-session-import",
        "event_type": "model_usage",
        "provider": provider,
        "model": model,
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": output_tokens,
        "estimated_cost_usd": cost_usd,
        "usage_confidence": "client_reported",
        "cost_confidence": (
            "client_reported"
            if client_reported_cost
            else ("estimated_from_tokens" if cost_usd is not None else "unknown")
        ),
        "metadata": metadata,
    }
    return mark_trusted_local_usage_import_event(event)


def _payload(result: object) -> dict[str, Any]:
    if isinstance(result, Mapping):
        payload = dict(result)
    else:
        to_dict = getattr(result, "to_dict", None)
        assert callable(to_dict), "reconcile result must be a mapping or expose to_dict()"
        payload = to_dict()
    assert isinstance(payload, dict)
    assert set(_COUNT_FIELDS).issubset(payload)
    assert "errors" in payload
    return payload


def _count(payload: Mapping[str, Any], field: str) -> int:
    value = payload[field]
    assert isinstance(value, int) and not isinstance(value, bool), f"{field} must be an integer count"
    assert value >= 0
    return value


def _error_count(payload: Mapping[str, Any]) -> int:
    value = payload["errors"]
    if isinstance(value, int) and not isinstance(value, bool):
        assert value >= 0
        return value
    assert isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    return len(value)


def _assert_counts(result: object, **expected: int) -> dict[str, Any]:
    payload = _payload(result)
    counts = {field: _count(payload, field) for field in _COUNT_FIELDS}
    wanted = dict.fromkeys(_COUNT_FIELDS, 0)
    wanted.update(expected)
    assert counts == wanted
    return payload


def _stats(runtime: EvidenceRuntime) -> dict[str, int]:
    return runtime.store.stats().to_dict()


def _spool_size(runtime: EvidenceRuntime) -> int:
    path = runtime.store.refreshable_usage_spool_path
    return path.stat().st_size if path.exists() else 0


def test_same_refresh_with_new_arrival_identity_is_a_physical_noop(tmp_path: Path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    first = _usage_event(event_id="evt_first_arrival", created_at=1_000.0)

    first_result = _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([first], complete=True),
        inserted=1,
        spool_written=1,
    )
    assert _error_count(first_result) == 0
    before_stats = _stats(runtime)
    before_spool = _spool_size(runtime)

    # event_id and created_at are Chronicle arrival artifacts, not the
    # refreshable usage fact's identity or content.
    repeated = _usage_event(event_id="evt_random_reissue", created_at=9_999.0)
    repeated_result = _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([repeated], complete=True),
        noop=1,
    )

    assert _error_count(repeated_result) == 0
    assert _stats(runtime) == before_stats
    assert _spool_size(runtime) == before_spool


def test_real_token_change_adds_one_revision_without_adding_a_logical_slot(tmp_path: Path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([_usage_event()], complete=True),
        inserted=1,
        spool_written=1,
    )
    first_spool = _spool_size(runtime)

    changed = _usage_event(
        event_id="evt_usage_updated",
        created_at=2_000.0,
        source_order=200.0,
        input_tokens=175,
    )
    changed_result = _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([changed], complete=True),
        updated=1,
        spool_written=1,
    )

    assert _error_count(changed_result) == 0
    stats = _stats(runtime)
    assert stats["logical_events"] == 1
    assert stats["evidence_versions"] == 2
    assert stats["receipts"] == 2
    assert stats["conflict_groups"] == 0
    assert _spool_size(runtime) > first_spool

    before_replay = (_stats(runtime), _spool_size(runtime))
    replay = _usage_event(
        event_id="evt_usage_updated_reissued",
        created_at=8_000.0,
        source_order=200.0,
        input_tokens=175,
    )
    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([replay], complete=True),
        noop=1,
    )
    assert (_stats(runtime), _spool_size(runtime)) == before_replay


def test_derived_repricing_is_noop_but_client_reported_cost_change_is_an_update(tmp_path: Path) -> None:
    derived = EvidenceRuntime(tmp_path / "derived", enabled=True)
    _assert_counts(
        derived.reconcile_refreshable_usage_snapshot(
            [_usage_event(cost_usd=0.01)],
            complete=True,
        ),
        inserted=1,
        spool_written=1,
    )
    derived_before = (_stats(derived), _spool_size(derived))
    repriced = _usage_event(
        event_id="evt_repriced",
        created_at=2_000.0,
        cost_usd=99.99,
    )
    _assert_counts(
        derived.reconcile_refreshable_usage_snapshot([repriced], complete=True),
        noop=1,
    )
    assert (_stats(derived), _spool_size(derived)) == derived_before

    reported = EvidenceRuntime(tmp_path / "client-reported", enabled=True)
    _assert_counts(
        reported.reconcile_refreshable_usage_snapshot(
            [_usage_event(cost_usd=0.01, client_reported_cost=True)],
            complete=True,
        ),
        inserted=1,
        spool_written=1,
    )
    changed_cost = _usage_event(
        event_id="evt_client_cost_changed",
        created_at=2_000.0,
        source_order=200.0,
        cost_usd=0.02,
        client_reported_cost=True,
    )
    _assert_counts(
        reported.reconcile_refreshable_usage_snapshot([changed_cost], complete=True),
        updated=1,
        spool_written=1,
    )
    stats = _stats(reported)
    assert stats["logical_events"] == 1
    assert stats["evidence_versions"] == 2
    assert stats["conflict_groups"] == 0


def test_product_exposes_only_reported_usage_and_client_cost_scalars(tmp_path: Path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    event = _usage_event(
        input_tokens=123,
        output_tokens=45,
        cost_usd=0.25,
        client_reported_cost=True,
    )
    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([event], complete=True),
        inserted=1,
        spool_written=1,
    )

    product = runtime.product()
    records = product["cost_outcome_basis"]["records"]
    usage = next(record for record in records if record["dimension"] == "usage")
    cost = next(record for record in records if record["dimension"] == "cost")
    assert usage["values"] == {
        "input_tokens": 123,
        "output_tokens": 45,
        "cached_input_tokens": 5,
        "cache_creation_input_tokens": 2,
        "cache_read_input_tokens": 3,
        "reasoning_output_tokens": 4,
    }
    assert cost["values"] == {"cost_usd": 0.25}

    envelope = runtime.store.query()[0].envelope
    nested = envelope.payload["refreshable_usage"]
    assert envelope.payload["measurement_id"] == nested["revision_id"]
    assert nested["truth"]["tokens"]["input_tokens"] == {
        "reported": True,
        "value": 123,
    }
    assert nested["truth_digest"]


def test_product_withholds_unreported_tokens_and_derived_repricing_cost(tmp_path: Path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    initial = _usage_event(input_tokens=-1, cost_usd=0.01)
    initial["metadata"]["input_tokens_reported"] = False
    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([initial], complete=True),
        inserted=1,
        spool_written=1,
    )
    before = (_stats(runtime), _spool_size(runtime))

    repriced = _usage_event(
        event_id="evt_derived_reprice",
        created_at=2_000.0,
        input_tokens=-1,
        cost_usd=99.99,
    )
    repriced["metadata"]["input_tokens_reported"] = False
    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([repriced], complete=True),
        noop=1,
    )
    assert (_stats(runtime), _spool_size(runtime)) == before

    records = runtime.product()["cost_outcome_basis"]["records"]
    usage = next(record for record in records if record["dimension"] == "usage")
    assert "input_tokens" not in usage["values"]
    assert all(record["dimension"] != "cost" for record in records)
    assert all("cost_usd" not in record["values"] for record in records)


def test_namespace_client_session_lane_and_representation_are_distinct_slots(tmp_path: Path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    events = [
        _usage_event(event_id="evt_base", lane="session_total"),
        _usage_event(event_id="evt_namespace", namespace=NAMESPACE_B, lane="session_total"),
        _usage_event(event_id="evt_client", client="hermes", lane="session_total"),
        _usage_event(event_id="evt_session", session_id="session-b", lane="session_total"),
        _usage_event(event_id="evt_lane", lane="project_total"),
        _usage_event(
            event_id="evt_representation",
            lane="session_total",
            representation="fallback-session-usage-v1",
        ),
    ]

    result = _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot(events, complete=True),
        inserted=6,
        spool_written=6,
    )

    assert _error_count(result) == 0
    stats = _stats(runtime)
    assert stats["logical_events"] == 6
    assert stats["evidence_versions"] == 6
    assert stats["receipts"] == 6
    assert stats["conflict_groups"] == 0
    run_ids = {
        record.envelope.subjects.run_id
        for record in runtime.store.query(limit=20)
    }
    assert None not in run_ids
    assert len(run_ids) == 6


def test_product_run_id_disambiguates_sanitized_and_truncated_sessions(
    tmp_path: Path,
) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    shared_prefix = "session." + "x" * 100
    events = [
        _usage_event(event_id="evt_long_a", session_id=shared_prefix + "-tail-a"),
        _usage_event(event_id="evt_long_b", session_id=shared_prefix + "-tail-b"),
    ]

    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot(events, complete=True),
        inserted=2,
        spool_written=2,
    )

    envelopes = [record.envelope for record in runtime.store.query(limit=10)]
    run_ids = {envelope.subjects.run_id for envelope in envelopes}
    assert len(run_ids) == 2
    assert all(run_id is not None and len(run_id) <= 512 for run_id in run_ids)


def test_product_run_id_is_stable_across_revisions_of_one_slot(
    tmp_path: Path,
) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([_usage_event()], complete=True),
        inserted=1,
        spool_written=1,
    )
    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot(
            [_usage_event(event_id="evt_update", source_order=200, input_tokens=200)],
            complete=True,
        ),
        updated=1,
        spool_written=1,
    )

    run_ids = {
        record.envelope.subjects.run_id
        for record in runtime.store.query(limit=10)
    }
    assert len(run_ids) == 1


def test_claude_model_lanes_stay_separate_while_codex_model_drift_updates_one_slot(tmp_path: Path) -> None:
    claude = EvidenceRuntime(tmp_path / "claude", enabled=True)
    opus = _usage_event(
        event_id="evt_opus",
        client="claude-code",
        model="claude-opus-4-8",
        representation="claude-assistant-message-usage-v1",
    )
    haiku = _usage_event(
        event_id="evt_haiku",
        client="claude-code",
        model="claude-haiku-4-5-20251001",
        representation="claude-assistant-message-usage-v1",
    )
    _assert_counts(
        claude.reconcile_refreshable_usage_snapshot([opus, haiku], complete=True),
        inserted=2,
        spool_written=2,
    )
    assert _stats(claude)["logical_events"] == 2

    codex = EvidenceRuntime(tmp_path / "codex", enabled=True)
    old_model = _usage_event(event_id="evt_codex_old_model", model="gpt-5.5")
    _assert_counts(
        codex.reconcile_refreshable_usage_snapshot([old_model], complete=True),
        inserted=1,
        spool_written=1,
    )
    new_model = _usage_event(
        event_id="evt_codex_new_model",
        created_at=2_000.0,
        source_order=200.0,
        model="gpt-5.6-sol",
        input_tokens=125,
    )
    _assert_counts(
        codex.reconcile_refreshable_usage_snapshot([new_model], complete=True),
        updated=1,
        spool_written=1,
    )
    stats = _stats(codex)
    assert stats["logical_events"] == 1
    assert stats["evidence_versions"] == 2
    assert stats["conflict_groups"] == 0


def test_claude_sanitized_and_truncated_model_collisions_remain_distinct_slots(
    tmp_path: Path,
) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    long_prefix = "x" * 80
    events = [
        _usage_event(event_id="evt_dot", client="claude-code", model="a.b"),
        _usage_event(event_id="evt_underscore", client="claude-code", model="a_b"),
        _usage_event(
            event_id="evt_long_a",
            client="claude-code",
            model=long_prefix + "-tail-a",
        ),
        _usage_event(
            event_id="evt_long_b",
            client="claude-code",
            model=long_prefix + "-tail-b",
        ),
    ]
    for event in events[:2]:
        event["metadata"]["usage_row_lane"] = "model:a_b"
    for event in events[2:]:
        event["metadata"]["usage_row_lane"] = "model:" + long_prefix

    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot(events, complete=True),
        inserted=4,
        spool_written=4,
    )
    assert _stats(runtime)["logical_events"] == 4
    assert runtime.store.refreshable_usage_stats().current_heads == 4


def test_evidenced_event_link_change_is_a_real_revision(tmp_path: Path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([_usage_event()], complete=True),
        inserted=1,
        spool_written=1,
    )
    linked = _usage_event(
        event_id="evt_linked_update",
        created_at=2_000.0,
        source_order=200.0,
        evidenced_event_ids=("evt_evidence_a", "evt_evidence_b"),
    )
    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([linked], complete=True),
        updated=1,
        spool_written=1,
    )
    stats = _stats(runtime)
    assert stats["logical_events"] == 1
    assert stats["evidence_versions"] == 2

    before_replay = (_stats(runtime), _spool_size(runtime))
    replay = _usage_event(
        event_id="evt_linked_update_reissued",
        created_at=9_000.0,
        source_order=200.0,
        evidenced_event_ids=("evt_evidence_a", "evt_evidence_b"),
    )
    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([replay], complete=True),
        noop=1,
    )
    assert (_stats(runtime), _spool_size(runtime)) == before_replay


def test_only_complete_snapshot_deletes_missing_current_slot(tmp_path: Path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    current = _usage_event()
    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([current], complete=True),
        inserted=1,
        spool_written=1,
    )
    before_partial = (_stats(runtime), _spool_size(runtime))

    partial = _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([], complete=False),
    )
    assert _error_count(partial) == 0
    assert (_stats(runtime), _spool_size(runtime)) == before_partial
    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([current], complete=False),
        noop=1,
    )

    deleted = _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([], complete=True),
        deleted=1,
        spool_written=1,
    )
    assert _error_count(deleted) == 0
    stats = _stats(runtime)
    assert stats["logical_events"] == 1
    # Deletion is a durable current-state tombstone transition, not a fake
    # zero-usage Evidence measurement.
    assert stats["evidence_versions"] == 1
    assert stats["receipts"] == 1
    refreshable = runtime.store.refreshable_usage_stats().to_dict()
    assert refreshable["current_heads"] == 0
    assert refreshable["tombstoned_heads"] == 1
    assert refreshable["transitions"] == 2
    after_delete = (_stats(runtime), _spool_size(runtime))

    # A complete empty snapshot is stable after the tombstone is current.
    _assert_counts(runtime.reconcile_refreshable_usage_snapshot([], complete=True))
    assert (_stats(runtime), _spool_size(runtime)) == after_delete


def test_complete_snapshot_resurrects_same_truth_after_tombstone(tmp_path: Path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    current = _usage_event()
    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([current], complete=True),
        inserted=1,
        spool_written=1,
    )
    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([], complete=True),
        deleted=1,
        spool_written=1,
    )

    restored = _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([current], complete=True),
        updated=1,
        spool_written=1,
    )

    assert restored["resurrected"] == 1
    head = runtime.store.refreshable_usage_heads()[0]
    assert head.tombstoned is False
    assert head.current_revision_id is not None
    before_repeat = (_stats(runtime), _spool_size(runtime))
    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([current], complete=True),
        noop=1,
    )
    assert (_stats(runtime), _spool_size(runtime)) == before_repeat


def test_older_source_arrival_is_stale_and_cannot_roll_back_current(tmp_path: Path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    current = _usage_event(
        event_id="evt_newer",
        created_at=2_000.0,
        source_order=200.0,
        input_tokens=200,
    )
    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([current], complete=True),
        inserted=1,
        spool_written=1,
    )
    before_stale = (_stats(runtime), _spool_size(runtime))

    stale = _usage_event(
        event_id="evt_older_late_arrival",
        created_at=9_000.0,
        source_order=100.0,
        input_tokens=100,
    )
    stale_result = _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([stale], complete=True),
        stale=1,
    )
    assert _error_count(stale_result) == 0
    assert (_stats(runtime), _spool_size(runtime)) == before_stale

    replay_current = _usage_event(
        event_id="evt_newer_reissued",
        created_at=10_000.0,
        source_order=200.0,
        input_tokens=200,
    )
    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([replay_current], complete=True),
        noop=1,
    )
    assert (_stats(runtime), _spool_size(runtime)) == before_stale


def test_equal_source_order_divergence_records_one_stable_conflict(tmp_path: Path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    incumbent = _usage_event(event_id="evt_incumbent", source_order=100.0, input_tokens=100)
    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([incumbent], complete=True),
        inserted=1,
        spool_written=1,
    )
    divergent = _usage_event(
        event_id="evt_divergent",
        created_at=2_000.0,
        source_order=100.0,
        input_tokens=200,
    )
    conflict = _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([divergent], complete=True),
        conflicts=1,
        spool_written=1,
    )
    assert _error_count(conflict) == 0
    stats = _stats(runtime)
    assert stats["logical_events"] == 1
    assert stats["evidence_versions"] == 2
    assert stats["receipts"] == 2
    assert stats["conflict_groups"] == 1
    assert stats["conflict_versions"] == 2
    after_first_conflict = (_stats(runtime), _spool_size(runtime))

    same_conflict_reissued = _usage_event(
        event_id="evt_divergent_reissued",
        created_at=9_000.0,
        source_order=100.0,
        input_tokens=200,
    )
    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([same_conflict_reissued], complete=True),
        noop=1,
        existing_conflicts=1,
    )
    assert (_stats(runtime), _spool_size(runtime)) == after_first_conflict


def test_identical_concurrent_snapshots_commit_one_physical_revision(tmp_path: Path) -> None:
    runtime_a = EvidenceRuntime(tmp_path, enabled=True)
    runtime_b = EvidenceRuntime(tmp_path, enabled=True)
    # Initialize both handles before the race so the test targets reconcile,
    # not schema bootstrap timing.
    runtime_a.store
    runtime_b.store
    barrier = Barrier(2)

    def reconcile(runtime: EvidenceRuntime, event: dict[str, Any]) -> dict[str, Any]:
        barrier.wait(timeout=5)
        return _payload(runtime.reconcile_refreshable_usage_snapshot([event], complete=True))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(reconcile, runtime_a, _usage_event(event_id="evt_race_a", created_at=1_000.0)),
            pool.submit(reconcile, runtime_b, _usage_event(event_id="evt_race_b", created_at=2_000.0)),
        ]
        results = [future.result(timeout=10) for future in futures]

    assert sum(_count(result, "inserted") for result in results) == 1
    assert sum(_count(result, "noop") for result in results) == 1
    assert sum(_count(result, "spool_written") for result in results) == 1
    assert sum(_error_count(result) for result in results) == 0
    stats = _stats(EvidenceRuntime(tmp_path, enabled=True))
    assert stats["logical_events"] == 1
    assert stats["evidence_versions"] == 1
    assert stats["receipts"] == 1


def test_atomic_increment_is_excluded_then_can_use_generic_shadow_lane(tmp_path: Path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    atomic = _usage_event(
        event_id="evt_atomic",
        update_semantics="atomic_increment",
        representation="immutable-usage-event-v1",
    )

    excluded = _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([atomic], complete=True),
        excluded=1,
    )
    assert _error_count(excluded) == 0
    stats = _stats(runtime)
    assert stats["logical_events"] == 0
    assert stats["evidence_versions"] == 0
    assert stats["receipts"] == 0

    # The caller retains the original event and routes it through ordinary
    # immutable Evidence append semantics instead of a current snapshot lane.
    fallback = runtime.shadow_v1_event(atomic, transport="internal")
    assert fallback.disposition == "inserted"
    stats = _stats(runtime)
    assert stats["logical_events"] == 1
    assert stats["evidence_versions"] == 1
    assert stats["receipts"] == 1


@pytest.mark.parametrize(
    "reserved_markers",
    [
        ("usage_source",),
        ("usage_provenance",),
        ("usage_source", "usage_provenance"),
    ],
)
@pytest.mark.parametrize(
    "update_semantics",
    ["codex_rollout_token_count_events", "atomic_increment"],
)
def test_reserved_marker_with_incomplete_identity_withholds_complete_deletion(
    tmp_path: Path,
    reserved_markers: tuple[str, ...],
    update_semantics: str,
) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([_usage_event()], complete=True),
        inserted=1,
        spool_written=1,
    )
    before = (_stats(runtime), _spool_size(runtime))

    malformed = _usage_event(
        event_id="evt_reserved_malformed",
        source_order=200.0,
        update_semantics=update_semantics,
    )
    malformed["metadata"].pop("client_session_id")
    marker_values = {
        "usage_source": LOCAL_USAGE_SOURCE,
        "usage_provenance": LOCAL_USAGE_PROVENANCE,
    }
    for marker, value in marker_values.items():
        if marker in reserved_markers:
            malformed["metadata"][marker] = value
        else:
            malformed["metadata"].pop(marker)

    result = _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([malformed], complete=True),
    )

    assert _error_count(result) == 1
    assert result["complete_applied"] is False
    assert _count(result, "excluded") == 0
    assert (_stats(runtime), _spool_size(runtime)) == before
    assert runtime.store.refreshable_usage_stats().current_heads == 1


def test_ordinary_event_without_reserved_usage_markers_is_ignored(tmp_path: Path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    ordinary = _usage_event()
    ordinary["metadata"].pop("usage_source")
    ordinary["metadata"].pop("usage_provenance")
    ordinary["metadata"].pop("client_session_id")

    result = _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([ordinary], complete=False),
    )

    assert _error_count(result) == 0
    assert runtime.store.refreshable_usage_stats().heads == 0


@pytest.mark.parametrize(
    "invalid_case",
    [
        "negative_token",
        "negative_client_cost",
        "invalid_namespace",
        "non_bool_presence",
        "invalid_usage_confidence",
        "invalid_precedence",
        "invalid_client_cost_confidence",
        "mismatched_claude_lane",
    ],
)
def test_invalid_refreshable_truth_cannot_update_or_delete_current_head(
    tmp_path: Path,
    invalid_case: str,
) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([_usage_event()], complete=True),
        inserted=1,
        spool_written=1,
    )
    before = (_stats(runtime), _spool_size(runtime))
    invalid = _usage_event(
        event_id=f"evt_{invalid_case}",
        source_order=200.0,
        input_tokens=200,
    )
    if invalid_case == "negative_token":
        invalid["estimated_input_tokens"] = -1
        invalid["metadata"]["input_tokens_reported"] = True
    elif invalid_case == "negative_client_cost":
        invalid["metadata"]["client_reported_cost_usd"] = -0.25
        invalid["cost_confidence"] = "client_reported"
    elif invalid_case == "invalid_namespace":
        invalid["metadata"]["source_namespace_fingerprint"] = "/private/client-home"
    elif invalid_case == "non_bool_presence":
        invalid["metadata"]["input_tokens_reported"] = "true"
    elif invalid_case == "invalid_usage_confidence":
        invalid["usage_confidence"] = "estimated"
    elif invalid_case == "invalid_precedence":
        invalid["metadata"]["precedence_role"] = 1
    elif invalid_case == "invalid_client_cost_confidence":
        invalid["metadata"]["client_reported_cost_usd"] = 0.25
        invalid["cost_confidence"] = "estimated_from_tokens"
    elif invalid_case == "mismatched_claude_lane":
        invalid = _usage_event(
            event_id="evt_mismatched_claude_lane",
            source_order=200.0,
            client="claude-code",
            model="a.b",
        )
        invalid["metadata"]["usage_row_lane"] = "model:other"

    result = _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([invalid], complete=True),
    )

    assert _error_count(result) == 1
    assert result["complete_applied"] is False
    assert (_stats(runtime), _spool_size(runtime)) == before
    refreshable = runtime.store.refreshable_usage_stats()
    assert refreshable.current_heads == 1
    assert refreshable.revisions == 1


def test_runtime_is_fail_open_and_next_identical_complete_snapshot_self_heals(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    store = runtime.store
    reconcile_store = store.reconcile_refreshable_usage_snapshot
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected refreshable reconcile failure")
        return reconcile_store(*args, **kwargs)

    monkeypatch.setattr(store, "reconcile_refreshable_usage_snapshot", fail_once)
    event = _usage_event()

    failed = _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([event], complete=True),
    )
    assert _error_count(failed) == 1
    assert _stats(runtime)["evidence_versions"] == 0
    assert _spool_size(runtime) == 0

    healed = _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([event], complete=True),
        inserted=1,
        spool_written=1,
    )
    assert _error_count(healed) == 0
    before_replay = (_stats(runtime), _spool_size(runtime))

    replay = _usage_event(event_id="evt_after_heal_reissued", created_at=9_000.0)
    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([replay], complete=True),
        noop=1,
    )
    assert (_stats(runtime), _spool_size(runtime)) == before_replay


def test_service_refresh_remints_v1_arrival_but_not_evidence_revision(tmp_path: Path) -> None:
    service = SentinelService(tmp_path)
    first = service.replace_events(
        lambda _event: False,
        [_usage_event(event_id="caller_first", created_at=1.0)],
        trusted_usage_import=True,
    )
    before = (service.evidence.store.stats(), _spool_size(service.evidence))

    repeated = service.replace_events(
        is_local_usage_import_event,
        [_usage_event(event_id="caller_second", created_at=9_999.0)],
        trusted_usage_import=True,
    )

    assert len(first) == len(repeated) == 1
    assert first[0]["event_id"] != repeated[0]["event_id"]
    assert first[0]["created_at"] != repeated[0]["created_at"]
    assert len(service.list_all_events()) == 1
    assert (service.evidence.store.stats(), _spool_size(service.evidence)) == before

    changed = service.replace_events(
        is_local_usage_import_event,
        [
            _usage_event(
                event_id="caller_changed",
                created_at=10_000.0,
                source_order=200.0,
                input_tokens=200,
            )
        ],
        trusted_usage_import=True,
    )
    assert len(changed) == 1
    stats = service.evidence.store.stats()
    assert stats.logical_events == 1
    assert stats.evidence_versions == 2
    assert stats.receipts == 2
    refreshable = service.evidence.store.refreshable_usage_stats()
    assert refreshable.current_heads == 1
    assert refreshable.revisions == 2
    assert refreshable.superseded_revisions == 1


def test_service_withholds_complete_deletion_when_v1_has_unparseable_line(tmp_path: Path, monkeypatch) -> None:
    # Hand-seeds a torn/unparseable raw line into events.jsonl to exercise the
    # snapshot's parse-completeness gate — flat-file mechanics only present in
    # legacy mirror mode.
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "0")
    service = SentinelService(tmp_path)
    service.replace_events(
        lambda _event: False,
        [_usage_event()],
        trusted_usage_import=True,
    )
    with service.events_path.open("a", encoding="utf-8") as handle:
        handle.write("{torn-json\n")
        handle.write("[]\n")

    result = service.reconcile_evidence_refreshable_usage_snapshot(complete=True)

    assert result["ledger_snapshot_complete_requested"] is True
    assert result["ledger_snapshot_parse_complete"] is False
    assert result["ledger_unparseable_lines"] == 2
    assert result["complete_applied"] is False
    assert result["deleted"] == 0
    assert any("deletion authority withheld" in error for error in result["errors"])
    assert service.evidence.store.refreshable_usage_stats().current_heads == 1


def test_in_batch_identical_truth_pair_reduces_to_one_head_and_one_noop(tmp_path: Path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    # Same slot and same truth; event_id/created_at churn is arrival noise.
    twin_a = _usage_event(event_id="evt_twin_a", created_at=1_000.0)
    twin_b = _usage_event(event_id="evt_twin_b", created_at=9_999.0)

    result = _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([twin_a, twin_b], complete=True),
        inserted=1,
        noop=1,
        spool_written=1,
    )

    assert _error_count(result) == 0
    assert result["complete_applied"] is True
    assert result["input_count"] == 2
    assert result["eligible"] == 2
    stats = _stats(runtime)
    # The store saw exactly one item, so the single noop is the in-batch
    # duplicate reduction, not a store-level unchanged head.
    assert stats["logical_events"] == 1
    assert stats["evidence_versions"] == 1
    assert stats["receipts"] == 1
    refreshable = runtime.store.refreshable_usage_stats()
    assert refreshable.current_heads == 1
    assert refreshable.revisions == 1


def test_in_batch_divergent_truth_installs_higher_source_order_content(tmp_path: Path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    older_truth = _usage_event(
        event_id="evt_low_order",
        created_at=9_000.0,
        source_order=100.0,
        input_tokens=100,
    )
    newer_truth = _usage_event(
        event_id="evt_high_order",
        created_at=1_000.0,
        source_order=200.0,
        input_tokens=333,
    )

    # Older truth arrives first in the batch with a LATER Chronicle arrival
    # time: selection must come from source order alone.
    result = _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot(
            [older_truth, newer_truth],
            complete=True,
        ),
        inserted=1,
        stale=1,
        spool_written=1,
    )

    assert _error_count(result) == 0
    assert result["complete_applied"] is True
    assert result["eligible"] == 2
    heads = runtime.store.refreshable_usage_heads()
    assert len(heads) == 1
    head = heads[0]
    assert head.tombstoned is False
    newer_order = refreshable_usage_source_order(newer_truth)
    older_order = refreshable_usage_source_order(older_truth)
    assert newer_order is not None and older_order is not None
    assert newer_order > older_order
    assert head.source_order == newer_order
    records = runtime.store.query(limit=10)
    assert len(records) == 1
    envelope = records[0].envelope
    nested = envelope.payload["refreshable_usage"]
    assert nested["revision_id"] == head.current_revision_id
    assert nested["truth"]["tokens"]["input_tokens"] == {
        "reported": True,
        "value": 333,
    }
    assert envelope.payload["input_tokens"] == 333
    stats = _stats(runtime)
    assert stats["logical_events"] == 1
    assert stats["evidence_versions"] == 1


def test_in_batch_equal_order_divergence_errors_and_installs_no_head(tmp_path: Path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    left = _usage_event(
        event_id="evt_equal_left",
        created_at=1_000.0,
        source_order=100.0,
        input_tokens=100,
    )
    right = _usage_event(
        event_id="evt_equal_right",
        created_at=2_000.0,
        source_order=100.0,
        input_tokens=200,
    )

    result = _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([left, right], complete=True),
    )

    assert _error_count(result) == 1
    assert any("unordered divergent" in error for error in result["errors"])
    assert result["complete_applied"] is False
    assert result["eligible"] == 2
    assert runtime.store.refreshable_usage_stats().heads == 0
    stats = _stats(runtime)
    assert stats["logical_events"] == 0
    assert stats["evidence_versions"] == 0
    assert _spool_size(runtime) == 0


def test_legacy_pre_provenance_row_is_excluded_without_withholding_complete_authority(
    tmp_path: Path,
) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    # Pre-provenance stored shape: reserved usage_source marker, importer
    # source suffix, valid identity, but no usage_provenance key.
    legacy = _usage_event(event_id="evt_legacy_pre_provenance")
    legacy["metadata"].pop("usage_provenance")
    assert legacy["source"] == "codex-local-session-import"
    assert not is_local_usage_import_event(legacy)
    assert is_legacy_local_usage_import_shape(legacy)
    trusted = _usage_event(
        event_id="evt_trusted_other_slot",
        session_id="session-trusted",
    )

    result = _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([legacy, trusted], complete=True),
        inserted=1,
        excluded=1,
        spool_written=1,
    )

    assert _error_count(result) == 0
    assert result["complete_applied"] is True
    assert result["eligible"] == 1
    heads = runtime.store.refreshable_usage_heads()
    assert len(heads) == 1
    assert heads[0].tombstoned is False
    records = runtime.store.query(limit=10)
    assert len(records) == 1
    assert records[0].envelope.subjects.client_session_id == "session-trusted"


def test_reserved_marker_row_outside_legacy_shape_still_withholds_complete_authority(
    tmp_path: Path,
) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    forged = _usage_event(event_id="evt_reserved_not_legacy")
    forged["metadata"].pop("usage_provenance")
    # Not the frozen importer source suffix, so the legacy carve-out must not
    # apply even though the reserved usage_source marker is present.
    forged["source"] = "codex-session-import"
    assert not is_legacy_local_usage_import_shape(forged)

    result = _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([forged], complete=True),
    )

    assert _error_count(result) == 1
    assert any("reserved local usage markers" in error for error in result["errors"])
    assert result["complete_applied"] is False
    assert _count(result, "excluded") == 0
    assert runtime.store.refreshable_usage_stats().heads == 0


def test_runtime_malformed_snapshot_is_fail_open_and_cannot_delete(tmp_path: Path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    _assert_counts(
        runtime.reconcile_refreshable_usage_snapshot([_usage_event()], complete=True),
        inserted=1,
        spool_written=1,
    )

    malformed = runtime.reconcile_refreshable_usage_snapshot(
        [[]],  # type: ignore[list-item] - exercise the runtime trust boundary.
        complete=True,
    )
    payload = _assert_counts(malformed)

    assert _error_count(payload) == 1
    assert payload["complete_applied"] is False
    assert runtime.store.refreshable_usage_stats().current_heads == 1
