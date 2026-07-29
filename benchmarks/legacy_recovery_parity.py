#!/usr/bin/env python3
"""Run manifest-bound legacy recovery parity in an isolated candidate pair.

The runner has no discovery path and no live-store mode.  It accepts exactly
three explicit absolute paths: a sealed snapshot root, its external manifest,
and a caller-owned private scratch directory.  Every retained JSON value is a
count or boolean; physical identities, manifest digests, paths, raw lines, and
random candidate identifiers never cross the reporting boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from agentacct.canonical.legacy_recovery import (
    LegacyRecoveryError,
    RECOVERY_DESIGN_ID,
    RecoveryClassification,
    RecoveryReplayReport,
    classify_legacy_recovery,
    replay_verified_recovery,
)
from agentacct.canonical.live_paths import (
    LivePathSafetyError,
    paths_overlap,
    reject_live_state_overlap,
)
from agentacct.canonical.migration_archive import (
    MigrationArchiveError,
    VerifiedMigrationArchive,
    build_migration_archive,
    scan_snapshot_lines,
)
from agentacct.canonical.product_parity import (
    RECOVERY_PRODUCT_ORACLE_CODE_VERSION,
    RECOVERY_PRODUCT_ORACLE_CONTRACT_VERSION,
    build_legacy_recovery_product_oracle_report,
)
from agentacct.canonical.safe_scratch import (
    AnchoredRunDirectory,
    ScratchSafetyError,
    create_anchored_run_directory,
)
from agentacct.canonical.snapshot import (
    SnapshotManifest,
    SnapshotSafetyError,
    VerifiedSnapshot,
)
from agentacct.canonical.sqlite import CanonicalStore


_ALLOWED_MANIFEST_KINDS = frozenset({"legacy", "legacy-chronicle"})
_DISPOSITIONS = (
    "ambiguous",
    "candidate_recovery",
    "canonical_imported",
    "canonical_no_effect",
    "identity_conflict",
    "invalid_retained",
    "namespace_only",
    "no_proof",
    "unsupported_retained",
)
_QUARANTINE_DISPOSITIONS = frozenset(
    {
        "ambiguous",
        "identity_conflict",
        "invalid_retained",
        "namespace_only",
        "no_proof",
        "unsupported_retained",
    }
)
# Only fields populated from local wall-clock/random allocation are excluded.
# Source-derived semantic time (for example occurred_at_us, observed_at_us,
# updated_at_us on usage rows, and price_effective_at_us) remains compared.
_RUNTIME_VARIANT_COLUMNS: Mapping[str, frozenset[str]] = {
    "facts": frozenset({"created_at_us"}),
    "migration_issues": frozenset({"first_seen_at_us"}),
    "projection_generations": frozenset({"built_at_us"}),
    "schema_migrations": frozenset({"applied_at_us"}),
    "session_edges": frozenset({"created_at_us"}),
    "session_observed_lineage": frozenset({"updated_at_us"}),
    "sessions": frozenset({"created_at_us", "updated_at_us"}),
    "source_conflicts": frozenset({"first_seen_at_us", "last_seen_at_us"}),
    "source_instances": frozenset({"created_at_us"}),
    "store_metadata": frozenset({"created_at_us", "store_uuid"}),
    "task_aliases": frozenset({"created_at_us"}),
    "task_anchors": frozenset({"created_at_us", "public_task_id"}),
    "usage_checkpoints": frozenset({"created_at_us"}),
    "rm_task_current": frozenset({"public_task_id"}),
}
_FORBIDDEN_SCRATCH_COMPONENTS = frozenset(
    {
        ".agent-sentinel",
        ".agent-sentinel-global",
        ".agent-chronicle",
        ".agent-chronicle-global",
        ".codex",
    }
)
_RUN_PREFIX = "legacy-recovery-parity-"
_ARCHIVE_DIRECTORY = "archive"
_CANDIDATE_A_FILE = "candidate-a.sqlite3"
_CANDIDATE_B_FILE = "candidate-b.sqlite3"
_REPORT_FILE = "recovery-parity.json"
_RECOVERY_PARITY_RUNNER_VERSION = 6


class RecoveryParityRefusal(SnapshotSafetyError):
    """The requested offline boundary is incomplete or unsafe."""


def _absolute_argument(raw: str | os.PathLike[str], *, label: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise RecoveryParityRefusal(f"{label} must be an explicit absolute path")
    return path


def _live_and_pairwise_preflight(
    *,
    snapshot_root: Path,
    snapshot_manifest: Path,
    scratch_root: Path,
) -> None:
    """Reject configured live overlap before any input read or output mkdir."""

    declared = (
        ("--snapshot-root", snapshot_root),
        ("--snapshot-manifest", snapshot_manifest),
        ("--scratch-root", scratch_root),
    )
    try:
        for label, path in declared:
            reject_live_state_overlap(path, label=label)
        resolved = tuple((label, path.resolve(strict=False)) for label, path in declared)
    except (LivePathSafetyError, OSError, RuntimeError) as exc:
        raise RecoveryParityRefusal(
            "declared offline paths cannot be proved disjoint from live state"
        ) from exc

    for index, (left_label, left) in enumerate(resolved):
        for right_label, right in resolved[index + 1 :]:
            if paths_overlap(left, right):
                raise RecoveryParityRefusal(
                    f"{left_label} and {right_label} must be disjoint"
                )


def _assert_no_symlink_components(path: Path, *, label: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            observed = current.lstat()
        except OSError as exc:
            raise RecoveryParityRefusal(
                f"{label} must be an existing directory"
            ) from exc
        if stat.S_ISLNK(observed.st_mode):
            raise RecoveryParityRefusal(
                f"{label} may not contain symlink components"
            )


def _private_directory(path: Path, *, label: str) -> bool:
    try:
        observed = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise RecoveryParityRefusal(f"{label} cannot be inspected safely") from exc
    return (
        stat.S_ISDIR(observed.st_mode)
        and observed.st_uid == os.geteuid()
        and stat.S_IMODE(observed.st_mode) == 0o700
    )


def _require_private_scratch(path: Path) -> Path:
    _assert_no_symlink_components(path, label="--scratch-root")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RecoveryParityRefusal(
            "--scratch-root must be an existing directory"
        ) from exc
    if resolved != path:
        raise RecoveryParityRefusal("--scratch-root must be a canonical path")
    if any(
        part.casefold() in _FORBIDDEN_SCRATCH_COMPONENTS
        for part in resolved.parts
    ):
        raise RecoveryParityRefusal(
            "--scratch-root may not be inside live agentacct or Codex state"
        )
    if not _private_directory(resolved, label="--scratch-root"):
        raise RecoveryParityRefusal(
            "--scratch-root must be owner-owned with exact mode 0700"
        )
    return resolved


def _create_private_archive_directory(run: AnchoredRunDirectory) -> Path:
    run.prove_unchanged()
    try:
        os.mkdir(_ARCHIVE_DIRECTORY, 0o700, dir_fd=run.descriptor)
        os.fsync(run.descriptor)
        observed = os.stat(
            _ARCHIVE_DIRECTORY,
            dir_fd=run.descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise RecoveryParityRefusal(
            "private archive directory could not be created"
        ) from exc
    if not (
        stat.S_ISDIR(observed.st_mode)
        and observed.st_uid == os.geteuid()
        and stat.S_IMODE(observed.st_mode) == 0o700
    ):
        raise RecoveryParityRefusal(
            "private archive directory must remain owner-only"
        )
    run.prove_unchanged()
    return run.child_path(_ARCHIVE_DIRECTORY)


def _private_regular_file(path: Path, *, label: str) -> bool:
    try:
        observed = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise RecoveryParityRefusal(f"{label} cannot be inspected safely") from exc
    return (
        stat.S_ISREG(observed.st_mode)
        and observed.st_uid == os.geteuid()
        and observed.st_nlink == 1
        and stat.S_IMODE(observed.st_mode) == 0o600
    )


def _archive_permissions(archive: VerifiedMigrationArchive) -> tuple[bool, int]:
    archive.verify_unchanged()
    if not _private_directory(archive.path, label="archive directory"):
        return False, 0
    try:
        children = tuple(archive.path.iterdir())
    except OSError as exc:
        raise RecoveryParityRefusal("archive contents cannot be inspected") from exc
    private = all(
        _private_regular_file(child, label="archive file") for child in children
    )
    archive.verify_unchanged()
    return private, len(children)


def _task_ids(store: CanonicalStore) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in store.connection.execute(
            "SELECT public_task_id FROM task_anchors ORDER BY task_anchor_id"
        )
    )


def _opaque_task_ids(task_ids: Sequence[str]) -> bool:
    return all(
        value.startswith("task_")
        and len(value) == 37
        and all(character in "0123456789abcdef" for character in value[5:])
        for value in task_ids
    )


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _hash_cell(digest: Any, value: object) -> None:
    if value is None:
        encoded = b"null"
    elif isinstance(value, bytes):
        encoded = b"blob:" + value
    elif isinstance(value, str):
        encoded = b"text:" + value.encode("utf-8")
    elif isinstance(value, bool):
        encoded = b"bool:1" if value else b"bool:0"
    elif isinstance(value, int):
        encoded = b"int:" + str(value).encode("ascii")
    elif isinstance(value, float):
        encoded = b"float:" + value.hex().encode("ascii")
    else:
        raise RecoveryParityRefusal(
            "candidate contains an unsupported SQLite value type"
        )
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def _semantic_fingerprint(store: CanonicalStore) -> tuple[bytes, int]:
    """Hash all canonical rows after removing runtime-only UUID/Task/time fields."""

    digest = hashlib.sha256()
    total_rows = 0
    table_names = tuple(
        str(row[0])
        for row in store.connection.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    )
    for table in table_names:
        quoted_table = _quote_identifier(table)
        info = tuple(store.connection.execute(f"PRAGMA table_info({quoted_table})"))
        excluded = _RUNTIME_VARIANT_COLUMNS.get(table, frozenset())
        included = tuple(
            str(row[1])
            for row in info
            if str(row[1]) not in excluded
        )
        if not included:
            raise RecoveryParityRefusal(
                "candidate semantic comparison found a table without stable columns"
            )
        primary = tuple(
            str(row[1])
            for row in sorted(info, key=lambda item: int(item[5]))
            if int(row[5]) > 0 and str(row[1]) in included
        )
        order = primary or included
        selected_sql = ", ".join(_quote_identifier(column) for column in included)
        order_sql = ", ".join(_quote_identifier(column) for column in order)
        digest.update(table.encode("utf-8"))
        digest.update(b"\x00")
        for column in included:
            digest.update(column.encode("utf-8"))
            digest.update(b"\x00")
        cursor = store.connection.execute(
            f"SELECT {selected_sql} FROM {quoted_table} ORDER BY {order_sql}"
        )
        for row in cursor:
            total_rows += 1
            digest.update(b"\x01")
            for value in row:
                _hash_cell(digest, value)
    return digest.digest(), total_rows


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _quarantined_event_ids(
    archive: VerifiedMigrationArchive,
    classification: RecoveryClassification,
) -> tuple[str, ...]:
    locators = tuple(
        draft.subject
        for draft in classification.drafts
        if draft.disposition in _QUARANTINE_DISPOSITIONS
    )
    raw_by_locator = archive.read_raw_many(locators)
    event_ids: set[str] = set()
    for raw in raw_by_locator.values():
        try:
            value = json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=_reject_duplicate_keys,
            )
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(value, Mapping):
            continue
        event_id = value.get("event_id")
        if isinstance(event_id, str) and event_id:
            event_ids.add(event_id)
    return tuple(sorted(event_ids))


def _canonical_fact_count_for_event_ids(
    store: CanonicalStore,
    event_ids: Sequence[str],
) -> int:
    total = 0
    for start in range(0, len(event_ids), 400):
        batch = tuple(event_ids[start : start + 400])
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        total += int(
            store.connection.execute(
                f"SELECT COUNT(*) FROM facts WHERE source_event_id IN ({placeholders})",
                batch,
            ).fetchone()[0]
        )
    return total


@dataclass(frozen=True, slots=True)
class _CandidateEvidence:
    first: RecoveryReplayReport
    second: RecoveryReplayReport | None
    table_counts: Mapping[str, int]
    canonical_sequence: int
    task_ids: tuple[str, ...]
    semantic_fingerprint: bytes
    semantic_rows: int
    quarantine_fact_count: int
    file_private_0600: bool
    sidecar_file_count: int
    rerun_total_changes_delta: int
    rerun_table_counts_stable: bool
    rerun_task_ids_stable: bool
    product_oracle: Mapping[str, int | bool]


def _run_candidate(
    *,
    snapshot: VerifiedSnapshot,
    archive: VerifiedMigrationArchive,
    plan: Any,
    run: AnchoredRunDirectory,
    candidate_name: str,
    quarantined_event_ids: Sequence[str],
    replay_twice: bool,
) -> _CandidateEvidence:
    run.prove_unchanged()
    candidate = snapshot.validate_candidate_target(
        run.child_path(candidate_name),
        scratch_root=run.path,
    )
    store = CanonicalStore.create(candidate)
    second: RecoveryReplayReport | None = None
    rerun_total_changes_delta = 0
    rerun_table_counts_stable = True
    rerun_task_ids_stable = True
    try:
        first = replay_verified_recovery(
            plan=plan,
            store=store,
            scratch_root=run.path,
        )
        if replay_twice:
            before_counts = dict(store.repository().table_counts())
            before_task_ids = _task_ids(store)
            before_changes = store.connection.total_changes
            second = replay_verified_recovery(
                plan=plan,
                store=store,
                scratch_root=run.path,
            )
            rerun_total_changes_delta = store.connection.total_changes - before_changes
            rerun_table_counts_stable = (
                dict(store.repository().table_counts()) == before_counts
            )
            rerun_task_ids_stable = _task_ids(store) == before_task_ids

        table_counts = dict(store.repository().table_counts())
        canonical_sequence = store.repository().canonical_sequence()
        task_ids = _task_ids(store)
        product_oracle = build_legacy_recovery_product_oracle_report(
            snapshot=snapshot,
            plan=plan,
            repository=store.repository(),
            quarantined_event_ids=tuple(quarantined_event_ids),
            runner_version=_RECOVERY_PARITY_RUNNER_VERSION,
        )
        semantic_fingerprint, semantic_rows = _semantic_fingerprint(store)
        quarantine_fact_count = _canonical_fact_count_for_event_ids(
            store,
            quarantined_event_ids,
        )
        archive.verify_unchanged()
        snapshot.verify_unchanged()
        run.prove_unchanged()
        store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        store.close()

    run.prove_unchanged()
    private = _private_regular_file(candidate, label="retained candidate")
    sidecars = sum(
        int(path.exists())
        for path in (
            Path(f"{candidate}-wal"),
            Path(f"{candidate}-shm"),
            Path(f"{candidate}-journal"),
        )
    )
    return _CandidateEvidence(
        first=first,
        second=second,
        table_counts=table_counts,
        canonical_sequence=canonical_sequence,
        task_ids=task_ids,
        semantic_fingerprint=semantic_fingerprint,
        semantic_rows=semantic_rows,
        quarantine_fact_count=quarantine_fact_count,
        file_private_0600=private,
        sidecar_file_count=sidecars,
        rerun_total_changes_delta=rerun_total_changes_delta,
        rerun_table_counts_stable=rerun_table_counts_stable,
        rerun_task_ids_stable=rerun_task_ids_stable,
        product_oracle=product_oracle,
    )


def _disposition_count(report: RecoveryReplayReport, disposition: str) -> int:
    return int(report.recovery.fact_dispositions.get(disposition, 0))


def _link_disposition_count(
    report: RecoveryReplayReport,
    disposition: str,
) -> int:
    return int(report.recovery.link_dispositions.get(disposition, 0))


def _scoped_import_parity_matches(report: RecoveryReplayReport) -> bool:
    """Require every ordinary-import source comparison to match."""

    return bool(report.scoped_imports) and all(
        scoped.parity.matches is True for scoped in report.scoped_imports
    )


_PRODUCT_ORACLE_FIELDS = (
    "contract_version",
    "oracle_code_version",
    "runner_version",
    "source_manifest_verified",
    "existing_product_functions_used",
    "source_malformed_or_non_object_rows",
    "eligible_recovery_rows",
    "recovered_section_rows",
    "adapter_membership_only_task_rows",
    "all_recovered_rows_have_existing_product_work_output_coverage",
    "source_adapter_rows",
    "candidate_adapter_rows",
    "recovery_transport_fact_rows",
    "source_adapter_conservation",
    "candidate_adapter_conservation",
    "adapter_contract_match",
    "adapter_contract_mismatch_count",
    "claim_fields_match",
    "claim_field_mismatch_count",
    "task_membership_match",
    "task_membership_mismatch_count",
    "source_full_context_work_event_rows",
    "candidate_full_context_work_event_rows",
    "source_existing_product_work_context_rows",
    "candidate_existing_product_work_context_rows",
    "existing_product_work_context_rows_match",
    "existing_product_work_covered_recovery_rows",
    "candidate_existing_product_work_covered_recovery_rows",
    "coverage_conservation",
    "target_work_identities_match",
    "target_work_identity_mismatch_count",
    "source_distinct_target_work_identity_rows",
    "candidate_distinct_target_work_identity_rows",
    "target_work_identity_presence_conservation",
    "source_target_work_output_rows",
    "candidate_target_work_output_rows",
    "source_work_output_conservation",
    "candidate_work_output_conservation",
    "work_outputs_match",
    "work_output_mismatch_count",
    "source_affected_task_output_rows",
    "candidate_affected_task_output_rows",
    "source_target_work_affected_task_occurrence_rows",
    "candidate_target_work_affected_task_occurrence_rows",
    "source_target_work_task_membership_conservation",
    "candidate_target_work_task_membership_conservation",
    "task_output_comparison_target_only",
    "task_outputs_match",
    "task_output_mismatch_count",
    "quarantine_event_ids_checked",
    "quarantine_canonical_fact_rows",
    "quarantine_zero_contribution",
    "passed",
)


_REPORT_SHAPE: Mapping[str, object] = {
    "archive": {
        "directory_private_0700": None,
        "file_count": None,
        "files_private_0600": None,
        "receipt_conservation": None,
        "receipt_count": None,
        "sealed_plan_verified": None,
    },
    "candidate_a": {
        "file_private_0600": None,
        "first_canonical_writes": None,
        "first_fact_inserted_count": None,
        "first_link_inserted_count": None,
        "first_projection_rebuilt": None,
        "first_recovery_candidate_count": None,
        "first_recovery_conservation": None,
        "first_scoped_import_parity_match": None,
        "first_sequence_delta": None,
        "opaque_task_ids_valid": None,
        "quarantine_canonical_fact_count": None,
        "quarantine_event_id_count": None,
        "quarantine_excluded_from_canonical": None,
        "rerun_canonical_writes": None,
        "rerun_fact_noop_count": None,
        "rerun_link_noop_count": None,
        "rerun_projection_rebuilt": None,
        "rerun_recovery_conservation": None,
        "rerun_scoped_import_parity_match": None,
        "rerun_sequence_delta": None,
        "rerun_table_counts_stable": None,
        "rerun_task_ids_stable": None,
        "rerun_total_changes_delta": None,
        "sidecar_file_count": None,
        "task_count": None,
        "task_ids_present_when_recovery_nonzero": None,
    },
    "candidate_b": {
        "file_private_0600": None,
        "first_canonical_writes": None,
        "first_fact_inserted_count": None,
        "first_link_inserted_count": None,
        "first_projection_rebuilt": None,
        "first_recovery_candidate_count": None,
        "first_recovery_conservation": None,
        "first_scoped_import_parity_match": None,
        "first_sequence_delta": None,
        "opaque_task_ids_valid": None,
        "quarantine_canonical_fact_count": None,
        "quarantine_excluded_from_canonical": None,
        "sidecar_file_count": None,
        "task_count": None,
        "task_ids_present_when_recovery_nonzero": None,
    },
    "classification": {
        **{key: None for key in _DISPOSITIONS},
        "classification_conservation": None,
    },
    "completed": None,
    "inventory": {
        "file_count": None,
        "parsed_object_event_count": None,
        "physical_line_count": None,
        "source_byte_count": None,
    },
    "offline_boundary": {
        "all_inputs_absolute": None,
        "configured_live_roots_disjoint": None,
        "run_directory_private_0700": None,
        "scratch_private_0700": None,
        "snapshot_verified": None,
    },
    "product_oracle": {
        **{key: None for key in _PRODUCT_ORACLE_FIELDS},
        "candidate_reports_match": None,
    },
    "parity": {
        "canonical_sequence_match": None,
        "opaque_task_ids_valid": None,
        "runtime_identity_and_time_excluded": None,
        "semantic_match": None,
        "semantic_row_count_a": None,
        "semantic_row_count_b": None,
        "semantic_row_count_match": None,
        "table_counts_match": None,
        "task_count_match": None,
    },
    "passed": None,
}


def _validate_report(
    value: Mapping[str, object],
    shape: Mapping[str, object] = _REPORT_SHAPE,
) -> None:
    if set(value) != set(shape):
        raise RecoveryParityRefusal("public report fields differ from the allowlist")
    for key, expected in shape.items():
        item = value[key]
        if isinstance(expected, Mapping):
            if not isinstance(item, Mapping):
                raise RecoveryParityRefusal("public report nesting is invalid")
            _validate_report(item, expected)
            continue
        if isinstance(item, bool):
            continue
        if isinstance(item, int) and item >= 0:
            continue
        raise RecoveryParityRefusal(
            "public report values must be non-negative counts or booleans"
        )


def _build_report(
    *,
    classification: RecoveryClassification,
    archive_file_count: int,
    archive_files_private: bool,
    archive_private: bool,
    plan_length: int,
    inventory_file_count: int,
    inventory_line_count: int,
    inventory_byte_count: int,
    scratch_private: bool,
    run_private: bool,
    candidate_a: _CandidateEvidence,
    candidate_b: _CandidateEvidence,
    quarantine_event_id_count: int,
) -> dict[str, object]:
    second = candidate_a.second
    if second is None:
        raise RecoveryParityRefusal("candidate A did not execute its required rerun")
    a_sequence_delta = (
        candidate_a.first.canonical_sequence_after
        - candidate_a.first.canonical_sequence_before
    )
    a_rerun_sequence_delta = (
        second.canonical_sequence_after - second.canonical_sequence_before
    )
    b_sequence_delta = (
        candidate_b.first.canonical_sequence_after
        - candidate_b.first.canonical_sequence_before
    )
    expected_candidates = int(
        classification.disposition_counts.get("candidate_recovery", 0)
    )
    observed_dispositions = Counter(
        draft.disposition for draft in classification.drafts
    )
    classification_conservation = all(
        (
            sum(int(value) for value in classification.disposition_counts.values())
            == inventory_line_count,
            all(
                int(classification.disposition_counts.get(disposition, 0))
                == int(observed_dispositions.get(disposition, 0))
                for disposition in _DISPOSITIONS
            ),
        )
    )
    semantic_match = (
        candidate_a.semantic_fingerprint == candidate_b.semantic_fingerprint
    )
    a_opaque = _opaque_task_ids(candidate_a.task_ids)
    b_opaque = _opaque_task_ids(candidate_b.task_ids)
    product_oracle = dict(candidate_a.product_oracle)
    product_oracle["candidate_reports_match"] = (
        dict(candidate_a.product_oracle) == dict(candidate_b.product_oracle)
    )
    report: dict[str, object] = {
        "archive": {
            "directory_private_0700": archive_private,
            "file_count": archive_file_count,
            "files_private_0600": archive_files_private,
            "receipt_conservation": plan_length == inventory_line_count,
            "receipt_count": plan_length,
            "sealed_plan_verified": True,
        },
        "candidate_a": {
            "file_private_0600": candidate_a.file_private_0600,
            "first_canonical_writes": candidate_a.first.canonical_writes,
            "first_fact_inserted_count": _disposition_count(
                candidate_a.first, "inserted"
            ),
            "first_link_inserted_count": _link_disposition_count(
                candidate_a.first, "inserted"
            ),
            "first_projection_rebuilt": candidate_a.first.projection_rebuilt,
            "first_recovery_candidate_count": (
                candidate_a.first.recovery.candidate_rows
            ),
            "first_recovery_conservation": all(
                (
                    candidate_a.first.recovery.candidate_rows
                    == expected_candidates,
                    _disposition_count(candidate_a.first, "inserted")
                    == expected_candidates,
                    _link_disposition_count(candidate_a.first, "inserted")
                    == expected_candidates,
                )
            ),
            "first_scoped_import_parity_match": (
                _scoped_import_parity_matches(candidate_a.first)
            ),
            "first_sequence_delta": a_sequence_delta,
            "opaque_task_ids_valid": a_opaque,
            "quarantine_canonical_fact_count": (
                candidate_a.quarantine_fact_count
            ),
            "quarantine_event_id_count": quarantine_event_id_count,
            "quarantine_excluded_from_canonical": (
                candidate_a.quarantine_fact_count == 0
            ),
            "rerun_canonical_writes": second.canonical_writes,
            "rerun_fact_noop_count": _disposition_count(second, "noop"),
            "rerun_link_noop_count": _link_disposition_count(second, "noop"),
            "rerun_projection_rebuilt": second.projection_rebuilt,
            "rerun_recovery_conservation": all(
                (
                    second.recovery.candidate_rows == expected_candidates,
                    _disposition_count(second, "noop") == expected_candidates,
                    _link_disposition_count(second, "noop")
                    == expected_candidates,
                )
            ),
            "rerun_scoped_import_parity_match": (
                _scoped_import_parity_matches(second)
            ),
            "rerun_sequence_delta": a_rerun_sequence_delta,
            "rerun_table_counts_stable": (
                candidate_a.rerun_table_counts_stable
            ),
            "rerun_task_ids_stable": candidate_a.rerun_task_ids_stable,
            "rerun_total_changes_delta": candidate_a.rerun_total_changes_delta,
            "sidecar_file_count": candidate_a.sidecar_file_count,
            "task_count": len(candidate_a.task_ids),
            "task_ids_present_when_recovery_nonzero": (
                expected_candidates == 0 or bool(candidate_a.task_ids)
            ),
        },
        "candidate_b": {
            "file_private_0600": candidate_b.file_private_0600,
            "first_canonical_writes": candidate_b.first.canonical_writes,
            "first_fact_inserted_count": _disposition_count(
                candidate_b.first, "inserted"
            ),
            "first_link_inserted_count": _link_disposition_count(
                candidate_b.first, "inserted"
            ),
            "first_projection_rebuilt": candidate_b.first.projection_rebuilt,
            "first_recovery_candidate_count": (
                candidate_b.first.recovery.candidate_rows
            ),
            "first_recovery_conservation": all(
                (
                    candidate_b.first.recovery.candidate_rows
                    == expected_candidates,
                    _disposition_count(candidate_b.first, "inserted")
                    == expected_candidates,
                    _link_disposition_count(candidate_b.first, "inserted")
                    == expected_candidates,
                )
            ),
            "first_scoped_import_parity_match": (
                _scoped_import_parity_matches(candidate_b.first)
            ),
            "first_sequence_delta": b_sequence_delta,
            "opaque_task_ids_valid": b_opaque,
            "quarantine_canonical_fact_count": (
                candidate_b.quarantine_fact_count
            ),
            "quarantine_excluded_from_canonical": (
                candidate_b.quarantine_fact_count == 0
            ),
            "sidecar_file_count": candidate_b.sidecar_file_count,
            "task_count": len(candidate_b.task_ids),
            "task_ids_present_when_recovery_nonzero": (
                expected_candidates == 0 or bool(candidate_b.task_ids)
            ),
        },
        "classification": {
            **{
                disposition: int(
                    classification.disposition_counts.get(disposition, 0)
                )
                for disposition in _DISPOSITIONS
            },
            "classification_conservation": classification_conservation,
        },
        "completed": True,
        "inventory": {
            "file_count": inventory_file_count,
            "parsed_object_event_count": classification.parsed_object_events,
            "physical_line_count": inventory_line_count,
            "source_byte_count": inventory_byte_count,
        },
        "offline_boundary": {
            "all_inputs_absolute": True,
            "configured_live_roots_disjoint": True,
            "run_directory_private_0700": run_private,
            "scratch_private_0700": scratch_private,
            "snapshot_verified": True,
        },
        "product_oracle": product_oracle,
        "parity": {
            "canonical_sequence_match": (
                candidate_a.canonical_sequence == candidate_b.canonical_sequence
            ),
            "opaque_task_ids_valid": a_opaque and b_opaque,
            "runtime_identity_and_time_excluded": True,
            "semantic_match": semantic_match,
            "semantic_row_count_a": candidate_a.semantic_rows,
            "semantic_row_count_b": candidate_b.semantic_rows,
            "semantic_row_count_match": (
                candidate_a.semantic_rows == candidate_b.semantic_rows
            ),
            "table_counts_match": (
                dict(candidate_a.table_counts) == dict(candidate_b.table_counts)
            ),
            "task_count_match": (
                len(candidate_a.task_ids) == len(candidate_b.task_ids)
            ),
        },
        "passed": False,
    }
    candidate_a_report = report["candidate_a"]
    candidate_b_report = report["candidate_b"]
    parity_report = report["parity"]
    boundary_report = report["offline_boundary"]
    archive_report = report["archive"]
    assert isinstance(candidate_a_report, Mapping)
    assert isinstance(candidate_b_report, Mapping)
    assert isinstance(parity_report, Mapping)
    assert isinstance(boundary_report, Mapping)
    assert isinstance(archive_report, Mapping)
    report["passed"] = _acceptance_passed(
        report,
        expected_candidates=expected_candidates,
        expected_quarantine_event_id_count=quarantine_event_id_count,
        inventory_line_count=inventory_line_count,
    )
    _validate_report(report)
    return report


def _product_oracle_accepted(
    report: Mapping[str, object],
    *,
    expected_candidates: int,
    expected_quarantine_event_id_count: int,
) -> bool:
    required_true = (
        "source_manifest_verified",
        "existing_product_functions_used",
        "source_adapter_conservation",
        "candidate_adapter_conservation",
        "adapter_contract_match",
        "claim_fields_match",
        "task_membership_match",
        "existing_product_work_context_rows_match",
        "coverage_conservation",
        "target_work_identities_match",
        "target_work_identity_presence_conservation",
        "source_work_output_conservation",
        "candidate_work_output_conservation",
        "source_target_work_task_membership_conservation",
        "candidate_target_work_task_membership_conservation",
        "task_output_comparison_target_only",
        "work_outputs_match",
        "task_outputs_match",
        "quarantine_zero_contribution",
        "passed",
        "candidate_reports_match",
    )
    return all(
        (
            report.get("contract_version")
            == RECOVERY_PRODUCT_ORACLE_CONTRACT_VERSION,
            report.get("oracle_code_version")
            == RECOVERY_PRODUCT_ORACLE_CODE_VERSION,
            report.get("runner_version") == _RECOVERY_PARITY_RUNNER_VERSION,
            all(report.get(key) is True for key in required_true),
            report.get("eligible_recovery_rows") == expected_candidates,
            report.get("source_adapter_rows") == expected_candidates,
            report.get("candidate_adapter_rows") == expected_candidates,
            report.get("recovery_transport_fact_rows") == expected_candidates,
            int(report.get("recovered_section_rows") or 0)
            + int(report.get("adapter_membership_only_task_rows") or 0)
            == expected_candidates,
            report.get("all_recovered_rows_have_existing_product_work_output_coverage")
            is (int(report.get("adapter_membership_only_task_rows") or 0) == 0),
            report.get("existing_product_work_covered_recovery_rows")
            == report.get("recovered_section_rows"),
            report.get("candidate_existing_product_work_covered_recovery_rows")
            == report.get("recovered_section_rows"),
            report.get("source_existing_product_work_context_rows")
            == report.get("candidate_existing_product_work_context_rows"),
            report.get("source_target_work_output_rows")
            == report.get("candidate_target_work_output_rows"),
            report.get("source_distinct_target_work_identity_rows")
            == report.get("candidate_distinct_target_work_identity_rows"),
            report.get("source_target_work_output_rows")
            == report.get("source_distinct_target_work_identity_rows"),
            report.get("candidate_target_work_output_rows")
            == report.get("candidate_distinct_target_work_identity_rows"),
            report.get("source_target_work_affected_task_occurrence_rows")
            == report.get("source_distinct_target_work_identity_rows"),
            report.get("candidate_target_work_affected_task_occurrence_rows")
            == report.get("candidate_distinct_target_work_identity_rows"),
            (
                int(report.get("source_distinct_target_work_identity_rows") or 0)
                > 0
            )
            is (int(report.get("recovered_section_rows") or 0) > 0),
            (
                int(
                    report.get("candidate_distinct_target_work_identity_rows")
                    or 0
                )
                > 0
            )
            is (int(report.get("recovered_section_rows") or 0) > 0),
            report.get("source_affected_task_output_rows")
            == report.get("candidate_affected_task_output_rows"),
            (
                report.get("source_affected_task_output_rows") == 0
                if int(report.get("source_distinct_target_work_identity_rows") or 0)
                == 0
                else 0
                < int(report.get("source_affected_task_output_rows") or 0)
                <= int(
                    report.get("source_target_work_affected_task_occurrence_rows")
                    or 0
                )
            ),
            (
                report.get("candidate_affected_task_output_rows") == 0
                if int(
                    report.get("candidate_distinct_target_work_identity_rows")
                    or 0
                )
                == 0
                else 0
                < int(report.get("candidate_affected_task_output_rows") or 0)
                <= int(
                    report.get(
                        "candidate_target_work_affected_task_occurrence_rows"
                    )
                    or 0
                )
            ),
            report.get("adapter_contract_mismatch_count") == 0,
            report.get("claim_field_mismatch_count") == 0,
            report.get("task_membership_mismatch_count") == 0,
            report.get("target_work_identity_mismatch_count") == 0,
            report.get("work_output_mismatch_count") == 0,
            report.get("task_output_mismatch_count") == 0,
            report.get("quarantine_event_ids_checked")
            == expected_quarantine_event_id_count,
            report.get("quarantine_canonical_fact_rows") == 0,
        )
    )


def _acceptance_passed(
    report: Mapping[str, object],
    *,
    expected_candidates: int,
    expected_quarantine_event_id_count: int,
    inventory_line_count: int,
) -> bool:
    candidate_a_report = report["candidate_a"]
    candidate_b_report = report["candidate_b"]
    parity_report = report["parity"]
    boundary_report = report["offline_boundary"]
    archive_report = report["archive"]
    classification_report = report["classification"]
    product_oracle_report = report["product_oracle"]
    if not all(
        isinstance(value, Mapping)
        for value in (
            candidate_a_report,
            candidate_b_report,
            parity_report,
            boundary_report,
            archive_report,
            classification_report,
            product_oracle_report,
        )
    ):
        return False
    assert isinstance(candidate_a_report, Mapping)
    assert isinstance(candidate_b_report, Mapping)
    assert isinstance(parity_report, Mapping)
    assert isinstance(boundary_report, Mapping)
    assert isinstance(archive_report, Mapping)
    assert isinstance(classification_report, Mapping)
    assert isinstance(product_oracle_report, Mapping)
    return all(
        (
            classification_report["classification_conservation"] is True,
            sum(
                int(classification_report[disposition])
                for disposition in _DISPOSITIONS
            )
            == inventory_line_count,
            archive_report["directory_private_0700"] is True,
            archive_report["files_private_0600"] is True,
            archive_report["receipt_conservation"] is True,
            archive_report["receipt_count"] == inventory_line_count,
            archive_report["sealed_plan_verified"] is True,
            candidate_a_report["file_private_0600"] is True,
            candidate_a_report["first_recovery_conservation"] is True,
            candidate_a_report["first_scoped_import_parity_match"] is True,
            candidate_a_report["first_recovery_candidate_count"]
            == expected_candidates,
            candidate_a_report["first_fact_inserted_count"]
            == expected_candidates,
            candidate_a_report["first_link_inserted_count"]
            == expected_candidates,
            candidate_a_report["first_projection_rebuilt"] is True,
            candidate_a_report["quarantine_excluded_from_canonical"] is True,
            candidate_a_report["rerun_canonical_writes"] == 0,
            candidate_a_report["rerun_recovery_conservation"] is True,
            candidate_a_report["rerun_scoped_import_parity_match"] is True,
            candidate_a_report["rerun_fact_noop_count"]
            == expected_candidates,
            candidate_a_report["rerun_link_noop_count"]
            == expected_candidates,
            candidate_a_report["rerun_projection_rebuilt"] is False,
            candidate_a_report["rerun_sequence_delta"] == 0,
            candidate_a_report["rerun_table_counts_stable"] is True,
            candidate_a_report["rerun_task_ids_stable"] is True,
            candidate_a_report["rerun_total_changes_delta"] == 0,
            candidate_a_report["sidecar_file_count"] == 0,
            candidate_a_report["task_ids_present_when_recovery_nonzero"]
            is True,
            expected_candidates == 0 or candidate_a_report["task_count"] > 0,
            candidate_b_report["file_private_0600"] is True,
            candidate_b_report["first_recovery_conservation"] is True,
            candidate_b_report["first_scoped_import_parity_match"] is True,
            candidate_b_report["first_recovery_candidate_count"]
            == expected_candidates,
            candidate_b_report["first_fact_inserted_count"]
            == expected_candidates,
            candidate_b_report["first_link_inserted_count"]
            == expected_candidates,
            candidate_b_report["first_projection_rebuilt"] is True,
            candidate_b_report["quarantine_excluded_from_canonical"] is True,
            candidate_b_report["sidecar_file_count"] == 0,
            candidate_b_report["task_ids_present_when_recovery_nonzero"]
            is True,
            expected_candidates == 0 or candidate_b_report["task_count"] > 0,
            all(value is True for value in boundary_report.values()),
            _product_oracle_accepted(
                product_oracle_report,
                expected_candidates=expected_candidates,
                expected_quarantine_event_id_count=(
                    expected_quarantine_event_id_count
                ),
            ),
            all(value is True for key, value in parity_report.items() if key not in {
                "semantic_row_count_a",
                "semantic_row_count_b",
            }),
        )
    )


def run_recovery_parity(
    arguments: argparse.Namespace,
) -> tuple[dict[str, object], Path]:
    """Execute and retain one counts-only recovery parity report."""

    snapshot_root = _absolute_argument(
        arguments.snapshot_root,
        label="--snapshot-root",
    )
    snapshot_manifest = _absolute_argument(
        arguments.snapshot_manifest,
        label="--snapshot-manifest",
    )
    declared_scratch = _absolute_argument(
        arguments.scratch_root,
        label="--scratch-root",
    )
    _live_and_pairwise_preflight(
        snapshot_root=snapshot_root,
        snapshot_manifest=snapshot_manifest,
        scratch_root=declared_scratch,
    )

    scratch_root = _require_private_scratch(declared_scratch)
    try:
        run = create_anchored_run_directory(
            scratch_root,
            prefix=_RUN_PREFIX,
            require_private_root=True,
        )
    except ScratchSafetyError as exc:
        raise RecoveryParityRefusal(str(exc)) from exc

    archive: VerifiedMigrationArchive | None = None
    try:
        # Pin and require the exact private scratch inode before a potentially
        # long manifest read, snapshot verification, or physical-line scan.
        run.prove_unchanged()
        manifest = SnapshotManifest.load(snapshot_manifest)
        if not manifest.kind_declared or manifest.kind not in _ALLOWED_MANIFEST_KINDS:
            raise RecoveryParityRefusal(
                "--snapshot-manifest must explicitly declare a supported legacy kind"
            )
        snapshot = VerifiedSnapshot.verify(snapshot_root, manifest)
        snapshot.verify_unchanged()
        run.prove_unchanged()
        inventory = scan_snapshot_lines(snapshot)
        run.prove_unchanged()
        archive_root = _create_private_archive_directory(run)
        archive = build_migration_archive(
            snapshot=snapshot,
            archive_root=archive_root,
            inventory=inventory,
        )
        classification = classify_legacy_recovery(
            snapshot=snapshot,
            inventory=inventory,
        )
        unknown = set(classification.disposition_counts) - set(_DISPOSITIONS)
        if unknown:
            raise RecoveryParityRefusal(
                "classifier produced a disposition outside the report allowlist"
            )
        archive.publish_receipts(classification.drafts)
        plan = archive.sealed_plan(design_id=RECOVERY_DESIGN_ID)
        plan.verify_unchanged()
        quarantined_event_ids = _quarantined_event_ids(archive, classification)

        candidate_a = _run_candidate(
            snapshot=snapshot,
            archive=archive,
            plan=plan,
            run=run,
            candidate_name=_CANDIDATE_A_FILE,
            quarantined_event_ids=quarantined_event_ids,
            replay_twice=True,
        )
        candidate_b = _run_candidate(
            snapshot=snapshot,
            archive=archive,
            plan=plan,
            run=run,
            candidate_name=_CANDIDATE_B_FILE,
            quarantined_event_ids=quarantined_event_ids,
            replay_twice=False,
        )
        plan.verify_unchanged()
        archive_files_private, archive_file_count = _archive_permissions(archive)
        scratch_private = _private_directory(
            scratch_root,
            label="--scratch-root",
        )
        run_private = _private_directory(
            run.path,
            label="per-run scratch directory",
        )
        archive_private = _private_directory(
            archive.path,
            label="archive directory",
        )
        report = _build_report(
            classification=classification,
            archive_file_count=archive_file_count,
            archive_files_private=archive_files_private,
            archive_private=archive_private,
            plan_length=len(plan),
            inventory_file_count=len(inventory.files),
            inventory_line_count=inventory.total_lines,
            inventory_byte_count=inventory.total_bytes,
            scratch_private=scratch_private,
            run_private=run_private,
            candidate_a=candidate_a,
            candidate_b=candidate_b,
            quarantine_event_id_count=len(quarantined_event_ids),
        )
        snapshot.verify_unchanged()
        run.prove_unchanged()
        run.atomic_write_json(_REPORT_FILE, report)
        run.prove_unchanged()
        report_path = run.child_path(_REPORT_FILE)
        if not _private_regular_file(report_path, label="retained report"):
            raise RecoveryParityRefusal("retained report must remain private")
        return report, report_path
    finally:
        if archive is not None:
            archive.close()
        run.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay one explicit manifest-verified legacy snapshot into two "
            "fresh isolated candidates and retain counts-only parity evidence."
        )
    )
    parser.add_argument("--snapshot-root", required=True)
    parser.add_argument("--snapshot-manifest", required=True)
    parser.add_argument("--scratch-root", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        report, _report_path = run_recovery_parity(arguments)
    except (RecoveryParityRefusal, SnapshotSafetyError, MigrationArchiveError, LegacyRecoveryError):
        print(
            json.dumps({"completed": False, "refused": True}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    except (OSError, sqlite3.DatabaseError, ValueError):
        print(
            json.dumps({"completed": False, "refused": False}, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    _validate_report(report)
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
