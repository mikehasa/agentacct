"""Owner-gated atomic activation and rollback for rebuilt Evidence v2 trees.

The Evidence store is a directory (two spools, a rebuildable SQLite
projection, and its advisory lock), so replacing individual files can expose a
mixed generation.  This module promotes the whole directory with macOS
``renamex_np(RENAME_SWAP)`` and deliberately has no production two-rename
fallback.  Tests on other platforms may inject an exchange primitive.

Every mutating operation requires an explicit stopped-writer acknowledgement,
then independently proves both Evidence locks and every store-root writer lock
are exclusively available.  A private, fsynced intent receipt is durable
before the exchange.  Its upstream snapshot/candidate bindings, path, inode,
per-file receipt, and digest bindings make a crash after exchange classifiable
even when the final receipt was never published.

No operation deletes either store generation.  Activation moves the displaced
live tree to one unique archive.  Rollback atomically exchanges that archive
with the current live tree and moves the failed/current generation (including
any observation-window writes) to another unique archive.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import stat
import sys
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence


ACTIVATION_INTENT_SCHEMA_VERSION = "agent-chronicle.evidence-activation-intent.v1"
ACTIVATION_RECEIPT_SCHEMA_VERSION = "agent-chronicle.evidence-activation.v1"
ROLLBACK_INTENT_SCHEMA_VERSION = "agent-chronicle.evidence-rollback-intent.v1"
ROLLBACK_RECEIPT_SCHEMA_VERSION = "agent-chronicle.evidence-rollback.v1"

EVIDENCE_LOCK_FILENAME = ".spool.lock"
STORE_WRITER_LOCK_RELATIVE_PATHS = (
    Path("events.jsonl.lock"),
    Path("ingestion-health/.state.lock"),
    Path("runtime/.runtime.lock"),
)
RENAME_SWAP = 0x00000002
RENAME_EXCL = 0x00000004

_COPY_CHUNK_SIZE = 1024 * 1024
_RECEIPT_SIZE_LIMIT = 8 * 1024 * 1024

SwapFunction = Callable[[Path, Path], None]
MoveFunction = Callable[[Path, Path], None]
FsyncFunction = Callable[[int], None]
FaultInjector = Callable[[str], None]


class EvidenceActivationError(RuntimeError):
    """Base class for a refused or failed Evidence activation operation."""


class WritersStoppedConfirmationRequired(EvidenceActivationError):
    """The caller did not explicitly acknowledge the stopped-writer gate."""


class EvidenceActivationLockBusy(EvidenceActivationError):
    """At least one store lock is held by another process."""


class EvidenceActivationDrift(EvidenceActivationError):
    """A tree no longer matches the receipt that authorized the operation."""


class EvidenceActivationReceiptError(EvidenceActivationError):
    """A receipt is unsafe, malformed, non-private, or would be overwritten."""


class EvidenceActivationPostSwapError(EvidenceActivationError):
    """An activation failed after the exchange crossed or may have crossed."""

    def __init__(
        self,
        intent: "ActivationIntent",
        cause: BaseException,
        *,
        state: str,
        classification_error: BaseException | None = None,
    ) -> None:
        message = (
            f"evidence activation state is {state}; later work failed: "
            f"{type(cause).__name__}: {cause}"
        )
        if classification_error is not None:
            message += (
                "; state classification also failed: "
                f"{type(classification_error).__name__}: {classification_error}"
            )
        super().__init__(message)
        self.intent = intent
        self.cause = cause
        self.state = state
        self.classification_error = classification_error


class EvidenceRollbackPostSwapError(EvidenceActivationError):
    """A rollback failed after its exchange crossed or may have crossed."""

    def __init__(
        self,
        intent: "RollbackIntent",
        cause: BaseException,
        *,
        state: str,
        classification_error: BaseException | None = None,
    ) -> None:
        message = (
            f"evidence rollback state is {state}; later work failed: "
            f"{type(cause).__name__}: {cause}"
        )
        if classification_error is not None:
            message += (
                "; state classification also failed: "
                f"{type(classification_error).__name__}: {classification_error}"
            )
        super().__init__(message)
        self.intent = intent
        self.cause = cause
        self.state = state
        self.classification_error = classification_error


@dataclass
class _DurableIntentGuard:
    """Keep one parsed intent bound to its durable file through mutation."""

    path: Path
    expected: ActivationIntent | RollbackIntent
    descriptor: int
    stat_signature: tuple[int, int, int, int, int]
    expected_bytes: bytes
    loader: Callable[[Path | str], ActivationIntent | RollbackIntent]

    def recheck(self) -> None:
        """Fail if the durable pathname, inode, bytes, or parsed intent changed."""

        try:
            pathname = _require_private_file(self.path, label="operation intent receipt")
        except (FileNotFoundError, EvidenceActivationError) as exc:
            raise EvidenceActivationDrift(
                "operation intent receipt disappeared or became unsafe before exchange or recovery mutation"
            ) from exc
        descriptor = os.fstat(self.descriptor)
        pathname_signature = _stat_signature(pathname)
        descriptor_signature = _stat_signature(descriptor)
        if (
            pathname_signature != self.stat_signature
            or descriptor_signature != self.stat_signature
        ):
            raise EvidenceActivationDrift(
                "operation intent receipt changed during crash recovery"
            )
        if _read_descriptor_bytes(self.descriptor) != self.expected_bytes:
            raise EvidenceActivationDrift(
                "operation intent receipt bytes changed during crash recovery"
            )
        if self.loader(self.path) != self.expected:
            raise EvidenceActivationDrift(
                "operation intent receipt content changed during crash recovery"
            )
        # The loader performs a second pathname-based read.  Rebind the name to
        # the held descriptor again after it returns so an unlink/replace at the
        # end of that read cannot slip across the irreversible boundary.
        try:
            final_pathname = _require_private_file(
                self.path,
                label="operation intent receipt",
            )
        except (FileNotFoundError, EvidenceActivationError) as exc:
            raise EvidenceActivationDrift(
                "operation intent receipt disappeared or became unsafe at the mutation boundary"
            ) from exc
        final_descriptor = os.fstat(self.descriptor)
        if (
            _stat_signature(final_pathname) != self.stat_signature
            or _stat_signature(final_descriptor) != self.stat_signature
        ):
            raise EvidenceActivationDrift(
                "operation intent receipt changed at the mutation boundary"
            )


@dataclass(frozen=True)
class TreeEntryReceipt:
    relative_path: str
    kind: str
    mode: int
    size_bytes: int
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int
    sha256: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "kind": self.kind,
            "mode": self.mode,
            "size_bytes": self.size_bytes,
            "device": self.device,
            "inode": self.inode,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class EvidenceTreeReceipt:
    path: Path
    tree_sha256: str
    total_bytes: int
    file_count: int
    directory_count: int
    device: int
    inode: int
    mode: int
    mtime_ns: int
    ctime_ns: int
    entries: tuple[TreeEntryReceipt, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "tree_sha256": self.tree_sha256,
            "total_bytes": self.total_bytes,
            "file_count": self.file_count,
            "directory_count": self.directory_count,
            "device": self.device,
            "inode": self.inode,
            "mode": self.mode,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
            "entries": [entry.to_dict() for entry in self.entries],
        }


@dataclass(frozen=True)
class ActivationIntent:
    schema_version: str
    operation_id: str
    prepared_at_ns: int
    live: EvidenceTreeReceipt
    candidate: EvidenceTreeReceipt
    archive_path: Path
    expected_live_tree_sha256: str | None
    expected_live_total_bytes: int | None
    expected_candidate_tree_sha256: str | None
    expected_candidate_total_bytes: int | None
    candidate_receipt_sha256: str | None
    snapshot_manifest_sha256: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "prepared_at_ns": self.prepared_at_ns,
            "live": self.live.to_dict(),
            "candidate": self.candidate.to_dict(),
            "archive_path": str(self.archive_path),
            "expected_live_tree_sha256": self.expected_live_tree_sha256,
            "expected_live_total_bytes": self.expected_live_total_bytes,
            "expected_candidate_tree_sha256": self.expected_candidate_tree_sha256,
            "expected_candidate_total_bytes": self.expected_candidate_total_bytes,
            "candidate_receipt_sha256": self.candidate_receipt_sha256,
            "snapshot_manifest_sha256": self.snapshot_manifest_sha256,
        }


@dataclass(frozen=True)
class ActivationReceipt:
    schema_version: str
    activated_at_ns: int
    intent: ActivationIntent
    installed_live: EvidenceTreeReceipt
    archived_previous_live: EvidenceTreeReceipt

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "activated_at_ns": self.activated_at_ns,
            "intent": self.intent.to_dict(),
            "installed_live": self.installed_live.to_dict(),
            "archived_previous_live": self.archived_previous_live.to_dict(),
        }


@dataclass(frozen=True)
class ActivationVerification:
    ok: bool
    state: str
    checks: tuple[str, ...]
    errors: tuple[str, ...]
    observed_live_sha256: str | None = None
    observed_archive_sha256: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "state": self.state,
            "checks": list(self.checks),
            "errors": list(self.errors),
            "observed_live_sha256": self.observed_live_sha256,
            "observed_archive_sha256": self.observed_archive_sha256,
        }


@dataclass(frozen=True)
class RollbackIntent:
    schema_version: str
    rollback_id: str
    prepared_at_ns: int
    activation_operation_id: str
    live_path: Path
    source_archive_path: Path
    failed_archive_path: Path
    current_live: EvidenceTreeReceipt
    archived_previous_live: EvidenceTreeReceipt

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "rollback_id": self.rollback_id,
            "prepared_at_ns": self.prepared_at_ns,
            "activation_operation_id": self.activation_operation_id,
            "live_path": str(self.live_path),
            "source_archive_path": str(self.source_archive_path),
            "failed_archive_path": str(self.failed_archive_path),
            "current_live": self.current_live.to_dict(),
            "archived_previous_live": self.archived_previous_live.to_dict(),
        }


@dataclass(frozen=True)
class RollbackReceipt:
    schema_version: str
    rolled_back_at_ns: int
    intent: RollbackIntent
    restored_live: EvidenceTreeReceipt
    failed_live_archive: EvidenceTreeReceipt

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "rolled_back_at_ns": self.rolled_back_at_ns,
            "intent": self.intent.to_dict(),
            "restored_live": self.restored_live.to_dict(),
            "failed_live_archive": self.failed_live_archive.to_dict(),
        }


def _confirm_writers_stopped(value: bool) -> None:
    if value is not True:
        raise WritersStoppedConfirmationRequired(
            "confirm_writers_stopped=True is required after stopping every Evidence writer"
        )


def _fault(injector: FaultInjector | None, phase: str) -> None:
    if injector is not None:
        injector(phase)


def _mode(observed: os.stat_result) -> int:
    return stat.S_IMODE(observed.st_mode)


def _identity(observed: os.stat_result) -> tuple[int, int]:
    return int(observed.st_dev), int(observed.st_ino)


def _stat_signature(observed: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(observed.st_dev),
        int(observed.st_ino),
        int(observed.st_size),
        int(observed.st_mtime_ns),
        int(observed.st_ctime_ns),
    )


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _absolute_path(value: Path | str, *, label: str, must_exist: bool) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise EvidenceActivationError(f"{label} must be an absolute path")
    if ".." in path.parts:
        raise EvidenceActivationError(f"{label} must be normalized")
    if must_exist:
        try:
            resolved = path.resolve(strict=True)
        except (FileNotFoundError, RuntimeError) as exc:
            raise EvidenceActivationError(f"{label} does not exist") from exc
        if resolved != path:
            raise EvidenceActivationError(f"{label} may not contain symlinks")
        return path
    parent = path.parent
    try:
        resolved_parent = parent.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise EvidenceActivationError(f"{label} parent does not exist") from exc
    if resolved_parent != parent:
        raise EvidenceActivationError(f"{label} parent may not contain symlinks")
    return path


def _require_private_directory(path: Path, *, label: str) -> os.stat_result:
    observed = path.lstat()
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.geteuid()
        or (_mode(observed) & 0o077) != 0
    ):
        raise EvidenceActivationError(
            f"{label} must be an owner-private directory"
        )
    return observed


def _require_owner_directory_0700(path: Path, *, label: str) -> os.stat_result:
    """Require an existing, non-symlink directory owned by this user."""

    try:
        observed = path.lstat()
    except FileNotFoundError as exc:
        raise EvidenceActivationError(f"{label} does not exist") from exc
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.geteuid()
        or _mode(observed) != 0o700
    ):
        raise EvidenceActivationError(
            f"{label} must be an owner-only directory with mode 0700"
        )
    return observed


def _require_private_file(path: Path, *, label: str) -> os.stat_result:
    observed = path.lstat()
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or observed.st_uid != os.geteuid()
        or _mode(observed) != 0o600
    ):
        raise EvidenceActivationError(
            f"{label} must be an owner-only unique regular file"
        )
    return observed


def _entry_receipt(path: Path, root: Path) -> TreeEntryReceipt:
    observed = path.lstat()
    relative = path.relative_to(root).as_posix()
    if stat.S_ISLNK(observed.st_mode):
        raise EvidenceActivationError("Evidence trees may not contain symlinks")
    if stat.S_ISDIR(observed.st_mode):
        if observed.st_uid != os.geteuid() or (_mode(observed) & 0o077) != 0:
            raise EvidenceActivationError(
                f"Evidence directory is not owner-private: {relative}"
            )
        return TreeEntryReceipt(
            relative_path=relative,
            kind="directory",
            mode=_mode(observed),
            size_bytes=0,
            device=int(observed.st_dev),
            inode=int(observed.st_ino),
            mtime_ns=int(observed.st_mtime_ns),
            ctime_ns=int(observed.st_ctime_ns),
        )
    if not stat.S_ISREG(observed.st_mode):
        raise EvidenceActivationError(
            f"Evidence tree contains a non-regular entry: {relative}"
        )
    if (
        observed.st_uid != os.geteuid()
        or observed.st_nlink != 1
        or (_mode(observed) & 0o077) != 0
    ):
        raise EvidenceActivationError(
            f"Evidence file is not owner-private and unique: {relative}"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(observed):
            raise EvidenceActivationDrift(
                f"Evidence file changed before hashing: {relative}"
            )
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, _COPY_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
        final = os.fstat(descriptor)
        if (
            _identity(final) != _identity(opened)
            or final.st_size != opened.st_size
            or final.st_mtime_ns != opened.st_mtime_ns
            or final.st_ctime_ns != opened.st_ctime_ns
        ):
            raise EvidenceActivationDrift(
                f"Evidence file changed while hashing: {relative}"
            )
    finally:
        os.close(descriptor)
    return TreeEntryReceipt(
        relative_path=relative,
        kind="file",
        mode=_mode(final),
        size_bytes=int(final.st_size),
        device=int(final.st_dev),
        inode=int(final.st_ino),
        mtime_ns=int(final.st_mtime_ns),
        ctime_ns=int(final.st_ctime_ns),
        sha256=digest.hexdigest(),
    )


def fingerprint_evidence_tree(path: Path | str) -> EvidenceTreeReceipt:
    """Return a strict path/inode/per-file receipt for one stopped tree."""

    root = _absolute_path(path, label="Evidence tree", must_exist=True)
    before = _require_private_directory(root, label="Evidence tree")
    entries: list[TreeEntryReceipt] = []
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names.sort()
        file_names.sort()
        current = Path(directory)
        # os.walk(followlinks=False) leaves directory symlinks in
        # directory_names but does not visit them.  Without this explicit
        # lstat gate they would be absent from both the receipt and digest.
        for name in directory_names:
            child = current / name
            observed_child = child.lstat()
            if stat.S_ISLNK(observed_child.st_mode):
                raise EvidenceActivationError(
                    f"Evidence trees may not contain symlinks: {child.relative_to(root)}"
                )
            if not stat.S_ISDIR(observed_child.st_mode):
                raise EvidenceActivationError(
                    "Evidence tree directory traversal encountered a non-directory"
                )
        if current != root:
            entries.append(_entry_receipt(current, root))
        for name in file_names:
            entries.append(_entry_receipt(current / name, root))
    after = root.lstat()
    if (
        _identity(after) != _identity(before)
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_ctime_ns != before.st_ctime_ns
    ):
        raise EvidenceActivationDrift("Evidence tree changed while it was fingerprinted")
    entries.sort(key=lambda item: (item.relative_path, item.kind))
    material = [
        {
            "relative_path": entry.relative_path,
            "kind": entry.kind,
            "mode": entry.mode,
            "size_bytes": entry.size_bytes,
            "sha256": entry.sha256,
        }
        for entry in entries
    ]
    return EvidenceTreeReceipt(
        path=root,
        tree_sha256=_sha256_bytes(_canonical_bytes(material)),
        total_bytes=sum(entry.size_bytes for entry in entries if entry.kind == "file"),
        file_count=sum(entry.kind == "file" for entry in entries),
        directory_count=1 + sum(entry.kind == "directory" for entry in entries),
        device=int(after.st_dev),
        inode=int(after.st_ino),
        mode=_mode(after),
        mtime_ns=int(after.st_mtime_ns),
        ctime_ns=int(after.st_ctime_ns),
        entries=tuple(entries),
    )


def _same_tree(expected: EvidenceTreeReceipt, observed: EvidenceTreeReceipt) -> bool:
    relocated = expected.path != observed.path
    comparable = replace(expected, path=observed.path)
    if relocated:
        # rename/exchange may legitimately update the moved directory inode's
        # ctime.  Its inode, complete entry receipts, and content digest remain
        # bound; root timestamps are ignored only across a path relocation.
        comparable = replace(
            comparable,
            mtime_ns=observed.mtime_ns,
            ctime_ns=observed.ctime_ns,
        )
    return comparable == observed


def _assert_tree(
    expected: EvidenceTreeReceipt,
    observed: EvidenceTreeReceipt,
    *,
    label: str,
) -> None:
    if not _same_tree(expected, observed):
        raise EvidenceActivationDrift(f"{label} digest, inode, or file receipts drifted")


def _required_store_writer_lock_paths(live_path: Path) -> tuple[Path, ...]:
    """Return the mandatory store-root locks used by every relevant writer.

    Missing locks fail closed.  An explicit stopped-writer acknowledgement is
    not race-free by itself: a writer could start between a caller-side probe
    and the directory exchange.  Requiring and holding the stable lock files
    makes that gate effective for the whole critical section.
    """

    store_root = live_path.parent
    _require_owner_directory_0700(store_root, label="Evidence store root")
    paths: list[Path] = []
    for relative in STORE_WRITER_LOCK_RELATIVE_PATHS:
        lock_path = store_root / relative
        try:
            lock_path.lstat()
        except FileNotFoundError as exc:
            raise EvidenceActivationError(
                "cannot prove writers stopped because required store writer "
                f"lock is missing: {lock_path}"
            ) from exc
        _require_owner_directory_0700(
            lock_path.parent,
            label=f"writer lock parent {lock_path.parent}",
        )
        _require_private_file(lock_path, label=f"store writer lock {lock_path}")
        paths.append(lock_path)
    return tuple(paths)


@contextmanager
def _exclusive_lock_files(paths: Sequence[Path]) -> Iterator[None]:
    ordered = sorted(dict.fromkeys(paths), key=lambda item: str(item))
    with ExitStack() as stack:
        handles = []
        for lock_path in ordered:
            _require_owner_directory_0700(
                lock_path.parent,
                label=f"writer lock parent {lock_path.parent}",
            )
            try:
                observed = _require_private_file(
                    lock_path,
                    label=f"writer lock {lock_path}",
                )
            except FileNotFoundError as exc:
                raise EvidenceActivationError(
                    f"cannot prove writers stopped because lock is missing: {lock_path}"
                ) from exc
            flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(lock_path, flags)
            except OSError as exc:
                raise EvidenceActivationDrift(
                    f"writer lock changed before it could be opened: {lock_path}"
                ) from exc
            handle = stack.enter_context(os.fdopen(descriptor, "a+b"))
            opened = os.fstat(handle.fileno())
            if _identity(opened) != _identity(observed):
                raise EvidenceActivationDrift("writer lock changed before acquisition")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                raise EvidenceActivationLockBusy(
                    f"writer lock is active: {lock_path}"
                ) from exc
            try:
                after_lock = _require_private_file(
                    lock_path,
                    label=f"writer lock {lock_path}",
                )
            except FileNotFoundError as exc:
                raise EvidenceActivationDrift(
                    f"writer lock disappeared during acquisition: {lock_path}"
                ) from exc
            if _identity(after_lock) != _identity(opened):
                raise EvidenceActivationDrift("writer lock changed during acquisition")
            handles.append(handle)
        try:
            yield
        finally:
            for handle in reversed(handles):
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass


@contextmanager
def _exclusive_operation_locks(
    trees: Sequence[Path],
    *,
    live_path: Path,
) -> Iterator[None]:
    lock_paths = [tree / EVIDENCE_LOCK_FILENAME for tree in trees]
    lock_paths.extend(_required_store_writer_lock_paths(live_path))
    with _exclusive_lock_files(lock_paths):
        yield


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


def _fsync_directories(paths: Sequence[Path], fsync_fn: FsyncFunction) -> None:
    for path in sorted(dict.fromkeys(paths), key=lambda item: str(item)):
        descriptor = _open_directory(path)
        try:
            fsync_fn(descriptor)
        finally:
            os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("receipt write made no progress")
        offset += written


def _read_descriptor_bytes(descriptor: int) -> bytes:
    """Read one bounded regular file through an already-bound descriptor."""

    before = os.fstat(descriptor)
    if before.st_size > _RECEIPT_SIZE_LIMIT:
        raise EvidenceActivationReceiptError("receipt is unexpectedly large")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = _RECEIPT_SIZE_LIMIT + 1
    while remaining:
        chunk = os.read(descriptor, min(_COPY_CHUNK_SIZE, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    encoded = b"".join(chunks)
    after = os.fstat(descriptor)
    if _stat_signature(after) != _stat_signature(before):
        raise EvidenceActivationReceiptError("receipt changed while it was read")
    if len(encoded) != before.st_size:
        raise EvidenceActivationReceiptError("receipt size changed while it was read")
    return encoded


def _write_private_json(
    path: Path | str,
    payload: Mapping[str, Any],
    *,
    fsync_fn: FsyncFunction = os.fsync,
) -> Path:
    destination = _absolute_path(path, label="receipt", must_exist=False)
    parent = destination.parent
    _require_private_directory(parent, label="receipt parent")
    try:
        destination.lstat()
    except FileNotFoundError:
        pass
    else:
        raise EvidenceActivationReceiptError("receipt path already exists")
    encoded = _canonical_bytes(payload) + b"\n"
    if len(encoded) > _RECEIPT_SIZE_LIMIT:
        raise EvidenceActivationReceiptError("receipt is unexpectedly large")
    stage_name = f".{destination.name}.staged-{uuid.uuid4().hex}"
    stage_path = parent / stage_name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(stage_path, flags, 0o600)
    staged_identity: tuple[int, int] | None = _identity(os.fstat(descriptor))
    published = False
    publication_error: BaseException | None = None
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, encoded)
        fsync_fn(descriptor)
        staged_identity = _identity(os.fstat(descriptor))
        # Hard-link publication is atomic and refuses an existing final name.
        # Both names are in the already-validated private parent directory.
        os.link(stage_path, destination, follow_symlinks=False)
        published = True
    except FileExistsError as exc:
        publication_error = EvidenceActivationReceiptError(
            "receipt path was concurrently created"
        )
        publication_error.__cause__ = exc
    except BaseException as exc:
        publication_error = exc
    finally:
        os.close(descriptor)
    if not published:
        try:
            current = stage_path.lstat()
        except FileNotFoundError:
            pass
        else:
            if staged_identity is not None and _identity(current) == staged_identity:
                stage_path.unlink()
                try:
                    _fsync_directories((parent,), fsync_fn)
                except OSError:
                    pass
        if publication_error is not None:
            raise publication_error
        raise EvidenceActivationReceiptError("receipt publication failed")
    try:
        current_stage = stage_path.lstat()
        if staged_identity is None or _identity(current_stage) != staged_identity:
            raise EvidenceActivationReceiptError("receipt staging identity changed")
        stage_path.unlink()
        _fsync_directories((parent,), fsync_fn)
        final = _require_private_file(destination, label="receipt")
        final_descriptor = os.open(
            destination,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            if _identity(os.fstat(final_descriptor)) != _identity(final):
                raise EvidenceActivationReceiptError("receipt identity changed after publication")
            fsync_fn(final_descriptor)
        finally:
            os.close(final_descriptor)
        _fsync_directories((parent,), fsync_fn)
    except BaseException:
        # Never unlink the published name: after publication it is the crash
        # receipt, even when a later fsync reports uncertainty.
        raise
    return destination


def _read_private_json(path: Path | str) -> Mapping[str, Any]:
    receipt = _absolute_path(path, label="receipt", must_exist=True)
    observed = _require_private_file(receipt, label="receipt")
    if observed.st_size > _RECEIPT_SIZE_LIMIT:
        raise EvidenceActivationReceiptError("receipt is unexpectedly large")
    descriptor = os.open(
        receipt,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(observed):
            raise EvidenceActivationReceiptError("receipt changed before reading")
        chunks: list[bytes] = []
        remaining = _RECEIPT_SIZE_LIMIT + 1
        while remaining:
            chunk = os.read(descriptor, min(_COPY_CHUNK_SIZE, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        final = os.fstat(descriptor)
        if (
            _identity(final) != _identity(opened)
            or final.st_size != opened.st_size
            or final.st_mtime_ns != opened.st_mtime_ns
            or final.st_ctime_ns != opened.st_ctime_ns
        ):
            raise EvidenceActivationReceiptError("receipt changed while reading")
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceActivationReceiptError("receipt is not valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise EvidenceActivationReceiptError("receipt root must be an object")
    return payload


def _expect_keys(value: object, expected: set[str], *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise EvidenceActivationReceiptError(f"{label} fields are incompatible")
    if any(not isinstance(key, str) for key in value):
        raise EvidenceActivationReceiptError(f"{label} keys must be strings")
    return value


def _receipt_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvidenceActivationReceiptError(f"{label} must be a non-empty string")
    return value


def _receipt_int(value: object, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise EvidenceActivationReceiptError(f"{label} must be a non-negative integer")
    return value


def _receipt_path(value: object, *, label: str, must_exist: bool) -> Path:
    encoded = _receipt_string(value, label=label)
    path = Path(encoded)
    if not path.is_absolute() or str(path) != encoded or ".." in path.parts:
        raise EvidenceActivationReceiptError(f"{label} must be a normalized absolute path")
    if must_exist:
        return _absolute_path(path, label=label, must_exist=True)
    _absolute_path(path, label=label, must_exist=False)
    return path


def _receipt_digest(value: object, *, label: str) -> str:
    digest = _receipt_string(value, label=label)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise EvidenceActivationReceiptError(f"{label} must be a lowercase SHA-256 digest")
    return digest


def _receipt_optional_digest(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    return _receipt_digest(value, label=label)


def _receipt_optional_int(value: object, *, label: str) -> int | None:
    if value is None:
        return None
    return _receipt_int(value, label=label)


def _parse_entry(value: object) -> TreeEntryReceipt:
    payload = _expect_keys(
        value,
        {
            "relative_path",
            "kind",
            "mode",
            "size_bytes",
            "device",
            "inode",
            "mtime_ns",
            "ctime_ns",
            "sha256",
        },
        label="tree entry receipt",
    )
    relative = _receipt_string(payload["relative_path"], label="relative_path")
    relative_path = Path(relative)
    if (
        relative_path.is_absolute()
        or ".." in relative_path.parts
        or relative_path.as_posix() != relative
    ):
        raise EvidenceActivationReceiptError("tree entry path is unsafe")
    kind = _receipt_string(payload["kind"], label="entry kind")
    if kind not in {"file", "directory"}:
        raise EvidenceActivationReceiptError("tree entry kind is unsupported")
    digest_value = payload["sha256"]
    digest = None if digest_value is None else _receipt_digest(digest_value, label="entry sha256")
    if (kind == "file") != (digest is not None):
        raise EvidenceActivationReceiptError("tree entry digest does not match its kind")
    return TreeEntryReceipt(
        relative_path=relative,
        kind=kind,
        mode=_receipt_int(payload["mode"], label="entry mode"),
        size_bytes=_receipt_int(payload["size_bytes"], label="entry size"),
        device=_receipt_int(payload["device"], label="entry device"),
        inode=_receipt_int(payload["inode"], label="entry inode"),
        mtime_ns=_receipt_int(payload["mtime_ns"], label="entry mtime"),
        ctime_ns=_receipt_int(payload["ctime_ns"], label="entry ctime"),
        sha256=digest,
    )


def _parse_tree(value: object, *, require_path: bool) -> EvidenceTreeReceipt:
    payload = _expect_keys(
        value,
        {
            "path",
            "tree_sha256",
            "total_bytes",
            "file_count",
            "directory_count",
            "device",
            "inode",
            "mode",
            "mtime_ns",
            "ctime_ns",
            "entries",
        },
        label="tree receipt",
    )
    raw_entries = payload["entries"]
    if not isinstance(raw_entries, list):
        raise EvidenceActivationReceiptError("tree entries must be an array")
    entries = tuple(_parse_entry(item) for item in raw_entries)
    if tuple(sorted(entries, key=lambda item: (item.relative_path, item.kind))) != entries:
        raise EvidenceActivationReceiptError("tree entries are not canonically ordered")
    if len({entry.relative_path for entry in entries}) != len(entries):
        raise EvidenceActivationReceiptError("tree entries contain duplicate paths")
    receipt = EvidenceTreeReceipt(
        path=_receipt_path(payload["path"], label="tree path", must_exist=require_path),
        tree_sha256=_receipt_digest(payload["tree_sha256"], label="tree sha256"),
        total_bytes=_receipt_int(payload["total_bytes"], label="tree bytes"),
        file_count=_receipt_int(payload["file_count"], label="tree file count"),
        directory_count=_receipt_int(payload["directory_count"], label="tree directory count"),
        device=_receipt_int(payload["device"], label="tree device"),
        inode=_receipt_int(payload["inode"], label="tree inode"),
        mode=_receipt_int(payload["mode"], label="tree mode"),
        mtime_ns=_receipt_int(payload["mtime_ns"], label="tree mtime"),
        ctime_ns=_receipt_int(payload["ctime_ns"], label="tree ctime"),
        entries=entries,
    )
    material = [
        {
            "relative_path": entry.relative_path,
            "kind": entry.kind,
            "mode": entry.mode,
            "size_bytes": entry.size_bytes,
            "sha256": entry.sha256,
        }
        for entry in entries
    ]
    if receipt.tree_sha256 != _sha256_bytes(_canonical_bytes(material)):
        raise EvidenceActivationReceiptError("tree receipt aggregate digest is invalid")
    if receipt.file_count != sum(item.kind == "file" for item in entries):
        raise EvidenceActivationReceiptError("tree receipt file count is invalid")
    if receipt.directory_count != 1 + sum(item.kind == "directory" for item in entries):
        raise EvidenceActivationReceiptError("tree receipt directory count is invalid")
    if receipt.total_bytes != sum(item.size_bytes for item in entries if item.kind == "file"):
        raise EvidenceActivationReceiptError("tree receipt byte count is invalid")
    return receipt


def _parse_activation_intent(
    value: object,
    *,
    require_source_paths: bool,
) -> ActivationIntent:
    payload = _expect_keys(
        value,
        {
            "schema_version",
            "operation_id",
            "prepared_at_ns",
            "live",
            "candidate",
            "archive_path",
            "expected_live_tree_sha256",
            "expected_live_total_bytes",
            "expected_candidate_tree_sha256",
            "expected_candidate_total_bytes",
            "candidate_receipt_sha256",
            "snapshot_manifest_sha256",
        },
        label="activation intent",
    )
    if payload["schema_version"] != ACTIVATION_INTENT_SCHEMA_VERSION:
        raise EvidenceActivationReceiptError("activation intent schema is unsupported")
    intent = ActivationIntent(
        schema_version=ACTIVATION_INTENT_SCHEMA_VERSION,
        operation_id=_receipt_string(payload["operation_id"], label="operation id"),
        prepared_at_ns=_receipt_int(payload["prepared_at_ns"], label="prepared_at_ns"),
        live=_parse_tree(payload["live"], require_path=require_source_paths),
        candidate=_parse_tree(payload["candidate"], require_path=require_source_paths),
        archive_path=_receipt_path(
            payload["archive_path"],
            label="archive path",
            must_exist=False,
        ),
        expected_live_tree_sha256=_receipt_optional_digest(
            payload["expected_live_tree_sha256"],
            label="expected live tree SHA-256",
        ),
        expected_live_total_bytes=_receipt_optional_int(
            payload["expected_live_total_bytes"],
            label="expected live tree bytes",
        ),
        expected_candidate_tree_sha256=_receipt_optional_digest(
            payload["expected_candidate_tree_sha256"],
            label="expected candidate tree SHA-256",
        ),
        expected_candidate_total_bytes=_receipt_optional_int(
            payload["expected_candidate_total_bytes"],
            label="expected candidate tree bytes",
        ),
        candidate_receipt_sha256=_receipt_optional_digest(
            payload["candidate_receipt_sha256"],
            label="candidate receipt SHA-256",
        ),
        snapshot_manifest_sha256=_receipt_optional_digest(
            payload["snapshot_manifest_sha256"],
            label="snapshot manifest SHA-256",
        ),
    )
    if intent.live.path == intent.candidate.path:
        raise EvidenceActivationReceiptError("live and candidate paths must differ")
    try:
        _validate_archive_target(
            intent.archive_path,
            trees=(intent.live.path, intent.candidate.path),
            label="activation archive",
        )
        _assert_upstream_tree_bindings(
            intent,
            live=intent.live,
            candidate=intent.candidate,
        )
    except EvidenceActivationDrift as exc:
        raise EvidenceActivationReceiptError(str(exc)) from exc
    except EvidenceActivationError as exc:
        raise EvidenceActivationReceiptError(str(exc)) from exc
    return intent


def _parse_activation_receipt(value: object, *, require_paths: bool) -> ActivationReceipt:
    payload = _expect_keys(
        value,
        {
            "schema_version",
            "activated_at_ns",
            "intent",
            "installed_live",
            "archived_previous_live",
        },
        label="activation receipt",
    )
    if payload["schema_version"] != ACTIVATION_RECEIPT_SCHEMA_VERSION:
        raise EvidenceActivationReceiptError("activation receipt schema is unsupported")
    receipt = ActivationReceipt(
        schema_version=ACTIVATION_RECEIPT_SCHEMA_VERSION,
        activated_at_ns=_receipt_int(payload["activated_at_ns"], label="activated_at_ns"),
        intent=_parse_activation_intent(payload["intent"], require_source_paths=False),
        installed_live=_parse_tree(payload["installed_live"], require_path=require_paths),
        archived_previous_live=_parse_tree(
            payload["archived_previous_live"], require_path=require_paths
        ),
    )
    if receipt.installed_live.path != receipt.intent.live.path:
        raise EvidenceActivationReceiptError("installed live path differs from intent")
    if receipt.archived_previous_live.path != receipt.intent.archive_path:
        raise EvidenceActivationReceiptError("archive path differs from intent")
    if not _same_tree(receipt.intent.candidate, receipt.installed_live):
        raise EvidenceActivationReceiptError("installed live receipt is not the candidate")
    if not _same_tree(receipt.intent.live, receipt.archived_previous_live):
        raise EvidenceActivationReceiptError("archive receipt is not the previous live tree")
    return receipt


def _parse_rollback_intent(value: object, *, require_paths: bool) -> RollbackIntent:
    payload = _expect_keys(
        value,
        {
            "schema_version",
            "rollback_id",
            "prepared_at_ns",
            "activation_operation_id",
            "live_path",
            "source_archive_path",
            "failed_archive_path",
            "current_live",
            "archived_previous_live",
        },
        label="rollback intent",
    )
    if payload["schema_version"] != ROLLBACK_INTENT_SCHEMA_VERSION:
        raise EvidenceActivationReceiptError("rollback intent schema is unsupported")
    intent = RollbackIntent(
        schema_version=ROLLBACK_INTENT_SCHEMA_VERSION,
        rollback_id=_receipt_string(payload["rollback_id"], label="rollback id"),
        prepared_at_ns=_receipt_int(payload["prepared_at_ns"], label="prepared_at_ns"),
        activation_operation_id=_receipt_string(
            payload["activation_operation_id"], label="activation operation id"
        ),
        live_path=_receipt_path(payload["live_path"], label="live path", must_exist=require_paths),
        source_archive_path=_receipt_path(
            payload["source_archive_path"],
            label="source archive path",
            must_exist=require_paths,
        ),
        failed_archive_path=_receipt_path(
            payload["failed_archive_path"],
            label="failed archive path",
            must_exist=False,
        ),
        current_live=_parse_tree(payload["current_live"], require_path=require_paths),
        archived_previous_live=_parse_tree(
            payload["archived_previous_live"], require_path=require_paths
        ),
    )
    if intent.current_live.path != intent.live_path:
        raise EvidenceActivationReceiptError("rollback current live path differs")
    if intent.archived_previous_live.path != intent.source_archive_path:
        raise EvidenceActivationReceiptError("rollback source archive path differs")
    try:
        _validate_archive_target(
            intent.failed_archive_path,
            trees=(intent.live_path, intent.source_archive_path),
            label="failed archive",
        )
    except EvidenceActivationError as exc:
        raise EvidenceActivationReceiptError(str(exc)) from exc
    return intent


def _parse_rollback_receipt(value: object, *, require_paths: bool) -> RollbackReceipt:
    payload = _expect_keys(
        value,
        {
            "schema_version",
            "rolled_back_at_ns",
            "intent",
            "restored_live",
            "failed_live_archive",
        },
        label="rollback receipt",
    )
    if payload["schema_version"] != ROLLBACK_RECEIPT_SCHEMA_VERSION:
        raise EvidenceActivationReceiptError("rollback receipt schema is unsupported")
    receipt = RollbackReceipt(
        schema_version=ROLLBACK_RECEIPT_SCHEMA_VERSION,
        rolled_back_at_ns=_receipt_int(payload["rolled_back_at_ns"], label="rolled_back_at_ns"),
        intent=_parse_rollback_intent(payload["intent"], require_paths=False),
        restored_live=_parse_tree(payload["restored_live"], require_path=require_paths),
        failed_live_archive=_parse_tree(
            payload["failed_live_archive"], require_path=require_paths
        ),
    )
    if receipt.restored_live.path != receipt.intent.live_path:
        raise EvidenceActivationReceiptError("rollback restored live path differs")
    if receipt.failed_live_archive.path != receipt.intent.failed_archive_path:
        raise EvidenceActivationReceiptError("rollback failed archive path differs")
    if not _same_tree(receipt.intent.archived_previous_live, receipt.restored_live):
        raise EvidenceActivationReceiptError("rollback did not restore the original live tree")
    if not _same_tree(receipt.intent.current_live, receipt.failed_live_archive):
        raise EvidenceActivationReceiptError("rollback did not preserve the failed live tree")
    return receipt


def load_activation_intent(path: Path | str) -> ActivationIntent:
    return _parse_activation_intent(_read_private_json(path), require_source_paths=False)


def load_activation_receipt(path: Path | str) -> ActivationReceipt:
    return _parse_activation_receipt(_read_private_json(path), require_paths=False)


def load_rollback_intent(path: Path | str) -> RollbackIntent:
    return _parse_rollback_intent(_read_private_json(path), require_paths=False)


def load_rollback_receipt(path: Path | str) -> RollbackReceipt:
    return _parse_rollback_receipt(_read_private_json(path), require_paths=False)


@contextmanager
def _durable_intent_guard(
    path: Path,
    expected: ActivationIntent | RollbackIntent,
    *,
    loader: Callable[[Path | str], ActivationIntent | RollbackIntent],
    fsync_fn: FsyncFunction,
) -> Iterator[_DurableIntentGuard]:
    """Bind, parse, fsync, and continuously retain one durable intent inode."""

    durable = loader(path)
    if durable != expected:
        raise EvidenceActivationDrift(
            "operation intent receipt changed before crash recovery"
        )
    observed = _require_private_file(path, label="operation intent receipt")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        signature = _stat_signature(opened)
        if signature != _stat_signature(observed):
            raise EvidenceActivationDrift(
                "operation intent receipt changed before it was opened"
            )
        expected_bytes = _canonical_bytes(expected.to_dict()) + b"\n"
        if _read_descriptor_bytes(descriptor) != expected_bytes:
            raise EvidenceActivationDrift(
                "operation intent receipt is not the durable canonical intent"
            )
        fsync_fn(descriptor)
        _fsync_directories((path.parent,), fsync_fn)
        guard = _DurableIntentGuard(
            path=path,
            expected=expected,
            descriptor=descriptor,
            stat_signature=signature,
            expected_bytes=expected_bytes,
            loader=loader,
        )
        guard.recheck()
        yield guard
    finally:
        os.close(descriptor)


def _renamex_np(source: Path, destination: Path, flags: int) -> None:
    if sys.platform != "darwin":
        raise EvidenceActivationError(
            "atomic Evidence directory rename requires macOS renamex_np; "
            "no production fallback exists"
        )
    libc = ctypes.CDLL(None, use_errno=True)
    renamex = getattr(libc, "renamex_np", None)
    if renamex is None:
        raise EvidenceActivationError("macOS renamex_np is unavailable")
    renamex.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    renamex.restype = ctypes.c_int
    result = renamex(os.fsencode(source), os.fsencode(destination), flags)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(source), str(destination))


def _production_swap(source: Path, destination: Path) -> None:
    _renamex_np(source, destination, RENAME_SWAP)


def _production_move_exclusive(source: Path, destination: Path) -> None:
    _renamex_np(source, destination, RENAME_EXCL)


def _path_device(path: Path) -> int:
    return int(path.lstat().st_dev)


def _assert_same_device(paths: Sequence[Path]) -> None:
    devices = {_path_device(path) for path in paths}
    if len(devices) != 1:
        raise EvidenceActivationError(
            "live, candidate/source, and archive parent must share one device"
        )


def _ensure_absent(path: Path, *, label: str) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise EvidenceActivationError(f"{label} already exists; refusing overwrite")


def _outside_trees(path: Path, trees: Sequence[Path], *, label: str) -> None:
    for tree in trees:
        try:
            path.relative_to(tree)
        except ValueError:
            pass
        else:
            raise EvidenceActivationReceiptError(
                f"{label} must be disjoint from every Evidence tree"
            )
        try:
            tree.relative_to(path)
        except ValueError:
            continue
        raise EvidenceActivationReceiptError(
            f"{label} must be disjoint from every Evidence tree"
        )


def _validate_archive_target(
    path: Path,
    *,
    trees: Sequence[Path],
    label: str,
) -> None:
    _require_owner_directory_0700(path.parent, label=f"{label} parent")
    _outside_trees(path, trees, label=label)


def _validated_sha256(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise EvidenceActivationError(
            f"{label} must be a lowercase SHA-256 digest"
        )
    return value


def _validated_bytes(value: int | None, *, label: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise EvidenceActivationError(f"{label} must be a non-negative integer")
    return value


def _activation_binding_values(
    *,
    expected_live_tree_sha256: str | None,
    expected_live_total_bytes: int | None,
    expected_candidate_tree_sha256: str | None,
    expected_candidate_total_bytes: int | None,
    candidate_receipt_sha256: str | None,
    snapshot_manifest_sha256: str | None,
    required: bool,
) -> tuple[str | None, int | None, str | None, int | None, str | None, str | None]:
    values = (
        _validated_sha256(
            expected_live_tree_sha256,
            label="expected live tree SHA-256",
        ),
        _validated_bytes(
            expected_live_total_bytes,
            label="expected live tree bytes",
        ),
        _validated_sha256(
            expected_candidate_tree_sha256,
            label="expected candidate tree SHA-256",
        ),
        _validated_bytes(
            expected_candidate_total_bytes,
            label="expected candidate tree bytes",
        ),
        _validated_sha256(
            candidate_receipt_sha256,
            label="candidate receipt SHA-256",
        ),
        _validated_sha256(
            snapshot_manifest_sha256,
            label="snapshot manifest SHA-256",
        ),
    )
    populated = tuple(value is not None for value in values)
    if any(populated) and not all(populated):
        raise EvidenceActivationError(
            "receipt-bound activation requires all live/candidate tree and "
            "upstream receipt bindings"
        )
    if required and not all(populated):
        raise EvidenceActivationError(
            "receipt-bound activation is required but upstream bindings are missing"
        )
    return values


def _assert_upstream_tree_bindings(
    intent: ActivationIntent,
    *,
    live: EvidenceTreeReceipt,
    candidate: EvidenceTreeReceipt,
) -> None:
    bindings = _activation_binding_values(
        expected_live_tree_sha256=intent.expected_live_tree_sha256,
        expected_live_total_bytes=intent.expected_live_total_bytes,
        expected_candidate_tree_sha256=intent.expected_candidate_tree_sha256,
        expected_candidate_total_bytes=intent.expected_candidate_total_bytes,
        candidate_receipt_sha256=intent.candidate_receipt_sha256,
        snapshot_manifest_sha256=intent.snapshot_manifest_sha256,
        required=False,
    )
    if bindings[0] is None:
        return
    if (
        live.tree_sha256 != bindings[0]
        or live.total_bytes != bindings[1]
    ):
        raise EvidenceActivationDrift(
            "live Evidence tree does not match the bound snapshot manifest"
        )
    if (
        candidate.tree_sha256 != bindings[2]
        or candidate.total_bytes != bindings[3]
    ):
        raise EvidenceActivationDrift(
            "candidate Evidence tree does not match the bound candidate receipt"
        )


def _intent_value(value: ActivationIntent | Path | str) -> ActivationIntent:
    if isinstance(value, ActivationIntent):
        return _parse_activation_intent(value.to_dict(), require_source_paths=False)
    return load_activation_intent(value)


def _activation_receipt_value(
    value: ActivationReceipt | Path | str,
) -> ActivationReceipt:
    if isinstance(value, ActivationReceipt):
        return _parse_activation_receipt(value.to_dict(), require_paths=False)
    return load_activation_receipt(value)


def _rollback_intent_value(value: RollbackIntent | Path | str) -> RollbackIntent:
    if isinstance(value, RollbackIntent):
        return _parse_rollback_intent(value.to_dict(), require_paths=False)
    return load_rollback_intent(value)


def _matches_at(path: Path, expected: EvidenceTreeReceipt) -> bool:
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(observed.st_mode) or _identity(observed) != (
        expected.device,
        expected.inode,
    ):
        return False
    try:
        return _same_tree(expected, fingerprint_evidence_tree(path))
    except EvidenceActivationError:
        return False


def _is_absent(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    return False


def inspect_activation_state(value: ActivationIntent | Path | str) -> str:
    """Classify on-disk state from the durable pre-swap intent receipt."""

    intent = _intent_value(value)
    live_is_old = _matches_at(intent.live.path, intent.live)
    live_is_candidate = _matches_at(intent.live.path, intent.candidate)
    candidate_is_candidate = _matches_at(intent.candidate.path, intent.candidate)
    candidate_is_old = _matches_at(intent.candidate.path, intent.live)
    archive_is_old = _matches_at(intent.archive_path, intent.live)
    candidate_absent = _is_absent(intent.candidate.path)
    archive_absent = _is_absent(intent.archive_path)
    if live_is_old and candidate_is_candidate and archive_absent:
        return "pre_swap"
    if live_is_candidate and candidate_is_old and archive_absent:
        return "swapped_pending_archive"
    if live_is_candidate and candidate_absent and archive_is_old:
        return "installed"
    return "ambiguous"


def inspect_rollback_state(value: RollbackIntent | Path | str) -> str:
    """Classify rollback state from its durable pre-swap intent receipt."""

    intent = _rollback_intent_value(value)
    live_is_current = _matches_at(intent.live_path, intent.current_live)
    live_is_old = _matches_at(intent.live_path, intent.archived_previous_live)
    source_is_old = _matches_at(
        intent.source_archive_path,
        intent.archived_previous_live,
    )
    source_is_current = _matches_at(intent.source_archive_path, intent.current_live)
    failed_is_current = _matches_at(intent.failed_archive_path, intent.current_live)
    source_absent = _is_absent(intent.source_archive_path)
    failed_absent = _is_absent(intent.failed_archive_path)
    if live_is_current and source_is_old and failed_absent:
        return "pre_swap"
    if live_is_old and source_is_current and failed_absent:
        return "swapped_pending_failed_archive"
    if live_is_old and source_absent and failed_is_current:
        return "rolled_back"
    return "ambiguous"


def _classify_activation_after_mutation(
    intent: ActivationIntent,
) -> tuple[str, BaseException | None]:
    """Classify conservatively without masking an attempted live mutation."""

    try:
        return inspect_activation_state(intent), None
    except BaseException as exc:
        return "unknown_post_swap", exc


def _classify_rollback_after_mutation(
    intent: RollbackIntent,
) -> tuple[str, BaseException | None]:
    """Classify conservatively without masking an attempted rollback mutation."""

    try:
        return inspect_rollback_state(intent), None
    except BaseException as exc:
        return "unknown_post_swap", exc


def prepare_activation_intent(
    live_path: Path | str,
    candidate_path: Path | str,
    *,
    intent_path: Path | str,
    confirm_writers_stopped: bool,
    archive_path: Path | str | None = None,
    expected_live_tree_sha256: str | None = None,
    expected_live_total_bytes: int | None = None,
    expected_candidate_tree_sha256: str | None = None,
    expected_candidate_total_bytes: int | None = None,
    candidate_receipt_sha256: str | None = None,
    snapshot_manifest_sha256: str | None = None,
    require_receipt_bindings: bool = False,
    fsync_fn: FsyncFunction = os.fsync,
) -> ActivationIntent:
    """Seal a no-overwrite activation intent without changing either tree."""

    _confirm_writers_stopped(confirm_writers_stopped)
    live = _absolute_path(live_path, label="live Evidence tree", must_exist=True)
    candidate = _absolute_path(
        candidate_path,
        label="candidate Evidence tree",
        must_exist=True,
    )
    if live == candidate:
        raise EvidenceActivationError("live and candidate paths must differ")
    _require_owner_directory_0700(live.parent, label="live Evidence parent")
    _require_owner_directory_0700(candidate.parent, label="candidate Evidence parent")
    bindings = _activation_binding_values(
        expected_live_tree_sha256=expected_live_tree_sha256,
        expected_live_total_bytes=expected_live_total_bytes,
        expected_candidate_tree_sha256=expected_candidate_tree_sha256,
        expected_candidate_total_bytes=expected_candidate_total_bytes,
        candidate_receipt_sha256=candidate_receipt_sha256,
        snapshot_manifest_sha256=snapshot_manifest_sha256,
        required=require_receipt_bindings,
    )
    operation_id = uuid.uuid4().hex
    archive = (
        live.parent / f"evidence-v2.archive-{operation_id}"
        if archive_path is None
        else _absolute_path(archive_path, label="archive path", must_exist=False)
    )
    _validate_archive_target(
        archive,
        trees=(live, candidate),
        label="archive path",
    )
    _ensure_absent(archive, label="archive path")
    receipt_path = _absolute_path(intent_path, label="activation intent", must_exist=False)
    _outside_trees(receipt_path, (live, candidate), label="activation intent")
    if receipt_path == archive:
        raise EvidenceActivationReceiptError("activation intent may not occupy the archive path")
    _assert_same_device((live, candidate, archive.parent))
    with _exclusive_operation_locks((live, candidate), live_path=live):
        _validate_archive_target(
            archive,
            trees=(live, candidate),
            label="archive path",
        )
        _assert_same_device((live, candidate, archive.parent))
        live_receipt = fingerprint_evidence_tree(live)
        candidate_receipt = fingerprint_evidence_tree(candidate)
        if live_receipt.device != candidate_receipt.device:
            raise EvidenceActivationError("live and candidate receipts are on different devices")
        intent = ActivationIntent(
            schema_version=ACTIVATION_INTENT_SCHEMA_VERSION,
            operation_id=operation_id,
            prepared_at_ns=time.time_ns(),
            live=live_receipt,
            candidate=candidate_receipt,
            archive_path=archive,
            expected_live_tree_sha256=bindings[0],
            expected_live_total_bytes=bindings[1],
            expected_candidate_tree_sha256=bindings[2],
            expected_candidate_total_bytes=bindings[3],
            candidate_receipt_sha256=bindings[4],
            snapshot_manifest_sha256=bindings[5],
        )
        _assert_upstream_tree_bindings(
            intent,
            live=live_receipt,
            candidate=candidate_receipt,
        )
        _write_private_json(receipt_path, intent.to_dict(), fsync_fn=fsync_fn)
        _assert_tree(live_receipt, fingerprint_evidence_tree(live), label="live tree")
        _assert_tree(
            candidate_receipt,
            fingerprint_evidence_tree(candidate),
            label="candidate tree",
        )
        _ensure_absent(archive, label="archive path")
    return intent


def activate_evidence_rebuild(
    intent_or_path: Path | str,
    *,
    final_receipt_path: Path | str,
    confirm_writers_stopped: bool,
    swap_fn: SwapFunction | None = None,
    move_fn: MoveFunction | None = None,
    fsync_fn: FsyncFunction = os.fsync,
    fault_injector: FaultInjector | None = None,
) -> ActivationReceipt:
    """Atomically install a sealed candidate and retain the displaced live tree."""

    _confirm_writers_stopped(confirm_writers_stopped)
    if isinstance(intent_or_path, ActivationIntent):
        raise EvidenceActivationReceiptError(
            "activation requires the durable intent receipt path, not an in-memory intent"
        )
    intent_path = _absolute_path(
        intent_or_path,
        label="activation intent receipt",
        must_exist=True,
    )
    intent = load_activation_intent(intent_path)
    live = intent.live.path
    candidate = intent.candidate.path
    archive = intent.archive_path
    final_path = _absolute_path(
        final_receipt_path,
        label="activation receipt",
        must_exist=False,
    )
    _outside_trees(final_path, (live, candidate), label="activation receipt")
    if final_path == archive:
        raise EvidenceActivationReceiptError("activation receipt may not occupy the archive path")
    _validate_archive_target(
        archive,
        trees=(live, candidate),
        label="archive path",
    )
    _ensure_absent(final_path, label="activation receipt")
    _ensure_absent(archive, label="archive path")
    _assert_same_device((live, candidate, archive.parent))
    exchange = swap_fn or _production_swap
    move = move_fn or _production_move_exclusive
    swap_attempted = False
    with _exclusive_operation_locks((live, candidate), live_path=live):
        with _durable_intent_guard(
            intent_path,
            intent,
            loader=load_activation_intent,
            fsync_fn=fsync_fn,
        ) as intent_guard:
            _validate_archive_target(
                archive,
                trees=(live, candidate),
                label="archive path",
            )
            _assert_same_device((live, candidate, archive.parent))
            observed_live = fingerprint_evidence_tree(live)
            observed_candidate = fingerprint_evidence_tree(candidate)
            _assert_tree(intent.live, observed_live, label="live tree")
            _assert_tree(
                intent.candidate,
                observed_candidate,
                label="candidate tree",
            )
            _assert_upstream_tree_bindings(
                intent,
                live=observed_live,
                candidate=observed_candidate,
            )
            _ensure_absent(archive, label="archive path")
            _ensure_absent(final_path, label="activation receipt")
            parent_paths = (live.parent, candidate.parent, archive.parent)
            try:
                _fsync_directories(parent_paths, fsync_fn)
                _fault(fault_injector, "before_swap")
                # Re-read both the held descriptor and its pathname immediately
                # before the irreversible boundary.  Same-inode truncate/rewrite
                # races are rejected alongside unlink/replace races.
                intent_guard.recheck()
                swap_attempted = True
                exchange(live, candidate)
                _fault(fault_injector, "after_swap")
                _fsync_directories(parent_paths, fsync_fn)
                _assert_tree(
                    intent.candidate,
                    fingerprint_evidence_tree(live),
                    label="installed candidate",
                )
                _assert_tree(
                    intent.live,
                    fingerprint_evidence_tree(candidate),
                    label="displaced live tree",
                )
                _validate_archive_target(
                    archive,
                    trees=(live, candidate),
                    label="archive path",
                )
                _assert_same_device((live, candidate, archive.parent))
                _ensure_absent(archive, label="archive path")
                intent_guard.recheck()
                move(candidate, archive)
                _fault(fault_injector, "after_archive_move")
                _fsync_directories(parent_paths, fsync_fn)
                if not _is_absent(candidate):
                    raise EvidenceActivationError("candidate path remained after archive move")
                installed = fingerprint_evidence_tree(live)
                archived = fingerprint_evidence_tree(archive)
                _assert_tree(intent.candidate, installed, label="installed candidate")
                _assert_tree(intent.live, archived, label="archived previous live tree")
                receipt = ActivationReceipt(
                    schema_version=ACTIVATION_RECEIPT_SCHEMA_VERSION,
                    activated_at_ns=time.time_ns(),
                    intent=intent,
                    installed_live=installed,
                    archived_previous_live=archived,
                )
                _fault(fault_injector, "before_final_receipt")
                intent_guard.recheck()
                _write_private_json(final_path, receipt.to_dict(), fsync_fn=fsync_fn)
                _fault(fault_injector, "after_final_receipt")
            except BaseException as exc:
                state, classification_error = _classify_activation_after_mutation(intent)
                if swap_attempted:
                    raise EvidenceActivationPostSwapError(
                        intent,
                        exc,
                        state=state,
                        classification_error=classification_error,
                    ) from exc
                if isinstance(exc, EvidenceActivationError):
                    raise
                raise EvidenceActivationError(
                    f"activation failed before exchange: {type(exc).__name__}: {exc}"
                ) from exc
    try:
        verification = verify_evidence_activation(
            receipt,
            confirm_writers_stopped=True,
        )
    except BaseException as exc:
        state, classification_error = _classify_activation_after_mutation(intent)
        raise EvidenceActivationPostSwapError(
            intent,
            exc,
            state=state,
            classification_error=classification_error,
        ) from exc
    if not verification.ok:
        raise EvidenceActivationPostSwapError(
            intent,
            EvidenceActivationError("; ".join(verification.errors)),
            state=verification.state,
        )
    return receipt


def verify_evidence_activation(
    receipt_or_path: ActivationReceipt | Path | str,
    *,
    confirm_writers_stopped: bool,
) -> ActivationVerification:
    """Verify live/archive path, inode, digest, and complete entry receipts."""

    _confirm_writers_stopped(confirm_writers_stopped)
    receipt = _activation_receipt_value(receipt_or_path)
    checks: list[str] = []
    errors: list[str] = []
    live_digest: str | None = None
    archive_digest: str | None = None
    try:
        if not _is_absent(receipt.intent.candidate.path):
            raise EvidenceActivationDrift("candidate path still exists after activation")
        checks.append("candidate_path_absent")
        with _exclusive_operation_locks(
            (receipt.installed_live.path, receipt.archived_previous_live.path),
            live_path=receipt.installed_live.path,
        ):
            live = fingerprint_evidence_tree(receipt.installed_live.path)
            archive = fingerprint_evidence_tree(receipt.archived_previous_live.path)
            live_digest = live.tree_sha256
            archive_digest = archive.tree_sha256
            _assert_tree(receipt.installed_live, live, label="installed live tree")
            checks.append("live_path_inode_digest_receipts")
            _assert_tree(
                receipt.archived_previous_live,
                archive,
                label="archived previous live tree",
            )
            checks.append("archive_path_inode_digest_receipts")
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    state = "installed"
    if errors:
        state, classification_error = _classify_activation_after_mutation(receipt.intent)
        if classification_error is not None:
            errors.append(
                "state classification failed: "
                f"{type(classification_error).__name__}: {classification_error}"
            )
    return ActivationVerification(
        ok=not errors,
        state=state,
        checks=tuple(checks),
        errors=tuple(errors),
        observed_live_sha256=live_digest,
        observed_archive_sha256=archive_digest,
    )


def rollback_evidence_activation(
    receipt_or_path: ActivationReceipt | Path | str,
    *,
    rollback_intent_path: Path | str,
    final_receipt_path: Path | str,
    confirm_writers_stopped: bool,
    failed_archive_path: Path | str | None = None,
    swap_fn: SwapFunction | None = None,
    move_fn: MoveFunction | None = None,
    fsync_fn: FsyncFunction = os.fsync,
    fault_injector: FaultInjector | None = None,
) -> RollbackReceipt:
    """Restore the archived live tree while preserving the complete failed tree."""

    _confirm_writers_stopped(confirm_writers_stopped)
    activation = _activation_receipt_value(receipt_or_path)
    live = activation.installed_live.path
    source_archive = activation.archived_previous_live.path
    rollback_id = uuid.uuid4().hex
    failed_archive = (
        live.parent / f"evidence-v2.failed-{rollback_id}"
        if failed_archive_path is None
        else _absolute_path(
            failed_archive_path,
            label="failed archive path",
            must_exist=False,
        )
    )
    _validate_archive_target(
        failed_archive,
        trees=(live, source_archive),
        label="failed archive",
    )
    intent_path = _absolute_path(
        rollback_intent_path,
        label="rollback intent",
        must_exist=False,
    )
    final_path = _absolute_path(
        final_receipt_path,
        label="rollback receipt",
        must_exist=False,
    )
    _outside_trees(intent_path, (live, source_archive), label="rollback intent")
    _outside_trees(final_path, (live, source_archive), label="rollback receipt")
    if intent_path == failed_archive or final_path == failed_archive:
        raise EvidenceActivationReceiptError(
            "rollback receipts may not occupy the failed archive path"
        )
    _ensure_absent(intent_path, label="rollback intent")
    _ensure_absent(final_path, label="rollback receipt")
    _ensure_absent(failed_archive, label="failed archive")
    _assert_same_device((live, source_archive, failed_archive.parent))
    exchange = swap_fn or _production_swap
    move = move_fn or _production_move_exclusive
    swap_attempted = False
    with _exclusive_operation_locks((live, source_archive), live_path=live):
        _validate_archive_target(
            failed_archive,
            trees=(live, source_archive),
            label="failed archive",
        )
        _assert_same_device((live, source_archive, failed_archive.parent))
        current_live = fingerprint_evidence_tree(live)
        if (current_live.device, current_live.inode) != (
            activation.installed_live.device,
            activation.installed_live.inode,
        ):
            raise EvidenceActivationDrift(
                "current live directory inode differs from the activated tree"
            )
        archived = fingerprint_evidence_tree(source_archive)
        _assert_tree(
            activation.archived_previous_live,
            archived,
            label="archived previous live tree",
        )
        intent = RollbackIntent(
            schema_version=ROLLBACK_INTENT_SCHEMA_VERSION,
            rollback_id=rollback_id,
            prepared_at_ns=time.time_ns(),
            activation_operation_id=activation.intent.operation_id,
            live_path=live,
            source_archive_path=source_archive,
            failed_archive_path=failed_archive,
            current_live=current_live,
            archived_previous_live=archived,
        )
        _write_private_json(intent_path, intent.to_dict(), fsync_fn=fsync_fn)
        with _durable_intent_guard(
            intent_path,
            intent,
            loader=load_rollback_intent,
            fsync_fn=fsync_fn,
        ) as intent_guard:
            _assert_tree(
                current_live,
                fingerprint_evidence_tree(live),
                label="current live tree",
            )
            _assert_tree(
                archived,
                fingerprint_evidence_tree(source_archive),
                label="archived previous live tree",
            )
            parent_paths = (live.parent, source_archive.parent, failed_archive.parent)
            try:
                _fsync_directories(parent_paths, fsync_fn)
                _fault(fault_injector, "before_rollback_swap")
                intent_guard.recheck()
                swap_attempted = True
                exchange(live, source_archive)
                _fault(fault_injector, "after_rollback_swap")
                _fsync_directories(parent_paths, fsync_fn)
                _assert_tree(
                    archived,
                    fingerprint_evidence_tree(live),
                    label="restored live tree",
                )
                _assert_tree(
                    current_live,
                    fingerprint_evidence_tree(source_archive),
                    label="failed live tree",
                )
                _validate_archive_target(
                    failed_archive,
                    trees=(live, source_archive),
                    label="failed archive",
                )
                _assert_same_device((live, source_archive, failed_archive.parent))
                _ensure_absent(failed_archive, label="failed archive")
                intent_guard.recheck()
                move(source_archive, failed_archive)
                _fault(fault_injector, "after_failed_archive_move")
                _fsync_directories(parent_paths, fsync_fn)
                if not _is_absent(source_archive):
                    raise EvidenceActivationError(
                        "source archive remained after rollback archive move"
                    )
                restored = fingerprint_evidence_tree(live)
                failed = fingerprint_evidence_tree(failed_archive)
                _assert_tree(archived, restored, label="restored live tree")
                _assert_tree(current_live, failed, label="preserved failed live tree")
                receipt = RollbackReceipt(
                    schema_version=ROLLBACK_RECEIPT_SCHEMA_VERSION,
                    rolled_back_at_ns=time.time_ns(),
                    intent=intent,
                    restored_live=restored,
                    failed_live_archive=failed,
                )
                _fault(fault_injector, "before_rollback_receipt")
                intent_guard.recheck()
                _write_private_json(final_path, receipt.to_dict(), fsync_fn=fsync_fn)
                _fault(fault_injector, "after_rollback_receipt")
            except BaseException as exc:
                state, classification_error = _classify_rollback_after_mutation(intent)
                if swap_attempted:
                    raise EvidenceRollbackPostSwapError(
                        intent,
                        exc,
                        state=state,
                        classification_error=classification_error,
                    ) from exc
                if isinstance(exc, EvidenceActivationError):
                    raise
                raise EvidenceActivationError(
                    f"rollback failed before exchange: {type(exc).__name__}: {exc}"
                ) from exc
    final_state, classification_error = _classify_rollback_after_mutation(receipt.intent)
    if final_state != "rolled_back" or classification_error is not None:
        raise EvidenceRollbackPostSwapError(
            receipt.intent,
            EvidenceActivationDrift("rollback final paths failed verification"),
            state=final_state,
            classification_error=classification_error,
        )
    return receipt


def recover_activation_from_intent(
    intent_or_path: Path | str,
    *,
    final_receipt_path: Path | str,
    confirm_writers_stopped: bool,
    swap_fn: SwapFunction | None = None,
    move_fn: MoveFunction | None = None,
    fsync_fn: FsyncFunction = os.fsync,
    fault_injector: FaultInjector | None = None,
) -> ActivationReceipt:
    """Resume or finalize exactly one durable activation crash intent.

    ``pre_swap`` performs the sealed exchange, ``swapped_pending_archive``
    exclusively moves the displaced generation to its already-bound archive,
    and ``installed`` only publishes the missing final receipt.  ``ambiguous``
    never mutates anything.
    """

    _confirm_writers_stopped(confirm_writers_stopped)
    if isinstance(intent_or_path, ActivationIntent):
        raise EvidenceActivationReceiptError(
            "activation recovery requires the durable intent receipt path"
        )
    intent_path = _absolute_path(
        intent_or_path,
        label="activation intent receipt",
        must_exist=True,
    )
    intent = load_activation_intent(intent_path)
    live = intent.live.path
    candidate = intent.candidate.path
    archive = intent.archive_path
    final_path = _absolute_path(
        final_receipt_path,
        label="activation receipt",
        must_exist=False,
    )
    _outside_trees(
        final_path,
        (live, candidate, archive),
        label="activation receipt",
    )
    _outside_trees(
        intent_path,
        (live, candidate, archive),
        label="activation intent receipt",
    )
    if final_path == intent_path:
        raise EvidenceActivationReceiptError(
            "activation receipt may not overwrite its durable intent"
        )
    _ensure_absent(final_path, label="activation receipt")
    initial_state = inspect_activation_state(intent)
    if initial_state == "ambiguous":
        raise EvidenceActivationDrift(
            "activation crash intent is ambiguous; refusing recovery"
        )
    lock_trees = (
        (live, archive) if initial_state == "installed" else (live, candidate)
    )
    exchange = swap_fn or _production_swap
    move = move_fn or _production_move_exclusive
    mutation_attempted = False
    with _exclusive_operation_locks(lock_trees, live_path=live):
        with _durable_intent_guard(
            intent_path,
            intent,
            loader=load_activation_intent,
            fsync_fn=fsync_fn,
        ) as intent_guard:
            state = inspect_activation_state(intent)
            if state != initial_state:
                raise EvidenceActivationDrift(
                    "activation state changed while recovery locks were acquired"
                )
            _ensure_absent(final_path, label="activation receipt")
            parent_paths = (live.parent, candidate.parent, archive.parent)
            try:
                if state == "pre_swap":
                    _validate_archive_target(
                        archive,
                        trees=(live, candidate),
                        label="archive path",
                    )
                    _assert_same_device((live, candidate, archive.parent))
                    observed_live = fingerprint_evidence_tree(live)
                    observed_candidate = fingerprint_evidence_tree(candidate)
                    _assert_tree(intent.live, observed_live, label="live tree")
                    _assert_tree(
                        intent.candidate,
                        observed_candidate,
                        label="candidate tree",
                    )
                    _assert_upstream_tree_bindings(
                        intent,
                        live=observed_live,
                        candidate=observed_candidate,
                    )
                    _ensure_absent(archive, label="archive path")
                    _fsync_directories(parent_paths, fsync_fn)
                    _fault(fault_injector, "before_recovery_swap")
                    intent_guard.recheck()
                    mutation_attempted = True
                    exchange(live, candidate)
                    _fault(fault_injector, "after_recovery_swap")
                    _fsync_directories(parent_paths, fsync_fn)
                    state = inspect_activation_state(intent)
                    if state != "swapped_pending_archive":
                        raise EvidenceActivationDrift(
                            "activation recovery exchange produced an ambiguous state"
                        )

                if state == "swapped_pending_archive":
                    _assert_tree(
                        intent.candidate,
                        fingerprint_evidence_tree(live),
                        label="installed candidate",
                    )
                    _assert_tree(
                        intent.live,
                        fingerprint_evidence_tree(candidate),
                        label="displaced live tree",
                    )
                    _validate_archive_target(
                        archive,
                        trees=(live, candidate),
                        label="archive path",
                    )
                    _assert_same_device((live, candidate, archive.parent))
                    _ensure_absent(archive, label="archive path")
                    _fsync_directories(parent_paths, fsync_fn)
                    _fault(fault_injector, "before_recovery_archive_move")
                    intent_guard.recheck()
                    mutation_attempted = True
                    move(candidate, archive)
                    _fault(fault_injector, "after_recovery_archive_move")
                    _fsync_directories(parent_paths, fsync_fn)
                    state = inspect_activation_state(intent)
                    if state != "installed":
                        raise EvidenceActivationDrift(
                            "activation recovery archive move produced an ambiguous state"
                        )

                installed = fingerprint_evidence_tree(live)
                archived = fingerprint_evidence_tree(archive)
                _assert_tree(intent.candidate, installed, label="installed candidate")
                _assert_tree(intent.live, archived, label="archived previous live tree")
                if not _is_absent(candidate):
                    raise EvidenceActivationDrift(
                        "candidate path still exists after activation recovery"
                    )
                receipt = ActivationReceipt(
                    schema_version=ACTIVATION_RECEIPT_SCHEMA_VERSION,
                    activated_at_ns=time.time_ns(),
                    intent=intent,
                    installed_live=installed,
                    archived_previous_live=archived,
                )
                _fault(fault_injector, "before_recovery_receipt")
                intent_guard.recheck()
                _ensure_absent(final_path, label="activation receipt")
                _write_private_json(final_path, receipt.to_dict(), fsync_fn=fsync_fn)
                _fault(fault_injector, "after_recovery_receipt")
            except BaseException as exc:
                observed_state, classification_error = (
                    _classify_activation_after_mutation(intent)
                )
                if (
                    initial_state != "pre_swap"
                    or mutation_attempted
                    or observed_state != "pre_swap"
                    or classification_error is not None
                ):
                    raise EvidenceActivationPostSwapError(
                        intent,
                        exc,
                        state=observed_state,
                        classification_error=classification_error,
                    ) from exc
                if isinstance(exc, EvidenceActivationError):
                    raise
                raise EvidenceActivationError(
                    "activation recovery failed before exchange: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
    try:
        verification = verify_evidence_activation(
            receipt,
            confirm_writers_stopped=True,
        )
    except BaseException as exc:
        state, classification_error = _classify_activation_after_mutation(intent)
        raise EvidenceActivationPostSwapError(
            intent,
            exc,
            state=state,
            classification_error=classification_error,
        ) from exc
    if not verification.ok:
        raise EvidenceActivationPostSwapError(
            intent,
            EvidenceActivationDrift("; ".join(verification.errors)),
            state=verification.state,
        )
    return receipt


def recover_rollback_from_intent(
    intent_or_path: Path | str,
    *,
    final_receipt_path: Path | str,
    confirm_writers_stopped: bool,
    swap_fn: SwapFunction | None = None,
    move_fn: MoveFunction | None = None,
    fsync_fn: FsyncFunction = os.fsync,
    fault_injector: FaultInjector | None = None,
) -> RollbackReceipt:
    """Resume or finalize exactly one durable rollback crash intent."""

    _confirm_writers_stopped(confirm_writers_stopped)
    if isinstance(intent_or_path, RollbackIntent):
        raise EvidenceActivationReceiptError(
            "rollback recovery requires the durable intent receipt path"
        )
    intent_path = _absolute_path(
        intent_or_path,
        label="rollback intent receipt",
        must_exist=True,
    )
    intent = load_rollback_intent(intent_path)
    live = intent.live_path
    source_archive = intent.source_archive_path
    failed_archive = intent.failed_archive_path
    final_path = _absolute_path(
        final_receipt_path,
        label="rollback receipt",
        must_exist=False,
    )
    _outside_trees(
        final_path,
        (live, source_archive, failed_archive),
        label="rollback receipt",
    )
    _outside_trees(
        intent_path,
        (live, source_archive, failed_archive),
        label="rollback intent receipt",
    )
    if final_path == intent_path:
        raise EvidenceActivationReceiptError(
            "rollback receipt may not overwrite its durable intent"
        )
    _ensure_absent(final_path, label="rollback receipt")
    initial_state = inspect_rollback_state(intent)
    if initial_state == "ambiguous":
        raise EvidenceActivationDrift(
            "rollback crash intent is ambiguous; refusing recovery"
        )
    lock_trees = (
        (live, failed_archive)
        if initial_state == "rolled_back"
        else (live, source_archive)
    )
    exchange = swap_fn or _production_swap
    move = move_fn or _production_move_exclusive
    mutation_attempted = False
    with _exclusive_operation_locks(lock_trees, live_path=live):
        with _durable_intent_guard(
            intent_path,
            intent,
            loader=load_rollback_intent,
            fsync_fn=fsync_fn,
        ) as intent_guard:
            state = inspect_rollback_state(intent)
            if state != initial_state:
                raise EvidenceActivationDrift(
                    "rollback state changed while recovery locks were acquired"
                )
            _ensure_absent(final_path, label="rollback receipt")
            parent_paths = (live.parent, source_archive.parent, failed_archive.parent)
            try:
                if state == "pre_swap":
                    _validate_archive_target(
                        failed_archive,
                        trees=(live, source_archive),
                        label="failed archive",
                    )
                    _assert_same_device((live, source_archive, failed_archive.parent))
                    current = fingerprint_evidence_tree(live)
                    archived = fingerprint_evidence_tree(source_archive)
                    _assert_tree(intent.current_live, current, label="current live tree")
                    _assert_tree(
                        intent.archived_previous_live,
                        archived,
                        label="archived previous live tree",
                    )
                    _ensure_absent(failed_archive, label="failed archive")
                    _fsync_directories(parent_paths, fsync_fn)
                    _fault(fault_injector, "before_recovery_rollback_swap")
                    intent_guard.recheck()
                    mutation_attempted = True
                    exchange(live, source_archive)
                    _fault(fault_injector, "after_recovery_rollback_swap")
                    _fsync_directories(parent_paths, fsync_fn)
                    state = inspect_rollback_state(intent)
                    if state != "swapped_pending_failed_archive":
                        raise EvidenceActivationDrift(
                            "rollback recovery exchange produced an ambiguous state"
                        )

                if state == "swapped_pending_failed_archive":
                    _assert_tree(
                        intent.archived_previous_live,
                        fingerprint_evidence_tree(live),
                        label="restored live tree",
                    )
                    _assert_tree(
                        intent.current_live,
                        fingerprint_evidence_tree(source_archive),
                        label="failed live tree",
                    )
                    _validate_archive_target(
                        failed_archive,
                        trees=(live, source_archive),
                        label="failed archive",
                    )
                    _assert_same_device((live, source_archive, failed_archive.parent))
                    _ensure_absent(failed_archive, label="failed archive")
                    _fsync_directories(parent_paths, fsync_fn)
                    _fault(fault_injector, "before_recovery_failed_archive_move")
                    intent_guard.recheck()
                    mutation_attempted = True
                    move(source_archive, failed_archive)
                    _fault(fault_injector, "after_recovery_failed_archive_move")
                    _fsync_directories(parent_paths, fsync_fn)
                    state = inspect_rollback_state(intent)
                    if state != "rolled_back":
                        raise EvidenceActivationDrift(
                            "rollback recovery archive move produced an ambiguous state"
                        )

                restored = fingerprint_evidence_tree(live)
                failed = fingerprint_evidence_tree(failed_archive)
                _assert_tree(
                    intent.archived_previous_live,
                    restored,
                    label="restored live tree",
                )
                _assert_tree(intent.current_live, failed, label="preserved failed live tree")
                if not _is_absent(source_archive):
                    raise EvidenceActivationDrift(
                        "source archive still exists after rollback recovery"
                    )
                receipt = RollbackReceipt(
                    schema_version=ROLLBACK_RECEIPT_SCHEMA_VERSION,
                    rolled_back_at_ns=time.time_ns(),
                    intent=intent,
                    restored_live=restored,
                    failed_live_archive=failed,
                )
                _fault(fault_injector, "before_recovery_rollback_receipt")
                intent_guard.recheck()
                _ensure_absent(final_path, label="rollback receipt")
                _write_private_json(final_path, receipt.to_dict(), fsync_fn=fsync_fn)
                _fault(fault_injector, "after_recovery_rollback_receipt")
            except BaseException as exc:
                observed_state, classification_error = (
                    _classify_rollback_after_mutation(intent)
                )
                if (
                    initial_state != "pre_swap"
                    or mutation_attempted
                    or observed_state != "pre_swap"
                    or classification_error is not None
                ):
                    raise EvidenceRollbackPostSwapError(
                        intent,
                        exc,
                        state=observed_state,
                        classification_error=classification_error,
                    ) from exc
                if isinstance(exc, EvidenceActivationError):
                    raise
                raise EvidenceActivationError(
                    "rollback recovery failed before exchange: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
    final_state, classification_error = _classify_rollback_after_mutation(receipt.intent)
    if final_state != "rolled_back" or classification_error is not None:
        raise EvidenceRollbackPostSwapError(
            intent,
            EvidenceActivationDrift("rollback recovery final paths failed verification"),
            state=final_state,
            classification_error=classification_error,
        )
    return receipt


__all__ = [
    "ACTIVATION_INTENT_SCHEMA_VERSION",
    "ACTIVATION_RECEIPT_SCHEMA_VERSION",
    "ROLLBACK_INTENT_SCHEMA_VERSION",
    "ROLLBACK_RECEIPT_SCHEMA_VERSION",
    "ActivationIntent",
    "ActivationReceipt",
    "ActivationVerification",
    "EvidenceActivationDrift",
    "EvidenceActivationError",
    "EvidenceActivationLockBusy",
    "EvidenceActivationPostSwapError",
    "EvidenceActivationReceiptError",
    "EvidenceRollbackPostSwapError",
    "EvidenceTreeReceipt",
    "RollbackIntent",
    "RollbackReceipt",
    "TreeEntryReceipt",
    "WritersStoppedConfirmationRequired",
    "activate_evidence_rebuild",
    "fingerprint_evidence_tree",
    "inspect_activation_state",
    "inspect_rollback_state",
    "load_activation_intent",
    "load_activation_receipt",
    "load_rollback_intent",
    "load_rollback_receipt",
    "prepare_activation_intent",
    "recover_activation_from_intent",
    "recover_rollback_from_intent",
    "rollback_evidence_activation",
    "verify_evidence_activation",
]
