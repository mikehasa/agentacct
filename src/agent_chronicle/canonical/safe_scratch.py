"""Descriptor-anchored private run directories for offline spike tools.

The offline benchmark and parity runners accept a caller-owned scratch root.
Path validation alone is not enough: the name could be exchanged after it is
checked but before a pathname-based temporary-directory creator opens it.  This
module keeps the validated root open, creates the run directory with ``mkdirat``
semantics, and exposes only descriptor-relative file operations for artifacts
created by the runners themselves.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .live_paths import LivePathSafetyError, reject_live_state_overlap
from .snapshot import SnapshotSafetyError


_UNIQUE_DIRECTORY_ATTEMPTS = 128


class ScratchSafetyError(SnapshotSafetyError):
    """A scratch path or anchored run-directory identity became unsafe."""


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _identity(observed: os.stat_result) -> tuple[int, int]:
    return int(observed.st_dev), int(observed.st_ino)


def _assert_direct_name(name: str, *, label: str) -> None:
    if not name or name in {".", ".."} or Path(name).name != name:
        raise ScratchSafetyError(f"{label} must be one direct child name")


def _assert_no_symlink_components(path: Path, *, label: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            observed = current.lstat()
        except OSError as exc:
            raise ScratchSafetyError(f"{label} must remain an existing directory") from exc
        if stat.S_ISLNK(observed.st_mode):
            raise ScratchSafetyError(f"{label} may not contain symlink components")


def _validate_directory(
    observed: os.stat_result,
    *,
    label: str,
    owner_only: bool,
) -> None:
    if not stat.S_ISDIR(observed.st_mode):
        raise ScratchSafetyError(f"{label} must remain a directory")
    if owner_only:
        if (
            observed.st_uid != os.geteuid()
            or stat.S_IMODE(observed.st_mode) != 0o700
        ):
            raise ScratchSafetyError(
                f"{label} must remain owner-owned with exact mode 0700"
            )


def _reject_live(path: Path, *, label: str) -> None:
    try:
        reject_live_state_overlap(path, label=label)
    except LivePathSafetyError as exc:
        raise ScratchSafetyError(str(exc)) from exc


def _prove_open_directory(
    path: Path,
    descriptor: int,
    *,
    expected_identity: tuple[int, int],
    label: str,
    owner_only: bool,
) -> None:
    if not path.is_absolute():
        raise ScratchSafetyError(f"{label} must remain an absolute path")
    _reject_live(path, label=label)
    _assert_no_symlink_components(path, label=label)
    try:
        resolved = path.resolve(strict=True)
        path_stat = path.stat(follow_symlinks=False)
        descriptor_stat = os.fstat(descriptor)
    except (OSError, RuntimeError) as exc:
        raise ScratchSafetyError(f"{label} cannot be re-proved safely") from exc
    if resolved != path:
        raise ScratchSafetyError(f"{label} resolved path changed")
    _reject_live(resolved, label=label)
    _validate_directory(path_stat, label=label, owner_only=owner_only)
    _validate_directory(descriptor_stat, label=label, owner_only=owner_only)
    if (
        _identity(path_stat) != expected_identity
        or _identity(descriptor_stat) != expected_identity
    ):
        raise ScratchSafetyError(f"{label} path identity changed")


@dataclass
class AnchoredRunDirectory:
    """A private run directory pinned to validated directory descriptors."""

    path: Path
    descriptor: int
    identity: tuple[int, int]
    scratch_root: Path
    scratch_descriptor: int
    scratch_identity: tuple[int, int]
    require_private_root: bool = False
    _closed: bool = False

    def __enter__(self) -> "AnchoredRunDirectory":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self.descriptor)
        finally:
            os.close(self.scratch_descriptor)

    def prove_unchanged(self) -> None:
        if self._closed:
            raise ScratchSafetyError("per-run scratch directory is closed")
        _prove_open_directory(
            self.scratch_root,
            self.scratch_descriptor,
            expected_identity=self.scratch_identity,
            label="--scratch-root",
            owner_only=self.require_private_root,
        )
        _prove_open_directory(
            self.path,
            self.descriptor,
            expected_identity=self.identity,
            label="per-run scratch directory",
            owner_only=True,
        )

    def child_path(self, name: str) -> Path:
        _assert_direct_name(name, label="run artifact")
        self.prove_unchanged()
        return self.path / name

    def open_file(self, name: str, flags: int, mode: int = 0o600) -> int:
        """Open one direct child relative to the pinned run-directory inode."""

        _assert_direct_name(name, label="run artifact")
        self.prove_unchanged()
        try:
            descriptor = os.open(
                name,
                flags
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                mode,
                dir_fd=self.descriptor,
            )
        except OSError:
            self.prove_unchanged()
            raise
        try:
            self.prove_unchanged()
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def stat_child(self, name: str) -> os.stat_result:
        _assert_direct_name(name, label="run artifact")
        self.prove_unchanged()
        observed = os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
        self.prove_unchanged()
        return observed

    def atomic_write_json(self, name: str, value: Mapping[str, Any]) -> None:
        """Publish one private JSON file without reopening the run path."""

        _assert_direct_name(name, label="JSON artifact")
        encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
        temporary_name = f".{name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
        descriptor: int | None = None
        try:
            descriptor = self.open_file(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                descriptor = None
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            self.prove_unchanged()
            os.replace(
                temporary_name,
                name,
                src_dir_fd=self.descriptor,
                dst_dir_fd=self.descriptor,
            )
            os.fsync(self.descriptor)
            self.prove_unchanged()
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary_name, dir_fd=self.descriptor)
            except FileNotFoundError:
                pass


def create_anchored_run_directory(
    scratch_root: Path,
    *,
    prefix: str,
    require_private_root: bool = False,
) -> AnchoredRunDirectory:
    """Create a random owner-only child while pinning the validated root."""

    if not scratch_root.is_absolute():
        raise ScratchSafetyError("--scratch-root must remain an absolute path")
    if not prefix or "/" in prefix or prefix in {".", ".."}:
        raise ScratchSafetyError("run-directory prefix must be a direct name prefix")
    if not isinstance(require_private_root, bool):
        raise ScratchSafetyError("require_private_root must be a boolean")

    _reject_live(scratch_root, label="--scratch-root")
    _assert_no_symlink_components(scratch_root, label="--scratch-root")
    try:
        before = scratch_root.stat(follow_symlinks=False)
        resolved = scratch_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ScratchSafetyError("--scratch-root cannot be opened safely") from exc
    if resolved != scratch_root:
        raise ScratchSafetyError("--scratch-root resolved path changed")
    _reject_live(resolved, label="--scratch-root")
    _validate_directory(
        before,
        label="--scratch-root",
        owner_only=require_private_root,
    )
    expected_root_identity = _identity(before)

    try:
        scratch_descriptor = os.open(scratch_root, _directory_open_flags())
    except OSError as exc:
        raise ScratchSafetyError("--scratch-root cannot be opened safely") from exc
    run_descriptor: int | None = None
    try:
        _prove_open_directory(
            scratch_root,
            scratch_descriptor,
            expected_identity=expected_root_identity,
            label="--scratch-root",
            owner_only=require_private_root,
        )
        run_name = ""
        for _ in range(_UNIQUE_DIRECTORY_ATTEMPTS):
            candidate = f"{prefix}{secrets.token_hex(12)}"
            try:
                os.mkdir(candidate, 0o700, dir_fd=scratch_descriptor)
            except FileExistsError:
                continue
            run_name = candidate
            break
        if not run_name:
            raise ScratchSafetyError("could not reserve a unique per-run scratch directory")

        created = os.stat(
            run_name,
            dir_fd=scratch_descriptor,
            follow_symlinks=False,
        )
        _validate_directory(
            created,
            label="per-run scratch directory",
            owner_only=True,
        )
        created_identity = _identity(created)
        run_descriptor = os.open(
            run_name,
            _directory_open_flags(),
            dir_fd=scratch_descriptor,
        )
        os.fchmod(run_descriptor, 0o700)
        run_identity = _identity(os.fstat(run_descriptor))
        if run_identity != created_identity:
            raise ScratchSafetyError("per-run scratch directory identity changed")
        run_path = scratch_root / run_name
        anchored = AnchoredRunDirectory(
            path=run_path,
            descriptor=run_descriptor,
            identity=run_identity,
            scratch_root=scratch_root,
            scratch_descriptor=scratch_descriptor,
            scratch_identity=expected_root_identity,
            require_private_root=require_private_root,
        )
        anchored.prove_unchanged()
        return anchored
    except BaseException:
        if run_descriptor is not None:
            os.close(run_descriptor)
        os.close(scratch_descriptor)
        raise


__all__ = [
    "AnchoredRunDirectory",
    "ScratchSafetyError",
    "create_anchored_run_directory",
]
