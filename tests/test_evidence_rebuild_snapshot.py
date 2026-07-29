from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

import agentacct.evidence_rebuild_snapshot as snapshot_module
from agentacct.evidence import canonical_digest, canonical_json_bytes
from agentacct.evidence_rebuild_activation import fingerprint_evidence_tree
from agentacct.evidence_rebuild_snapshot import (
    SnapshotDriftError,
    SnapshotIntegrityError,
    SnapshotSafetyError,
    VerifiedEvidenceSnapshot,
    create_evidence_snapshot,
    rehearse_evidence_snapshot_restore,
    verify_evidence_snapshot,
)


COMMIT = "a" * 40
STORE_UUID = "4ced52f1-76c6-4ba5-b67f-b423506b04d6"


def _spool_record(*, refreshable: bool, fence: int = 0) -> bytes:
    body: dict[str, object] = {
        "spool_schema_version": (
            "agent-chronicle.refreshable-usage-spool.v1"
            if refreshable
            else "agent-chronicle.evidence-spool.v1"
        ),
        "receipt_id": "rcp_refresh" if refreshable else "rcp_main",
        "received_at": "2026-07-23T00:00:00.000000Z",
        "kind": "refreshable_usage" if refreshable else "evidence",
        "payload": {"complete": True, "transitions": []} if refreshable else {"fixture": True},
    }
    if refreshable:
        body["main_spool_fence"] = fence
    return canonical_json_bytes({**body, "record_hash": canonical_digest(body)}) + b"\n"


def _create_sqlite(path: Path, statements: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(statements)
        connection.commit()
    finally:
        connection.close()


def _build_source(
    root: Path,
    *,
    main_raw: bytes | None = None,
    refresh_raw: bytes | None = None,
    main_cursor: int | None = None,
    refresh_cursor: int | None = None,
    include_refresh_spool: bool = True,
) -> Path:
    root.mkdir(parents=True)
    (root / "events.jsonl").write_bytes(b'{"event_type":"fixture"}\n')
    (root / "events.jsonl.lock").write_bytes(b"")
    evidence = root / "evidence-v2"
    evidence.mkdir()
    (evidence / ".spool.lock").write_bytes(b"")
    if main_raw is None:
        main_raw = _spool_record(refreshable=False)
    (evidence / "spool.jsonl").write_bytes(main_raw)
    if include_refresh_spool:
        if refresh_raw is None:
            refresh_raw = _spool_record(refreshable=True, fence=len(main_raw))
        (evidence / "refreshable-usage.jsonl").write_bytes(refresh_raw)
    elif refresh_raw is not None:
        raise ValueError("refresh_raw requires include_refresh_spool=True")
    if main_cursor is None:
        main_cursor = len(main_raw)
    if refresh_cursor is None:
        refresh_cursor = len(refresh_raw) if refresh_raw is not None else 0
    _create_sqlite(
        evidence / "projection.sqlite3",
        f"""
        PRAGMA foreign_keys = ON;
        CREATE TABLE store_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO store_metadata VALUES('schema_version', 'agent-chronicle.evidence-store.v1');
        INSERT INTO store_metadata VALUES('replay_offset', '{main_cursor}');
        INSERT INTO store_metadata VALUES('refreshable_usage_replay_offset', '{refresh_cursor}');
        """,
    )
    _create_sqlite(
        root / "chronicle.sqlite3",
        f"""
        PRAGMA foreign_keys = ON;
        CREATE TABLE store_metadata(
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            store_uuid TEXT NOT NULL UNIQUE,
            schema_version INTEGER NOT NULL
        );
        INSERT INTO store_metadata VALUES(1, '{STORE_UUID}', 4);
        """,
    )
    empty = root / "empty-directory"
    empty.mkdir()
    for directory in (root, *(path for path in root.rglob("*") if path.is_dir())):
        directory.chmod(0o700)
    for path in (path for path in root.rglob("*") if path.is_file()):
        path.chmod(0o600)
    return root


def _tree_content_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _identities(root: Path) -> dict[str, tuple[int, int, int, int, int]]:
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_dev,
            path.stat().st_ino,
            path.stat().st_size,
            path.stat().st_mtime_ns,
            path.stat().st_ctime_ns,
        )
        for path in sorted(root.rglob("*"))
    }


def test_snapshot_happy_path_is_private_manifested_verified_and_restorable(tmp_path: Path) -> None:
    source = _build_source(tmp_path / "source")
    source_hashes = _tree_content_hashes(source)

    result = create_evidence_snapshot(
        source,
        tmp_path / "snapshot",
        confirm_writers_stopped=True,
        deployed_commit=COMMIT,
    )
    verification = verify_evidence_snapshot(result.snapshot_root)
    restore = rehearse_evidence_snapshot_restore(result.snapshot_root, tmp_path / "restore")
    verified = VerifiedEvidenceSnapshot.verify(result.snapshot_root)
    with verified.open_binary("events.jsonl") as handle:
        assert handle.read() == b'{"event_type":"fixture"}\n'
    with pytest.raises(SnapshotIntegrityError, match="not declared"):
        with verified.open_binary("missing.jsonl"):
            pass

    assert json.loads(json.dumps(result.to_dict()))["snapshot_root"] == str(tmp_path / "snapshot")
    assert verification.canonical_store_uuid == STORE_UUID
    assert verification.canonical_schema_version == 4
    assert verification.sqlite_files_checked == 2
    assert verification.main_spool_records == 1
    assert verification.main_spool_bytes > 0
    assert verification.refreshable_spool_present is True
    assert verification.refreshable_spool_records == 1
    assert verification.refreshable_spool_bytes > 0
    assert restore.source_manifest_sha256 == verification.manifest_sha256
    assert restore.canonical_store_uuid == STORE_UUID
    assert _tree_content_hashes(source) == source_hashes

    manifest = json.loads(result.manifest_path.read_text())
    receipt = json.loads(result.receipt_path.read_text())
    assert manifest["deployed_commit"] == COMMIT
    assert manifest["source_root"] == str(source.resolve())
    assert manifest["snapshot_root"] == str((tmp_path / "snapshot").resolve())
    assert manifest["canonical_store"] == {
        "path": "chronicle.sqlite3",
        "schema_version": 4,
        "store_uuid": STORE_UUID,
    }
    assert receipt["manifest_sha256"] == hashlib.sha256(result.manifest_path.read_bytes()).hexdigest()
    assert receipt["tree_digest"] == manifest["tree_digest"]
    assert receipt["live_evidence_tree"] == manifest["live_evidence_tree"]
    assert receipt["main_spool_bytes"] == verification.main_spool_bytes
    assert receipt["refreshable_spool_present"] is True
    assert receipt["refreshable_spool_bytes"] == verification.refreshable_spool_bytes
    assert manifest["live_evidence_tree"] == fingerprint_evidence_tree(
        source / "evidence-v2"
    ).to_dict()
    assert manifest["live_evidence_tree"]["tree_sha256"]
    restore_receipt = json.loads(restore.receipt_path.read_text())
    assert restore_receipt["restore_steps"] == [
        "stop_all_dashboard_watcher_mcp_and_hook_writers",
        "verify_restore_target_is_new_private_and_disjoint",
        "preserve_failed_live_store_aside_without_deleting_it",
        "copy_store_tree_from_sealed_snapshot",
        "recompute_sha256_tree_digest_sqlite_spool_fence_and_cursor_checks",
        "restart_with_the_preincident_runtime_flags",
        "verify_health_issues_empty_and_observe_multiple_complete_watcher_ticks",
    ]
    assert stat.S_IMODE(result.snapshot_root.stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
        for path in result.snapshot_root.rglob("*")
    )
    assert stat.S_IMODE(restore.restore_root.stat().st_mode) == 0o700


def test_snapshot_accepts_legacy_absent_refreshable_spool_only_with_empty_state(
    tmp_path: Path,
) -> None:
    source = _build_source(tmp_path / "source", include_refresh_spool=False)

    result = create_evidence_snapshot(
        source,
        tmp_path / "snapshot",
        confirm_writers_stopped=True,
        deployed_commit=COMMIT,
    )
    verification = verify_evidence_snapshot(result.snapshot_root)
    restore = rehearse_evidence_snapshot_restore(result.snapshot_root, tmp_path / "restore")

    assert verification.main_spool_records == 1
    assert verification.main_spool_bytes > 0
    assert verification.refreshable_spool_present is False
    assert verification.refreshable_spool_records == 0
    assert verification.refreshable_spool_bytes == 0
    assert verification.refreshable_replay_offset == 0
    manifest = json.loads(result.manifest_path.read_text())
    manifest_paths = {item["path"] for item in manifest["files"]}
    assert "evidence-v2/spool.jsonl" in manifest_paths
    assert "evidence-v2/projection.sqlite3" in manifest_paths
    assert "evidence-v2/refreshable-usage.jsonl" not in manifest_paths
    evidence_entries = {
        item["relative_path"] for item in manifest["live_evidence_tree"]["entries"]
    }
    assert "refreshable-usage.jsonl" not in evidence_entries
    receipt = json.loads(result.receipt_path.read_text())
    assert receipt["refreshable_spool_present"] is False
    assert receipt["refreshable_spool_records"] == 0
    assert receipt["refreshable_spool_bytes"] == 0
    restore_receipt = json.loads(restore.receipt_path.read_text())
    assert restore_receipt["refreshable_spool_present"] is False
    assert restore_receipt["refreshable_spool_records"] == 0
    assert restore_receipt["refreshable_spool_bytes"] == 0
    assert not (
        result.snapshot_root / "store" / "evidence-v2" / "refreshable-usage.jsonl"
    ).exists()
    assert not (
        restore.restore_root / "store" / "evidence-v2" / "refreshable-usage.jsonl"
    ).exists()


def test_snapshot_rejects_absent_refreshable_spool_with_nonzero_cursor(
    tmp_path: Path,
) -> None:
    source = _build_source(
        tmp_path / "source",
        include_refresh_spool=False,
        refresh_cursor=1,
    )

    with pytest.raises(
        SnapshotIntegrityError,
        match="refreshable_usage_replay_offset must be zero",
    ):
        create_evidence_snapshot(
            source,
            tmp_path / "snapshot",
            confirm_writers_stopped=True,
            deployed_commit=COMMIT,
        )


def test_snapshot_rejects_absent_refreshable_spool_with_projected_refresh_state(
    tmp_path: Path,
) -> None:
    source = _build_source(tmp_path / "source", include_refresh_spool=False)
    projection = source / "evidence-v2" / "projection.sqlite3"
    connection = sqlite3.connect(projection)
    try:
        connection.executescript(
            """
            CREATE TABLE refreshable_usage_batch_receipts(receipt_id TEXT PRIMARY KEY);
            INSERT INTO refreshable_usage_batch_receipts VALUES('rcp_orphaned');
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SnapshotIntegrityError, match="refreshable projection state"):
        create_evidence_snapshot(
            source,
            tmp_path / "snapshot",
            confirm_writers_stopped=True,
            deployed_commit=COMMIT,
        )


def test_snapshot_with_present_refreshable_spool_reports_strict_path_bytes(
    tmp_path: Path,
) -> None:
    source = _build_source(tmp_path / "source")

    result = create_evidence_snapshot(
        source,
        tmp_path / "snapshot",
        confirm_writers_stopped=True,
        deployed_commit=COMMIT,
    )

    refresh_path = source / "evidence-v2" / "refreshable-usage.jsonl"
    assert result.verification.refreshable_spool_present is True
    assert result.verification.refreshable_spool_records == 1
    assert result.verification.refreshable_spool_bytes == refresh_path.stat().st_size


def test_verify_accepts_legacy_v1_receipt_without_spool_presence_fields(
    tmp_path: Path,
) -> None:
    source = _build_source(tmp_path / "source")
    result = create_evidence_snapshot(
        source,
        tmp_path / "snapshot",
        confirm_writers_stopped=True,
        deployed_commit=COMMIT,
    )
    receipt = json.loads(result.receipt_path.read_text())
    for key in (
        "main_spool_bytes",
        "refreshable_spool_present",
        "refreshable_spool_bytes",
    ):
        receipt.pop(key)
    receipt_body = {key: value for key, value in receipt.items() if key != "receipt_hash"}
    receipt["receipt_hash"] = canonical_digest(receipt_body)
    result.receipt_path.write_bytes(canonical_json_bytes(receipt) + b"\n")

    verification = verify_evidence_snapshot(result.snapshot_root)

    assert verification.refreshable_spool_present is True
    assert verification.refreshable_spool_bytes > 0


def test_snapshot_requires_acknowledgement_valid_commit_and_new_safe_target(tmp_path: Path) -> None:
    source = _build_source(tmp_path / "source")
    with pytest.raises(SnapshotSafetyError, match="confirm_writers_stopped"):
        create_evidence_snapshot(
            source,
            tmp_path / "snapshot-a",
            confirm_writers_stopped=False,
            deployed_commit=COMMIT,
        )
    with pytest.raises(SnapshotSafetyError, match="deployed_commit"):
        create_evidence_snapshot(
            source,
            tmp_path / "snapshot-b",
            confirm_writers_stopped=True,
            deployed_commit="short",
        )
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(SnapshotSafetyError, match="must not already exist"):
        create_evidence_snapshot(
            source,
            existing,
            confirm_writers_stopped=True,
            deployed_commit=COMMIT,
        )
    with pytest.raises(SnapshotSafetyError, match="disjoint"):
        create_evidence_snapshot(
            source,
            source / "nested-snapshot",
            confirm_writers_stopped=True,
            deployed_commit=COMMIT,
        )


def test_snapshot_refuses_an_observed_active_writer_lock(tmp_path: Path) -> None:
    source = _build_source(tmp_path / "source")
    descriptor = os.open(source / "events.jsonl.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SnapshotSafetyError, match="writer lock is active"):
            create_evidence_snapshot(
                source,
                tmp_path / "snapshot",
                confirm_writers_stopped=True,
                deployed_commit=COMMIT,
            )
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_snapshot_refuses_non_private_writer_lock_even_with_confirmation(tmp_path: Path) -> None:
    source = _build_source(tmp_path / "source")
    (source / "events.jsonl.lock").chmod(0o640)
    with pytest.raises(SnapshotSafetyError, match="identity/ownership/mode is unsafe"):
        create_evidence_snapshot(
            source,
            tmp_path / "snapshot",
            confirm_writers_stopped=True,
            deployed_commit=COMMIT,
        )


def test_snapshot_rejects_symlinks_hardlinks_and_missing_core_lock(tmp_path: Path) -> None:
    source = _build_source(tmp_path / "source-symlink")
    outside = tmp_path / "outside"
    outside.write_text("outside")
    (source / "link").symlink_to(outside)
    with pytest.raises(SnapshotSafetyError, match="symlink"):
        create_evidence_snapshot(
            source,
            tmp_path / "snapshot-symlink",
            confirm_writers_stopped=True,
            deployed_commit=COMMIT,
        )

    source = _build_source(tmp_path / "source-hardlink")
    os.link(source / "events.jsonl", source / "events-copy.jsonl")
    with pytest.raises(SnapshotSafetyError, match="hard-linked"):
        create_evidence_snapshot(
            source,
            tmp_path / "snapshot-hardlink",
            confirm_writers_stopped=True,
            deployed_commit=COMMIT,
        )

    source = _build_source(tmp_path / "source-no-lock")
    (source / "evidence-v2" / ".spool.lock").unlink()
    with pytest.raises(SnapshotSafetyError, match="required lock files are missing"):
        create_evidence_snapshot(
            source,
            tmp_path / "snapshot-no-lock",
            confirm_writers_stopped=True,
            deployed_commit=COMMIT,
        )


def test_snapshot_detects_source_drift_and_cleans_only_its_new_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _build_source(tmp_path / "source")
    original = snapshot_module._stream_copy
    changed = False

    def mutate_after_copy(path: Path, target: Path, expected: object) -> str:
        nonlocal changed
        digest = original(path, target, expected)  # type: ignore[arg-type]
        if path.name == "events.jsonl" and not changed:
            path.write_bytes(path.read_bytes() + b'{"changed":true}\n')
            changed = True
        return digest

    monkeypatch.setattr(snapshot_module, "_stream_copy", mutate_after_copy)
    target = tmp_path / "snapshot"
    with pytest.raises(SnapshotDriftError, match="source tree changed"):
        create_evidence_snapshot(
            source,
            target,
            confirm_writers_stopped=True,
            deployed_commit=COMMIT,
        )
    assert not target.exists()
    assert not list(tmp_path.glob(".snapshot.snapshot-stage-*"))


def test_snapshot_publication_never_replaces_a_concurrently_created_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _build_source(tmp_path / "source")
    target = tmp_path / "snapshot"
    original = snapshot_module._fsync_tree
    raced = False
    racing_inode: tuple[int, int] | None = None

    def create_racing_target(stage: Path) -> None:
        nonlocal raced, racing_inode
        original(stage)
        if not raced and ".snapshot.snapshot-stage-" in stage.name:
            target.mkdir(mode=0o700)
            target_stat = target.stat()
            racing_inode = (target_stat.st_dev, target_stat.st_ino)
            raced = True

    monkeypatch.setattr(snapshot_module, "_fsync_tree", create_racing_target)
    with pytest.raises(SnapshotSafetyError, match="concurrently created"):
        create_evidence_snapshot(
            source,
            target,
            confirm_writers_stopped=True,
            deployed_commit=COMMIT,
        )

    assert racing_inode is not None
    target_stat = target.stat()
    assert (target_stat.st_dev, target_stat.st_ino) == racing_inode
    assert list(target.iterdir()) == []
    assert not list(tmp_path.glob(".snapshot.snapshot-stage-*"))


def test_restore_publication_never_replaces_a_concurrently_created_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _build_source(tmp_path / "source")
    sealed = create_evidence_snapshot(
        source,
        tmp_path / "snapshot",
        confirm_writers_stopped=True,
        deployed_commit=COMMIT,
    )
    target = tmp_path / "restore"
    original = snapshot_module._fsync_tree
    raced = False
    racing_inode: tuple[int, int] | None = None

    def create_racing_target(stage: Path) -> None:
        nonlocal raced, racing_inode
        original(stage)
        if not raced and ".restore.restore-stage-" in stage.name:
            target.mkdir(mode=0o700)
            target_stat = target.stat()
            racing_inode = (target_stat.st_dev, target_stat.st_ino)
            raced = True

    monkeypatch.setattr(snapshot_module, "_fsync_tree", create_racing_target)
    with pytest.raises(SnapshotSafetyError, match="concurrently created"):
        rehearse_evidence_snapshot_restore(sealed.snapshot_root, target)

    assert racing_inode is not None
    target_stat = target.stat()
    assert (target_stat.st_dev, target_stat.st_ino) == racing_inode
    assert list(target.iterdir()) == []
    assert not list(tmp_path.glob(".restore.restore-stage-*"))


def test_snapshot_and_restore_require_owner_only_target_parents(tmp_path: Path) -> None:
    source = _build_source(tmp_path / "source")
    shared_snapshot_parent = tmp_path / "shared-snapshots"
    shared_snapshot_parent.mkdir(mode=0o700)
    shared_snapshot_parent.chmod(0o755)

    with pytest.raises(SnapshotSafetyError, match="mode 0700"):
        create_evidence_snapshot(
            source,
            shared_snapshot_parent / "snapshot",
            confirm_writers_stopped=True,
            deployed_commit=COMMIT,
        )

    sealed = create_evidence_snapshot(
        source,
        tmp_path / "sealed",
        confirm_writers_stopped=True,
        deployed_commit=COMMIT,
    )
    shared_restore_parent = tmp_path / "shared-restores"
    shared_restore_parent.mkdir(mode=0o700)
    shared_restore_parent.chmod(0o755)

    with pytest.raises(SnapshotSafetyError, match="mode 0700"):
        rehearse_evidence_snapshot_restore(
            sealed.snapshot_root,
            shared_restore_parent / "restore",
        )


@pytest.mark.parametrize(
    ("variant", "message"),
    [
        ("truncated-main", "truncated/non-newline tail"),
        ("bad-fence", "not a main spool record boundary"),
        ("bad-main-cursor", "replay_offset is not a main spool record boundary"),
        ("bad-refresh-cursor", "refreshable_usage_replay_offset"),
    ],
)
def test_snapshot_creation_rejects_spool_corruption_and_bad_boundaries(
    tmp_path: Path,
    variant: str,
    message: str,
) -> None:
    main = _spool_record(refreshable=False)
    refresh = _spool_record(refreshable=True, fence=len(main))
    main_cursor: int | None = None
    refresh_cursor: int | None = None
    if variant == "truncated-main":
        main = main.rstrip(b"\n")
        refresh = _spool_record(refreshable=True, fence=len(main))
    elif variant == "bad-fence":
        refresh = _spool_record(refreshable=True, fence=1)
    elif variant == "bad-main-cursor":
        main_cursor = 1
    elif variant == "bad-refresh-cursor":
        refresh_cursor = 1
    source = _build_source(
        tmp_path / "source",
        main_raw=main,
        refresh_raw=refresh,
        main_cursor=main_cursor,
        refresh_cursor=refresh_cursor,
    )
    with pytest.raises(SnapshotIntegrityError, match=message):
        create_evidence_snapshot(
            source,
            tmp_path / "snapshot",
            confirm_writers_stopped=True,
            deployed_commit=COMMIT,
        )


def test_snapshot_tolerates_preserved_invalid_spool_record_and_counts_it(
    tmp_path: Path,
) -> None:
    """A store that preserved a torn/invalid record must stay snapshottable.

    The live EvidenceStore keeps damaged lines verbatim and keeps operating
    (they are counted in ``invalid_spool_records``); the DR entry gate must
    mirror that damage model instead of refusing the store forever.
    """

    main = _spool_record(refreshable=False)
    value = json.loads(main)
    value["record_hash"] = "sha256:" + "0" * 64
    main = canonical_json_bytes(value) + b"\n"
    refresh = _spool_record(refreshable=True, fence=len(main))
    source = _build_source(tmp_path / "source", main_raw=main, refresh_raw=refresh)

    sealed = create_evidence_snapshot(
        source,
        tmp_path / "snapshot",
        confirm_writers_stopped=True,
        deployed_commit=COMMIT,
    )

    assert sealed.verification.main_spool_records == 0
    assert sealed.verification.main_spool_invalid_records == 1
    assert sealed.verification.refreshable_spool_records == 1
    assert sealed.verification.refreshable_spool_invalid_records == 0
    reverified = verify_evidence_snapshot(sealed.snapshot_root)
    assert reverified.main_spool_invalid_records == 1


def test_snapshot_accepts_refresh_only_store_without_main_spool(
    tmp_path: Path,
) -> None:
    """A refresh-only store never creates spool.jsonl; DR must still work."""

    refresh = _spool_record(refreshable=True, fence=0)
    source = _build_source(
        tmp_path / "source",
        main_raw=b"",
        refresh_raw=refresh,
        main_cursor=0,
    )
    (source / "evidence-v2" / "spool.jsonl").unlink()

    sealed = create_evidence_snapshot(
        source,
        tmp_path / "snapshot",
        confirm_writers_stopped=True,
        deployed_commit=COMMIT,
    )

    assert sealed.verification.main_spool_present is False
    assert sealed.verification.main_spool_records == 0
    assert sealed.verification.main_spool_bytes == 0
    assert sealed.verification.refreshable_spool_records == 1
    reverified = verify_evidence_snapshot(sealed.snapshot_root)
    assert reverified.main_spool_present is False


def test_snapshot_creation_rejects_sqlite_corruption(tmp_path: Path) -> None:
    source = _build_source(tmp_path / "source")
    (source / "chronicle.sqlite3").write_bytes(b"not a sqlite database")
    with pytest.raises(SnapshotIntegrityError, match="SQLite"):
        create_evidence_snapshot(
            source,
            tmp_path / "snapshot",
            confirm_writers_stopped=True,
            deployed_commit=COMMIT,
        )


def test_spool_validation_is_streaming_and_never_uses_path_read_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _build_source(tmp_path / "source")
    original = Path.read_bytes

    def reject_spool_read_bytes(path: Path) -> bytes:
        if path.name in {"spool.jsonl", "refreshable-usage.jsonl"}:
            raise AssertionError("spool validation must stream")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", reject_spool_read_bytes)
    result = create_evidence_snapshot(
        source,
        tmp_path / "snapshot",
        confirm_writers_stopped=True,
        deployed_commit=COMMIT,
    )
    assert result.verification.main_spool_records == 1


def test_sqlite_wal_is_validated_on_temp_copy_without_touching_sealed_snapshot(
    tmp_path: Path,
) -> None:
    source = _build_source(tmp_path / "source")
    canonical = source / "chronicle.sqlite3"
    connection = sqlite3.connect(canonical)
    wal_uuid = "6f4d44ce-f82d-4d9b-8e72-f73072883d6f"
    try:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        connection.execute("PRAGMA wal_autocheckpoint = 0")
        connection.execute("UPDATE store_metadata SET store_uuid = ?", (wal_uuid,))
        connection.commit()
        assert canonical.with_name("chronicle.sqlite3-wal").stat().st_size > 0
        for sidecar in (
            canonical.with_name("chronicle.sqlite3-wal"),
            canonical.with_name("chronicle.sqlite3-shm"),
        ):
            sidecar.chmod(0o600)
        result = create_evidence_snapshot(
            source,
            tmp_path / "snapshot",
            confirm_writers_stopped=True,
            deployed_commit=COMMIT,
        )
        assert result.verification.canonical_store_uuid == wal_uuid
        before_hashes = _tree_content_hashes(result.snapshot_root)
        before_identities = _identities(result.snapshot_root)
        assert verify_evidence_snapshot(result.snapshot_root).canonical_store_uuid == wal_uuid
        assert _tree_content_hashes(result.snapshot_root) == before_hashes
        assert _identities(result.snapshot_root) == before_identities
    finally:
        connection.close()


def test_verify_is_read_only_and_detects_corruption_and_permissions(tmp_path: Path) -> None:
    source = _build_source(tmp_path / "source")
    result = create_evidence_snapshot(
        source,
        tmp_path / "snapshot",
        confirm_writers_stopped=True,
        deployed_commit=COMMIT,
    )
    before_hashes = _tree_content_hashes(result.snapshot_root)
    before_identities = _identities(result.snapshot_root)
    verify_evidence_snapshot(result.snapshot_root)
    assert _tree_content_hashes(result.snapshot_root) == before_hashes
    assert _identities(result.snapshot_root) == before_identities

    spool = result.snapshot_root / "store" / "evidence-v2" / "spool.jsonl"
    spool.write_bytes(spool.read_bytes()[:-1])
    with pytest.raises(SnapshotIntegrityError, match="identity mismatch|SHA-256 mismatch"):
        verify_evidence_snapshot(result.snapshot_root)

    second = create_evidence_snapshot(
        source,
        tmp_path / "snapshot-permission",
        confirm_writers_stopped=True,
        deployed_commit=COMMIT,
    )
    projection = second.snapshot_root / "store" / "evidence-v2" / "projection.sqlite3"
    projection.chmod(0o644)
    with pytest.raises(SnapshotIntegrityError, match="identity mismatch|owner-only"):
        verify_evidence_snapshot(second.snapshot_root)


def test_verify_rejects_manifest_or_receipt_tampering(tmp_path: Path) -> None:
    source = _build_source(tmp_path / "source")
    result = create_evidence_snapshot(
        source,
        tmp_path / "snapshot",
        confirm_writers_stopped=True,
        deployed_commit=COMMIT,
    )
    manifest = json.loads(result.manifest_path.read_text())
    manifest["deployed_commit"] = "b" * 40
    result.manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
    with pytest.raises(SnapshotIntegrityError, match="receipt does not bind|commit"):
        verify_evidence_snapshot(result.snapshot_root)

    second = create_evidence_snapshot(
        source,
        tmp_path / "snapshot-receipt",
        confirm_writers_stopped=True,
        deployed_commit=COMMIT,
    )
    receipt = json.loads(second.receipt_path.read_text())
    receipt["tree_digest"] = "sha256:" + "0" * 64
    second.receipt_path.write_bytes(canonical_json_bytes(receipt) + b"\n")
    with pytest.raises(SnapshotIntegrityError, match="receipt hash mismatch"):
        verify_evidence_snapshot(second.snapshot_root)
