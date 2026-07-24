#!/usr/bin/env python3
"""Seal an already-created offline Codex copy for read-only benchmarks.

This command never discovers or copies a client store.  It accepts one
explicit snapshot root, rejects live agentacct/Codex paths and runtime files,
hashes the exact allow-listed inventory, writes a manifest outside the root,
and then verifies the result through the canonical ``VerifiedSnapshot``
boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from agent_chronicle.canonical.live_paths import (
    LivePathSafetyError,
    reject_live_state_overlap,
)
from agent_chronicle.canonical.snapshot import (
    MAX_MANIFEST_FILES,
    SNAPSHOT_MANIFEST_VERSION,
    SnapshotSafetyError,
    VerifiedSnapshot,
)


_HASH_CHUNK_BYTES = 8 * 1024 * 1024
_FORBIDDEN_STATE_SEQUENCES = (
    (".agent-sentinel", "state"),
    (".agent-sentinel-global", "state"),
    (".agent-chronicle", "state"),
    (".agent-chronicle-global", "state"),
)


class SealRefusal(SnapshotSafetyError):
    """The requested root or manifest target is not safe to seal."""


@dataclass(frozen=True, slots=True)
class _ObservedFile:
    """One payload proven through descriptors anchored below the root fd."""

    relative_path: str
    size_bytes: int
    sha256: str
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int

    def manifest_entry(self) -> dict[str, Any]:
        return {
            "path": self.relative_path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


def _require_absolute(value: str, *, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SealRefusal(f"{label} must be an explicit absolute path")
    return path


def _assert_no_symlink_components(path: Path, *, label: str) -> os.stat_result:
    current = Path(path.anchor)
    try:
        observed = current.lstat()
    except OSError as exc:
        raise SealRefusal(f"{label} does not exist") from exc
    for part in path.parts[1:]:
        current = current / part
        try:
            observed = current.lstat()
        except OSError as exc:
            raise SealRefusal(f"{label} does not exist") from exc
        if stat.S_ISLNK(observed.st_mode):
            raise SealRefusal(f"{label} may not contain symlink components")
    return observed


def _parts_contain_sequence(parts: tuple[str, ...], sequence: tuple[str, ...]) -> bool:
    folded = tuple(part.casefold() for part in parts)
    target = tuple(part.casefold() for part in sequence)
    width = len(target)
    return any(
        folded[index : index + width] == target
        for index in range(len(folded) - width + 1)
    )


def _reject_live_state(path: Path, *, label: str) -> None:
    parts = path.resolve(strict=False).parts
    for sequence in _FORBIDDEN_STATE_SEQUENCES:
        if _parts_contain_sequence(parts, sequence):
            raise SealRefusal(f"{label} may not be inside live agentacct state")
    resolved = path.resolve(strict=False)
    live_roots = {(Path.home() / ".codex").resolve(strict=False)}
    configured = os.environ.get("CODEX_HOME")
    if configured:
        configured_root = Path(configured).expanduser()
        if not configured_root.is_absolute():
            configured_root = Path.cwd() / configured_root
        live_roots.add(configured_root.resolve(strict=False))
    for live_codex in live_roots:
        if resolved == live_codex or resolved.is_relative_to(live_codex):
            raise SealRefusal(f"{label} must be an offline copy, not live Codex state")
    try:
        reject_live_state_overlap(path, label=label)
    except LivePathSafetyError as exc:
        raise SealRefusal(str(exc)) from exc


def _device_inode(observed: os.stat_result) -> tuple[int, int]:
    return int(observed.st_dev), int(observed.st_ino)


def _stable_signature(observed: os.stat_result) -> tuple[int, ...]:
    return (
        int(observed.st_dev),
        int(observed.st_ino),
        int(observed.st_size),
        int(observed.st_mtime_ns),
        int(observed.st_ctime_ns),
    )


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
        raise SealRefusal(f"{label} must be a directory")
    if observed.st_uid != os.geteuid():
        raise SealRefusal(f"{label} must be owned by the current user")
    mode = stat.S_IMODE(observed.st_mode)
    if owner_only and mode & 0o077:
        raise SealRefusal(f"{label} must be owner-only")
    if not owner_only and mode & 0o022:
        raise SealRefusal(f"{label} may not be group- or world-writable")


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
    try:
        path_stat = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise SealRefusal(f"{label} path identity changed") from exc
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
        raise SealRefusal(f"{label} path identity changed")


def _open_anchored_directory(
    path: Path,
    *,
    label: str,
    owner_only: bool,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, tuple[int, int]]:
    try:
        descriptor = os.open(path, _directory_open_flags())
    except OSError as exc:
        raise SealRefusal(f"{label} cannot be opened safely") from exc
    try:
        identity = _device_inode(os.fstat(descriptor))
        if expected_identity is not None and identity != expected_identity:
            raise SealRefusal(f"{label} path identity changed before open")
        _prove_directory_path(
            path,
            descriptor,
            expected_identity=identity,
            label=label,
            owner_only=owner_only,
        )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, identity


def _codex_path_allowed(relative_path: str) -> bool:
    pure = PurePosixPath(relative_path)
    if len(pure.parts) == 1:
        return pure.name == "session_index.jsonl"
    return (
        pure.parts[0] == "sessions"
        and pure.name.startswith("rollout-")
        and pure.suffix == ".jsonl"
    )


def _hash_open_file(descriptor: int, *, relative_path: str) -> _ObservedFile:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise SealRefusal(
            f"snapshot file is not a unique regular file: {relative_path}"
        )
    digest = hashlib.sha256()
    size = 0
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        block = os.read(descriptor, _HASH_CHUNK_BYTES)
        if not block:
            break
        digest.update(block)
        size += len(block)
    after = os.fstat(descriptor)
    if _stable_signature(before) != _stable_signature(after) or size != before.st_size:
        raise SealRefusal(f"snapshot file changed while hashing: {relative_path}")
    return _ObservedFile(
        relative_path=relative_path,
        size_bytes=size,
        sha256=digest.hexdigest(),
        device=int(before.st_dev),
        inode=int(before.st_ino),
        mtime_ns=int(before.st_mtime_ns),
        ctime_ns=int(before.st_ctime_ns),
    )


def _scan_snapshot_directory(
    directory_descriptor: int,
    *,
    relative_parts: tuple[str, ...],
    files: list[_ObservedFile],
) -> None:
    directory_before = os.fstat(directory_descriptor)
    try:
        with os.scandir(directory_descriptor) as entries:
            names = sorted(entry.name for entry in entries)
    except OSError as exc:
        location = PurePosixPath(*relative_parts).as_posix() or "."
        raise SealRefusal(f"cannot scan snapshot directory {location}: {exc}") from exc

    for name in names:
        child_parts = (*relative_parts, name)
        relative = PurePosixPath(*child_parts).as_posix()
        try:
            path_before = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise SealRefusal(f"cannot inspect snapshot path: {relative}") from exc
        if stat.S_ISLNK(path_before.st_mode):
            raise SealRefusal(f"snapshot contains a symlink: {relative}")
        if stat.S_ISDIR(path_before.st_mode):
            if relative == "runtime" or relative.startswith("runtime/"):
                raise SealRefusal("snapshot contains a runtime directory")
            try:
                child_descriptor = os.open(
                    name,
                    _directory_open_flags(),
                    dir_fd=directory_descriptor,
                )
            except OSError as exc:
                raise SealRefusal(
                    f"cannot open snapshot directory safely: {relative}"
                ) from exc
            try:
                opened = os.fstat(child_descriptor)
                if not stat.S_ISDIR(opened.st_mode) or _device_inode(
                    opened
                ) != _device_inode(path_before):
                    raise SealRefusal(
                        f"snapshot directory identity changed: {relative}"
                    )
                _scan_snapshot_directory(
                    child_descriptor,
                    relative_parts=child_parts,
                    files=files,
                )
                path_after = os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if _stable_signature(os.fstat(child_descriptor)) != _stable_signature(
                    opened
                ) or _device_inode(path_after) != _device_inode(opened):
                    raise SealRefusal(
                        f"snapshot directory identity changed: {relative}"
                    )
            finally:
                os.close(child_descriptor)
            continue
        if not stat.S_ISREG(path_before.st_mode):
            raise SealRefusal(f"snapshot contains a non-regular file: {relative}")
        if not _codex_path_allowed(relative):
            raise SealRefusal(f"snapshot contains a non-source Codex file: {relative}")

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=directory_descriptor)
        except OSError as exc:
            raise SealRefusal(f"cannot open snapshot file safely: {relative}") from exc
        try:
            opened = os.fstat(descriptor)
            if _device_inode(opened) != _device_inode(path_before):
                raise SealRefusal(f"snapshot file identity changed: {relative}")
            observed = _hash_open_file(descriptor, relative_path=relative)
            path_after = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if _stable_signature(path_after) != _stable_signature(opened):
                raise SealRefusal(f"snapshot file identity changed: {relative}")
        finally:
            os.close(descriptor)
        files.append(observed)
        if len(files) > MAX_MANIFEST_FILES:
            raise SealRefusal("snapshot exceeds the canonical manifest file limit")

    if _stable_signature(os.fstat(directory_descriptor)) != _stable_signature(
        directory_before
    ):
        location = PurePosixPath(*relative_parts).as_posix() or "."
        raise SealRefusal(f"snapshot directory changed while scanning: {location}")


def _inventory_anchored(root_descriptor: int) -> tuple[_ObservedFile, ...]:
    files: list[_ObservedFile] = []
    _scan_snapshot_directory(root_descriptor, relative_parts=(), files=files)
    if not files:
        raise SealRefusal("snapshot must contain at least one source file")
    return tuple(sorted(files, key=lambda item: item.relative_path))


def _write_all(descriptor: int, value: bytes) -> None:
    offset = 0
    while offset < len(value):
        written = os.write(descriptor, value[offset:])
        if written <= 0:
            raise SealRefusal("manifest write did not make progress")
        offset += written


def _hash_exact(descriptor: int, *, size_bytes: int, label: str) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    remaining = size_bytes
    while remaining:
        block = os.read(descriptor, min(_HASH_CHUNK_BYTES, remaining))
        if not block:
            raise SealRefusal(f"{label} ended before its declared size")
        digest.update(block)
        remaining -= len(block)
    if os.read(descriptor, 1):
        raise SealRefusal(f"{label} exceeds its declared size")
    return digest.hexdigest()


def _prove_manifest_file(
    parent_descriptor: int,
    name: str,
    descriptor: int,
    *,
    expected_identity: tuple[int, int],
    expected_size: int,
    expected_sha256: str,
    label: str,
) -> None:
    descriptor_stat = os.fstat(descriptor)
    try:
        path_stat = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise SealRefusal(f"{label} path identity changed") from exc
    if (
        _device_inode(descriptor_stat) != expected_identity
        or _device_inode(path_stat) != expected_identity
        or not stat.S_ISREG(descriptor_stat.st_mode)
        or not stat.S_ISREG(path_stat.st_mode)
        or descriptor_stat.st_uid != os.geteuid()
        or path_stat.st_uid != os.geteuid()
        or descriptor_stat.st_nlink != 1
        or path_stat.st_nlink != 1
        or descriptor_stat.st_size != expected_size
        or path_stat.st_size != expected_size
        or stat.S_IMODE(descriptor_stat.st_mode) != 0o600
        or stat.S_IMODE(path_stat.st_mode) != 0o600
        or _hash_exact(
            descriptor,
            size_bytes=expected_size,
            label=label,
        )
        != expected_sha256
    ):
        raise SealRefusal(f"{label} identity, permissions, or content changed")


def _unlink_if_identity(
    parent_descriptor: int,
    name: str,
    *,
    expected_identity: tuple[int, int],
) -> bool:
    try:
        observed = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return True
    if _device_inode(observed) != expected_identity:
        return False
    os.unlink(name, dir_fd=parent_descriptor)
    os.fsync(parent_descriptor)
    return True


def _stage_manifest(
    parent_descriptor: int,
    *,
    final_name: str,
    value: bytes,
) -> tuple[str, int, tuple[int, int]]:
    temporary_name = f".{final_name}.staged-{os.getpid()}-{secrets.token_hex(8)}"
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(
        temporary_name,
        flags,
        0o600,
        dir_fd=parent_descriptor,
    )
    identity = _device_inode(os.fstat(descriptor))
    try:
        _write_all(descriptor, value)
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
        _prove_manifest_file(
            parent_descriptor,
            temporary_name,
            descriptor,
            expected_identity=identity,
            expected_size=len(value),
            expected_sha256=hashlib.sha256(value).hexdigest(),
            label="staged manifest",
        )
    except BaseException:
        os.close(descriptor)
        try:
            _unlink_if_identity(
                parent_descriptor,
                temporary_name,
                expected_identity=identity,
            )
        except OSError:
            pass
        raise
    return temporary_name, descriptor, identity


def _verify_complete_snapshot(
    *,
    root: Path,
    root_descriptor: int,
    root_identity: tuple[int, int],
    manifest_parent: Path,
    manifest_parent_descriptor: int,
    manifest_parent_identity: tuple[int, int],
    manifest_path: Path,
    manifest_name: str,
    manifest_descriptor: int,
    manifest_identity: tuple[int, int],
    manifest_bytes: bytes,
    expected_files: tuple[_ObservedFile, ...],
) -> VerifiedSnapshot:
    expected_digest = hashlib.sha256(manifest_bytes).hexdigest()
    _prove_directory_path(
        root,
        root_descriptor,
        expected_identity=root_identity,
        label="--snapshot-root",
        owner_only=False,
    )
    _prove_directory_path(
        manifest_parent,
        manifest_parent_descriptor,
        expected_identity=manifest_parent_identity,
        label="--manifest parent",
        owner_only=True,
    )
    _prove_manifest_file(
        manifest_parent_descriptor,
        manifest_name,
        manifest_descriptor,
        expected_identity=manifest_identity,
        expected_size=len(manifest_bytes),
        expected_sha256=expected_digest,
        label="manifest",
    )
    if _inventory_anchored(root_descriptor) != expected_files:
        raise SealRefusal("snapshot changed after its manifest was staged")

    # Keep the canonical verifier in the boundary, but surround its pathname
    # API with fd/path identity proofs and compare all returned file identities
    # with the independently anchored inventory.
    verified = VerifiedSnapshot.verify(root, manifest_path)
    verified.verify_unchanged()

    _prove_directory_path(
        root,
        root_descriptor,
        expected_identity=root_identity,
        label="--snapshot-root",
        owner_only=False,
    )
    _prove_directory_path(
        manifest_parent,
        manifest_parent_descriptor,
        expected_identity=manifest_parent_identity,
        label="--manifest parent",
        owner_only=True,
    )
    _prove_manifest_file(
        manifest_parent_descriptor,
        manifest_name,
        manifest_descriptor,
        expected_identity=manifest_identity,
        expected_size=len(manifest_bytes),
        expected_sha256=expected_digest,
        label="manifest",
    )
    if _inventory_anchored(root_descriptor) != expected_files:
        raise SealRefusal("snapshot changed during canonical verification")
    canonical_files = tuple(
        (
            item.relative_path,
            item.size_bytes,
            item.sha256,
            int(item.device),
            int(item.inode),
            int(item.mtime_ns),
            int(item.ctime_ns),
        )
        for item in verified.files
    )
    anchored_files = tuple(
        (
            item.relative_path,
            item.size_bytes,
            item.sha256,
            item.device,
            item.inode,
            item.mtime_ns,
            item.ctime_ns,
        )
        for item in expected_files
    )
    if (
        verified.manifest.digest_sha256 != expected_digest
        or canonical_files != anchored_files
    ):
        raise SealRefusal("canonical verification did not match anchored evidence")
    return verified


def _publish_final_manifest(
    parent_descriptor: int,
    *,
    staged_name: str,
    final_name: str,
    manifest_descriptor: int,
    manifest_identity: tuple[int, int],
    expected_size: int,
    expected_sha256: str,
) -> None:
    linked = False
    try:
        try:
            os.link(
                staged_name,
                final_name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise SealRefusal("--manifest must name a new file") from exc
        linked = True
        os.fsync(parent_descriptor)
        final_stat = os.stat(
            final_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        staged_stat = os.stat(
            staged_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            _device_inode(final_stat) != manifest_identity
            or _device_inode(staged_stat) != manifest_identity
            or _device_inode(os.fstat(manifest_descriptor)) != manifest_identity
        ):
            raise SealRefusal("manifest identity changed during publication")
        if not _unlink_if_identity(
            parent_descriptor,
            staged_name,
            expected_identity=manifest_identity,
        ):
            raise SealRefusal("staged manifest path changed during publication")
        _prove_manifest_file(
            parent_descriptor,
            final_name,
            manifest_descriptor,
            expected_identity=manifest_identity,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            label="published manifest",
        )
    except BaseException:
        if linked:
            try:
                removed = _unlink_if_identity(
                    parent_descriptor,
                    final_name,
                    expected_identity=manifest_identity,
                )
            except OSError as rollback_exc:
                raise SealRefusal(
                    "could not roll back incomplete manifest publication"
                ) from rollback_exc
            if not removed:
                raise SealRefusal(
                    "could not safely roll back incomplete manifest publication"
                )
        raise


def seal_snapshot(root_text: str, manifest_text: str) -> dict[str, Any]:
    root = _require_absolute(root_text, label="--snapshot-root")
    manifest = _require_absolute(manifest_text, label="--manifest")
    _reject_live_state(root, label="--snapshot-root")
    _reject_live_state(manifest, label="--manifest")
    root_lexical_stat = _assert_no_symlink_components(
        root,
        label="--snapshot-root",
    )
    _validate_directory_stat(
        root_lexical_stat,
        label="--snapshot-root",
        owner_only=False,
    )
    root_lexical_identity = _device_inode(root_lexical_stat)
    root = root.resolve(strict=True)
    _reject_live_state(root, label="--snapshot-root")
    if _device_inode(root.stat(follow_symlinks=False)) != root_lexical_identity:
        raise SealRefusal("--snapshot-root path identity changed during resolution")
    manifest_parent = manifest.parent
    manifest_parent_lexical_stat = _assert_no_symlink_components(
        manifest_parent,
        label="--manifest parent",
    )
    _validate_directory_stat(
        manifest_parent_lexical_stat,
        label="--manifest parent",
        owner_only=True,
    )
    manifest_parent_lexical_identity = _device_inode(manifest_parent_lexical_stat)
    manifest_parent = manifest_parent.resolve(strict=True)
    manifest = manifest_parent / manifest.name
    _reject_live_state(manifest, label="--manifest")
    if (
        _device_inode(manifest_parent.stat(follow_symlinks=False))
        != manifest_parent_lexical_identity
    ):
        raise SealRefusal("--manifest parent path identity changed during resolution")
    if manifest == root or manifest.is_relative_to(root):
        raise SealRefusal("--manifest must be outside the snapshot root")

    root_descriptor: int | None = None
    manifest_parent_descriptor: int | None = None
    staged_descriptor: int | None = None
    staged_name: str | None = None
    staged_identity: tuple[int, int] | None = None
    try:
        root_descriptor, root_identity = _open_anchored_directory(
            root,
            label="--snapshot-root",
            owner_only=False,
            expected_identity=root_lexical_identity,
        )
        manifest_parent_descriptor, manifest_parent_identity = _open_anchored_directory(
            manifest_parent,
            label="--manifest parent",
            owner_only=True,
            expected_identity=manifest_parent_lexical_identity,
        )
        if root_identity == manifest_parent_identity:
            raise SealRefusal("--manifest must be outside the snapshot root")
        _reject_live_state(root, label="--snapshot-root")
        _reject_live_state(manifest, label="--manifest")
        _prove_directory_path(
            root,
            root_descriptor,
            expected_identity=root_identity,
            label="--snapshot-root",
            owner_only=False,
        )
        _prove_directory_path(
            manifest_parent,
            manifest_parent_descriptor,
            expected_identity=manifest_parent_identity,
            label="--manifest parent",
            owner_only=True,
        )
        try:
            os.stat(
                manifest.name,
                dir_fd=manifest_parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise SealRefusal("--manifest must name a new file")

        expected_files = _inventory_anchored(root_descriptor)
        _prove_directory_path(
            root,
            root_descriptor,
            expected_identity=root_identity,
            label="--snapshot-root",
            owner_only=False,
        )
        encoded = (
            json.dumps(
                {
                    "version": SNAPSHOT_MANIFEST_VERSION,
                    "kind": "codex",
                    "files": [item.manifest_entry() for item in expected_files],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        manifest_sha256 = hashlib.sha256(encoded).hexdigest()
        staged_name, staged_descriptor, staged_identity = _stage_manifest(
            manifest_parent_descriptor,
            final_name=manifest.name,
            value=encoded,
        )
        staged_path = manifest_parent / staged_name
        _verify_complete_snapshot(
            root=root,
            root_descriptor=root_descriptor,
            root_identity=root_identity,
            manifest_parent=manifest_parent,
            manifest_parent_descriptor=manifest_parent_descriptor,
            manifest_parent_identity=manifest_parent_identity,
            manifest_path=staged_path,
            manifest_name=staged_name,
            manifest_descriptor=staged_descriptor,
            manifest_identity=staged_identity,
            manifest_bytes=encoded,
            expected_files=expected_files,
        )

        _publish_final_manifest(
            manifest_parent_descriptor,
            staged_name=staged_name,
            final_name=manifest.name,
            manifest_descriptor=staged_descriptor,
            manifest_identity=staged_identity,
            expected_size=len(encoded),
            expected_sha256=manifest_sha256,
        )
        try:
            final_verified = _verify_complete_snapshot(
                root=root,
                root_descriptor=root_descriptor,
                root_identity=root_identity,
                manifest_parent=manifest_parent,
                manifest_parent_descriptor=manifest_parent_descriptor,
                manifest_parent_identity=manifest_parent_identity,
                manifest_path=manifest,
                manifest_name=manifest.name,
                manifest_descriptor=staged_descriptor,
                manifest_identity=staged_identity,
                manifest_bytes=encoded,
                expected_files=expected_files,
            )
        except BaseException:
            try:
                removed = _unlink_if_identity(
                    manifest_parent_descriptor,
                    manifest.name,
                    expected_identity=staged_identity,
                )
            except OSError as rollback_exc:
                raise SealRefusal(
                    "could not roll back invalid final manifest"
                ) from rollback_exc
            if not removed:
                raise SealRefusal("could not safely roll back invalid final manifest")
            raise

        return {
            "status": "sealed",
            "snapshot_root": str(root),
            "manifest": str(manifest),
            "manifest_sha256": final_verified.manifest.digest_sha256,
            "file_count": len(expected_files),
            "total_size_bytes": sum(item.size_bytes for item in expected_files),
        }
    finally:
        if (
            manifest_parent_descriptor is not None
            and staged_name is not None
            and staged_identity is not None
        ):
            try:
                _unlink_if_identity(
                    manifest_parent_descriptor,
                    staged_name,
                    expected_identity=staged_identity,
                )
            except OSError:
                pass
        if staged_descriptor is not None:
            os.close(staged_descriptor)
        if manifest_parent_descriptor is not None:
            os.close(manifest_parent_descriptor)
        if root_descriptor is not None:
            os.close(root_descriptor)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-root", required=True)
    parser.add_argument("--manifest", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = seal_snapshot(arguments.snapshot_root, arguments.manifest)
    except (SnapshotSafetyError, OSError) as exc:
        print(
            json.dumps({"status": "refused", "error": str(exc)}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
