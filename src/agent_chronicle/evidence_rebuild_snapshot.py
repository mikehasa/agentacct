"""Owner-gated, fail-closed snapshots for an offline agentacct store.

The live store is only ever opened read-only by this module.  Snapshot
creation requires an explicit writers-stopped acknowledgement *and* proves
that every existing agentacct lock can be acquired without blocking.  The
acknowledgement is therefore not a substitute for an observed lock check.

The snapshot layout is intentionally simple::

    <snapshot>/
      store/                    # byte-for-byte copy of the source tree
      snapshot-manifest.json   # per-file content and filesystem identity
      snapshot-receipt.json    # manifest digest and validation result

Verification does not instantiate :class:`EvidenceStore`; doing so would
recover spools and write the projection.  SQLite files are opened with
``mode=ro`` and ``query_only`` and all spool/cursor checks are implemented
locally.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, BinaryIO, Iterator, Mapping, Sequence

from .canonical.live_paths import LivePathSafetyError, paths_overlap, reject_live_state_overlap
from .evidence import canonical_digest, canonical_json_bytes, normalize_timestamp
from .evidence_rebuild_activation import EvidenceActivationError, fingerprint_evidence_tree


SNAPSHOT_SCHEMA_VERSION = "agent-chronicle.evidence-snapshot.v1"
SNAPSHOT_RECEIPT_SCHEMA_VERSION = "agent-chronicle.evidence-snapshot-receipt.v1"
RESTORE_RECEIPT_SCHEMA_VERSION = "agent-chronicle.evidence-restore-rehearsal.v1"
MANIFEST_FILENAME = "snapshot-manifest.json"
RECEIPT_FILENAME = "snapshot-receipt.json"
PAYLOAD_DIRNAME = "store"
RESTORE_RECEIPT_FILENAME = "restore-rehearsal-receipt.json"

EVIDENCE_SPOOL_SCHEMA_VERSION = "agent-chronicle.evidence-spool.v1"
REFRESHABLE_USAGE_SPOOL_SCHEMA_VERSION = "agent-chronicle.refreshable-usage-spool.v1"
EVIDENCE_STORE_SCHEMA_VERSION = "agent-chronicle.evidence-store.v1"
EVIDENCE_ROOT = PurePosixPath("evidence-v2")
MAIN_SPOOL = EVIDENCE_ROOT / "spool.jsonl"
REFRESHABLE_SPOOL = EVIDENCE_ROOT / "refreshable-usage.jsonl"
PROJECTION = EVIDENCE_ROOT / "projection.sqlite3"
_REFRESHABLE_PROJECTION_TABLES = (
    "refreshable_usage_batch_receipts",
    "refreshable_usage_revisions",
    "refreshable_usage_heads",
    "refreshable_usage_conflicts",
    "refreshable_usage_transitions",
)

# Tables populated exclusively by replaying spool.jsonl. A store whose main
# spool never existed (refresh-only usage store) must show all of them empty.
# They deliberately exclude the refreshable lane, store_metadata, and
# spool_errors (which also records refresh-lane damage and can therefore be
# legitimately non-empty without a main spool).
_GENERIC_PROJECTION_TABLES = (
    "evidence_receipts",
    "evidence_versions",
    "evidence_dimensions",
    "evidence_acknowledgements",
    "claimed_link_receipts",
    "claimed_link_versions",
)

_COPY_CHUNK_BYTES = 1024 * 1024
_MAX_SPOOL_RECORD_BYTES = 64 * 1024 * 1024
_AT_FDCWD = -100
_RENAME_NOREPLACE = 0x1
_RENAME_EXCL = 0x4
_METADATA_FILES = frozenset({MANIFEST_FILENAME, RECEIPT_FILENAME})
_STAT_FIELDS = ("mode", "uid", "gid", "mtime_ns", "inode", "device")
_RESTORE_STEPS = (
    "stop_all_dashboard_watcher_mcp_and_hook_writers",
    "verify_restore_target_is_new_private_and_disjoint",
    "preserve_failed_live_store_aside_without_deleting_it",
    "copy_store_tree_from_sealed_snapshot",
    "recompute_sha256_tree_digest_sqlite_spool_fence_and_cursor_checks",
    "restart_with_the_preincident_runtime_flags",
    "verify_health_issues_empty_and_observe_multiple_complete_watcher_ticks",
)


class EvidenceSnapshotError(RuntimeError):
    """Base class for snapshot safety and integrity failures."""


class SnapshotSafetyError(EvidenceSnapshotError):
    """A source, destination, or lock boundary is unsafe."""


class SnapshotDriftError(EvidenceSnapshotError):
    """The source changed while it was being copied or verified."""


class SnapshotIntegrityError(EvidenceSnapshotError):
    """Snapshot bytes, metadata, SQLite, or spool invariants are invalid."""


def _publish_new_directory(source: Path, destination: Path) -> None:
    """Atomically publish ``source`` without ever replacing ``destination``.

    A stopped store can take many minutes to copy.  An absence check before
    that copy is not a no-overwrite guarantee: another process may claim the
    requested snapshot name in the meantime.  Use the platform's atomic
    no-replace rename and fail closed where no such primitive is available.
    """

    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    operation = None
    arguments: tuple[object, ...]
    if sys.platform == "darwin":
        operation = getattr(libc, "renamex_np", None)
        if operation is not None:
            operation.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            operation.restype = ctypes.c_int
        arguments = (source_bytes, destination_bytes, _RENAME_EXCL)
    elif sys.platform.startswith("linux"):
        operation = getattr(libc, "renameat2", None)
        if operation is not None:
            operation.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            operation.restype = ctypes.c_int
        arguments = (
            _AT_FDCWD,
            source_bytes,
            _AT_FDCWD,
            destination_bytes,
            _RENAME_NOREPLACE,
        )
    else:
        arguments = ()
    if operation is None:
        raise SnapshotSafetyError(
            "atomic no-replace snapshot publication is unavailable on this platform"
        )
    ctypes.set_errno(0)
    if operation(*arguments) == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise SnapshotSafetyError(
            "snapshot or restore target was concurrently created; refusing overwrite"
        )
    raise OSError(error_number, os.strerror(error_number), str(destination))


class _SpoolBoundaryError(SnapshotIntegrityError):
    def __init__(self, path: Path, offset: int, *, exceeds: bool) -> None:
        self.path = path
        self.offset = offset
        detail = "exceeds readable spool" if exceeds else "is not a record boundary"
        super().__init__(f"required offset {offset} {detail}: {path}")


@dataclass(frozen=True, slots=True)
class FileIdentity:
    mode: int
    uid: int
    gid: int
    mtime_ns: int
    ctime_ns: int
    inode: int
    device: int
    size_bytes: int
    link_count: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> "FileIdentity":
        return cls(
            mode=stat.S_IMODE(value.st_mode),
            uid=value.st_uid,
            gid=value.st_gid,
            mtime_ns=value.st_mtime_ns,
            ctime_ns=value.st_ctime_ns,
            inode=value.st_ino,
            device=value.st_dev,
            size_bytes=value.st_size,
            link_count=value.st_nlink,
        )

    def manifest_dict(self) -> dict[str, int]:
        # ctime and link count are used for drift/hard-link detection but are
        # not portable restore attributes.  They remain recorded as evidence.
        return {
            "mode": self.mode,
            "uid": self.uid,
            "gid": self.gid,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
            "inode": self.inode,
            "device": self.device,
            "size_bytes": self.size_bytes,
            "link_count": self.link_count,
        }


@dataclass(frozen=True, slots=True)
class _TreeInventory:
    root_identity: FileIdentity
    directories: tuple[str, ...]
    directory_identities: tuple[tuple[str, FileIdentity], ...]
    files: tuple[tuple[str, FileIdentity], ...]

    @property
    def by_path(self) -> dict[str, FileIdentity]:
        return dict(self.files)

    @property
    def directory_modes(self) -> dict[str, int]:
        return {path: identity.mode for path, identity in self.directory_identities}


@dataclass(frozen=True, slots=True)
class SnapshotVerification:
    snapshot_root: Path
    manifest_sha256: str
    tree_digest: str
    file_count: int
    total_bytes: int
    canonical_store_uuid: str
    canonical_schema_version: int
    sqlite_files_checked: int
    main_spool_present: bool
    main_spool_records: int
    main_spool_bytes: int
    main_spool_invalid_records: int
    refreshable_spool_present: bool
    refreshable_spool_records: int
    refreshable_spool_bytes: int
    refreshable_spool_invalid_records: int
    main_replay_offset: int
    refreshable_replay_offset: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_root": str(self.snapshot_root),
            "manifest_sha256": self.manifest_sha256,
            "tree_digest": self.tree_digest,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "canonical_store_uuid": self.canonical_store_uuid,
            "canonical_schema_version": self.canonical_schema_version,
            "sqlite_files_checked": self.sqlite_files_checked,
            "main_spool_present": self.main_spool_present,
            "main_spool_records": self.main_spool_records,
            "main_spool_bytes": self.main_spool_bytes,
            "main_spool_invalid_records": self.main_spool_invalid_records,
            "refreshable_spool_present": self.refreshable_spool_present,
            "refreshable_spool_records": self.refreshable_spool_records,
            "refreshable_spool_bytes": self.refreshable_spool_bytes,
            "refreshable_spool_invalid_records": self.refreshable_spool_invalid_records,
            "main_replay_offset": self.main_replay_offset,
            "refreshable_replay_offset": self.refreshable_replay_offset,
        }


@dataclass(frozen=True, slots=True)
class SnapshotCreationResult:
    snapshot_root: Path
    manifest_path: Path
    receipt_path: Path
    verification: SnapshotVerification

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_root": str(self.snapshot_root),
            "manifest_path": str(self.manifest_path),
            "receipt_path": str(self.receipt_path),
            "verification": self.verification.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class VerifiedEvidenceSnapshot:
    """Manifest-bound, read-only access to files in one verified snapshot."""

    root: Path
    verification: SnapshotVerification
    _files: Mapping[str, tuple[FileIdentity, str]]
    _manifest_identity: FileIdentity

    @classmethod
    def verify(cls, snapshot_root: Path | str) -> "VerifiedEvidenceSnapshot":
        verification = verify_evidence_snapshot(snapshot_root)
        root = verification.snapshot_root
        manifest, _raw, _receipt, _receipt_raw = _load_snapshot_metadata(root)
        _directories, files = _verify_manifest_shape(manifest)
        by_path = MappingProxyType(
            {
                str(item["path"]): (
                    _manifest_entry_identity(
                        item["snapshot_stat"], label=f"{item['path']}.snapshot_stat"
                    ),
                    str(item["sha256"]),
                )
                for item in files
            }
        )
        return cls(
            root=root,
            verification=verification,
            _files=by_path,
            _manifest_identity=FileIdentity.from_stat((root / MANIFEST_FILENAME).lstat()),
        )

    @contextmanager
    def open_binary(self, relative_path: str | PurePosixPath) -> Iterator[BinaryIO]:
        relative = _safe_relative_path(relative_path)
        try:
            expected, _digest = self._files[relative]
        except KeyError as exc:
            raise SnapshotIntegrityError(
                f"path is not declared by the verified snapshot: {relative!r}"
            ) from exc
        path = self.root / PAYLOAD_DIRNAME / PurePosixPath(relative)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        handle: BinaryIO | None = None
        try:
            if FileIdentity.from_stat(os.fstat(descriptor)) != expected:
                raise SnapshotDriftError(f"snapshot file changed before open: {relative}")
            handle = os.fdopen(descriptor, "rb", closefd=True)
            descriptor = -1
            yield handle
        finally:
            try:
                if handle is not None:
                    try:
                        final_identity = FileIdentity.from_stat(os.fstat(handle.fileno()))
                    except (OSError, ValueError) as exc:
                        raise SnapshotDriftError(
                            f"snapshot file handle became invalid while open: {relative}"
                        ) from exc
                    if final_identity != expected:
                        raise SnapshotDriftError(f"snapshot file changed while open: {relative}")
                if (
                    FileIdentity.from_stat((self.root / MANIFEST_FILENAME).lstat())
                    != self._manifest_identity
                ):
                    raise SnapshotDriftError("snapshot manifest changed while a file was open")
            finally:
                if handle is not None:
                    handle.close()
                elif descriptor >= 0:
                    os.close(descriptor)

    def to_dict(self) -> dict[str, Any]:
        return self.verification.to_dict()


@dataclass(frozen=True, slots=True)
class RestoreRehearsalResult:
    restore_root: Path
    receipt_path: Path
    source_manifest_sha256: str
    tree_digest: str
    file_count: int
    total_bytes: int
    canonical_store_uuid: str
    canonical_schema_version: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "restore_root": str(self.restore_root),
            "receipt_path": str(self.receipt_path),
            "source_manifest_sha256": self.source_manifest_sha256,
            "tree_digest": self.tree_digest,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "canonical_store_uuid": self.canonical_store_uuid,
            "canonical_schema_version": self.canonical_schema_version,
        }


@dataclass(frozen=True, slots=True)
class _PayloadValidation:
    canonical_store_uuid: str
    canonical_schema_version: int
    sqlite_files_checked: int
    main_spool_present: bool
    main_spool_records: int
    main_spool_bytes: int
    main_spool_invalid_records: int
    refreshable_spool_present: bool
    refreshable_spool_records: int
    refreshable_spool_bytes: int
    refreshable_spool_invalid_records: int
    main_replay_offset: int
    refreshable_replay_offset: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json_no_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key!r}")
        value[key] = child
    return value


def _load_strict_json(raw: bytes, *, label: str) -> Any:
    try:
        text = raw.decode("utf-8", errors="strict")
        return json.loads(text, object_pairs_hook=_json_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SnapshotIntegrityError(f"{label} is not strict JSON: {exc}") from exc


def _write_private_json(path: Path, value: Mapping[str, Any]) -> bytes:
    raw = canonical_json_bytes(value) + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return raw


def _assert_absolute_no_symlink(path: Path, *, label: str, must_exist: bool) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        raise SnapshotSafetyError(f"{label} must be an explicit absolute path")
    current = Path(path.anchor)
    parts = path.parts[1:]
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if must_exist or index != len(parts) - 1:
                raise SnapshotSafetyError(f"{label} does not exist: {current}")
            break
        if stat.S_ISLNK(info.st_mode):
            raise SnapshotSafetyError(f"{label} contains a symlink component")
    if must_exist and not path.exists():
        raise SnapshotSafetyError(f"{label} does not exist")
    try:
        return path.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise SnapshotSafetyError(f"{label} cannot be resolved safely") from exc


def _validate_source(source_root: Path | str) -> Path:
    source = _assert_absolute_no_symlink(Path(source_root), label="source root", must_exist=True)
    info = source.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise SnapshotSafetyError("source root must be a directory")
    return source


def _validate_new_target(
    target_root: Path | str,
    *,
    source_root: Path | None = None,
    label: str,
) -> Path:
    requested = Path(target_root).expanduser()
    if not requested.is_absolute():
        raise SnapshotSafetyError(f"{label} must be an explicit absolute path")
    try:
        reject_live_state_overlap(requested, label=label)
    except LivePathSafetyError as exc:
        raise SnapshotSafetyError(str(exc)) from exc
    if requested.exists() or requested.is_symlink():
        raise SnapshotSafetyError(f"{label} must not already exist")
    parent = _assert_absolute_no_symlink(requested.parent, label=f"{label} parent", must_exist=True)
    parent_info = parent.lstat()
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != os.geteuid()
        or stat.S_IMODE(parent_info.st_mode) != 0o700
    ):
        raise SnapshotSafetyError(
            f"{label} parent must be an owner-only directory with mode 0700"
        )
    target = parent / requested.name
    if source_root is not None and paths_overlap(source_root, target):
        raise SnapshotSafetyError(f"{label} must be disjoint from the source root")
    if target.name in {"", ".", ".."}:
        raise SnapshotSafetyError(f"{label} name is unsafe")
    return target


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _safe_relative_path(value: str | PurePosixPath) -> str:
    raw = str(value)
    candidate = PurePosixPath(raw)
    if (
        not raw
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or str(candidate) != raw
    ):
        raise SnapshotSafetyError("snapshot file path must be canonical and relative")
    return raw


def _scan_tree(root: Path) -> _TreeInventory:
    root_info = root.lstat()
    if not stat.S_ISDIR(root_info.st_mode):
        raise SnapshotSafetyError(f"tree root is not a directory: {root}")
    directories: list[str] = []
    directory_identities: list[tuple[str, FileIdentity]] = []
    files: list[tuple[str, FileIdentity]] = []

    def visit(directory: Path) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise SnapshotSafetyError(f"cannot inventory source tree: {directory}") from exc
        for child in children:
            child_path = Path(child.path)
            try:
                info = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise SnapshotSafetyError(f"cannot stat source entry: {child_path}") from exc
            relative = _relative(child_path, root)
            if stat.S_ISLNK(info.st_mode):
                raise SnapshotSafetyError(f"source tree contains symlink: {relative}")
            if stat.S_ISDIR(info.st_mode):
                directories.append(relative)
                directory_identities.append((relative, FileIdentity.from_stat(info)))
                visit(child_path)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise SnapshotSafetyError(f"source tree contains non-regular file: {relative}")
            identity = FileIdentity.from_stat(info)
            if identity.link_count != 1:
                raise SnapshotSafetyError(
                    f"source tree contains hard-linked file: {relative} (links={identity.link_count})"
                )
            files.append((relative, identity))

    visit(root)
    return _TreeInventory(
        FileIdentity.from_stat(root_info),
        tuple(sorted(directories)),
        tuple(sorted(directory_identities, key=lambda item: item[0])),
        tuple(sorted(files, key=lambda item: item[0])),
    )


def _expected_lock_paths(
    source: Path,
    inventory: _TreeInventory,
) -> tuple[tuple[Path, FileIdentity], ...]:
    by_path = inventory.by_path
    required: list[str] = []
    if "events.jsonl" in by_path:
        required.append("events.jsonl.lock")
    if any(path == "evidence-v2" or path.startswith("evidence-v2/") for path in inventory.directories):
        required.append("evidence-v2/.spool.lock")
    missing = [path for path in required if path not in by_path]
    if missing:
        raise SnapshotSafetyError(
            "cannot prove writers stopped because required lock files are missing: "
            + ", ".join(missing)
        )
    lock_paths = [
        (source / path, by_path[path])
        for path in by_path
        if path.endswith(".lock")
    ]
    if not lock_paths:
        raise SnapshotSafetyError("cannot prove writers stopped because no lock files exist")
    return tuple(sorted(lock_paths, key=lambda item: item[0].as_posix()))


@contextmanager
def _hold_writer_locks(paths: Sequence[tuple[Path, FileIdentity]]) -> Iterator[None]:
    descriptors: list[int] = []
    try:
        for path, expected in paths:
            flags = os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags)
            info = os.fstat(descriptor)
            actual = FileIdentity.from_stat(info)
            if (
                not stat.S_ISREG(info.st_mode)
                or actual != expected
                or actual.link_count != 1
                or actual.uid != os.geteuid()
                or actual.mode & 0o077
            ):
                os.close(descriptor)
                raise SnapshotSafetyError(
                    f"writer lock identity/ownership/mode is unsafe: {path}"
                )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as exc:
                os.close(descriptor)
                if isinstance(exc, BlockingIOError) or getattr(exc, "errno", None) in {
                    errno.EACCES,
                    errno.EAGAIN,
                }:
                    raise SnapshotSafetyError(f"writer lock is active: {path}") from exc
                raise
            descriptors.append(descriptor)
        yield
    finally:
        for descriptor in reversed(descriptors):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _identity_matches(actual: os.stat_result, expected: FileIdentity) -> bool:
    return FileIdentity.from_stat(actual) == expected


def _stream_copy(source: Path, target: Path, expected: FileIdentity) -> str:
    source_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
    source_descriptor = os.open(source, source_flags)
    target_descriptor = -1
    digest = hashlib.sha256()
    try:
        before = os.fstat(source_descriptor)
        if not _identity_matches(before, expected) or not stat.S_ISREG(before.st_mode):
            raise SnapshotDriftError(f"source changed before copy: {source}")
        target_descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.fchmod(target_descriptor, 0o600)
        while True:
            chunk = os.read(source_descriptor, _COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(target_descriptor, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
        os.fsync(target_descriptor)
        after = os.fstat(source_descriptor)
        if not _identity_matches(after, expected):
            raise SnapshotDriftError(f"source changed during copy: {source}")
        if os.fstat(target_descriptor).st_size != expected.size_bytes:
            raise SnapshotDriftError(f"copied size differs from source: {source}")
    finally:
        if target_descriptor >= 0:
            os.close(target_descriptor)
        os.close(source_descriptor)
    return digest.hexdigest()


def _copy_inventory(
    source: Path,
    payload: Path,
    inventory: _TreeInventory,
) -> dict[str, str]:
    payload.mkdir(mode=0o700)
    os.chmod(payload, 0o700)
    for relative in inventory.directories:
        directory = payload / PurePosixPath(relative)
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
    digests: dict[str, str] = {}
    for relative, identity in inventory.files:
        destination = payload / PurePosixPath(relative)
        digests[relative] = _stream_copy(source / PurePosixPath(relative), destination, identity)
    _fsync_tree(payload)
    return digests


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    directories = [root]
    directories.extend(path for path in root.rglob("*") if path.is_dir())
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _fsync_directory(directory)


def _hash_regular_file(path: Path) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise SnapshotIntegrityError(f"snapshot payload is not a private regular file: {path}")
        while True:
            chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if FileIdentity.from_stat(before) != FileIdentity.from_stat(after):
            raise SnapshotDriftError(f"file changed while hashing: {path}")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _open_sqlite_read_only(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(
            path.resolve(strict=True).as_uri() + "?mode=ro",
            uri=True,
            isolation_level=None,
            timeout=5,
        )
        connection.execute("PRAGMA query_only = ON")
        if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
            connection.close()
            raise SnapshotIntegrityError(f"SQLite query_only could not be enabled: {path}")
        return connection
    except (OSError, sqlite3.DatabaseError) as exc:
        raise SnapshotIntegrityError(f"cannot open SQLite read-only: {path}: {exc}") from exc


@contextmanager
def _sqlite_verification_copy(path: Path, *, sealed_root: Path) -> Iterator[Path]:
    """Yield a private disposable db+sidecar copy for SQLite inspection.

    Even a ``mode=ro`` WAL reader may update shared-memory read marks.  The
    sealed snapshot is therefore never passed to sqlite3 at all.
    """

    temporary_root = Path(tempfile.mkdtemp(prefix="agent-chronicle-sqlite-verify-"))
    os.chmod(temporary_root, 0o700)
    if paths_overlap(temporary_root.resolve(strict=True), sealed_root.resolve(strict=True)):
        shutil.rmtree(temporary_root)
        raise SnapshotSafetyError("SQLite verification temp must be disjoint from sealed bytes")
    try:
        copied = temporary_root / path.name
        bundle = [path]
        bundle.extend(
            candidate
            for suffix in ("-wal", "-shm", "-journal")
            if (candidate := path.with_name(path.name + suffix)).is_file()
        )
        for source in bundle:
            info = source.lstat()
            identity = FileIdentity.from_stat(info)
            if not stat.S_ISREG(info.st_mode) or identity.link_count != 1:
                raise SnapshotIntegrityError(
                    f"SQLite bundle member is not a private regular file: {source}"
                )
            destination = temporary_root / source.name
            _stream_copy(source, destination, identity)
        _fsync_tree(temporary_root)
        yield copied
    finally:
        shutil.rmtree(temporary_root)


def _validate_sqlite(path: Path) -> None:
    connection = _open_sqlite_read_only(path)
    try:
        rows = connection.execute("PRAGMA quick_check").fetchall()
        if not rows or any(tuple(row) != ("ok",) for row in rows):
            raise SnapshotIntegrityError(f"SQLite quick_check failed: {path}")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise SnapshotIntegrityError(f"SQLite foreign_key_check failed: {path}")
    except sqlite3.DatabaseError as exc:
        raise SnapshotIntegrityError(f"SQLite integrity check failed: {path}: {exc}") from exc
    finally:
        connection.close()


def _canonical_metadata(path: Path) -> tuple[str, int]:
    connection = _open_sqlite_read_only(path)
    try:
        rows = connection.execute(
            "SELECT singleton, store_uuid, schema_version FROM store_metadata"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise SnapshotIntegrityError(f"canonical store metadata is unreadable: {exc}") from exc
    finally:
        connection.close()
    if len(rows) != 1 or rows[0][0] != 1:
        raise SnapshotIntegrityError("canonical store_metadata must contain exactly singleton=1")
    store_uuid = rows[0][1]
    schema_version = rows[0][2]
    if not isinstance(store_uuid, str) or not store_uuid.strip():
        raise SnapshotIntegrityError("canonical store UUID is invalid")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version < 1:
        raise SnapshotIntegrityError("canonical schema version is invalid")
    return store_uuid, schema_version


def _strict_spool_records(
    path: Path,
    *,
    refreshable: bool,
    required_boundaries: Sequence[int] = (),
) -> tuple[int, int, tuple[int, ...], int]:
    targets = sorted(set(required_boundaries))
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in targets):
        raise SnapshotIntegrityError(f"spool required boundary is invalid: {path}")
    target_index = 0
    while target_index < len(targets) and targets[target_index] == 0:
        target_index += 1
    fences: list[int] = []
    offset = 0
    count = 0
    invalid = 0
    previous_fence = -1

    def _validate_record(line: bytes, line_number: int) -> dict[str, Any] | None:
        """Strict record validation mirroring live replay's damage model.

        The live EvidenceStore deliberately preserves a torn/invalid record
        verbatim (newline-terminated) and keeps operating, counting it in
        ``invalid_spool_records``. A snapshot of such a store is healthy by
        the store's own contract, so preserved damage is counted here rather
        than making the DR entry gate permanently refuse the store. Structural
        violations (oversize records, a non-newline tail, boundary breaks)
        still raise.
        """

        if not line[:-1].strip():
            return None
        try:
            record = _load_strict_json(line[:-1], label=f"spool record {path}:{line_number}")
        except SnapshotIntegrityError:
            return None
        if not isinstance(record, dict):
            return None
        expected_schema = (
            REFRESHABLE_USAGE_SPOOL_SCHEMA_VERSION
            if refreshable
            else EVIDENCE_SPOOL_SCHEMA_VERSION
        )
        if record.get("spool_schema_version") != expected_schema:
            return None
        receipt_id = record.get("receipt_id")
        if not isinstance(receipt_id, str) or not receipt_id.startswith("rcp_"):
            return None
        kind = record.get("kind")
        if (refreshable and kind != "refreshable_usage") or (
            not refreshable and kind not in {"evidence", "claimed_link"}
        ):
            return None
        if not isinstance(record.get("payload"), dict):
            return None
        try:
            normalize_timestamp(record.get("received_at"), "received_at")
        except (TypeError, ValueError):
            return None
        expected_hash = canonical_digest(
            {key: value for key, value in record.items() if key != "record_hash"}
        )
        if record.get("record_hash") != expected_hash:
            return None
        return record

    with path.open("rb") as handle:
        line_number = 0
        while True:
            line = handle.readline(_MAX_SPOOL_RECORD_BYTES + 1)
            if not line:
                break
            line_number += 1
            if len(line) > _MAX_SPOOL_RECORD_BYTES:
                raise SnapshotIntegrityError(
                    f"spool record exceeds {_MAX_SPOOL_RECORD_BYTES} bytes: {path}:{line_number}"
                )
            if not line.endswith(b"\n"):
                raise SnapshotIntegrityError(f"spool has a truncated/non-newline tail: {path}")
            record = _validate_record(line, line_number)
            if record is None:
                # Blank filler and preserved-invalid records are counted (the
                # latter), never projected, and never carry a fence; their end
                # offsets remain valid record boundaries exactly as in the
                # live store's replay.
                if line[:-1].strip():
                    invalid += 1
                offset += len(line)
                while target_index < len(targets) and targets[target_index] == offset:
                    target_index += 1
                if target_index < len(targets) and targets[target_index] < offset:
                    raise _SpoolBoundaryError(path, targets[target_index], exceeds=False)
                continue
            if refreshable:
                fence = record.get("main_spool_fence")
                if isinstance(fence, bool) or not isinstance(fence, int) or fence < 0:
                    raise SnapshotIntegrityError(
                        f"refreshable spool fence is invalid: {path}:{line_number}"
                    )
                if fence < previous_fence:
                    raise SnapshotIntegrityError("refreshable main_spool_fence moved backwards")
                previous_fence = fence
                if not fences or fences[-1] != fence:
                    fences.append(fence)
            offset += len(line)
            while target_index < len(targets) and targets[target_index] == offset:
                target_index += 1
            if target_index < len(targets) and targets[target_index] < offset:
                raise _SpoolBoundaryError(path, targets[target_index], exceeds=False)
            count += 1
    if target_index != len(targets):
        raise _SpoolBoundaryError(path, targets[target_index], exceeds=True)
    return count, offset, tuple(fences), invalid


def _projection_cursors(path: Path) -> tuple[int, int]:
    connection = _open_sqlite_read_only(path)
    try:
        rows = dict(
            connection.execute(
                "SELECT key, value FROM store_metadata WHERE key IN "
                "('schema_version', 'replay_offset', 'refreshable_usage_replay_offset')"
            ).fetchall()
        )
    except sqlite3.DatabaseError as exc:
        raise SnapshotIntegrityError(f"Evidence projection metadata is unreadable: {exc}") from exc
    finally:
        connection.close()
    if rows.get("schema_version") != EVIDENCE_STORE_SCHEMA_VERSION:
        raise SnapshotIntegrityError("Evidence projection schema version is invalid")

    def parse(key: str) -> int:
        raw = rows.get(key, "0")
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise SnapshotIntegrityError(f"Evidence projection {key} is invalid") from exc
        if value < 0 or str(value) != str(raw):
            raise SnapshotIntegrityError(f"Evidence projection {key} is invalid")
        return value

    return parse("replay_offset"), parse("refreshable_usage_replay_offset")


def _projection_state_counts(
    path: Path,
    *,
    tables: tuple[str, ...],
    lane_label: str,
) -> dict[str, int]:
    """Return populated tables for one lane without assuming a post-P0 schema."""

    connection = _open_sqlite_read_only(path)
    try:
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
        populated: dict[str, int] = {}
        for table_name in tables:
            if table_name not in table_names:
                continue
            row = connection.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()
            if (
                row is None
                or isinstance(row[0], bool)
                or not isinstance(row[0], int)
                or row[0] < 0
            ):
                raise SnapshotIntegrityError(
                    f"Evidence projection {table_name} row count is invalid"
                )
            if row[0]:
                populated[table_name] = row[0]
        return populated
    except sqlite3.DatabaseError as exc:
        raise SnapshotIntegrityError(
            f"Evidence {lane_label} projection state is unreadable: {exc}"
        ) from exc
    finally:
        connection.close()


def _validate_payload(payload: Path) -> _PayloadValidation:
    canonical = payload / "chronicle.sqlite3"
    if not canonical.is_file():
        raise SnapshotIntegrityError("snapshot is missing canonical chronicle.sqlite3")
    evidence_root = payload / EVIDENCE_ROOT
    main_spool = payload / MAIN_SPOOL
    refreshable_spool = payload / REFRESHABLE_SPOOL
    projection = payload / PROJECTION
    if not evidence_root.is_dir() or not projection.is_file():
        raise SnapshotIntegrityError(
            "snapshot must contain the Evidence root and projection.sqlite3"
        )
    # Both spools are lazy: the generic spool is only created on the first
    # generic append, so a store driven purely by the usage importer/watcher
    # legitimately has refreshable-usage.jsonl and no spool.jsonl. Absence is
    # validated against the projection cursors/state below instead of being
    # refused at the door.
    main_spool_present = main_spool.is_file()
    if main_spool.exists() and not main_spool_present:
        raise SnapshotIntegrityError(
            "snapshot spool.jsonl path must be a regular file when present"
        )
    refreshable_spool_present = refreshable_spool.is_file()
    if refreshable_spool.exists() and not refreshable_spool_present:
        raise SnapshotIntegrityError(
            "snapshot refreshable-usage.jsonl path must be a regular file when present"
        )

    sqlite_paths = sorted(payload.rglob("*.sqlite3"), key=lambda item: item.as_posix())
    store_uuid: str | None = None
    schema_version: int | None = None
    main_cursor: int | None = None
    refresh_cursor: int | None = None
    refresh_state_counts: dict[str, int] | None = None
    generic_state_counts: dict[str, int] | None = None
    for sqlite_path in sqlite_paths:
        with _sqlite_verification_copy(sqlite_path, sealed_root=payload) as sqlite_copy:
            _validate_sqlite(sqlite_copy)
            if sqlite_path == canonical:
                store_uuid, schema_version = _canonical_metadata(sqlite_copy)
            if sqlite_path == projection:
                main_cursor, refresh_cursor = _projection_cursors(sqlite_copy)
                if not refreshable_spool_present:
                    refresh_state_counts = _projection_state_counts(
                        sqlite_copy,
                        tables=_REFRESHABLE_PROJECTION_TABLES,
                        lane_label="refreshable",
                    )
                if not main_spool_present:
                    generic_state_counts = _projection_state_counts(
                        sqlite_copy,
                        tables=_GENERIC_PROJECTION_TABLES,
                        lane_label="generic",
                    )
    if store_uuid is None or schema_version is None:
        raise SnapshotIntegrityError("canonical store metadata was not validated")
    if main_cursor is None or refresh_cursor is None:
        raise SnapshotIntegrityError("Evidence projection cursors were not validated")

    if refreshable_spool_present:
        try:
            refresh_count, refresh_size, fences, refresh_invalid = _strict_spool_records(
                refreshable_spool,
                refreshable=True,
                required_boundaries=(refresh_cursor,),
            )
        except _SpoolBoundaryError as exc:
            raise SnapshotIntegrityError(
                "Evidence refreshable_usage_replay_offset is not a refreshable spool record boundary"
            ) from exc
    else:
        if refresh_cursor != 0:
            raise SnapshotIntegrityError(
                "Evidence refreshable_usage_replay_offset must be zero when "
                "refreshable-usage.jsonl is absent"
            )
        if refresh_state_counts is None:
            raise SnapshotIntegrityError("Evidence refreshable projection state was not validated")
        if refresh_state_counts:
            detail = ", ".join(
                f"{table_name}={count}"
                for table_name, count in sorted(refresh_state_counts.items())
            )
            raise SnapshotIntegrityError(
                "Evidence refreshable projection state is non-empty while "
                f"refreshable-usage.jsonl is absent: {detail}"
            )
        refresh_count = 0
        refresh_size = 0
        fences = ()
        refresh_invalid = 0
    if main_spool_present:
        try:
            main_count, main_size, _, main_invalid = _strict_spool_records(
                main_spool,
                refreshable=False,
                required_boundaries=(main_cursor, *fences),
            )
        except _SpoolBoundaryError as exc:
            if exc.offset == main_cursor and exc.offset not in fences:
                raise SnapshotIntegrityError(
                    "Evidence replay_offset is not a main spool record boundary"
                ) from exc
            if exc.offset in fences and exc.offset != main_cursor:
                raise SnapshotIntegrityError(
                    "refreshable main_spool_fence is not a main spool record boundary"
                ) from exc
            raise SnapshotIntegrityError(
                "Evidence replay_offset or refreshable main_spool_fence is not a main spool record boundary"
            ) from exc
    else:
        if main_cursor != 0:
            raise SnapshotIntegrityError(
                "Evidence replay_offset must be zero when spool.jsonl is absent"
            )
        if any(fence != 0 for fence in fences):
            raise SnapshotIntegrityError(
                "refreshable main_spool_fence must be zero when spool.jsonl is absent"
            )
        if generic_state_counts is None:
            raise SnapshotIntegrityError("Evidence generic projection state was not validated")
        if generic_state_counts:
            detail = ", ".join(
                f"{table_name}={count}"
                for table_name, count in sorted(generic_state_counts.items())
            )
            raise SnapshotIntegrityError(
                "Evidence generic projection state is non-empty while "
                f"spool.jsonl is absent: {detail}"
            )
        main_count = 0
        main_size = 0
        main_invalid = 0
    return _PayloadValidation(
        canonical_store_uuid=store_uuid,
        canonical_schema_version=schema_version,
        sqlite_files_checked=len(sqlite_paths),
        main_spool_present=main_spool_present,
        main_spool_records=main_count,
        main_spool_bytes=main_size,
        main_spool_invalid_records=main_invalid,
        refreshable_spool_present=refreshable_spool_present,
        refreshable_spool_records=refresh_count,
        refreshable_spool_bytes=refresh_size,
        refreshable_spool_invalid_records=refresh_invalid,
        main_replay_offset=main_cursor,
        refreshable_replay_offset=refresh_cursor,
    )


def _snapshot_file_entries(
    payload: Path,
    source_inventory: _TreeInventory,
    copied_digests: Mapping[str, str],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for relative, source_identity in source_inventory.files:
        path = payload / PurePosixPath(relative)
        info = path.lstat()
        snapshot_identity = FileIdentity.from_stat(info)
        if not stat.S_ISREG(info.st_mode) or snapshot_identity.link_count != 1:
            raise SnapshotIntegrityError(f"snapshot file is not private and regular: {relative}")
        if snapshot_identity.mode != 0o600:
            raise SnapshotIntegrityError(f"snapshot file mode is not 0600: {relative}")
        digest = _hash_regular_file(path)
        if digest != copied_digests[relative]:
            raise SnapshotDriftError(f"snapshot bytes changed after copy: {relative}")
        entries.append(
            {
                "path": relative,
                "size_bytes": snapshot_identity.size_bytes,
                "sha256": digest,
                "source_stat": source_identity.manifest_dict(),
                "snapshot_stat": snapshot_identity.manifest_dict(),
            }
        )
    return entries


def _tree_digest(entries: Sequence[Mapping[str, Any]], directories: Sequence[str]) -> str:
    return canonical_digest({"directories": list(directories), "files": list(entries)})


def _receipt_with_hash(body: Mapping[str, Any]) -> dict[str, Any]:
    return {**body, "receipt_hash": canonical_digest(body)}


def _evidence_tree_receipt(path: Path) -> dict[str, object]:
    try:
        return fingerprint_evidence_tree(path).to_dict()
    except EvidenceActivationError as exc:
        raise SnapshotIntegrityError(f"Evidence tree fingerprint failed: {exc}") from exc


def _assert_evidence_tree_content_matches(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> None:
    content_keys = (
        "tree_sha256",
        "total_bytes",
        "file_count",
        "directory_count",
        "mode",
    )
    if any(expected.get(key) != actual.get(key) for key in content_keys):
        raise SnapshotIntegrityError(
            "snapshot Evidence subtree does not match the sealed live Evidence tree"
        )


def _validate_live_evidence_tree_receipt(
    value: Any,
    *,
    source_root: str,
) -> dict[str, Any]:
    expected_keys = {
        "path", "tree_sha256", "total_bytes", "file_count", "directory_count",
        "device", "inode", "mode", "mtime_ns", "ctime_ns", "entries",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise SnapshotIntegrityError("snapshot manifest live_evidence_tree fields are invalid")
    if value.get("path") != str(Path(source_root) / EVIDENCE_ROOT):
        raise SnapshotIntegrityError("snapshot live_evidence_tree path is not bound to source_root")
    digest = value.get("tree_sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise SnapshotIntegrityError("snapshot live_evidence_tree digest is invalid")
    integer_keys = {
        "total_bytes", "file_count", "directory_count", "device", "inode", "mode",
        "mtime_ns", "ctime_ns",
    }
    if any(
        isinstance(value.get(key), bool)
        or not isinstance(value.get(key), int)
        or int(value[key]) < 0
        for key in integer_keys
    ):
        raise SnapshotIntegrityError("snapshot live_evidence_tree counters are invalid")
    entries = value.get("entries")
    if not isinstance(entries, list):
        raise SnapshotIntegrityError("snapshot live_evidence_tree entries are invalid")
    expected_entry_keys = {
        "relative_path", "kind", "mode", "size_bytes", "device", "inode", "mtime_ns",
        "ctime_ns", "sha256",
    }
    seen: set[tuple[str, str]] = set()
    files = 0
    directories = 1
    total_bytes = 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != expected_entry_keys:
            raise SnapshotIntegrityError("snapshot live_evidence_tree entry fields are invalid")
        relative = entry.get("relative_path")
        kind = entry.get("kind")
        if not isinstance(relative, str) or _safe_relative_path(relative) != relative:
            raise SnapshotIntegrityError("snapshot live_evidence_tree entry path is invalid")
        if kind not in {"file", "directory"} or (relative, str(kind)) in seen:
            raise SnapshotIntegrityError("snapshot live_evidence_tree entry identity is invalid")
        seen.add((relative, str(kind)))
        for key in ("mode", "size_bytes", "device", "inode", "mtime_ns", "ctime_ns"):
            child = entry.get(key)
            if isinstance(child, bool) or not isinstance(child, int) or child < 0:
                raise SnapshotIntegrityError("snapshot live_evidence_tree entry stat is invalid")
        child_digest = entry.get("sha256")
        if kind == "file":
            if (
                not isinstance(child_digest, str)
                or len(child_digest) != 64
                or any(character not in "0123456789abcdef" for character in child_digest)
            ):
                raise SnapshotIntegrityError("snapshot live_evidence_tree file digest is invalid")
            files += 1
            total_bytes += int(entry["size_bytes"])
        else:
            if child_digest is not None:
                raise SnapshotIntegrityError("snapshot live_evidence_tree directory digest must be null")
            directories += 1
    if (
        files != value["file_count"]
        or directories != value["directory_count"]
        or total_bytes != value["total_bytes"]
    ):
        raise SnapshotIntegrityError("snapshot live_evidence_tree counts are inconsistent")
    return value


def create_evidence_snapshot(
    source_root: Path | str,
    snapshot_root: Path | str,
    *,
    confirm_writers_stopped: bool,
    deployed_commit: str,
) -> SnapshotCreationResult:
    """Copy a stopped agentacct store into a new owner-only snapshot.

    ``confirm_writers_stopped`` is mandatory, but creation still fails if any
    discovered lock is held or if the tree changes before, during, or after
    copying.
    """

    if confirm_writers_stopped is not True:
        raise SnapshotSafetyError("confirm_writers_stopped=True is required")
    if (
        not isinstance(deployed_commit, str)
        or len(deployed_commit) != 40
        or any(character not in "0123456789abcdef" for character in deployed_commit)
    ):
        raise SnapshotSafetyError("deployed_commit must be a full lowercase 40-hex commit")
    source = _validate_source(source_root)
    target = _validate_new_target(snapshot_root, source_root=source, label="snapshot target")
    initial = _scan_tree(source)
    lock_paths = _expected_lock_paths(source, initial)
    stage = target.parent / f".{target.name}.snapshot-stage-{uuid.uuid4().hex}"
    stage_created = False
    try:
        with _hold_writer_locks(lock_paths):
            locked_inventory = _scan_tree(source)
            if locked_inventory != initial:
                raise SnapshotDriftError("source tree changed while writer locks were acquired")
            live_evidence_tree = _evidence_tree_receipt(source / EVIDENCE_ROOT)
            stage.mkdir(mode=0o700)
            stage_created = True
            os.chmod(stage, 0o700)
            payload = stage / PAYLOAD_DIRNAME
            copied_digests = _copy_inventory(source, payload, locked_inventory)
            if _scan_tree(source) != locked_inventory:
                raise SnapshotDriftError("source tree changed while snapshot was copied")

            validation = _validate_payload(payload)
            snapshot_evidence_tree = _evidence_tree_receipt(payload / EVIDENCE_ROOT)
            _assert_evidence_tree_content_matches(live_evidence_tree, snapshot_evidence_tree)
            entries = _snapshot_file_entries(payload, locked_inventory, copied_digests)
            tree_digest = _tree_digest(entries, locked_inventory.directories)
            created_at = _utc_now()
            manifest = {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "created_at": created_at,
                "deployed_commit": deployed_commit,
                "source_root": str(source),
                "snapshot_root": str(target),
                "directories": list(locked_inventory.directories),
                "files": entries,
                "tree_digest": tree_digest,
                "canonical_store": {
                    "path": "chronicle.sqlite3",
                    "store_uuid": validation.canonical_store_uuid,
                    "schema_version": validation.canonical_schema_version,
                },
                "live_evidence_tree": live_evidence_tree,
            }
            manifest_raw = _write_private_json(stage / MANIFEST_FILENAME, manifest)
            manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
            receipt_body = {
                "schema_version": SNAPSHOT_RECEIPT_SCHEMA_VERSION,
                "created_at": created_at,
                "deployed_commit": deployed_commit,
                "source_root": str(source),
                "snapshot_root": str(target),
                "manifest_sha256": manifest_sha256,
                "tree_digest": tree_digest,
                "file_count": len(entries),
                "total_bytes": sum(int(item["size_bytes"]) for item in entries),
                "canonical_store_uuid": validation.canonical_store_uuid,
                "canonical_schema_version": validation.canonical_schema_version,
                "sqlite_files_checked": validation.sqlite_files_checked,
                "main_spool_present": validation.main_spool_present,
                "main_spool_records": validation.main_spool_records,
                "main_spool_bytes": validation.main_spool_bytes,
                "main_spool_invalid_records": validation.main_spool_invalid_records,
                "refreshable_spool_present": validation.refreshable_spool_present,
                "refreshable_spool_records": validation.refreshable_spool_records,
                "refreshable_spool_bytes": validation.refreshable_spool_bytes,
                "refreshable_spool_invalid_records": validation.refreshable_spool_invalid_records,
                "main_replay_offset": validation.main_replay_offset,
                "refreshable_replay_offset": validation.refreshable_replay_offset,
                "writer_lock_count": len(lock_paths),
                "live_evidence_tree": live_evidence_tree,
            }
            _write_private_json(stage / RECEIPT_FILENAME, _receipt_with_hash(receipt_body))
            _fsync_tree(stage)
            _publish_new_directory(stage, target)
            stage_created = False
            _fsync_directory(target.parent)
        verification = verify_evidence_snapshot(target)
        return SnapshotCreationResult(
            snapshot_root=target,
            manifest_path=target / MANIFEST_FILENAME,
            receipt_path=target / RECEIPT_FILENAME,
            verification=verification,
        )
    except Exception:
        if stage_created and stage.exists():
            shutil.rmtree(stage)
            _fsync_directory(stage.parent)
        raise


def _manifest_entry_identity(value: Any, *, label: str) -> FileIdentity:
    if not isinstance(value, dict):
        raise SnapshotIntegrityError(f"{label} must be an object")
    expected_keys = {
        "mode", "uid", "gid", "mtime_ns", "ctime_ns", "inode", "device", "size_bytes", "link_count"
    }
    if set(value) != expected_keys or any(
        isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] < 0
        for key in expected_keys
    ):
        raise SnapshotIntegrityError(f"{label} has invalid stat fields")
    return FileIdentity(
        mode=value["mode"],
        uid=value["uid"],
        gid=value["gid"],
        mtime_ns=value["mtime_ns"],
        ctime_ns=value["ctime_ns"],
        inode=value["inode"],
        device=value["device"],
        size_bytes=value["size_bytes"],
        link_count=value["link_count"],
    )


def _load_snapshot_metadata(
    root: Path,
) -> tuple[dict[str, Any], bytes, dict[str, Any], bytes]:
    manifest_path = root / MANIFEST_FILENAME
    receipt_path = root / RECEIPT_FILENAME
    for path in (manifest_path, receipt_path):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SnapshotIntegrityError(f"snapshot metadata is not a private regular file: {path.name}")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise SnapshotIntegrityError(f"snapshot metadata mode is not 0600: {path.name}")
    manifest_raw = manifest_path.read_bytes()
    receipt_raw = receipt_path.read_bytes()
    manifest = _load_strict_json(manifest_raw, label="snapshot manifest")
    receipt = _load_strict_json(receipt_raw, label="snapshot receipt")
    if not isinstance(manifest, dict) or not isinstance(receipt, dict):
        raise SnapshotIntegrityError("snapshot manifest and receipt must be objects")
    return manifest, manifest_raw, receipt, receipt_raw


def _verify_manifest_shape(manifest: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    expected = {
        "schema_version", "created_at", "deployed_commit", "source_root", "snapshot_root",
        "directories", "files", "tree_digest", "canonical_store", "live_evidence_tree"
    }
    if set(manifest) != expected or manifest.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise SnapshotIntegrityError("snapshot manifest schema or fields are invalid")
    deployed_commit = manifest.get("deployed_commit")
    if (
        not isinstance(deployed_commit, str)
        or len(deployed_commit) != 40
        or any(character not in "0123456789abcdef" for character in deployed_commit)
    ):
        raise SnapshotIntegrityError("snapshot manifest deployed_commit is invalid")
    try:
        normalize_timestamp(manifest.get("created_at"), "created_at")
    except (TypeError, ValueError) as exc:
        raise SnapshotIntegrityError("snapshot manifest capture UTC is invalid") from exc
    for key in ("source_root", "snapshot_root"):
        value = manifest.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise SnapshotIntegrityError(f"snapshot manifest {key} is not absolute")
    _validate_live_evidence_tree_receipt(
        manifest.get("live_evidence_tree"),
        source_root=str(manifest["source_root"]),
    )
    directories = manifest.get("directories")
    files = manifest.get("files")
    if not isinstance(directories, list) or not isinstance(files, list) or not files:
        raise SnapshotIntegrityError("snapshot manifest inventory is invalid")
    if any(not isinstance(item, str) or not item or item.startswith("/") or ".." in PurePosixPath(item).parts for item in directories):
        raise SnapshotIntegrityError("snapshot manifest directory path is invalid")
    if directories != sorted(set(directories)):
        raise SnapshotIntegrityError("snapshot manifest directories are duplicated or unsorted")
    parsed_files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(files):
        if not isinstance(item, dict) or set(item) != {
            "path", "size_bytes", "sha256", "source_stat", "snapshot_stat"
        }:
            raise SnapshotIntegrityError(f"snapshot manifest files[{index}] is invalid")
        relative = item.get("path")
        if (
            not isinstance(relative, str)
            or not relative
            or relative.startswith("/")
            or ".." in PurePosixPath(relative).parts
            or relative in seen
        ):
            raise SnapshotIntegrityError(f"snapshot manifest files[{index}].path is invalid")
        seen.add(relative)
        digest = item.get("sha256")
        size = item.get("size_bytes")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            raise SnapshotIntegrityError(f"snapshot manifest files[{index}] content fields are invalid")
        _manifest_entry_identity(item.get("source_stat"), label=f"files[{index}].source_stat")
        snapshot_identity = _manifest_entry_identity(
            item.get("snapshot_stat"), label=f"files[{index}].snapshot_stat"
        )
        if snapshot_identity.size_bytes != size:
            raise SnapshotIntegrityError(f"snapshot manifest files[{index}] size fields disagree")
        parsed_files.append(item)
    if [item["path"] for item in parsed_files] != sorted(seen):
        raise SnapshotIntegrityError("snapshot manifest files are unsorted")
    return directories, parsed_files


def verify_evidence_snapshot(snapshot_root: Path | str) -> SnapshotVerification:
    """Verify a snapshot without modifying its bytes or projection."""

    root = _assert_absolute_no_symlink(Path(snapshot_root), label="snapshot root", must_exist=True)
    try:
        reject_live_state_overlap(root, label="snapshot root")
    except LivePathSafetyError as exc:
        raise SnapshotSafetyError(str(exc)) from exc
    root_info = root.lstat()
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_IMODE(root_info.st_mode) != 0o700:
        raise SnapshotIntegrityError("snapshot root must be an owner-only 0700 directory")
    root_children = {item.name for item in os.scandir(root)}
    if root_children != {PAYLOAD_DIRNAME, MANIFEST_FILENAME, RECEIPT_FILENAME}:
        raise SnapshotIntegrityError("snapshot root inventory is not exact")
    payload = root / PAYLOAD_DIRNAME
    if not payload.is_dir() or stat.S_IMODE(payload.lstat().st_mode) != 0o700:
        raise SnapshotIntegrityError("snapshot payload must be an owner-only 0700 directory")

    before = _scan_tree(payload)
    manifest_path = root / MANIFEST_FILENAME
    receipt_path = root / RECEIPT_FILENAME
    metadata_before = {
        MANIFEST_FILENAME: FileIdentity.from_stat(manifest_path.lstat()),
        RECEIPT_FILENAME: FileIdentity.from_stat(receipt_path.lstat()),
    }
    manifest, manifest_raw, receipt, receipt_raw = _load_snapshot_metadata(root)
    if metadata_before != {
        MANIFEST_FILENAME: FileIdentity.from_stat(manifest_path.lstat()),
        RECEIPT_FILENAME: FileIdentity.from_stat(receipt_path.lstat()),
    }:
        raise SnapshotDriftError("snapshot metadata changed while it was read")
    directories, files = _verify_manifest_shape(manifest)
    if tuple(directories) != before.directories or tuple(item["path"] for item in files) != tuple(
        path for path, _ in before.files
    ):
        raise SnapshotIntegrityError("snapshot payload inventory does not match the manifest")
    for directory in (payload, *(payload / PurePosixPath(item) for item in directories)):
        if stat.S_IMODE(directory.lstat().st_mode) != 0o700:
            raise SnapshotIntegrityError(f"snapshot directory mode is not 0700: {directory}")

    for item, (relative, actual_identity) in zip(files, before.files, strict=True):
        expected_identity = _manifest_entry_identity(
            item["snapshot_stat"], label=f"{relative}.snapshot_stat"
        )
        if actual_identity != expected_identity:
            raise SnapshotIntegrityError(f"snapshot file identity mismatch: {relative}")
        if actual_identity.mode != 0o600 or actual_identity.link_count != 1:
            raise SnapshotIntegrityError(f"snapshot file is not owner-only and private: {relative}")
        if _hash_regular_file(payload / PurePosixPath(relative)) != item["sha256"]:
            raise SnapshotIntegrityError(f"snapshot SHA-256 mismatch: {relative}")

    tree_digest = _tree_digest(files, directories)
    if manifest.get("tree_digest") != tree_digest:
        raise SnapshotIntegrityError("snapshot tree digest mismatch")
    manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
    receipt_hash = receipt.get("receipt_hash")
    if receipt_hash != canonical_digest({key: value for key, value in receipt.items() if key != "receipt_hash"}):
        raise SnapshotIntegrityError("snapshot receipt hash mismatch")
    if receipt.get("schema_version") != SNAPSHOT_RECEIPT_SCHEMA_VERSION:
        raise SnapshotIntegrityError("snapshot receipt schema is invalid")
    if receipt.get("manifest_sha256") != manifest_sha256 or receipt.get("tree_digest") != tree_digest:
        raise SnapshotIntegrityError("snapshot receipt does not bind the manifest")
    if (
        manifest.get("snapshot_root") != str(root)
        or receipt.get("snapshot_root") != str(root)
        or receipt.get("source_root") != manifest.get("source_root")
        or receipt.get("deployed_commit") != manifest.get("deployed_commit")
        or receipt.get("created_at") != manifest.get("created_at")
        or receipt.get("live_evidence_tree") != manifest.get("live_evidence_tree")
    ):
        raise SnapshotIntegrityError("snapshot paths, commit, or capture UTC do not agree")

    validation = _validate_payload(payload)
    live_evidence_tree = manifest.get("live_evidence_tree")
    if not isinstance(live_evidence_tree, dict):
        raise SnapshotIntegrityError("snapshot manifest live_evidence_tree is invalid")
    snapshot_evidence_tree = _evidence_tree_receipt(payload / EVIDENCE_ROOT)
    _assert_evidence_tree_content_matches(live_evidence_tree, snapshot_evidence_tree)
    canonical_manifest = manifest.get("canonical_store")
    if canonical_manifest != {
        "path": "chronicle.sqlite3",
        "store_uuid": validation.canonical_store_uuid,
        "schema_version": validation.canonical_schema_version,
    }:
        raise SnapshotIntegrityError("canonical metadata does not match the manifest")
    expected_receipt_values = {
        "canonical_store_uuid": validation.canonical_store_uuid,
        "canonical_schema_version": validation.canonical_schema_version,
        "sqlite_files_checked": validation.sqlite_files_checked,
        "main_spool_records": validation.main_spool_records,
        "refreshable_spool_records": validation.refreshable_spool_records,
        "main_replay_offset": validation.main_replay_offset,
        "refreshable_replay_offset": validation.refreshable_replay_offset,
        "file_count": len(files),
        "total_bytes": sum(int(item["size_bytes"]) for item in files),
    }
    extended_receipt_values = {
        "main_spool_bytes": validation.main_spool_bytes,
        "refreshable_spool_present": validation.refreshable_spool_present,
        "refreshable_spool_bytes": validation.refreshable_spool_bytes,
    }
    extended_fields = set(extended_receipt_values)
    present_extended_fields = extended_fields.intersection(receipt)
    if present_extended_fields and present_extended_fields != extended_fields:
        raise SnapshotIntegrityError("snapshot receipt spool presence fields are incomplete")
    if present_extended_fields:
        if (
            not isinstance(receipt.get("refreshable_spool_present"), bool)
            or any(
                isinstance(receipt.get(key), bool)
                or not isinstance(receipt.get(key), int)
                or int(receipt[key]) < 0
                for key in ("main_spool_bytes", "refreshable_spool_bytes")
            )
        ):
            raise SnapshotIntegrityError("snapshot receipt spool presence fields are invalid")
        expected_receipt_values.update(extended_receipt_values)
    # Second extended group (added with preserved-damage tolerance and the
    # optional main spool): verified when present so pre-existing receipts
    # keep verifying, exactly like the first extended group above.
    damage_receipt_values = {
        "main_spool_present": validation.main_spool_present,
        "main_spool_invalid_records": validation.main_spool_invalid_records,
        "refreshable_spool_invalid_records": validation.refreshable_spool_invalid_records,
    }
    damage_fields = set(damage_receipt_values)
    present_damage_fields = damage_fields.intersection(receipt)
    if present_damage_fields and present_damage_fields != damage_fields:
        raise SnapshotIntegrityError("snapshot receipt spool damage fields are incomplete")
    if present_damage_fields:
        if (
            not isinstance(receipt.get("main_spool_present"), bool)
            or any(
                isinstance(receipt.get(key), bool)
                or not isinstance(receipt.get(key), int)
                or int(receipt[key]) < 0
                for key in (
                    "main_spool_invalid_records",
                    "refreshable_spool_invalid_records",
                )
            )
        ):
            raise SnapshotIntegrityError("snapshot receipt spool damage fields are invalid")
        expected_receipt_values.update(damage_receipt_values)
    if any(receipt.get(key) != value for key, value in expected_receipt_values.items()):
        raise SnapshotIntegrityError("snapshot receipt validation counts do not match")
    after = _scan_tree(payload)
    if after != before:
        raise SnapshotDriftError("snapshot payload changed during read-only verification")
    if metadata_before != {
        MANIFEST_FILENAME: FileIdentity.from_stat(manifest_path.lstat()),
        RECEIPT_FILENAME: FileIdentity.from_stat(receipt_path.lstat()),
    }:
        raise SnapshotDriftError("snapshot metadata changed during read-only verification")
    if (
        _hash_regular_file(manifest_path) != hashlib.sha256(manifest_raw).hexdigest()
        or _hash_regular_file(receipt_path) != hashlib.sha256(receipt_raw).hexdigest()
    ):
        raise SnapshotDriftError("snapshot metadata bytes changed during read-only verification")
    return SnapshotVerification(
        snapshot_root=root,
        manifest_sha256=manifest_sha256,
        tree_digest=tree_digest,
        file_count=len(files),
        total_bytes=sum(int(item["size_bytes"]) for item in files),
        canonical_store_uuid=validation.canonical_store_uuid,
        canonical_schema_version=validation.canonical_schema_version,
        sqlite_files_checked=validation.sqlite_files_checked,
        main_spool_present=validation.main_spool_present,
        main_spool_records=validation.main_spool_records,
        main_spool_bytes=validation.main_spool_bytes,
        main_spool_invalid_records=validation.main_spool_invalid_records,
        refreshable_spool_present=validation.refreshable_spool_present,
        refreshable_spool_records=validation.refreshable_spool_records,
        refreshable_spool_bytes=validation.refreshable_spool_bytes,
        refreshable_spool_invalid_records=validation.refreshable_spool_invalid_records,
        main_replay_offset=validation.main_replay_offset,
        refreshable_replay_offset=validation.refreshable_replay_offset,
    )


def rehearse_evidence_snapshot_restore(
    snapshot_root: Path | str,
    restore_root: Path | str,
) -> RestoreRehearsalResult:
    """Copy a verified snapshot to a new private root and validate the copy."""

    verification = verify_evidence_snapshot(snapshot_root)
    snapshot = Path(snapshot_root).expanduser().resolve(strict=True)
    target = _validate_new_target(restore_root, source_root=snapshot, label="restore rehearsal target")
    manifest = _load_strict_json((snapshot / MANIFEST_FILENAME).read_bytes(), label="snapshot manifest")
    assert isinstance(manifest, dict)  # proved by verify_evidence_snapshot
    directories, files = _verify_manifest_shape(manifest)
    payload_source = snapshot / PAYLOAD_DIRNAME
    source_inventory = _scan_tree(payload_source)
    stage = target.parent / f".{target.name}.restore-stage-{uuid.uuid4().hex}"
    stage_created = False
    try:
        stage.mkdir(mode=0o700)
        stage_created = True
        os.chmod(stage, 0o700)
        payload_target = stage / PAYLOAD_DIRNAME
        copied = _copy_inventory(payload_source, payload_target, source_inventory)
        if _scan_tree(payload_source) != source_inventory:
            raise SnapshotDriftError("snapshot changed during restore rehearsal copy")
        for item in files:
            if copied[item["path"]] != item["sha256"]:
                raise SnapshotIntegrityError(f"restore rehearsal copy mismatch: {item['path']}")
        restored = _validate_payload(payload_target)
        if (
            restored.canonical_store_uuid != verification.canonical_store_uuid
            or restored.canonical_schema_version != verification.canonical_schema_version
        ):
            raise SnapshotIntegrityError("restore rehearsal canonical metadata changed")
        live_evidence_tree = manifest.get("live_evidence_tree")
        assert isinstance(live_evidence_tree, dict)  # proved by verify_evidence_snapshot
        restored_evidence_tree = _evidence_tree_receipt(payload_target / EVIDENCE_ROOT)
        _assert_evidence_tree_content_matches(live_evidence_tree, restored_evidence_tree)
        receipt_body = {
            "schema_version": RESTORE_RECEIPT_SCHEMA_VERSION,
            "created_at": _utc_now(),
            "snapshot_root": str(snapshot),
            "restore_root": str(target),
            "source_manifest_sha256": verification.manifest_sha256,
            "tree_digest": verification.tree_digest,
            "file_count": verification.file_count,
            "total_bytes": verification.total_bytes,
            "canonical_store_uuid": restored.canonical_store_uuid,
            "canonical_schema_version": restored.canonical_schema_version,
            "sqlite_files_checked": restored.sqlite_files_checked,
            "main_spool_present": restored.main_spool_present,
            "main_spool_records": restored.main_spool_records,
            "main_spool_bytes": restored.main_spool_bytes,
            "main_spool_invalid_records": restored.main_spool_invalid_records,
            "refreshable_spool_present": restored.refreshable_spool_present,
            "refreshable_spool_records": restored.refreshable_spool_records,
            "refreshable_spool_bytes": restored.refreshable_spool_bytes,
            "refreshable_spool_invalid_records": restored.refreshable_spool_invalid_records,
            "main_replay_offset": restored.main_replay_offset,
            "refreshable_replay_offset": restored.refreshable_replay_offset,
            "restore_steps": list(_RESTORE_STEPS),
        }
        _write_private_json(stage / RESTORE_RECEIPT_FILENAME, _receipt_with_hash(receipt_body))
        _fsync_tree(stage)
        _publish_new_directory(stage, target)
        stage_created = False
        _fsync_directory(target.parent)
        return RestoreRehearsalResult(
            restore_root=target,
            receipt_path=target / RESTORE_RECEIPT_FILENAME,
            source_manifest_sha256=verification.manifest_sha256,
            tree_digest=verification.tree_digest,
            file_count=verification.file_count,
            total_bytes=verification.total_bytes,
            canonical_store_uuid=restored.canonical_store_uuid,
            canonical_schema_version=restored.canonical_schema_version,
        )
    except Exception:
        if stage_created and stage.exists():
            shutil.rmtree(stage)
            _fsync_directory(stage.parent)
        raise


@contextmanager
def open_snapshot_file(
    snapshot_root: Path | str,
    relative_path: str | PurePosixPath,
) -> Iterator[BinaryIO]:
    """Verify a snapshot and open one manifest-declared payload file read-only."""

    verified = VerifiedEvidenceSnapshot.verify(snapshot_root)
    with verified.open_binary(relative_path) as handle:
        yield handle


# Short aliases for CLI integration without importing implementation details.
create_snapshot = create_evidence_snapshot
verify_snapshot = verify_evidence_snapshot
rehearse_restore = rehearse_evidence_snapshot_restore


__all__ = [
    "EvidenceSnapshotError",
    "RestoreRehearsalResult",
    "SnapshotCreationResult",
    "SnapshotDriftError",
    "SnapshotIntegrityError",
    "SnapshotSafetyError",
    "SnapshotVerification",
    "VerifiedEvidenceSnapshot",
    "create_evidence_snapshot",
    "create_snapshot",
    "rehearse_evidence_snapshot_restore",
    "rehearse_restore",
    "open_snapshot_file",
    "verify_evidence_snapshot",
    "verify_snapshot",
]
