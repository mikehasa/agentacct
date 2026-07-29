"""Fail-closed canonical candidate promotion and rollback primitives.

This module is deliberately path-explicit.  It never discovers a store and it
never creates a candidate: phase 5 may promote only the exact candidate sealed
by a successful legacy parity-runner v3 report.  The caller is responsible for
stopping every agentacct writer before calling these functions.

Promotion copies the sealed candidate to a sibling staging file, normalizes the
one importer/live-writer adapter seam, changes the store role there, fsyncs it,
backs up the disposable shadow, and atomically replaces the reserved live name.
The source candidate and parity report remain untouched.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..canonical_live import LIVE_EVENTS_ADAPTER
from .legacy_import import LEGACY_EVENTS_ADAPTER, LEGACY_EVENTS_REPRESENTATION
from .migration_disposition_policy import (
    build_migration_disposition_policy_evidence,
)
from .product_parity import PRODUCT_PARITY_SCHEMA_VERSION
from .sqlite import (
    APPLICATION_ID,
    LIVE_STORE_FILENAME,
    MINIMAL_READ_MODEL_NAMES,
    SCHEMA_VERSION,
    CanonicalStore,
)


PARITY_RUNNER_SCHEMA_VERSION = "agent-chronicle.legacy-parity-runner.v3"
CUTOVER_RECEIPT_VERSION = "agent-chronicle.canonical-cutover.v1"
_BACKUP_PREFIX = f".{LIVE_STORE_FILENAME}.shadow-backup-"
_BACKUP_SUFFIX = ".sqlite3"
_PROMOTE_STAGE_PREFIX = f".{LIVE_STORE_FILENAME}.promote-"
_ROLLBACK_STAGE_PREFIX = f".{LIVE_STORE_FILENAME}.rollback-"
_STAGE_SUFFIX = ".stage"
_REPORT_SIZE_LIMIT = 16 * 1024 * 1024
_RECEIPT_SIZE_LIMIT = 256 * 1024
_COPY_CHUNK_SIZE = 1024 * 1024
_CORE_PARITY_SURFACES = frozenset(
    {
        "sessions",
        "task_membership",
        "usage_presence",
        "usage_aggregates",
    }
)


class CutoverError(RuntimeError):
    """Base class for a refused or failed cutover operation."""


class CutoverPreparationError(CutoverError):
    """The candidate is not bound to acceptable parity evidence."""


class CutoverPromotionError(CutoverError):
    """The prepared candidate cannot be installed safely."""


class CutoverReceiptPersistenceError(CutoverError):
    """Promotion completed, but its durable receipt could not be written."""

    def __init__(
        self,
        receipt: "PromotionReceipt",
        receipt_path: Path,
        cause: BaseException,
        *,
        fallback_receipt_path: Path | None,
    ) -> None:
        super().__init__(
            "canonical promotion completed but the durable receipt could not be written: "
            f"{type(cause).__name__}: {cause}"
        )
        self.receipt = receipt
        self.receipt_path = receipt_path
        self.cause = cause
        self.fallback_receipt_path = fallback_receipt_path


class CutoverPostReplaceError(CutoverError):
    """Promotion crossed (or may have crossed) replacement before failing."""

    def __init__(
        self,
        receipt: "PromotionReceipt",
        cause: BaseException,
        *,
        fallback_receipt_path: Path | None,
        replacement_state: str,
    ) -> None:
        if replacement_state not in {"installed", "ambiguous"}:
            raise ValueError("promotion replacement state must be installed or ambiguous")
        super().__init__(
            "canonical promotion replacement state is "
            f"{replacement_state}; later work failed: "
            f"{type(cause).__name__}: {cause}"
        )
        self.receipt = receipt
        self.cause = cause
        self.fallback_receipt_path = fallback_receipt_path
        self.replacement_state = replacement_state


class CutoverVerificationError(CutoverError):
    """The atomic replacement completed but post-install verification failed."""

    def __init__(
        self,
        receipt: "PromotionReceipt",
        verification: "CutoverVerification",
    ) -> None:
        super().__init__("promoted canonical store failed post-install verification")
        self.receipt = receipt
        self.verification = verification


class RollbackPostReplaceError(CutoverError):
    """Rollback crossed (or may have crossed) replacement before failing."""

    def __init__(
        self,
        receipt: "RollbackReceipt | None",
        cause: BaseException,
        *,
        replacement_state: str,
        verification: "CutoverVerification | None" = None,
    ) -> None:
        if replacement_state not in {"installed", "ambiguous"}:
            raise ValueError("rollback replacement state must be installed or ambiguous")
        super().__init__(
            "canonical rollback replacement state is "
            f"{replacement_state}; later work failed: {type(cause).__name__}: {cause}"
        )
        self.receipt = receipt
        self.cause = cause
        self.replacement_state = replacement_state
        self.verification = verification


@dataclass(frozen=True)
class ProjectionReadiness:
    projection_name: str
    projection_version: int
    built_through_sequence: int
    state: str

    def to_dict(self) -> dict[str, object]:
        return {
            "projection_name": self.projection_name,
            "projection_version": self.projection_version,
            "built_through_sequence": self.built_through_sequence,
            "state": self.state,
        }


@dataclass(frozen=True)
class CutoverPreparationReceipt:
    receipt_version: str
    prepared_at_us: int
    candidate_path: Path
    parity_report_path: Path
    candidate_sha256: str
    parity_report_sha256: str
    candidate_size_bytes: int
    candidate_device: int
    candidate_inode: int
    candidate_store_uuid: str
    schema_version: int
    canonical_sequence: int
    projections: tuple[ProjectionReadiness, ...]
    adapter_rows_to_normalize: int

    def to_dict(self) -> dict[str, object]:
        return {
            "receipt_version": self.receipt_version,
            "prepared_at_us": self.prepared_at_us,
            "candidate_path": str(self.candidate_path),
            "parity_report_path": str(self.parity_report_path),
            "candidate_sha256": self.candidate_sha256,
            "parity_report_sha256": self.parity_report_sha256,
            "candidate_size_bytes": self.candidate_size_bytes,
            "candidate_device": self.candidate_device,
            "candidate_inode": self.candidate_inode,
            "candidate_store_uuid": self.candidate_store_uuid,
            "schema_version": self.schema_version,
            "canonical_sequence": self.canonical_sequence,
            "projections": [item.to_dict() for item in self.projections],
            "adapter_rows_to_normalize": self.adapter_rows_to_normalize,
        }


@dataclass(frozen=True)
class PromotionReceipt:
    receipt_version: str
    operation_id: str
    promoted_at_us: int
    preparation: CutoverPreparationReceipt
    live_path: Path
    backup_path: Path
    previous_live_store_uuid: str
    previous_live_sha256: str
    previous_live_size_bytes: int
    promoted_store_uuid: str
    promoted_sha256: str
    promoted_size_bytes: int
    promoted_device: int
    promoted_inode: int
    normalized_source_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "receipt_version": self.receipt_version,
            "operation_id": self.operation_id,
            "promoted_at_us": self.promoted_at_us,
            "preparation": self.preparation.to_dict(),
            "live_path": str(self.live_path),
            "backup_path": str(self.backup_path),
            "previous_live_store_uuid": self.previous_live_store_uuid,
            "previous_live_sha256": self.previous_live_sha256,
            "previous_live_size_bytes": self.previous_live_size_bytes,
            "promoted_store_uuid": self.promoted_store_uuid,
            "promoted_sha256": self.promoted_sha256,
            "promoted_size_bytes": self.promoted_size_bytes,
            "promoted_device": self.promoted_device,
            "promoted_inode": self.promoted_inode,
            "normalized_source_count": self.normalized_source_count,
        }


@dataclass(frozen=True)
class CutoverVerification:
    verified_at_us: int
    ok: bool
    live_store_uuid: str | None
    canonical_sequence: int | None
    live_sha256: str | None
    checks: tuple[str, ...]
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "verified_at_us": self.verified_at_us,
            "ok": self.ok,
            "live_store_uuid": self.live_store_uuid,
            "canonical_sequence": self.canonical_sequence,
            "live_sha256": self.live_sha256,
            "checks": list(self.checks),
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class PromotionResult:
    receipt: PromotionReceipt
    verification: CutoverVerification

    def to_dict(self) -> dict[str, object]:
        return {
            "receipt": self.receipt.to_dict(),
            "verification": self.verification.to_dict(),
        }


@dataclass(frozen=True)
class RollbackReceipt:
    receipt_version: str
    operation_id: str
    promotion_operation_id: str
    rolled_back_at_us: int
    live_path: Path
    backup_path: Path
    replaced_store_uuid: str
    restored_store_uuid: str
    restored_sha256: str
    restored_size_bytes: int
    restored_device: int
    restored_inode: int

    def to_dict(self) -> dict[str, object]:
        return {
            "receipt_version": self.receipt_version,
            "operation_id": self.operation_id,
            "promotion_operation_id": self.promotion_operation_id,
            "rolled_back_at_us": self.rolled_back_at_us,
            "live_path": str(self.live_path),
            "backup_path": str(self.backup_path),
            "replaced_store_uuid": self.replaced_store_uuid,
            "restored_store_uuid": self.restored_store_uuid,
            "restored_sha256": self.restored_sha256,
            "restored_size_bytes": self.restored_size_bytes,
            "restored_device": self.restored_device,
            "restored_inode": self.restored_inode,
        }


@dataclass(frozen=True)
class RollbackResult:
    receipt: RollbackReceipt
    verification: CutoverVerification

    def to_dict(self) -> dict[str, object]:
        return {
            "receipt": self.receipt.to_dict(),
            "verification": self.verification.to_dict(),
        }


@dataclass(frozen=True)
class _StoreReadiness:
    store_uuid: str
    schema_version: int
    canonical_sequence: int
    projections: tuple[ProjectionReadiness, ...]
    importer_adapter_count: int
    migration_issues: tuple[tuple[str, str, int, int], ...]


def _now_us() -> int:
    return time.time_ns() // 1_000


def _identity(observed: os.stat_result) -> tuple[int, int]:
    return int(observed.st_dev), int(observed.st_ino)


def _assert_absolute(path: Path, *, label: str) -> None:
    if not path.is_absolute():
        raise CutoverPreparationError(f"{label} path must be absolute")


def _assert_no_symlink_components(path: Path, *, label: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            observed = current.lstat()
        except OSError as exc:
            raise CutoverError(f"{label} path cannot be inspected safely: {current}") from exc
        if stat.S_ISLNK(observed.st_mode):
            raise CutoverError(f"{label} path may not contain symlinks: {current}")


def _require_private_regular(path: Path, *, label: str) -> os.stat_result:
    _assert_no_symlink_components(path, label=label)
    observed = path.lstat()
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise CutoverError(f"{label} must be a unique regular non-symlink file")
    if observed.st_uid != os.geteuid() or stat.S_IMODE(observed.st_mode) != 0o600:
        raise PermissionError(f"{label} must be owned by the current user and mode 0600")
    return observed


def _require_private_run_directory(path: Path) -> os.stat_result:
    _assert_no_symlink_components(path, label="parity run directory")
    observed = path.lstat()
    if not stat.S_ISDIR(observed.st_mode) or observed.st_nlink < 1:
        raise CutoverPreparationError("parity run directory must be a real directory")
    if observed.st_uid != os.geteuid() or stat.S_IMODE(observed.st_mode) != 0o700:
        raise PermissionError(
            "parity run directory must be owned by the current user and mode 0700"
        )
    return observed


def _open_store_root(store_root: Path | str) -> tuple[Path, int]:
    root = Path(store_root).expanduser()
    if not root.is_absolute():
        raise CutoverPromotionError("live store root must be an absolute path")
    _assert_no_symlink_components(root, label="live store root")
    observed = root.lstat()
    if not stat.S_ISDIR(observed.st_mode):
        raise CutoverPromotionError("live store root must be an existing directory")
    # Existing agentacct roots may be 0755, but only their owner may be able
    # to replace names inside them.
    if observed.st_uid != os.geteuid() or stat.S_IMODE(observed.st_mode) & 0o022:
        raise PermissionError("live store root must be owned by the current user and not group/world writable")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(root, flags)
    try:
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(observed):
            raise CutoverPromotionError("live store root changed during validation")
        resolved = root.resolve(strict=True)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    return resolved, descriptor


def _reject_sidecars(path: Path, *, label: str) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{path}{suffix}")
        try:
            sidecar.lstat()
        except FileNotFoundError:
            continue
        raise CutoverError(
            f"{label} has SQLite sidecar {sidecar.name}; stop writers and checkpoint before cutover"
        )


def _private_sidecar_stat(path: Path, *, label: str) -> os.stat_result | None:
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or observed.st_uid != os.geteuid()
        or stat.S_IMODE(observed.st_mode) != 0o600
    ):
        raise CutoverError(f"{label} must be a private unique regular file")
    return observed


def _seal_stopped_live_store(path: Path) -> None:
    """Checkpoint and remove only sidecars proven to belong to a stopped DB."""

    main_before = _require_private_regular(path, label="live store")
    journal = Path(f"{path}-journal")
    if _private_sidecar_stat(journal, label="live store rollback journal") is not None:
        raise CutoverError("live store has a rollback journal; refusing cutover")
    # Validate every pre-existing name before SQLite is allowed to inspect it.
    initial_sidecars: dict[Path, os.stat_result] = {}
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        observed = _private_sidecar_stat(
            sidecar,
            label=f"live store {suffix[1:].upper()} sidecar",
        )
        if observed is not None:
            initial_sidecars[sidecar] = observed

    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=rw",
        uri=True,
        timeout=0,
        isolation_level=None,
    )
    try:
        connection.execute("PRAGMA busy_timeout = 0")
        connection.execute("PRAGMA synchronous = FULL")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ROLLBACK")
        except sqlite3.OperationalError as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise CutoverError(
                "live store is busy; stop every SQLite writer before cutover"
            ) from exc
        try:
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.OperationalError as exc:
            raise CutoverError("live store WAL checkpoint could not complete") from exc
        if (
            checkpoint is None
            or int(checkpoint[0]) != 0
            or int(checkpoint[1]) != 0
        ):
            raise CutoverError(
                "live store WAL is busy or retained frames after TRUNCATE checkpoint"
            )
    finally:
        connection.close()

    main_after = _require_private_regular(path, label="live store")
    if _identity(main_after) != _identity(main_before):
        raise CutoverError("live store inode changed while sealing SQLite sidecars")
    sidecars: list[tuple[Path, os.stat_result]] = []
    wal = Path(f"{path}-wal")
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        observed = _private_sidecar_stat(
            sidecar,
            label=f"live store {suffix[1:].upper()} sidecar",
        )
        if observed is not None:
            initial = initial_sidecars.get(sidecar)
            if initial is not None and _identity(observed) != _identity(initial):
                raise CutoverError("live store SQLite sidecar identity changed while sealing")
            sidecars.append((sidecar, observed))
    wal_stat = next((observed for sidecar, observed in sidecars if sidecar == wal), None)
    if wal_stat is not None and wal_stat.st_size != 0:
        raise CutoverError("live store WAL retained bytes after TRUNCATE checkpoint")

    main_descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(main_descriptor)
    finally:
        os.close(main_descriptor)
    root, directory_descriptor = _open_store_root(path.parent)
    try:
        if root / path.name != path:
            raise CutoverError("live store root changed while sealing SQLite sidecars")
        for sidecar, expected in sidecars:
            current = sidecar.lstat()
            if _identity(current) != _identity(expected):
                raise CutoverError("live store SQLite sidecar changed during sealing")
            os.unlink(sidecar.name, dir_fd=directory_descriptor)
        _fsync_directory(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _assert_not_busy(path: Path, *, label: str) -> None:
    """Take and release SQLite's write reservation with zero wait."""

    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=rw",
        uri=True,
        timeout=0,
        isolation_level=None,
    )
    try:
        connection.execute("PRAGMA busy_timeout = 0")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise CutoverError(f"{label} is busy; stop every SQLite writer before cutover") from exc
    finally:
        connection.close()
    _remove_clean_sidecars_created_by_probe(path, label=label)


def _remove_clean_sidecars_created_by_probe(path: Path, *, label: str) -> None:
    """Remove only inert WAL lock files created by our own clean-store probe.

    Callers reject every pre-existing sidecar before probing.  SQLite on
    macOS keeps a zero-byte WAL and a 32 KiB SHM after the last connection;
    leaving those probe artifacts would make the immediately following
    sidecar gate reject its own evidence.  A non-empty WAL is never removed.
    """

    wal = Path(f"{path}-wal")
    shm = Path(f"{path}-shm")
    journal = Path(f"{path}-journal")
    try:
        journal.lstat()
    except FileNotFoundError:
        pass
    else:
        raise CutoverError(f"{label} acquired an unexpected rollback journal")
    observed: list[tuple[Path, os.stat_result]] = []
    for sidecar in (wal, shm):
        try:
            sidecar_stat = sidecar.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(sidecar_stat.st_mode)
            or sidecar_stat.st_nlink != 1
            or sidecar_stat.st_uid != os.geteuid()
            or stat.S_IMODE(sidecar_stat.st_mode) != 0o600
        ):
            raise CutoverError(f"{label} probe produced an unsafe SQLite sidecar")
        observed.append((sidecar, sidecar_stat))
    wal_stat = next((item for sidecar, item in observed if sidecar == wal), None)
    if wal_stat is not None and wal_stat.st_size != 0:
        raise CutoverError(f"{label} has a non-empty WAL after the no-writer probe")
    for sidecar, sidecar_stat in observed:
        current = sidecar.lstat()
        if _identity(current) != _identity(sidecar_stat):
            raise CutoverError(f"{label} SQLite sidecar changed during cleanup")
        sidecar.unlink()


def _read_private_json(
    path: Path,
    *,
    label: str = "parity report",
    size_limit: int = _REPORT_SIZE_LIMIT,
) -> tuple[Mapping[str, Any], str]:
    observed = _require_private_regular(path, label=label)
    if observed.st_size > size_limit:
        raise CutoverPreparationError(f"{label} is unexpectedly large")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(observed):
            raise CutoverPreparationError("parity report changed during validation")
        chunks: list[bytes] = []
        remaining = size_limit + 1
        while remaining:
            chunk = os.read(descriptor, min(_COPY_CHUNK_SIZE, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        if len(encoded) > size_limit:
            raise CutoverPreparationError(f"{label} is unexpectedly large")
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CutoverPreparationError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise CutoverPreparationError(f"{label} root must be an object")
    return payload, hashlib.sha256(encoded).hexdigest()


def _hash_private_file(path: Path, *, label: str) -> tuple[str, os.stat_result]:
    observed = _require_private_regular(path, label=label)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(observed):
            raise CutoverError(f"{label} changed before hashing")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, _COPY_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            _identity(after) != _identity(opened)
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or after.st_ctime_ns != opened.st_ctime_ns
        ):
            raise CutoverError(f"{label} changed while hashing")
        return digest.hexdigest(), after
    finally:
        os.close(descriptor)


def _projection_readiness(
    connection: sqlite3.Connection,
    *,
    canonical_sequence: int,
) -> tuple[ProjectionReadiness, ...]:
    placeholders = ",".join("?" for _ in MINIMAL_READ_MODEL_NAMES)
    rows = connection.execute(
        "SELECT projection_name, projection_version, built_through_sequence, state "
        f"FROM projection_generations WHERE projection_name IN ({placeholders}) "
        "ORDER BY projection_name",
        MINIMAL_READ_MODEL_NAMES,
    ).fetchall()
    by_name = {str(row[0]): row for row in rows}
    missing = [name for name in MINIMAL_READ_MODEL_NAMES if name not in by_name]
    if missing:
        raise CutoverPreparationError(
            f"candidate projections were never built: {', '.join(missing)}"
        )
    result: list[ProjectionReadiness] = []
    for name in MINIMAL_READ_MODEL_NAMES:
        row = by_name[name]
        projection = ProjectionReadiness(
            projection_name=name,
            projection_version=int(row[1]),
            built_through_sequence=int(row[2]),
            state=str(row[3]),
        )
        if projection.state != "current":
            raise CutoverPreparationError(f"candidate projection {name} is not current")
        if projection.projection_version != 1:
            raise CutoverPreparationError(
                f"candidate projection {name} has unsupported version {projection.projection_version}"
            )
        if projection.built_through_sequence != canonical_sequence:
            raise CutoverPreparationError(
                f"candidate projection {name} is stale at sequence "
                f"{projection.built_through_sequence}, expected {canonical_sequence}"
            )
        result.append(projection)
    return tuple(result)


def _migration_issue_readiness(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, str, int, int], ...]:
    """Return aggregate issue counts plus independently counted issue rows."""

    rows = connection.execute(
        "SELECT reason, disposition, SUM(count), COUNT(*) FROM migration_issues "
        "GROUP BY reason, disposition ORDER BY reason, disposition"
    ).fetchall()
    return tuple(
        (str(row[0]), str(row[1]), int(row[2]), int(row[3]))
        for row in rows
    )


def _inspect_store(
    store: CanonicalStore,
    *,
    require_current_projections: bool,
    expected_adapter: str,
) -> _StoreReadiness:
    store.connection.execute("PRAGMA busy_timeout = 0")
    with store.transaction():
        check = store.quick_check()
        if not check["ok"]:
            raise CutoverPreparationError(
                "canonical store failed quick_check or foreign_key_check"
            )
        metadata = store.connection.execute(
            "SELECT store_uuid, schema_version, canonical_sequence "
            "FROM store_metadata WHERE singleton = 1"
        ).fetchone()
        if metadata is None:
            raise CutoverPreparationError("canonical store metadata is missing")
        store_uuid = str(metadata[0])
        schema_version = int(metadata[1])
        canonical_sequence = int(metadata[2])
        if schema_version != SCHEMA_VERSION:
            raise CutoverPreparationError("canonical store schema version is not current")
        open_conflicts = int(
            store.connection.execute(
                "SELECT COUNT(*) FROM source_conflicts WHERE resolution_state = 'open'"
            ).fetchone()[0]
        )
        if open_conflicts:
            raise CutoverPreparationError(
                f"canonical store has {open_conflicts} unresolved source conflicts"
            )
        adapter_rows = store.connection.execute(
            "SELECT adapter, COUNT(*) FROM source_instances "
            "WHERE representation = ? GROUP BY adapter ORDER BY adapter",
            (LEGACY_EVENTS_REPRESENTATION,),
        ).fetchall()
        unexpected = [str(row[0]) for row in adapter_rows if str(row[0]) != expected_adapter]
        if unexpected:
            raise CutoverPreparationError(
                "canonical legacy-v1 sources contain unexpected adapters: "
                + ", ".join(unexpected)
            )
        adapter_count = sum(int(row[1]) for row in adapter_rows)
        migration_issues = _migration_issue_readiness(store.connection)
        projections = (
            _projection_readiness(
                store.connection,
                canonical_sequence=canonical_sequence,
            )
            if require_current_projections
            else ()
        )
    return _StoreReadiness(
        store_uuid=store_uuid,
        schema_version=schema_version,
        canonical_sequence=canonical_sequence,
        projections=projections,
        importer_adapter_count=adapter_count,
        migration_issues=migration_issues,
    )


def _inspect_candidate(path: Path) -> _StoreReadiness:
    _reject_sidecars(path, label="candidate")
    _assert_not_busy(path, label="candidate")
    _reject_sidecars(path, label="candidate")
    with CanonicalStore.open(path, read_only=True) as store:
        readiness = _inspect_store(
            store,
            require_current_projections=True,
            expected_adapter=LEGACY_EVENTS_ADAPTER,
        )
    _remove_clean_sidecars_created_by_probe(path, label="candidate")
    _reject_sidecars(path, label="candidate")
    return readiness


def _validate_parity_report(
    payload: Mapping[str, Any],
    *,
    report_path: Path,
    candidate_path: Path,
    candidate_sha256: str,
    candidate_size_bytes: int,
    candidate_migration_issues: tuple[tuple[str, str, int, int], ...],
) -> None:
    runner = payload.get("runner")
    acceptance = payload.get("acceptance")
    migration = payload.get("migration")
    rerun = payload.get("rerun")
    comparisons = payload.get("comparisons")
    issue_summary = payload.get("issue_summary")
    exclusions = payload.get("exclusions")
    snapshot = payload.get("snapshot")
    existing_product = payload.get("existing_product")
    migration_disposition_policy = payload.get("migration_disposition_policy")
    if payload.get("schema_version") != PRODUCT_PARITY_SCHEMA_VERSION:
        raise CutoverPreparationError(
            "parity report does not use the supported product-parity schema"
        )
    if not all(
        isinstance(value, Mapping)
        for value in (
            runner,
            acceptance,
            migration,
            rerun,
            issue_summary,
            snapshot,
            existing_product,
            migration_disposition_policy,
        )
    ) or not isinstance(comparisons, list) or not isinstance(exclusions, list):
        raise CutoverPreparationError(
            "parity report is missing structured product-parity evidence"
        )
    required = (
        payload.get("status") == "passed",
        payload.get("decision") == "go-core-truth-slice",
        payload.get("cutover_decision") == "no-go",
        runner.get("schema_version") == PARITY_RUNNER_SCHEMA_VERSION,
        runner.get("execution_completed") is True,
        runner.get("candidate_retained") is True,
        runner.get("first_import_internal_parity_matches") is True,
        runner.get("second_import_internal_parity_matches") is True,
    )
    if not all(required):
        raise CutoverPreparationError(
            "parity report does not carry the required passed core-truth-slice evidence"
        )
    runner_keys = {
        "schema_version",
        "execution_completed",
        "candidate_retained",
        "candidate_file",
        "report_file",
        "first_import_internal_parity_matches",
        "second_import_internal_parity_matches",
        "candidate_sha256",
        "candidate_size_bytes",
    }
    if set(runner) != runner_keys:
        raise CutoverPreparationError("parity report runner evidence is incompatible")

    expected_acceptance_booleans: dict[str, bool] = {
        "manifest_integrity": True,
        "candidate_integrity": True,
        "exact_supported_truth": True,
        "zero_unresolved_hard_conflicts": True,
        "exclusions_visible": True,
        "migration_policy_applied": True,
        "source_line_conservation": True,
        "rerun_zero_write_and_stable_ids": True,
        "core_truth_slice_passed": True,
        # The parity runner deliberately does not grant the separate product
        # scope or operational cutover approvals.
        "product_scope_complete": False,
        "cutover_gate_passed": False,
    }
    acceptance_count_keys = {
        "approved_exclusion_count",
        "approved_issue_instance_count",
        "unapproved_exclusion_count",
        "unapproved_issue_instance_count",
        "affected_issue_line_count",
    }
    if set(acceptance) != (
        set(expected_acceptance_booleans) | acceptance_count_keys
    ) or any(
        acceptance.get(key) is not expected
        for key, expected in expected_acceptance_booleans.items()
    ) or any(
        type(acceptance.get(key)) is not int or acceptance.get(key) < 0
        for key in acceptance_count_keys
    ):
        raise CutoverPreparationError(
            "parity report acceptance evidence is incomplete or non-passing"
        )

    comparison_keys = frozenset(
        {
            "surface",
            "required_core",
            "matches",
            "source_count",
            "candidate_count",
            "mismatch_count",
        }
    )
    observed_surfaces: set[str] = set()
    if len(comparisons) != len(_CORE_PARITY_SURFACES):
        raise CutoverPreparationError(
            "parity report must contain exactly the four core comparisons"
        )
    for comparison in comparisons:
        if not isinstance(comparison, Mapping) or set(comparison) != comparison_keys:
            raise CutoverPreparationError(
                "parity report contains an incompatible core comparison"
            )
        surface = comparison.get("surface")
        source_count = comparison.get("source_count")
        candidate_count = comparison.get("candidate_count")
        mismatch_count = comparison.get("mismatch_count")
        if (
            not isinstance(surface, str)
            or surface not in _CORE_PARITY_SURFACES
            or surface in observed_surfaces
            or comparison.get("required_core") is not True
            or comparison.get("matches") is not True
            or type(source_count) is not int
            or source_count < 0
            or type(candidate_count) is not int
            or candidate_count < 0
            or source_count != candidate_count
            or type(mismatch_count) is not int
            or mismatch_count != 0
        ):
            raise CutoverPreparationError(
                "parity report contains a missing, duplicate, or non-passing core comparison"
            )
        observed_surfaces.add(surface)
    if observed_surfaces != _CORE_PARITY_SURFACES:
        raise CutoverPreparationError(
            "parity report does not cover every required core comparison"
        )

    migration_count_fields = (
        "lines_seen",
        "parsed_events",
        "malformed_or_excluded_lines",
        "issue_lines",
        "excluded_lines",
        "processed_with_issues_lines",
        "migration_issue_count",
    )
    migration_keys = {
        "lines_seen",
        "parsed_events",
        "malformed_or_excluded_lines",
        "issue_lines",
        "excluded_lines",
        "processed_with_issues_lines",
        "migration_issue_count",
        "write_dispositions",
        "internal_parity_matches",
        "internal_parity_difference_keys",
    }
    migration_dispositions = migration.get("write_dispositions")
    if (
        set(migration) != migration_keys
        or any(
            type(migration.get(key)) is not int or migration.get(key) < 0
            for key in migration_count_fields
        )
        or not isinstance(migration_dispositions, Mapping)
        or any(
            not isinstance(counts, Mapping)
            or not set(counts).issubset({"inserted", "noop"})
            or any(type(count) is not int or count < 0 for count in counts.values())
            for counts in migration_dispositions.values()
        )
        or migration.get("internal_parity_matches") is not True
        or migration.get("internal_parity_difference_keys") != []
    ):
        raise CutoverPreparationError(
            "parity report migration evidence contains differences or issues"
        )

    issue_summary_keys = {
        "issue_instances",
        "affected_lines",
        "excluded_lines",
        "processed_with_issues_lines",
    }
    if (
        set(issue_summary) != issue_summary_keys
        or any(
            type(issue_summary.get(key)) is not int or issue_summary.get(key) < 0
            for key in issue_summary_keys
        )
        or any(not isinstance(row, Mapping) for row in exclusions)
    ):
        raise CutoverPreparationError(
            "parity report issue evidence is malformed"
        )

    snapshot_keys = {
        "kind",
        "manifest_sha256",
        "file_count",
        "bytes",
        "verified_before",
        "verified_after",
    }
    snapshot_manifest_sha256 = snapshot.get("manifest_sha256")
    if (
        set(snapshot) != snapshot_keys
        or snapshot.get("kind") not in {"legacy", "legacy-chronicle"}
        or not isinstance(snapshot_manifest_sha256, str)
        or len(snapshot_manifest_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in snapshot_manifest_sha256
        )
        or type(snapshot.get("file_count")) is not int
        or snapshot.get("file_count") <= 0
        or type(snapshot.get("bytes")) is not int
        or snapshot.get("bytes") < 0
        or snapshot.get("verified_before") is not True
        or snapshot.get("verified_after") is not True
    ):
        raise CutoverPreparationError(
            "parity report snapshot manifest binding is malformed"
        )
    existing_product_keys = {
        "work_ledger_schema_version",
        "task_projection_schema_version",
        "store_scope",
        "lines_seen",
        "parsed_object_events",
        "malformed_or_non_object",
    }
    if (
        set(existing_product) != existing_product_keys
        or existing_product.get("store_scope") not in {"project", "custom"}
        or any(
            type(existing_product.get(key)) is not int
            or existing_product.get(key) < 0
            for key in (
                "lines_seen",
                "parsed_object_events",
                "malformed_or_non_object",
            )
        )
        or existing_product["lines_seen"]
        != existing_product["parsed_object_events"]
        + existing_product["malformed_or_non_object"]
        or existing_product["malformed_or_non_object"] != 0
    ):
        raise CutoverPreparationError(
            "parity report independent source-line evidence is malformed"
        )
    try:
        expected_policy = build_migration_disposition_policy_evidence(
            snapshot_manifest_sha256=snapshot_manifest_sha256,
            issues=exclusions,
            source_lines_seen=existing_product["lines_seen"],
            importer_lines_seen=migration["lines_seen"],
            parsed_events=migration["parsed_events"],
            malformed_or_excluded_lines=migration["malformed_or_excluded_lines"],
            issue_lines=migration["issue_lines"],
            excluded_lines=migration["excluded_lines"],
            processed_with_issues_lines=migration["processed_with_issues_lines"],
            migration_issue_count=migration["migration_issue_count"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CutoverPreparationError(
            "parity report migration disposition policy is malformed"
        ) from exc
    if dict(migration_disposition_policy) != expected_policy:
        raise CutoverPreparationError(
            "parity report migration disposition policy is incompatible"
        )
    approved_exclusion_count = int(expected_policy["approved_exclusion_count"])
    candidate_exclusions = [
        {"reason": reason, "disposition": disposition, "count": count}
        for reason, disposition, count, _row_count in candidate_migration_issues
    ]
    if (
        expected_policy["unapproved_exclusion_count"] != 0
        or expected_policy["unapproved_exclusions"]
        or expected_policy["source_line_conservation"] is not True
        or expected_policy["approved_exclusion_conservation"] is not True
        or candidate_exclusions != exclusions
        or any(
            count != row_count
            for _reason, _disposition, count, row_count in candidate_migration_issues
        )
        or sum(row[3] for row in candidate_migration_issues)
        != migration["issue_lines"]
        or issue_summary["issue_instances"] != migration["migration_issue_count"]
        or issue_summary["affected_lines"] != migration["issue_lines"]
        or issue_summary["excluded_lines"] != migration["excluded_lines"]
        or issue_summary["processed_with_issues_lines"]
        != migration["processed_with_issues_lines"]
        or acceptance["approved_exclusion_count"] != approved_exclusion_count
        or acceptance["approved_issue_instance_count"] != approved_exclusion_count
        or acceptance["unapproved_exclusion_count"] != 0
        or acceptance["unapproved_issue_instance_count"] != 0
        or acceptance["affected_issue_line_count"] != migration["issue_lines"]
    ):
        raise CutoverPreparationError(
            "parity report contains unapproved or unconserved migration exclusions"
        )

    second_import = rerun.get("second_import")
    if not isinstance(second_import, Mapping):
        raise CutoverPreparationError("parity report is missing second-import evidence")
    rerun_keys = {
        "canonical_writes",
        "canonical_sequence_delta",
        "task_ids_stable",
        "opaque_task_ids_valid",
        "task_id_count",
        "table_counts_stable",
        "projection_rebuilt",
        "second_import",
    }
    second_import_keys = {
        "write_dispositions",
        "internal_parity_matches",
        "internal_parity_difference_keys",
    }
    dispositions = second_import.get("write_dispositions")
    noop_only = bool(
        isinstance(dispositions, Mapping)
        and all(
            isinstance(counts, Mapping)
            and set(counts).issubset({"noop"})
            and all(type(count) is int and count >= 0 for count in counts.values())
            for counts in dispositions.values()
        )
    )
    rerun_passed = (
        set(rerun) == rerun_keys
        and set(second_import) == second_import_keys
        and type(rerun.get("canonical_writes")) is int
        and rerun.get("canonical_writes") == 0
        and type(rerun.get("canonical_sequence_delta")) is int
        and rerun.get("canonical_sequence_delta") == 0
        and rerun.get("task_ids_stable") is True
        and rerun.get("opaque_task_ids_valid") is True
        and type(rerun.get("task_id_count")) is int
        and rerun.get("task_id_count") >= 0
        and rerun.get("table_counts_stable") is True
        and rerun.get("projection_rebuilt") is False
        and second_import.get("internal_parity_matches") is True
        and second_import.get("internal_parity_difference_keys") == []
        and noop_only
    )
    if not rerun_passed:
        raise CutoverPreparationError(
            "parity report rerun evidence is not zero-write and stable"
        )

    candidate_file = runner.get("candidate_file")
    if (
        not isinstance(candidate_file, str)
        or Path(candidate_file).name != candidate_file
        or candidate_file != candidate_path.name
    ):
        raise CutoverPreparationError("parity report candidate_file does not match candidate basename")
    report_file = runner.get("report_file")
    if not isinstance(report_file, str) or report_file != report_path.name:
        raise CutoverPreparationError("parity report report_file does not match report basename")
    reported_digest = runner.get("candidate_sha256")
    if not isinstance(reported_digest, str) or reported_digest != candidate_sha256:
        raise CutoverPreparationError("parity report candidate_sha256 does not match candidate bytes")
    reported_size = runner.get("candidate_size_bytes")
    if type(reported_size) is not int or reported_size != candidate_size_bytes:
        raise CutoverPreparationError("parity report candidate_size_bytes does not match candidate")


def prepare_cutover(
    candidate_path: Path | str,
    parity_report_path: Path | str,
) -> CutoverPreparationReceipt:
    """Bind one sealed candidate to one successful parity-runner v3 report."""

    candidate = Path(candidate_path).expanduser()
    report = Path(parity_report_path).expanduser()
    _assert_absolute(candidate, label="candidate")
    _assert_absolute(report, label="parity report")
    # Inspect the caller's lexical names before resolving.  Resolving first
    # would erase the evidence that a leaf or parent was a symlink.
    _assert_no_symlink_components(candidate, label="candidate")
    _assert_no_symlink_components(report, label="parity report")
    candidate = candidate.resolve(strict=True)
    report = report.resolve(strict=True)
    if candidate.parent != report.parent:
        raise CutoverPreparationError(
            "candidate and parity report must be retained in the same private run directory"
        )
    _require_private_run_directory(candidate.parent)
    candidate_sha256, candidate_stat = _hash_private_file(candidate, label="candidate")
    payload, report_sha256 = _read_private_json(report)
    readiness = _inspect_candidate(candidate)
    _validate_parity_report(
        payload,
        report_path=report,
        candidate_path=candidate,
        candidate_sha256=candidate_sha256,
        candidate_size_bytes=int(candidate_stat.st_size),
        candidate_migration_issues=readiness.migration_issues,
    )
    # Re-hash after every SQLite check so a report cannot bless an inode that
    # changed while readiness was being established.
    final_digest, final_stat = _hash_private_file(candidate, label="candidate")
    if final_digest != candidate_sha256 or _identity(final_stat) != _identity(candidate_stat):
        raise CutoverPreparationError("candidate changed while preparing cutover")
    return CutoverPreparationReceipt(
        receipt_version=CUTOVER_RECEIPT_VERSION,
        prepared_at_us=_now_us(),
        candidate_path=candidate,
        parity_report_path=report,
        candidate_sha256=candidate_sha256,
        parity_report_sha256=report_sha256,
        candidate_size_bytes=int(final_stat.st_size),
        candidate_device=int(final_stat.st_dev),
        candidate_inode=int(final_stat.st_ino),
        candidate_store_uuid=readiness.store_uuid,
        schema_version=readiness.schema_version,
        canonical_sequence=readiness.canonical_sequence,
        projections=readiness.projections,
        adapter_rows_to_normalize=readiness.importer_adapter_count,
    )


def _revalidate_preparation(receipt: CutoverPreparationReceipt) -> _StoreReadiness:
    parsed = _parse_preparation_receipt(
        receipt.to_dict(),
        require_artifacts=True,
    )
    if parsed != receipt:
        raise CutoverPromotionError("cutover preparation receipt is not canonical")
    if receipt.receipt_version != CUTOVER_RECEIPT_VERSION:
        raise CutoverPromotionError("unsupported cutover preparation receipt version")
    payload, report_digest = _read_private_json(receipt.parity_report_path)
    candidate_digest, observed = _hash_private_file(
        receipt.candidate_path,
        label="candidate",
    )
    if report_digest != receipt.parity_report_sha256:
        raise CutoverPromotionError("parity report changed after cutover preparation")
    if candidate_digest != receipt.candidate_sha256:
        raise CutoverPromotionError("candidate changed after cutover preparation")
    if _identity(observed) != (receipt.candidate_device, receipt.candidate_inode):
        raise CutoverPromotionError("candidate inode changed after cutover preparation")
    readiness = _inspect_candidate(receipt.candidate_path)
    _validate_parity_report(
        payload,
        report_path=receipt.parity_report_path,
        candidate_path=receipt.candidate_path,
        candidate_sha256=candidate_digest,
        candidate_size_bytes=int(observed.st_size),
        candidate_migration_issues=readiness.migration_issues,
    )
    if (
        readiness.store_uuid != receipt.candidate_store_uuid
        or readiness.schema_version != receipt.schema_version
        or readiness.canonical_sequence != receipt.canonical_sequence
        or readiness.projections != receipt.projections
        or readiness.importer_adapter_count != receipt.adapter_rows_to_normalize
    ):
        raise CutoverPromotionError("candidate readiness changed after preparation")
    return readiness


def _expect_object_keys(
    value: object,
    *,
    expected: frozenset[str],
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CutoverPromotionError(f"{label} must be an object")
    keys = frozenset(str(key) for key in value)
    if keys != expected or any(not isinstance(key, str) for key in value):
        missing = sorted(expected - keys)
        unknown = sorted(keys - expected)
        raise CutoverPromotionError(
            f"{label} fields are incompatible; missing={missing}, unknown={unknown}"
        )
    return value


def _receipt_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CutoverPromotionError(f"{label} must be a non-empty string")
    return value


def _receipt_int(value: object, *, label: str, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if type(value) is not int or value < minimum:
        qualifier = "positive" if positive else "non-negative"
        raise CutoverPromotionError(f"{label} must be a {qualifier} integer")
    return value


def _receipt_hash(value: object, *, label: str) -> str:
    encoded = _receipt_string(value, label=label)
    if len(encoded) != 64 or any(character not in "0123456789abcdef" for character in encoded):
        raise CutoverPromotionError(f"{label} must be a lowercase SHA-256 digest")
    return encoded


def _receipt_existing_path(value: object, *, label: str) -> Path:
    encoded = _receipt_string(value, label=label)
    path = Path(encoded)
    if not path.is_absolute() or str(path) != encoded:
        raise CutoverPromotionError(f"{label} must be a normalized absolute path")
    _assert_no_symlink_components(path, label=label)
    if path.resolve(strict=True) != path:
        raise CutoverPromotionError(f"{label} must be a resolved non-symlink path")
    return path


def _receipt_archival_path(value: object, *, label: str) -> Path:
    """Validate a recorded absolute path without requiring its artifact."""

    encoded = _receipt_string(value, label=label)
    path = Path(encoded)
    if not path.is_absolute() or str(path) != encoded or ".." in path.parts:
        raise CutoverPromotionError(f"{label} must be a normalized absolute path")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            observed = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(observed.st_mode):
            raise CutoverPromotionError(f"{label} may not contain symlinks")
    return path


def _parse_preparation_receipt(
    value: object,
    *,
    require_artifacts: bool,
) -> CutoverPreparationReceipt:
    expected = frozenset(
        {
            "receipt_version",
            "prepared_at_us",
            "candidate_path",
            "parity_report_path",
            "candidate_sha256",
            "parity_report_sha256",
            "candidate_size_bytes",
            "candidate_device",
            "candidate_inode",
            "candidate_store_uuid",
            "schema_version",
            "canonical_sequence",
            "projections",
            "adapter_rows_to_normalize",
        }
    )
    payload = _expect_object_keys(value, expected=expected, label="preparation receipt")
    version = _receipt_string(payload["receipt_version"], label="preparation receipt version")
    if version != CUTOVER_RECEIPT_VERSION:
        raise CutoverPromotionError("unsupported preparation receipt version")
    path_parser = _receipt_existing_path if require_artifacts else _receipt_archival_path
    candidate_path = path_parser(payload["candidate_path"], label="candidate path")
    report_path = path_parser(payload["parity_report_path"], label="parity report path")
    if candidate_path.parent != report_path.parent:
        raise CutoverPromotionError("receipt candidate and parity report are not siblings")
    if require_artifacts:
        _require_private_run_directory(candidate_path.parent)
    raw_projections = payload["projections"]
    if not isinstance(raw_projections, list):
        raise CutoverPromotionError("preparation projections must be an array")
    projection_keys = frozenset(
        {
            "projection_name",
            "projection_version",
            "built_through_sequence",
            "state",
        }
    )
    projections: list[ProjectionReadiness] = []
    for index, raw in enumerate(raw_projections):
        projection = _expect_object_keys(
            raw,
            expected=projection_keys,
            label=f"preparation projection {index}",
        )
        projections.append(
            ProjectionReadiness(
                projection_name=_receipt_string(
                    projection["projection_name"],
                    label="projection name",
                ),
                projection_version=_receipt_int(
                    projection["projection_version"],
                    label="projection version",
                    positive=True,
                ),
                built_through_sequence=_receipt_int(
                    projection["built_through_sequence"],
                    label="projection built-through sequence",
                ),
                state=_receipt_string(projection["state"], label="projection state"),
            )
        )
    expected_projection_names = tuple(sorted(MINIMAL_READ_MODEL_NAMES))
    if tuple(item.projection_name for item in projections) != expected_projection_names:
        raise CutoverPromotionError("preparation receipt projection set is incompatible")
    receipt = CutoverPreparationReceipt(
        receipt_version=version,
        prepared_at_us=_receipt_int(payload["prepared_at_us"], label="prepared_at_us"),
        candidate_path=candidate_path,
        parity_report_path=report_path,
        candidate_sha256=_receipt_hash(payload["candidate_sha256"], label="candidate_sha256"),
        parity_report_sha256=_receipt_hash(
            payload["parity_report_sha256"],
            label="parity_report_sha256",
        ),
        candidate_size_bytes=_receipt_int(
            payload["candidate_size_bytes"],
            label="candidate_size_bytes",
            positive=True,
        ),
        candidate_device=_receipt_int(
            payload["candidate_device"],
            label="candidate_device",
            positive=True,
        ),
        candidate_inode=_receipt_int(
            payload["candidate_inode"],
            label="candidate_inode",
            positive=True,
        ),
        candidate_store_uuid=_receipt_string(
            payload["candidate_store_uuid"],
            label="candidate_store_uuid",
        ),
        schema_version=_receipt_int(
            payload["schema_version"],
            label="schema_version",
            positive=True,
        ),
        canonical_sequence=_receipt_int(
            payload["canonical_sequence"],
            label="canonical_sequence",
        ),
        projections=tuple(projections),
        adapter_rows_to_normalize=_receipt_int(
            payload["adapter_rows_to_normalize"],
            label="adapter_rows_to_normalize",
        ),
    )
    if receipt.schema_version != SCHEMA_VERSION:
        raise CutoverPromotionError("preparation receipt schema version is incompatible")
    if any(
        item.state != "current"
        or item.projection_version != 1
        or item.built_through_sequence != receipt.canonical_sequence
        for item in receipt.projections
    ):
        raise CutoverPromotionError("preparation receipt projection readiness is incompatible")
    return receipt


def _parse_promotion_receipt(
    payload: Mapping[str, Any],
    *,
    require_preparation_artifacts: bool,
) -> PromotionReceipt:
    expected = frozenset(
        {
            "receipt_version",
            "operation_id",
            "promoted_at_us",
            "preparation",
            "live_path",
            "backup_path",
            "previous_live_store_uuid",
            "previous_live_sha256",
            "previous_live_size_bytes",
            "promoted_store_uuid",
            "promoted_sha256",
            "promoted_size_bytes",
            "promoted_device",
            "promoted_inode",
            "normalized_source_count",
        }
    )
    data = _expect_object_keys(payload, expected=expected, label="promotion receipt")
    version = _receipt_string(data["receipt_version"], label="promotion receipt version")
    if version != CUTOVER_RECEIPT_VERSION:
        raise CutoverPromotionError("unsupported promotion receipt version")
    operation_id = _receipt_string(data["operation_id"], label="operation_id")
    if len(operation_id) != 32 or any(character not in "0123456789abcdef" for character in operation_id):
        raise CutoverPromotionError("operation_id must be 32 lowercase hexadecimal characters")
    preparation = _parse_preparation_receipt(
        data["preparation"],
        require_artifacts=require_preparation_artifacts,
    )
    live_path = _receipt_existing_path(data["live_path"], label="live path")
    backup_path = _receipt_existing_path(data["backup_path"], label="backup path")
    if live_path.name != LIVE_STORE_FILENAME or live_path.parent != backup_path.parent:
        raise CutoverPromotionError("receipt live and backup paths are incompatible")
    if backup_path.name != f"{_BACKUP_PREFIX}{operation_id}{_BACKUP_SUFFIX}":
        raise CutoverPromotionError("receipt backup path is not bound to operation_id")
    receipt = PromotionReceipt(
        receipt_version=version,
        operation_id=operation_id,
        promoted_at_us=_receipt_int(data["promoted_at_us"], label="promoted_at_us"),
        preparation=preparation,
        live_path=live_path,
        backup_path=backup_path,
        previous_live_store_uuid=_receipt_string(
            data["previous_live_store_uuid"],
            label="previous_live_store_uuid",
        ),
        previous_live_sha256=_receipt_hash(
            data["previous_live_sha256"],
            label="previous_live_sha256",
        ),
        previous_live_size_bytes=_receipt_int(
            data["previous_live_size_bytes"],
            label="previous_live_size_bytes",
            positive=True,
        ),
        promoted_store_uuid=_receipt_string(
            data["promoted_store_uuid"],
            label="promoted_store_uuid",
        ),
        promoted_sha256=_receipt_hash(data["promoted_sha256"], label="promoted_sha256"),
        promoted_size_bytes=_receipt_int(
            data["promoted_size_bytes"],
            label="promoted_size_bytes",
            positive=True,
        ),
        promoted_device=_receipt_int(
            data["promoted_device"],
            label="promoted_device",
            positive=True,
        ),
        promoted_inode=_receipt_int(
            data["promoted_inode"],
            label="promoted_inode",
            positive=True,
        ),
        normalized_source_count=_receipt_int(
            data["normalized_source_count"],
            label="normalized_source_count",
        ),
    )
    if receipt.promoted_store_uuid != preparation.candidate_store_uuid:
        raise CutoverPromotionError("promoted store UUID is not the prepared candidate UUID")
    if receipt.normalized_source_count != preparation.adapter_rows_to_normalize:
        raise CutoverPromotionError("normalized source count is not bound to preparation")
    return receipt


def _validate_receipt_output_path(path: Path | str) -> Path:
    target = Path(path).expanduser()
    if not target.is_absolute():
        raise CutoverPromotionError("promotion receipt path must be absolute")
    if (
        target.name == LIVE_STORE_FILENAME
        or target.name.startswith(_BACKUP_PREFIX)
        or target.name.startswith(_PROMOTE_STAGE_PREFIX)
        or target.name.startswith(_ROLLBACK_STAGE_PREFIX)
        or target.name.endswith(("-wal", "-shm", "-journal"))
    ):
        raise CutoverPromotionError("promotion receipt path uses a reserved cutover name")
    _assert_no_symlink_components(target.parent, label="promotion receipt parent")
    resolved_parent, descriptor = _open_store_root(target.parent)
    os.close(descriptor)
    target = resolved_parent / target.name
    try:
        target.lstat()
    except FileNotFoundError:
        return target
    raise FileExistsError(f"promotion receipt already exists: {target}")


def write_promotion_receipt(
    path: Path | str,
    receipt: PromotionReceipt,
) -> Path:
    """Persist a receipt once, privately and durably; never overwrite."""

    target = _validate_receipt_output_path(path)
    # Parsing our own shape before writing catches manually-constructed typed
    # objects that violate cross-field invariants.
    body = receipt.to_dict()
    _parse_promotion_receipt(body, require_preparation_artifacts=True)
    canonical_body = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    document = {
        "document_version": CUTOVER_RECEIPT_VERSION,
        "receipt": body,
        "receipt_sha256": hashlib.sha256(canonical_body).hexdigest(),
    }
    encoded = (
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(encoded) > _RECEIPT_SIZE_LIMIT:
        raise CutoverPromotionError("promotion receipt is unexpectedly large")
    root, directory_descriptor = _open_store_root(target.parent)
    if root != target.parent:
        os.close(directory_descriptor)
        raise CutoverPromotionError("promotion receipt parent changed during validation")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    created_identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(target.name, flags, 0o600, dir_fd=directory_descriptor)
        os.fchmod(descriptor, 0o600)
        created_identity = _identity(os.fstat(descriptor))
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while persisting promotion receipt")
            view = view[written:]
        os.fsync(descriptor)
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or observed.st_uid != os.geteuid()
            or stat.S_IMODE(observed.st_mode) != 0o600
            or observed.st_size != len(encoded)
        ):
            raise CutoverPromotionError("persisted promotion receipt is not private and complete")
        _fsync_directory(directory_descriptor)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        _remove_created_name(
            target.name,
            directory_descriptor=directory_descriptor,
            expected_identity=created_identity,
        )
        try:
            _fsync_directory(directory_descriptor)
        except OSError:
            pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_descriptor)
    return target


def load_promotion_receipt(path: Path | str) -> PromotionReceipt:
    """Load a complete, private receipt with strict schema and path checks."""

    target = Path(path).expanduser()
    if not target.is_absolute():
        raise CutoverPromotionError("promotion receipt path must be absolute")
    _assert_no_symlink_components(target, label="promotion receipt")
    target = target.resolve(strict=True)
    resolved_parent, directory_descriptor = _open_store_root(target.parent)
    os.close(directory_descriptor)
    if resolved_parent != target.parent:
        raise CutoverPromotionError("promotion receipt parent changed during validation")
    payload, _digest = _read_private_json(
        target,
        label="promotion receipt",
        size_limit=_RECEIPT_SIZE_LIMIT,
    )
    document = _expect_object_keys(
        payload,
        expected=frozenset({"document_version", "receipt", "receipt_sha256"}),
        label="promotion receipt document",
    )
    if document["document_version"] != CUTOVER_RECEIPT_VERSION:
        raise CutoverPromotionError("unsupported promotion receipt document version")
    body = document["receipt"]
    if not isinstance(body, Mapping):
        raise CutoverPromotionError("promotion receipt body must be an object")
    canonical_body = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected_digest = _receipt_hash(
        document["receipt_sha256"],
        label="receipt_sha256",
    )
    if hashlib.sha256(canonical_body).hexdigest() != expected_digest:
        raise CutoverPromotionError("promotion receipt document digest does not match its body")
    return _parse_promotion_receipt(body, require_preparation_artifacts=False)


def _load_matching_emergency_receipt(
    path: Path,
    receipt: PromotionReceipt,
) -> Path | None:
    try:
        loaded = load_promotion_receipt(path)
    except Exception:
        return None
    return path if loaded == receipt else None


def _copy_fd(source: int, destination: int) -> tuple[str, int]:
    os.lseek(source, 0, os.SEEK_SET)
    os.lseek(destination, 0, os.SEEK_SET)
    os.ftruncate(destination, 0)
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(source, _COPY_CHUNK_SIZE)
        if not chunk:
            break
        digest.update(chunk)
        view = memoryview(chunk)
        while view:
            written = os.write(destination, view)
            if written <= 0:
                raise OSError("short write while staging canonical database")
            view = view[written:]
        size += len(chunk)
    os.fsync(destination)
    return digest.hexdigest(), size


def _copy_path_to_new_name(
    source_path: Path,
    *,
    directory_descriptor: int,
    destination_name: str,
    label: str,
) -> tuple[str, int, tuple[int, int]]:
    source_stat = _require_private_regular(source_path, label=label)
    source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    destination_flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    source = os.open(source_path, source_flags)
    try:
        opened_source = os.fstat(source)
        if _identity(opened_source) != _identity(source_stat):
            raise CutoverPromotionError(f"{label} changed before staging copy")
        destination = os.open(
            destination_name,
            destination_flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        try:
            os.fchmod(destination, 0o600)
            digest, size = _copy_fd(source, destination)
            staged = os.fstat(destination)
            if (
                not stat.S_ISREG(staged.st_mode)
                or staged.st_nlink != 1
                or staged.st_uid != os.geteuid()
                or stat.S_IMODE(staged.st_mode) != 0o600
            ):
                raise CutoverPromotionError("staged database is not a private unique file")
            final_source = os.fstat(source)
            if (
                _identity(final_source) != _identity(opened_source)
                or final_source.st_size != opened_source.st_size
                or final_source.st_mtime_ns != opened_source.st_mtime_ns
                or final_source.st_ctime_ns != opened_source.st_ctime_ns
            ):
                raise CutoverPromotionError(f"{label} changed while it was copied")
            return digest, size, _identity(staged)
        finally:
            os.close(destination)
    finally:
        os.close(source)


def _copy_live_to_new_name(
    root: Path,
    *,
    directory_descriptor: int,
    destination_name: str,
) -> tuple[str, int, tuple[int, int]]:
    return _copy_path_to_new_name(
        root / LIVE_STORE_FILENAME,
        directory_descriptor=directory_descriptor,
        destination_name=destination_name,
        label="existing live shadow",
    )


def _fsync_directory(descriptor: int) -> None:
    os.fsync(descriptor)


def _remove_created_name(
    name: str,
    *,
    directory_descriptor: int,
    expected_identity: tuple[int, int] | None,
) -> None:
    if expected_identity is None:
        return
    try:
        observed = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    if _identity(observed) == expected_identity and stat.S_ISREG(observed.st_mode):
        os.unlink(name, dir_fd=directory_descriptor)


def _remove_stage_sidecars(stage_path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        path = Path(f"{stage_path}{suffix}")
        try:
            observed = path.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or observed.st_uid != os.geteuid()
        ):
            raise CutoverPromotionError("staging SQLite sidecar is unsafe")
        if suffix == "-wal" and observed.st_size != 0:
            raise CutoverPromotionError("staging WAL was not fully checkpointed")
        path.unlink()


def _raw_store_metadata(path: Path, *, expected_role: str) -> tuple[str, int, int]:
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro&immutable=1",
        uri=True,
        timeout=0,
        isolation_level=None,
    )
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 0")
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        metadata = connection.execute(
            "SELECT store_uuid, store_role, schema_version, canonical_sequence "
            "FROM store_metadata WHERE singleton = 1"
        ).fetchone()
        quick = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
        foreign = list(connection.execute("PRAGMA foreign_key_check"))
        if application_id != APPLICATION_ID or user_version != SCHEMA_VERSION:
            raise CutoverPromotionError("staged canonical store has incompatible schema identity")
        if (
            metadata is None
            or str(metadata[1]) != expected_role
            or int(metadata[2]) != SCHEMA_VERSION
        ):
            raise CutoverPromotionError("staged canonical store has incompatible role metadata")
        if quick != ["ok"] or foreign:
            raise CutoverPromotionError("staged canonical store failed integrity checks")
        return str(metadata[0]), int(metadata[2]), int(metadata[3])
    except sqlite3.DatabaseError as exc:
        raise CutoverPromotionError("staged canonical store is unreadable") from exc
    finally:
        connection.close()


def _prepare_promoted_stage(
    stage_path: Path,
    *,
    expected_store_uuid: str,
    expected_sequence: int,
    expected_adapter_count: int,
) -> int:
    connection = sqlite3.connect(
        f"{stage_path.as_uri()}?mode=rw",
        uri=True,
        timeout=0,
        isolation_level=None,
    )
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 0")
        connection.execute("PRAGMA synchronous = FULL")
        metadata = connection.execute(
            "SELECT store_uuid, store_role, schema_version, canonical_sequence "
            "FROM store_metadata WHERE singleton = 1"
        ).fetchone()
        if (
            metadata is None
            or str(metadata[0]) != expected_store_uuid
            or str(metadata[1]) != "candidate"
            or int(metadata[2]) != SCHEMA_VERSION
            or int(metadata[3]) != expected_sequence
        ):
            raise CutoverPromotionError("staged candidate metadata changed before promotion")
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "UPDATE source_instances SET adapter = ? "
            "WHERE representation = ? AND adapter = ?",
            (
                LIVE_EVENTS_ADAPTER,
                LEGACY_EVENTS_REPRESENTATION,
                LEGACY_EVENTS_ADAPTER,
            ),
        )
        normalized = int(cursor.rowcount)
        if normalized != expected_adapter_count:
            raise CutoverPromotionError("staged adapter normalization count changed")
        connection.execute(
            "UPDATE store_metadata SET store_role = 'live' WHERE singleton = 1"
        )
        connection.execute("COMMIT")
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or int(checkpoint[0]) != 0 or int(checkpoint[1]) != 0:
            raise CutoverPromotionError("staging WAL could not be checkpointed completely")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    _remove_stage_sidecars(stage_path)
    store_uuid, schema_version, sequence = _raw_store_metadata(
        stage_path,
        expected_role="live",
    )
    if (
        store_uuid != expected_store_uuid
        or schema_version != SCHEMA_VERSION
        or sequence != expected_sequence
    ):
        raise CutoverPromotionError("promoted staging metadata failed verification")
    connection = sqlite3.connect(
        f"{stage_path.as_uri()}?mode=ro&immutable=1",
        uri=True,
        timeout=0,
        isolation_level=None,
    )
    try:
        remaining = int(
            connection.execute(
                "SELECT COUNT(*) FROM source_instances "
                "WHERE representation = ? AND adapter != ?",
                (LEGACY_EVENTS_REPRESENTATION, LIVE_EVENTS_ADAPTER),
            ).fetchone()[0]
        )
        if remaining:
            raise CutoverPromotionError("staged candidate has unnormalized legacy-v1 adapters")
    finally:
        connection.close()
    _reject_sidecars(stage_path, label="promoted staging database")
    descriptor = os.open(
        stage_path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return normalized


def _cutover_artifacts(directory_descriptor: int) -> tuple[list[str], list[str]]:
    names = os.listdir(directory_descriptor)
    backups = [
        name
        for name in names
        if name.startswith(_BACKUP_PREFIX) and name.endswith(_BACKUP_SUFFIX)
    ]
    stages = [
        name
        for name in names
        if (
            name.startswith(_PROMOTE_STAGE_PREFIX)
            or name.startswith(_ROLLBACK_STAGE_PREFIX)
        )
        and name.endswith(_STAGE_SUFFIX)
    ]
    return sorted(backups), sorted(stages)


def _inspect_live(root: Path, *, require_current_projections: bool) -> _StoreReadiness:
    live_path = root / LIVE_STORE_FILENAME
    _seal_stopped_live_store(live_path)
    _reject_sidecars(live_path, label="live store")
    with CanonicalStore.open_live(root, read_only=True) as store:
        readiness = _inspect_store(
            store,
            require_current_projections=require_current_projections,
            expected_adapter=LIVE_EVENTS_ADAPTER,
        )
    _remove_clean_sidecars_created_by_probe(live_path, label="live store")
    _reject_sidecars(live_path, label="live store")
    return readiness


def _inspect_live_immutable(path: Path) -> _StoreReadiness:
    """Inspect one sidecar-free live main file without touching SQLite state."""

    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro&immutable=1",
        uri=True,
        timeout=0,
        isolation_level=None,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 0")
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if application_id != APPLICATION_ID or user_version != SCHEMA_VERSION:
            raise CutoverPromotionError(
                "promoted live store has incompatible schema identity"
            )
        quick = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
        foreign = list(connection.execute("PRAGMA foreign_key_check"))
        if quick != ["ok"] or foreign:
            raise CutoverPromotionError(
                "promoted live store failed quick_check or foreign_key_check"
            )
        metadata = connection.execute(
            "SELECT store_uuid, store_role, schema_version, canonical_sequence "
            "FROM store_metadata WHERE singleton = 1"
        ).fetchone()
        if (
            metadata is None
            or str(metadata["store_role"]) != "live"
            or int(metadata["schema_version"]) != SCHEMA_VERSION
        ):
            raise CutoverPromotionError(
                "promoted live store has incompatible role metadata"
            )
        open_conflicts = int(
            connection.execute(
                "SELECT COUNT(*) FROM source_conflicts WHERE resolution_state = 'open'"
            ).fetchone()[0]
        )
        if open_conflicts:
            raise CutoverPromotionError(
                f"promoted live store has {open_conflicts} unresolved source conflicts"
            )
        adapter_rows = connection.execute(
            "SELECT adapter, COUNT(*) FROM source_instances "
            "WHERE representation = ? GROUP BY adapter ORDER BY adapter",
            (LEGACY_EVENTS_REPRESENTATION,),
        ).fetchall()
        unexpected = [
            str(row[0])
            for row in adapter_rows
            if str(row[0]) != LIVE_EVENTS_ADAPTER
        ]
        if unexpected:
            raise CutoverPromotionError(
                "promoted live legacy-v1 sources contain unexpected adapters: "
                + ", ".join(unexpected)
            )
        sequence = int(metadata["canonical_sequence"])
        projections = _projection_readiness(
            connection,
            canonical_sequence=sequence,
        )
        return _StoreReadiness(
            store_uuid=str(metadata["store_uuid"]),
            schema_version=int(metadata["schema_version"]),
            canonical_sequence=sequence,
            projections=projections,
            importer_adapter_count=sum(int(row[1]) for row in adapter_rows),
            migration_issues=_migration_issue_readiness(connection),
        )
    except sqlite3.DatabaseError as exc:
        raise CutoverPromotionError("promoted live store is unreadable") from exc
    finally:
        connection.close()


def promote_candidate(
    preparation: CutoverPreparationReceipt,
    store_root: Path | str,
    *,
    receipt_path: Path | str,
) -> PromotionResult:
    """Install the prepared candidate over one stopped, healthy shadow store."""

    durable_receipt_path = _validate_receipt_output_path(receipt_path)
    candidate = _revalidate_preparation(preparation)
    root, directory_descriptor = _open_store_root(store_root)
    operation_id = uuid.uuid4().hex
    stage_name = f"{_PROMOTE_STAGE_PREFIX}{operation_id}{_STAGE_SUFFIX}"
    backup_name = f"{_BACKUP_PREFIX}{operation_id}{_BACKUP_SUFFIX}"
    emergency_name = f".{LIVE_STORE_FILENAME}.promotion-receipt-{operation_id}.json"
    stage_path = root / stage_name
    backup_path = root / backup_name
    emergency_path = root / emergency_name
    live_path = root / LIVE_STORE_FILENAME
    stage_identity: tuple[int, int] | None = None
    backup_identity: tuple[int, int] | None = None
    emergency_identity: tuple[int, int] | None = None
    receipt: PromotionReceipt | None = None
    installed = False
    replace_attempted = False
    previous_stat: os.stat_result | None = None
    try:
        backups, stages = _cutover_artifacts(directory_descriptor)
        if backups:
            raise CutoverPromotionError(
                "a shadow backup already exists; archive or rollback it before another promotion"
            )
        if stages:
            raise CutoverPromotionError(
                "a prior cutover staging file exists; inspect it before retrying"
            )
        previous = _inspect_live(root, require_current_projections=False)
        previous_digest, previous_stat = _hash_private_file(
            live_path,
            label="existing live shadow",
        )

        staged_digest, staged_size, stage_identity = _copy_path_to_new_name(
            preparation.candidate_path,
            directory_descriptor=directory_descriptor,
            destination_name=stage_name,
            label="candidate",
        )
        if (
            staged_digest != preparation.candidate_sha256
            or staged_size != preparation.candidate_size_bytes
        ):
            raise CutoverPromotionError("staged candidate bytes do not match preparation receipt")
        normalized = _prepare_promoted_stage(
            stage_path,
            expected_store_uuid=candidate.store_uuid,
            expected_sequence=candidate.canonical_sequence,
            expected_adapter_count=candidate.importer_adapter_count,
        )
        promoted_digest, promoted_stat = _hash_private_file(
            stage_path,
            label="promoted staging database",
        )

        backup_digest, backup_size, backup_identity = _copy_live_to_new_name(
            root,
            directory_descriptor=directory_descriptor,
            destination_name=backup_name,
        )
        backup_uuid, backup_schema, backup_sequence = _raw_store_metadata(
            backup_path,
            expected_role="live",
        )
        if (
            backup_uuid != previous.store_uuid
            or backup_schema != SCHEMA_VERSION
            or backup_sequence != previous.canonical_sequence
            or backup_digest != previous_digest
        ):
            raise CutoverPromotionError("shadow backup does not match the validated live store")
        # Re-prove that the target still is the exact inode copied to backup.
        current_digest, current_stat = _hash_private_file(
            live_path,
            label="existing live shadow",
        )
        if (
            _identity(current_stat) != _identity(previous_stat)
            or current_digest != backup_digest
            or int(current_stat.st_size) != backup_size
        ):
            raise CutoverPromotionError("live shadow changed while its backup was prepared")
        receipt = PromotionReceipt(
            receipt_version=CUTOVER_RECEIPT_VERSION,
            operation_id=operation_id,
            promoted_at_us=_now_us(),
            preparation=preparation,
            live_path=live_path,
            backup_path=backup_path,
            previous_live_store_uuid=previous.store_uuid,
            previous_live_sha256=backup_digest,
            previous_live_size_bytes=backup_size,
            promoted_store_uuid=candidate.store_uuid,
            promoted_sha256=promoted_digest,
            promoted_size_bytes=int(promoted_stat.st_size),
            promoted_device=int(promoted_stat.st_dev),
            promoted_inode=int(promoted_stat.st_ino),
            normalized_source_count=normalized,
        )
        # This recovery handle is durable BEFORE replacement.  A process or
        # machine crash after os.replace can therefore still load the exact
        # backup/live binding for rollback, even if the requested receipt has
        # not been written yet.
        write_promotion_receipt(emergency_path, receipt)
        emergency_identity = _identity(emergency_path.lstat())
        _reject_sidecars(live_path, label="live store")
        _fsync_directory(directory_descriptor)
        replace_attempted = True
        os.replace(
            stage_name,
            LIVE_STORE_FILENAME,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        installed = True
        _fsync_directory(directory_descriptor)
        live_stat = _require_private_regular(live_path, label="promoted live store")
        if _identity(live_stat) != _identity(promoted_stat):
            raise CutoverPromotionError("promoted live inode differs from staged inode")
    except BaseException as exc:
        replacement_observed = installed
        replacement_ambiguous = False
        if replace_attempted and receipt is not None:
            try:
                current = live_path.lstat()
            except OSError:
                replacement_ambiguous = True
            else:
                current_identity = _identity(current)
                if current_identity == (
                    receipt.promoted_device,
                    receipt.promoted_inode,
                ):
                    replacement_observed = True
                elif previous_stat is None or current_identity != _identity(previous_stat):
                    replacement_ambiguous = True
        if not replacement_observed and not replacement_ambiguous:
            _remove_created_name(
                stage_name,
                directory_descriptor=directory_descriptor,
                expected_identity=stage_identity,
            )
            _remove_created_name(
                backup_name,
                directory_descriptor=directory_descriptor,
                expected_identity=backup_identity,
            )
            _remove_created_name(
                emergency_name,
                directory_descriptor=directory_descriptor,
                expected_identity=emergency_identity,
            )
            try:
                _fsync_directory(directory_descriptor)
            except OSError:
                pass
            raise
        if receipt is not None and (replacement_observed or replacement_ambiguous):
            fallback_written = _load_matching_emergency_receipt(
                emergency_path,
                receipt,
            )
            raise CutoverPostReplaceError(
                receipt,
                exc,
                fallback_receipt_path=fallback_written,
                replacement_state=(
                    "installed" if replacement_observed else "ambiguous"
                ),
            ) from exc
        raise
    finally:
        os.close(directory_descriptor)

    assert receipt is not None
    try:
        write_promotion_receipt(durable_receipt_path, receipt)
    except BaseException as exc:
        fallback_written = _load_matching_emergency_receipt(
            emergency_path,
            receipt,
        )
        raise CutoverReceiptPersistenceError(
            receipt,
            durable_receipt_path,
            exc,
            fallback_receipt_path=fallback_written,
        ) from exc
    verification = verify_promotion(receipt)
    if not verification.ok:
        raise CutoverVerificationError(receipt, verification)
    # The requested receipt is now durable and verified.  Remove the
    # pre-replace emergency duplicate best-effort; retaining it is harmless
    # and safer than turning an otherwise successful cutover into a failure.
    cleanup_descriptor: int | None = None
    try:
        cleanup_root, cleanup_descriptor = _open_store_root(root)
        if cleanup_root == root:
            _remove_created_name(
                emergency_name,
                directory_descriptor=cleanup_descriptor,
                expected_identity=emergency_identity,
            )
            _fsync_directory(cleanup_descriptor)
    except Exception:
        pass
    finally:
        if cleanup_descriptor is not None:
            os.close(cleanup_descriptor)
    return PromotionResult(receipt=receipt, verification=verification)


def _verify_backup(receipt: PromotionReceipt) -> None:
    digest, observed = _hash_private_file(receipt.backup_path, label="shadow backup")
    if digest != receipt.previous_live_sha256:
        raise CutoverPromotionError("shadow backup digest differs from promotion receipt")
    if int(observed.st_size) != receipt.previous_live_size_bytes:
        raise CutoverPromotionError("shadow backup size differs from promotion receipt")
    store_uuid, schema_version, _sequence = _raw_store_metadata(
        receipt.backup_path,
        expected_role="live",
    )
    if store_uuid != receipt.previous_live_store_uuid or schema_version != SCHEMA_VERSION:
        raise CutoverPromotionError("shadow backup metadata differs from promotion receipt")


def verify_promotion(receipt: PromotionReceipt) -> CutoverVerification:
    """Verify the exact just-promoted inode and its one retained shadow backup."""

    checks: list[str] = []
    errors: list[str] = []
    store_uuid: str | None = None
    canonical_sequence: int | None = None
    digest: str | None = None
    try:
        parsed = _parse_promotion_receipt(
            receipt.to_dict(),
            require_preparation_artifacts=False,
        )
        if parsed != receipt:
            raise CutoverPromotionError("promotion receipt is not canonical")
        root, directory_descriptor = _open_store_root(receipt.live_path.parent)
        try:
            if root / LIVE_STORE_FILENAME != receipt.live_path:
                raise CutoverPromotionError("promotion receipt live path is not the reserved live name")
            backups, stages = _cutover_artifacts(directory_descriptor)
            if backups != [receipt.backup_path.name]:
                raise CutoverPromotionError("promotion must retain exactly its one shadow backup")
            if stages:
                raise CutoverPromotionError("cutover staging files remain after promotion")
        finally:
            os.close(directory_descriptor)
        _verify_backup(receipt)
        checks.append("shadow_backup")
        digest, observed = _hash_private_file(
            receipt.live_path,
            label="promoted live store",
        )
        if digest != receipt.promoted_sha256:
            raise CutoverPromotionError("promoted live digest differs from promotion receipt")
        if _identity(observed) != (receipt.promoted_device, receipt.promoted_inode):
            raise CutoverPromotionError("promoted live inode differs from promotion receipt")
        checks.append("promoted_identity")
        # Operator verification has no stop-the-world acknowledgement.  It
        # must never checkpoint or unlink another process's WAL/SHM; any
        # sidecar is instead an explicit, non-mutating blocked result.
        _reject_sidecars(receipt.live_path, label="live store verification")
        readiness = _inspect_live_immutable(receipt.live_path)
        _reject_sidecars(receipt.live_path, label="live store verification")
        store_uuid = readiness.store_uuid
        canonical_sequence = readiness.canonical_sequence
        if store_uuid != receipt.promoted_store_uuid:
            raise CutoverPromotionError("live store UUID differs from promotion receipt")
        if canonical_sequence != receipt.preparation.canonical_sequence:
            raise CutoverPromotionError("live canonical sequence differs from promotion receipt")
        if readiness.importer_adapter_count != receipt.normalized_source_count:
            raise CutoverPromotionError("live normalized source count differs from promotion receipt")
        checks.extend(("role_schema_integrity", "projections_current", "adapters_normalized"))
        final_digest, final_observed = _hash_private_file(
            receipt.live_path,
            label="promoted live store",
        )
        if (
            final_digest != digest
            or _identity(final_observed) != _identity(observed)
        ):
            raise CutoverPromotionError("promoted live store changed during verification")
        _reject_sidecars(receipt.live_path, label="live store verification")
    except Exception as exc:  # A typed verification result keeps failure visible to CLI callers.
        errors.append(f"{type(exc).__name__}: {exc}")
    return CutoverVerification(
        verified_at_us=_now_us(),
        ok=not errors,
        live_store_uuid=store_uuid,
        canonical_sequence=canonical_sequence,
        live_sha256=digest,
        checks=tuple(checks),
        errors=tuple(errors),
    )


def _verify_rollback(receipt: RollbackReceipt) -> CutoverVerification:
    checks: list[str] = []
    errors: list[str] = []
    store_uuid: str | None = None
    sequence: int | None = None
    digest: str | None = None
    try:
        root = receipt.live_path.parent
        readiness = _inspect_live(root, require_current_projections=False)
        store_uuid = readiness.store_uuid
        sequence = readiness.canonical_sequence
        if store_uuid != receipt.restored_store_uuid:
            raise CutoverPromotionError("rolled-back store UUID is not the shadow UUID")
        digest, observed = _hash_private_file(receipt.live_path, label="rolled-back live store")
        if digest != receipt.restored_sha256:
            raise CutoverPromotionError("rolled-back live digest differs from rollback receipt")
        if _identity(observed) != (receipt.restored_device, receipt.restored_inode):
            raise CutoverPromotionError("rolled-back live inode differs from rollback receipt")
        checks.extend(("role_schema_integrity", "shadow_bytes_restored"))
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    return CutoverVerification(
        verified_at_us=_now_us(),
        ok=not errors,
        live_store_uuid=store_uuid,
        canonical_sequence=sequence,
        live_sha256=digest,
        checks=tuple(checks),
        errors=tuple(errors),
    )


def rollback_promotion(receipt: PromotionReceipt) -> RollbackResult:
    """Atomically restore the exact shadow backup if the promoted DB is unchanged."""

    parsed = _parse_promotion_receipt(
        receipt.to_dict(),
        require_preparation_artifacts=False,
    )
    if parsed != receipt:
        raise CutoverPromotionError("promotion receipt is not canonical")
    # Rollback is a stop-the-world operation. Seal only after the caller's
    # acknowledgement so the non-mutating verify primitive can require a
    # sidecar-free main file without making a stopped, clean WAL a false block.
    _seal_stopped_live_store(receipt.live_path)
    current = verify_promotion(receipt)
    if not current.ok:
        raise CutoverPromotionError(
            "refusing rollback because the promoted store no longer matches its receipt: "
            + "; ".join(current.errors)
        )
    _verify_backup(receipt)
    root, directory_descriptor = _open_store_root(receipt.live_path.parent)
    operation_id = uuid.uuid4().hex
    stage_name = f"{_ROLLBACK_STAGE_PREFIX}{operation_id}{_STAGE_SUFFIX}"
    stage_path = root / stage_name
    stage_identity: tuple[int, int] | None = None
    rollback_receipt: RollbackReceipt | None = None
    installed = False
    replace_attempted = False
    promoted_identity: tuple[int, int] | None = None
    try:
        _digest, _size, stage_identity = _copy_path_to_new_name(
            receipt.backup_path,
            directory_descriptor=directory_descriptor,
            destination_name=stage_name,
            label="shadow backup",
        )
        restored_digest, staged = _hash_private_file(
            stage_path,
            label="rollback staging database",
        )
        if restored_digest != receipt.previous_live_sha256:
            raise CutoverPromotionError("rollback staging bytes differ from shadow backup")
        restored_uuid, schema_version, _sequence = _raw_store_metadata(
            stage_path,
            expected_role="live",
        )
        if (
            restored_uuid != receipt.previous_live_store_uuid
            or schema_version != SCHEMA_VERSION
        ):
            raise CutoverPromotionError("rollback staging metadata differs from shadow backup")
        current_digest, current_stat = _hash_private_file(
            receipt.live_path,
            label="promoted live store",
        )
        if (
            current_digest != receipt.promoted_sha256
            or _identity(current_stat)
            != (receipt.promoted_device, receipt.promoted_inode)
        ):
            raise CutoverPromotionError("promoted live store changed while rollback was staged")
        promoted_identity = _identity(current_stat)
        rollback_receipt = RollbackReceipt(
            receipt_version=CUTOVER_RECEIPT_VERSION,
            operation_id=operation_id,
            promotion_operation_id=receipt.operation_id,
            rolled_back_at_us=_now_us(),
            live_path=receipt.live_path,
            backup_path=receipt.backup_path,
            replaced_store_uuid=receipt.promoted_store_uuid,
            restored_store_uuid=restored_uuid,
            restored_sha256=restored_digest,
            restored_size_bytes=int(staged.st_size),
            restored_device=int(staged.st_dev),
            restored_inode=int(staged.st_ino),
        )
        _reject_sidecars(receipt.live_path, label="promoted live store")
        _fsync_directory(directory_descriptor)
        replace_attempted = True
        os.replace(
            stage_name,
            LIVE_STORE_FILENAME,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        installed = True
        _fsync_directory(directory_descriptor)
        restored = _require_private_regular(receipt.live_path, label="rolled-back live store")
        if _identity(restored) != _identity(staged):
            raise CutoverPromotionError("rolled-back live inode differs from staging inode")
    except BaseException as exc:
        replacement_observed = installed
        replacement_ambiguous = False
        if replace_attempted and rollback_receipt is not None:
            try:
                current_live = receipt.live_path.lstat()
            except OSError:
                replacement_ambiguous = True
            else:
                current_identity = _identity(current_live)
                restored_identity = (
                    rollback_receipt.restored_device,
                    rollback_receipt.restored_inode,
                )
                if current_identity == restored_identity:
                    replacement_observed = True
                elif promoted_identity is None or current_identity != promoted_identity:
                    replacement_ambiguous = True
        if not replacement_observed and not replacement_ambiguous:
            _remove_created_name(
                stage_name,
                directory_descriptor=directory_descriptor,
                expected_identity=stage_identity,
            )
            try:
                _fsync_directory(directory_descriptor)
            except OSError:
                pass
            raise
        raise RollbackPostReplaceError(
            rollback_receipt,
            exc,
            replacement_state=("installed" if replacement_observed else "ambiguous"),
        ) from exc
    finally:
        os.close(directory_descriptor)
    assert rollback_receipt is not None
    try:
        verification = _verify_rollback(rollback_receipt)
    except BaseException as exc:
        raise RollbackPostReplaceError(
            rollback_receipt,
            exc,
            replacement_state="installed",
        ) from exc
    if not verification.ok:
        cause = CutoverPromotionError(
            "shadow rollback completed but failed verification: "
            + "; ".join(verification.errors)
        )
        raise RollbackPostReplaceError(
            rollback_receipt,
            cause,
            replacement_state="installed",
            verification=verification,
        )
    return RollbackResult(receipt=rollback_receipt, verification=verification)


__all__ = [
    "CUTOVER_RECEIPT_VERSION",
    "PARITY_RUNNER_SCHEMA_VERSION",
    "CutoverError",
    "CutoverPreparationError",
    "CutoverPreparationReceipt",
    "CutoverPostReplaceError",
    "CutoverPromotionError",
    "CutoverReceiptPersistenceError",
    "CutoverVerification",
    "CutoverVerificationError",
    "ProjectionReadiness",
    "PromotionReceipt",
    "PromotionResult",
    "RollbackReceipt",
    "RollbackResult",
    "RollbackPostReplaceError",
    "prepare_cutover",
    "load_promotion_receipt",
    "promote_candidate",
    "rollback_promotion",
    "verify_promotion",
    "write_promotion_receipt",
]
