#!/usr/bin/env python3
"""Create one lock-consistent offline copy of a live legacy event ledger.

This command is deliberately narrow and opt-in.  It never discovers a store,
stops a watcher, or writes below the source root.  The caller must name one
explicit live store and one existing owner-only output parent.  The command
opens the existing writer lock read-only, takes a short shared lock, copies the
exact bytes of ``events.jsonl`` into a private directory, and publishes an
external ``legacy-chronicle`` manifest only after identity and hash checks pass.

Known agentacct writers take an exclusive lock on the same lock file for both
append and atomic-replace paths.  Source fd/path identity checks and an
independent destination hash additionally fail closed if an uncooperative
writer changes the ledger during capture.

Offline publication creates each final name directly with ``O_EXCL``.  It
never rolls a failed publication back by pathname: a same-UID rename can race
any stat-then-unlink sequence, so failed owner-only capture artifacts are
retained for explicit inspection instead.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from agentacct.canonical.live_paths import (
    LivePathSafetyError,
    configured_live_state_roots,
    paths_overlap,
    reject_live_state_overlap,
)
from agentacct.canonical.snapshot import SNAPSHOT_MANIFEST_VERSION, VerifiedSnapshot


EVENTS_NAME = "events.jsonl"
LOCK_NAME = "events.jsonl.lock"
MANIFEST_NAME = "legacy-chronicle-manifest.json"
ATTESTATION_NAME = "capture-attestation.json"
SNAPSHOT_DIRECTORY_NAME = "snapshot"
PARITY_SCRATCH_DIRECTORY_NAME = "parity-scratch"
MANIFEST_KIND = "legacy-chronicle"
COPY_CHUNK_BYTES = 1024 * 1024
MAX_LOCKED_COPY_BYTES = 64 * 1024 * 1024
DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0


class CaptureRefusal(RuntimeError):
    """The requested live read or offline publication is not proven safe."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _assert_no_symlink_components(path: Path, *, label: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            observed = current.lstat()
        except OSError as exc:
            raise CaptureRefusal(f"{label} does not exist or cannot be inspected") from exc
        if stat.S_ISLNK(observed.st_mode):
            raise CaptureRefusal(f"{label} may not contain symlink components")


def _require_absolute_directory(
    raw: Path | str,
    *,
    label: str,
    owner_only: bool,
) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise CaptureRefusal(f"{label} must be an explicit absolute path")
    _assert_no_symlink_components(path, label=label)
    try:
        observed = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise CaptureRefusal(f"{label} must be an existing directory") from exc
    if not stat.S_ISDIR(observed.st_mode):
        raise CaptureRefusal(f"{label} must be an existing directory")
    if observed.st_uid != os.geteuid():
        raise CaptureRefusal(f"{label} must be owned by the current user")
    mode = stat.S_IMODE(observed.st_mode)
    if owner_only and mode & 0o077:
        raise CaptureRefusal(f"{label} must be owner-only")
    if not owner_only and mode & 0o022:
        raise CaptureRefusal(f"{label} may not be group- or world-writable")
    return path.resolve(strict=True)


def _require_source_store(raw: Path | str) -> Path:
    source = _require_absolute_directory(
        raw,
        label="--source-store-root",
        owner_only=False,
    )
    if source not in configured_live_state_roots():
        raise CaptureRefusal(
            "--source-store-root must exactly match a configured/default live state root"
        )
    return source


def _require_output_parent(raw: Path | str, *, source: Path) -> Path:
    output = _require_absolute_directory(
        raw,
        label="--output-parent",
        owner_only=True,
    )
    try:
        reject_live_state_overlap(output, label="--output-parent")
    except LivePathSafetyError as exc:
        raise CaptureRefusal(str(exc)) from exc
    if paths_overlap(output, source):
        raise CaptureRefusal("--output-parent must be disjoint from the source store")
    return output


def _device_inode(observed: os.stat_result) -> tuple[int, int]:
    return int(observed.st_dev), int(observed.st_ino)


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _validate_directory_stat(
    observed: os.stat_result,
    *,
    label: str,
    owner_only: bool,
) -> None:
    if not stat.S_ISDIR(observed.st_mode):
        raise CaptureRefusal(f"{label} must be a directory")
    if observed.st_uid != os.geteuid():
        raise CaptureRefusal(f"{label} must be owned by the current user")
    mode = stat.S_IMODE(observed.st_mode)
    if owner_only and mode & 0o077:
        raise CaptureRefusal(f"{label} must be owner-only")
    if not owner_only and mode & 0o022:
        raise CaptureRefusal(f"{label} may not be group- or world-writable")


def _prove_directory_path(
    path: Path,
    descriptor: int,
    *,
    expected_identity: tuple[int, int],
    label: str,
    owner_only: bool,
) -> None:
    _assert_no_symlink_components(path, label=label)
    descriptor_stat = os.fstat(descriptor)
    path_stat = path.stat(follow_symlinks=False)
    _validate_directory_stat(
        descriptor_stat,
        label=label,
        owner_only=owner_only,
    )
    _validate_directory_stat(path_stat, label=label, owner_only=owner_only)
    if (
        _device_inode(descriptor_stat) != expected_identity
        or _device_inode(path_stat) != expected_identity
    ):
        raise CaptureRefusal(f"{label} path identity changed")


def _open_anchored_directory(
    path: Path,
    *,
    label: str,
    owner_only: bool,
) -> tuple[int, tuple[int, int]]:
    try:
        descriptor = os.open(path, _directory_open_flags())
    except OSError as exc:
        raise CaptureRefusal(f"{label} cannot be opened safely") from exc
    try:
        identity = _device_inode(os.fstat(descriptor))
        _prove_directory_path(
            path,
            descriptor,
            expected_identity=identity,
            label=label,
            owner_only=owner_only,
        )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, identity


def _prove_child_directory(
    parent_descriptor: int,
    name: str,
    descriptor: int,
    *,
    expected_identity: tuple[int, int],
    label: str,
) -> None:
    descriptor_stat = os.fstat(descriptor)
    path_stat = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    _validate_directory_stat(descriptor_stat, label=label, owner_only=True)
    _validate_directory_stat(path_stat, label=label, owner_only=True)
    if (
        _device_inode(descriptor_stat) != expected_identity
        or _device_inode(path_stat) != expected_identity
    ):
        raise CaptureRefusal(f"{label} path identity changed")


def _source_signature(observed: os.stat_result) -> tuple[int, ...]:
    return (
        int(observed.st_dev),
        int(observed.st_ino),
        int(observed.st_size),
        int(observed.st_mtime_ns),
        int(observed.st_ctime_ns),
    )


def _validate_regular_source(
    observed: os.stat_result,
    *,
    label: str,
    require_nonempty: bool,
) -> None:
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise CaptureRefusal(f"{label} must be one unique regular file")
    if observed.st_uid != os.geteuid():
        raise CaptureRefusal(f"{label} must be owned by the current user")
    if stat.S_IMODE(observed.st_mode) & 0o022:
        raise CaptureRefusal(f"{label} may not be group- or world-writable")
    if require_nonempty and observed.st_size <= 0:
        raise CaptureRefusal(f"{label} must be non-empty")


def _acquire_shared_lock(descriptor: int, *, timeout_seconds: float) -> float:
    if not 0 < timeout_seconds <= 60:
        raise CaptureRefusal("lock timeout must be greater than 0 and at most 60 seconds")
    started = time.monotonic()
    deadline = started + timeout_seconds
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return time.monotonic()
        except BlockingIOError as exc:
            if time.monotonic() >= deadline:
                raise CaptureRefusal(
                    "timed out waiting for the existing agentacct writer lock"
                ) from exc
            time.sleep(0.025)


def _write_all(descriptor: int, value: bytes) -> None:
    offset = 0
    while offset < len(value):
        written = os.write(descriptor, value[offset:])
        if written <= 0:
            raise CaptureRefusal("offline snapshot write did not make progress")
        offset += written


def _copy_exact(
    source_descriptor: int,
    destination_descriptor: int,
    *,
    size_bytes: int,
) -> str:
    os.lseek(source_descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    remaining = size_bytes
    while remaining:
        block = os.read(source_descriptor, min(COPY_CHUNK_BYTES, remaining))
        if not block:
            raise CaptureRefusal("source ledger truncated during capture")
        digest.update(block)
        _write_all(destination_descriptor, block)
        remaining -= len(block)
    if os.read(source_descriptor, 1):
        raise CaptureRefusal("source ledger grew during the locked capture")
    return digest.hexdigest()


def _hash_exact(descriptor: int, *, size_bytes: int, label: str) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    remaining = size_bytes
    while remaining:
        block = os.read(descriptor, min(COPY_CHUNK_BYTES, remaining))
        if not block:
            raise CaptureRefusal(f"{label} ended before its declared size")
        digest.update(block)
        remaining -= len(block)
    if os.read(descriptor, 1):
        raise CaptureRefusal(f"{label} exceeds its declared size")
    return digest.hexdigest()


def _publish_new_file(
    parent: Path,
    parent_descriptor: int,
    name: str,
    value: bytes,
) -> Path:
    """Create one owner-only final name without pathname rollback.

    Direct ``O_EXCL`` creation avoids a staging-name unlink on success.  Any
    later identity, content, or durability failure deliberately leaves the
    private artifact in place; deleting it by name would introduce a
    stat-then-unlink race with a same-UID replacement.
    """

    final = parent / name
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=parent_descriptor)
    except FileExistsError as exc:
        raise CaptureRefusal(f"refusing to overwrite existing {name}") from exc
    expected_identity = _device_inode(os.fstat(descriptor))
    try:
        _write_all(descriptor, value)
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
        descriptor_stat = os.fstat(descriptor)
        os.fsync(parent_descriptor)
        path_stat = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            _device_inode(descriptor_stat) != expected_identity
            or _device_inode(path_stat) != expected_identity
            or not stat.S_ISREG(descriptor_stat.st_mode)
            or not stat.S_ISREG(path_stat.st_mode)
            or descriptor_stat.st_nlink != 1
            or path_stat.st_nlink != 1
            or descriptor_stat.st_size != len(value)
            or path_stat.st_size != len(value)
            or stat.S_IMODE(descriptor_stat.st_mode) != 0o600
            or stat.S_IMODE(path_stat.st_mode) != 0o600
            or _hash_exact(
                descriptor,
                size_bytes=len(value),
                label=f"published {name}",
            )
            != hashlib.sha256(value).hexdigest()
        ):
            raise CaptureRefusal(f"published {name} identity or content changed")
        return final
    finally:
        os.close(descriptor)


def _publish_existing_file(
    parent: Path,
    parent_descriptor: int,
    source_name: str,
    final_name: str,
    *,
    expected_size: int,
    expected_sha256: str,
) -> Path:
    """Copy one already-fsynced file to a unique final name.

    The source is read through its anchored descriptor and intentionally kept
    as owner-only evidence.  The final file is a distinct ``O_EXCL`` inode, so
    success requires no path unlink; every failure retains both names rather
    than risking deletion of a same-UID replacement.
    """

    source_descriptor = os.open(
        source_name,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_descriptor,
    )
    final_descriptor: int | None = None
    try:
        source_stat = os.fstat(source_descriptor)
        source_identity = _device_inode(source_stat)
        source_path_stat = os.stat(
            source_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            _device_inode(source_path_stat) != source_identity
            or not stat.S_ISREG(source_stat.st_mode)
            or not stat.S_ISREG(source_path_stat.st_mode)
            or source_stat.st_nlink != 1
            or source_path_stat.st_nlink != 1
            or source_stat.st_size != expected_size
            or source_path_stat.st_size != expected_size
            or stat.S_IMODE(source_stat.st_mode) != 0o600
            or stat.S_IMODE(source_path_stat.st_mode) != 0o600
            or _hash_exact(
                source_descriptor,
                size_bytes=expected_size,
                label=f"staged {final_name}",
            )
            != expected_sha256
        ):
            raise CaptureRefusal(f"staged {final_name} identity or content changed")
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            final_descriptor = os.open(
                final_name,
                flags,
                0o600,
                dir_fd=parent_descriptor,
            )
        except FileExistsError as exc:
            raise CaptureRefusal(f"refusing to overwrite existing {final_name}") from exc
        final_identity = _device_inode(os.fstat(final_descriptor))
        os.lseek(source_descriptor, 0, os.SEEK_SET)
        copied_digest = hashlib.sha256()
        remaining = expected_size
        while remaining:
            block = os.read(source_descriptor, min(COPY_CHUNK_BYTES, remaining))
            if not block:
                raise CaptureRefusal(
                    f"staged {final_name} ended before its declared size"
                )
            copied_digest.update(block)
            _write_all(final_descriptor, block)
            remaining -= len(block)
        if os.read(source_descriptor, 1):
            raise CaptureRefusal(f"staged {final_name} exceeds its declared size")
        if copied_digest.hexdigest() != expected_sha256:
            raise CaptureRefusal(f"staged {final_name} content changed during publication")
        os.fsync(final_descriptor)
        os.fchmod(final_descriptor, 0o600)
        os.fsync(parent_descriptor)

        final_stat = os.fstat(final_descriptor)
        final_path_stat = os.stat(
            final_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        source_after = os.fstat(source_descriptor)
        source_path_after = os.stat(
            source_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            _device_inode(final_stat) != final_identity
            or _device_inode(final_path_stat) != final_identity
            or _device_inode(source_after) != source_identity
            or _device_inode(source_path_after) != source_identity
            or not stat.S_ISREG(final_stat.st_mode)
            or not stat.S_ISREG(final_path_stat.st_mode)
            or not stat.S_ISREG(source_after.st_mode)
            or not stat.S_ISREG(source_path_after.st_mode)
            or final_stat.st_nlink != 1
            or final_path_stat.st_nlink != 1
            or source_after.st_nlink != 1
            or source_path_after.st_nlink != 1
            or final_stat.st_size != expected_size
            or final_path_stat.st_size != expected_size
            or source_after.st_size != expected_size
            or source_path_after.st_size != expected_size
            or stat.S_IMODE(final_stat.st_mode) != 0o600
            or stat.S_IMODE(final_path_stat.st_mode) != 0o600
            or stat.S_IMODE(source_after.st_mode) != 0o600
            or stat.S_IMODE(source_path_after.st_mode) != 0o600
            or _hash_exact(
                final_descriptor,
                size_bytes=expected_size,
                label=f"published {final_name}",
            )
            != expected_sha256
            or _hash_exact(
                source_descriptor,
                size_bytes=expected_size,
                label=f"retained staged {final_name}",
            )
            != expected_sha256
        ):
            raise CaptureRefusal(
                f"published {final_name} identity or content changed; "
                "owner-only artifacts retained"
            )
        return parent / final_name
    finally:
        if final_descriptor is not None:
            os.close(final_descriptor)
        os.close(source_descriptor)


def _capture_events(
    *,
    source_root_descriptor: int,
    snapshot_root_descriptor: int,
    lock_timeout_seconds: float,
) -> dict[str, Any]:
    lock_descriptor: int | None = None
    source_descriptor: int | None = None
    destination_descriptor: int | None = None
    lock_started = 0.0
    lock_before: os.stat_result | None = None
    try:
        lock_descriptor = os.open(
            LOCK_NAME,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=source_root_descriptor,
        )
        lock_stat = os.fstat(lock_descriptor)
        _validate_regular_source(
            lock_stat,
            label="existing writer lock",
            require_nonempty=False,
        )
        lock_started = _acquire_shared_lock(
            lock_descriptor,
            timeout_seconds=lock_timeout_seconds,
        )
        lock_before = os.fstat(lock_descriptor)
        lock_path = os.stat(
            LOCK_NAME,
            dir_fd=source_root_descriptor,
            follow_symlinks=False,
        )
        if (
            _source_signature(lock_before) != _source_signature(lock_stat)
            or _source_signature(lock_path) != _source_signature(lock_stat)
        ):
            raise CaptureRefusal("existing writer lock changed before capture")

        source_descriptor = os.open(
            EVENTS_NAME,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=source_root_descriptor,
        )
        before = os.fstat(source_descriptor)
        _validate_regular_source(
            before,
            label="source events ledger",
            require_nonempty=True,
        )
        if before.st_size > MAX_LOCKED_COPY_BYTES:
            raise CaptureRefusal(
                "source events ledger exceeds the bounded locked-copy limit"
            )
        before_path = os.stat(
            EVENTS_NAME,
            dir_fd=source_root_descriptor,
            follow_symlinks=False,
        )
        if _device_inode(before_path) != _device_inode(before):
            raise CaptureRefusal("source events path changed before capture")

        try:
            destination_descriptor = os.open(
                EVENTS_NAME,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=snapshot_root_descriptor,
            )
        except FileExistsError as exc:
            raise CaptureRefusal(
                "offline snapshot events destination already exists"
            ) from exc
        copied_sha256 = _copy_exact(
            source_descriptor,
            destination_descriptor,
            size_bytes=int(before.st_size),
        )
        os.fsync(destination_descriptor)
        os.fchmod(destination_descriptor, 0o600)
        destination_stat = os.fstat(destination_descriptor)
        if (
            not stat.S_ISREG(destination_stat.st_mode)
            or destination_stat.st_nlink != 1
            or destination_stat.st_size != before.st_size
            or stat.S_IMODE(destination_stat.st_mode) != 0o600
        ):
            raise CaptureRefusal("offline destination identity or permissions changed")
        source_sha256 = _hash_exact(
            source_descriptor,
            size_bytes=int(before.st_size),
            label="source events ledger",
        )
        offline_sha256 = _hash_exact(
            destination_descriptor,
            size_bytes=int(before.st_size),
            label="offline events copy",
        )
        if len({copied_sha256, source_sha256, offline_sha256}) != 1:
            raise CaptureRefusal("source and offline snapshot hashes do not match")

        after = os.fstat(source_descriptor)
        after_path = os.stat(
            EVENTS_NAME,
            dir_fd=source_root_descriptor,
            follow_symlinks=False,
        )
        if (
            _source_signature(after) != _source_signature(before)
            or _source_signature(after_path) != _source_signature(before)
        ):
            raise CaptureRefusal("source events ledger changed during capture")

        os.fsync(snapshot_root_descriptor)
        published_path = os.stat(
            EVENTS_NAME,
            dir_fd=snapshot_root_descriptor,
            follow_symlinks=False,
        )
        published_descriptor = os.fstat(destination_descriptor)
        if (
            _device_inode(published_path) != _device_inode(destination_stat)
            or _device_inode(published_descriptor) != _device_inode(destination_stat)
            or published_path.st_nlink != 1
            or published_descriptor.st_nlink != 1
            or published_path.st_size != before.st_size
        ):
            raise CaptureRefusal("published offline events identity changed")

        lock_after = os.fstat(lock_descriptor)
        lock_after_path = os.stat(
            LOCK_NAME,
            dir_fd=source_root_descriptor,
            follow_symlinks=False,
        )
        if lock_before is None or (
            _source_signature(lock_after) != _source_signature(lock_before)
            or _source_signature(lock_after_path) != _source_signature(lock_before)
        ):
            raise CaptureRefusal("existing writer lock changed during capture")
        lock_held_seconds = time.monotonic() - lock_started
        return {
            "path": EVENTS_NAME,
            "size_bytes": int(before.st_size),
            "sha256": source_sha256,
            "source_device": int(before.st_dev),
            "source_inode": int(before.st_ino),
            "source_mtime_ns": int(before.st_mtime_ns),
            "source_ctime_ns": int(before.st_ctime_ns),
            "lock_held_seconds": lock_held_seconds,
        }
    except FileNotFoundError as exc:
        raise CaptureRefusal(
            "source store must already contain events.jsonl and events.jsonl.lock"
        ) from exc
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        if source_descriptor is not None:
            os.close(source_descriptor)
        if lock_descriptor is not None:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(lock_descriptor)


def _create_capture_tree(
    output: Path,
    output_descriptor: int,
) -> tuple[Path, str, int, tuple[int, int], int, tuple[int, int]]:
    capture_name = ""
    for _ in range(128):
        candidate = f"legacy-chronicle-capture-{secrets.token_hex(12)}"
        try:
            os.mkdir(candidate, 0o700, dir_fd=output_descriptor)
        except FileExistsError:
            continue
        capture_name = candidate
        break
    if not capture_name:
        raise CaptureRefusal("could not reserve a unique offline capture directory")

    capture_descriptor = os.open(
        capture_name,
        _directory_open_flags(),
        dir_fd=output_descriptor,
    )
    snapshot_descriptor: int | None = None
    try:
        os.fchmod(capture_descriptor, 0o700)
        capture_identity = _device_inode(os.fstat(capture_descriptor))
        _prove_child_directory(
            output_descriptor,
            capture_name,
            capture_descriptor,
            expected_identity=capture_identity,
            label="offline capture root",
        )

        os.mkdir(SNAPSHOT_DIRECTORY_NAME, 0o700, dir_fd=capture_descriptor)
        snapshot_descriptor = os.open(
            SNAPSHOT_DIRECTORY_NAME,
            _directory_open_flags(),
            dir_fd=capture_descriptor,
        )
        os.fchmod(snapshot_descriptor, 0o700)
        snapshot_identity = _device_inode(os.fstat(snapshot_descriptor))
        _prove_child_directory(
            capture_descriptor,
            SNAPSHOT_DIRECTORY_NAME,
            snapshot_descriptor,
            expected_identity=snapshot_identity,
            label="offline snapshot root",
        )

        os.mkdir(PARITY_SCRATCH_DIRECTORY_NAME, 0o700, dir_fd=capture_descriptor)
        parity_descriptor = os.open(
            PARITY_SCRATCH_DIRECTORY_NAME,
            _directory_open_flags(),
            dir_fd=capture_descriptor,
        )
        try:
            os.fchmod(parity_descriptor, 0o700)
            parity_identity = _device_inode(os.fstat(parity_descriptor))
            _prove_child_directory(
                capture_descriptor,
                PARITY_SCRATCH_DIRECTORY_NAME,
                parity_descriptor,
                expected_identity=parity_identity,
                label="offline parity scratch",
            )
        finally:
            os.close(parity_descriptor)
        os.fsync(snapshot_descriptor)
        os.fsync(capture_descriptor)
        os.fsync(output_descriptor)
        return (
            output / capture_name,
            capture_name,
            capture_descriptor,
            capture_identity,
            snapshot_descriptor,
            snapshot_identity,
        )
    except Exception:
        if snapshot_descriptor is not None:
            os.close(snapshot_descriptor)
        os.close(capture_descriptor)
        raise


def _prove_capture_paths(
    *,
    output: Path,
    output_descriptor: int,
    output_identity: tuple[int, int],
    capture_name: str,
    capture_descriptor: int,
    capture_identity: tuple[int, int],
    snapshot_descriptor: int,
    snapshot_identity: tuple[int, int],
) -> None:
    _prove_directory_path(
        output,
        output_descriptor,
        expected_identity=output_identity,
        label="--output-parent",
        owner_only=True,
    )
    _prove_child_directory(
        output_descriptor,
        capture_name,
        capture_descriptor,
        expected_identity=capture_identity,
        label="offline capture root",
    )
    _prove_child_directory(
        capture_descriptor,
        SNAPSHOT_DIRECTORY_NAME,
        snapshot_descriptor,
        expected_identity=snapshot_identity,
        label="offline snapshot root",
    )


def capture_snapshot(
    *,
    source_store_root: Path | str,
    output_parent: Path | str,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Capture and verify one exact live-ledger version without live writes."""

    source = _require_source_store(source_store_root)
    output = _require_output_parent(output_parent, source=source)
    source_descriptor, source_identity = _open_anchored_directory(
        source,
        label="--source-store-root",
        owner_only=False,
    )
    output_descriptor: int | None = None
    capture_descriptor: int | None = None
    snapshot_descriptor: int | None = None
    try:
        output_descriptor, output_identity = _open_anchored_directory(
            output,
            label="--output-parent",
            owner_only=True,
        )
        # Re-prove the source/output relationship after both directory fds are
        # open and before the first mkdir.  A same-UID rename must not be able
        # to move the approved source inode under the output name and turn an
        # offline write into a live-store write.
        if output_identity == source_identity:
            raise CaptureRefusal("--output-parent resolved to the source store inode")
        _prove_directory_path(
            source,
            source_descriptor,
            expected_identity=source_identity,
            label="--source-store-root",
            owner_only=False,
        )
        try:
            reject_live_state_overlap(output, label="--output-parent")
        except LivePathSafetyError as exc:
            raise CaptureRefusal(str(exc)) from exc
        if paths_overlap(output, source):
            raise CaptureRefusal("--output-parent must be disjoint from the source store")
        (
            capture_root,
            capture_name,
            capture_descriptor,
            capture_identity,
            snapshot_descriptor,
            snapshot_identity,
        ) = _create_capture_tree(output, output_descriptor)
        snapshot_root = capture_root / SNAPSHOT_DIRECTORY_NAME
        parity_scratch = capture_root / PARITY_SCRATCH_DIRECTORY_NAME
        try:
            reject_live_state_overlap(capture_root, label="offline capture root")
        except LivePathSafetyError as exc:
            raise CaptureRefusal(str(exc)) from exc
        _prove_directory_path(
            source,
            source_descriptor,
            expected_identity=source_identity,
            label="--source-store-root",
            owner_only=False,
        )
        _prove_capture_paths(
            output=output,
            output_descriptor=output_descriptor,
            output_identity=output_identity,
            capture_name=capture_name,
            capture_descriptor=capture_descriptor,
            capture_identity=capture_identity,
            snapshot_descriptor=snapshot_descriptor,
            snapshot_identity=snapshot_identity,
        )

        captured_at = _utc_now()
        entry = _capture_events(
            source_root_descriptor=source_descriptor,
            snapshot_root_descriptor=snapshot_descriptor,
            lock_timeout_seconds=lock_timeout_seconds,
        )
        _prove_directory_path(
            source,
            source_descriptor,
            expected_identity=source_identity,
            label="--source-store-root",
            owner_only=False,
        )
        _prove_capture_paths(
            output=output,
            output_descriptor=output_descriptor,
            output_identity=output_identity,
            capture_name=capture_name,
            capture_descriptor=capture_descriptor,
            capture_identity=capture_identity,
            snapshot_descriptor=snapshot_descriptor,
            snapshot_identity=snapshot_identity,
        )

        manifest_bytes = (
            json.dumps(
                {
                    "version": SNAPSHOT_MANIFEST_VERSION,
                    "kind": MANIFEST_KIND,
                    "files": [
                        {
                            "path": entry["path"],
                            "size_bytes": entry["size_bytes"],
                            "sha256": entry["sha256"],
                        }
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        staged_manifest_name = (
            f".{MANIFEST_NAME}.verified-{os.getpid()}-{secrets.token_hex(8)}"
        )
        staged_manifest_path = _publish_new_file(
            capture_root,
            capture_descriptor,
            staged_manifest_name,
            manifest_bytes,
        )

        _prove_capture_paths(
            output=output,
            output_descriptor=output_descriptor,
            output_identity=output_identity,
            capture_name=capture_name,
            capture_descriptor=capture_descriptor,
            capture_identity=capture_identity,
            snapshot_descriptor=snapshot_descriptor,
            snapshot_identity=snapshot_identity,
        )
        verified = VerifiedSnapshot.verify(snapshot_root, staged_manifest_path)
        verified.verify_unchanged()
        _prove_capture_paths(
            output=output,
            output_descriptor=output_descriptor,
            output_identity=output_identity,
            capture_name=capture_name,
            capture_descriptor=capture_descriptor,
            capture_identity=capture_identity,
            snapshot_descriptor=snapshot_descriptor,
            snapshot_identity=snapshot_identity,
        )
        if verified.manifest.digest_sha256 != manifest_sha256:
            raise CaptureRefusal("staged manifest digest changed during verification")

        final_manifest_path = capture_root / MANIFEST_NAME
        attestation = {
            "schema_version": "agent-chronicle.legacy-snapshot-capture.v1",
            "status": "verified",
            "captured_at": captured_at,
            "source_store_root": str(source),
            "source_open_mode": "read-only-no-follow",
            "writer_coordination": "existing-events-lock-shared",
            "watcher_stopped": False,
            "live_content_modified": False,
            "snapshot_root": str(snapshot_root),
            "manifest": str(final_manifest_path),
            "manifest_sha256": manifest_sha256,
            "parity_scratch": str(parity_scratch),
            "file_count": len(verified.files),
            "total_size_bytes": sum(item.size_bytes for item in verified.files),
            "source_file_sha256": entry["sha256"],
            "source_device": entry["source_device"],
            "source_inode": entry["source_inode"],
            "source_mtime_ns": entry["source_mtime_ns"],
            "source_ctime_ns": entry["source_ctime_ns"],
            "lock_held_seconds": entry["lock_held_seconds"],
            "verified_before_handoff": True,
            "completion_marker": MANIFEST_NAME,
        }
        attestation_bytes = (
            json.dumps(attestation, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        attestation_path = _publish_new_file(
            capture_root,
            capture_descriptor,
            ATTESTATION_NAME,
            attestation_bytes,
        )
        manifest_path = _publish_existing_file(
            capture_root,
            capture_descriptor,
            staged_manifest_path.name,
            MANIFEST_NAME,
            expected_size=len(manifest_bytes),
            expected_sha256=manifest_sha256,
        )
        try:
            final_verified = VerifiedSnapshot.verify(snapshot_root, manifest_path)
            final_verified.verify_unchanged()
            _prove_capture_paths(
                output=output,
                output_descriptor=output_descriptor,
                output_identity=output_identity,
                capture_name=capture_name,
                capture_descriptor=capture_descriptor,
                capture_identity=capture_identity,
                snapshot_descriptor=snapshot_descriptor,
                snapshot_identity=snapshot_identity,
            )
            if final_verified.manifest.digest_sha256 != manifest_sha256:
                raise CaptureRefusal("final manifest digest changed after publication")
        except Exception as exc:
            raise CaptureRefusal(
                "final manifest verification failed; retained owner-only capture artifacts"
            ) from exc
        return {
            **attestation,
            "manifest": str(manifest_path),
            "attestation": str(attestation_path),
        }
    finally:
        if snapshot_descriptor is not None:
            os.close(snapshot_descriptor)
        if capture_descriptor is not None:
            os.close(capture_descriptor)
        if output_descriptor is not None:
            os.close(output_descriptor)
        os.close(source_descriptor)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-store-root", required=True)
    parser.add_argument("--output-parent", required=True)
    parser.add_argument(
        "--lock-timeout-seconds",
        type=float,
        default=DEFAULT_LOCK_TIMEOUT_SECONDS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = capture_snapshot(
            source_store_root=arguments.source_store_root,
            output_parent=arguments.output_parent,
            lock_timeout_seconds=arguments.lock_timeout_seconds,
        )
    except (CaptureRefusal, LivePathSafetyError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "refused", "error": str(exc)},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
