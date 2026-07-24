"""Owner-gated, offline Evidence v2 rebuild candidate tooling.

This module deliberately has no activation or cleanup operation.  It accepts a
manifest-verified, already-sealed agentacct snapshot and writes only into a new
owner-only candidate directory.  The live store and the sealed source are
never opened for writing.

The generic Evidence spool is conserved except for the historical duplicate
lane: trusted cumulative local-usage rows adapted from the v1 ledger.  Those
rows are rebuilt once through the refreshable current-state lane.  Immutable
usage evidence from MCP/provider/telemetry sources, every non-usage envelope,
conflict versions, and valid claimed links remain generic evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from .canonical.live_paths import LivePathSafetyError, paths_overlap, reject_live_state_overlap
from .client_usage import ClientUsageEvent, discover_client_usage_with_diagnostics
from .evidence import (
    ClaimedLink,
    EvidenceEnvelope,
    adapt_v1_event,
    canonical_digest,
    canonical_json_bytes,
    raw_digest_for,
)
from .evidence_runtime import EvidenceRuntime
from .evidence_rebuild_activation import EvidenceActivationError, fingerprint_evidence_tree
from .evidence_rebuild_snapshot import (
    MANIFEST_FILENAME as SNAPSHOT_MANIFEST_FILENAME,
    PAYLOAD_DIRNAME as SNAPSHOT_PAYLOAD_DIRNAME,
    SNAPSHOT_SCHEMA_VERSION,
    EvidenceSnapshotError,
    verify_evidence_snapshot,
)
from .evidence_store import (
    EVIDENCE_PROJECTION_FILENAME,
    EVIDENCE_SPOOL_FILENAME,
    EVIDENCE_STORE_DIRNAME,
    REFRESHABLE_USAGE_SPOOL_FILENAME,
    EvidenceStore,
    RefreshableUsageItem,
)
from .refreshable_usage import (
    is_trusted_refreshable_local_usage,
    refreshable_usage_slot_digest,
    refreshable_usage_slot_identity,
    refreshable_usage_source_order,
    refreshable_usage_truth_digest,
    refreshable_usage_truth_digest_from_material,
    refreshable_usage_usage_material_digest,
    refreshable_usage_usage_material_digest_from_material,
)
from .usage_truth import mark_trusted_local_usage_import_event


REBUILD_RECEIPT_SCHEMA = "agent-chronicle.evidence-rebuild-candidate.v1"
REBUILD_RECEIPT_FILENAME = "evidence-rebuild-receipt.json"
DEFAULT_REBUILD_CLIENTS = ("codex", "claude-code", "hermes")
REBUILD_FULL_HISTORY_LIMIT = 2**63 - 1
_SUPPORTED_REBUILD_CLIENTS = frozenset(DEFAULT_REBUILD_CLIENTS)
_V1_TRANSPORT_ADAPTER_RE = re.compile(r"chronicle-v1-([a-z0-9_-]+)-adapter\Z")
_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_COPY_CHUNK_BYTES = 1024 * 1024


class EvidenceRebuildCandidateError(RuntimeError):
    """Base error for a candidate that cannot be built or trusted."""


class EvidenceRebuildSafetyError(EvidenceRebuildCandidateError):
    """The declared source or output boundary is unsafe or ambiguous."""


class EvidenceRebuildVerificationError(EvidenceRebuildCandidateError):
    """Candidate source-of-truth bytes or projection checks did not verify."""


@dataclass(frozen=True)
class _RawInventory:
    entries: int
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {"entries": self.entries, "digest": self.digest}


@dataclass(frozen=True)
class _GenericSpoolRead:
    envelopes: tuple[EvidenceEnvelope, ...]
    links: tuple[ClaimedLink, ...]
    records: int
    filtered_refreshable_usage: int
    duplicate_evidence_ids: int
    duplicate_link_ids: int
    boundaries: frozenset[int]
    size_bytes: int
    invalid_records: int = 0


@dataclass(frozen=True)
class _SnapshotFile:
    relative_path: str
    path: Path
    size_bytes: int
    sha256: str
    snapshot_stat: Mapping[str, int]


@dataclass(frozen=True)
class _EvidenceSnapshotView:
    """Strict, read-only view over an already verified P0 snapshot."""

    root: Path
    payload_root: Path
    manifest_path: Path
    manifest_digest_sha256: str
    tree_digest: str
    files: tuple[_SnapshotFile, ...]

    @property
    def kind(self) -> str:
        return SNAPSHOT_SCHEMA_VERSION

    @contextmanager
    def open_binary(self, relative_path: str) -> Iterator[BinaryIO]:
        matches = [item for item in self.files if item.relative_path == relative_path]
        if len(matches) != 1:
            raise EvidenceRebuildVerificationError(
                f"path is not declared by the Evidence snapshot: {relative_path}"
            )
        item = matches[0]
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(item.path, flags)
        except OSError as exc:
            raise EvidenceRebuildVerificationError("snapshot file cannot be opened read-only") from exc
        try:
            observed = os.fstat(descriptor)
            expected = item.snapshot_stat
            compared = {
                "mode": stat.S_IMODE(observed.st_mode),
                "uid": observed.st_uid,
                "gid": observed.st_gid,
                "mtime_ns": observed.st_mtime_ns,
                "ctime_ns": observed.st_ctime_ns,
                "inode": observed.st_ino,
                "device": observed.st_dev,
                "size_bytes": observed.st_size,
                "link_count": observed.st_nlink,
            }
            if any(compared[key] != expected.get(key) for key in compared):
                raise EvidenceRebuildVerificationError("snapshot file identity changed after verification")
            if not stat.S_ISREG(observed.st_mode):
                raise EvidenceRebuildVerificationError("snapshot file is not regular")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                yield handle
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def verify_unchanged(self) -> None:
        try:
            verification = verify_evidence_snapshot(self.root)
        except EvidenceSnapshotError as exc:
            raise EvidenceRebuildVerificationError("Evidence snapshot changed after it was opened") from exc
        if (
            verification.manifest_sha256 != self.manifest_digest_sha256
            or verification.tree_digest != self.tree_digest
        ):
            raise EvidenceRebuildVerificationError("Evidence snapshot identity changed")


def _open_evidence_snapshot(
    snapshot_root: Path | str,
    snapshot_manifest: Path | str,
) -> _EvidenceSnapshotView:
    root = Path(snapshot_root).expanduser()
    manifest = Path(snapshot_manifest).expanduser()
    if not root.is_absolute() or not manifest.is_absolute():
        raise EvidenceRebuildSafetyError("snapshot root and manifest must be explicit absolute paths")
    try:
        resolved_root = root.resolve(strict=True)
        resolved_manifest = manifest.resolve(strict=True)
    except OSError as exc:
        raise EvidenceRebuildSafetyError("snapshot root or manifest does not exist") from exc
    expected_manifest = resolved_root / SNAPSHOT_MANIFEST_FILENAME
    if resolved_manifest != expected_manifest:
        raise EvidenceRebuildSafetyError(
            f"snapshot_manifest must equal <snapshot>/{SNAPSHOT_MANIFEST_FILENAME}"
        )
    try:
        verification = verify_evidence_snapshot(resolved_root)
    except EvidenceSnapshotError as exc:
        raise EvidenceRebuildSafetyError(str(exc)) from exc
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(resolved_manifest, flags)
    try:
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            manifest_value = _strict_json_object(handle.read(), label="Evidence snapshot manifest")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if manifest_value.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise EvidenceRebuildVerificationError("Evidence snapshot manifest schema is invalid")
    raw_files = manifest_value.get("files")
    if not isinstance(raw_files, list):
        raise EvidenceRebuildVerificationError("Evidence snapshot manifest files are invalid")
    payload_root = resolved_root / SNAPSHOT_PAYLOAD_DIRNAME
    files: list[_SnapshotFile] = []
    for item in raw_files:
        if not isinstance(item, Mapping):
            raise EvidenceRebuildVerificationError("Evidence snapshot manifest file entry is invalid")
        relative = item.get("path")
        snapshot_stat = item.get("snapshot_stat")
        if not isinstance(relative, str) or not isinstance(snapshot_stat, Mapping):
            raise EvidenceRebuildVerificationError("Evidence snapshot manifest file entry is invalid")
        files.append(
            _SnapshotFile(
                relative_path=relative,
                path=payload_root / relative,
                size_bytes=int(item.get("size_bytes") or 0),
                sha256=str(item.get("sha256") or ""),
                snapshot_stat={str(key): int(value) for key, value in snapshot_stat.items()},
            )
        )
    return _EvidenceSnapshotView(
        root=resolved_root,
        payload_root=payload_root,
        manifest_path=resolved_manifest,
        manifest_digest_sha256=verification.manifest_sha256,
        tree_digest=verification.tree_digest,
        files=tuple(files),
    )


def _strict_json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise EvidenceRebuildVerificationError(f"{label} is not strict JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise EvidenceRebuildVerificationError(f"{label} must be a JSON object")
    return value


def _sha256_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_COPY_CHUNK_BYTES):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _assert_no_symlink_components(path: Path, *, label: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            observed = current.lstat()
        except OSError as exc:
            raise EvidenceRebuildSafetyError(f"{label} cannot be inspected") from exc
        if stat.S_ISLNK(observed.st_mode):
            raise EvidenceRebuildSafetyError(f"{label} may not contain symlink components")


def _read_private_regular_file(path: Path, *, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EvidenceRebuildVerificationError(f"{label} is missing or unsafe") from exc
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.geteuid()
            or observed.st_nlink != 1
            or stat.S_IMODE(observed.st_mode) & 0o077
        ):
            raise EvidenceRebuildVerificationError(f"{label} is not owner-private and regular")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, _COPY_CHUNK_BYTES):
            chunks.append(chunk)
        final = os.fstat(descriptor)
        if (
            (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns)
            != (
                observed.st_dev,
                observed.st_ino,
                observed.st_size,
                observed.st_mtime_ns,
                observed.st_ctime_ns,
            )
        ):
            raise EvidenceRebuildVerificationError(f"{label} changed while it was read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _copy_private_regular_file(source: Path, target: Path, *, label: str) -> None:
    """Copy one owner-private file without following or reopening its pathname."""

    source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_descriptor = os.open(source, source_flags)
    except OSError as exc:
        raise EvidenceRebuildVerificationError(f"{label} is missing or unsafe") from exc
    target.parent.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIR_MODE)
    os.chmod(target.parent, _PRIVATE_DIR_MODE)
    try:
        observed = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.geteuid()
            or observed.st_nlink != 1
            or stat.S_IMODE(observed.st_mode) & 0o077
        ):
            raise EvidenceRebuildVerificationError(f"{label} is not owner-private and regular")
        target_descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            _PRIVATE_FILE_MODE,
        )
        try:
            while chunk := os.read(source_descriptor, _COPY_CHUNK_BYTES):
                view = memoryview(chunk)
                while view:
                    written = os.write(target_descriptor, view)
                    view = view[written:]
            os.fsync(target_descriptor)
        finally:
            os.close(target_descriptor)
        final = os.fstat(source_descriptor)
        if (
            final.st_dev,
            final.st_ino,
            final.st_size,
            final.st_mtime_ns,
            final.st_ctime_ns,
        ) != (
            observed.st_dev,
            observed.st_ino,
            observed.st_size,
            observed.st_mtime_ns,
            observed.st_ctime_ns,
        ):
            raise EvidenceRebuildVerificationError(f"{label} changed while it was copied")
    finally:
        os.close(source_descriptor)


def _write_private_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = canonical_json_bytes(value) + b"\n"
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        _PRIVATE_FILE_MODE,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        raise


def _copy_verified_file(snapshot: _EvidenceSnapshotView, relative_path: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIR_MODE)
    os.chmod(target.parent, _PRIVATE_DIR_MODE)
    descriptor = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        _PRIVATE_FILE_MODE,
    )
    try:
        with snapshot.open_binary(relative_path) as source, os.fdopen(descriptor, "wb") as destination:
            while chunk := source.read(_COPY_CHUNK_BYTES):
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
    except BaseException:
        raise


def _require_owner_only_parent(raw: Path | str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise EvidenceRebuildSafetyError("candidate_parent must be an explicit absolute path")
    _assert_no_symlink_components(path, label="candidate_parent")
    try:
        observed = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise EvidenceRebuildSafetyError("candidate_parent must be an existing directory") from exc
    if not stat.S_ISDIR(observed.st_mode):
        raise EvidenceRebuildSafetyError("candidate_parent must be an existing directory")
    if stat.S_ISLNK(path.lstat().st_mode):
        raise EvidenceRebuildSafetyError("candidate_parent may not be a symlink")
    if observed.st_uid != os.geteuid() or stat.S_IMODE(observed.st_mode) & 0o077:
        raise EvidenceRebuildSafetyError("candidate_parent must be owner-owned and owner-only")
    resolved = path.resolve(strict=True)
    try:
        reject_live_state_overlap(resolved, label="candidate_parent")
    except LivePathSafetyError as exc:
        raise EvidenceRebuildSafetyError(str(exc)) from exc
    return resolved


def _normalize_clients(clients: Sequence[str]) -> tuple[str, ...]:
    if isinstance(clients, (str, bytes)):
        raise EvidenceRebuildSafetyError("clients must be a sequence of client names")
    normalized = tuple(str(client).strip() for client in clients)
    if not normalized or len(set(normalized)) != len(normalized):
        raise EvidenceRebuildSafetyError("clients must be non-empty and unique")
    unsupported = set(normalized) - _SUPPORTED_REBUILD_CLIENTS
    if unsupported:
        raise EvidenceRebuildSafetyError(
            "unsupported rebuild clients: " + ", ".join(sorted(unsupported))
        )
    return normalized


def _normalize_raw_roots(
    raw_source_roots: Mapping[str, Path | str | Sequence[Path | str]],
    *,
    clients: Sequence[str],
) -> dict[str, tuple[Path, ...]]:
    if not isinstance(raw_source_roots, Mapping):
        raise EvidenceRebuildSafetyError("raw_source_roots must be a mapping")
    result: dict[str, tuple[Path, ...]] = {}
    for client in clients:
        raw = raw_source_roots.get(client)
        if raw is None:
            result[client] = ()
            continue
        values: Sequence[Path | str]
        if isinstance(raw, (str, Path)):
            values = (raw,)
        elif isinstance(raw, Sequence):
            values = raw
        else:
            raise EvidenceRebuildSafetyError(f"raw source roots for {client} are invalid")
        paths: list[Path] = []
        for value in values:
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise EvidenceRebuildSafetyError(f"raw source root for {client} must be absolute")
            _assert_no_symlink_components(path, label=f"raw source root for {client}")
            try:
                observed = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise EvidenceRebuildSafetyError(f"raw source root for {client} does not exist") from exc
            if stat.S_ISLNK(path.lstat().st_mode):
                raise EvidenceRebuildSafetyError(f"raw source root for {client} may not be a symlink")
            if not (stat.S_ISDIR(observed.st_mode) or stat.S_ISREG(observed.st_mode)):
                raise EvidenceRebuildSafetyError(f"raw source root for {client} must be a file or directory")
            if client in {"codex", "claude-code"} and not stat.S_ISDIR(observed.st_mode):
                raise EvidenceRebuildSafetyError(f"raw source root for {client} must be a directory")
            if (
                client == "hermes"
                and stat.S_ISREG(observed.st_mode)
                and path.name != "state.db"
            ):
                raise EvidenceRebuildSafetyError(
                    "raw source root file for hermes must be named state.db"
                )
            paths.append(path.resolve(strict=True))
        # The existing discoverers accept one explicit home per invocation.
        # Refuse ambiguity instead of silently omitting a second root.
        if len(paths) > 1:
            raise EvidenceRebuildSafetyError(f"raw source roots for {client} must contain at most one path")
        result[client] = tuple(paths)
    return result


def _raw_inventory_fingerprint(
    observed: os.stat_result,
) -> tuple[int, int, int, int, int, int, int]:
    return (
        int(observed.st_dev),
        int(observed.st_ino),
        stat.S_IFMT(observed.st_mode),
        int(observed.st_nlink),
        int(observed.st_size),
        int(observed.st_mtime_ns),
        int(observed.st_ctime_ns),
    )


def _raw_root_identity(observed: os.stat_result) -> tuple[int, int, int]:
    return (
        int(observed.st_dev),
        int(observed.st_ino),
        stat.S_IFMT(observed.st_mode),
    )


def _open_absolute_directory_no_follow(path: Path) -> tuple[int, os.stat_result]:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    current_fd = os.open(path.anchor, directory_flags)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        observed = os.fstat(current_fd)
        if not stat.S_ISDIR(observed.st_mode):
            raise OSError("raw carrier is not a directory")
        return current_fd, observed
    except BaseException:
        os.close(current_fd)
        raise


def _open_absolute_regular_no_follow(path: Path) -> tuple[int, os.stat_result]:
    parent_fd, _parent_stat = _open_absolute_directory_no_follow(path.parent)
    file_fd: int | None = None
    try:
        file_fd = os.open(
            path.name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
        observed = os.fstat(file_fd)
        if not stat.S_ISREG(observed.st_mode):
            raise OSError("raw carrier is not a regular file")
        opened_fd = file_fd
        file_fd = None
        return opened_fd, observed
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(parent_fd)


def _raw_inventory_record(
    *,
    client: str,
    root_index: int,
    carrier: str,
    relative: str,
    observed: os.stat_result,
) -> dict[str, Any]:
    return {
        "client": client,
        "root": root_index,
        "carrier": carrier,
        "relative_digest": hashlib.sha256(
            f"{carrier}\0{relative}".encode("utf-8")
        ).hexdigest(),
        "mode": stat.S_IFMT(observed.st_mode),
        "device": int(observed.st_dev),
        "inode": int(observed.st_ino),
        "links": int(observed.st_nlink),
        "size": int(observed.st_size),
        "mtime_ns": int(observed.st_mtime_ns),
        "ctime_ns": int(observed.st_ctime_ns),
    }


def _inventory_carrier_directory(
    *,
    directory_fd: int,
    client: str,
    root_index: int,
    carrier: str,
    relative: Path,
    material: list[dict[str, Any]],
) -> None:
    before = os.fstat(directory_fd)
    material.append(
        _raw_inventory_record(
            client=client,
            root_index=root_index,
            carrier=carrier,
            relative=relative.as_posix() if relative.parts else ".",
            observed=before,
        )
    )
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        with os.scandir(directory_fd) as iterator:
            entries = sorted(list(iterator), key=lambda entry: entry.name)
    except OSError as exc:
        raise EvidenceRebuildVerificationError(
            f"raw {client} carrier traversal failed"
        ) from exc
    for entry in entries:
        name = entry.name
        if not name or name in {".", ".."} or "/" in name:
            raise EvidenceRebuildVerificationError(
                f"raw {client} carrier contains an unsafe entry"
            )
        try:
            first = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise EvidenceRebuildVerificationError(
                f"raw {client} carrier traversal failed"
            ) from exc
        if stat.S_ISLNK(first.st_mode):
            raise EvidenceRebuildVerificationError(
                f"raw {client} carrier contains a symlink"
            )
        child_relative = relative / name
        if stat.S_ISDIR(first.st_mode):
            try:
                child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
            except OSError as exc:
                raise EvidenceRebuildVerificationError(
                    f"raw {client} carrier directory could not be opened"
                ) from exc
            try:
                if _raw_inventory_fingerprint(
                    os.fstat(child_fd)
                ) != _raw_inventory_fingerprint(first):
                    raise EvidenceRebuildVerificationError(
                        f"raw {client} carrier changed during inventory"
                    )
                _inventory_carrier_directory(
                    directory_fd=child_fd,
                    client=client,
                    root_index=root_index,
                    carrier=carrier,
                    relative=child_relative,
                    material=material,
                )
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(first.st_mode):
            material.append(
                _raw_inventory_record(
                    client=client,
                    root_index=root_index,
                    carrier=carrier,
                    relative=child_relative.as_posix(),
                    observed=first,
                )
            )
            try:
                file_fd = os.open(name, file_flags, dir_fd=directory_fd)
            except OSError as exc:
                raise EvidenceRebuildVerificationError(
                    f"raw {client} carrier file could not be opened"
                ) from exc
            try:
                opened = os.fstat(file_fd)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or _raw_inventory_fingerprint(opened)
                    != _raw_inventory_fingerprint(first)
                ):
                    raise EvidenceRebuildVerificationError(
                        f"raw {client} carrier changed during inventory"
                    )
            finally:
                os.close(file_fd)
        else:
            raise EvidenceRebuildVerificationError(
                f"raw {client} carrier contains a non-regular entry"
            )
        try:
            final = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise EvidenceRebuildVerificationError(
                f"raw {client} carrier changed during inventory"
            ) from exc
        if _raw_inventory_fingerprint(final) != _raw_inventory_fingerprint(first):
            raise EvidenceRebuildVerificationError(
                f"raw {client} carrier changed during inventory"
            )
    if _raw_inventory_fingerprint(
        os.fstat(directory_fd)
    ) != _raw_inventory_fingerprint(before):
        raise EvidenceRebuildVerificationError(
            f"raw {client} carrier changed during inventory"
        )


def _raw_carrier_specs(client: str, root: Path) -> tuple[tuple[str, Path, str], ...]:
    # SQLite -wal/-journal sidecars are part of the carrier set: a running
    # client commits into the WAL without touching the main db file's
    # size/mtime/inode, so an inventory that ignores them cannot detect a
    # mid-discovery commit. -shm is deliberately excluded — our own read-only
    # connection mutates it, which would self-trigger the drift check.
    if client == "codex":
        return (
            ("state_5.sqlite", root / "state_5.sqlite", "file"),
            ("state_5.sqlite-wal", root / "state_5.sqlite-wal", "file"),
            ("state_5.sqlite-journal", root / "state_5.sqlite-journal", "file"),
            ("session_index.jsonl", root / "session_index.jsonl", "file"),
            ("sessions", root / "sessions", "directory"),
            ("archived_sessions", root / "archived_sessions", "directory"),
        )
    if client == "claude-code":
        return (("projects", root / "projects", "directory"),)
    if client == "hermes":
        state_db = root if root.name == "state.db" else root / "state.db"
        return (
            ("state.db", state_db, "file"),
            ("state.db-wal", state_db.with_name(state_db.name + "-wal"), "file"),
            ("state.db-journal", state_db.with_name(state_db.name + "-journal"), "file"),
        )
    raise EvidenceRebuildSafetyError(f"unsupported raw inventory client: {client}")


def _assert_raw_sqlite_quiesced(roots: Mapping[str, tuple[Path, ...]]) -> None:
    """Refuse to build while a raw SQLite carrier has uncheckpointed frames.

    ``confirm_writers_stopped`` proves agentacct's own writers are stopped,
    not that the client apps are. A non-empty WAL means the client committed
    rows the main database file does not yet contain; discovery would read
    them through the read-only connection while the main-file fingerprint
    stays unchanged.
    """

    for client in sorted(roots):
        for root in roots[client]:
            for carrier, path, kind in _raw_carrier_specs(client, root):
                if kind != "file" or not carrier.endswith("-wal"):
                    continue
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise EvidenceRebuildVerificationError(
                        f"raw {client} carrier cannot be inspected"
                    ) from exc
                if stat.S_ISREG(info.st_mode) and info.st_size > 0:
                    raise EvidenceRebuildSafetyError(
                        f"raw {client} SQLite WAL is non-empty ({carrier}); "
                        f"stop the {client} client app so it checkpoints before rebuilding"
                    )


def _inventory_paths(roots: Mapping[str, tuple[Path, ...]]) -> _RawInventory:
    material: list[dict[str, Any]] = []
    for client in sorted(roots):
        for root_index, root in enumerate(roots[client]):
            try:
                root_before = root.lstat()
            except OSError as exc:
                raise EvidenceRebuildVerificationError(
                    f"raw source root for {client} cannot be inspected"
                ) from exc
            top_states: list[tuple[Path, bool, tuple[int, ...] | None]] = []
            for carrier, path, expected_kind in _raw_carrier_specs(client, root):
                try:
                    first = path.lstat()
                except FileNotFoundError:
                    material.append(
                        {
                            "client": client,
                            "root": root_index,
                            "carrier": carrier,
                            "present": False,
                        }
                    )
                    top_states.append((path, False, None))
                    continue
                except OSError as exc:
                    raise EvidenceRebuildVerificationError(
                        f"raw {client} carrier cannot be inspected"
                    ) from exc
                if stat.S_ISLNK(first.st_mode):
                    raise EvidenceRebuildVerificationError(
                        f"raw {client} carrier may not be a symlink"
                    )
                top_fingerprint = _raw_inventory_fingerprint(first)
                top_states.append((path, True, top_fingerprint))
                if expected_kind == "directory":
                    if not stat.S_ISDIR(first.st_mode):
                        raise EvidenceRebuildVerificationError(
                            f"raw {client} carrier has the wrong type"
                        )
                    try:
                        directory_fd, opened = _open_absolute_directory_no_follow(path)
                    except OSError as exc:
                        raise EvidenceRebuildVerificationError(
                            f"raw {client} carrier directory could not be opened"
                        ) from exc
                    try:
                        if _raw_inventory_fingerprint(opened) != top_fingerprint:
                            raise EvidenceRebuildVerificationError(
                                f"raw {client} carrier changed during inventory"
                            )
                        _inventory_carrier_directory(
                            directory_fd=directory_fd,
                            client=client,
                            root_index=root_index,
                            carrier=carrier,
                            relative=Path(),
                            material=material,
                        )
                    finally:
                        os.close(directory_fd)
                else:
                    if not stat.S_ISREG(first.st_mode):
                        raise EvidenceRebuildVerificationError(
                            f"raw {client} carrier has the wrong type"
                        )
                    try:
                        file_fd, opened = _open_absolute_regular_no_follow(path)
                    except OSError as exc:
                        raise EvidenceRebuildVerificationError(
                            f"raw {client} carrier file could not be opened"
                        ) from exc
                    try:
                        if _raw_inventory_fingerprint(opened) != top_fingerprint:
                            raise EvidenceRebuildVerificationError(
                                f"raw {client} carrier changed during inventory"
                            )
                        material.append(
                            _raw_inventory_record(
                                client=client,
                                root_index=root_index,
                                carrier=carrier,
                                relative=".",
                                observed=opened,
                            )
                        )
                    finally:
                        os.close(file_fd)
            for path, was_present, expected_fingerprint in top_states:
                try:
                    final = path.lstat()
                except FileNotFoundError:
                    if was_present:
                        raise EvidenceRebuildVerificationError(
                            f"raw {client} carrier changed during inventory"
                        )
                    continue
                except OSError as exc:
                    raise EvidenceRebuildVerificationError(
                        f"raw {client} carrier changed during inventory"
                    ) from exc
                if (
                    not was_present
                    or expected_fingerprint is None
                    or _raw_inventory_fingerprint(final) != expected_fingerprint
                ):
                    raise EvidenceRebuildVerificationError(
                        f"raw {client} carrier changed during inventory"
                    )
            try:
                if stat.S_ISDIR(root_before.st_mode):
                    verification_fd, root_after = _open_absolute_directory_no_follow(root)
                elif stat.S_ISREG(root_before.st_mode):
                    verification_fd, root_after = _open_absolute_regular_no_follow(root)
                else:
                    raise EvidenceRebuildVerificationError(
                        f"raw source root for {client} changed during inventory"
                    )
            except OSError as exc:
                raise EvidenceRebuildVerificationError(
                    f"raw source root for {client} changed during inventory"
                ) from exc
            try:
                if _raw_root_identity(root_after) != _raw_root_identity(root_before):
                    raise EvidenceRebuildVerificationError(
                        f"raw source root for {client} changed during inventory"
                    )
            finally:
                os.close(verification_fd)
    return _RawInventory(entries=len(material), digest=canonical_digest(material))


def _read_ledger(snapshot: _EvidenceSnapshotView) -> list[dict[str, Any]]:
    try:
        events: list[dict[str, Any]] = []
        with snapshot.open_binary("events.jsonl") as handle:
            line_number = 0
            while raw := handle.readline():
                line_number += 1
                if not raw.strip():
                    continue
                if not raw.endswith(b"\n"):
                    raise EvidenceRebuildVerificationError("events.jsonl has a torn final record")
                events.append(_strict_json_object(raw, label=f"events.jsonl line {line_number}"))
    except EvidenceRebuildCandidateError:
        raise
    except (OSError, ValueError) as exc:
        raise EvidenceRebuildVerificationError("sealed snapshot events.jsonl cannot be read") from exc
    return events


def _legacy_event_from_envelope(envelope: EvidenceEnvelope) -> dict[str, Any] | None:
    legacy = envelope.payload.get("legacy")
    if not isinstance(legacy, Mapping):
        return None
    metadata = legacy.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    return {
        "event_type": envelope.event_type,
        "provider": legacy.get("provider"),
        "model": legacy.get("model"),
        "usage_confidence": legacy.get("usage_confidence"),
        "cost_confidence": legacy.get("cost_confidence"),
        "estimated_input_tokens": legacy.get("estimated_input_tokens"),
        "estimated_output_tokens": legacy.get("estimated_output_tokens"),
        "estimated_cost_usd": legacy.get("estimated_cost_usd"),
        "metadata": dict(metadata),
    }


def _is_inflated_legacy_usage(envelope: EvidenceEnvelope) -> bool:
    if (
        envelope.event_type != "model_usage"
        or envelope.source_type != "local_client_log"
        or not _is_v1_adapter_envelope(envelope)
    ):
        return False

    event = _legacy_event_from_envelope(envelope)
    return event is not None and is_trusted_refreshable_local_usage(event)


def _is_v1_adapter_envelope(envelope: EvidenceEnvelope) -> bool:
    if "v1_compat" not in envelope.tags:
        return False
    default_adapter = (
        envelope.source_instance == "legacy-events-jsonl"
        and envelope.adapter == "chronicle-v1-event-adapter"
    )
    transport_match = _V1_TRANSPORT_ADAPTER_RE.fullmatch(envelope.adapter)
    transport_adapter = bool(
        transport_match
        and envelope.source_instance == f"v1-{transport_match.group(1)}"
    )
    if not (default_adapter or transport_adapter):
        return False
    return isinstance(envelope.payload.get("legacy"), Mapping)


def _read_generic_spool(snapshot: _EvidenceSnapshotView) -> _GenericSpoolRead:
    relative = f"{EVIDENCE_STORE_DIRNAME}/{EVIDENCE_SPOOL_FILENAME}"
    declared = {item.relative_path for item in snapshot.files}
    if relative not in declared:
        return _GenericSpoolRead((), (), 0, 0, 0, 0, frozenset({0}), 0)

    envelopes: list[EvidenceEnvelope] = []
    links: list[ClaimedLink] = []
    evidence_by_id: dict[str, bytes] = {}
    link_by_id: dict[str, bytes] = {}
    records = filtered = duplicate_evidence = duplicate_links = invalid = 0
    boundaries: set[int] = {0}
    offset = 0
    with snapshot.open_binary(relative) as handle:
        line_number = 0
        while raw := handle.readline():
            line_number += 1
            offset += len(raw)
            boundaries.add(offset)
            if not raw.strip():
                continue
            if not raw.endswith(b"\n"):
                raise EvidenceRebuildVerificationError("generic Evidence spool has a torn final record")
            # Preserved damage (torn/invalid lines the live store keeps
            # verbatim and never projects) is counted and skipped, mirroring
            # live replay; its end offset stays a valid record boundary.
            try:
                record = _strict_json_object(raw, label=f"generic Evidence spool line {line_number}")
                kind, payload = EvidenceStore._validate_spool_record(record)
            except (EvidenceRebuildVerificationError, TypeError, ValueError, KeyError):
                invalid += 1
                continue
            records += 1
            if kind == "evidence":
                try:
                    envelope = EvidenceEnvelope.from_dict(payload)
                except (TypeError, ValueError, KeyError):
                    invalid += 1
                    records -= 1
                    continue
                if _is_inflated_legacy_usage(envelope):
                    filtered += 1
                    continue
                encoded = canonical_json_bytes(envelope.to_dict())
                prior = evidence_by_id.get(envelope.evidence_id)
                if prior is not None:
                    if prior != encoded:
                        raise EvidenceRebuildVerificationError("one evidence_id has divergent content")
                    duplicate_evidence += 1
                    continue
                evidence_by_id[envelope.evidence_id] = encoded
                envelopes.append(envelope)
            else:
                try:
                    link = ClaimedLink.from_dict(payload)
                except (TypeError, ValueError, KeyError):
                    invalid += 1
                    records -= 1
                    continue
                encoded = canonical_json_bytes(link.to_dict())
                prior = link_by_id.get(link.link_id)
                if prior is not None:
                    if prior != encoded:
                        raise EvidenceRebuildVerificationError("one link_id has divergent content")
                    duplicate_links += 1
                    continue
                link_by_id[link.link_id] = encoded
                links.append(link)
    return _GenericSpoolRead(
        envelopes=tuple(envelopes),
        links=tuple(links),
        records=records,
        filtered_refreshable_usage=filtered,
        duplicate_evidence_ids=duplicate_evidence,
        duplicate_link_ids=duplicate_links,
        boundaries=frozenset(boundaries),
        size_bytes=offset,
        invalid_records=invalid,
    )


def _validate_ignored_refresh_spool(
    snapshot: _EvidenceSnapshotView,
    *,
    main_boundaries: frozenset[int],
    main_size: int,
) -> dict[str, int]:
    relative = f"{EVIDENCE_STORE_DIRNAME}/{REFRESHABLE_USAGE_SPOOL_FILENAME}"
    declared = {item.relative_path for item in snapshot.files}
    if relative not in declared:
        return {"records": 0, "bytes": 0, "invalid_records": 0}
    records = size = invalid = 0
    with snapshot.open_binary(relative) as handle:
        line_number = 0
        while raw := handle.readline():
            line_number += 1
            size += len(raw)
            if not raw.strip():
                continue
            if not raw.endswith(b"\n"):
                raise EvidenceRebuildVerificationError("refreshable usage spool has a torn final record")
            # Preserved damage is counted and skipped (its fence is
            # unreadable/untrusted), mirroring live replay's damage model.
            try:
                record = _strict_json_object(raw, label=f"refreshable usage spool line {line_number}")
                _payload, fence = EvidenceStore._validate_refreshable_usage_spool_record(record)
            except (EvidenceRebuildVerificationError, TypeError, ValueError, KeyError):
                invalid += 1
                continue
            if fence > main_size or fence not in main_boundaries:
                raise EvidenceRebuildVerificationError(
                    "refreshable usage spool references a non-record generic spool fence"
                )
            records += 1
    return {"records": records, "bytes": size, "invalid_records": invalid}


def _safe_diagnostics(value: Mapping[str, Any] | None) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    result: dict[str, Any] = {}
    for key in (
        "discovered",
        "parsed",
        "skipped",
        "error_count",
        "selected_root_groups",
        "returned_root_groups",
        "returned_rows",
        "excluded_by_limit",
        "unparsed_selected_rows",
        "unresolved_identity_files",
        "excluded_by_source_namespace",
        "observed_sessions",
        "usage_sessions",
        "sessions_without_usage",
    ):
        raw = source.get(key)
        result[key] = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else None
    result["error_codes"] = sorted(
        {str(code) for code in source.get("error_codes") or () if isinstance(code, str)}
    )
    source_present = source.get("source_present")
    result["source_present"] = source_present if isinstance(source_present, bool) else None
    return result


def _diagnostic_is_complete(
    diagnostic: Mapping[str, Any],
) -> bool:
    required_integer_fields = (
        "parsed",
        "skipped",
        "returned_rows",
        "error_count",
        "excluded_by_limit",
        "unparsed_selected_rows",
        "unresolved_identity_files",
        "excluded_by_source_namespace",
    )
    if any(
        isinstance(diagnostic.get(key), bool)
        or not isinstance(diagnostic.get(key), int)
        for key in required_integer_fields
    ):
        return False
    if diagnostic.get("error_count") != 0:
        return False
    for key in (
        "skipped",
        "excluded_by_limit",
        "unparsed_selected_rows",
        "unresolved_identity_files",
        "excluded_by_source_namespace",
    ):
        if diagnostic.get(key) != 0:
            return False
    # Completeness authority comes from a client-specific carrier opened by
    # the discoverer (Codex sqlite/rollout, Claude projects fd, Hermes sqlite
    # plan/schema), never from a non-empty caller-provided directory.
    if diagnostic.get("source_present") is not True:
        return False
    selected = diagnostic.get("selected_root_groups")
    returned = diagnostic.get("returned_root_groups")
    if selected is None or returned is None:
        return selected is None and returned is None
    return (
        isinstance(selected, int)
        and not isinstance(selected, bool)
        and isinstance(returned, int)
        and not isinstance(returned, bool)
        and selected == returned
    )


def _discover_full_history(
    *,
    clients: Sequence[str],
    roots: Mapping[str, tuple[Path, ...]],
    discoverer: Callable[..., Any],
    progress: Callable[[str], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], bool]:
    events: list[dict[str, Any]] = []
    diagnostics: dict[str, dict[str, Any]] = {}
    complete = True
    argument_by_client = {
        "codex": "codex_home",
        "claude-code": "claude_home",
        "hermes": "hermes_home",
    }
    for client in clients:
        if progress is not None:
            progress(f"scanning {client} full history")
        if not roots[client]:
            diagnostics[client] = {
                "discovered": None,
                "parsed": None,
                "skipped": None,
                "error_count": 1,
                "selected_root_groups": None,
                "returned_root_groups": None,
                "returned_rows": None,
                "excluded_by_limit": None,
                "unparsed_selected_rows": None,
                "unresolved_identity_files": None,
                "excluded_by_source_namespace": None,
                "observed_sessions": None,
                "usage_sessions": None,
                "sessions_without_usage": None,
                "error_codes": ["raw_source_root_not_declared"],
                "source_present": None,
            }
            complete = False
            continue
        kwargs = {
            "client": client,
            # The public discoverers historically perform integer comparisons
            # and slices, so ``None`` is not portable across clients.  This
            # rebuild-only int64 ceiling is outside the normal CLI's 1..200
            # contract; diagnostics must still prove that it excluded zero.
            "limit_sessions": REBUILD_FULL_HISTORY_LIMIT,
            argument_by_client[client]: roots[client][0],
        }
        try:
            discovery = discoverer(**kwargs)
        except Exception:  # fail closed without leaking paths or source text
            diagnostics[client] = {
                "discovered": None,
                "parsed": None,
                "skipped": None,
                "error_count": 1,
                "selected_root_groups": None,
                "returned_root_groups": None,
                "returned_rows": None,
                "excluded_by_limit": None,
                "unparsed_selected_rows": None,
                "unresolved_identity_files": None,
                "excluded_by_source_namespace": None,
                "observed_sessions": None,
                "usage_sessions": None,
                "sessions_without_usage": None,
                "error_codes": ["discoverer_failed"],
                "source_present": None,
            }
            complete = False
            continue
        raw_diagnostics = getattr(discovery, "diagnostics", {})
        diagnostic_present = isinstance(raw_diagnostics, Mapping) and client in raw_diagnostics
        raw_diagnostic = raw_diagnostics.get(client, {}) if isinstance(raw_diagnostics, Mapping) else {}
        diagnostic = _safe_diagnostics(raw_diagnostic)
        diagnostics[client] = diagnostic
        valid_client_events = 0
        for candidate in getattr(discovery, "events", ()):
            if not isinstance(candidate, ClientUsageEvent):
                complete = False
                diagnostics[client]["error_count"] = int(diagnostics[client].get("error_count") or 0) + 1
                diagnostics[client]["error_codes"] = sorted(
                    {*diagnostics[client]["error_codes"], "unexpected_usage_candidate_type"}
                )
                continue
            if candidate.client != client:
                complete = False
                diagnostics[client]["error_count"] = int(diagnostics[client].get("error_count") or 0) + 1
                diagnostics[client]["error_codes"] = sorted(
                    {*diagnostics[client]["error_codes"], "unexpected_usage_candidate_client"}
                )
                continue
            valid_client_events += 1
            if not candidate.source_parse_complete:
                complete = False
                diagnostics[client]["error_codes"] = sorted(
                    {*diagnostics[client]["error_codes"], "source_parse_incomplete"}
                )
            events.append(mark_trusted_local_usage_import_event(candidate.to_sentinel_event()))
        diagnostic_complete = diagnostic_present and _diagnostic_is_complete(diagnostic)
        returned_rows_match = diagnostic.get("returned_rows") == valid_client_events
        if not returned_rows_match:
            diagnostic["error_codes"] = sorted(
                {*diagnostic["error_codes"], "rebuild_returned_rows_mismatch"}
            )
        if not diagnostic_complete or not returned_rows_match:
            complete = False
            diagnostic["error_codes"] = sorted(
                {*diagnostic["error_codes"], "rebuild_completeness_unknown"}
            )
    return events, diagnostics, complete


def _append_generic_conservation(
    store: EvidenceStore,
    *,
    source: _GenericSpoolRead,
    ledger_events: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, int], list[ClaimedLink]]:
    seen_evidence: set[str] = set()
    # v1 transport was not historically persisted on most ledger rows.  Match
    # old generic envelopes by the two transport-independent source facts
    # before considering a recovery append: event id and exact raw digest.
    # This conserves v1-mcp/v1-cli/v1-internal identities without guessing a
    # transport or minting a parallel legacy adapter copy.
    retained_v1_by_source_event: dict[str, list[EvidenceEnvelope]] = {}
    for envelope in source.envelopes:
        if _is_v1_adapter_envelope(envelope) and envelope.source_event_id is not None:
            retained_v1_by_source_event.setdefault(envelope.source_event_id, []).append(envelope)
    counts = {
        "source_envelopes_preserved": 0,
        "ledger_envelopes_added": 0,
        "ledger_envelopes_deduplicated": 0,
        "ledger_source_fact_matches": 0,
        "ledger_source_event_conflicts": 0,
        "transport_unresolved_recovered": 0,
    }
    for envelope in source.envelopes:
        store.append(envelope)
        seen_evidence.add(envelope.evidence_id)
        counts["source_envelopes_preserved"] += 1

    for event in ledger_events:
        if is_trusted_refreshable_local_usage(event):
            continue
        source_event_id = (
            event.get("event_id")
            if isinstance(event.get("event_id"), str)
            else raw_digest_for(event)
        )
        try:
            envelope = adapt_v1_event(event)
        except (TypeError, ValueError) as exc:
            raise EvidenceRebuildVerificationError("sealed ledger contains an invalid v1 evidence event") from exc
        retained = retained_v1_by_source_event.get(str(source_event_id), [])
        if retained:
            normalized_expected = _normalized_v1_envelope_signature(envelope)
            normalized_retained = {
                canonical_json_bytes(_normalized_v1_envelope_signature(item))
                for item in retained
            }
            if canonical_json_bytes(normalized_expected) not in normalized_retained:
                counts["ledger_source_event_conflicts"] += 1
            elif len(normalized_retained) > 1:
                counts["ledger_source_event_conflicts"] += 1
            else:
                counts["ledger_source_fact_matches"] += 1
            counts["ledger_envelopes_deduplicated"] += 1
            continue
        if envelope.evidence_id in seen_evidence:
            counts["ledger_envelopes_deduplicated"] += 1
            continue
        store.append(envelope)
        seen_evidence.add(envelope.evidence_id)
        counts["ledger_envelopes_added"] += 1
        counts["transport_unresolved_recovered"] += 1
    return counts, list(source.links)


def _normalized_shadow_legacy(value: object) -> object:
    """Remove only metadata injected by EvidenceRuntime.shadow_v1_event."""

    if not isinstance(value, Mapping):
        return value
    normalized = dict(value)
    raw_metadata = normalized.get("metadata")
    if isinstance(raw_metadata, Mapping):
        metadata = dict(raw_metadata)
        metadata.pop("work_event_transport", None)
        metadata.pop("work_event_schema_version", None)
        normalized["metadata"] = metadata
    return normalized


def _normalized_v1_envelope_signature(envelope: EvidenceEnvelope) -> dict[str, Any]:
    """Transport-independent v1 content used only for conservation matching."""

    return {
        "event_type": envelope.event_type,
        "assertion": envelope.assertion,
        "source_type": envelope.source_type,
        "source_system": envelope.source_system,
        "source_schema": envelope.source_schema,
        "legacy": _normalized_shadow_legacy(envelope.payload.get("legacy")),
    }


def _selected_raw_usage_truths(
    raw_events: Sequence[Mapping[str, Any]],
    *,
    authoritative_clients: frozenset[str],
) -> tuple[dict[str, tuple[str, str, int | None]], dict[str, set[str]]]:
    """Reduce raw truth exactly enough to audit same-slot ledger observations.

    ``selected`` maps a slot key to ``(truth_digest, usage_material_digest,
    source_order)``; the second digest is the evidence-link-free projection the
    equal-order audit compares before blocking.
    """

    grouped: dict[str, dict[str, list[int | None]]] = {}
    material_by_truth: dict[str, str] = {}
    raw_slots: dict[str, set[str]] = {client: set() for client in authoritative_clients}
    for event in raw_events:
        metadata = event.get("metadata")
        client = (
            str(metadata.get("client") or "").strip()
            if isinstance(metadata, Mapping)
            else ""
        )
        if client not in authoritative_clients:
            continue
        try:
            slot = refreshable_usage_slot_identity(event)
            truth_digest = refreshable_usage_truth_digest(event)
            source_order = refreshable_usage_source_order(event)
        except (TypeError, ValueError) as exc:
            raise EvidenceRebuildVerificationError(
                "raw usage event has invalid refreshable truth"
            ) from exc
        if slot is None or slot.namespace_kind != "explicit":
            raise EvidenceRebuildVerificationError(
                "raw usage event lacks an explicit refreshable slot identity"
            )
        if truth_digest is None:
            raise EvidenceRebuildVerificationError(
                "raw usage event lacks valid refreshable truth"
            )
        if truth_digest not in material_by_truth:
            # Equal truth digests imply identical material, so one projection
            # per distinct truth is exact. Truth digest success above proves
            # the material is derivable; a None here would be a logic error.
            material_digest = refreshable_usage_usage_material_digest(event)
            if material_digest is None:
                raise EvidenceRebuildVerificationError(
                    "raw usage event lacks valid refreshable truth"
                )
            material_by_truth[truth_digest] = material_digest
        slot_key = refreshable_usage_slot_digest(slot)
        raw_slots[client].add(slot_key)
        grouped.setdefault(slot_key, {}).setdefault(truth_digest, []).append(source_order)

    selected: dict[str, tuple[str, str, int | None]] = {}
    for slot_key, by_truth in grouped.items():
        candidates: list[tuple[str, int | None]] = []
        for truth_digest, orders in by_truth.items():
            ordered = [order for order in orders if order is not None]
            candidates.append((truth_digest, max(ordered) if ordered else None))
        chosen: tuple[str, int | None] | None = None
        if len(candidates) == 1:
            chosen = candidates[0]
        elif all(source_order is not None for _truth, source_order in candidates):
            ordered_candidates = sorted(
                candidates,
                key=lambda item: (int(item[1] or 0), item[0]),
                reverse=True,
            )
            if ordered_candidates[0][1] != ordered_candidates[1][1]:
                chosen = ordered_candidates[0]
        if chosen is not None:
            selected[slot_key] = (chosen[0], material_by_truth[chosen[0]], chosen[1])
    # Reducer-ambiguous slots (divergent truths that cannot be strictly
    # ordered) produce no candidate head: the reconcile errors and the
    # verifier's head-derived view never contains them. Keep the build-time
    # audit on the same basis, otherwise the ledger row is classified
    # divergent here but quarantined_obsolete_slot at verify time and an
    # honest blocked candidate dies as a receipt-tamper mismatch.
    for slots in raw_slots.values():
        slots.intersection_update(selected)
    return selected, raw_slots


def _refreshable_fallback_plan(
    ledger_events: Sequence[Mapping[str, Any]],
    *,
    current_raw: Mapping[str, tuple[str, str, int | None]],
    raw_slots: Mapping[str, set[str]],
    authoritative_clients: Sequence[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Plan ledger fallback against already-selected authoritative raw heads.

    For an authoritative raw namespace, same-slot ledger rows are audit input
    only and are never submitted to current-state reconciliation.  Equal truth
    is parity.  Divergent truth is tolerable only when the ledger source order
    is explicit and strictly older than raw; newer, equal-order, or unordered
    divergence is blocking but still cannot replace raw current truth.
    """

    authoritative = frozenset(authoritative_clients)
    included: list[dict[str, Any]] = []
    counts = {
        "ledger_refreshable_events": 0,
        "included": 0,
        "included_same_slot_parity": 0,
        "included_unscanned_client": 0,
        "same_slot_audited": 0,
        "same_slot_equal_truth": 0,
        "same_slot_older_divergent": 0,
        "same_slot_blocking_divergent": 0,
        "same_slot_newer_divergent": 0,
        "same_slot_equal_order_divergent": 0,
        "same_slot_equal_order_link_drift": 0,
        "same_slot_unordered_divergent": 0,
        "quarantined": 0,
        "quarantined_unresolved_namespace": 0,
        "quarantined_obsolete_slot": 0,
        "quarantined_invalid_identity": 0,
        "quarantined_invalid_truth": 0,
    }
    quarantined: list[str] = []
    same_slot_audit: list[dict[str, Any]] = []

    def quarantine(event: Mapping[str, Any], reason: str) -> None:
        counts["quarantined"] += 1
        counts[f"quarantined_{reason}"] += 1
        quarantined.append(f"{reason}:{raw_digest_for(event)}")

    def audit_same_slot(
        *,
        classification: str,
        slot_key: str,
        raw_truth: str | None,
        ledger_truth: str | None,
        raw_order: int | None,
        ledger_order: int | None,
    ) -> None:
        counts["same_slot_audited"] += 1
        counts[f"same_slot_{classification}"] += 1
        same_slot_audit.append(
            {
                "classification": classification,
                "slot_key": slot_key,
                "raw_truth": raw_truth,
                "ledger_truth": ledger_truth,
                "raw_source_order": raw_order,
                "ledger_source_order": ledger_order,
            }
        )

    for event in ledger_events:
        if not is_trusted_refreshable_local_usage(event):
            continue
        counts["ledger_refreshable_events"] += 1
        metadata = event.get("metadata")
        client = (
            str(metadata.get("client") or "").strip()
            if isinstance(metadata, Mapping)
            else ""
        )
        if client not in authoritative:
            included.append(dict(event))
            counts["included"] += 1
            counts["included_unscanned_client"] += 1
            continue
        try:
            slot = refreshable_usage_slot_identity(event)
        except (TypeError, ValueError):
            quarantine(event, "invalid_identity")
            continue
        if slot is None:
            quarantine(event, "invalid_identity")
            continue
        if slot.namespace_kind != "explicit":
            quarantine(event, "unresolved_namespace")
            continue
        slot_key = refreshable_usage_slot_digest(slot)
        if slot_key not in raw_slots.get(client, set()):
            quarantine(event, "obsolete_slot")
            continue
        try:
            ledger_truth = refreshable_usage_truth_digest(event)
            ledger_order = refreshable_usage_source_order(event)
        except (TypeError, ValueError):
            quarantine(event, "invalid_truth")
            continue
        if ledger_truth is None:
            quarantine(event, "invalid_truth")
            continue
        raw_current = current_raw.get(slot_key)
        raw_truth, raw_material, raw_order = (
            raw_current if raw_current is not None else (None, None, None)
        )
        if raw_truth == ledger_truth:
            audit_same_slot(
                classification="equal_truth",
                slot_key=slot_key,
                raw_truth=raw_truth,
                ledger_truth=ledger_truth,
                raw_order=raw_order,
                ledger_order=ledger_order,
            )
            continue
        if raw_order is not None and ledger_order is not None and ledger_order < raw_order:
            audit_same_slot(
                classification="older_divergent",
                slot_key=slot_key,
                raw_truth=raw_truth,
                ledger_truth=ledger_truth,
                raw_order=raw_order,
                ledger_order=ledger_order,
            )
            continue
        if raw_order is None or ledger_order is None:
            classification = "unordered_divergent"
        elif ledger_order == raw_order:
            # At equal source order, divergence confined to the evidence-link
            # subtree is provenance drift, not usage divergence: a multi-model
            # session keeps growing its transcript (and therefore its client-log
            # evidence links) after one lane's usage froze, so a later raw
            # rescan legitimately computes different links at the same order.
            # Compare the usage-material-only projection before blocking;
            # usage-material divergence stays blocking, never downgraded.
            ledger_material = refreshable_usage_usage_material_digest(event)
            if (
                raw_material is not None
                and ledger_material is not None
                and ledger_material == raw_material
            ):
                classification = "equal_order_link_drift"
            else:
                classification = "equal_order_divergent"
        else:
            classification = "newer_divergent"
        if classification != "equal_order_link_drift":
            counts["same_slot_blocking_divergent"] += 1
        audit_same_slot(
            classification=classification,
            slot_key=slot_key,
            raw_truth=raw_truth,
            ledger_truth=ledger_truth,
            raw_order=raw_order,
            ledger_order=ledger_order,
        )

    return included, {
        **counts,
        "authoritative_clients": sorted(authoritative),
        "quarantine_digest": canonical_digest(sorted(quarantined)),
        "same_slot_audit_digest": canonical_digest(
            sorted(same_slot_audit, key=canonical_json_bytes)
        ),
    }


def _refreshable_fallback_events(
    ledger_events: Sequence[Mapping[str, Any]],
    *,
    raw_events: Sequence[Mapping[str, Any]],
    authoritative_clients: Sequence[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Plan ledger fallback while keeping complete raw scans authoritative."""

    authoritative = frozenset(authoritative_clients)
    current_raw, raw_slots = _selected_raw_usage_truths(
        raw_events,
        authoritative_clients=authoritative,
    )
    return _refreshable_fallback_plan(
        ledger_events,
        current_raw=current_raw,
        raw_slots=raw_slots,
        authoritative_clients=authoritative,
    )


def _append_links(
    store: EvidenceStore,
    links: Sequence[ClaimedLink],
) -> dict[str, Any]:
    evidence_ids: set[str] = set()
    with store._connection() as connection:
        evidence_ids = {
            str(row[0]) for row in connection.execute("SELECT evidence_id FROM evidence_versions")
        }
    retained = 0
    quarantined: list[str] = []
    for link in links:
        if link.claimed_evidence_id not in evidence_ids or link.observed_evidence_id not in evidence_ids:
            quarantined.append(link.link_id)
            continue
        store.append_claimed_link(link)
        retained += 1
    return {
        "retained": retained,
        "quarantined": len(quarantined),
        "quarantine_digest": canonical_digest(sorted(quarantined)),
    }


def _projection_sidecars(projection: Path) -> tuple[Path, ...]:
    return tuple(
        projection.with_name(projection.name + suffix)
        for suffix in ("-wal", "-shm", "-journal")
    )


def _assert_projection_sidecars_absent(projection: Path) -> None:
    for sidecar in _projection_sidecars(projection):
        try:
            sidecar.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise EvidenceRebuildVerificationError(
                "candidate projection sidecar state cannot be inspected"
            ) from exc
        raise EvidenceRebuildVerificationError(
            "candidate projection is not sealed: SQLite sidecar is present"
        )


def _sqlite_header(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EvidenceRebuildVerificationError("candidate projection is missing or unsafe") from exc
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.geteuid()
            or observed.st_nlink != 1
            or stat.S_IMODE(observed.st_mode) & 0o077
        ):
            raise EvidenceRebuildVerificationError(
                "candidate projection is not owner-private and regular"
            )
        header = os.read(descriptor, 100)
        final = os.fstat(descriptor)
        if (
            final.st_dev,
            final.st_ino,
            final.st_size,
            final.st_mtime_ns,
            final.st_ctime_ns,
        ) != (
            observed.st_dev,
            observed.st_ino,
            observed.st_size,
            observed.st_mtime_ns,
            observed.st_ctime_ns,
        ):
            raise EvidenceRebuildVerificationError(
                "candidate projection changed while its header was read"
            )
        return header
    finally:
        os.close(descriptor)


def _assert_projection_rollback_journal_header(projection: Path) -> None:
    header = _sqlite_header(projection)
    if len(header) != 100 or header[:16] != b"SQLite format 3\x00":
        raise EvidenceRebuildVerificationError("candidate projection has an invalid SQLite header")
    # SQLite header bytes 18 and 19 are the persistent write/read versions:
    # 1 is rollback journal, while 2 means WAL and may require external frames.
    if header[18] != 1 or header[19] != 1:
        raise EvidenceRebuildVerificationError(
            "candidate projection is not sealed in rollback-journal mode"
        )


def _fsync_sealed_projection(projection: Path) -> None:
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(projection, file_flags)
    except OSError as exc:
        raise EvidenceRebuildVerificationError(
            "sealed candidate projection could not be opened for durability"
        ) from exc
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_uid != os.geteuid():
            raise EvidenceRebuildVerificationError(
                "sealed candidate projection has an unsafe identity"
            )
        os.fsync(descriptor)
        final = os.fstat(descriptor)
        if (
            final.st_dev,
            final.st_ino,
            final.st_size,
            final.st_mtime_ns,
            final.st_ctime_ns,
        ) != (
            observed.st_dev,
            observed.st_ino,
            observed.st_size,
            observed.st_mtime_ns,
            observed.st_ctime_ns,
        ):
            raise EvidenceRebuildVerificationError(
                "sealed candidate projection changed during durability sync"
            )
    except OSError as exc:
        raise EvidenceRebuildVerificationError(
            "sealed candidate projection durability sync failed"
        ) from exc
    finally:
        os.close(descriptor)

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_descriptor = os.open(projection.parent, directory_flags)
    except OSError as exc:
        raise EvidenceRebuildVerificationError(
            "sealed candidate directory could not be opened for durability"
        ) from exc
    try:
        observed_directory = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(observed_directory.st_mode)
            or observed_directory.st_uid != os.geteuid()
            or stat.S_IMODE(observed_directory.st_mode) & 0o077
        ):
            raise EvidenceRebuildVerificationError(
                "sealed candidate directory has an unsafe identity"
            )
        os.fsync(directory_descriptor)
    except OSError as exc:
        raise EvidenceRebuildVerificationError(
            "sealed candidate directory durability sync failed"
        ) from exc
    finally:
        os.close(directory_descriptor)


def _seal_projection(store: EvidenceStore) -> dict[str, Any]:
    """Checkpoint every WAL frame and leave a self-contained main database."""

    with store._locked():
        with store._connection() as connection:
            try:
                checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                mode_row = connection.execute("PRAGMA journal_mode = DELETE").fetchone()
            except sqlite3.DatabaseError as exc:
                raise EvidenceRebuildVerificationError(
                    "candidate projection could not be sealed"
                ) from exc
    if checkpoint is None or len(checkpoint) != 3:
        raise EvidenceRebuildVerificationError("candidate WAL checkpoint result is invalid")
    busy, log_frames, checkpointed_frames = (int(value) for value in checkpoint)
    if busy != 0 or log_frames != checkpointed_frames:
        raise EvidenceRebuildVerificationError("candidate WAL checkpoint was incomplete")
    journal_mode = str(mode_row[0]).lower() if mode_row else ""
    if journal_mode != "delete":
        raise EvidenceRebuildVerificationError(
            "candidate projection did not enter rollback-journal mode"
        )
    _assert_projection_sidecars_absent(store.projection_path)
    _assert_projection_rollback_journal_header(store.projection_path)
    _fsync_sealed_projection(store.projection_path)
    return {
        "journal_mode": journal_mode,
        "wal_checkpoint": {
            "busy": busy,
            "log_frames": log_frames,
            "checkpointed_frames": checkpointed_frames,
        },
        "sidecars_present": 0,
        "main_db_fsynced": True,
        "directory_fsynced": True,
    }


def _integrity_check(projection: Path) -> str:
    try:
        connection = sqlite3.connect(projection)
        row = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as exc:
        raise EvidenceRebuildVerificationError("candidate projection integrity check failed") from exc
    finally:
        if "connection" in locals():
            connection.close()
    result = str(row[0]) if row else "missing"
    if result != "ok":
        raise EvidenceRebuildVerificationError("candidate projection integrity check failed")
    return result


def _projection_logical_material(projection: Path) -> dict[str, Any]:
    connection = sqlite3.connect(projection)
    connection.row_factory = sqlite3.Row
    try:
        evidence = [
            {"envelope": json.loads(row["envelope_json"]), "is_conflict": bool(row["is_conflict"])}
            for row in connection.execute(
                "SELECT envelope_json, is_conflict FROM evidence_versions ORDER BY evidence_id"
            )
        ]
        links = [
            {
                "link": json.loads(row["link_json"]),
                "validation_state": str(row["validation_state"]),
                "is_conflict": bool(row["is_conflict"]),
            }
            for row in connection.execute(
                "SELECT link_json, validation_state, is_conflict FROM claimed_link_versions ORDER BY link_id"
            )
        ]
        heads = [
            {
                "slot_key": str(row["slot_key"]),
                "slot_identity": json.loads(row["slot_identity_json"]),
                "content_hash": str(row["content_hash"]),
                "source_order": row["source_order"],
                "current_revision_id": row["current_revision_id"],
                "last_revision_id": str(row["last_revision_id"]),
                "evidence_id": str(row["evidence_id"]),
                "tombstoned": bool(row["tombstoned"]),
            }
            for row in connection.execute("SELECT * FROM refreshable_usage_heads ORDER BY slot_key")
        ]
        revisions = [
            {
                "revision_id": str(row["revision_id"]),
                "slot_key": str(row["slot_key"]),
                "slot_identity": json.loads(row["slot_identity_json"]),
                "content_hash": str(row["content_hash"]),
                "source_order": row["source_order"],
                "evidence_id": str(row["evidence_id"]),
                "status": str(row["status"]),
            }
            for row in connection.execute(
                "SELECT * FROM refreshable_usage_revisions ORDER BY revision_id"
            )
        ]
        conflicts = [
            {
                key: row[key]
                for key in (
                    "conflict_key",
                    "slot_key",
                    "slot_identity_json",
                    "current_revision_id",
                    "current_content_hash",
                    "current_source_order",
                    "head_tombstoned",
                    "candidate_content_hash",
                    "candidate_revision_id",
                    "candidate_source_order",
                    "candidate_evidence_id",
                    "candidate_integrity_hash",
                    "candidate_envelope_json",
                )
            }
            for row in connection.execute(
                "SELECT * FROM refreshable_usage_conflicts ORDER BY conflict_key"
            )
        ]
        return {
            "evidence": evidence,
            "links": links,
            "refreshable_heads": heads,
            "refreshable_revisions": revisions,
            "refreshable_conflicts": conflicts,
        }
    finally:
        connection.close()


def _projection_logical_digest(projection: Path) -> str:
    return canonical_digest(_projection_logical_material(projection))


def _current_refresh_items(store: EvidenceStore) -> list[RefreshableUsageItem]:
    items: list[RefreshableUsageItem] = []
    for head in store.refreshable_usage_heads(include_tombstoned=False):
        envelope = store.get(head.evidence_id)
        if envelope is None or head.current_revision_id is None:
            raise EvidenceRebuildVerificationError("refreshable head references missing evidence")
        items.append(
            RefreshableUsageItem(
                slot_key=head.slot_key,
                slot_identity=head.slot_identity,
                content_hash=head.content_hash,
                revision_id=head.current_revision_id,
                source_order=head.source_order,
                envelope=envelope,
            )
        )
    return items


def _verified_candidate_fallback_isolation(
    store: EvidenceStore,
    ledger_events: Sequence[Mapping[str, Any]],
    *,
    authoritative_clients: Sequence[str],
) -> dict[str, Any]:
    """Recompute ledger isolation from snapshot bytes and replayed current heads.

    A candidate receipt is only a self-hashed statement.  Activation readiness
    therefore cannot derive same-slot safety from its isolation counters.  The
    verifier rebuilds the current heads from the sealed spools, treats only
    complete raw client namespaces as authoritative, and audits the separately
    verified snapshot ledger against those heads.
    """

    authoritative = frozenset(authoritative_clients)
    current_raw: dict[str, tuple[str, str, int | None]] = {}
    raw_slots: dict[str, set[str]] = {client: set() for client in authoritative}
    for item in _current_refresh_items(store):
        identity = item.slot_identity
        if not isinstance(identity, Mapping):
            raise EvidenceRebuildVerificationError(
                "candidate current head has invalid slot identity"
            )
        client = identity.get("client")
        if not isinstance(client, str) or client not in authoritative:
            continue
        if identity.get("namespace_kind") != "explicit":
            raise EvidenceRebuildVerificationError(
                "authoritative candidate current head lacks explicit namespace"
            )
        if item.slot_key in current_raw:
            raise EvidenceRebuildVerificationError(
                "candidate current heads contain a duplicate slot"
            )
        # The equal-order audit needs the usage-material projection of this
        # head. Re-derive it from the envelope's stored truth material and
        # re-bind that material to the recorded content hash, so a projection
        # can never be attributed to a truth the head does not actually hold.
        payload = item.envelope.payload.get("refreshable_usage")
        truth = payload.get("truth") if isinstance(payload, Mapping) else None
        if not isinstance(truth, Mapping):
            raise EvidenceRebuildVerificationError(
                "candidate current head envelope lacks refreshable truth material"
            )
        if refreshable_usage_truth_digest_from_material(truth) != item.content_hash:
            raise EvidenceRebuildVerificationError(
                "candidate current head content hash does not match its truth material"
            )
        material_digest = refreshable_usage_usage_material_digest_from_material(truth)
        current_raw[item.slot_key] = (
            item.content_hash,
            material_digest,
            item.source_order,
        )
        raw_slots[client].add(item.slot_key)

    _fallback, isolation = _refreshable_fallback_plan(
        ledger_events,
        current_raw=current_raw,
        raw_slots=raw_slots,
        authoritative_clients=authoritative,
    )
    return isolation


def _assert_second_reconcile_no_growth(store: EvidenceStore) -> dict[str, Any]:
    before = (
        store.spool_path.stat().st_size if store.spool_path.exists() else 0,
        store.refreshable_usage_spool_path.stat().st_size
        if store.refreshable_usage_spool_path.exists()
        else 0,
        store.stats().receipts,
        store.refreshable_usage_stats().batch_receipts,
    )
    result = store.reconcile_refreshable_usage_snapshot(
        _current_refresh_items(store),
        complete=True,
    )
    after = (
        store.spool_path.stat().st_size if store.spool_path.exists() else 0,
        store.refreshable_usage_spool_path.stat().st_size
        if store.refreshable_usage_spool_path.exists()
        else 0,
        store.stats().receipts,
        store.refreshable_usage_stats().batch_receipts,
    )
    if result.changed or before != after:
        raise EvidenceRebuildVerificationError("second refreshable reconcile caused physical growth")
    return {"physical_growth": 0, "unchanged": result.unchanged}


def _candidate_source_files(candidate_root: Path) -> dict[str, dict[str, Any]]:
    relative_paths = [
        "events.jsonl",
        f"{EVIDENCE_STORE_DIRNAME}/{EVIDENCE_SPOOL_FILENAME}",
        f"{EVIDENCE_STORE_DIRNAME}/{REFRESHABLE_USAGE_SPOOL_FILENAME}",
    ]
    result: dict[str, dict[str, Any]] = {}
    for relative in relative_paths:
        path = candidate_root / relative
        if not path.is_file():
            continue
        size, digest = _sha256_file(path)
        result[relative] = {"size_bytes": size, "sha256": digest}
    return result


def _receipt_body(receipt: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in receipt.items() if key != "receipt_sha256"}


def _receipt_with_digest(body: Mapping[str, Any]) -> dict[str, Any]:
    material = dict(body)
    return {**material, "receipt_sha256": hashlib.sha256(canonical_json_bytes(material)).hexdigest()}


def _verify_receipt(receipt: Mapping[str, Any]) -> None:
    if receipt.get("schema_version") != REBUILD_RECEIPT_SCHEMA:
        raise EvidenceRebuildVerificationError("unsupported candidate receipt schema")
    expected = hashlib.sha256(canonical_json_bytes(_receipt_body(receipt))).hexdigest()
    if receipt.get("receipt_sha256") != expected:
        raise EvidenceRebuildVerificationError("candidate receipt digest mismatch")


def _receipt_mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceRebuildVerificationError(f"candidate receipt {label} is invalid")
    return value


def _receipt_count(section: Mapping[str, Any], key: str, *, label: str) -> int:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvidenceRebuildVerificationError(f"candidate receipt {label} is invalid")
    return value


def _activation_ready_from_material(
    receipt: Mapping[str, Any],
    *,
    actual_generic_stats: Mapping[str, Any],
    actual_refresh_stats: Mapping[str, Any],
    replay_integrity: str,
    second_reconcile: Mapping[str, Any],
    verified_isolation: Mapping[str, Any] | None,
    verified_ledger_events: int | None,
) -> bool:
    """Derive readiness from independently bound evidence, never ready bits."""

    source = _receipt_mapping(receipt.get("source"), label="source material")
    claimed_ledger_events = _receipt_count(
        source,
        "ledger_events",
        label="source ledger event count",
    )
    if (
        verified_ledger_events is not None
        and claimed_ledger_events != verified_ledger_events
    ):
        raise EvidenceRebuildVerificationError(
            "candidate receipt ledger event count does not match sealed snapshot"
        )
    raw_import = _receipt_mapping(receipt.get("raw_import"), label="raw import")
    clients = raw_import.get("clients")
    if (
        not isinstance(clients, list)
        or not clients
        or any(not isinstance(client, str) or client not in _SUPPORTED_REBUILD_CLIENTS for client in clients)
        or len(clients) != len(set(clients))
    ):
        raise EvidenceRebuildVerificationError("candidate receipt raw clients are invalid")
    diagnostics = _receipt_mapping(raw_import.get("diagnostics"), label="raw diagnostics")
    if set(diagnostics) != set(clients):
        raise EvidenceRebuildVerificationError("candidate receipt raw diagnostics are incomplete")
    raw_events = _receipt_count(raw_import, "events", label="raw event count")
    diagnostics_complete = True
    returned_rows = 0
    for client in clients:
        diagnostic = _receipt_mapping(
            diagnostics.get(client),
            label="raw client diagnostics",
        )
        normalized = _safe_diagnostics(diagnostic)
        if normalized != dict(diagnostic):
            raise EvidenceRebuildVerificationError(
                "candidate receipt raw client diagnostics are invalid"
            )
        returned = normalized.get("returned_rows")
        if isinstance(returned, int) and not isinstance(returned, bool):
            returned_rows += returned
        else:
            diagnostics_complete = False
        if not _diagnostic_is_complete(normalized) or normalized.get("error_codes") != []:
            diagnostics_complete = False
    diagnostics_complete = diagnostics_complete and returned_rows == raw_events
    raw_complete = raw_import.get("complete")
    if not isinstance(raw_complete, bool) or raw_complete is not diagnostics_complete:
        raise EvidenceRebuildVerificationError(
            "candidate receipt raw completeness declaration mismatch"
        )
    if raw_import.get("limit_sessions") != REBUILD_FULL_HISTORY_LIMIT:
        raise EvidenceRebuildVerificationError("candidate receipt rebuild limit is invalid")

    generic = _receipt_mapping(receipt.get("generic_rebuild"), label="generic rebuild")
    isolation = _receipt_mapping(
        receipt.get("trusted_ledger_usage_isolation"),
        label="trusted ledger isolation",
    )
    if verified_isolation is not None and dict(isolation) != dict(verified_isolation):
        raise EvidenceRebuildVerificationError(
            "candidate receipt trusted ledger isolation does not match verified evidence"
        )
    refresh = _receipt_mapping(receipt.get("refresh_reconcile"), label="refresh reconcile")
    candidate = _receipt_mapping(receipt.get("candidate"), label="candidate material")
    claimed_generic_stats = _receipt_mapping(
        candidate.get("generic_stats"),
        label="generic stats",
    )
    claimed_refresh_stats = _receipt_mapping(
        candidate.get("refreshable_usage_stats"),
        label="refreshable usage stats",
    )
    if dict(claimed_generic_stats) != dict(actual_generic_stats):
        raise EvidenceRebuildVerificationError("candidate generic stats mismatch")
    if dict(claimed_refresh_stats) != dict(actual_refresh_stats):
        raise EvidenceRebuildVerificationError("candidate refreshable usage stats mismatch")

    included = _receipt_count(isolation, "included", label="ledger included count")
    included_unscanned = _receipt_count(
        isolation,
        "included_unscanned_client",
        label="ledger unscanned-client count",
    )
    included_same_slot = _receipt_count(
        isolation,
        "included_same_slot_parity",
        label="ledger same-slot include count",
    )
    same_slot = _receipt_count(isolation, "same_slot_audited", label="same-slot audit count")
    same_slot_equal = _receipt_count(
        isolation,
        "same_slot_equal_truth",
        label="same-slot equal-truth count",
    )
    same_slot_older = _receipt_count(
        isolation,
        "same_slot_older_divergent",
        label="same-slot older-divergent count",
    )
    same_slot_newer = _receipt_count(
        isolation,
        "same_slot_newer_divergent",
        label="same-slot newer-divergent count",
    )
    same_slot_equal_order = _receipt_count(
        isolation,
        "same_slot_equal_order_divergent",
        label="same-slot equal-order-divergent count",
    )
    same_slot_link_drift = _receipt_count(
        isolation,
        "same_slot_equal_order_link_drift",
        label="same-slot equal-order link-drift count",
    )
    same_slot_unordered = _receipt_count(
        isolation,
        "same_slot_unordered_divergent",
        label="same-slot unordered-divergent count",
    )
    same_slot_blocking = _receipt_count(
        isolation,
        "same_slot_blocking_divergent",
        label="same-slot blocking-divergent count",
    )
    quarantined = _receipt_count(isolation, "quarantined", label="ledger quarantine count")
    quarantine_parts = sum(
        _receipt_count(isolation, key, label="ledger quarantine partition")
        for key in (
            "quarantined_unresolved_namespace",
            "quarantined_obsolete_slot",
            "quarantined_invalid_identity",
            "quarantined_invalid_truth",
        )
    )
    ledger_events = _receipt_count(
        isolation,
        "ledger_refreshable_events",
        label="ledger refreshable count",
    )
    if (
        included != included_unscanned + included_same_slot
        or included_same_slot != 0
        or same_slot
        != same_slot_equal
        + same_slot_older
        + same_slot_newer
        + same_slot_equal_order
        + same_slot_link_drift
        + same_slot_unordered
        or same_slot_blocking != same_slot_newer + same_slot_equal_order + same_slot_unordered
        or quarantined != quarantine_parts
        or ledger_events != included + same_slot + quarantined
    ):
        raise EvidenceRebuildVerificationError(
            "candidate receipt trusted ledger isolation counts are inconsistent"
        )
    fallback_count = receipt.get("trusted_ledger_usage_fallback")
    if (
        isinstance(fallback_count, bool)
        or not isinstance(fallback_count, int)
        or fallback_count != included
    ):
        raise EvidenceRebuildVerificationError(
            "candidate receipt trusted ledger fallback count is inconsistent"
        )

    refresh_errors = refresh.get("errors")
    if not isinstance(refresh_errors, list) or any(
        not isinstance(error, str) for error in refresh_errors
    ):
        raise EvidenceRebuildVerificationError("candidate receipt refresh errors are invalid")
    refresh_input = _receipt_count(refresh, "input_count", label="refresh input count")
    if refresh_input != raw_events + fallback_count:
        raise EvidenceRebuildVerificationError(
            "candidate receipt refresh input count is inconsistent"
        )
    if refresh.get("complete_requested") is not raw_complete:
        raise EvidenceRebuildVerificationError(
            "candidate receipt refresh completeness request is inconsistent"
        )
    invalid_generic = _receipt_count(
        actual_generic_stats,
        "invalid_spool_records",
        label="actual invalid generic records",
    )
    actual_refresh_conflicts = _receipt_count(
        actual_refresh_stats,
        "conflicts",
        label="actual refresh conflicts",
    )
    physical_growth = second_reconcile.get("physical_growth")
    if physical_growth != 0 or replay_integrity != "ok":
        raise EvidenceRebuildVerificationError("candidate replay verification is incomplete")

    return bool(
        verified_isolation is not None
        and verified_ledger_events is not None
        and raw_complete
        and refresh.get("complete_applied") is True
        and not refresh_errors
        and _receipt_count(refresh, "conflicts", label="refresh conflict count") == 0
        and _receipt_count(
            generic,
            "ledger_source_event_conflicts",
            label="ledger source-event conflict count",
        )
        == 0
        and invalid_generic == 0
        and _receipt_count(
            isolation,
            "quarantined_invalid_identity",
            label="invalid ledger identity count",
        )
        == 0
        and _receipt_count(
            isolation,
            "quarantined_invalid_truth",
            label="invalid ledger truth count",
        )
        == 0
        and same_slot_blocking == 0
        and actual_refresh_conflicts == 0
    )


def _verify_readiness_declaration(receipt: Mapping[str, Any], derived: bool) -> None:
    claimed = receipt.get("activation_ready")
    expected_status = "verified_candidate" if derived else "blocked_candidate"
    if not isinstance(claimed, bool) or claimed is not derived or receipt.get("status") != expected_status:
        raise EvidenceRebuildVerificationError(
            "candidate readiness declaration does not match verified evidence"
        )


def build_evidence_rebuild_candidate(
    snapshot_root: Path | str,
    snapshot_manifest: Path | str,
    candidate_parent: Path | str,
    *,
    confirm_owner_approved: bool = False,
    raw_source_roots: Mapping[str, Path | str | Sequence[Path | str]],
    clients: Sequence[str] = DEFAULT_REBUILD_CLIENTS,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Build, but never activate, one private Evidence v2 candidate.

    Full-history discovery is deliberately private to this builder and passes
    a rebuild-only int64 ceiling and then requires ``excluded_by_limit=0``.
    The ordinary CLI validator and its 200-session ceiling are not imported or
    modified.

    ``progress`` receives one short stage message before each long-running
    phase; a multi-minute full-history build must not look like a hang.
    Messages carry stage names only — never paths, counts, or content.
    """

    def stage(message: str) -> None:
        if progress is not None:
            progress(message)

    if confirm_owner_approved is not True:
        raise EvidenceRebuildSafetyError(
            "confirm_owner_approved=True is required before rebuilding a candidate"
        )
    normalized_clients = _normalize_clients(clients)
    stage("verifying sealed snapshot")
    snapshot = _open_evidence_snapshot(snapshot_root, snapshot_manifest)
    parent = _require_owner_only_parent(candidate_parent)
    if paths_overlap(parent, snapshot.root) or paths_overlap(parent, snapshot.manifest_path):
        raise EvidenceRebuildSafetyError("candidate_parent must be disjoint from the sealed snapshot")
    roots = _normalize_raw_roots(raw_source_roots, clients=normalized_clients)

    _assert_raw_sqlite_quiesced(roots)
    stage("inventorying raw sources")
    before_inventory = _inventory_paths(roots)
    raw_events, diagnostics, raw_complete = _discover_full_history(
        clients=normalized_clients,
        roots=roots,
        discoverer=discover_client_usage_with_diagnostics,
        progress=progress,
    )
    stage("re-checking raw source inventory")
    after_inventory = _inventory_paths(roots)
    if before_inventory != after_inventory:
        raise EvidenceRebuildVerificationError("raw source inventory changed during full-history discovery")

    stage("reading sealed ledger and spools")
    ledger_events = _read_ledger(snapshot)
    generic = _read_generic_spool(snapshot)
    ignored_refresh = _validate_ignored_refresh_spool(
        snapshot,
        main_boundaries=generic.boundaries,
        main_size=generic.size_bytes,
    )

    stage("building candidate store")
    candidate_root = parent / f"evidence-rebuild-candidate-{uuid.uuid4().hex}"
    try:
        candidate_root.mkdir(mode=_PRIVATE_DIR_MODE)
    except OSError as exc:
        raise EvidenceRebuildSafetyError("could not create a new candidate directory") from exc
    os.chmod(candidate_root, _PRIVATE_DIR_MODE)

    # From this point a failed owner-only candidate is intentionally retained
    # for inspection.  This module never performs cleanup by pathname.
    _copy_verified_file(snapshot, "events.jsonl", candidate_root / "events.jsonl")
    store = EvidenceStore(candidate_root)
    conservation, links = _append_generic_conservation(
        store,
        source=generic,
        ledger_events=ledger_events,
    )
    fallback, fallback_isolation = _refreshable_fallback_events(
        ledger_events,
        raw_events=raw_events,
        authoritative_clients=normalized_clients if raw_complete else (),
    )
    stage("reconciling refreshable usage")
    refresh_result = EvidenceRuntime(candidate_root, enabled=True).reconcile_refreshable_usage_snapshot(
        [*raw_events, *fallback],
        complete=raw_complete,
        transport="internal",
    ).to_dict()
    links_result = _append_links(store, links)
    stage("checking projection integrity and idempotence")
    integrity = _integrity_check(store.projection_path)
    idempotence = _assert_second_reconcile_no_growth(store)
    logical_digest = _projection_logical_digest(store.projection_path)
    generic_stats = store.stats().to_dict()
    refresh_stats = store.refreshable_usage_stats().to_dict()
    stage("sealing and fingerprinting candidate")
    projection_seal = _seal_projection(store)
    source_files = _candidate_source_files(candidate_root)
    try:
        evidence_tree = fingerprint_evidence_tree(store.evidence_root).to_dict()
    except EvidenceActivationError as exc:
        raise EvidenceRebuildVerificationError("candidate Evidence tree cannot be fingerprinted") from exc

    receipt_material: dict[str, Any] = {
        "schema_version": REBUILD_RECEIPT_SCHEMA,
        "activation_requires_owner_approval": True,
        "automatic_activation_performed": False,
        "source": {
            "snapshot_manifest_sha256": snapshot.manifest_digest_sha256,
            "snapshot_kind": snapshot.kind,
            "snapshot_files": len(snapshot.files),
            "ledger_events": len(ledger_events),
            "raw_inventory": before_inventory.to_dict(),
        },
        "raw_import": {
            "clients": list(normalized_clients),
            "events": len(raw_events),
            "complete": raw_complete,
            "limit_sessions": REBUILD_FULL_HISTORY_LIMIT,
            "diagnostics": diagnostics,
        },
        "generic_rebuild": {
            "source_records": generic.records,
            "filtered_refreshable_usage": generic.filtered_refreshable_usage,
            "source_duplicate_evidence_ids": generic.duplicate_evidence_ids,
            "source_duplicate_link_ids": generic.duplicate_link_ids,
            "source_invalid_spool_records": generic.invalid_records,
            **conservation,
        },
        "derived_refresh_spool_ignored": ignored_refresh,
        "trusted_ledger_usage_fallback": len(fallback),
        "trusted_ledger_usage_isolation": fallback_isolation,
        "refresh_reconcile": refresh_result,
        "claimed_links": links_result,
        "candidate": {
            "candidate_root": str(candidate_root),
            "evidence_path": str(store.evidence_root),
            "source_files": source_files,
            # Exact activation-compatible fingerprint.  In particular,
            # tree_sha256 uses the activation module's canonical material:
            # relative_path/kind/mode/size_bytes/sha256 for every entry.
            "evidence_tree": evidence_tree,
            "logical_digest": logical_digest,
            "generic_stats": generic_stats,
            "refreshable_usage_stats": refresh_stats,
            "projection_integrity": integrity,
            "projection_seal": projection_seal,
            "second_reconcile": idempotence,
        },
        "privacy": {
            "receipt_content": "counts_digests_and_stable_codes_only",
            "prompts_transcripts_secrets_in_receipt": False,
        },
    }
    activation_ready = _activation_ready_from_material(
        receipt_material,
        actual_generic_stats=generic_stats,
        actual_refresh_stats=refresh_stats,
        replay_integrity=integrity,
        second_reconcile=idempotence,
        verified_isolation=fallback_isolation,
        verified_ledger_events=len(ledger_events),
    )
    receipt = _receipt_with_digest(
        {
            **receipt_material,
            "status": "verified_candidate" if activation_ready else "blocked_candidate",
            "activation_ready": activation_ready,
        }
    )
    _verify_readiness_declaration(receipt, activation_ready)
    _write_private_json(candidate_root / REBUILD_RECEIPT_FILENAME, receipt)
    snapshot.verify_unchanged()

    stage("verifying candidate (spool replay and fingerprints)")
    verification = verify_evidence_rebuild_candidate(
        candidate_root,
        scratch_parent=parent,
        snapshot_root=snapshot.root,
        snapshot_manifest=snapshot.manifest_path,
        required_clients=normalized_clients,
    )
    return {
        "status": receipt["status"],
        "candidate_root": str(candidate_root),
        "receipt": str(candidate_root / REBUILD_RECEIPT_FILENAME),
        "activation_ready": activation_ready,
        "activation_requires_owner_approval": True,
        "logical_digest": logical_digest,
        "verification": verification,
    }


def verify_evidence_rebuild_candidate(
    candidate_root: Path | str,
    *,
    scratch_parent: Path | str | None = None,
    snapshot_root: Path | str | None = None,
    snapshot_manifest: Path | str | None = None,
    required_clients: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Verify one candidate from its spools, without activating it."""

    externally_required_clients = (
        _normalize_clients(required_clients) if required_clients is not None else None
    )

    root = Path(candidate_root).expanduser()
    if not root.is_absolute():
        raise EvidenceRebuildSafetyError("candidate_root must be an explicit absolute path")
    _assert_no_symlink_components(root, label="candidate_root")
    try:
        observed = root.stat(follow_symlinks=False)
    except OSError as exc:
        raise EvidenceRebuildSafetyError("candidate_root must be an existing directory") from exc
    if not stat.S_ISDIR(observed.st_mode) or stat.S_ISLNK(root.lstat().st_mode):
        raise EvidenceRebuildSafetyError("candidate_root must be a non-symlink directory")
    if observed.st_uid != os.geteuid() or stat.S_IMODE(observed.st_mode) & 0o077:
        raise EvidenceRebuildSafetyError("candidate_root must be owner-owned and owner-only")
    root = root.resolve(strict=True)
    try:
        reject_live_state_overlap(root, label="candidate_root")
    except LivePathSafetyError as exc:
        raise EvidenceRebuildSafetyError(str(exc)) from exc

    if (snapshot_root is None) != (snapshot_manifest is None):
        raise EvidenceRebuildSafetyError("snapshot_root and snapshot_manifest must be supplied together")
    verified_snapshot: _EvidenceSnapshotView | None = None
    if snapshot_root is not None and snapshot_manifest is not None:
        verified_snapshot = _open_evidence_snapshot(snapshot_root, snapshot_manifest)
        if paths_overlap(root, verified_snapshot.root) or paths_overlap(root, verified_snapshot.manifest_path):
            raise EvidenceRebuildSafetyError("candidate_root must be disjoint from the sealed snapshot")

    receipt_path = root / REBUILD_RECEIPT_FILENAME
    receipt_bytes = _read_private_regular_file(receipt_path, label="candidate receipt")
    receipt = _strict_json_object(receipt_bytes, label="candidate receipt")
    _verify_receipt(receipt)
    candidate_material = _receipt_mapping(receipt.get("candidate"), label="candidate material")
    expected_files = candidate_material.get("source_files")
    if not isinstance(expected_files, Mapping):
        raise EvidenceRebuildVerificationError("candidate receipt source-file inventory is invalid")
    actual_files = _candidate_source_files(root)
    if actual_files != expected_files:
        raise EvidenceRebuildVerificationError("candidate source-of-truth file digest mismatch")
    expected_tree = candidate_material.get("evidence_tree")
    if not isinstance(expected_tree, Mapping):
        raise EvidenceRebuildVerificationError("candidate Evidence tree receipt is invalid")
    seal = _receipt_mapping(candidate_material.get("projection_seal"), label="projection seal")
    checkpoint = _receipt_mapping(seal.get("wal_checkpoint"), label="WAL checkpoint")
    if (
        seal.get("journal_mode") != "delete"
        or seal.get("sidecars_present") != 0
        or seal.get("main_db_fsynced") is not True
        or seal.get("directory_fsynced") is not True
        or checkpoint.get("busy") != 0
        or not isinstance(checkpoint.get("log_frames"), int)
        or isinstance(checkpoint.get("log_frames"), bool)
        or checkpoint.get("log_frames") != checkpoint.get("checkpointed_frames")
    ):
        raise EvidenceRebuildVerificationError("candidate projection seal receipt is invalid")
    projection = root / EVIDENCE_STORE_DIRNAME / EVIDENCE_PROJECTION_FILENAME
    _assert_projection_sidecars_absent(projection)
    try:
        actual_tree = fingerprint_evidence_tree(root / EVIDENCE_STORE_DIRNAME).to_dict()
    except EvidenceActivationError as exc:
        raise EvidenceRebuildVerificationError("candidate Evidence tree cannot be fingerprinted") from exc
    if actual_tree != expected_tree:
        raise EvidenceRebuildVerificationError("candidate Evidence tree fingerprint mismatch")
    if verified_snapshot is not None:
        if receipt.get("source", {}).get("snapshot_manifest_sha256") != verified_snapshot.manifest_digest_sha256:
            raise EvidenceRebuildVerificationError("candidate source manifest digest mismatch")
        verified_snapshot.verify_unchanged()

    expected_digest = candidate_material.get("logical_digest")
    if not isinstance(expected_digest, str):
        raise EvidenceRebuildVerificationError("candidate projection logical digest is invalid")
    scratch_base = _require_owner_only_parent(
        root.parent if scratch_parent is None else scratch_parent
    )
    if scratch_base == root or scratch_base.is_relative_to(root):
        raise EvidenceRebuildSafetyError("scratch_parent must not be inside candidate_root")
    with tempfile.TemporaryDirectory(prefix="evidence-rebuild-verify-", dir=scratch_base) as temporary:
        scratch_root = Path(temporary)
        os.chmod(scratch_root, _PRIVATE_DIR_MODE)

        # The sealed candidate is never opened by SQLite.  Its exact bytes are
        # copied through no-follow descriptors and every database operation is
        # confined to this disposable owner-private bundle.
        bundle_root = scratch_root / "candidate-bundle"
        bundle_root.mkdir(mode=_PRIVATE_DIR_MODE)
        bundle_projection = bundle_root / EVIDENCE_STORE_DIRNAME / EVIDENCE_PROJECTION_FILENAME
        _copy_private_regular_file(
            projection,
            bundle_projection,
            label="candidate projection",
        )
        for filename in (EVIDENCE_SPOOL_FILENAME, REFRESHABLE_USAGE_SPOOL_FILENAME):
            source = root / EVIDENCE_STORE_DIRNAME / filename
            if source.is_file():
                _copy_private_regular_file(
                    source,
                    bundle_root / EVIDENCE_STORE_DIRNAME / filename,
                    label="candidate source spool",
                )
        _assert_projection_rollback_journal_header(bundle_projection)
        integrity = _integrity_check(bundle_projection)
        current_digest = _projection_logical_digest(bundle_projection)
        if integrity != candidate_material.get("projection_integrity"):
            raise EvidenceRebuildVerificationError("candidate projection integrity receipt mismatch")
        if current_digest != expected_digest:
            raise EvidenceRebuildVerificationError("candidate projection logical digest mismatch")
        bundle_store = EvidenceStore(bundle_root)
        if _projection_logical_digest(bundle_store.projection_path) != current_digest:
            raise EvidenceRebuildVerificationError(
                "candidate projection does not match its source spools"
            )
        bundle_generic_stats = bundle_store.stats().to_dict()
        bundle_refresh_stats = bundle_store.refreshable_usage_stats().to_dict()

        replay_root = scratch_root / "spool-replay"
        for filename in (EVIDENCE_SPOOL_FILENAME, REFRESHABLE_USAGE_SPOOL_FILENAME):
            source = bundle_root / EVIDENCE_STORE_DIRNAME / filename
            if source.is_file():
                _copy_private_regular_file(
                    source,
                    replay_root / EVIDENCE_STORE_DIRNAME / filename,
                    label="copied candidate source spool",
                )
        scratch_store = EvidenceStore(replay_root)
        replay_integrity = _integrity_check(scratch_store.projection_path)
        rebuilt_digest = _projection_logical_digest(scratch_store.projection_path)
        if rebuilt_digest != expected_digest:
            raise EvidenceRebuildVerificationError("spool-only projection rebuild logical digest mismatch")
        replay_generic_stats = scratch_store.stats().to_dict()
        replay_refresh_stats = scratch_store.refreshable_usage_stats().to_dict()
        if (
            replay_generic_stats != bundle_generic_stats
            or replay_refresh_stats != bundle_refresh_stats
        ):
            raise EvidenceRebuildVerificationError(
                "candidate projection stats do not match spool-only rebuild"
            )
        second_reconcile = _assert_second_reconcile_no_growth(scratch_store)
        claimed_second_reconcile = _receipt_mapping(
            candidate_material.get("second_reconcile"),
            label="second reconcile",
        )
        if dict(claimed_second_reconcile) != second_reconcile:
            raise EvidenceRebuildVerificationError("candidate second reconcile receipt mismatch")
        verified_isolation: Mapping[str, Any] | None = None
        verified_ledger_events: int | None = None
        if verified_snapshot is not None and externally_required_clients is not None:
            raw_import = _receipt_mapping(receipt.get("raw_import"), label="raw import")
            raw_clients = raw_import.get("clients")
            if not isinstance(raw_clients, list) or any(
                not isinstance(client, str) for client in raw_clients
            ):
                raise EvidenceRebuildVerificationError(
                    "candidate receipt raw clients are invalid"
                )
            if tuple(raw_clients) != externally_required_clients:
                raise EvidenceRebuildVerificationError(
                    "candidate receipt raw clients do not match the externally required client set"
                )
            authoritative_clients = (
                externally_required_clients if raw_import.get("complete") is True else ()
            )
            snapshot_ledger = _read_ledger(verified_snapshot)
            verified_ledger_events = len(snapshot_ledger)
            verified_isolation = _verified_candidate_fallback_isolation(
                scratch_store,
                snapshot_ledger,
                authoritative_clients=authoritative_clients,
            )
        activation_ready = _activation_ready_from_material(
            receipt,
            actual_generic_stats=replay_generic_stats,
            actual_refresh_stats=replay_refresh_stats,
            replay_integrity=replay_integrity,
            second_reconcile=second_reconcile,
            verified_isolation=verified_isolation,
            verified_ledger_events=verified_ledger_events,
        )
        _verify_readiness_declaration(receipt, activation_ready)

    # Bind every copied/read byte back to the unchanged sealed tree.  These are
    # plain file reads/fingerprints and cannot create SQLite WAL/SHM state.
    _assert_projection_sidecars_absent(projection)
    if _candidate_source_files(root) != expected_files:
        raise EvidenceRebuildVerificationError("candidate source-of-truth changed during verification")
    try:
        final_tree = fingerprint_evidence_tree(root / EVIDENCE_STORE_DIRNAME).to_dict()
    except EvidenceActivationError as exc:
        raise EvidenceRebuildVerificationError("candidate Evidence tree cannot be fingerprinted") from exc
    if final_tree != expected_tree:
        raise EvidenceRebuildVerificationError("candidate Evidence tree changed during verification")
    if _read_private_regular_file(receipt_path, label="candidate receipt") != receipt_bytes:
        raise EvidenceRebuildVerificationError("candidate receipt changed during verification")
    if verified_snapshot is not None:
        verified_snapshot.verify_unchanged()

    return {
        "status": "verified",
        "candidate_root": str(root),
        "receipt_sha256": receipt["receipt_sha256"],
        "logical_digest": current_digest,
        "projection_integrity": integrity,
        "spool_rebuild_integrity": replay_integrity,
        "second_reconcile": second_reconcile,
        "activation_ready": activation_ready,
        "activation_requires_owner_approval": True,
        "automatic_activation_performed": False,
    }


__all__ = [
    "DEFAULT_REBUILD_CLIENTS",
    "REBUILD_RECEIPT_FILENAME",
    "REBUILD_RECEIPT_SCHEMA",
    "REBUILD_FULL_HISTORY_LIMIT",
    "EvidenceRebuildCandidateError",
    "EvidenceRebuildSafetyError",
    "EvidenceRebuildVerificationError",
    "build_evidence_rebuild_candidate",
    "verify_evidence_rebuild_candidate",
]
