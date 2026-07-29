from __future__ import annotations

import fcntl
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentacct.evidence import ClaimedLink, EvidenceEnvelope, SubjectRefs, canonical_digest
from agentacct.evidence_runtime import EvidenceRuntime
from agentacct.evidence_store import (
    EVIDENCE_STORE_DIRNAME,
    EVIDENCE_SPOOL_SCHEMA_VERSION,
    REFRESHABLE_USAGE_SPOOL_FILENAME,
    REFRESHABLE_USAGE_SPOOL_SCHEMA_VERSION,
    EvidenceStore,
    RefreshableUsageItem,
)


def _evidence(
    source_event_id: str,
    *,
    timestamp: str = "2026-07-13T00:00:00Z",
    payload_value: int = 1,
    assertion: str = "observed",
    dimension: str = "tool_activity",
    source_type: str | None = None,
    source_system: str | None = None,
    event_type: str | None = None,
) -> EvidenceEnvelope:
    return EvidenceEnvelope.create(
        assertion=assertion,
        claimant="agent:codex" if assertion == "claimed" else None,
        event_type=event_type or ("tool_completed" if assertion == "observed" else "task_checkpoint"),
        source_type=source_type or ("client_hook" if assertion == "observed" else "mcp_agent_reported"),
        source_system=source_system or ("claude-code" if assertion == "observed" else "codex"),
        source_instance="workstation-a",
        source_schema="fixture.v1",
        adapter="fixture-adapter.v1",
        source_event_id=source_event_id,
        event_timestamp=timestamp,
        dimensions=(dimension,),
        measurement_basis="client_hook_observed" if assertion == "observed" else "agent_claimed",
        subjects=SubjectRefs(client_session_id="session-1", section_id="kernel"),
        payload={"value": payload_value, "tool_name": "Read"},
    )


def _refreshable_usage_item(
    slot_key: str,
    *,
    value: int,
    source_order: int | None,
    timestamp: str = "2026-07-13T00:00:00Z",
) -> RefreshableUsageItem:
    content_hash = canonical_digest({"value": value})
    revision_digest = canonical_digest(
        {"slot_key": slot_key, "content_hash": content_hash}
    ).removeprefix("sha256:")
    return RefreshableUsageItem(
        slot_key=slot_key,
        slot_identity={
            "source_namespace": "/trusted/local/client",
            "client": "codex",
            "session_id": slot_key,
            "lane": "cumulative",
            "representation": "rollout",
        },
        content_hash=content_hash,
        revision_id=f"rurev_{revision_digest}",
        source_order=source_order,
        envelope=_evidence(
            f"refreshable:{slot_key}",
            timestamp=timestamp,
            payload_value=value,
            dimension="usage",
            source_type="local_client_log",
            source_system="codex",
            event_type="usage_observed",
        ),
    )


def test_append_is_additive_and_preserves_duplicate_receipts(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    envelope = _evidence("source-1")

    first = store.append(envelope)
    duplicate = store.append(envelope)

    assert first.inserted is True
    assert duplicate.duplicate is True
    assert duplicate.evidence_id == first.evidence_id
    assert duplicate.receipt_sequence > first.receipt_sequence
    assert len(store.receipts(envelope.evidence_id)) == 2
    record = store.query()[0]
    assert record.receipt_count == 2
    assert record.duplicate_receipt_count == 1
    stats = store.stats()
    assert stats.logical_events == 1
    assert stats.evidence_versions == 1
    assert stats.receipts == 2
    assert stats.duplicate_receipts == 1

    assert store.spool_path == tmp_path / EVIDENCE_STORE_DIRNAME / "spool.jsonl"
    assert len(store.spool_path.read_text(encoding="utf-8").splitlines()) == 2
    assert not (tmp_path / "events.jsonl").exists()


def test_same_source_replayed_100_times_has_one_version_and_incremental_cursor(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    envelope = _evidence("replay-one-hundred")

    dispositions = [store.append(envelope).disposition for _ in range(100)]

    assert dispositions == ["inserted", *("duplicate" for _ in range(99))]
    assert store.stats().logical_events == 1
    assert store.stats().evidence_versions == 1
    assert store.stats().receipts == 100
    assert store.stats().duplicate_receipts == 99
    assert len(store.spool_path.read_text(encoding="utf-8").splitlines()) == 100

    # The persisted cursor is at EOF: reopening/recovering never rescans all
    # prior receipts, which is the non-timing performance invariant.
    reopened = EvidenceStore(tmp_path)
    replay = reopened.recover()
    assert replay.projected_receipts == 0
    assert replay.already_projected_receipts == 0
    assert replay.invalid_records == 0


def test_same_source_key_with_changed_content_preserves_conflict_versions(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    original = _evidence("source-conflict", payload_value=1)
    corrected = _evidence("source-conflict", payload_value=2)

    assert original.idempotency_key == corrected.idempotency_key
    assert store.append(original).disposition == "inserted"
    conflict = store.append(corrected)

    assert conflict.disposition == "conflict"
    assert set(conflict.conflict_evidence_ids) == {original.evidence_id, corrected.evidence_id}
    versions = store.conflicts(idempotency_key=original.idempotency_key)
    assert {record.evidence_id for record in versions} == {original.evidence_id, corrected.evidence_id}
    assert all(record.is_conflict for record in versions)
    assert store.get(original.evidence_id) == original
    assert store.get(corrected.evidence_id) == corrected
    assert store.stats().conflict_groups == 1
    assert store.stats().conflict_versions == 2


def test_refreshable_usage_unchanged_refresh_is_a_physical_noop(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    initial = _refreshable_usage_item("slot-a", value=10, source_order=1)
    reminted_same_content = _refreshable_usage_item(
        "slot-a",
        value=10,
        source_order=1,
        timestamp="2026-07-13T00:01:00Z",
    )

    inserted = store.reconcile_refreshable_usage((initial,))
    before_refreshable = store.refreshable_usage_stats()
    before_generic = store.stats()
    before_heads = store.refreshable_usage_heads()

    unchanged = store.reconcile_refreshable_usage((reminted_same_content,))

    assert inserted.inserted == 1
    assert inserted.transition_count == 1
    assert unchanged.receipt_id is None
    assert unchanged.unchanged == 1
    assert unchanged.transition_count == 0
    assert unchanged.changed is False
    assert store.refreshable_usage_stats() == before_refreshable
    assert store.stats() == before_generic
    assert store.refreshable_usage_heads() == before_heads
    assert len(store.refreshable_usage_spool_path.read_text(encoding="utf-8").splitlines()) == 1
    assert not store.spool_path.exists()


def test_refreshable_usage_spool_is_downgrade_isolated_and_both_spools_rebuild(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    generic = _evidence("legacy-main-spool-a")
    later_generic = _evidence("legacy-main-spool-b")
    item = _refreshable_usage_item("slot-a", value=10, source_order=1)

    store.append(generic)
    store.reconcile_refreshable_usage((item,))
    store.append(later_generic)
    arrival_before_rebuild = [
        record.evidence_id for record in store.query(order_by="arrival")
    ]
    assert arrival_before_rebuild == [
        generic.evidence_id,
        item.envelope.evidence_id,
        later_generic.evidence_id,
    ]

    assert store.refreshable_usage_spool_path == (
        tmp_path / EVIDENCE_STORE_DIRNAME / REFRESHABLE_USAGE_SPOOL_FILENAME
    )
    main_lines = store.spool_path.read_bytes().splitlines(keepends=True)
    main_records = [json.loads(line) for line in main_lines]
    refreshable_records = [
        json.loads(line)
        for line in store.refreshable_usage_spool_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [(record["spool_schema_version"], record["kind"]) for record in main_records] == [
        (EVIDENCE_SPOOL_SCHEMA_VERSION, "evidence"),
        (EVIDENCE_SPOOL_SCHEMA_VERSION, "evidence"),
    ]
    assert [
        (record["spool_schema_version"], record["kind"])
        for record in refreshable_records
    ] == [(REFRESHABLE_USAGE_SPOOL_SCHEMA_VERSION, "refreshable_usage")]
    assert refreshable_records[0]["main_spool_fence"] == len(main_lines[0])
    assert store.stats().spool_bytes == store.spool_path.stat().st_size
    assert (
        store.refreshable_usage_stats().spool_bytes
        == store.refreshable_usage_spool_path.stat().st_size
    )

    projection = store.projection_path
    projection.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{projection}{suffix}")
        if sidecar.exists():
            sidecar.unlink()

    rebuilt = EvidenceStore(tmp_path)
    assert rebuilt.get(generic.evidence_id) == generic
    assert rebuilt.get(later_generic.evidence_id) == later_generic
    assert rebuilt.refreshable_usage_heads()[0].last_revision_id == item.revision_id
    assert [record.evidence_id for record in rebuilt.query(order_by="arrival")] == arrival_before_rebuild
    assert rebuilt.stats().evidence_versions == 3
    assert rebuilt.stats().receipts == 3
    assert rebuilt.refreshable_usage_stats().batch_receipts == 1
    assert rebuilt.recover().projected_receipts == 0


def test_refreshable_usage_newer_same_content_advances_only_durable_watermark(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    initial = _refreshable_usage_item("slot-a", value=10, source_order=1)
    newer_same_content = _refreshable_usage_item(
        "slot-a",
        value=10,
        source_order=100,
        timestamp="2026-07-13T00:01:00Z",
    )
    older_divergence = _refreshable_usage_item(
        "slot-a",
        value=20,
        source_order=50,
        timestamp="2026-07-13T00:02:00Z",
    )

    store.reconcile_refreshable_usage((initial,))
    evidence_before = store.stats()
    refreshable_before = store.refreshable_usage_stats()
    watermarked = store.reconcile_refreshable_usage((newer_same_content,))

    assert watermarked.watermarked == 1
    assert watermarked.transition_count == 1
    evidence_after = store.stats()
    assert evidence_after == evidence_before
    assert store.refreshable_usage_stats().spool_bytes > refreshable_before.spool_bytes
    assert store.refreshable_usage_heads()[0].source_order == 100
    assert store.refreshable_usage_stats().revisions == 1
    assert store.refreshable_usage_stats().batch_receipts == 2
    assert store.refreshable_usage_stats().transitions == 2
    assert len(store.refreshable_usage_spool_path.read_text(encoding="utf-8").splitlines()) == 2
    with store._connection() as connection:
        revision_order = connection.execute(
            "SELECT source_order FROM refreshable_usage_revisions WHERE revision_id = ?",
            (initial.revision_id,),
        ).fetchone()
    assert revision_order is not None
    assert revision_order["source_order"] == 100

    before_stale = store.refreshable_usage_stats()
    stale = store.reconcile_refreshable_usage((older_divergence,))
    assert stale.receipt_id is None
    assert stale.stale == 1
    assert store.refreshable_usage_stats() == before_stale
    assert store.refreshable_usage_heads()[0].content_hash == initial.content_hash

    projection = store.projection_path
    projection.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{projection}{suffix}")
        if sidecar.exists():
            sidecar.unlink()
    rebuilt = EvidenceStore(tmp_path)
    assert rebuilt.refreshable_usage_heads()[0].source_order == 100
    rebuilt_stats = rebuilt.stats()
    assert rebuilt_stats == evidence_before
    assert rebuilt.refreshable_usage_stats().spool_bytes == before_stale.spool_bytes


def test_refreshable_usage_rejects_identity_drift_before_spool(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    initial = _refreshable_usage_item("slot-a", value=10, source_order=1)
    store.reconcile_refreshable_usage((initial,))
    before = (store.refreshable_usage_stats(), store.stats())

    changed_identity = replace(
        initial,
        slot_identity={**initial.slot_identity, "source_namespace": "/other/home"},
    )
    changed_revision = replace(initial, revision_id="rurev_" + "f" * 64)
    changed_envelope = replace(
        initial,
        envelope=_evidence(
            "unstable-source-id",
            timestamp="2026-07-13T00:01:00Z",
            payload_value=10,
            dimension="usage",
            source_type="local_client_log",
            source_system="codex",
            event_type="usage_observed",
        ),
    )

    with pytest.raises(ValueError, match="slot identity changed"):
        store.reconcile_refreshable_usage((changed_identity,))
    with pytest.raises(ValueError, match="content/revision identity mismatch"):
        store.reconcile_refreshable_usage((changed_revision,))
    with pytest.raises(ValueError, match="envelope identity changed"):
        store.reconcile_refreshable_usage((changed_envelope,))
    assert (store.refreshable_usage_stats(), store.stats()) == before


def test_refreshable_usage_newer_update_supersedes_and_stale_does_not_grow(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    initial = _refreshable_usage_item("slot-a", value=10, source_order=1)
    newer = _refreshable_usage_item(
        "slot-a",
        value=20,
        source_order=2,
        timestamp="2026-07-13T00:01:00Z",
    )
    stale = _refreshable_usage_item(
        "slot-a",
        value=30,
        source_order=1,
        timestamp="2026-07-13T00:02:00Z",
    )

    store.reconcile_refreshable_usage((initial,))
    updated = store.reconcile_refreshable_usage((newer,))
    before_stale = store.refreshable_usage_stats()
    stale_result = store.reconcile_refreshable_usage((stale,))

    assert initial.envelope.idempotency_key == newer.envelope.idempotency_key
    assert updated.updated == 1
    assert stale_result.receipt_id is None
    assert stale_result.stale == 1
    assert store.refreshable_usage_stats() == before_stale
    stats = store.refreshable_usage_stats()
    assert stats.heads == 1
    assert stats.revisions == 2
    assert stats.current_revisions == 1
    assert stats.superseded_revisions == 1
    head = store.refreshable_usage_heads()[0]
    assert head.content_hash == newer.content_hash
    assert head.source_order == 2
    assert head.evidence_id == newer.envelope.evidence_id
    versions = store.query(idempotency_key=initial.envelope.idempotency_key)
    assert {record.evidence_id for record in versions} == {
        initial.envelope.evidence_id,
        newer.envelope.evidence_id,
    }
    assert all(not record.is_conflict for record in versions)


@pytest.mark.parametrize(
    ("initial_order", "candidate_order", "repeated_order"),
    [(5, 5, 5), (None, 1, 2)],
)
def test_refreshable_usage_tie_or_unordered_conflict_is_stable(
    tmp_path: Path,
    initial_order: int | None,
    candidate_order: int | None,
    repeated_order: int | None,
) -> None:
    store = EvidenceStore(tmp_path)
    initial = _refreshable_usage_item("slot-a", value=10, source_order=initial_order)
    first_candidate = _refreshable_usage_item(
        "slot-a",
        value=20,
        source_order=candidate_order,
        timestamp="2026-07-13T00:01:00Z",
    )
    reminted_candidate = _refreshable_usage_item(
        "slot-a",
        value=20,
        source_order=repeated_order,
        timestamp="2026-07-13T00:02:00Z",
    )

    store.reconcile_refreshable_usage((initial,))
    conflict = store.reconcile_refreshable_usage((first_candidate,))
    before_repeat = store.refreshable_usage_stats()
    generic_before_repeat = store.stats()
    repeated = store.reconcile_refreshable_usage((reminted_candidate,))

    assert conflict.conflicts == 1
    assert repeated.receipt_id is None
    assert repeated.existing_conflicts == 1
    assert store.refreshable_usage_stats() == before_repeat
    assert store.stats() == generic_before_repeat
    assert store.refreshable_usage_stats().conflicts == 1
    assert store.stats().evidence_versions == 2
    assert store.stats().receipts == 2
    assert store.stats().conflict_groups == 1
    assert store.stats().conflict_versions == 2
    assert {record.evidence_id for record in store.conflicts()} == {
        initial.envelope.evidence_id,
        first_candidate.envelope.evidence_id,
    }

    projection = store.projection_path
    projection.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{projection}{suffix}")
        if sidecar.exists():
            sidecar.unlink()
    rebuilt = EvidenceStore(tmp_path)
    assert rebuilt.refreshable_usage_heads()[0].last_revision_id == initial.revision_id
    assert rebuilt.refreshable_usage_stats().conflicts == 1
    assert rebuilt.stats().conflict_versions == 2
    rebuilt_before_repeat = (rebuilt.refreshable_usage_stats(), rebuilt.stats())
    rebuilt_repeat = rebuilt.reconcile_refreshable_usage((reminted_candidate,))
    assert rebuilt_repeat.receipt_id is None
    assert rebuilt_repeat.existing_conflicts == 1
    assert (rebuilt.refreshable_usage_stats(), rebuilt.stats()) == rebuilt_before_repeat


def test_refreshable_usage_complete_tombstones_partial_does_not_and_newer_reappears(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    first = _refreshable_usage_item("slot-a", value=10, source_order=1)
    second = _refreshable_usage_item("slot-b", value=20, source_order=1)

    store.reconcile_refreshable_usage((first, second), complete=True)
    partial = store.reconcile_refreshable_usage((first,), complete=False)
    assert partial.unchanged == 1
    assert store.refreshable_usage_stats().current_heads == 2

    deleted = store.reconcile_refreshable_usage((first,), complete=True)
    assert deleted.tombstoned == 1
    assert store.refreshable_usage_stats().current_heads == 1
    assert store.refreshable_usage_stats().tombstoned_heads == 1
    assert [head.slot_key for head in store.refreshable_usage_heads(include_tombstoned=False)] == ["slot-a"]

    same_order = store.reconcile_refreshable_usage((first, second), complete=False)
    assert same_order.receipt_id is None
    assert same_order.unchanged == 2
    assert store.refreshable_usage_stats().tombstoned_heads == 1

    reappeared = _refreshable_usage_item(
        "slot-b",
        value=20,
        source_order=2,
        timestamp="2026-07-13T00:03:00Z",
    )
    resurrected = store.reconcile_refreshable_usage((first, reappeared), complete=False)

    assert resurrected.resurrected == 1
    assert resurrected.unchanged == 1
    stats = store.refreshable_usage_stats()
    assert stats.heads == 2
    assert stats.current_heads == 2
    assert stats.tombstoned_heads == 0
    assert stats.revisions == 2
    assert stats.current_revisions == 2
    assert stats.superseded_revisions == 0
    assert store.stats().evidence_versions == 3
    assert {head.slot_key: head.source_order for head in store.refreshable_usage_heads()} == {
        "slot-a": 1,
        "slot-b": 2,
    }
    assert {head.slot_key: head.last_revision_id for head in store.refreshable_usage_heads()} == {
        "slot-a": first.revision_id,
        "slot-b": second.revision_id,
    }
    with store._connection() as connection:
        second_revision = connection.execute(
            "SELECT source_order FROM refreshable_usage_revisions WHERE revision_id = ?",
            (second.revision_id,),
        ).fetchone()
    assert second_revision is not None
    assert second_revision["source_order"] == 2


def test_refreshable_usage_complete_same_truth_resurrects_tombstoned_head(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path)
    item = _refreshable_usage_item("slot-a", value=10, source_order=1)
    store.reconcile_refreshable_usage((item,), complete=True)
    store.reconcile_refreshable_usage((), complete=True)
    assert store.refreshable_usage_stats().tombstoned_heads == 1

    resurrected = store.reconcile_refreshable_usage((item,), complete=True)

    assert resurrected.resurrected == 1
    assert resurrected.transition_count == 1
    head = store.refreshable_usage_heads()[0]
    assert head.tombstoned is False
    assert head.current_revision_id == item.revision_id
    stats = store.refreshable_usage_stats()
    assert stats.current_heads == 1
    assert stats.tombstoned_heads == 0
    assert stats.revisions == 1
    assert stats.current_revisions == 1
    assert stats.superseded_revisions == 0

    before_repeat = (store.refreshable_usage_stats(), store.stats())
    spool_size = store.refreshable_usage_spool_path.stat().st_size
    repeated = store.reconcile_refreshable_usage((item,), complete=True)
    assert repeated.receipt_id is None
    assert repeated.unchanged == 1
    assert (store.refreshable_usage_stats(), store.stats()) == before_repeat
    assert store.refreshable_usage_spool_path.stat().st_size == spool_size


def test_refreshable_usage_tombstoned_head_resurrects_with_changed_newer_content(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    original = _refreshable_usage_item("slot-a", value=10, source_order=1)
    store.reconcile_refreshable_usage((original,), complete=True)
    tombstoned = store.reconcile_refreshable_usage((), complete=True)
    assert tombstoned.tombstoned == 1
    assert store.refreshable_usage_stats().tombstoned_heads == 1

    changed = _refreshable_usage_item(
        "slot-a",
        value=20,
        source_order=2,
        timestamp="2026-07-13T00:03:00Z",
    )
    resurrected = store.reconcile_refreshable_usage((changed,))

    assert resurrected.resurrected == 1
    assert resurrected.transition_count == 1
    head = store.refreshable_usage_heads()[0]
    assert head.tombstoned is False
    assert head.content_hash == changed.content_hash
    assert head.current_revision_id == changed.revision_id
    assert head.last_revision_id == changed.revision_id
    assert head.source_order == 2
    assert head.evidence_id == changed.envelope.evidence_id
    stats = store.refreshable_usage_stats()
    assert stats.current_heads == 1
    assert stats.tombstoned_heads == 0
    assert stats.revisions == 2
    assert stats.current_revisions == 1
    assert stats.superseded_revisions == 1
    heads_before = store.refreshable_usage_heads()

    projection = store.projection_path
    projection.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{projection}{suffix}")
        if sidecar.exists():
            sidecar.unlink()
    rebuilt = EvidenceStore(tmp_path)
    assert rebuilt.refreshable_usage_heads() == heads_before
    assert rebuilt.refreshable_usage_stats() == stats
    assert rebuilt.recover().projected_receipts == 0


def test_refreshable_usage_tombstoned_head_unordered_divergence_is_a_tombstoned_conflict(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    original = _refreshable_usage_item("slot-a", value=10, source_order=1)
    store.reconcile_refreshable_usage((original,), complete=True)
    store.reconcile_refreshable_usage((), complete=True)

    divergent = _refreshable_usage_item(
        "slot-a",
        value=20,
        source_order=None,
        timestamp="2026-07-13T00:03:00Z",
    )
    conflicted = store.reconcile_refreshable_usage((divergent,))

    assert conflicted.conflicts == 1
    head = store.refreshable_usage_heads()[0]
    assert head.tombstoned is True
    assert head.last_revision_id == original.revision_id
    with store._connection() as connection:
        conflict_rows = connection.execute(
            "SELECT * FROM refreshable_usage_conflicts"
        ).fetchall()
    assert len(conflict_rows) == 1
    conflict_row = dict(conflict_rows[0])
    assert conflict_row["slot_key"] == "slot-a"
    assert conflict_row["head_tombstoned"] == 1
    assert conflict_row["current_revision_id"] == original.revision_id
    assert conflict_row["candidate_revision_id"] == divergent.revision_id
    assert conflict_row["candidate_content_hash"] == divergent.content_hash
    assert conflict_row["candidate_source_order"] is None
    stats = store.refreshable_usage_stats()
    assert stats.conflicts == 1
    assert stats.tombstoned_heads == 1
    assert store.stats().conflict_versions == 2
    heads_before = store.refreshable_usage_heads()

    projection = store.projection_path
    projection.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{projection}{suffix}")
        if sidecar.exists():
            sidecar.unlink()
    rebuilt = EvidenceStore(tmp_path)
    assert rebuilt.refreshable_usage_heads() == heads_before
    assert rebuilt.refreshable_usage_stats() == stats
    with rebuilt._connection() as connection:
        rebuilt_rows = connection.execute(
            "SELECT * FROM refreshable_usage_conflicts"
        ).fetchall()
    assert [dict(row) for row in rebuilt_rows] == [conflict_row]
    assert rebuilt.recover().projected_receipts == 0


def test_refreshable_usage_two_instances_converge_before_spool_append(tmp_path: Path) -> None:
    item = _refreshable_usage_item("slot-a", value=10, source_order=1)
    stores = [EvidenceStore(tmp_path), EvidenceStore(tmp_path)]

    first = stores[0].reconcile_refreshable_usage((item,))
    second = stores[1].reconcile_refreshable_usage_snapshot((item,))

    assert first.inserted == 1
    assert second.receipt_id is None
    assert second.unchanged == 1
    assert stores[0].refreshable_usage_stats().batch_receipts == 1
    assert stores[1].refreshable_usage_stats().transitions == 1
    assert len(stores[0].refreshable_usage_spool_path.read_text(encoding="utf-8").splitlines()) == 1
    assert not stores[0].spool_path.exists()


def test_refreshable_usage_fsynced_batch_recovers_and_rebuilds_heads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EvidenceStore(tmp_path)
    item = _refreshable_usage_item("slot-a", value=10, source_order=1)
    second_item = _refreshable_usage_item("slot-b", value=20, source_order=1)

    def fail_projection(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated refreshable projection crash")

    monkeypatch.setattr(store, "_project_refreshable_usage_record", fail_projection)
    with pytest.raises(RuntimeError, match="simulated refreshable projection crash"):
        store.reconcile_refreshable_usage((item, second_item))

    assert len(store.refreshable_usage_spool_path.read_text(encoding="utf-8").splitlines()) == 1
    assert not store.spool_path.exists()
    assert store.refreshable_usage_stats().heads == 0
    assert store.stats().evidence_versions == 0

    recovered = EvidenceStore(tmp_path)
    assert {head.evidence_id for head in recovered.refreshable_usage_heads()} == {
        item.envelope.evidence_id,
        second_item.envelope.evidence_id,
    }
    assert {head.last_revision_id for head in recovered.refreshable_usage_heads()} == {
        item.revision_id,
        second_item.revision_id,
    }
    assert recovered.refreshable_usage_stats().revisions == 2
    assert recovered.refreshable_usage_stats().batch_receipts == 1
    assert recovered.refreshable_usage_stats().transitions == 2
    assert recovered.stats().evidence_versions == 2
    assert recovered.stats().receipts == 2

    projection = recovered.projection_path
    projection.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{projection}{suffix}")
        if sidecar.exists():
            sidecar.unlink()

    rebuilt = EvidenceStore(tmp_path)
    assert rebuilt.refreshable_usage_heads() == recovered.refreshable_usage_heads()
    assert {head.last_revision_id for head in rebuilt.refreshable_usage_heads()} == {
        item.revision_id,
        second_item.revision_id,
    }
    assert rebuilt.refreshable_usage_stats().revisions == 2
    assert rebuilt.stats().evidence_versions == 2
    assert rebuilt.recover().projected_receipts == 0


def test_refreshable_usage_pending_projection_is_recovered_before_later_main_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = EvidenceStore(tmp_path)
    generic = _evidence("legacy-before-refresh-crash")
    later_generic = _evidence("legacy-after-refresh-crash")
    item = _refreshable_usage_item("slot-a", value=10, source_order=1)
    store.append(generic)

    original_projector = store._project_refreshable_usage_record

    def fail_projection(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated refreshable projection crash")

    monkeypatch.setattr(store, "_project_refreshable_usage_record", fail_projection)
    with pytest.raises(RuntimeError, match="simulated refreshable projection crash"):
        store.reconcile_refreshable_usage((item,))
    monkeypatch.setattr(store, "_project_refreshable_usage_record", original_projector)

    assert store.get(item.envelope.evidence_id) is None
    store.append(later_generic)
    expected_arrival = [
        generic.evidence_id,
        item.envelope.evidence_id,
        later_generic.evidence_id,
    ]
    assert [record.evidence_id for record in store.query(order_by="arrival")] == expected_arrival
    assert store.refreshable_usage_heads()[0].last_revision_id == item.revision_id

    projection = store.projection_path
    projection.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{projection}{suffix}")
        if sidecar.exists():
            sidecar.unlink()

    rebuilt = EvidenceStore(tmp_path)
    assert [record.evidence_id for record in rebuilt.query(order_by="arrival")] == expected_arrival
    assert rebuilt.refreshable_usage_heads()[0].last_revision_id == item.revision_id
    assert rebuilt.recover().projected_receipts == 0


def test_refreshable_usage_fence_normalizes_torn_invalid_main_boundary_and_rebuilds(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path)
    store.spool_path.write_bytes(b'{"torn":')

    replay = store.recover()
    assert replay.invalid_records == 1
    assert store.stats().invalid_spool_records == 1

    item = _refreshable_usage_item("slot-a", value=10, source_order=1)
    later_generic = _evidence("legacy-after-invalid-fence")
    store.reconcile_refreshable_usage((item,))
    store.append(later_generic)

    normalized_invalid_line = b'{"torn":\n'
    assert store.spool_path.read_bytes().startswith(normalized_invalid_line)
    refreshable_record = json.loads(
        store.refreshable_usage_spool_path.read_text(encoding="utf-8").splitlines()[0]
    )
    assert refreshable_record["main_spool_fence"] == len(normalized_invalid_line)
    expected_arrival = [item.envelope.evidence_id, later_generic.evidence_id]
    assert [record.evidence_id for record in store.query(order_by="arrival")] == expected_arrival

    projection = store.projection_path
    projection.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{projection}{suffix}")
        if sidecar.exists():
            sidecar.unlink()

    rebuilt = EvidenceStore(tmp_path)
    assert [record.evidence_id for record in rebuilt.query(order_by="arrival")] == expected_arrival
    assert rebuilt.stats().invalid_spool_records == 1
    assert rebuilt.refreshable_usage_heads()[0].last_revision_id == item.revision_id
    assert rebuilt.recover().projected_receipts == 0


def _interleaved_fenced_store(tmp_path: Path) -> tuple[EvidenceStore, int]:
    # Interleave both spools so one refreshable fence is recorded, then delete
    # the projection so the next open must replay across that fence.
    store = EvidenceStore(tmp_path)
    store.append(_evidence("fence-before"))
    store.reconcile_refreshable_usage(
        (_refreshable_usage_item("slot-a", value=10, source_order=1),)
    )
    store.append(_evidence("fence-after"))
    fence = json.loads(
        store.refreshable_usage_spool_path.read_text(encoding="utf-8").splitlines()[0]
    )["main_spool_fence"]
    assert fence == len(store.spool_path.read_bytes().splitlines(keepends=True)[0])

    projection = store.projection_path
    projection.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{projection}{suffix}")
        if sidecar.exists():
            sidecar.unlink()
    return store, fence


def test_replay_fails_closed_when_fence_requires_truncated_legacy_bytes(tmp_path: Path) -> None:
    store, fence = _interleaved_fenced_store(tmp_path)
    store.spool_path.write_bytes(store.spool_path.read_bytes()[: fence - 1])

    with pytest.raises(
        RuntimeError,
        match="refreshable usage spool requires missing legacy spool bytes",
    ):
        EvidenceStore(tmp_path)


def test_replay_fails_closed_when_fenced_legacy_spool_is_deleted(tmp_path: Path) -> None:
    store, fence = _interleaved_fenced_store(tmp_path)
    assert fence > 0
    store.spool_path.unlink()

    with pytest.raises(
        RuntimeError,
        match="refreshable usage spool requires missing legacy spool bytes",
    ):
        EvidenceStore(tmp_path)


def test_replay_fails_closed_when_fence_lands_mid_record(tmp_path: Path) -> None:
    store, fence = _interleaved_fenced_store(tmp_path)
    raw = store.spool_path.read_bytes()
    # Drop the newline at the fence so the two legacy records become one line.
    merged = raw[: fence - 1] + raw[fence:]
    assert len(merged) >= fence
    store.spool_path.write_bytes(merged)

    with pytest.raises(
        RuntimeError,
        match="refreshable usage spool fence is not a legacy record boundary",
    ):
        EvidenceStore(tmp_path)


def test_out_of_order_arrival_is_preserved_and_query_order_is_explicit(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    late_arrival = _evidence("later-event", timestamp="2026-07-13T02:00:00Z")
    early_arrival = _evidence("earlier-event", timestamp="2026-07-13T01:00:00Z")
    store.append(late_arrival)
    store.append(early_arrival)

    by_event_time = store.query(order_by="event_time")
    by_arrival = store.query(order_by="arrival")
    assert [row.evidence_id for row in by_event_time] == [early_arrival.evidence_id, late_arrival.evidence_id]
    assert [row.evidence_id for row in by_arrival] == [late_arrival.evidence_id, early_arrival.evidence_id]
    assert by_arrival[0].first_receipt_sequence < by_arrival[1].first_receipt_sequence


def test_arrival_cursor_pages_descending_without_repeats_or_omissions(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    first = _evidence("cursor-first")
    conflicted_original = _evidence("cursor-conflict", payload_value=1)
    conflicted_correction = _evidence("cursor-conflict", payload_value=2)
    middle = _evidence("cursor-middle")
    latest = _evidence("cursor-latest")
    filtered_out = _evidence("cursor-filtered", dimension="usage")

    store.append(first)
    store.append(conflicted_original)
    store.append(first)  # A later duplicate must not change first-arrival order.
    store.append(conflicted_correction)
    store.append(middle)
    store.append(filtered_out)
    store.append(conflicted_correction)
    store.append(latest)

    filters = {
        "source_type": "client_hook",
        "dimension": "tool_activity",
        "client_session_id": "session-1",
        "order_by": "arrival",
        "descending": True,
    }
    expected = store.query(**filters)
    assert len(expected) == 5
    assert any(record.duplicate_receipt_count == 1 for record in expected)
    assert sum(record.is_conflict for record in expected) == 2

    traversed = []
    cursor = None
    while True:
        page = store.query(**filters, arrival_before_sequence=cursor, limit=2)
        if not page:
            break
        traversed.extend(page)
        cursor = page[-1].first_receipt_sequence

    expected_ids = [record.evidence_id for record in expected]
    traversed_ids = [record.evidence_id for record in traversed]
    assert traversed_ids == expected_ids
    assert len(traversed_ids) == len(set(traversed_ids))
    assert store.query(**filters, arrival_before_sequence=0, limit=2) == []


@pytest.mark.parametrize("cursor", [-1, True, 1.5, "1"])
def test_arrival_cursor_rejects_invalid_values(tmp_path: Path, cursor: object) -> None:
    store = EvidenceStore(tmp_path)

    with pytest.raises(ValueError, match="arrival_before_sequence must be a non-negative integer"):
        store.query(order_by="arrival", arrival_before_sequence=cursor)  # type: ignore[arg-type]


def test_arrival_cursor_requires_arrival_order(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)

    with pytest.raises(ValueError, match="arrival_before_sequence requires order_by='arrival'"):
        store.query(arrival_before_sequence=1)


def test_ack_and_replay_are_scoped_per_consumer(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    first = _evidence("ack-1")
    second = _evidence("ack-2")
    first_result, second_result = store.append_many((first, second))

    assert [record.evidence_id for record in store.replay(consumer="projection-a")] == [first.evidence_id, second.evidence_id]
    assert store.ack(first.evidence_id, consumer="projection-a") is True
    assert [record.evidence_id for record in store.replay(consumer="projection-a")] == [second.evidence_id]
    assert [record.evidence_id for record in store.replay(consumer="projection-b")] == [first.evidence_id, second.evidence_id]
    assert store.ack("evd_" + "0" * 64, consumer="projection-a") is False

    after_first = store.replay(
        consumer="projection-b",
        after_sequence=first_result.receipt_sequence,
        include_acknowledged=True,
    )
    assert [record.evidence_id for record in after_first] == [second.evidence_id]
    assert second_result.receipt_sequence > first_result.receipt_sequence
    assert store.stats().acknowledgements == 1


def test_query_uses_indexable_source_dimension_subject_and_time_filters(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    target = _evidence("query-target", timestamp="2026-07-13T01:00:00Z")
    other = EvidenceEnvelope.create(
        assertion="observed",
        event_type="usage_observed",
        source_type="local_client_log",
        source_system="codex",
        source_instance="workstation-a",
        source_schema="codex-rollout.v1",
        adapter="codex-usage.v1",
        source_event_id="query-other",
        event_timestamp="2026-07-13T02:00:00Z",
        dimensions=("usage",),
        measurement_basis="client_reported",
        subjects=SubjectRefs(client_session_id="session-2", work_id="other-work"),
        payload={"input_tokens": 7},
    )
    store.append_many((target, other))

    assert [row.evidence_id for row in store.query(source_type="client_hook")] == [target.evidence_id]
    assert [row.evidence_id for row in store.query(dimension="usage")] == [other.evidence_id]
    assert [row.evidence_id for row in store.query(client_session_id="session-1")] == [target.evidence_id]
    assert [row.evidence_id for row in store.query(event_at_or_after="2026-07-13T01:30:00Z")] == [other.evidence_id]


def test_recent_source_is_arrival_descending_and_aggregates_only_after_limit(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    oldest = _evidence("recent-oldest")
    ignored = _evidence("recent-ignored", assertion="claimed")
    middle = _evidence("recent-middle")
    newest = _evidence("recent-newest")

    store.append(oldest)
    store.append(ignored)
    store.append(middle)
    store.append(newest)
    store.append(newest)
    # A duplicate is a new receipt, not a new evidence-version arrival.  It
    # must enrich the selected version without moving an old version into the
    # recent window.
    store.append(oldest)

    rows = store.query_recent_source(source_type="client_hook", limit=2)

    assert [row.evidence_id for row in rows] == [newest.evidence_id, middle.evidence_id]
    assert rows[0].receipt_count == 2
    assert rows[0].duplicate_receipt_count == 1
    assert rows[1].receipt_count == 1
    assert rows[0].first_receipt_sequence > rows[1].first_receipt_sequence


def test_recent_source_applies_source_filters_before_limit_and_preserves_conflicts(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    expected_old = _evidence("recent-filter-old")
    wrong_system = _evidence("recent-filter-system", source_system="cursor")
    wrong_assertion = _evidence(
        "recent-filter-assertion",
        assertion="claimed",
        source_type="client_hook",
        source_system="claude-code",
        event_type="tool_completed",
    )
    expected_original = _evidence("recent-filter-conflict", payload_value=1)
    expected_conflict = _evidence("recent-filter-conflict", payload_value=2)
    wrong_event = _evidence("recent-filter-event", event_type="session_started")
    store.append_many(
        (
            expected_old,
            wrong_system,
            wrong_assertion,
            expected_original,
            expected_conflict,
            wrong_event,
        )
    )
    store.acknowledge(expected_conflict.evidence_id, consumer="dashboard")

    rows = store.query_recent_source(
        source_type="client_hook",
        source_system="claude-code",
        assertion="observed",
        event_type="tool_completed",
        consumer="dashboard",
        limit=2,
    )

    assert [row.evidence_id for row in rows] == [expected_conflict.evidence_id, expected_original.evidence_id]
    assert all(row.is_conflict for row in rows)
    assert [row.acknowledged for row in rows] == [True, False]


def test_recent_source_query_plan_limits_with_arrival_index_before_receipt_lookup(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    sql, params = store._recent_source_query(
        source_type="client_hook",
        source_system=None,
        assertion=None,
        event_type=None,
        consumer="",
        limit=10_000,
    )

    with store._connection() as connection:
        plan_rows = connection.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    plan = " | ".join(str(row["detail"]) for row in plan_rows)

    assert "MATERIALIZE recent_evidence" in plan
    assert "idx_evidence_source_arrival" in plan
    assert "MATERIALIZE receipt_totals" in plan
    assert "idx_receipts_evidence" in plan


def test_general_query_plan_limits_versions_before_receipt_lookup(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    sql = store._bounded_query_sql(
        where_sql="",
        order_column="e.event_timestamp",
        direction="ASC",
    )

    with store._connection() as connection:
        plan_rows = connection.execute(f"EXPLAIN QUERY PLAN {sql}", (10_000, "")).fetchall()
    plan = " | ".join(str(row["detail"]) for row in plan_rows)

    assert "MATERIALIZE bounded_evidence" in plan
    assert "idx_evidence_time" in plan
    assert "MATERIALIZE receipt_totals" in plan
    assert "idx_receipts_evidence" in plan


@pytest.mark.parametrize("limit", [0, 10_001, True, 1.5])
def test_recent_source_rejects_invalid_limits(tmp_path: Path, limit: object) -> None:
    store = EvidenceStore(tmp_path)

    with pytest.raises(ValueError, match="limit must be between 1 and 10000"):
        store.query_recent_source(source_type="client_hook", limit=limit)  # type: ignore[arg-type]


def test_recent_runtime_returns_records_and_envelopes_from_the_bounded_path(tmp_path: Path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    older = _evidence("runtime-recent-older")
    newer = _evidence("runtime-recent-newer")
    runtime.append(older)
    runtime.append(newer)

    records = runtime.recent_records(source_type="client_hook", limit=1)
    envelopes = runtime.recent_envelopes(source_type="client_hook", limit=1)

    assert [record.evidence_id for record in records] == [newer.evidence_id]
    assert records[0].first_receipt_sequence > 0
    assert envelopes == [newer]
    assert EvidenceRuntime(tmp_path / "disabled", enabled=False).recent_records(source_type="client_hook") == []


def test_fsynced_spool_receipt_recovers_after_projection_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EvidenceStore(tmp_path)
    envelope = _evidence("crash-window")

    def fail_projection(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated crash after fsync")

    monkeypatch.setattr(store, "_project_evidence_record", fail_projection)
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.append(envelope)

    assert store.spool_path.is_file()
    assert len(store.spool_path.read_text(encoding="utf-8").splitlines()) == 1
    assert store.get(envelope.evidence_id) is None

    reopened = EvidenceStore(tmp_path)
    assert reopened.get(envelope.evidence_id) == envelope
    assert reopened.stats().receipts == 1
    replay = reopened.recover()
    assert replay.projected_receipts == 0
    assert replay.already_projected_receipts == 0


def test_projection_can_be_rebuilt_only_from_append_only_spool(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    envelopes = (_evidence("rebuild-1"), _evidence("rebuild-2"))
    store.append_many(envelopes)
    projection = store.projection_path

    projection.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{projection}{suffix}")
        if sidecar.exists():
            sidecar.unlink()

    rebuilt = EvidenceStore(tmp_path)
    assert {row.evidence_id for row in rebuilt.query()} == {envelope.evidence_id for envelope in envelopes}
    assert rebuilt.stats().receipts == 2
    assert len(rebuilt.spool_path.read_text(encoding="utf-8").splitlines()) == 2


def test_torn_spool_tail_is_preserved_counted_and_does_not_block_new_receipts(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    store.spool_path.write_bytes(b'{"torn":')

    replay = store.recover()
    assert replay.invalid_records == 1
    assert store.stats().invalid_spool_records == 1

    envelope = _evidence("after-torn-tail")
    result = store.append(envelope)
    assert result.inserted is True
    raw = store.spool_path.read_bytes()
    assert raw.startswith(b'{"torn":\n')
    assert store.get(envelope.evidence_id) == envelope
    assert store.stats().invalid_spool_records == 1


def test_tampered_spool_record_is_rejected_without_losing_bytes(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    envelope = _evidence("tampered-spool")
    record = store._spool_record(kind="evidence", payload=envelope.to_dict())
    record["payload"]["payload"]["value"] = 99
    original_bytes = json.dumps(record, sort_keys=True).encode("utf-8") + b"\n"
    store.spool_path.write_bytes(original_bytes)

    replay = store.recover()
    assert replay.invalid_records == 1
    assert store.get(envelope.evidence_id) is None
    assert store.spool_path.read_bytes() == original_bytes


def test_concurrent_duplicate_appends_keep_every_receipt_and_one_version(tmp_path: Path) -> None:
    envelope = _evidence("concurrent-duplicate")

    def append_once(_: int) -> str:
        return EvidenceStore(tmp_path).append(envelope).disposition

    with ThreadPoolExecutor(max_workers=4) as executor:
        dispositions = list(executor.map(append_once, range(8)))

    store = EvidenceStore(tmp_path)
    assert dispositions.count("inserted") == 1
    assert dispositions.count("duplicate") == 7
    assert store.stats().evidence_versions == 1
    assert store.stats().receipts == 8
    assert len(store.receipts(envelope.evidence_id)) == 8


def test_locked_writer_rebinds_to_the_live_lock_inode_after_atomic_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = EvidenceStore(tmp_path)
    real_flock = fcntl.flock
    lock_attempted = threading.Event()
    entered = threading.Event()
    release = threading.Event()
    worker_errors: list[BaseException] = []

    def signaling_flock(descriptor: int, operation: int) -> None:
        if operation & fcntl.LOCK_EX:
            lock_attempted.set()
        real_flock(descriptor, operation)

    monkeypatch.setattr(
        "agentacct.evidence_store.fcntl",
        SimpleNamespace(
            LOCK_EX=fcntl.LOCK_EX,
            LOCK_UN=fcntl.LOCK_UN,
            flock=signaling_flock,
        ),
    )

    def hold_locked_section() -> None:
        try:
            with store._locked():
                entered.set()
                assert release.wait(timeout=10.0)
        except BaseException as exc:
            worker_errors.append(exc)

    old_descriptor = os.open(store.lock_path, os.O_RDWR)
    worker = threading.Thread(target=hold_locked_section, daemon=True)
    try:
        real_flock(old_descriptor, fcntl.LOCK_EX)
        worker.start()
        assert lock_attempted.wait(timeout=10.0)

        # The worker opened the old inode before its first flock attempt, so
        # replacing the lock file now while still holding the old inode's lock
        # forces the writer through the post-acquisition identity re-check.
        replacement = store.lock_path.with_name(".spool.lock.replacement")
        replacement.write_bytes(b"")
        os.rename(replacement, store.lock_path)
        real_flock(old_descriptor, fcntl.LOCK_UN)

        assert entered.wait(timeout=10.0)
        assert worker_errors == []
        probe_descriptor = os.open(store.lock_path, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                real_flock(probe_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release.set()
            worker.join(timeout=10.0)
            assert not worker.is_alive()
            assert worker_errors == []
            real_flock(probe_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            real_flock(probe_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(probe_descriptor)
    finally:
        release.set()
        os.close(old_descriptor)
        if worker.ident is not None:
            worker.join(timeout=10.0)


def test_claimed_link_can_arrive_before_evidence_and_resolves_when_both_exist(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    claim = _evidence("claim-link", assertion="claimed", dimension="task_semantics")
    observation = _evidence("observation-link", dimension="task_semantics")
    link = ClaimedLink.create(
        claimed_evidence_id=claim.evidence_id,
        observed_evidence_id=observation.evidence_id,
        relationship="corroborates",
        dimensions=("task_semantics",),
        created_at="2026-07-13T00:00:01Z",
        created_by="joiner.v1",
    )

    pending = store.append_claimed_link(link)
    assert pending.validation_state == "pending"
    store.append(observation)
    assert store.query_claimed_links()[0].validation_state == "pending"
    store.append(claim)
    linked = store.query_claimed_links()[0]
    assert linked.validation_state == "valid"
    assert linked.link == link

    duplicate = store.append_claimed_link(link)
    assert duplicate.disposition == "duplicate"
    assert store.query_claimed_links()[0].receipt_count == 2


def test_claimed_link_direction_and_dimension_are_verified_by_projection(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    first_observation = _evidence("wrong-direction-a", dimension="task_semantics")
    second_observation = _evidence("wrong-direction-b", dimension="task_semantics")
    store.append_many((first_observation, second_observation))
    link = ClaimedLink.create(
        claimed_evidence_id=first_observation.evidence_id,
        observed_evidence_id=second_observation.evidence_id,
        relationship="corroborates",
        dimensions=("task_semantics",),
        created_at="2026-07-13T00:00:01Z",
        created_by="joiner.v1",
    )
    result = store.append_claimed_link(link)
    assert result.validation_state == "invalid"
    assert store.query_claimed_links(validation_state="invalid")[0].link_id == link.link_id


def test_default_privacy_keeps_prompt_and_tool_bodies_out_of_spool_and_sqlite(tmp_path: Path) -> None:
    secret = "super-secret-tool-body"
    envelope = EvidenceEnvelope.create(
        assertion="observed",
        event_type="tool_completed",
        source_type="client_hook",
        source_system="claude-code",
        source_instance="workstation-a",
        source_schema="hook.v1",
        adapter="hook-adapter.v1",
        source_event_id="privacy-store",
        event_timestamp="2026-07-13T00:00:00Z",
        dimensions=("tool_activity",),
        measurement_basis="client_hook_observed",
        payload={"tool_name": "Bash", "tool_input": {"command": secret}, "tool_output": secret},
    )
    store = EvidenceStore(tmp_path)
    store.append(envelope)

    assert secret.encode("utf-8") not in store.spool_path.read_bytes()
    # A plain SQLite file scan is a useful belt-and-braces privacy assertion:
    # the projection receives only the already-sanitized immutable envelope.
    assert secret.encode("utf-8") not in store.projection_path.read_bytes()
    assert "tool_input" not in store.get(envelope.evidence_id).payload  # type: ignore[union-attr]
    assert "tool_output" not in store.get(envelope.evidence_id).payload  # type: ignore[union-attr]


def test_evidence_store_uses_owner_only_posix_permissions(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    store.append(_evidence("private-mode"))
    store.reconcile_refreshable_usage(
        (_refreshable_usage_item("private-slot", value=1, source_order=1),)
    )

    assert store.evidence_root.stat().st_mode & 0o077 == 0
    assert store.spool_path.stat().st_mode & 0o077 == 0
    assert store.refreshable_usage_spool_path.stat().st_mode & 0o077 == 0
    assert store.projection_path.stat().st_mode & 0o077 == 0
    assert store.lock_path.stat().st_mode & 0o077 == 0
