from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import agent_chronicle.canonical.migration_archive as migration_archive_module
from agent_chronicle.canonical.migration_archive import (
    EMPTY_ARCHIVE_HEAD,
    ArchiveHead,
    MigrationArchiveError,
    MigrationRecoveryReceipt,
    ReceiptCASMismatch,
    ReceiptConflictError,
    ReceiptDraft,
    ReceiptValidationError,
    RecoveryIdentity,
    VerifiedMigrationArchive,
    VerifiedRecoveryPlan,
    build_migration_archive,
    canonical_receipt_bytes,
    receipt_digest,
    scan_snapshot_lines,
)
from agent_chronicle.canonical.snapshot import VerifiedSnapshot


RULES_DIGEST = "a1" * 32
DESIGN_ID = "unscoped_legacy_recovery_v1"


def _entry(path: str, content: bytes) -> dict[str, object]:
    return {
        "path": path,
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _snapshot(tmp_path: Path, files: dict[str, bytes]) -> VerifiedSnapshot:
    root = tmp_path / "snapshot"
    root.mkdir(mode=0o700)
    for relative_path, content in files.items():
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    manifest = tmp_path / "snapshot-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "legacy-chronicle",
                "files": [_entry(path, files[path]) for path in sorted(files)],
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return VerifiedSnapshot.verify(root.resolve(), manifest.resolve())


def _archive(tmp_path: Path, snapshot: VerifiedSnapshot) -> VerifiedMigrationArchive:
    root = tmp_path / "archive"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    return build_migration_archive(snapshot=snapshot, archive_root=root.resolve())


def _draft(
    subject,
    *,
    disposition: str = "canonical_imported",
    donors=(),
    recovery: RecoveryIdentity | None = None,
    reason: str = "synthetic",
) -> ReceiptDraft:
    return ReceiptDraft(
        design_id=DESIGN_ID,
        subject=subject,
        disposition=disposition,
        classifier="synthetic-classifier",
        classifier_version="1",
        rule_version="legacy-client-log-recovery-v1",
        rules_digest=RULES_DIGEST,
        reason_codes=(reason,),
        donors=tuple(donors),
        recovery=recovery,
    )


def test_physical_inventory_conserves_crlf_blank_duplicate_and_final_line(
    tmp_path: Path,
) -> None:
    duplicate = b'{"same":1}\r\n'
    payload = duplicate + b"\n" + duplicate + b'{"malformed":\n' + b"tail"
    snapshot = _snapshot(tmp_path, {"events.jsonl": payload, "empty.jsonl": b""})

    inventory = scan_snapshot_lines(snapshot)

    assert inventory.total_bytes == len(payload)
    assert inventory.total_lines == 5
    event_lines = [line for line in inventory.lines if line.relative_path == "events.jsonl"]
    assert [line.byte_offset for line in event_lines] == [
        0,
        len(duplicate),
        len(duplicate) + 1,
        len(duplicate) * 2 + 1,
        len(duplicate) * 2 + 1 + len(b'{"malformed":\n'),
    ]
    assert [line.byte_length for line in event_lines] == [
        len(duplicate),
        1,
        len(duplicate),
        len(b'{"malformed":\n'),
        len(b"tail"),
    ]
    assert event_lines[0].raw_sha256 == event_lines[2].raw_sha256
    assert event_lines[0] != event_lines[2]
    assert next(item for item in inventory.files if item.relative_path == "empty.jsonl").line_count == 0


def test_archive_copies_exact_payload_and_reads_each_locator_from_both_sources(
    tmp_path: Path,
) -> None:
    payload = b'{"a":1}\r\nnot-json\n{"b":2}'
    snapshot = _snapshot(tmp_path, {"events.jsonl": payload})

    with _archive(tmp_path, snapshot) as archive:
        assert archive.inventory.total_bytes == len(payload)
        assert b"".join(
            archive.read_raw_many(archive.inventory.lines)[locator]
            for locator in archive.inventory.lines
        ) == payload
        assert b"".join(archive.read_raw(locator) for locator in archive.inventory.lines) == payload
        assert archive.head() == EMPTY_ARCHIVE_HEAD
        assert set(path.name for path in archive.path.iterdir()) == {
            "archive.lock",
            "archive-manifest-v1.json",
            "line-index-v1.jsonl",
            "payload-v1.bin",
        }


def test_archive_requires_absolute_empty_owner_only_disjoint_directory(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, {"events.jsonl": b"{}\n"})
    relative = Path("relative-archive")
    with pytest.raises(MigrationArchiveError, match="absolute"):
        build_migration_archive(snapshot=snapshot, archive_root=relative)

    broad = tmp_path / "broad"
    broad.mkdir(mode=0o755)
    os.chmod(broad, 0o755)
    with pytest.raises(MigrationArchiveError, match="owner-only"):
        build_migration_archive(snapshot=snapshot, archive_root=broad.resolve())

    nonempty = tmp_path / "nonempty"
    nonempty.mkdir(mode=0o700)
    (nonempty / "foreign").write_text("x", encoding="utf-8")
    with pytest.raises(MigrationArchiveError, match="must be empty"):
        build_migration_archive(snapshot=snapshot, archive_root=nonempty.resolve())

    inside = snapshot.root / "archive"
    inside.mkdir(mode=0o700)
    with pytest.raises(MigrationArchiveError, match="disjoint"):
        build_migration_archive(snapshot=snapshot, archive_root=inside.resolve())


def test_receipt_codec_is_compact_utf8_deterministic_and_domain_separated(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, {"events.jsonl": b'{"title":"caf\xc3\xa9"}\n'})
    locator = scan_snapshot_lines(snapshot).lines[0]
    draft = _draft(locator, reason="unicode-proof")
    receipt = MigrationRecoveryReceipt(
        archive_manifest_digest="b2" * 32,
        sequence=1,
        previous_receipt_digest=None,
        draft=draft,
    )

    encoded = canonical_receipt_bytes(receipt)

    assert b"caf" not in encoded  # receipt binds bytes by digest, not raw event content
    assert b"\n" not in encoded
    assert encoded == json.dumps(
        receipt.to_dict(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert receipt_digest(receipt) == hashlib.sha256(
        b"agent-chronicle-migration-recovery-receipt-v1\x00" + encoded
    ).hexdigest()


def test_receipt_schema_enforces_disposition_donor_and_recovery_cardinality(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, {"events.jsonl": b"{}\n{}\n"})
    first, second = scan_snapshot_lines(snapshot).lines
    identity = RecoveryIdentity(
        client="codex",
        source_namespace_fingerprint="private-source-home",
        client_session_id="session-1",
    )

    with pytest.raises(ReceiptValidationError, match="requires one donor"):
        _draft(first, disposition="candidate_recovery", recovery=identity)
    with pytest.raises(ReceiptValidationError, match="requires donors"):
        _draft(first, disposition="ambiguous")
    with pytest.raises(ReceiptValidationError, match="forbids donors"):
        _draft(first, disposition="no_proof", donors=(second,))
    with pytest.raises(ReceiptValidationError, match="may not donate to itself"):
        _draft(
            first,
            disposition="candidate_recovery",
            donors=(first,),
            recovery=identity,
        )
    with pytest.raises(ReceiptValidationError, match="must equal"):
        RecoveryIdentity(
            client="codex",
            source_namespace_fingerprint="private-source-home",
            client_session_id="session-1",
            link_confidence="exact",
        )
    with pytest.raises(ReceiptValidationError, match="be null"):
        ReceiptDraft(
            design_id=DESIGN_ID,
            subject=first,
            disposition="canonical_imported",
            classifier="synthetic-classifier",
            classifier_version="1",
            rule_version="legacy-client-log-recovery-v1",
            rules_digest=RULES_DIGEST,
            reason_codes=("synthetic",),
            supersedes_receipt_digest="ab" * 32,
        )


def test_receipt_publish_is_cas_chained_and_same_subject_identical_is_noop(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, {"events.jsonl": b"{}\n{}\n{}\n"})
    archive = _archive(tmp_path, snapshot)
    first, second, _third = archive.inventory.lines
    first_draft = _draft(first)
    try:
        published = archive.publish_receipt(first_draft, expected_head=EMPTY_ARCHIVE_HEAD)
        assert published.status == "published"
        assert published.receipt_sequence == 1
        assert archive.head() == published.head

        duplicate = archive.publish_receipt(first_draft, expected_head=EMPTY_ARCHIVE_HEAD)
        assert duplicate.status == "noop"
        assert duplicate.receipt_digest == published.receipt_digest
        assert duplicate.head == published.head
        assert len(archive.receipts()) == 1

        with pytest.raises(ReceiptConflictError, match="different receipt"):
            archive.publish_receipt(
                _draft(first, disposition="canonical_no_effect", reason="different"),
                expected_head=published.head,
            )
        with pytest.raises(ReceiptCASMismatch, match="stale"):
            archive.publish_receipt(_draft(second), expected_head=EMPTY_ARCHIVE_HEAD)

        second_result = archive.publish_receipt(_draft(second), expected_head=published.head)
        assert second_result.receipt_sequence == 2
        chain = archive.receipts()
        assert chain[1][0].previous_receipt_digest == chain[0][1]
    finally:
        archive.close()


def test_batch_publish_seals_complete_plan_and_exposes_only_candidate_decisions(
    tmp_path: Path,
) -> None:
    rows = (
        b'{"event_id":"evt_subject"}\n'
        b'{"event_id":"evt_donor"}\n'
        b"not-json\n"
    )
    snapshot = _snapshot(tmp_path, {"events.jsonl": rows})
    archive = _archive(tmp_path, snapshot)
    subject, donor, invalid = archive.inventory.lines
    identity = RecoveryIdentity(
        client="codex",
        source_namespace_fingerprint="private-source-home",
        client_session_id="session-1",
    )
    drafts = (
        _draft(
            subject,
            disposition="candidate_recovery",
            donors=(donor,),
            recovery=identity,
            reason="unique-log-donor",
        ),
        _draft(donor),
        _draft(invalid, disposition="invalid_retained", reason="malformed-jsonl"),
    )
    try:
        with pytest.raises(MigrationArchiveError, match="every physical line"):
            archive.sealed_plan(design_id=DESIGN_ID)
        head = archive.publish_receipts(tuple(reversed(drafts)))
        assert head.sequence == 3
        assert archive.publish_receipts(drafts) == head
        assert len(archive.receipts()) == 3
        assert [receipt.draft.subject for receipt, _digest in archive.receipts()] == [
            subject,
            donor,
            invalid,
        ]

        plan = archive.sealed_plan(design_id=DESIGN_ID)
        assert len(plan) == 3
        assert list(plan.iter_lines()) == [
            (subject, "candidate_recovery"),
            (donor, "canonical_imported"),
            (invalid, "invalid_retained"),
        ]
        decision = plan.recovery_for(subject)
        assert decision is not None
        assert decision.donor == donor
        assert decision.recovery.link_confidence == "high"
        assert plan.recovery_for(donor) is None
        assert plan.read_event(decision)["event_id"] == "evt_subject"
        assert plan.verify_unchanged() is plan
        with pytest.raises(TypeError, match="from_archive"):
            VerifiedRecoveryPlan(
                archive=archive,
                design_id=DESIGN_ID,
                head=ArchiveHead(3, head.digest),
                receipts={},
            )
    finally:
        archive.close()


def test_receipt_or_static_payload_tamper_is_detected(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, {"events.jsonl": b"{}\n{}\n"})
    archive = _archive(tmp_path, snapshot)
    try:
        archive.publish_receipt(_draft(archive.inventory.lines[0]), expected_head=EMPTY_ARCHIVE_HEAD)
        receipt_path = next(archive.path.glob("receipt-*.json"))
        raw = bytearray(receipt_path.read_bytes())
        raw[-1] = ord(" ")
        receipt_path.write_bytes(raw)
        os.chmod(receipt_path, 0o600)
        with pytest.raises((MigrationArchiveError, ReceiptValidationError)):
            archive.receipts()
    finally:
        archive.close()

    clean_root = tmp_path / "clean-archive"
    clean_root.mkdir(mode=0o700)
    clean = build_migration_archive(snapshot=snapshot, archive_root=clean_root.resolve())
    try:
        payload_path = clean.path / "payload-v1.bin"
        payload = bytearray(payload_path.read_bytes())
        payload[0] ^= 1
        payload_path.write_bytes(payload)
        os.chmod(payload_path, 0o600)
        with pytest.raises(MigrationArchiveError, match="changed"):
            clean.verify_unchanged()
    finally:
        clean.close()


def test_interrupted_no_replace_publish_keeps_complete_receipt_as_valid_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(tmp_path, {"events.jsonl": b"{}\n"})
    archive = _archive(tmp_path, snapshot)
    real_unlink = migration_archive_module.os.unlink

    def interrupt_staged_unlink(
        path: str | bytes,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if str(path).startswith(".receipt-"):
            raise KeyboardInterrupt
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(migration_archive_module.os, "unlink", interrupt_staged_unlink)
    try:
        with pytest.raises(KeyboardInterrupt):
            archive.publish_receipt(
                _draft(archive.inventory.lines[0]),
                expected_head=EMPTY_ARCHIVE_HEAD,
            )

        chain = archive.receipts()
        assert len(chain) == 1
        assert archive.head() == ArchiveHead(1, chain[0][1])
        assert len(list(archive.path.glob(".receipt-*.staged-*"))) == 1
    finally:
        archive.close()
