from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentacct.evidence import ClaimedLink, EvidenceEnvelope, SubjectRefs, canonical_digest, canonical_json_bytes
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


def _tool_activity_shadow(source_event_id: str, *, timestamp: str = "2026-07-13T00:00:00.000000Z"):
    return _evidence(
        source_event_id,
        timestamp=timestamp,
        assertion="claimed",
        dimension="tool_activity",
        source_type="mcp_agent_reported",
        source_system="codex",
        event_type="tool_activity_observed",
    )


def test_prune_default_deletes_only_tool_activity_shadow_and_children(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    ta_ids = [store.append(_tool_activity_shadow(f"ta-{i}")).evidence_id for i in range(3)]
    store.append(_evidence("check-1", assertion="observed"))  # client_hook
    store.reconcile_refreshable_usage(
        (_refreshable_usage_item("slot-a", value=1, source_order=1),)
    )

    before = store.stats().evidence_versions
    result = store.prune_versions(dry_run=False, vacuum=False)

    assert result.matched_versions == 3
    assert result.deleted_versions == 3
    assert store.stats().evidence_versions == before - 3
    for eid in ta_ids:
        assert store.receipts(eid) == []
        assert store.get(eid) is None
    assert store.query(source_type="client_hook")
    assert store.query(source_type="local_client_log")


def test_prune_dry_run_writes_nothing(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    for i in range(2):
        store.append(_tool_activity_shadow(f"ta-{i}"))
    before = store.stats().evidence_versions

    result = store.prune_versions(dry_run=True)

    assert result.matched_versions == 2
    assert result.deleted_versions == 0
    assert store.stats().evidence_versions == before


def test_prune_denylist_refuses_honesty_critical(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    with pytest.raises(ValueError):
        store.prune_versions(source_types=["client_hook"], dry_run=False)
    with pytest.raises(ValueError):
        store.prune_versions(source_types=["local_client_log"], dry_run=False)


def test_prune_excludes_claimed_link_refs(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    keep = store.append(_tool_activity_shadow("ta-keep")).evidence_id
    drop = store.append(_tool_activity_shadow("ta-drop")).evidence_id
    with store._connection() as conn:
        conn.execute(
            "INSERT INTO claimed_link_versions(link_id, idempotency_key, integrity_hash, "
            "claimed_evidence_id, observed_evidence_id, dimensions_json, link_json, validation_state) "
            "VALUES('lnk-1','idem-1','hash-1',?,?,'[]','{}','pending')",
            (keep, keep),
        )

    result = store.prune_versions(dry_run=False)

    assert result.deleted_versions == 1
    assert store.get(keep) is not None
    assert store.get(drop) is None


def test_prune_does_not_resurrect_on_recover(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    for i in range(4):
        store.append(_tool_activity_shadow(f"ta-{i}"))

    store.prune_versions(dry_run=False, vacuum=False)
    assert store.query(source_type="mcp_agent_reported", event_type="tool_activity_observed") == []

    reopened = EvidenceStore(tmp_path)
    replay = reopened.recover()
    assert replay.projected_receipts == 0
    assert reopened.query(source_type="mcp_agent_reported", event_type="tool_activity_observed") == []


def test_prune_older_than_filters_by_timestamp(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    old = store.append(_tool_activity_shadow("ta-old", timestamp="2026-01-01T00:00:00.000000Z")).evidence_id
    new = store.append(_tool_activity_shadow("ta-new", timestamp="2026-07-01T00:00:00.000000Z")).evidence_id

    result = store.prune_versions(older_than="2026-03-01T00:00:00.000000Z", dry_run=False)

    assert result.deleted_versions == 1
    assert store.get(old) is None
    assert store.get(new) is not None


def test_prune_vacuum_shrinks_projection_file(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    for i in range(200):
        store.append(_tool_activity_shadow(f"ta-{i}"))
    before = store.projection_path.stat().st_size

    result = store.prune_versions(dry_run=False, vacuum=True)

    assert result.vacuumed is True
    assert store.projection_path.stat().st_size < before
    assert oct(store.projection_path.stat().st_mode)[-3:] == "600"


def test_prune_deletes_every_match_across_many_batches(tmp_path: Path) -> None:
    # More rows than one chunk (batch_size=100) so the forward-by-rowid walk
    # must span multiple batches and still remove every matching row — the
    # regression that the earlier "delete from the temp table each batch"
    # approach made O(N^2) and could leave rows behind on interruption.
    store = EvidenceStore(tmp_path)
    for i in range(250):
        store.append(_tool_activity_shadow(f"ta-{i}"))
    store.append(_evidence("keep-1", assertion="observed"))  # client_hook survivor

    result = store.prune_versions(dry_run=False, vacuum=False, batch_size=100)

    assert result.matched_versions == 250
    assert result.deleted_versions == 250
    assert result.batches >= 3
    assert store.query(source_type="mcp_agent_reported", event_type="tool_activity_observed") == []
    assert store.query(source_type="client_hook")  # untouched
    # A second run finds nothing left.
    assert store.prune_versions(dry_run=False, vacuum=False).matched_versions == 0


def test_auto_prune_throttles(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    for i in range(5):
        store.append(_tool_activity_shadow(f"ta-{i}"))

    first = store.auto_prune_if_due(
        min_interval_seconds=3600, older_than_seconds=0, max_rows=100, now=1000.0
    )
    assert first is not None
    assert first.deleted_versions == 5

    second = store.auto_prune_if_due(
        min_interval_seconds=3600, older_than_seconds=0, max_rows=100, now=1060.0
    )
    assert second is None


def _spool_lines(path: Path) -> list[bytes]:
    return path.read_bytes().splitlines(keepends=True)


def _spool_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _lines_for(path: Path, evidence_ids: set[str]) -> list[bytes]:
    return [
        line
        for line in _spool_lines(path)
        if json.loads(line).get("payload", {}).get("evidence_id") in evidence_ids
    ]


def _readable_projection(store: EvidenceStore) -> dict[str, object]:
    """Everything a reader can see, in one comparable value.

    The compaction's contract is that none of it moves: the compared counts, the
    arrival order, every receipt with its disposition, dimensions,
    acknowledgements, refreshable heads and revisions, and claimed links.
    """

    summary = store._projection_compaction_summary()
    records = store.query(order_by="arrival", limit=10_000)
    return {
        "counts": dict(summary["counts"]),
        "arrival_rows": summary["arrival_rows"],
        "arrival": [record.evidence_id for record in records],
        "receipts": {
            record.evidence_id: [
                (receipt["receipt_id"], receipt["disposition"])
                for receipt in store.receipts(record.evidence_id)
            ]
            for record in records
        },
        "heads": [
            (head.slot_key, head.last_revision_id, head.evidence_id, head.tombstoned)
            for head in store.refreshable_usage_heads()
        ],
        "refreshable": {
            key: value
            for key, value in store.refreshable_usage_stats().to_dict().items()
            # spool_bytes is a size of the rewritten file, not projection state.
            if key != "spool_bytes"
        },
        "links": [
            (link.link_id, link.validation_state) for link in store.query_claimed_links(limit=1000)
        ],
    }


def _reopen_from_zero(tmp_path: Path) -> EvidenceStore:
    """Discard the projection so opening the store replays both spools."""

    projection = tmp_path / EVIDENCE_STORE_DIRNAME / "projection.sqlite3"
    projection.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{projection}{suffix}")
        if sidecar.exists():
            sidecar.unlink()
    return EvidenceStore(tmp_path)


def _projection_counts_after_a_rebuild(store: EvidenceStore) -> dict[str, int]:
    """The counts a from-zero rebuild of these spools produces.

    The rebuild reads hard links of the store's spool files in a scratch store,
    so the live store and its projection are untouched. Once ``prune_versions``
    has run this is deliberately *larger* than the live projection: the rebuild
    resurrects every pruned version the append-only spool still holds, which is
    exactly why the live projection cannot be the compaction's baseline.
    """

    with tempfile.TemporaryDirectory() as scratch:
        rebuild = EvidenceStore(Path(scratch))
        os.link(store.spool_path, rebuild.spool_path)
        if store.refreshable_usage_spool_path.is_file():
            os.link(store.refreshable_usage_spool_path, rebuild.refreshable_usage_spool_path)
        rebuild._recover_unlocked()
        return dict(rebuild._projection_compaction_summary()["counts"])


def _compactable_store(tmp_path: Path, *, shadows: int = 6) -> tuple[EvidenceStore, list[str]]:
    """A store whose pruned shadow rows are still sitting in its spool.

    ``prune_versions`` deletes those versions and their receipts yet leaves the
    append-only bytes behind, and replay only ever moves forward from the EOF
    cursor — exactly the unreachable bloat a spool compaction reclaims. The
    client_hook and refreshable-usage lanes are the honesty-critical rows it has
    to keep.
    """

    store = EvidenceStore(tmp_path)
    dropped = [
        store.append(_tool_activity_shadow(f"ta-{i}")).evidence_id for i in range(shadows - 1)
    ]
    store.append(_evidence("check-1", assertion="observed"))
    store.reconcile_refreshable_usage(
        (_refreshable_usage_item("slot-a", value=10, source_order=1),)
    )
    # One shadow row after the refreshable receipt, so its fence has to be
    # re-mapped past a dropped record.
    dropped.append(store.append(_tool_activity_shadow("ta-tail")).evidence_id)
    pruned = store.prune_versions(dry_run=False, vacuum=False)
    assert pruned.deleted_versions == shadows
    return store, dropped


def _spool_line_for(store: EvidenceStore, envelope: EvidenceEnvelope) -> bytes:
    record = store._spool_record(kind="evidence", payload=envelope.to_dict())
    return canonical_json_bytes(record) + b"\n"


def test_compact_spool_replays_to_the_same_projection(tmp_path: Path) -> None:
    store, dropped_ids = _compactable_store(tmp_path)
    before = _readable_projection(store)
    original_lines = _spool_lines(store.spool_path)
    dropped_set = set(dropped_ids)
    expected_kept_lines = [
        line for line in original_lines if json.loads(line)["payload"]["evidence_id"] not in dropped_set
    ]
    assert len(expected_kept_lines) < len(original_lines)

    result = store.compact_spool(dry_run=False, archive=False)

    assert result.dry_run is False
    assert result.swapped is True
    assert result.dropped_rows == len(dropped_ids)
    assert result.kept_rows == result.rows_before - len(dropped_ids)
    assert result.rows_after == result.kept_rows
    assert result.spool_bytes_after == store.spool_path.stat().st_size < result.spool_bytes_before
    assert result.bytes_reclaimed() == result.spool_bytes_before - result.spool_bytes_after
    assert result.verification["equivalent"] is True
    assert result.verification["outcome"] == "swapped"
    # Every kept row is rewritten byte for byte in the original order; only the
    # dropped rows are gone, and no surviving receipt lost its visibility.
    assert store.spool_path.read_bytes() == b"".join(expected_kept_lines)
    for evidence_id in dropped_ids:
        assert store.get(evidence_id) is None
        assert store.receipts(evidence_id) == []

    assert _readable_projection(store) == before
    rebuilt = _reopen_from_zero(tmp_path)
    assert _readable_projection(rebuilt) == before
    assert rebuilt.recover().projected_receipts == 0


def _fenced_store(tmp_path: Path) -> tuple[EvidenceStore, list[int]]:
    """A pruned store whose refreshable receipts carry stale spool fences."""

    store = EvidenceStore(tmp_path)
    store.append(_evidence("fence-before"))
    store.reconcile_refreshable_usage(
        (_refreshable_usage_item("slot-a", value=10, source_order=1),)
    )
    for i in range(4):
        store.append(_tool_activity_shadow(f"ta-{i}"))
    store.reconcile_refreshable_usage(
        (_refreshable_usage_item("slot-a", value=20, source_order=2),)
    )
    store.append(_evidence("fence-after"))
    store.prune_versions(dry_run=False, vacuum=False)
    fences = [
        json.loads(line)["main_spool_fence"]
        for line in store.refreshable_usage_spool_path.read_text(encoding="utf-8").splitlines()
    ]
    return store, fences


def test_compact_spool_remaps_refreshable_fences_and_replays_from_zero(tmp_path: Path) -> None:
    store, fences_before = _fenced_store(tmp_path)
    before = _readable_projection(store)
    expected_arrival = [record.evidence_id for record in store.query(order_by="arrival", limit=100)]

    result = store.compact_spool(dry_run=False, archive=False)

    assert result.swapped is True
    assert result.verification["equivalent"] is True
    assert result.verification["refreshable_fences_remapped"] >= 1
    records = [
        json.loads(line)
        for line in store.refreshable_usage_spool_path.read_text(encoding="utf-8").splitlines()
    ]
    raw_spool = store.spool_path.read_bytes()
    assert len(records) == len(fences_before)
    for newer, older in zip(records, fences_before):
        # The record hash covers the fence, so it has to be re-derived.
        assert newer["record_hash"] == canonical_digest(
            {key: value for key, value in newer.items() if key != "record_hash"}
        )
        fence = newer["main_spool_fence"]
        # A fence has to name a record boundary inside the rewritten spool.
        assert 0 <= fence <= len(raw_spool)
        assert fence == 0 or raw_spool[fence - 1 : fence] == b"\n"
        assert fence <= older
    assert records[-1]["main_spool_fence"] < fences_before[-1]

    rebuilt = _reopen_from_zero(tmp_path)
    assert _readable_projection(rebuilt) == before
    assert [record.evidence_id for record in rebuilt.query(order_by="arrival", limit=100)] == (
        expected_arrival
    )
    # Both lanes keep working on top of the rewritten spools.
    rebuilt.append(_evidence("fence-after-compaction"))
    rebuilt.reconcile_refreshable_usage(
        (_refreshable_usage_item("slot-a", value=30, source_order=3),)
    )
    rebuilt.reconcile_refreshable_usage(
        (_refreshable_usage_item("slot-b", value=1, source_order=1),)
    )
    assert [head.slot_key for head in rebuilt.refreshable_usage_heads()] == ["slot-a", "slot-b"]
    latest_arrival = [record.evidence_id for record in rebuilt.query(order_by="arrival", limit=100)]
    again = _reopen_from_zero(tmp_path)
    assert [record.evidence_id for record in again.query(order_by="arrival", limit=100)] == (
        latest_arrival
    )
    assert again.refreshable_usage_stats().heads == 2


def test_compact_spool_fence_remap_is_what_keeps_the_from_zero_replay_working(
    tmp_path: Path,
) -> None:
    # Negative control: put the pre-compaction fences back and the from-zero
    # replay fails exactly the way an unmapped fence would make it fail.
    store, fences_before = _fenced_store(tmp_path)
    result = store.compact_spool(dry_run=False, archive=False)
    assert result.swapped is True
    assert result.verification["refreshable_fences_remapped"] >= 1

    lines = store.refreshable_usage_spool_path.read_bytes().splitlines(keepends=True)
    stale = json.loads(lines[-1])
    body = {key: value for key, value in stale.items() if key != "record_hash"}
    body["main_spool_fence"] = fences_before[-1]
    lines[-1] = canonical_json_bytes({**body, "record_hash": canonical_digest(body)}) + b"\n"
    store.refreshable_usage_spool_path.write_bytes(b"".join(lines))

    with pytest.raises(
        RuntimeError,
        match="refreshable usage spool requires missing legacy spool bytes",
    ):
        _reopen_from_zero(tmp_path)


def test_compact_spool_row_verdict_keeps_every_guarded_lane(tmp_path: Path) -> None:
    # Classifier-level guards, with deliberately empty protection sets: a row is
    # only droppable when it is the default prune target, it still validates, and
    # nothing in the projection answers for its identity.
    store = EvidenceStore(tmp_path)
    shadow = _tool_activity_shadow("ta-1")
    hook = _evidence(
        "hook-ta",
        assertion="observed",
        source_type="client_hook",
        event_type="tool_activity_observed",
        dimension="tool_activity",
    )
    local_log = _evidence(
        "log-ta",
        assertion="observed",
        source_type="local_client_log",
        event_type="tool_activity_observed",
        dimension="tool_activity",
    )
    nothing: frozenset[str] = frozenset()

    assert store._compaction_row_verdict(_spool_line_for(store, shadow), nothing, nothing) == "drop"
    assert store._compaction_row_verdict(_spool_line_for(store, hook), nothing, nothing) == "keep"
    assert store._compaction_row_verdict(_spool_line_for(store, local_log), nothing, nothing) == "keep"
    assert (
        store._compaction_row_verdict(
            _spool_line_for(store, shadow), frozenset({shadow.idempotency_key}), nothing
        )
        == "keep"
    )
    assert (
        store._compaction_row_verdict(
            _spool_line_for(store, shadow), nothing, frozenset({shadow.evidence_id})
        )
        == "keep"
    )
    # Unreadable, blank, and tampered lines are never dropped.
    assert store._compaction_row_verdict(b'{"torn":\n', nothing, nothing) == "unclassified"
    assert store._compaction_row_verdict(b"", nothing, nothing) == "keep"
    assert store._compaction_row_verdict(b"\n", nothing, nothing) == "keep"
    tampered = store._spool_record(kind="evidence", payload=shadow.to_dict())
    tampered["received_at"] = "2026-07-13T00:00:00.000000Z"
    assert (
        store._compaction_row_verdict(canonical_json_bytes(tampered) + b"\n", nothing, nothing)
        == "unclassified"
    )
    claimed_link = store._spool_record(
        kind="claimed_link",
        payload={"claimed_evidence_id": shadow.evidence_id},
    )
    assert (
        store._compaction_row_verdict(canonical_json_bytes(claimed_link) + b"\n", nothing, nothing)
        == "keep"
    )


def test_compact_spool_keeps_client_log_and_referenced_rows(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    # A shadow-shaped row the claimed-link lane answers for: prune_versions
    # excludes it from its target set, and so must the spool compaction.
    observed = store.append(
        _evidence("hook-observed", assertion="observed", dimension="tool_activity")
    ).evidence_id
    for i in range(3):
        store.append(_tool_activity_shadow(f"ta-{i}"))
    referenced = store.append(_tool_activity_shadow("ta-referenced")).evidence_id
    link = store.append_claimed_link(
        ClaimedLink.create(
            claimed_evidence_id=referenced,
            observed_evidence_id=observed,
            relationship="corroborates",
            dimensions=("tool_activity",),
            created_at="2026-07-13T00:00:01Z",
            created_by="joiner.v1",
        )
    )
    assert link.validation_state == "valid"
    # Rows whose source type is honesty-critical, carrying the default target's
    # very event type: they are no more droppable than any other client_hook row.
    client_hook = store.append(
        _evidence(
            "hook-ta",
            assertion="observed",
            source_type="client_hook",
            event_type="tool_activity_observed",
            dimension="tool_activity",
        )
    ).evidence_id
    local_log = store.append(
        _evidence(
            "log-ta",
            assertion="observed",
            source_type="local_client_log",
            event_type="tool_activity_observed",
            dimension="tool_activity",
        )
    ).evidence_id
    pruned = store.prune_versions(dry_run=False, vacuum=False)
    assert pruned.deleted_versions == 3
    surviving_ids = {client_hook, local_log, referenced, observed}
    assert referenced in store._compaction_protected_identities()[1]
    before = _readable_projection(store)
    kept_lines_before = _lines_for(store.spool_path, surviving_ids)

    result = store.compact_spool(dry_run=False, archive=False)

    assert result.swapped is True
    assert result.dropped_rows == 3
    assert _lines_for(store.spool_path, surviving_ids) == kept_lines_before
    assert len(kept_lines_before) == len(surviving_ids) == 4
    for evidence_id in surviving_ids:
        assert store.get(evidence_id) is not None
    assert store.query(source_type="client_hook")
    assert store.query(source_type="local_client_log")
    assert store.query_claimed_links()[0].validation_state == "valid"
    assert _readable_projection(store) == before


def test_compact_spool_dry_run_reports_exactly_and_writes_nothing(tmp_path: Path) -> None:
    store, dropped_ids = _compactable_store(tmp_path)
    spool = store.spool_path
    refreshable = store.refreshable_usage_spool_path
    dropped_bytes = sum(len(line) for line in _lines_for(spool, set(dropped_ids)))
    before = (
        spool.stat().st_size,
        spool.stat().st_mtime_ns,
        _spool_sha256(spool),
        refreshable.stat().st_size,
        refreshable.stat().st_mtime_ns,
        _spool_sha256(refreshable),
        sorted(path.name for path in store.evidence_root.iterdir()),
    )
    before_state = _readable_projection(store)

    result = store.compact_spool()

    assert result.dry_run is True
    assert result.swapped is False
    assert result.archived_path is None
    assert result.archive_bytes == 0
    assert result.dropped_rows == len(dropped_ids)
    assert result.kept_rows == result.rows_before - len(dropped_ids)
    assert result.dropped_bytes == dropped_bytes > 0
    assert result.spool_bytes_after == result.spool_bytes_before - dropped_bytes
    # A dry run writes nothing and rebuilds nothing: it reports what a real run
    # would drop, and says explicitly that no comparison ran.
    assert result.verification["outcome"] == "dry_run"
    assert result.verification["equivalent"] is None
    assert result.verification["candidate_rows"] == result.kept_rows
    assert result.verification["candidate_bytes"] == result.spool_bytes_after
    assert "--write" in result.verification["reason"]
    assert "counts" not in result.verification
    assert (
        spool.stat().st_size,
        spool.stat().st_mtime_ns,
        _spool_sha256(spool),
        refreshable.stat().st_size,
        refreshable.stat().st_mtime_ns,
        _spool_sha256(refreshable),
        sorted(path.name for path in store.evidence_root.iterdir()),
    ) == before
    assert not (tmp_path / "archive").exists()
    assert _readable_projection(store) == before_state


def test_compact_spool_dry_run_never_replays_and_never_writes_a_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The two from-zero rebuilds are the heavy part of a compaction, and a
    # candidate copy of a 20 GB spool is the other: a dry run must pay for
    # neither. Both are made to fail loudly, and the dry run still reports exact
    # numbers with every file left as it was.
    store, dropped_ids = _compactable_store(tmp_path)
    spool = store.spool_path
    original = spool.read_bytes()
    listing = sorted(path.name for path in store.evidence_root.iterdir())

    def refuse_to_replay(self: EvidenceStore, **kwargs: object) -> None:
        raise AssertionError("a dry run must not replay a spool")

    def refuse_to_scratch(self: object, *args: object, **kwargs: object) -> None:
        raise AssertionError("a dry run must not create a scratch workspace")

    monkeypatch.setattr(EvidenceStore, "_recover_unlocked", refuse_to_replay)
    monkeypatch.setattr(tempfile, "mkdtemp", refuse_to_scratch)
    monkeypatch.setattr(tempfile, "TemporaryDirectory", refuse_to_scratch)

    result = store.compact_spool()

    assert result.dry_run is True
    assert result.dropped_rows == len(dropped_ids) > 0
    assert result.verification["outcome"] == "dry_run"
    assert spool.read_bytes() == original
    assert sorted(path.name for path in store.evidence_root.iterdir()) == listing


def test_compact_spool_second_run_changes_nothing(tmp_path: Path) -> None:
    store, dropped_ids = _compactable_store(tmp_path)
    assert store._compaction_generation() == 0
    assert store.compact_spool().dry_run is True
    # A dry run reports the generation a real run would use and stores none.
    assert store._compaction_generation() == 0
    first = store.compact_spool(dry_run=False, archive=False)
    assert first.swapped is True
    assert first.dropped_rows == len(dropped_ids)
    assert first.generation == 1
    assert store._compaction_generation() == first.generation
    spool = store.spool_path
    after_first = (spool.stat().st_size, spool.stat().st_mtime_ns, _spool_sha256(spool))
    state = _readable_projection(store)
    listing = sorted(path.name for path in store.evidence_root.iterdir())

    second = store.compact_spool(dry_run=False, archive=True)

    assert second.dry_run is False
    assert second.swapped is False
    assert second.dropped_rows == 0
    assert second.kept_rows == second.rows_before == second.rows_after
    assert second.spool_bytes_after == second.spool_bytes_before
    assert second.archived_path is None
    assert second.archive_bytes == 0
    assert second.verification["outcome"] == "nothing_to_do"
    assert second.verification["equivalent"] is True
    assert (spool.stat().st_size, spool.stat().st_mtime_ns, _spool_sha256(spool)) == after_first
    assert sorted(path.name for path in store.evidence_root.iterdir()) == listing
    assert not (tmp_path / "archive").exists()
    assert _readable_projection(store) == state
    assert store.compact_spool().dropped_rows == 0
    # A run that writes nothing advances neither the stored generation nor the
    # generation a later run would establish.
    assert second.generation == first.generation + 1
    assert store._compaction_generation() == first.generation
    assert store.compact_spool().generation == second.generation


def test_compact_spool_aborts_without_swapping_when_verification_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, dropped_ids = _compactable_store(tmp_path)
    spool = store.spool_path
    original = spool.read_bytes()
    stamp = spool.stat().st_mtime_ns
    listing = sorted(path.name for path in store.evidence_root.iterdir())

    def failing_verification(
        self,
        *,
        plan: object,
        candidate_main: Path,
        candidate_refreshable: Path | None,
        main_limit: int,
        refreshable_limit: int,
        warnings: list[str],
    ) -> dict[str, object]:
        warnings.append("injected verification failure")
        return {
            "equivalent": False,
            "mismatches": {"evidence_versions": {"live_rows_missing_from_the_rebuild": 1}},
            "counts": {},
            "live_rows_checked": 0,
            "arrival_order_rows": 0,
            "arrival_order_equal": False,
        }

    monkeypatch.setattr(EvidenceStore, "_verify_compaction_candidate", failing_verification)

    result = store.compact_spool(dry_run=False, archive=True)

    assert result.swapped is False
    assert result.dropped_rows == len(dropped_ids)
    assert result.spool_bytes_after == result.spool_bytes_before
    assert result.verification["equivalent"] is False
    assert result.verification["outcome"] == "aborted"
    assert any("injected verification failure" in warning for warning in result.warnings)
    assert spool.read_bytes() == original
    assert spool.stat().st_mtime_ns == stamp
    assert sorted(path.name for path in store.evidence_root.iterdir()) == listing
    assert not (tmp_path / "archive").exists()
    assert store.query(source_type="client_hook")


def test_compact_spool_swaps_when_a_kept_row_outlives_its_pruned_version(
    tmp_path: Path,
) -> None:
    # Regression pin for the baseline. A referenced row is kept even though its
    # version already left the projection, so a from-zero rebuild resurrects a
    # version the live projection does not have. That is prune's doing, not the
    # compaction's: the live projection is no longer the baseline, so the swap is
    # allowed — the old live-vs-rebuild comparison misreported this store as
    # lossy and aborted every compaction of it.
    store = EvidenceStore(tmp_path)
    for i in range(2):
        store.append(_tool_activity_shadow(f"ta-{i}"))
    referenced = store.append(_tool_activity_shadow("ta-referenced")).evidence_id
    store.prune_versions(dry_run=False, vacuum=False)
    _link_after_the_prune(store, referenced)
    live_versions = store.stats().evidence_versions
    rebuild = _projection_counts_after_a_rebuild(store)
    assert rebuild["evidence_versions"] > live_versions

    result = store.compact_spool(dry_run=False, archive=False)

    assert result.swapped is True
    assert result.dropped_rows == 2
    assert result.verification["equivalent"] is True
    assert result.verification["mismatches"] == {}
    # The kept row is still in the spool (its version was already gone from the
    # live projection, which the compaction does not re-project), and the
    # rebuild carries it, so the containment holds.
    assert _lines_for(store.spool_path, {referenced})
    assert result.verification["live_rows_checked"] > 0
    assert store.get(referenced) is None
    # The comparison the old implementation used — the live projection against a
    # rebuild of the result — differs right here (1 versus 0), which is why it
    # refused this swap: prune deleted the version and the rebuild resurrects it.
    assert _projection_counts_after_a_rebuild(store)["evidence_versions"] != live_versions
    assert _reopen_from_zero(tmp_path).get(referenced) is not None


def _link_after_the_prune(store: EvidenceStore, evidence_id: str) -> None:
    """Pin a claimed link to an id whose evidence version prune already deleted.

    The link is appended through the store's own spool path *after* the prune, so
    the projection answers for it while the spool holds it: a from-zero rebuild
    reproduces the link, and the compaction has to keep the row it references
    even though the live projection no longer has that version.
    """

    observed = store.append(_tool_activity_shadow("ta-link-observed")).evidence_id
    link = ClaimedLink.create(
        claimed_evidence_id=evidence_id,
        observed_evidence_id=observed,
        relationship="corroborates",
        dimensions=("tool_activity",),
        created_at="2026-07-13T00:00:01Z",
        created_by="joiner.v1",
    )
    record = store._spool_record(kind="claimed_link", payload=link.to_dict())
    with store._locked():
        offset = store._append_spool_record(record)
        store._project_claimed_link_record(record, offset)
        store._set_replay_offset(store.spool_path.stat().st_size)


def test_compact_spool_swaps_when_prune_deleted_a_conflict_version(
    tmp_path: Path,
) -> None:
    # The real-store shape: two versions of one logical event (same idempotency
    # key, different content) are both pruned, and a claimed link appended before
    # the compaction pins the second row's evidence id. The compaction drops the
    # unreferenced twin and keeps the referenced one, so one key has a dropped row
    # and a kept row at once; the old live-versus-rebuild comparison aborted here.
    store = EvidenceStore(tmp_path)
    first = store.append(_tool_activity_shadow("ta-conflict"))
    second = store.append(
        _evidence(
            "ta-conflict",
            payload_value=2,
            timestamp="2026-07-13T00:00:01.000000Z",
            assertion="claimed",
            dimension="tool_activity",
            source_type="mcp_agent_reported",
            source_system="codex",
            event_type="tool_activity_observed",
        )
    )
    assert first.idempotency_key == second.idempotency_key
    assert first.disposition == "inserted"
    assert second.disposition == "conflict"
    store.prune_versions(dry_run=False, vacuum=False)
    assert store.get(first.evidence_id) is None
    assert store.get(second.evidence_id) is None
    _link_after_the_prune(store, second.evidence_id)
    before = store.stats().evidence_versions

    result = store.compact_spool(dry_run=False, archive=False)

    assert result.swapped is True
    assert result.dropped_rows == 1
    assert result.kept_rows == 3
    assert result.verification["equivalent"] is True
    # The guarded twin survives in the spool and shadows the unreachable twin
    # that was dropped; the live projection still answers for neither, because
    # prune removed both versions and compaction never re-projects.
    assert _lines_for(store.spool_path, {second.evidence_id})
    assert store.get(second.evidence_id) is None
    assert store.stats().evidence_versions == before
    assert _projection_counts_after_a_rebuild(store)["evidence_versions"] == before + 1
    # The live projection answers for neither twin while a rebuild of the result
    # answers for the kept one: the old live-versus-rebuild baseline differed here
    # too, and refused the swap.
    assert before != _projection_counts_after_a_rebuild(store)["evidence_versions"]


def test_compact_spool_aborts_when_the_candidate_is_not_the_snapshot_remainder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A kept row is copied byte for byte, so the candidate's size and row count
    # are arithmetic. A filter that lost or duplicated rows fails that check —
    # the identity comparison alone could read it as the compaction's own doing.
    store, dropped_ids = _compactable_store(tmp_path)
    spool = store.spool_path
    original = spool.read_bytes()
    original_filter = EvidenceStore._filter_spool_snapshot

    def filter_then_duplicate(self: EvidenceStore, **kwargs: object) -> object:
        plan = original_filter(self, **kwargs)
        candidate = kwargs.get("candidate_main")
        assert isinstance(candidate, Path)
        with candidate.open("ab") as handle:
            handle.write(b'{"kind":"evidence","payload":{}}\n')
        return plan

    monkeypatch.setattr(EvidenceStore, "_filter_spool_snapshot", filter_then_duplicate)

    result = store.compact_spool(dry_run=False, archive=True)

    assert result.swapped is False
    assert result.verification["equivalent"] is False
    assert result.verification["outcome"] == "aborted"
    assert "candidate_rows" in result.verification["mismatches"]
    assert "candidate_bytes" in result.verification["mismatches"]
    assert result.archived_path is None
    assert spool.read_bytes() == original
    assert store.get(dropped_ids[0]) is None
    assert not list((tmp_path / "archive").glob("*"))


def test_compact_spool_tolerates_a_live_evidence_row_the_spool_never_carried(
    tmp_path: Path,
) -> None:
    # The real-store shape that motivated the accountability rule: the live
    # projection can hold evidence versions no main-spool row answers for (the
    # refreshable-usage lane projects its own evidence, and an older code path
    # can leave rows behind), so no rebuild of this spool reproduces them. The
    # compaction cannot have removed a row this spool never carried, so the swap
    # goes ahead.
    store, dropped_ids = _compactable_store(tmp_path)
    with store._connection() as connection:
        connection.execute(
            "INSERT INTO evidence_versions(evidence_id, idempotency_key, integrity_hash, "
            "schema_version, assertion, event_type, source_type, source_system, source_instance, "
            "source_schema, adapter, event_timestamp, observed_at, dimensions_json, envelope_json, "
            "is_conflict) VALUES('evd_' || printf('%064d', 7), 'idem_live_only', 'sha256:live_only', "
            "'agent-chronicle.evidence.v2', 'observed', 'model_usage', 'local_client_log', 'codex', "
            "'trusted-v1-current-usage', 'agent-chronicle.refreshable-usage-truth.v1', "
            "'chronicle-refreshable-usage-adapter.v1', '2026-07-13T00:00:00.000000Z', "
            "'2026-07-13T00:00:00.000000Z', '[\"usage\"]', '{}', 0)"
        )

    result = store.compact_spool(dry_run=False, archive=False)

    assert result.swapped is True
    assert result.dropped_rows == len(dropped_ids)
    assert result.verification["equivalent"] is True
    assert result.verification["mismatches"] == {}
    # It was still counted in what the live projection carries, minus the
    # unaccountable row.
    assert result.verification["live_rows_checked"] > 0


def test_compact_spool_aborts_when_live_holds_a_row_the_spool_cannot_answer_for(
    tmp_path: Path,
) -> None:
    # A projection row in a lane the compaction never drops from, with no spool
    # record behind it: the shape a hand-edited or half-migrated store has. That
    # lane is supposed to be reproducible from the spool in full, so the gate
    # refuses to swap over it.
    store, dropped_ids = _compactable_store(tmp_path)
    with store._connection() as connection:
        connection.execute(
            "INSERT INTO claimed_link_versions(link_id, idempotency_key, integrity_hash, "
            "claimed_evidence_id, observed_evidence_id, dimensions_json, link_json, validation_state) "
            "VALUES('lnk-orphan','idem-orphan','hash-orphan',?,?,'[]','{}','pending')",
            (dropped_ids[0], dropped_ids[0]),
        )
    spool = store.spool_path
    original = spool.read_bytes()

    result = store.compact_spool(dry_run=False, archive=True)

    assert result.swapped is False
    assert result.verification["outcome"] == "aborted"
    assert result.verification["equivalent"] is False
    assert result.verification["mismatches"]["claimed_link_versions"][
        "live_rows_missing_from_the_rebuild"
    ] == 1
    assert result.archived_path is None
    assert spool.read_bytes() == original
    assert not list((tmp_path / "archive").glob("*"))


def test_compact_spool_archives_the_pre_compaction_spool(tmp_path: Path) -> None:
    import zstandard

    store, _ = _compactable_store(tmp_path)
    original = store.spool_path.read_bytes()
    moment = 1_771_000_000.0

    result = store.compact_spool(dry_run=False, archive=True, now=moment)

    assert result.swapped is True
    assert result.archived_path is not None
    archive = Path(result.archived_path)
    assert archive == tmp_path / "archive" / f"spool-20260213-gen{result.generation}.jsonl.zst"
    assert archive.is_file()
    assert result.archive_bytes == archive.stat().st_size > 0
    assert oct(archive.stat().st_mode)[-3:] == "600"
    with archive.open("rb") as handle:
        restored = zstandard.ZstdDecompressor().stream_reader(handle).read()
    assert restored == original
    assert oct(store.spool_path.stat().st_mode)[-3:] == "600"
    assert oct(store.refreshable_usage_spool_path.stat().st_mode)[-3:] == "600"
    assert oct(archive.stat().st_mode)[-3:] == "600"

    next_run = store.compact_spool(dry_run=False, archive=True, now=moment + 86_400)
    assert next_run.swapped is False
    assert next_run.generation == result.generation + 1
    assert next_run.archived_path is None


def test_compact_spool_never_overwrites_an_existing_archive(tmp_path: Path) -> None:
    store, _ = _compactable_store(tmp_path)
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    occupied = archive_dir / f"spool-20260213-gen{store._compaction_generation() + 1}.jsonl.zst"
    occupied.write_bytes(b"previous archive")
    spool = store.spool_path
    original = spool.read_bytes()

    result = store.compact_spool(dry_run=False, archive=True, now=1_771_000_000.0)

    assert result.swapped is False
    assert result.verification["outcome"] == "aborted"
    assert "the cold archive failed" in result.verification["abort_reason"]
    assert occupied.read_bytes() == b"previous archive"
    assert spool.read_bytes() == original


def test_compact_spool_carries_receipts_written_during_the_offline_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The scan, the verification rebuild, and the archive run without the lock,
    # so a live watcher keeps appending. Those receipts are not part of the
    # snapshot and must be copied verbatim onto the compacted spool.
    store, dropped_ids = _compactable_store(tmp_path)
    late = _evidence("late-arrival")
    original_filter = EvidenceStore._filter_spool_snapshot

    def filter_then_append(self: EvidenceStore, **kwargs: object) -> object:
        plan = original_filter(self, **kwargs)
        record = self._spool_record(kind="evidence", payload=late.to_dict())
        offset = self._append_spool_record(record)
        self._project_evidence_record(record, offset)
        self._set_replay_offset(self.spool_path.stat().st_size)
        return plan

    monkeypatch.setattr(EvidenceStore, "_filter_spool_snapshot", filter_then_append)

    result = store.compact_spool(dry_run=False, archive=False)

    assert result.swapped is True
    assert result.dropped_rows == len(dropped_ids)
    assert result.rows_after == result.kept_rows + 1
    assert store.get(late.evidence_id) is not None
    expected_arrival = [record.evidence_id for record in store.query(order_by="arrival", limit=100)]
    assert expected_arrival[-1] == late.evidence_id

    rebuilt = _reopen_from_zero(tmp_path)
    assert [record.evidence_id for record in rebuilt.query(order_by="arrival", limit=100)] == (
        expected_arrival
    )
    assert rebuilt.get(late.evidence_id) is not None


def test_compact_spool_excludes_refreshable_rows_written_after_the_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The refreshable-usage spool is append-only too, and a live watcher keeps
    # reconciling while the candidate is built. Those records reach the candidate
    # through the delta copy at swap time, not through the scan, so the rows they
    # project — a batch receipt, a revision, a head, its transitions — exist only
    # in live until then. A containment check that ignored that refused a correct
    # compaction, which is exactly what the real store's second aborted --write
    # run hit (fifteen rows, all written during the run).
    store, dropped_ids = _compactable_store(tmp_path)
    late = _evidence("late-arrival")
    original_filter = EvidenceStore._filter_spool_snapshot

    def filter_then_reconcile(self: EvidenceStore, **kwargs: object) -> object:
        plan = original_filter(self, **kwargs)
        # An insert, then an update, then a watermark (same content, newer order)
        # — the last one matters most: its transition and its head change without
        # naming any post-snapshot evidence id, so only the receipt they came from
        # marks them as post-snapshot. Between them, a main-spool append.
        self.reconcile_refreshable_usage(
            (_refreshable_usage_item("slot-late", value=1, source_order=1),)
        )
        record = self._spool_record(kind="evidence", payload=late.to_dict())
        offset = self._append_spool_record(record)
        self._project_evidence_record(record, offset)
        self._set_replay_offset(self.spool_path.stat().st_size)
        self.reconcile_refreshable_usage(
            (_refreshable_usage_item("slot-late", value=2, source_order=2),)
        )
        # A watermark on the *pre-snapshot* slot: it advances that slot's head and
        # adds a transition without naming any post-snapshot evidence id, so only
        # the receipt those rows came from marks them as post-snapshot.
        assert store.refreshable_usage_heads()[0].slot_key == "slot-a"
        watermarked = self.reconcile_refreshable_usage(
            (_refreshable_usage_item("slot-a", value=10, source_order=2),)
        )
        assert watermarked.watermarked == 1
        return plan

    monkeypatch.setattr(EvidenceStore, "_filter_spool_snapshot", filter_then_reconcile)

    result = store.compact_spool(dry_run=False, archive=False)

    assert result.swapped is True
    assert result.dropped_rows == len(dropped_ids)
    assert result.verification["equivalent"] is True
    assert result.verification["mismatches"] == {}
    # Both reconciles survived, on top of the pre-snapshot one.
    assert [head.slot_key for head in store.refreshable_usage_heads()] == ["slot-a", "slot-late"]
    assert store.refreshable_usage_stats().heads == 2
    assert store.get(late.evidence_id) is not None
    # And the swapped spools still replay to exactly what the store answers now:
    # the post-snapshot records were copied with their fences translated.
    expected = _readable_projection(store)
    rebuilt = _reopen_from_zero(tmp_path)
    assert _readable_projection(rebuilt) == expected
    assert [head.slot_key for head in rebuilt.refreshable_usage_heads()] == ["slot-a", "slot-late"]


def test_compact_spool_aborts_when_a_pre_snapshot_refreshable_row_is_missing(
    tmp_path: Path,
) -> None:
    # The other side of the boundary. The exclusion covers rows the refreshable
    # spool received *after* the snapshot (at or past the length it had then, in
    # its own byte offsets); a receipt strictly before that boundary that no
    # rebuild can reproduce is a real loss the gate must still refuse to swap
    # over. Both are inserted here, one byte apart, to pin the boundary.
    store, _ = _compactable_store(tmp_path)
    snapshot_length = store.refreshable_usage_spool_path.stat().st_size
    with store._connection() as connection:
        for receipt_id, offset in (
            ("rrb_after_the_snapshot", snapshot_length),
            ("rrb_before_the_snapshot", snapshot_length - 1),
        ):
            connection.execute(
                "INSERT INTO refreshable_usage_batch_receipts(receipt_id, spool_offset, received_at, "
                "complete, transition_count, inserted_count, updated_count, resurrected_count, "
                "watermarked_count, tombstoned_count, conflict_count) "
                "VALUES(?, ?, '2026-07-13T00:00:00.000000Z', 0, 0, 0, 0, 0, 0, 0, 0)",
                (receipt_id, offset),
            )
    spool = store.spool_path
    original = spool.read_bytes()

    result = store.compact_spool(dry_run=False, archive=True)

    assert result.swapped is False
    assert result.verification["outcome"] == "aborted"
    assert result.verification["equivalent"] is False
    missing = result.verification["mismatches"]["refreshable_usage_batch_receipts"]
    assert missing["live_rows_missing_from_the_rebuild"] == 1
    assert [row[0] for row in missing["sample"]] == ["rrb_before_the_snapshot"]
    assert spool.read_bytes() == original
    assert not list((tmp_path / "archive").glob("*"))


def test_compact_spool_refuses_to_swap_when_the_spool_was_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Another actor replacing the spool while the candidate is built must abort
    # the swap: the post-snapshot bytes would no longer be this snapshot's tail.
    store, _ = _compactable_store(tmp_path)
    spool = store.spool_path
    original_filter = EvidenceStore._filter_spool_snapshot

    def filter_then_replace(self: EvidenceStore, **kwargs: object) -> object:
        plan = original_filter(self, **kwargs)
        replacement = self.evidence_root / "replacement.jsonl"
        replacement.write_bytes(b'{"torn":\n')
        os.replace(replacement, self.spool_path)
        return plan

    monkeypatch.setattr(EvidenceStore, "_filter_spool_snapshot", filter_then_replace)

    result = store.compact_spool(dry_run=False, archive=True)

    assert result.swapped is False
    assert result.verification["outcome"] == "aborted"
    assert spool.read_bytes() == b'{"torn":\n'
    assert any("changed identity" in warning for warning in result.warnings)
    # An archive of a spool that was never replaced would collide with the next
    # attempt at the same generation, so the abort removes it again.
    assert result.archived_path is None
    assert result.archive_bytes == 0
    assert not list((tmp_path / "archive").glob("*"))


def test_compact_spool_preserves_a_torn_tail_and_its_error_row(tmp_path: Path) -> None:
    # A torn tail is invalid on both sides of the compaction, so its bytes and
    # its spool-error row survive the rewrite unchanged.
    store = EvidenceStore(tmp_path)
    for i in range(2):
        store.append(_tool_activity_shadow(f"ta-{i}"))
    store.append(_evidence("check-1", assertion="observed"))
    store.prune_versions(dry_run=False, vacuum=False)
    with store.spool_path.open("ab") as handle:
        handle.write(b'{"torn":')
    store.recover()
    assert store.stats().invalid_spool_records == 1
    before = _readable_projection(store)

    result = store.compact_spool(dry_run=False, archive=False)

    assert result.swapped is True
    assert result.dropped_rows == 2
    assert any("could not be classified" in warning for warning in result.warnings)
    assert store.spool_path.read_bytes().endswith(b'{"torn":')
    assert store.stats().invalid_spool_records == 1
    rebuilt = _reopen_from_zero(tmp_path)
    assert rebuilt.stats().invalid_spool_records == 1
    assert _readable_projection(rebuilt) == before
    assert store.spool_path.read_bytes() == rebuilt.spool_path.read_bytes()
