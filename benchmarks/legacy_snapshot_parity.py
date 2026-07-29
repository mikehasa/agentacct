#!/usr/bin/env python3
"""Run independent canonical parity against one sealed legacy snapshot.

This executable never discovers or copies agentacct state.  It accepts only an
explicit, already-created snapshot root plus an external SHA-256 manifest,
creates one new candidate below an explicit scratch root, and retains both the
candidate and a redacted report for inspection.  It has no cutover, adapter,
dual-write, watcher, rename, or cleanup operation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path
from typing import Any, Sequence

from agentacct.canonical.legacy_import import import_legacy_snapshot
from agentacct.canonical.product_parity import (
    ProductParityError,
    SUPPORTED_LEGACY_MANIFEST_KINDS,
    build_legacy_product_parity_report,
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
from agentacct.store_resolution import (
    ENV_STORE_DIR,
    LEGACY_ENV_STORE_DIR,
)


RUNNER_SCHEMA_VERSION = "agent-chronicle.legacy-parity-runner.v3"
_FORBIDDEN_SCRATCH_COMPONENTS = frozenset(
    {
        ".agent-sentinel",
        ".agent-sentinel-global",
        ".agent-chronicle",
        ".agent-chronicle-global",
        ".codex",
    }
)


class ParityRunnerRefusal(SnapshotSafetyError):
    """The requested source or destination is outside the offline boundary."""


def _remove_checkpointed_candidate_sidecars(
    run_directory: AnchoredRunDirectory,
    candidate_name: str,
) -> None:
    """Seal the runner-owned WAL database as one transportable file.

    SQLite can retain an empty WAL and a non-empty shared-memory index after a
    successful TRUNCATE checkpoint and final close. Both are disposable only
    after that close. The private, descriptor-anchored run directory lets the
    runner remove exactly its own validated sidecars without pathname
    discovery or touching a caller-owned database.
    """

    journal_name = f"{candidate_name}-journal"
    try:
        run_directory.stat_child(journal_name)
    except FileNotFoundError:
        pass
    else:
        raise ParityRunnerRefusal(
            "retained candidate still has a rollback journal after close"
        )

    removed = False
    for suffix in ("-wal", "-shm"):
        name = f"{candidate_name}{suffix}"
        try:
            observed = run_directory.stat_child(name)
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or observed.st_uid != os.geteuid()
            or stat.S_IMODE(observed.st_mode) != 0o600
        ):
            raise ParityRunnerRefusal(
                "retained candidate sidecars must be private, unique regular files"
            )
        if suffix == "-wal" and observed.st_size != 0:
            raise ParityRunnerRefusal(
                "retained candidate WAL was not fully checkpointed"
            )
        descriptor = run_directory.open_file(name, os.O_RDONLY)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino):
                raise ParityRunnerRefusal(
                    "retained candidate sidecar changed before sealing"
                )
        finally:
            os.close(descriptor)
        latest = run_directory.stat_child(name)
        if (latest.st_dev, latest.st_ino) != (observed.st_dev, observed.st_ino):
            raise ParityRunnerRefusal(
                "retained candidate sidecar changed before sealing"
            )
        os.unlink(name, dir_fd=run_directory.descriptor)
        removed = True
    if removed:
        os.fsync(run_directory.descriptor)
    run_directory.prove_unchanged()


def _resolved_boundary_path(raw: str, *, label: str) -> Path:
    """Resolve a caller-declared path without opening or scanning its contents."""

    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ParityRunnerRefusal(f"{label} must be an explicit absolute path")
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ParityRunnerRefusal(f"{label} cannot be resolved safely") from exc


def _configured_live_store_roots() -> tuple[Path, ...]:
    """Return configured/default live roots without resolving a store or reading it."""

    home = Path.home().expanduser()
    roots = {
        # Retired historical fallback and documented machine-wide store.
        home / ".agent-sentinel",
        home / ".agent-sentinel-global" / "state",
        # Fail closed for installations that adopted the product rename in a
        # custom wrapper even though the repository keeps the old names.
        home / ".agent-chronicle",
        home / ".agent-chronicle-global" / "state",
        # Snapshot verification also protects Codex inputs; putting it in the
        # runner preflight prevents a custom CODEX_HOME from being scanned.
        home / ".codex",
    }
    for variable in (ENV_STORE_DIR, LEGACY_ENV_STORE_DIR, "CODEX_HOME"):
        value = (os.environ.get(variable) or "").strip()
        if not value:
            continue
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        roots.add(candidate)

    resolved: set[Path] = set()
    for root in roots:
        try:
            resolved.add(root.resolve(strict=False))
        except (OSError, RuntimeError) as exc:
            raise ParityRunnerRefusal(
                "configured live store root cannot be resolved safely"
            ) from exc
    return tuple(sorted(resolved, key=str))


def _paths_overlap(left: Path, right: Path) -> bool:
    return (
        left == right
        or left.is_relative_to(right)
        or right.is_relative_to(left)
    )


def _require_offline_paths_disjoint_from_live_roots(
    *,
    snapshot_root: Path,
    snapshot_manifest: Path,
    scratch_root: Path,
) -> None:
    """Fail before snapshot verification or scratch creation on any overlap."""

    declared_paths = (
        ("--snapshot-root", snapshot_root),
        ("--snapshot-manifest", snapshot_manifest),
        ("--scratch-root", scratch_root),
    )
    live_roots = _configured_live_store_roots()
    for label, path in declared_paths:
        if any(
            _paths_overlap(path, live_root)
            for live_root in live_roots
        ):
            raise ParityRunnerRefusal(
                f"{label} must be disjoint from every configured live store root"
            )


def _assert_no_symlink_components(path: Path, *, label: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            observed = current.lstat()
        except OSError as exc:
            raise ParityRunnerRefusal(f"{label} must be an existing directory") from exc
        if stat.S_ISLNK(observed.st_mode):
            raise ParityRunnerRefusal(f"{label} may not contain symlink components")


def _require_scratch_root(raw: Path | str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ParityRunnerRefusal("--scratch-root must be an explicit absolute path")
    _assert_no_symlink_components(path, label="--scratch-root")
    observed = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(observed.st_mode):
        raise ParityRunnerRefusal("--scratch-root must be a real directory")
    resolved = path.resolve(strict=True)
    if any(
        part.casefold() in _FORBIDDEN_SCRATCH_COMPONENTS
        for part in resolved.parts
    ):
        raise ParityRunnerRefusal(
            "--scratch-root may not be inside live agentacct or Codex state"
        )
    return resolved


def _require_disjoint_snapshot_and_scratch(
    *,
    snapshot: VerifiedSnapshot,
    scratch_root: Path,
) -> None:
    if (
        snapshot.root == scratch_root
        or snapshot.root.is_relative_to(scratch_root)
        or scratch_root.is_relative_to(snapshot.root)
    ):
        raise ParityRunnerRefusal(
            "--scratch-root and --snapshot-root must be disjoint directories"
        )


def _public_task_ids(store: CanonicalStore) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in store.connection.execute(
            "SELECT public_task_id FROM task_anchors ORDER BY task_anchor_id"
        ).fetchall()
    )


def _opaque_task_ids_are_valid(task_ids: tuple[str, ...]) -> bool:
    return all(
        value.startswith("task_")
        and len(value) == 37
        and all(character in "0123456789abcdef" for character in value[5:])
        for value in task_ids
    )


def run_parity(arguments: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    """Execute one retained offline parity run and return its redacted report."""

    snapshot_root = _resolved_boundary_path(
        arguments.snapshot_root,
        label="--snapshot-root",
    )
    snapshot_manifest = _resolved_boundary_path(
        arguments.snapshot_manifest,
        label="--snapshot-manifest",
    )
    declared_scratch_root = _resolved_boundary_path(
        arguments.scratch_root,
        label="--scratch-root",
    )
    _require_offline_paths_disjoint_from_live_roots(
        snapshot_root=snapshot_root,
        snapshot_manifest=snapshot_manifest,
        scratch_root=declared_scratch_root,
    )

    scratch_root = _require_scratch_root(declared_scratch_root)
    manifest = SnapshotManifest.load(snapshot_manifest)
    if not manifest.kind_declared:
        raise ParityRunnerRefusal(
            "--snapshot-manifest must explicitly declare kind for legacy parity"
        )
    if manifest.kind not in SUPPORTED_LEGACY_MANIFEST_KINDS:
        raise ParityRunnerRefusal(
            "--snapshot-manifest kind must be legacy or legacy-chronicle"
        )
    snapshot = VerifiedSnapshot.verify(
        snapshot_root,
        manifest,
    )
    _require_disjoint_snapshot_and_scratch(
        snapshot=snapshot,
        scratch_root=scratch_root,
    )
    # Prove the named core file is manifest-declared before creating anything.
    snapshot.path_for(arguments.source_file)

    try:
        run_directory = create_anchored_run_directory(
            scratch_root,
            prefix="legacy-snapshot-parity-",
        )
    except ScratchSafetyError as exc:
        raise ParityRunnerRefusal(str(exc)) from exc
    try:
        return _run_parity_in_directory(
            arguments,
            snapshot=snapshot,
            run_directory=run_directory,
        )
    finally:
        run_directory.close()


def _run_parity_in_directory(
    arguments: argparse.Namespace,
    *,
    snapshot: VerifiedSnapshot,
    run_directory: AnchoredRunDirectory,
) -> tuple[dict[str, Any], Path]:
    run_directory.prove_unchanged()
    run_dir = run_directory.path
    candidate_path = snapshot.validate_candidate_target(
        run_dir / "candidate.sqlite3",
        scratch_root=run_dir,
    )
    report_path = run_dir / "parity-report.json"

    run_directory.prove_unchanged()
    store = CanonicalStore.create(candidate_path)
    try:
        run_directory.prove_unchanged()
        first = import_legacy_snapshot(
            snapshot=snapshot,
            store=store,
            scratch_root=run_dir,
            source_file=arguments.source_file,
        )
        first_counts = dict(store.repository().table_counts())
        task_ids_before = _public_task_ids(store)
        sequence_before = store.repository().canonical_sequence()
        changes_before = store.connection.total_changes

        second = import_legacy_snapshot(
            snapshot=snapshot,
            store=store,
            scratch_root=run_dir,
            source_file=arguments.source_file,
        )
        task_ids_after = _public_task_ids(store)
        rerun_evidence: dict[str, object] = {
            "canonical_writes": store.connection.total_changes - changes_before,
            "canonical_sequence_delta": (
                store.repository().canonical_sequence() - sequence_before
            ),
            "task_ids_stable": task_ids_after == task_ids_before,
            "opaque_task_ids_valid": _opaque_task_ids_are_valid(task_ids_after),
            "task_id_count": len(task_ids_after),
            "table_counts_stable": (
                dict(store.repository().table_counts()) == first_counts
            ),
            "projection_rebuilt": second.projection_rebuilt,
            "second_import": {
                "write_dispositions": second.write_dispositions,
                "internal_parity_matches": second.parity.matches,
                "internal_parity_difference_keys": sorted(
                    second.parity.differences
                ),
            },
        }
        report = build_legacy_product_parity_report(
            snapshot=snapshot,
            repository=store.repository(),
            migration=first,
            source_file=arguments.source_file,
            legacy_store_scope=arguments.legacy_store_scope,
            rerun_evidence=rerun_evidence,
        )
        report["runner"] = {
            "schema_version": RUNNER_SCHEMA_VERSION,
            "execution_completed": True,
            "candidate_retained": True,
            "candidate_file": candidate_path.name,
            "report_file": report_path.name,
            "first_import_internal_parity_matches": first.parity.matches,
            "second_import_internal_parity_matches": second.parity.matches,
        }
        snapshot.verify_unchanged()
        run_directory.prove_unchanged()
        store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        store.close()

    _remove_checkpointed_candidate_sidecars(run_directory, candidate_path.name)
    run_directory.prove_unchanged()
    candidate_stat = run_directory.stat_child(candidate_path.name)
    if (
        not stat.S_ISREG(candidate_stat.st_mode)
        or candidate_stat.st_nlink != 1
        or stat.S_IMODE(candidate_stat.st_mode) != 0o600
    ):
        raise ParityRunnerRefusal(
            "retained candidate must be a private, non-aliased regular file"
        )
    candidate_descriptor = run_directory.open_file(candidate_path.name, os.O_RDONLY)
    with os.fdopen(candidate_descriptor, "rb", closefd=True) as candidate_handle:
        opened_candidate = os.fstat(candidate_handle.fileno())
        if (
            (opened_candidate.st_dev, opened_candidate.st_ino)
            != (candidate_stat.st_dev, candidate_stat.st_ino)
            or opened_candidate.st_size != candidate_stat.st_size
        ):
            raise ParityRunnerRefusal(
                "retained candidate changed before its digest was sealed"
            )
        report["runner"]["candidate_sha256"] = hashlib.file_digest(
            candidate_handle, "sha256"
        ).hexdigest()
        after_hash = os.fstat(candidate_handle.fileno())
        if (
            (after_hash.st_dev, after_hash.st_ino)
            != (opened_candidate.st_dev, opened_candidate.st_ino)
            or after_hash.st_size != opened_candidate.st_size
            or after_hash.st_mtime_ns != opened_candidate.st_mtime_ns
            or after_hash.st_ctime_ns != opened_candidate.st_ctime_ns
        ):
            raise ParityRunnerRefusal(
                "retained candidate changed while its digest was sealed"
            )
    report["runner"]["candidate_size_bytes"] = candidate_stat.st_size
    report["status"] = (
        "passed"
        if report["acceptance"]["core_truth_slice_passed"] is True
        else "failed"
    )
    run_directory.atomic_write_json(report_path.name, report)
    run_directory.prove_unchanged()
    return report, report_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare existing-product core truth with a canonical candidate "
            "using only an explicit manifest-verified legacy snapshot."
        )
    )
    parser.add_argument("--scratch-root", required=True)
    parser.add_argument("--snapshot-root", required=True)
    parser.add_argument("--snapshot-manifest", required=True)
    parser.add_argument("--source-file", default="events.jsonl")
    parser.add_argument(
        "--legacy-store-scope",
        required=True,
        choices=("project", "custom"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        report, report_path = run_parity(arguments)
    except (SnapshotSafetyError, ProductParityError) as exc:
        print(
            json.dumps({"status": "refused", "error": str(exc)}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    payload = {
        "status": report["status"],
        "decision": report["decision"],
        "cutover_decision": report["cutover_decision"],
        "manifest_sha256": report["snapshot"]["manifest_sha256"],
        "result_path": str(report_path),
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
