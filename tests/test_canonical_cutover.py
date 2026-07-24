from __future__ import annotations

import argparse
import errno
import hashlib
import importlib.util
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

from agent_chronicle.client_usage import ClientUsageEvent
from agent_chronicle.canonical.cutover import (
    CUTOVER_RECEIPT_VERSION,
    CutoverError,
    CutoverPostReplaceError,
    CutoverPreparationError,
    CutoverPromotionError,
    CutoverReceiptPersistenceError,
    RollbackPostReplaceError,
    load_promotion_receipt,
    prepare_cutover,
    promote_candidate,
    rollback_promotion,
    verify_promotion,
    write_promotion_receipt,
)
from agent_chronicle.canonical.legacy_import import (
    LEGACY_EVENTS_ADAPTER,
    LEGACY_EVENTS_REPRESENTATION,
)
from agent_chronicle.canonical.migration_disposition_policy import (
    build_migration_disposition_policy_evidence,
)
from agent_chronicle.canonical.product_parity import PRODUCT_PARITY_SCHEMA_VERSION
from agent_chronicle.canonical.sqlite import CanonicalStore
from agent_chronicle.canonical.types import SourceInstanceInput
from agent_chronicle.canonical_live import LIVE_EVENTS_ADAPTER
from agent_chronicle.usage_truth import mark_trusted_local_usage_import_event


_PARITY_RUNNER_PATH = (
    Path(__file__).resolve().parents[1]
    / "benchmarks"
    / "legacy_snapshot_parity.py"
)
_PARITY_RUNNER_SPEC = importlib.util.spec_from_file_location(
    "canonical_cutover_real_parity_runner",
    _PARITY_RUNNER_PATH,
)
assert _PARITY_RUNNER_SPEC is not None and _PARITY_RUNNER_SPEC.loader is not None
_PARITY_RUNNER = importlib.util.module_from_spec(_PARITY_RUNNER_SPEC)
sys.modules[_PARITY_RUNNER_SPEC.name] = _PARITY_RUNNER
_PARITY_RUNNER_SPEC.loader.exec_module(_PARITY_RUNNER)


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _read_store_uuid(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return str(
            connection.execute(
                "SELECT store_uuid FROM store_metadata WHERE singleton = 1"
            ).fetchone()[0]
        )
    finally:
        connection.close()


def _real_runner_snapshot(tmp_path: Path) -> tuple[Path, Path]:
    event = ClientUsageEvent(
        client="codex",
        client_session_id="cutover-real-runner-session",
        source_path=Path("/private/offline/cutover-source.jsonl"),
        title=None,
        cwd="/private/offline/cutover-project",
        model="gpt-cutover-runner-fixture",
        input_tokens=10,
        output_tokens=0,
        cached_input_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_tokens_reported=True,
        cache_read_tokens_reported=True,
        reasoning_output_tokens=0,
        provider_name="codex",
        started_at=1_780_000_000,
        updated_at=1_780_000_001,
        turn_count=1,
        source_namespace_fingerprint="b6" * 32,
        input_tokens_reported=True,
        output_tokens_reported=True,
        reasoning_output_tokens_reported=True,
        total_tokens=10,
        total_tokens_reported=True,
    ).to_sentinel_event()
    event.update({"event_id": "cutover-runner-event", "created_at": 1_780_000_000.0})
    event = mark_trusted_local_usage_import_event(event)
    payload = (
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    root = tmp_path / "sealed-legacy"
    root.mkdir()
    (root / "events.jsonl").write_bytes(payload)
    manifest = tmp_path / "legacy-chronicle-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "legacy-chronicle",
                "files": [
                    {
                        "path": "events.jsonl",
                        "size_bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return root.resolve(), manifest.resolve()


def _remove_clean_sqlite_sidecars(path: Path) -> None:
    wal = Path(f"{path}-wal")
    if wal.exists():
        assert wal.stat().st_size == 0
    for suffix in ("-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _install_clean_sqlite_sidecars(path: Path) -> tuple[Path, Path]:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ROLLBACK")
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert checkpoint is not None and checkpoint[0] == 0 and checkpoint[1] == 0
        wal = Path(f"{path}-wal")
        shm = Path(f"{path}-shm")
        wal_bytes = wal.read_bytes()
        shm_bytes = shm.read_bytes()
    finally:
        connection.close()
    wal.write_bytes(wal_bytes)
    shm.write_bytes(shm_bytes)
    wal.chmod(0o600)
    shm.chmod(0o600)
    return wal, shm


def _candidate(tmp_path: Path, *, name: str = "run") -> tuple[Path, Path, str]:
    run_dir = tmp_path / name
    run_dir.mkdir(mode=0o700)
    candidate = run_dir / "candidate.sqlite3"
    with CanonicalStore.create(candidate) as store:
        store.repository().get_or_create_source(
            SourceInstanceInput(
                client="codex",
                adapter=LEGACY_EVENTS_ADAPTER,
                representation=LEGACY_EVENTS_REPRESENTATION,
                namespace_digest=b"c" * 32,
                namespace_scheme="source-namespace-fingerprint-v1",
            )
        )
        store.repository().rebuild_minimal_read_models()
        store_uuid = str(
            store.connection.execute(
                "SELECT store_uuid FROM store_metadata WHERE singleton = 1"
            ).fetchone()[0]
        )
        store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    _remove_clean_sqlite_sidecars(candidate)
    report = run_dir / "parity.json"
    _write_report(candidate, report)
    return candidate, report, store_uuid


def _write_report(
    candidate: Path,
    report: Path,
    *,
    approved_exclusion_count: int = 0,
    mutate: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    manifest_sha256 = "a" * 64
    exclusions = (
        [
            {
                "reason": "unresolved_source_namespace",
                "disposition": "requires_choice",
                "count": approved_exclusion_count,
            }
        ]
        if approved_exclusion_count
        else []
    )
    migration_policy = build_migration_disposition_policy_evidence(
        snapshot_manifest_sha256=manifest_sha256,
        issues=exclusions,
        source_lines_seen=approved_exclusion_count,
        importer_lines_seen=approved_exclusion_count,
        parsed_events=0,
        malformed_or_excluded_lines=approved_exclusion_count,
        issue_lines=approved_exclusion_count,
        excluded_lines=approved_exclusion_count,
        processed_with_issues_lines=0,
        migration_issue_count=approved_exclusion_count,
    )
    payload: dict[str, Any] = {
        "schema_version": PRODUCT_PARITY_SCHEMA_VERSION,
        "snapshot": {
            "kind": "legacy-chronicle",
            "manifest_sha256": manifest_sha256,
            "file_count": 1,
            "bytes": approved_exclusion_count,
            "verified_before": True,
            "verified_after": True,
        },
        "existing_product": {
            "work_ledger_schema_version": "test-work-ledger-v1",
            "task_projection_schema_version": "test-task-projection-v1",
            "store_scope": "custom",
            "lines_seen": approved_exclusion_count,
            "parsed_object_events": approved_exclusion_count,
            "malformed_or_non_object": 0,
        },
        "status": "passed",
        "decision": "go-core-truth-slice",
        # This remains honest: the core parity runner never grants the
        # separate owner/operational live-cutover approval.
        "cutover_decision": "no-go",
        "acceptance": {
            "manifest_integrity": True,
            "candidate_integrity": True,
            "exact_supported_truth": True,
            "zero_unresolved_hard_conflicts": True,
            "exclusions_visible": True,
            "migration_policy_applied": True,
            "source_line_conservation": True,
            "approved_exclusion_count": approved_exclusion_count,
            "approved_issue_instance_count": approved_exclusion_count,
            "unapproved_exclusion_count": 0,
            "unapproved_issue_instance_count": 0,
            "affected_issue_line_count": approved_exclusion_count,
            "rerun_zero_write_and_stable_ids": True,
            "core_truth_slice_passed": True,
            "product_scope_complete": False,
            "cutover_gate_passed": False,
        },
        "comparisons": [
            {
                "surface": surface,
                "required_core": True,
                "matches": True,
                "source_count": 0,
                "candidate_count": 0,
                "mismatch_count": 0,
            }
            for surface in (
                "sessions",
                "task_membership",
                "usage_presence",
                "usage_aggregates",
            )
        ],
        "migration": {
            "lines_seen": approved_exclusion_count,
            "parsed_events": 0,
            "malformed_or_excluded_lines": approved_exclusion_count,
            "issue_lines": approved_exclusion_count,
            "excluded_lines": approved_exclusion_count,
            "processed_with_issues_lines": 0,
            "migration_issue_count": approved_exclusion_count,
            "write_dispositions": {},
            "internal_parity_matches": True,
            "internal_parity_difference_keys": [],
        },
        "exclusions": exclusions,
        "migration_disposition_policy": migration_policy,
        "issue_summary": {
            "issue_instances": approved_exclusion_count,
            "affected_lines": approved_exclusion_count,
            "excluded_lines": approved_exclusion_count,
            "processed_with_issues_lines": 0,
        },
        "rerun": {
            "canonical_writes": 0,
            "canonical_sequence_delta": 0,
            "task_ids_stable": True,
            "opaque_task_ids_valid": True,
            "task_id_count": 0,
            "table_counts_stable": True,
            "projection_rebuilt": False,
            "second_import": {
                "write_dispositions": {},
                "internal_parity_matches": True,
                "internal_parity_difference_keys": [],
            },
        },
        "runner": {
            "schema_version": "agent-chronicle.legacy-parity-runner.v3",
            "execution_completed": True,
            "candidate_retained": True,
            "candidate_file": candidate.name,
            "candidate_size_bytes": candidate.stat().st_size,
            "candidate_sha256": _sha256(candidate),
            "report_file": report.name,
            "first_import_internal_parity_matches": True,
            "second_import_internal_parity_matches": True,
        },
    }
    if mutate is not None:
        mutate(payload)
    report.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    report.chmod(0o600)


def _shadow(
    tmp_path: Path,
    *,
    name: str = "store",
    keep_clean_sidecars: bool = False,
) -> tuple[Path, str, str]:
    root = tmp_path / name
    root.mkdir(mode=0o755)
    with CanonicalStore.create_live(root) as store:
        store.repository().get_or_create_source(
            SourceInstanceInput(
                client="codex",
                adapter=LIVE_EVENTS_ADAPTER,
                representation=LEGACY_EVENTS_REPRESENTATION,
                namespace_digest=b"s" * 32,
                namespace_scheme="source-namespace-fingerprint-v1",
            )
        )
        store_uuid = str(
            store.connection.execute(
                "SELECT store_uuid FROM store_metadata WHERE singleton = 1"
            ).fetchone()[0]
        )
        store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    live = root / "chronicle.sqlite3"
    if not keep_clean_sidecars:
        _remove_clean_sqlite_sidecars(live)
    return root, store_uuid, _sha256(live)


def _receipt_path(tmp_path: Path, *, name: str = "promotion.json") -> Path:
    directory = tmp_path / "receipts"
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory / name


def _promote(tmp_path: Path):
    candidate, report, candidate_uuid = _candidate(tmp_path)
    root, shadow_uuid, shadow_digest = _shadow(tmp_path)
    preparation = prepare_cutover(candidate, report)
    receipt_path = _receipt_path(tmp_path)
    result = promote_candidate(
        preparation,
        root,
        receipt_path=receipt_path,
    )
    return (
        candidate,
        report,
        candidate_uuid,
        root,
        shadow_uuid,
        shadow_digest,
        receipt_path,
        result,
    )


def _mutate_candidate(candidate: Path, statement: str, parameters: tuple[Any, ...] = ()) -> None:
    connection = sqlite3.connect(candidate, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(statement, parameters)
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    _remove_clean_sqlite_sidecars(candidate)


def _insert_candidate_approved_issues(candidate: Path, count: int) -> None:
    connection = sqlite3.connect(candidate, isolation_level=None)
    try:
        connection.executemany(
            "INSERT INTO migration_issues(legacy_origin, location_digest, reason, "
            "disposition, count, first_seen_at_us) VALUES (?, ?, ?, ?, 1, 1)",
            [
                (
                    "events.jsonl",
                    hashlib.sha256(f"approved-issue-{index}".encode()).digest(),
                    "unresolved_source_namespace",
                    "requires_choice",
                )
                for index in range(count)
            ],
        )
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    _remove_clean_sqlite_sidecars(candidate)


def test_prepare_binds_v3_report_candidate_hash_and_readiness(tmp_path: Path) -> None:
    candidate, report, store_uuid = _candidate(tmp_path)

    receipt = prepare_cutover(candidate, report)

    assert receipt.receipt_version == CUTOVER_RECEIPT_VERSION
    assert receipt.candidate_store_uuid == store_uuid
    assert receipt.candidate_sha256 == _sha256(candidate)
    assert receipt.adapter_rows_to_normalize == 1
    assert {item.projection_name for item in receipt.projections} == {
        "rm_task_current",
        "rm_usage_day",
    }
    assert all(
        item.state == "current"
        and item.built_through_sequence == receipt.canonical_sequence
        for item in receipt.projections
    )


def test_prepare_accepts_the_real_v3_parity_runner_artifacts(tmp_path: Path) -> None:
    snapshot_root, manifest = _real_runner_snapshot(tmp_path)
    scratch = tmp_path / "runner-scratch"
    scratch.mkdir(mode=0o700)
    report, report_path = _PARITY_RUNNER.run_parity(
        argparse.Namespace(
            scratch_root=str(scratch.resolve()),
            snapshot_root=str(snapshot_root),
            snapshot_manifest=str(manifest),
            source_file="events.jsonl",
            legacy_store_scope="custom",
        )
    )
    candidate = report_path.parent / str(report["runner"]["candidate_file"])

    receipt = prepare_cutover(candidate, report_path)

    assert receipt.candidate_sha256 == report["runner"]["candidate_sha256"]
    assert not Path(f"{candidate}-wal").exists()
    assert not Path(f"{candidate}-shm").exists()


def test_prepare_accepts_policy_bound_approved_exclusions(tmp_path: Path) -> None:
    candidate, report, store_uuid = _candidate(tmp_path)
    _insert_candidate_approved_issues(candidate, 3)
    _write_report(candidate, report, approved_exclusion_count=3)

    receipt = prepare_cutover(candidate, report)

    assert receipt.candidate_store_uuid == store_uuid
    assert receipt.candidate_sha256 == _sha256(candidate)


def test_prepare_refuses_report_candidate_issue_mismatch(tmp_path: Path) -> None:
    candidate, report, _store_uuid = _candidate(tmp_path)
    _write_report(candidate, report, approved_exclusion_count=3)

    with pytest.raises(CutoverPreparationError, match="unconserved"):
        prepare_cutover(candidate, report)


def test_prepare_refuses_forged_pass_with_unapproved_exclusion(tmp_path: Path) -> None:
    candidate, report, _store_uuid = _candidate(tmp_path)

    def add_unapproved_exclusion(payload: dict[str, Any]) -> None:
        issue = {
            "reason": "new_unapproved_reason",
            "disposition": "requires_choice",
            "count": 1,
        }
        migration = payload["migration"]
        migration.update(
            {
                "lines_seen": 1,
                "malformed_or_excluded_lines": 1,
                "issue_lines": 1,
                "excluded_lines": 1,
                "migration_issue_count": 1,
            }
        )
        payload["exclusions"] = [issue]
        payload["issue_summary"].update(
            {
                "issue_instances": 1,
                "affected_lines": 1,
                "excluded_lines": 1,
            }
        )
        payload["migration_disposition_policy"] = (
            build_migration_disposition_policy_evidence(
                snapshot_manifest_sha256=payload["snapshot"]["manifest_sha256"],
                issues=[issue],
                source_lines_seen=1,
                importer_lines_seen=1,
                parsed_events=0,
                malformed_or_excluded_lines=1,
                issue_lines=1,
                excluded_lines=1,
                processed_with_issues_lines=0,
                migration_issue_count=1,
            )
        )
        # Deliberately forge the outer acceptance block as passing. The
        # cutover validator must recompute policy instead of trusting it.
        payload["acceptance"]["affected_issue_line_count"] = 1

    _write_report(candidate, report, mutate=add_unapproved_exclusion)

    with pytest.raises(
        CutoverPreparationError,
        match="unapproved|incompatible",
    ):
        prepare_cutover(candidate, report)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.__setitem__("status", "failed"),
        lambda payload: payload.__setitem__("decision", "no-go"),
        lambda payload: payload.__setitem__("cutover_decision", "go"),
        lambda payload: payload["acceptance"].__setitem__(
            "core_truth_slice_passed", False
        ),
        lambda payload: payload["runner"].__setitem__(
            "schema_version", "agent-chronicle.legacy-parity-runner.v1"
        ),
        lambda payload: payload["runner"].__setitem__(
            "candidate_file", "other.sqlite3"
        ),
        lambda payload: payload["runner"].__setitem__("candidate_sha256", "0" * 64),
    ],
)
def test_prepare_refuses_unbound_or_nonpassing_report(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    candidate, report, _uuid = _candidate(tmp_path)
    _write_report(candidate, report, mutate=mutate)

    with pytest.raises(CutoverPreparationError):
        prepare_cutover(candidate, report)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.pop("schema_version"),
        lambda payload: payload.__setitem__(
            "schema_version", "agent-chronicle.legacy-product-parity.v0"
        ),
        lambda payload: payload.__setitem__(
            "schema_version", "agent-chronicle.legacy-product-parity.v1"
        ),
        lambda payload: payload["snapshot"].__setitem__("verified_after", False),
        lambda payload: payload["existing_product"].__setitem__("lines_seen", 1),
        lambda payload: payload["comparisons"].pop(),
        lambda payload: payload["comparisons"][0].__setitem__("matches", False),
        lambda payload: payload["comparisons"][0].__setitem__("mismatch_count", 1),
        lambda payload: payload["acceptance"].__setitem__("candidate_integrity", False),
        lambda payload: payload.pop("migration_disposition_policy"),
        lambda payload: payload["migration_disposition_policy"].__setitem__(
            "rules_digest", "0" * 64
        ),
        lambda payload: payload["acceptance"].__setitem__(
            "approved_exclusion_count", 1
        ),
        lambda payload: payload["acceptance"].__setitem__(
            "unapproved_issue_instance_count", 1
        ),
        lambda payload: payload["migration"].__setitem__(
            "internal_parity_matches", False
        ),
        lambda payload: payload["migration"].__setitem__("migration_issue_count", 1),
        lambda payload: payload["migration"].pop("lines_seen"),
        lambda payload: payload["rerun"].__setitem__("canonical_writes", 1),
        lambda payload: payload["rerun"].__setitem__("task_ids_stable", False),
        lambda payload: payload["rerun"]["second_import"].__setitem__(
            "internal_parity_difference_keys", ["sessions"]
        ),
        lambda payload: payload["runner"].__setitem__(
            "first_import_internal_parity_matches", False
        ),
        lambda payload: payload["runner"].__setitem__(
            "second_import_internal_parity_matches", False
        ),
    ],
)
def test_prepare_refuses_incomplete_or_tampered_product_parity_evidence(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    candidate, report, _uuid = _candidate(tmp_path)
    _write_report(candidate, report, mutate=mutate)

    with pytest.raises(CutoverPreparationError):
        prepare_cutover(candidate, report)


def test_prepare_refuses_missing_or_stale_projection(tmp_path: Path) -> None:
    candidate, report, _uuid = _candidate(tmp_path)
    _mutate_candidate(
        candidate,
        "DELETE FROM projection_generations WHERE projection_name = 'rm_usage_day'",
    )
    _write_report(candidate, report)

    with pytest.raises(CutoverPreparationError, match="never built"):
        prepare_cutover(candidate, report)

    candidate2, report2, _uuid2 = _candidate(tmp_path, name="stale")
    _mutate_candidate(
        candidate2,
        "UPDATE store_metadata SET canonical_sequence = canonical_sequence + 1",
    )
    _write_report(candidate2, report2)
    with pytest.raises(CutoverPreparationError, match="stale"):
        prepare_cutover(candidate2, report2)


def test_prepare_refuses_open_conflict_and_unexpected_adapter(tmp_path: Path) -> None:
    candidate, report, _uuid = _candidate(tmp_path)
    _mutate_candidate(
        candidate,
        "INSERT INTO source_conflicts(source_instance_id, native_entity_kind, "
        "native_entity_key, incumbent_hash, incoming_hash, reason, first_seen_at_us, "
        "last_seen_at_us) VALUES (1, 'session', 's', ?, ?, 'test', 1, 1)",
        (b"a" * 32, b"b" * 32),
    )
    _write_report(candidate, report)
    with pytest.raises(CutoverPreparationError, match="unresolved"):
        prepare_cutover(candidate, report)

    candidate2, report2, _uuid2 = _candidate(tmp_path, name="adapter")
    _mutate_candidate(
        candidate2,
        "UPDATE source_instances SET adapter = 'untrusted-adapter'",
    )
    _write_report(candidate2, report2)
    with pytest.raises(CutoverPreparationError, match="unexpected adapters"):
        prepare_cutover(candidate2, report2)


def test_prepare_refuses_sidecar_hardlink_and_symlink(tmp_path: Path) -> None:
    candidate, report, _uuid = _candidate(tmp_path)
    Path(f"{candidate}-wal").write_bytes(b"busy")
    with pytest.raises(CutoverError, match="sidecar"):
        prepare_cutover(candidate, report)

    candidate2, report2, _uuid2 = _candidate(tmp_path, name="hardlink")
    os.link(candidate2, candidate2.with_name("alias.sqlite3"))
    with pytest.raises(CutoverError, match="unique"):
        prepare_cutover(candidate2, report2)

    candidate3, report3, _uuid3 = _candidate(tmp_path, name="symlink")
    linked_report = report3.with_name("linked-report.json")
    linked_report.symlink_to(report3)
    with pytest.raises(CutoverError, match="symlink"):
        prepare_cutover(candidate3, linked_report)


def test_prepare_refuses_nonprivate_run_directory(tmp_path: Path) -> None:
    candidate, report, _uuid = _candidate(tmp_path)
    candidate.parent.chmod(0o755)

    with pytest.raises(PermissionError, match="0700"):
        prepare_cutover(candidate, report)


@pytest.mark.parametrize("failure_point", ["fstat", "resolve"])
def test_open_store_root_closes_descriptor_on_validation_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    from agent_chronicle.canonical import cutover

    root = tmp_path / "store-root"
    root.mkdir()
    original_open = cutover.os.open
    original_fstat = cutover.os.fstat
    opened_descriptors: list[int] = []

    def observe_open(*args: object, **kwargs: object) -> int:
        descriptor = original_open(*args, **kwargs)  # type: ignore[arg-type]
        opened_descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(cutover.os, "open", observe_open)
    if failure_point == "fstat":
        def fail_fstat(_descriptor: int) -> os.stat_result:
            raise OSError("simulated fstat failure")

        monkeypatch.setattr(cutover.os, "fstat", fail_fstat)
    else:
        original_resolve = cutover.Path.resolve

        def fail_resolve(path: Path, *args: object, **kwargs: object) -> Path:
            if path == root:
                raise OSError("simulated resolve failure")
            return original_resolve(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(cutover.Path, "resolve", fail_resolve)

    with pytest.raises(OSError, match=failure_point):
        cutover._open_store_root(root)

    assert len(opened_descriptors) == 1
    with pytest.raises(OSError) as captured:
        original_fstat(opened_descriptors[0])
    assert captured.value.errno == errno.EBADF


def test_promotion_is_staged_normalized_backed_up_and_verified(tmp_path: Path) -> None:
    (
        candidate,
        _report,
        candidate_uuid,
        root,
        shadow_uuid,
        shadow_digest,
        receipt_path,
        result,
    ) = _promote(tmp_path)

    assert result.verification.ok is True
    assert result.receipt.promoted_store_uuid == candidate_uuid
    assert result.receipt.previous_live_store_uuid == shadow_uuid
    assert result.receipt.previous_live_sha256 == shadow_digest
    assert result.receipt.backup_path.exists()
    assert _sha256(result.receipt.backup_path) == shadow_digest
    assert stat.S_IMODE(result.receipt.backup_path.stat().st_mode) == 0o600
    assert receipt_path.exists()
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert load_promotion_receipt(receipt_path) == result.receipt
    assert not list(root.glob(".chronicle.sqlite3.*.stage"))
    assert len(list(root.glob(".chronicle.sqlite3.shadow-backup-*.sqlite3"))) == 1

    live = sqlite3.connect(root / "chronicle.sqlite3")
    try:
        assert live.execute(
            "SELECT store_role FROM store_metadata WHERE singleton = 1"
        ).fetchone()[0] == "live"
        assert live.execute("SELECT adapter FROM source_instances").fetchone()[0] == LIVE_EVENTS_ADAPTER
    finally:
        live.close()
    untouched = sqlite3.connect(candidate)
    try:
        assert untouched.execute(
            "SELECT store_role FROM store_metadata WHERE singleton = 1"
        ).fetchone()[0] == "candidate"
        assert untouched.execute("SELECT adapter FROM source_instances").fetchone()[0] == LEGACY_EVENTS_ADAPTER
    finally:
        untouched.close()


def test_promotion_revalidates_candidate_and_refuses_existing_backup(tmp_path: Path) -> None:
    candidate, report, _uuid = _candidate(tmp_path)
    root, _shadow_uuid, _shadow_digest = _shadow(tmp_path)
    preparation = prepare_cutover(candidate, report)
    _mutate_candidate(candidate, "UPDATE store_metadata SET canonical_sequence = canonical_sequence + 1")

    with pytest.raises(CutoverPromotionError, match="changed"):
        promote_candidate(
            preparation,
            root,
            receipt_path=_receipt_path(tmp_path),
        )

    # A prior retained backup is a deliberate one-operation gate, never an
    # overwrite target.
    candidate2, report2, _uuid2 = _candidate(tmp_path, name="second")
    preparation2 = prepare_cutover(candidate2, report2)
    backup = root / ".chronicle.sqlite3.shadow-backup-existing.sqlite3"
    backup.write_bytes(b"do not overwrite")
    backup.chmod(0o600)
    with pytest.raises(CutoverPromotionError, match="backup already exists"):
        promote_candidate(
            preparation2,
            root,
            receipt_path=_receipt_path(tmp_path, name="second.json"),
        )
    assert backup.read_bytes() == b"do not overwrite"


def test_promotion_refuses_busy_shadow_sidecar(tmp_path: Path) -> None:
    candidate, report, _uuid = _candidate(tmp_path)
    root, _shadow_uuid, _shadow_digest = _shadow(tmp_path)
    preparation = prepare_cutover(candidate, report)
    busy = sqlite3.connect(root / "chronicle.sqlite3", isolation_level=None)
    try:
        busy.execute("BEGIN IMMEDIATE")
        busy.execute("UPDATE store_metadata SET canonical_sequence = canonical_sequence")
        with pytest.raises(CutoverError, match="sidecar|busy"):
            promote_candidate(
                preparation,
                root,
                receipt_path=_receipt_path(tmp_path),
            )
    finally:
        busy.execute("ROLLBACK")
        busy.close()


def test_promotion_safely_seals_real_clean_shadow_sidecars(tmp_path: Path) -> None:
    candidate, report, _uuid = _candidate(tmp_path)
    root, _shadow_uuid, _shadow_digest = _shadow(
        tmp_path,
        keep_clean_sidecars=True,
    )
    wal, shm = _install_clean_sqlite_sidecars(root / "chronicle.sqlite3")
    assert wal.exists() and wal.stat().st_size == 0
    assert shm.exists()

    result = promote_candidate(
        prepare_cutover(candidate, report),
        root,
        receipt_path=_receipt_path(tmp_path),
    )

    assert result.verification.ok is True
    assert not wal.exists()
    assert not shm.exists()


def test_promotion_refuses_nonprivate_shadow_sidecar(tmp_path: Path) -> None:
    candidate, report, _uuid = _candidate(tmp_path)
    root, _shadow_uuid, _shadow_digest = _shadow(
        tmp_path,
        keep_clean_sidecars=True,
    )
    wal, _shm = _install_clean_sqlite_sidecars(root / "chronicle.sqlite3")
    wal.chmod(0o644)

    with pytest.raises(CutoverError, match="private unique"):
        promote_candidate(
            prepare_cutover(candidate, report),
            root,
            receipt_path=_receipt_path(tmp_path),
        )


def test_promotion_refuses_wal_pinned_by_active_reader(tmp_path: Path) -> None:
    candidate, report, _uuid = _candidate(tmp_path)
    root, _shadow_uuid, _shadow_digest = _shadow(tmp_path)
    live = root / "chronicle.sqlite3"
    reader = sqlite3.connect(live, isolation_level=None)
    writer = sqlite3.connect(live, isolation_level=None)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT canonical_sequence FROM store_metadata").fetchone()
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE store_metadata SET canonical_sequence = canonical_sequence + 1"
        )
        writer.execute("COMMIT")
        assert Path(f"{live}-wal").stat().st_size > 0

        with pytest.raises(CutoverError, match="busy|retained frames|checkpoint"):
            promote_candidate(
                prepare_cutover(candidate, report),
                root,
                receipt_path=_receipt_path(tmp_path),
            )
    finally:
        if reader.in_transaction:
            reader.execute("ROLLBACK")
        reader.close()
        writer.close()


def test_promotion_receipt_write_failure_after_replace_carries_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, report, candidate_uuid = _candidate(tmp_path)
    root, shadow_uuid, _shadow_digest = _shadow(tmp_path)
    preparation = prepare_cutover(candidate, report)
    receipt_path = _receipt_path(tmp_path)

    from agent_chronicle.canonical import cutover

    original_write = cutover.write_promotion_receipt
    calls = 0

    def fail_primary_then_write_fallback(path: Path, receipt: object) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated primary receipt disk failure")
        return original_write(path, receipt)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "agent_chronicle.canonical.cutover.write_promotion_receipt",
        fail_primary_then_write_fallback,
    )
    with pytest.raises(CutoverReceiptPersistenceError) as captured:
        promote_candidate(preparation, root, receipt_path=receipt_path)

    assert captured.value.receipt.promoted_store_uuid == candidate_uuid
    assert captured.value.receipt.previous_live_store_uuid == shadow_uuid
    assert captured.value.fallback_receipt_path is not None
    assert load_promotion_receipt(captured.value.fallback_receipt_path) == captured.value.receipt
    # Replacement really happened and the in-memory receipt is sufficient to
    # persist elsewhere or roll back; the exception never pretends otherwise.
    assert verify_promotion(captured.value.receipt).ok is True


def test_post_replace_failure_writes_emergency_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, report, candidate_uuid = _candidate(tmp_path)
    root, shadow_uuid, _shadow_digest = _shadow(tmp_path)
    preparation = prepare_cutover(candidate, report)
    from agent_chronicle.canonical import cutover

    original_require = cutover._require_private_regular

    def fail_promoted_lstat(path: Path, *, label: str):
        if label == "promoted live store":
            raise OSError("simulated post-replace lstat failure")
        return original_require(path, label=label)

    monkeypatch.setattr(cutover, "_require_private_regular", fail_promoted_lstat)
    with pytest.raises(CutoverPostReplaceError) as captured:
        promote_candidate(
            preparation,
            root,
            receipt_path=_receipt_path(tmp_path),
        )
    monkeypatch.setattr(cutover, "_require_private_regular", original_require)

    assert captured.value.replacement_state == "installed"
    assert captured.value.receipt.promoted_store_uuid == candidate_uuid
    assert captured.value.receipt.previous_live_store_uuid == shadow_uuid
    assert captured.value.fallback_receipt_path is not None
    assert load_promotion_receipt(captured.value.fallback_receipt_path) == captured.value.receipt
    assert verify_promotion(captured.value.receipt).ok is True


def test_replace_async_exception_keeps_backup_and_prewrite_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, report, candidate_uuid = _candidate(tmp_path)
    root, shadow_uuid, _shadow_digest = _shadow(tmp_path)
    preparation = prepare_cutover(candidate, report)
    from agent_chronicle.canonical import cutover

    original_replace = cutover.os.replace

    def replace_then_interrupt(*args: object, **kwargs: object) -> None:
        original_replace(*args, **kwargs)  # type: ignore[arg-type]
        raise KeyboardInterrupt("simulated async exception after replace")

    monkeypatch.setattr(cutover.os, "replace", replace_then_interrupt)
    with pytest.raises(CutoverPostReplaceError) as captured:
        promote_candidate(
            preparation,
            root,
            receipt_path=_receipt_path(tmp_path),
        )

    assert captured.value.replacement_state == "installed"
    assert captured.value.receipt.promoted_store_uuid == candidate_uuid
    assert captured.value.receipt.previous_live_store_uuid == shadow_uuid
    assert captured.value.receipt.backup_path.exists()
    assert captured.value.fallback_receipt_path is not None
    assert load_promotion_receipt(captured.value.fallback_receipt_path) == captured.value.receipt
    assert len(list(root.glob(".chronicle.sqlite3.shadow-backup-*.sqlite3"))) == 1


def test_replace_async_exception_reports_ambiguous_if_live_inode_is_unrelated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, report, _candidate_uuid = _candidate(tmp_path)
    root, _shadow_uuid, _shadow_digest = _shadow(tmp_path)
    preparation = prepare_cutover(candidate, report)
    from agent_chronicle.canonical import cutover

    unrelated = root / "unrelated.sqlite3"
    unrelated.write_bytes(b"unrelated replacement bytes")
    unrelated.chmod(0o600)
    original_replace = cutover.os.replace

    def replace_obscure_then_interrupt(*args: object, **kwargs: object) -> None:
        original_replace(*args, **kwargs)  # type: ignore[arg-type]
        original_replace(unrelated, root / "chronicle.sqlite3")
        raise KeyboardInterrupt("simulated ambiguous post-replace state")

    monkeypatch.setattr(cutover.os, "replace", replace_obscure_then_interrupt)
    with pytest.raises(CutoverPostReplaceError) as captured:
        promote_candidate(
            preparation,
            root,
            receipt_path=_receipt_path(tmp_path),
        )

    assert captured.value.replacement_state == "ambiguous"
    assert captured.value.fallback_receipt_path is not None
    assert captured.value.receipt.backup_path.exists()


def test_recovery_receipt_is_durable_before_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, report, _candidate_uuid = _candidate(tmp_path)
    root, _shadow_uuid, _shadow_digest = _shadow(tmp_path)
    preparation = prepare_cutover(candidate, report)
    from agent_chronicle.canonical import cutover

    original_replace = cutover.os.replace
    observed_prewrite = False

    def assert_prewrite_then_replace(*args: object, **kwargs: object) -> None:
        nonlocal observed_prewrite
        emergency = list(
            root.glob(".chronicle.sqlite3.promotion-receipt-*.json")
        )
        assert len(emergency) == 1
        loaded = load_promotion_receipt(emergency[0])
        assert loaded.promoted_store_uuid == preparation.candidate_store_uuid
        observed_prewrite = True
        original_replace(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cutover.os, "replace", assert_prewrite_then_replace)
    result = promote_candidate(
        preparation,
        root,
        receipt_path=_receipt_path(tmp_path),
    )

    assert result.verification.ok is True
    assert observed_prewrite is True
    assert not list(root.glob(".chronicle.sqlite3.promotion-receipt-*.json"))


def test_receipt_loader_rejects_tamper_malformed_symlink_and_existing_output(
    tmp_path: Path,
) -> None:
    *_, receipt_path, result = _promote(tmp_path)
    original = receipt_path.read_text(encoding="utf-8")

    document = json.loads(original)
    document["receipt"]["promoted_sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(document), encoding="utf-8")
    receipt_path.chmod(0o600)
    with pytest.raises(CutoverPromotionError, match="digest"):
        load_promotion_receipt(receipt_path)

    receipt_path.write_text("not json", encoding="utf-8")
    receipt_path.chmod(0o600)
    with pytest.raises(CutoverPreparationError, match="JSON"):
        load_promotion_receipt(receipt_path)

    receipt_path.write_text(original, encoding="utf-8")
    receipt_path.chmod(0o600)
    linked = receipt_path.with_name("linked.json")
    linked.symlink_to(receipt_path)
    with pytest.raises(CutoverError, match="symlink"):
        load_promotion_receipt(linked)

    with pytest.raises(FileExistsError):
        write_promotion_receipt(receipt_path, result.receipt)


def test_receipt_loader_rejects_unknown_missing_and_wrong_typed_fields(tmp_path: Path) -> None:
    *_, receipt_path, _result = _promote(tmp_path)
    document = json.loads(receipt_path.read_text(encoding="utf-8"))

    for mutate in (
        lambda body: body.__setitem__("unknown", True),
        lambda body: body.pop("promoted_inode"),
        lambda body: body.__setitem__("promoted_inode", "1"),
    ):
        changed = json.loads(json.dumps(document))
        mutate(changed["receipt"])
        canonical = json.dumps(
            changed["receipt"],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        changed["receipt_sha256"] = hashlib.sha256(canonical).hexdigest()
        receipt_path.write_text(json.dumps(changed), encoding="utf-8")
        receipt_path.chmod(0o600)
        with pytest.raises(CutoverPromotionError):
            load_promotion_receipt(receipt_path)


def test_durable_receipt_load_verify_and_rollback_survive_archived_sources(
    tmp_path: Path,
) -> None:
    candidate, report, *_middle, receipt_path, promotion = _promote(tmp_path)
    candidate.unlink()
    report.unlink()

    loaded = load_promotion_receipt(receipt_path)

    assert loaded == promotion.receipt
    assert verify_promotion(loaded).ok is True
    assert rollback_promotion(loaded).verification.ok is True


def test_verify_is_nonmutating_and_fail_closed_with_idle_open_writer(
    tmp_path: Path,
) -> None:
    *_, root, _shadow_uuid, _shadow_digest, _receipt_path_value, promotion = _promote(
        tmp_path
    )
    live = root / "chronicle.sqlite3"
    idle_writer = sqlite3.connect(live, isolation_level=None)
    try:
        idle_writer.execute("SELECT canonical_sequence FROM store_metadata").fetchone()
        wal = Path(f"{live}-wal")
        shm = Path(f"{live}-shm")
        assert wal.exists() and shm.exists()
        wal_identity = (wal.stat().st_dev, wal.stat().st_ino)
        shm_identity = (shm.stat().st_dev, shm.stat().st_ino)

        verification = verify_promotion(promotion.receipt)

        resume_error: Exception | None = None
        try:
            idle_writer.execute("BEGIN IMMEDIATE")
            idle_writer.execute(
                "UPDATE store_metadata SET canonical_sequence = canonical_sequence"
            )
            idle_writer.execute("ROLLBACK")
        except Exception as exc:  # pragma: no cover - asserted below.
            resume_error = exc
            if idle_writer.in_transaction:
                idle_writer.execute("ROLLBACK")
        assert verification.ok is False
        assert any("sidecar" in error.lower() for error in verification.errors)
        assert wal.exists() and (wal.stat().st_dev, wal.stat().st_ino) == wal_identity
        assert shm.exists() and (shm.stat().st_dev, shm.stat().st_ino) == shm_identity
        assert resume_error is None
    finally:
        idle_writer.close()


def test_rollback_restores_exact_shadow_and_preserves_backup(tmp_path: Path) -> None:
    (
        _candidate_path,
        _report,
        candidate_uuid,
        root,
        shadow_uuid,
        shadow_digest,
        receipt_path,
        promotion,
    ) = _promote(tmp_path)

    loaded = load_promotion_receipt(receipt_path)
    rollback = rollback_promotion(loaded)

    assert rollback.verification.ok is True
    assert rollback.receipt.replaced_store_uuid == candidate_uuid
    assert rollback.receipt.restored_store_uuid == shadow_uuid
    assert _sha256(root / "chronicle.sqlite3") == shadow_digest
    assert promotion.receipt.backup_path.exists()
    assert _sha256(promotion.receipt.backup_path) == shadow_digest


def test_rollback_verification_failure_reports_installed_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    *_, root, shadow_uuid, _shadow_digest, _receipt_path_value, promotion = _promote(
        tmp_path
    )
    from agent_chronicle.canonical import cutover

    failed = cutover.CutoverVerification(
        verified_at_us=1,
        ok=False,
        live_store_uuid=shadow_uuid,
        canonical_sequence=None,
        live_sha256=None,
        checks=(),
        errors=("simulated post-replace verification failure",),
    )
    monkeypatch.setattr(cutover, "_verify_rollback", lambda _receipt: failed)

    with pytest.raises(RollbackPostReplaceError) as captured:
        rollback_promotion(promotion.receipt)

    assert captured.value.replacement_state == "installed"
    assert captured.value.receipt is not None
    assert captured.value.receipt.restored_store_uuid == shadow_uuid
    assert captured.value.verification == failed
    assert _read_store_uuid(root / "chronicle.sqlite3") == shadow_uuid


def test_rollback_verification_exception_reports_installed_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    *_, _root, shadow_uuid, _shadow_digest, _receipt_path_value, promotion = _promote(
        tmp_path
    )
    from agent_chronicle.canonical import cutover

    def fail_verification(_receipt: object) -> None:
        raise KeyboardInterrupt("simulated interruption during rollback verification")

    monkeypatch.setattr(cutover, "_verify_rollback", fail_verification)
    with pytest.raises(RollbackPostReplaceError) as captured:
        rollback_promotion(promotion.receipt)

    assert captured.value.replacement_state == "installed"
    assert captured.value.receipt is not None
    assert captured.value.receipt.restored_store_uuid == shadow_uuid
    assert captured.value.verification is None


def test_rollback_replace_async_exception_reports_observed_installed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    *_, root, shadow_uuid, _shadow_digest, _receipt_path_value, promotion = _promote(
        tmp_path
    )
    from agent_chronicle.canonical import cutover

    original_replace = cutover.os.replace

    def replace_then_interrupt(*args: object, **kwargs: object) -> None:
        original_replace(*args, **kwargs)  # type: ignore[arg-type]
        raise KeyboardInterrupt("simulated async exception after rollback replace")

    monkeypatch.setattr(cutover.os, "replace", replace_then_interrupt)
    with pytest.raises(RollbackPostReplaceError) as captured:
        rollback_promotion(promotion.receipt)

    assert captured.value.replacement_state == "installed"
    assert captured.value.receipt is not None
    assert captured.value.receipt.restored_store_uuid == shadow_uuid
    assert _read_store_uuid(root / "chronicle.sqlite3") == shadow_uuid


def test_rollback_refuses_live_store_changed_after_promotion(tmp_path: Path) -> None:
    *_, root, _shadow_uuid, _shadow_digest, _receipt_path_value, promotion = _promote(
        tmp_path
    )
    _mutate_candidate(
        root / "chronicle.sqlite3",
        "UPDATE store_metadata SET canonical_sequence = canonical_sequence + 1",
    )

    with pytest.raises(CutoverPromotionError, match="no longer matches"):
        rollback_promotion(promotion.receipt)
