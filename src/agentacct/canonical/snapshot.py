"""Fail-closed boundary for offline legacy/Codex snapshots.

This module deliberately has no snapshot-creation or copy operation.  It only
accepts an explicitly named, already-created snapshot root plus a manifest,
opens inputs read-only, and proves that the bytes match that manifest before a
migration or benchmark may use them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import BinaryIO, Any

from .live_paths import LivePathSafetyError, reject_live_state_overlap


SNAPSHOT_MANIFEST_VERSION = 1
DEFAULT_SNAPSHOT_KIND = "legacy"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_FILES = 250_000
_HASH_CHUNK_BYTES = 1024 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_KIND_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}")
_TOP_LEVEL_KEYS = frozenset({"version", "kind", "files"})
_REQUIRED_TOP_LEVEL_KEYS = frozenset({"version", "files"})
_ENTRY_KEYS = frozenset({"path", "size_bytes", "sha256"})
_LIVE_FILE_SUFFIXES = (".lock", ".pid", ".lease", "-wal", "-shm", "-journal")
_LIVE_ROOT_DIRECTORIES = frozenset({"runtime"})
_FORBIDDEN_STATE_COMPONENT_SEQUENCES = (
    (".agent-sentinel", "state"),
    (".agent-sentinel-global", "state"),
    (".agent-chronicle", "state"),
    (".agent-chronicle-global", "state"),
    # New canonical global store ($XDG_STATE_HOME/agentacct/state). Matched as a
    # PAIR: the bare name "agentacct" is too generic (pkg dir / repo dir) to add
    # to the single-component root list below.
    ("agentacct", "state"),
)
_FORBIDDEN_LIVE_ROOT_COMPONENTS = frozenset(
    {
        ".agent-sentinel",
        ".agent-sentinel-global",
        ".agent-chronicle",
        ".agent-chronicle-global",
        ".codex",
    }
)


class SnapshotSafetyError(ValueError):
    """Base error for a snapshot or candidate path that cannot be trusted."""


class ManifestValidationError(SnapshotSafetyError):
    """The manifest is malformed, ambiguous, or not a safe regular file."""


class SnapshotVerificationError(SnapshotSafetyError):
    """The snapshot tree or one of its files does not match the manifest."""


class CandidateTargetError(SnapshotSafetyError):
    """A candidate database target is not isolated in the declared scratch root."""


@dataclass(frozen=True, slots=True)
class SnapshotManifestEntry:
    """One canonical, root-relative regular file declared by a manifest."""

    path: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        canonical = _canonical_relative_path(self.path, error_type=ManifestValidationError)
        if canonical != self.path:
            raise ManifestValidationError(f"manifest path is not canonical: {self.path!r}")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes < 0:
            raise ManifestValidationError(f"manifest size_bytes is invalid for {self.path!r}")
        if not isinstance(self.sha256, str) or _SHA256_RE.fullmatch(self.sha256) is None:
            raise ManifestValidationError(f"manifest sha256 is invalid for {self.path!r}")


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    """Strictly parsed manifest, including the digest of its exact bytes."""

    path: Path
    version: int
    kind: str
    entries: tuple[SnapshotManifestEntry, ...]
    digest_sha256: str
    # Generic snapshot consumers keep accepting manifests from before ``kind``
    # was introduced, but safety-sensitive parity runners can require the
    # declaration rather than trusting DEFAULT_SNAPSHOT_KIND.
    kind_declared: bool = True

    @property
    def files(self) -> tuple[SnapshotManifestEntry, ...]:
        """Compatibility-friendly name for callers that treat entries as files."""

        return self.entries

    @classmethod
    def load(cls, path: Path | str) -> "SnapshotManifest":
        manifest_path = _require_absolute(path, label="manifest", error_type=ManifestValidationError)
        _reject_chronicle_state_path(manifest_path, label="manifest", error_type=ManifestValidationError)
        manifest_path = _canonical_absolute_path(
            manifest_path,
            label="manifest",
            error_type=ManifestValidationError,
        )
        raw = _read_standalone_regular_file(manifest_path)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ManifestValidationError(
                f"manifest exceeds the {MAX_MANIFEST_BYTES}-byte safety limit"
            )
        try:
            text = raw.decode("utf-8", errors="strict")
            value = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManifestValidationError(f"manifest is not strict UTF-8 JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise ManifestValidationError("manifest must be a JSON object")

        keys = frozenset(value)
        missing = _REQUIRED_TOP_LEVEL_KEYS - keys
        unknown = keys - _TOP_LEVEL_KEYS
        if missing:
            raise ManifestValidationError(
                "manifest is missing required fields: " + ", ".join(sorted(missing))
            )
        if unknown:
            raise ManifestValidationError(
                "manifest contains unknown fields: " + ", ".join(sorted(unknown))
            )

        version = value["version"]
        if isinstance(version, bool) or version != SNAPSHOT_MANIFEST_VERSION:
            raise ManifestValidationError(
                f"unsupported manifest version: {version!r}; expected {SNAPSHOT_MANIFEST_VERSION}"
            )
        kind_declared = "kind" in value
        kind = value.get("kind", DEFAULT_SNAPSHOT_KIND)
        if not isinstance(kind, str) or _KIND_RE.fullmatch(kind) is None:
            raise ManifestValidationError("manifest kind must be a short lowercase identifier")
        files_value = value["files"]
        if not isinstance(files_value, list):
            raise ManifestValidationError("manifest files must be a JSON array")
        if not files_value:
            raise ManifestValidationError("manifest must declare at least one file")
        if len(files_value) > MAX_MANIFEST_FILES:
            raise ManifestValidationError(
                f"manifest exceeds the {MAX_MANIFEST_FILES}-file safety limit"
            )

        entries: list[SnapshotManifestEntry] = []
        seen: set[str] = set()
        for index, item in enumerate(files_value):
            if not isinstance(item, dict):
                raise ManifestValidationError(f"manifest files[{index}] must be an object")
            item_keys = frozenset(item)
            if item_keys != _ENTRY_KEYS:
                missing_entry = _ENTRY_KEYS - item_keys
                unknown_entry = item_keys - _ENTRY_KEYS
                details: list[str] = []
                if missing_entry:
                    details.append("missing " + ", ".join(sorted(missing_entry)))
                if unknown_entry:
                    details.append("unknown " + ", ".join(sorted(unknown_entry)))
                raise ManifestValidationError(
                    f"manifest files[{index}] fields are invalid ({'; '.join(details)})"
                )
            entry = SnapshotManifestEntry(
                path=item["path"],
                size_bytes=item["size_bytes"],
                sha256=item["sha256"],
            )
            if entry.path in seen:
                raise ManifestValidationError(f"manifest contains duplicate path: {entry.path!r}")
            seen.add(entry.path)
            entries.append(entry)

        return cls(
            path=manifest_path,
            version=SNAPSHOT_MANIFEST_VERSION,
            kind=kind,
            entries=tuple(entries),
            digest_sha256=hashlib.sha256(raw).hexdigest(),
            kind_declared=kind_declared,
        )


@dataclass(frozen=True, slots=True)
class VerifiedSnapshotFile:
    """A manifest file whose bytes and stable filesystem identity were verified."""

    relative_path: str
    path: Path
    size_bytes: int
    sha256: str
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class VerifiedSnapshot:
    """An offline snapshot proven against a strict SHA-256 manifest.

    Construct instances through :meth:`verify`.  ``path_for`` only resolves
    files present in the verified manifest; it never creates or opens a file.
    Callers that need a Python handle can use ``open_binary`` to get a
    descriptor opened read-only with no symlink following.
    """

    root: Path
    root_device: int
    root_inode: int
    manifest: SnapshotManifest
    files: tuple[VerifiedSnapshotFile, ...]
    _files_by_path: Mapping[str, VerifiedSnapshotFile] = field(
        repr=False,
        compare=False,
        hash=False,
    )

    @property
    def kind(self) -> str:
        return self.manifest.kind

    @property
    def manifest_kind(self) -> str:
        return self.manifest.kind

    @property
    def manifest_kind_declared(self) -> bool:
        return self.manifest.kind_declared

    @property
    def manifest_digest(self) -> str:
        return self.manifest.digest_sha256

    @property
    def manifest_digest_sha256(self) -> str:
        return self.manifest.digest_sha256

    @classmethod
    def verify(
        cls,
        root: Path | str,
        manifest: Path | str | SnapshotManifest,
    ) -> "VerifiedSnapshot":
        snapshot_root = _require_absolute(
            root,
            label="snapshot root",
            error_type=SnapshotVerificationError,
        )
        _reject_chronicle_state_path(
            snapshot_root,
            label="snapshot root",
            error_type=SnapshotVerificationError,
        )
        snapshot_root = _canonical_absolute_path(
            snapshot_root,
            label="snapshot root",
            error_type=SnapshotVerificationError,
        )
        root_descriptor = _open_absolute_directory(
            snapshot_root,
            label="snapshot root",
            error_type=SnapshotVerificationError,
        )
        try:
            root_stat = os.fstat(root_descriptor)

            # Refuse copied runtime/lock state and every non-regular object
            # before opening manifest-declared payloads.  Inventory and hashes
            # deliberately share this one pinned root descriptor.
            inventory = _scan_snapshot_tree(root_descriptor, snapshot_root)

            if isinstance(manifest, SnapshotManifest):
                loaded_manifest = SnapshotManifest.load(manifest.path)
                if loaded_manifest.digest_sha256 != manifest.digest_sha256:
                    raise ManifestValidationError("manifest changed after it was loaded")
            else:
                loaded_manifest = SnapshotManifest.load(manifest)

            if loaded_manifest.path == snapshot_root or loaded_manifest.path.is_relative_to(snapshot_root):
                raise ManifestValidationError("snapshot manifest must be outside the snapshot root")
            declared_paths = {entry.path for entry in loaded_manifest.entries}
            inventory_paths = set(inventory.files)
            if inventory_paths != declared_paths:
                missing = len(declared_paths - inventory_paths)
                unlisted = len(inventory_paths - declared_paths)
                raise SnapshotVerificationError(
                    "snapshot tree must exactly match the manifest "
                    f"(missing={missing}, unlisted={unlisted})"
                )

            verified_files = tuple(
                _verify_manifest_entry(root_descriptor, snapshot_root, entry)
                for entry in loaded_manifest.entries
            )
            _assert_verified_files_match_inventory(verified_files, inventory)
            final_inventory = _scan_snapshot_tree(root_descriptor, snapshot_root)
            if final_inventory != inventory:
                raise SnapshotVerificationError(
                    "snapshot tree identity changed during verification"
                )
            _reprove_directory_path(
                snapshot_root,
                expected=(root_stat.st_dev, root_stat.st_ino),
                label="snapshot root",
                error_type=SnapshotVerificationError,
            )
        finally:
            os.close(root_descriptor)
        by_path = MappingProxyType({item.relative_path: item for item in verified_files})
        return cls(
            root=snapshot_root,
            root_device=root_stat.st_dev,
            root_inode=root_stat.st_ino,
            manifest=loaded_manifest,
            files=verified_files,
            _files_by_path=by_path,
        )

    @classmethod
    def from_manifest(
        cls,
        root: Path | str,
        manifest: Path | str | SnapshotManifest,
    ) -> "VerifiedSnapshot":
        """Readable alias for :meth:`verify`; it performs the same checks."""

        return cls.verify(root=root, manifest=manifest)

    def path_for(self, relative_path: str | PurePosixPath) -> Path:
        canonical = _canonical_relative_path(
            str(relative_path),
            error_type=SnapshotVerificationError,
        )
        try:
            return self._files_by_path[canonical].path
        except KeyError as exc:
            raise SnapshotVerificationError(
                f"path is not declared by the verified manifest: {canonical!r}"
            ) from exc

    def verify_unchanged(self) -> "VerifiedSnapshot":
        """Re-hash the manifest and all payloads, rejecting any replacement."""

        current_manifest = SnapshotManifest.load(self.manifest.path)
        if current_manifest.digest_sha256 != self.manifest.digest_sha256:
            raise SnapshotVerificationError("snapshot manifest changed after verification")
        root_descriptor = _open_verified_root(self)
        try:
            inventory = _scan_snapshot_tree(root_descriptor, self.root)
            declared_paths = {item.relative_path for item in self.files}
            inventory_paths = set(inventory.files)
            if inventory_paths != declared_paths:
                missing = len(declared_paths - inventory_paths)
                unlisted = len(inventory_paths - declared_paths)
                raise SnapshotVerificationError(
                    "snapshot tree changed after verification "
                    f"(missing={missing}, unlisted={unlisted})"
                )
            for previous in self.files:
                current = _verify_manifest_entry(
                    root_descriptor,
                    self.root,
                    SnapshotManifestEntry(
                        path=previous.relative_path,
                        size_bytes=previous.size_bytes,
                        sha256=previous.sha256,
                    ),
                )
                if _verified_file_identity(current) != _verified_file_identity(previous):
                    raise SnapshotVerificationError(
                        f"snapshot file identity changed after verification: {previous.relative_path!r}"
                    )
            final_inventory = _scan_snapshot_tree(root_descriptor, self.root)
            if final_inventory != inventory:
                raise SnapshotVerificationError(
                    "snapshot tree identity changed during reverification"
                )
            _reprove_directory_path(
                self.root,
                expected=(self.root_device, self.root_inode),
                label="snapshot root",
                error_type=SnapshotVerificationError,
            )
        finally:
            os.close(root_descriptor)
        return self

    @contextmanager
    def open_binary(self, relative_path: str | PurePosixPath) -> Iterator[BinaryIO]:
        """Open one already-verified file read-only without following symlinks."""

        canonical = _canonical_relative_path(
            str(relative_path),
            error_type=SnapshotVerificationError,
        )
        try:
            expected = self._files_by_path[canonical]
        except KeyError as exc:
            raise SnapshotVerificationError(
                f"path is not declared by the verified manifest: {canonical!r}"
            ) from exc
        root_descriptor = _open_verified_root(self)
        descriptor = -1
        try:
            descriptor = _open_file_beneath(
                root_descriptor,
                PurePosixPath(canonical).parts,
            )
            current = os.fstat(descriptor)
            if not stat.S_ISREG(current.st_mode):
                raise SnapshotVerificationError(f"snapshot path is not a regular file: {canonical!r}")
            if (
                current.st_dev,
                current.st_ino,
                current.st_size,
                current.st_mtime_ns,
                current.st_ctime_ns,
            ) != (
                expected.device,
                expected.inode,
                expected.size_bytes,
                expected.mtime_ns,
                expected.ctime_ns,
            ):
                raise SnapshotVerificationError(
                    f"snapshot file changed after verification: {canonical!r}"
                )
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                descriptor = -1
                try:
                    yield handle
                finally:
                    try:
                        after = os.fstat(handle.fileno())
                        if (
                            after.st_dev,
                            after.st_ino,
                            after.st_size,
                            after.st_mtime_ns,
                            after.st_ctime_ns,
                        ) != (
                            expected.device,
                            expected.inode,
                            expected.size_bytes,
                            expected.mtime_ns,
                            expected.ctime_ns,
                        ):
                            raise SnapshotVerificationError(
                                f"snapshot file changed while open: {canonical!r}"
                            )
                        handle.seek(0)
                        digest = hashlib.sha256()
                        while block := handle.read(_HASH_CHUNK_BYTES):
                            digest.update(block)
                        if digest.hexdigest() != expected.sha256:
                            raise SnapshotVerificationError(
                                f"snapshot file content changed while open: {canonical!r}"
                            )
                    except OSError as exc:
                        raise SnapshotVerificationError(
                            f"snapshot file could not be reverified while open: {canonical!r}"
                        ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                replacement_descriptor = -1
                try:
                    replacement_descriptor = _open_file_beneath(
                        root_descriptor,
                        PurePosixPath(canonical).parts,
                    )
                    replacement = os.fstat(replacement_descriptor)
                    if _stat_identity(replacement) != _verified_file_stat_identity(expected):
                        raise SnapshotVerificationError(
                            f"snapshot file identity changed while open: {canonical!r}"
                        )
                finally:
                    if replacement_descriptor >= 0:
                        os.close(replacement_descriptor)
                _reprove_directory_path(
                    self.root,
                    expected=(self.root_device, self.root_inode),
                    label="snapshot root",
                    error_type=SnapshotVerificationError,
                )
            finally:
                os.close(root_descriptor)

    def validate_candidate_target(
        self,
        target: Path | str,
        *,
        scratch_root: Path | str,
    ) -> Path:
        return validate_candidate_target(self, target, scratch_root)


def verify_snapshot(
    root: Path | str,
    manifest: Path | str | SnapshotManifest,
) -> VerifiedSnapshot:
    """Functional entry point for strict snapshot verification."""

    return VerifiedSnapshot.verify(root=root, manifest=manifest)


def validate_candidate_target(
    snapshot: VerifiedSnapshot,
    target: Path | str,
    scratch_root: Path | str,
) -> Path:
    """Validate an already-declared offline candidate database destination.

    The scratch root and target must both be explicit absolute paths.  This
    function creates nothing and never removes or truncates an existing file.
    """

    if not isinstance(snapshot, VerifiedSnapshot):
        raise CandidateTargetError("snapshot must be a VerifiedSnapshot")
    scratch = _require_absolute(
        scratch_root,
        label="scratch root",
        error_type=CandidateTargetError,
    )
    candidate = _require_absolute(
        target,
        label="candidate target",
        error_type=CandidateTargetError,
    )
    _reject_chronicle_state_path(scratch, label="scratch root", error_type=CandidateTargetError)
    _reject_chronicle_state_path(candidate, label="candidate target", error_type=CandidateTargetError)
    _assert_existing_path_without_symlinks(
        scratch,
        label="scratch root",
        error_type=CandidateTargetError,
    )
    scratch_stat = scratch.stat(follow_symlinks=False)
    if not stat.S_ISDIR(scratch_stat.st_mode):
        raise CandidateTargetError("scratch root must be a directory")
    scratch = scratch.resolve(strict=True)

    _assert_existing_path_without_symlinks(
        candidate.parent,
        label="candidate parent",
        error_type=CandidateTargetError,
    )
    parent_stat = candidate.parent.stat(follow_symlinks=False)
    if not stat.S_ISDIR(parent_stat.st_mode):
        raise CandidateTargetError("candidate parent must be a directory")
    lexical_candidate = candidate
    try:
        lexical_stat = lexical_candidate.lstat()
    except FileNotFoundError:
        lexical_stat = None
    except OSError as exc:
        raise CandidateTargetError(f"cannot inspect candidate target: {exc}") from exc
    if lexical_stat is not None and stat.S_ISLNK(lexical_stat.st_mode):
        raise CandidateTargetError("candidate target must not be a symlink")
    candidate = candidate.resolve(strict=False)

    if candidate == scratch or not candidate.is_relative_to(scratch):
        raise CandidateTargetError("candidate target must be strictly under the explicit scratch root")
    if candidate == snapshot.root or candidate.is_relative_to(snapshot.root):
        raise CandidateTargetError("candidate target must be outside the verified snapshot")
    if candidate == snapshot.manifest.path:
        raise CandidateTargetError("candidate target must not overwrite the snapshot manifest")
    relative_candidate = candidate.relative_to(scratch)
    if _is_live_marker_parts(relative_candidate.parts):
        raise CandidateTargetError("candidate target resembles live agentacct runtime state")

    candidate_stat = lexical_stat
    if candidate_stat is not None:
        if not stat.S_ISREG(candidate_stat.st_mode):
            raise CandidateTargetError("existing candidate target must be a regular file")
        source_identities = {(item.device, item.inode) for item in snapshot.files}
        if (candidate_stat.st_dev, candidate_stat.st_ino) in source_identities:
            raise CandidateTargetError("candidate target aliases a verified snapshot file")
        if candidate_stat.st_nlink != 1:
            raise CandidateTargetError("existing candidate target must not be a hard-linked alias")

    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = candidate.with_name(candidate.name + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise CandidateTargetError(
                f"candidate target has a live SQLite sidecar: {sidecar.name}"
            )
    return candidate


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestValidationError(f"manifest contains duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _require_absolute(
    path: Path | str,
    *,
    label: str,
    error_type: type[SnapshotSafetyError],
) -> Path:
    try:
        result = Path(path)
    except TypeError as exc:
        raise error_type(f"{label} must be a filesystem path") from exc
    if not result.is_absolute():
        raise error_type(f"{label} must be an explicit absolute path")
    return result


def _canonical_relative_path(
    value: str,
    *,
    error_type: type[SnapshotSafetyError],
) -> str:
    if not isinstance(value, str) or not value:
        raise error_type("manifest path must be a non-empty string")
    if "\\" in value or any(ord(character) < 32 for character in value):
        raise error_type(f"manifest path contains forbidden characters: {value!r}")
    pure = PurePosixPath(value)
    if pure.is_absolute() or value.startswith("/"):
        raise error_type(f"manifest path must be relative: {value!r}")
    if any(part in {"", ".", ".."} for part in pure.parts):
        raise error_type(f"manifest path may not contain dot traversal: {value!r}")
    canonical = pure.as_posix()
    if canonical in {"", "."} or canonical != value:
        raise error_type(f"manifest path is not canonical: {value!r}")
    return canonical


def _contains_component_sequence(parts: tuple[str, ...], sequence: tuple[str, ...]) -> bool:
    folded = tuple(part.casefold() for part in parts)
    wanted = tuple(part.casefold() for part in sequence)
    width = len(wanted)
    return any(folded[index : index + width] == wanted for index in range(len(folded) - width + 1))


def _reject_chronicle_state_path(
    path: Path,
    *,
    label: str,
    error_type: type[SnapshotSafetyError],
) -> None:
    candidates = [path]
    try:
        candidates.append(path.resolve(strict=False))
    except (OSError, RuntimeError):
        pass
    for candidate in candidates:
        for sequence in _FORBIDDEN_STATE_COMPONENT_SEQUENCES:
            if _contains_component_sequence(candidate.parts, sequence):
                raise error_type(f"{label} may not be inside {'/'.join(sequence)}")
        forbidden_components = {
            part.casefold()
            for part in candidate.parts
            if part.casefold() in _FORBIDDEN_LIVE_ROOT_COMPONENTS
        }
        if forbidden_components:
            component = sorted(forbidden_components)[0]
            product = "Codex" if component == ".codex" else "agentacct"
            raise error_type(
                f"{label} may not be inside a live {product} state root ({component})"
            )
        for live_root in _configured_live_codex_roots():
            if candidate == live_root or candidate.is_relative_to(live_root):
                raise error_type(f"{label} may not be inside the configured live Codex state root")
    try:
        reject_live_state_overlap(path, label=label)
    except LivePathSafetyError as exc:
        raise error_type(str(exc)) from exc


def _configured_live_codex_roots() -> tuple[Path, ...]:
    """Return live Codex roots without assuming CODEX_HOME is named ``.codex``."""

    roots = {(Path.home() / ".codex").resolve(strict=False)}
    configured = os.environ.get("CODEX_HOME")
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        roots.add(candidate.resolve(strict=False))
    return tuple(roots)


def _assert_existing_path_without_symlinks(
    path: Path,
    *,
    label: str,
    error_type: type[SnapshotSafetyError],
) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            current_stat = current.lstat()
        except FileNotFoundError as exc:
            raise error_type(f"{label} does not exist: {path}") from exc
        except OSError as exc:
            raise error_type(f"cannot inspect {label}: {exc}") from exc
        if stat.S_ISLNK(current_stat.st_mode):
            raise error_type(f"{label} may not contain symlink components: {current}")


def _canonical_absolute_path(
    path: Path,
    *,
    label: str,
    error_type: type[SnapshotSafetyError],
) -> Path:
    """Return a lexical absolute path that descriptor traversal can prove.

    ``Path.resolve`` is intentionally not the authority here: resolving first
    and opening later leaves a parent-component swap window.  Dot traversal is
    refused so the lexical path and the descriptor walk name the same object.
    """

    if any(part == ".." for part in path.parts):
        raise error_type(f"{label} may not contain parent traversal")
    canonical = Path(os.path.abspath(os.fspath(path)))
    _reject_chronicle_state_path(canonical, label=label, error_type=error_type)
    return canonical


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )


def _readonly_file_open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_absolute_directory(
    path: Path,
    *,
    label: str,
    error_type: type[SnapshotSafetyError],
) -> int:
    """Open every absolute directory component relative to a pinned parent fd."""

    if not path.is_absolute():
        raise error_type(f"{label} must be an explicit absolute path")
    flags = _directory_open_flags()
    anchor = Path(path.anchor)
    try:
        descriptor = os.open(anchor, flags)
    except OSError as exc:
        raise error_type(f"cannot open {label} anchor read-only: {exc}") from exc
    try:
        for part in path.parts[1:]:
            try:
                before = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            except OSError as exc:
                raise error_type(f"cannot inspect {label} component {part!r}: {exc}") from exc
            if stat.S_ISLNK(before.st_mode):
                raise error_type(f"{label} may not contain symlink components: {part!r}")
            if not stat.S_ISDIR(before.st_mode):
                raise error_type(f"{label} must be a directory")
            try:
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
            except OSError as exc:
                raise error_type(f"cannot open {label} component {part!r}: {exc}") from exc
            try:
                opened = os.fstat(next_descriptor)
                current = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                expected_identity = (before.st_dev, before.st_ino)
                if (
                    (opened.st_dev, opened.st_ino) != expected_identity
                    or (current.st_dev, current.st_ino) != expected_identity
                ):
                    raise error_type(f"{label} component identity changed while opening: {part!r}")
            except BaseException:
                os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _reprove_directory_path(
    path: Path,
    *,
    expected: tuple[int, int],
    label: str,
    error_type: type[SnapshotSafetyError],
) -> None:
    _reject_chronicle_state_path(path, label=label, error_type=error_type)
    descriptor = _open_absolute_directory(path, label=label, error_type=error_type)
    try:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != expected:
            raise error_type(f"{label} identity changed")
    finally:
        os.close(descriptor)


def _read_standalone_regular_file(path: Path) -> bytes:
    parent_descriptor = _open_absolute_directory(
        path.parent,
        label="manifest parent",
        error_type=ManifestValidationError,
    )
    descriptor = -1
    try:
        try:
            named_before = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except OSError as exc:
            raise ManifestValidationError(f"cannot inspect manifest: {exc}") from exc
        if stat.S_ISLNK(named_before.st_mode):
            raise ManifestValidationError("manifest must not be a symlink")
        try:
            descriptor = os.open(
                path.name,
                _readonly_file_open_flags(),
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise ManifestValidationError(f"cannot open manifest read-only: {exc}") from exc
        before = os.fstat(descriptor)
        named_after_open = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            _stat_identity(named_before) != _stat_identity(before)
            or _stat_identity(named_after_open) != _stat_identity(before)
        ):
            raise ManifestValidationError("manifest identity changed while it was being opened")
        if not stat.S_ISREG(before.st_mode):
            raise ManifestValidationError("manifest must be a regular file")
        if before.st_nlink != 1:
            raise ManifestValidationError("manifest must not be a hard-linked alias")
        chunks: list[bytes] = []
        remaining = MAX_MANIFEST_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        named_after_read = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            _stat_identity(before) != _stat_identity(after)
            or _stat_identity(named_after_read) != _stat_identity(before)
        ):
            raise ManifestValidationError("manifest changed while it was being read")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)

    # The descriptor above pins the safe parent even if its pathname is
    # renamed.  Re-open component-by-component before accepting the bytes so a
    # parent replacement cannot make ``manifest.path`` point elsewhere.
    _reprove_regular_file_path(
        path,
        expected=_stat_identity(before),
        label="manifest",
        error_type=ManifestValidationError,
    )
    _reject_chronicle_state_path(path, label="manifest", error_type=ManifestValidationError)
    return raw


def _reprove_regular_file_path(
    path: Path,
    *,
    expected: tuple[int, int, int, int, int],
    label: str,
    error_type: type[SnapshotSafetyError],
) -> None:
    parent_descriptor = _open_absolute_directory(
        path.parent,
        label=f"{label} parent",
        error_type=error_type,
    )
    try:
        try:
            current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except OSError as exc:
            raise error_type(f"cannot reprove {label}: {exc}") from exc
        if stat.S_ISLNK(current.st_mode) or _stat_identity(current) != expected:
            raise error_type(f"{label} identity changed")
    finally:
        os.close(parent_descriptor)


@dataclass(frozen=True, slots=True)
class _SnapshotInventory:
    files: Mapping[str, tuple[int, int, int, int, int]]
    directories: Mapping[str, tuple[int, int, int, int, int]]


def _scan_snapshot_tree(root_descriptor: int, root: Path) -> _SnapshotInventory:
    """Inventory beneath one pinned root fd without ever rescanning a pathname."""

    files: dict[str, tuple[int, int, int, int, int]] = {}
    directories: dict[str, tuple[int, int, int, int, int]] = {}

    def scan(directory_descriptor: int, relative_parts: tuple[str, ...]) -> None:
        before_directory = os.fstat(directory_descriptor)
        if not stat.S_ISDIR(before_directory.st_mode):
            raise SnapshotVerificationError("snapshot directory descriptor is not a directory")
        directory_key = PurePosixPath(*relative_parts).as_posix() if relative_parts else "."
        directories[directory_key] = _stat_identity(before_directory)
        try:
            with os.scandir(directory_descriptor) as children:
                for child in children:
                    child_parts = (*relative_parts, child.name)
                    relative_path = PurePosixPath(*child_parts).as_posix()
                    display_path = root.joinpath(*child_parts)
                    _reject_forbidden_snapshot_parts(child_parts, display_path)
                    try:
                        child_stat = os.stat(
                            child.name,
                            dir_fd=directory_descriptor,
                            follow_symlinks=False,
                        )
                    except OSError as exc:
                        raise SnapshotVerificationError(
                            f"cannot inspect snapshot path {display_path}: {exc}"
                        ) from exc
                    if stat.S_ISLNK(child_stat.st_mode):
                        raise SnapshotVerificationError(
                            f"snapshot may not contain symlinks: {display_path}"
                        )
                    if stat.S_ISDIR(child_stat.st_mode):
                        try:
                            child_descriptor = os.open(
                                child.name,
                                _directory_open_flags(),
                                dir_fd=directory_descriptor,
                            )
                        except OSError as exc:
                            raise SnapshotVerificationError(
                                f"cannot open snapshot directory {display_path}: {exc}"
                            ) from exc
                        try:
                            opened = os.fstat(child_descriptor)
                            current = os.stat(
                                child.name,
                                dir_fd=directory_descriptor,
                                follow_symlinks=False,
                            )
                            if (
                                (opened.st_dev, opened.st_ino)
                                != (child_stat.st_dev, child_stat.st_ino)
                                or (current.st_dev, current.st_ino)
                                != (child_stat.st_dev, child_stat.st_ino)
                            ):
                                raise SnapshotVerificationError(
                                    f"snapshot directory identity changed while opening: {relative_path!r}"
                                )
                            scan(child_descriptor, child_parts)
                            opened_after = os.fstat(child_descriptor)
                            named_after = os.stat(
                                child.name,
                                dir_fd=directory_descriptor,
                                follow_symlinks=False,
                            )
                            if (
                                _stat_identity(opened_after) != _stat_identity(opened)
                                or _stat_identity(named_after) != _stat_identity(opened)
                            ):
                                raise SnapshotVerificationError(
                                    f"snapshot directory identity changed while scanning: {relative_path!r}"
                                )
                        finally:
                            os.close(child_descriptor)
                    elif not stat.S_ISREG(child_stat.st_mode):
                        raise SnapshotVerificationError(
                            f"snapshot may contain only directories and regular files: {display_path}"
                        )
                    elif child_stat.st_nlink != 1:
                        raise SnapshotVerificationError(
                            f"snapshot may not contain hard-linked file aliases: {display_path}"
                        )
                    else:
                        files[relative_path] = _stat_identity(child_stat)
        except SnapshotVerificationError:
            raise
        except OSError as exc:
            raise SnapshotVerificationError(
                f"cannot scan snapshot directory {root.joinpath(*relative_parts)}: {exc}"
            ) from exc
        after_directory = os.fstat(directory_descriptor)
        if _stat_identity(before_directory) != _stat_identity(after_directory):
            raise SnapshotVerificationError(
                f"snapshot directory identity changed while scanning: {directory_key!r}"
            )

    scan(root_descriptor, ())
    return _SnapshotInventory(
        files=MappingProxyType(files),
        directories=MappingProxyType(directories),
    )


def _reject_forbidden_snapshot_parts(parts: tuple[str, ...], display_path: Path) -> None:
    for sequence in _FORBIDDEN_STATE_COMPONENT_SEQUENCES:
        if _contains_component_sequence(parts, sequence):
            raise SnapshotVerificationError(
                f"snapshot path may not be inside {'/'.join(sequence)}: {display_path}"
            )
    forbidden = next(
        (part for part in parts if part.casefold() in _FORBIDDEN_LIVE_ROOT_COMPONENTS),
        None,
    )
    if forbidden is not None:
        raise SnapshotVerificationError(
            f"snapshot path may not contain a live state root component: {display_path}"
        )
    if _is_live_marker_parts(parts):
        raise SnapshotVerificationError(
            f"snapshot contains live agentacct runtime marker: {display_path}"
        )


def _is_live_marker_parts(parts: tuple[str, ...]) -> bool:
    if not parts:
        return False
    folded = tuple(part.casefold() for part in parts)
    if folded[0] in _LIVE_ROOT_DIRECTORIES:
        return True
    name = folded[-1]
    return any(name.endswith(suffix) for suffix in _LIVE_FILE_SUFFIXES)


def _open_file_beneath(root_descriptor: int, parts: tuple[str, ...]) -> int:
    if not parts:
        raise SnapshotVerificationError("snapshot relative path is empty")
    try:
        directory_descriptor = os.dup(root_descriptor)
    except OSError as exc:
        raise SnapshotVerificationError(f"cannot duplicate snapshot root descriptor: {exc}") from exc
    try:
        for part in parts[:-1]:
            next_descriptor = -1
            try:
                before = os.stat(part, dir_fd=directory_descriptor, follow_symlinks=False)
                if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                    raise SnapshotVerificationError(
                        f"snapshot directory component is not a real directory: {part!r}"
                    )
                next_descriptor = os.open(
                    part,
                    _directory_open_flags(),
                    dir_fd=directory_descriptor,
                )
            except OSError as exc:
                raise SnapshotVerificationError(
                    f"cannot open snapshot directory component {part!r}: {exc}"
                ) from exc
            try:
                opened = os.fstat(next_descriptor)
                current = os.stat(part, dir_fd=directory_descriptor, follow_symlinks=False)
                if (
                    (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                    or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
                ):
                    raise SnapshotVerificationError(
                        f"snapshot directory component identity changed: {part!r}"
                    )
            except BaseException:
                os.close(next_descriptor)
                raise
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor
        result = -1
        try:
            before_file = os.stat(
                parts[-1],
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(before_file.st_mode):
                raise SnapshotVerificationError(
                    f"snapshot file must not be a symlink: {PurePosixPath(*parts).as_posix()!r}"
                )
            result = os.open(
                parts[-1],
                _readonly_file_open_flags(),
                dir_fd=directory_descriptor,
            )
        except OSError as exc:
            raise SnapshotVerificationError(
                f"cannot open snapshot file {PurePosixPath(*parts).as_posix()!r}: {exc}"
            ) from exc
        try:
            opened_file = os.fstat(result)
            current_file = os.stat(
                parts[-1],
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (
                _stat_identity(opened_file) != _stat_identity(before_file)
                or _stat_identity(current_file) != _stat_identity(before_file)
            ):
                raise SnapshotVerificationError(
                    f"snapshot file identity changed while opening: {PurePosixPath(*parts).as_posix()!r}"
                )
        except BaseException:
            os.close(result)
            raise
        return result
    finally:
        os.close(directory_descriptor)


def _verify_manifest_entry(
    root_descriptor: int,
    root: Path,
    entry: SnapshotManifestEntry,
) -> VerifiedSnapshotFile:
    parts = PurePosixPath(entry.path).parts
    if _is_live_marker_parts(parts):
        raise SnapshotVerificationError(
            f"manifest path resembles live agentacct runtime state: {entry.path!r}"
        )
    absolute_path = root.joinpath(*parts)
    _reject_forbidden_snapshot_parts(parts, absolute_path)
    descriptor = _open_file_beneath(root_descriptor, parts)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SnapshotVerificationError(
                f"manifest path is not a regular file: {entry.path!r}"
            )
        if before.st_nlink != 1:
            raise SnapshotVerificationError(
                f"manifest path is a hard-linked alias: {entry.path!r}"
            )
        if before.st_size != entry.size_bytes:
            raise SnapshotVerificationError(
                f"snapshot size mismatch for {entry.path!r}: "
                f"expected {entry.size_bytes}, found {before.st_size}"
            )
        digest = hashlib.sha256()
        observed_bytes = 0
        while True:
            chunk = os.read(descriptor, _HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            observed_bytes += len(chunk)
        after = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(after):
            raise SnapshotVerificationError(
                f"snapshot file changed while hashing: {entry.path!r}"
            )
        if observed_bytes != entry.size_bytes:
            raise SnapshotVerificationError(
                f"snapshot byte count changed while hashing: {entry.path!r}"
            )
        observed_digest = digest.hexdigest()
        if observed_digest != entry.sha256:
            raise SnapshotVerificationError(
                f"snapshot SHA-256 mismatch for {entry.path!r}: "
                f"expected {entry.sha256}, found {observed_digest}"
            )
        return VerifiedSnapshotFile(
            relative_path=entry.path,
            path=absolute_path,
            size_bytes=entry.size_bytes,
            sha256=entry.sha256,
            device=before.st_dev,
            inode=before.st_ino,
            mtime_ns=before.st_mtime_ns,
            ctime_ns=before.st_ctime_ns,
        )
    finally:
        os.close(descriptor)


def _verified_file_stat_identity(
    value: VerifiedSnapshotFile,
) -> tuple[int, int, int, int, int]:
    return (
        value.device,
        value.inode,
        value.size_bytes,
        value.mtime_ns,
        value.ctime_ns,
    )


def _verified_file_identity(
    value: VerifiedSnapshotFile,
) -> tuple[int, int, int, int, int]:
    return _verified_file_stat_identity(value)


def _assert_verified_files_match_inventory(
    files: tuple[VerifiedSnapshotFile, ...],
    inventory: _SnapshotInventory,
) -> None:
    for item in files:
        if inventory.files.get(item.relative_path) != _verified_file_stat_identity(item):
            raise SnapshotVerificationError(
                f"snapshot file identity changed during verification: {item.relative_path!r}"
            )


def _open_verified_root(snapshot: VerifiedSnapshot) -> int:
    _reject_chronicle_state_path(
        snapshot.root,
        label="snapshot root",
        error_type=SnapshotVerificationError,
    )
    descriptor = _open_absolute_directory(
        snapshot.root,
        label="snapshot root",
        error_type=SnapshotVerificationError,
    )
    try:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) == (snapshot.root_device, snapshot.root_inode):
            return descriptor
    except BaseException:
        os.close(descriptor)
        raise
    os.close(descriptor)
    raise SnapshotVerificationError("snapshot root identity changed after verification")


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
